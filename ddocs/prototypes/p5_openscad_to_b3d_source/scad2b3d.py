#!/usr/bin/env python3
"""
scad2b3d.py — prototype OpenSCAD -> build123d *source* transpiler.

Reads a `.scad` file, parses it with scad2py's PLY front end, and emits a
readable, runnable build123d Python program (algebra API).

This is a DERISKING PROTOTYPE for the question "is build123d a worthwhile
*transpilation target* for scad2py?". It is intentionally limited:

  * It targets build123d's ALGEBRA API (`Box(...) + Sphere(...)`), which is the
    closest fit for OpenSCAD's expression-oriented nesting.
  * It covers the common subset: 3D/2D primitives, booleans, transforms,
    color, linear_extrude/rotate_extrude, top-level `for`/`if`, and a
    best-effort lowering of user-defined `module`s to Python functions.
  * Constructs with no clean build123d equivalent (hull, minkowski, $fn-faceted
    intent, general multmatrix shear, OpenSCAD `function`s, list comprehensions
    in argument position, dynamic special-variable scoping) are emitted as a
    `# TODO` comment plus a `raise NotImplementedError(...)` so the generated
    file is honest: it imports and runs up to the unsupported node, never
    silently producing wrong geometry.

Usage:
    python scad2b3d.py input.scad [output.py]
    python scad2b3d.py input.scad -            # print to stdout

It does NOT depend on scad2py's transpiler/runtime/renderer — only on
`scad2py.parser` + `scad2py.scadast` (the front end).
"""

import os
import sys
import math
import types

# --------------------------------------------------------------------------
# Import shim: load scad2py's PARSER ONLY.
#
# `import scad2py.parser` would normally execute scad2py/__init__.py, which
# pulls in the whole runtime (trimesh, manifold3d, ...). We only need the
# front end, so we register an *empty* `scad2py` package object first; Python
# then imports the submodules `scad2py.parser`, `scad2py.scadast`,
# `scad2py.typechecking` against that empty package without ever running the
# real package __init__.
# --------------------------------------------------------------------------
SCAD2PY_ROOT = os.environ.get("SCAD2PY_ROOT", "/Users/ochafik/github/scad2py")

if "scad2py" not in sys.modules:
    _pkg = types.ModuleType("scad2py")
    _pkg.__path__ = [os.path.join(SCAD2PY_ROOT, "scad2py")]
    sys.modules["scad2py"] = _pkg

import scad2py.scadast as sc  # noqa: E402
from scad2py.parser import parse_units  # noqa: E402


# ==========================================================================
# Emission helpers
# ==========================================================================

class TranspileNote:
    """A non-fatal note collected during transpilation, surfaced in NOTES."""
    def __init__(self, kind, msg):
        self.kind = kind  # 'unsupported' | 'approx' | 'info'
        self.msg = msg


class Emitter:
    """
    Visitor over scad2py's OpenSCAD AST that returns build123d source.

    Geometry-producing nodes return a Python *expression string* (algebra API).
    Statement-level constructs (assignments, module defs, for/if) are emitted
    into `self.lines` directly.
    """

    # OpenSCAD builtin module names we know how to lower.
    GEOM_BUILTINS = {
        "cube", "sphere", "cylinder", "polyhedron",
        "square", "circle", "polygon", "text",
        "union", "difference", "intersection", "group",
        "translate", "rotate", "scale", "mirror", "multmatrix", "resize",
        "color", "linear_extrude", "rotate_extrude", "offset",
        "hull", "minkowski", "render", "projection", "fill", "import",
        "children",
    }

    def __init__(self, source_name):
        self.source_name = source_name
        self.lines = []          # output source lines
        self.notes = []          # list[TranspileNote]
        self.user_modules = {}   # name -> ModuleDefinition
        self.user_functions = {} # name -> FunctionDefinition
        self.indent = 0
        self._tmp = 0
        self._fn_stack = [{}]    # dynamic-scope of $fn/$fa/$fs (best-effort)

    # ---- low-level output -------------------------------------------------

    def emit(self, line=""):
        self.lines.append(("    " * self.indent) + line if line else "")

    def note(self, kind, msg):
        self.notes.append(TranspileNote(kind, msg))

    def fresh(self, base="t"):
        self._tmp += 1
        return f"_{base}{self._tmp}"

    @staticmethod
    def _pyname(name):
        """Map an OpenSCAD identifier to a valid Python identifier."""
        if name.startswith("$"):
            return "_dollar_" + name[1:]
        return name

    # ---- top-level driver -------------------------------------------------

    def transpile(self, unit):
        # First pass: collect ALL user module/function declarations, including
        # ones nested inside module bodies. OpenSCAD has no ordering
        # requirement and nested modules are visible in their enclosing scope;
        # build123d/Python needs every name defined before use, so the
        # prototype hoists them all to module level. (A production transpiler
        # must alpha-rename to avoid collisions, like scad2py's
        # pick_unique_names — out of scope here.)
        body = list(unit.body)
        all_decls = sc.collect(unit, lambda n: isinstance(
            n, (sc.ModuleDefinition, sc.FunctionDefinition)))
        non_decls = [s for s in body if not isinstance(
            s, (sc.ModuleDefinition, sc.FunctionDefinition))]
        # Top-level order preserved; nested ones appended.
        top_decls = [s for s in body if isinstance(
            s, (sc.ModuleDefinition, sc.FunctionDefinition))]
        nested = [d for d in all_decls if d not in top_decls]
        decls = top_decls + nested
        for d in decls:
            if isinstance(d, sc.ModuleDefinition):
                self.user_modules[d.name.name] = d
            else:
                self.user_functions[d.name.name] = d
        # OpenSCAD has SEPARATE namespaces for modules and functions, so
        # `module foo` and `function foo` may coexist. Python has one
        # namespace, so any colliding module name is mangled to `_mod_<name>`.
        self.module_pynames = {}
        for mname in self.user_modules:
            if mname in self.user_functions:
                self.module_pynames[mname] = "_mod_" + mname
                self.note("info",
                          f"name '{mname}' is both an OpenSCAD module and a "
                          f"function -> module renamed to _mod_{mname} "
                          f"(separate namespaces collapsed)")
            else:
                self.module_pynames[mname] = mname

        # Header.
        self.emit("# Auto-generated by scad2b3d.py (OpenSCAD -> build123d prototype)")
        self.emit(f"# Source: {self.source_name}")
        self.emit("from build123d import *")
        self.emit("from build123d import Align")
        self.emit("import math")
        self.emit("")
        self.emit("# ---- OpenSCAD compatibility shims ----")
        self.emit("# OpenSCAD trig is in DEGREES.")
        self.emit("def _sin(a): return math.sin(math.radians(a))")
        self.emit("def _cos(a): return math.cos(math.radians(a))")
        self.emit("def _tan(a): return math.tan(math.radians(a))")
        self.emit("")

        # Emit user functions (pure value functions).
        for d in decls:
            if isinstance(d, sc.FunctionDefinition):
                self._emit_function_def(d)
        # Emit user modules (geometry-producing functions).
        for d in decls:
            if isinstance(d, sc.ModuleDefinition):
                self._emit_module_def(d)

        # Emit top-level body: collect geometry expressions into `_parts`.
        self.emit("# ---- top-level geometry ----")
        self.emit("_parts = []")
        self.emit("")
        for stmt in non_decls:
            self._emit_statement(stmt)

        self.emit("")
        self.emit("# Combine all top-level geometry. OpenSCAD's top level is an")
        self.emit("# IMPLICIT GROUP (a CSG group node, not a boolean union): disjoint")
        self.emit("# parts are merged into one mesh only at F6 render. We mirror that")
        self.emit("# with a Compound -- O(n) and faithful -- instead of folding N")
        self.emit("# pairwise OCCT booleans (the O(n^2) build123d CSG cliff).")
        self.emit("result = _group(_parts)")
        self.emit("")
        self.emit("if __name__ == '__main__':")
        self.emit("    if result is not None:")
        self.emit("        print('result:', result)")
        self.emit("        try:")
        self.emit("            export_stl(result, 'out.stl')")
        self.emit("            print('exported out.stl')")
        self.emit("        except Exception as _e:")
        self.emit("            print('export skipped:', _e)")

        return "\n".join(self.lines) + "\n"

    # ---- statement-level emission ----------------------------------------

    def _emit_statement(self, stmt, sink="_parts"):
        """
        Emit a top-level / block-level statement. `sink` is the name of the
        Python list that geometry produced here should be appended to.
        """
        if isinstance(stmt, sc.ModuleInstantiation):
            expr = self._geom_expr(stmt)
            if expr is not None:
                self.emit(f"{sink}.append({expr})")
            return

        if isinstance(stmt, sc.ModifiedStatement):
            self._emit_modified(stmt, sink)
            return

        if isinstance(stmt, sc.Block):
            for s in stmt.body:
                self._emit_statement(s, sink)
            return

        if isinstance(stmt, sc.Assignment):
            # OpenSCAD variable -> Python variable. Top-level assignments are
            # plain globals. Special vars ($fn/$fa/$fs/$t/...) are NOT valid
            # Python identifiers and OpenSCAD scopes them dynamically; the
            # prototype lowers them to plain globals named `_dollar_fn` etc.
            tgt = self._pyname(stmt.var.name)
            if stmt.var.name.startswith("$"):
                self.note("approx",
                          f"special variable assignment {stmt.var.name} -> "
                          f"plain global {tgt} (dynamic scoping not modeled)")
            self.emit(f"{tgt} = {self._value_expr(stmt.value)}")
            return

        if isinstance(stmt, sc.For):
            self._emit_for(stmt, sink)
            return

        if isinstance(stmt, sc.If):
            self.emit(f"if {self._value_expr(stmt.cond)}:")
            self.indent += 1
            had = self._emit_block_or_stmt(stmt.body, sink)
            if not had:
                self.emit("pass")
            self.indent -= 1
            if stmt.else_body is not None:
                self.emit("else:")
                self.indent += 1
                had = self._emit_block_or_stmt(stmt.else_body, sink)
                if not had:
                    self.emit("pass")
                self.indent -= 1
            return

        if isinstance(stmt, sc.EchoStatement):
            args = ", ".join(self._echo_arg(a) for a in stmt.args)
            self.emit(f"print('ECHO:', {args})" if args else "print('ECHO:')")
            return

        if isinstance(stmt, sc.AssertStatement):
            cond = self._value_expr(stmt.cond)
            if stmt.message is not None:
                self.emit(f"assert {cond}, {self._value_expr(stmt.message)}")
            else:
                self.emit(f"assert {cond}")
            return

        if isinstance(stmt, (sc.ModuleDefinition, sc.FunctionDefinition)):
            # Hoisted to module level during transpile() - skip here.
            return

        if isinstance(stmt, sc.Include):
            self.note("unsupported",
                      f"include/use <{stmt.filename}> not inlined by prototype")
            self.emit(f"# TODO: include <{stmt.filename}> "
                      f"(prototype does not inline includes)")
            return

        self.note("unsupported", f"statement {type(stmt).__name__}")
        self.emit(f"# TODO: unsupported statement {type(stmt).__name__}")

    def _echo_arg(self, a):
        """Render an echo() argument (may be a NamedArg key=value)."""
        if isinstance(a, sc.NamedArg):
            return f"'{a.name.name}=', {self._value_expr(a.value)}"
        return self._value_expr(a)

    def _emit_block_or_stmt(self, node, sink):
        """Emit a Block or single statement; return True if anything emitted."""
        before = len(self.lines)
        if isinstance(node, sc.Block):
            for s in node.body:
                self._emit_statement(s, sink)
        else:
            self._emit_statement(node, sink)
        return len(self.lines) > before

    def _emit_for(self, stmt, sink):
        # OpenSCAD `for (i=[a:b], j=[..])` is a nested cartesian product whose
        # geometry is implicitly unioned.  -> nested Python for-loops.
        for asn in stmt.assignments:
            it = self._iterable_expr(asn.value)
            self.emit(f"for {asn.var.name} in {it}:")
            self.indent += 1
        had = self._emit_block_or_stmt(stmt.body, sink)
        if not had:
            self.emit("pass")
        for _ in stmt.assignments:
            self.indent -= 1

    def _emit_modified(self, stmt, sink):
        m = stmt.modifier
        if m == "*":  # disable -> drop entirely
            self.emit("# (OpenSCAD '*' disable modifier: subtree dropped)")
            return
        if m == "!":  # root -> only this subtree; emit normally (best effort)
            self.note("approx", "'!' root modifier emitted as plain geometry")
            self._emit_statement(stmt.statement, sink)
            return
        if m == "#":  # debug/highlight -> keep geometry, note it
            self.note("approx", "'#' highlight modifier -> plain geometry")
            self._emit_statement(stmt.statement, sink)
            return
        if m == "%":  # background -> excluded from result
            self.note("approx", "'%' background modifier -> geometry dropped "
                                 "from result")
            self.emit("# (OpenSCAD '%' background modifier: excluded from result)")
            return
        self._emit_statement(stmt.statement, sink)

    # ---- user module / function defs -------------------------------------

    def _emit_function_def(self, d):
        params = ", ".join(self._param_sig(p) for p in d.params)
        self.emit(f"def {d.name.name}({params}):")
        self.indent += 1
        self.emit(f"return {self._value_expr(d.body)}")
        self.indent -= 1
        self.emit("")

    def _emit_module_def(self, d):
        """
        OpenSCAD `module` -> Python function returning a build123d shape (or
        None).  Operator modules that reference `children()` are only
        partially supported: we pass children as a `_children` list parameter.
        """
        params = [self._param_sig(p) for p in d.params]
        uses_children = self._references_children(d.body)
        if uses_children:
            params.append("_children=None")
        pyname = self.module_pynames.get(d.name.name, d.name.name)
        self.emit(f"def {pyname}({', '.join(params)}):")
        self.indent += 1
        self.emit("_parts = []")
        if uses_children:
            self.emit("_children = _children or []")
        self._emit_block_or_stmt(d.body, "_parts")
        self.emit("return _group(_parts)")
        self.indent -= 1
        self.emit("")

    def _param_sig(self, p):
        if p.default is not None:
            return f"{p.name.name}={self._value_expr(p.default)}"
        return p.name.name

    def _references_children(self, node):
        found = []
        def cb(n):
            if isinstance(n, sc.ModuleInstantiation) and n.name.name == "children":
                found.append(n)
            if isinstance(n, (sc.Ident, sc.FunctionCall)):
                nm = getattr(n, "name", None)
                if isinstance(nm, sc.Ident) and nm.name == "$children":
                    found.append(n)
        sc.traverse(node, cb)
        return bool(found)

    # ======================================================================
    # GEOMETRY EXPRESSION emission (the heart of the algebra-API codegen)
    # ======================================================================

    def _geom_expr(self, mi):
        """ModuleInstantiation -> build123d geometry expression string, or None."""
        name = mi.name.name
        pos, named = self._split_args(mi.args)

        # ---- 3D primitives ----
        if name == "cube":
            return self._cube(pos, named)
        if name == "sphere":
            return self._sphere(pos, named)
        if name == "cylinder":
            return self._cylinder(pos, named)
        if name == "polyhedron":
            return self._polyhedron(pos, named)

        # ---- 2D primitives ----
        if name == "square":
            return self._square(pos, named)
        if name == "circle":
            return self._circle(pos, named)
        if name == "polygon":
            return self._polygon(pos, named)
        if name == "text":
            return self._text(pos, named)

        # ---- booleans ----
        if name in ("union", "group"):
            return self._combine(mi, "+")
        if name == "difference":
            return self._combine(mi, "-")
        if name == "intersection":
            return self._combine(mi, "&")

        # ---- transforms ----
        if name == "translate":
            return self._transform_pos(mi, pos, named)
        if name == "rotate":
            return self._transform_rot(mi, pos, named)
        if name == "scale":
            return self._transform_scale(mi, pos, named)
        if name == "mirror":
            return self._transform_mirror(mi, pos, named)
        if name == "multmatrix":
            return self._transform_multmatrix(mi, pos, named)
        if name == "color":
            return self._transform_color(mi, pos, named)
        if name in ("render",):
            return self._child_geom(mi)  # render() is geometrically transparent

        # ---- extrusion ----
        if name == "linear_extrude":
            return self._linear_extrude(mi, pos, named)
        if name == "rotate_extrude":
            return self._rotate_extrude(mi, pos, named)
        if name == "offset":
            return self._offset(mi, pos, named)

        # ---- hard / unsupported operators ----
        if name in ("hull", "minkowski", "projection", "fill", "resize",
                    "import", "surface"):
            return self._unsupported_op(name, mi)

        if name == "children":
            # inside a user module body
            if pos and self._is_number(pos[0]):
                return f"_children[{self._value_expr(pos[0])}]"
            return ("(sum(_children[1:], _children[0]) if _children else None)")

        # ---- user-defined module call ----
        if name in self.user_modules:
            return self._user_module_call(name, mi, pos, named)

        # ---- unknown ----
        self.note("unsupported", f"unknown module '{name}'")
        return (f"_raise('unknown OpenSCAD module: {name}')")

    def _child_geom(self, mi):
        """Geometry of a single-child operator's children, unioned."""
        children = self._child_statements(mi)
        exprs = [self._geom_expr(c) for c in children
                 if isinstance(c, sc.ModuleInstantiation)]
        exprs = [e for e in exprs if e is not None]
        if not exprs:
            # children may be for/if -> fall back to a generated helper
            return self._child_geom_via_helper(mi)
        if len(exprs) == 1:
            return exprs[0]
        return "(" + " + ".join(exprs) + ")"

    def _child_statements(self, mi):
        ch = mi.children
        if ch is None:
            return []
        if isinstance(ch, sc.Block):
            return list(ch.body)
        return [ch]

    def _child_geom_via_helper(self, mi):
        """
        When children contain control flow (for/if) we cannot produce a pure
        expression; emit an inline helper that accumulates `_parts`.
        """
        children = self._child_statements(mi)
        if not children:
            return "None"
        helper = self.fresh("kids")
        self.emit(f"def {helper}():")
        self.indent += 1
        self.emit("_parts = []")
        for c in children:
            self._emit_statement(c, "_parts")
        self.emit("return _group(_parts)")
        self.indent -= 1
        return f"{helper}()"

    def _combine(self, mi, op):
        children = self._child_statements(mi)
        # Pure-expression children?
        simple = all(isinstance(c, sc.ModuleInstantiation) or
                     (isinstance(c, sc.ModifiedStatement)) for c in children)
        if simple:
            exprs = []
            for c in children:
                if isinstance(c, sc.ModifiedStatement):
                    if c.modifier in ("*", "%"):
                        continue  # disabled / background
                    c = c.statement
                if isinstance(c, sc.ModuleInstantiation):
                    e = self._geom_expr(c)
                    if e is not None:
                        exprs.append(e)
            if not exprs:
                return "None"
            if len(exprs) == 1:
                return exprs[0]
            return "(" + f" {op} ".join(f"({e})" for e in exprs) + ")"
        # Children include control flow -> helper that folds with op.
        helper = self.fresh("csg")
        self.emit(f"def {helper}():")
        self.indent += 1
        self.emit("_parts = []")
        for c in children:
            self._emit_statement(c, "_parts")
        if op == "+":
            self.emit("return _group(_parts)")
        else:
            self.emit("_r = _parts[0] if _parts else None")
            self.emit(f"for _p in _parts[1:]: _r = _r {op} _p")
            self.emit("return _r")
        self.indent -= 1
        return f"{helper}()"

    # ---- 3D primitive emitters -------------------------------------------

    def _cube(self, pos, named):
        size = named.get("size", pos[0] if pos else None)
        center = named.get("center", pos[1] if len(pos) > 1 else None)
        if size is None:
            l = w = h = "1"
        elif isinstance(size, sc.List):
            comps = [self._value_expr(p) for p in size.parts]
            l, w, h = (comps + ["1", "1", "1"])[:3]
        else:
            s = self._value_expr(size)
            l = w = h = s
        centered = self._truthy(center)
        if centered is True:
            return f"Box({l}, {w}, {h})"
        align = "align=(Align.MIN, Align.MIN, Align.MIN)"
        if centered is None:
            # default OpenSCAD: corner at origin
            return f"Box({l}, {w}, {h}, {align})"
        return f"Box({l}, {w}, {h}, {align})"

    def _radius(self, pos, named, r_idx=0):
        """Resolve r vs d for circle/sphere/cylinder."""
        if "r" in named:
            return self._value_expr(named["r"])
        if "d" in named:
            return f"({self._value_expr(named['d'])} / 2)"
        if pos and len(pos) > r_idx:
            return self._value_expr(pos[r_idx])
        return None

    def _sphere(self, pos, named):
        self._note_fn(named, "sphere")
        r = self._radius(pos, named)
        return f"Sphere({r if r else '1'})"

    def _cylinder(self, pos, named):
        self._note_fn(named, "cylinder")
        # OpenSCAD: cylinder(h, r1, r2, center, r, d, d1, d2)
        h = named.get("h", pos[0] if pos else None)
        h_s = self._value_expr(h) if h is not None else "1"
        # radii
        def diam(key_r, key_d, idx):
            if key_r in named:
                return self._value_expr(named[key_r])
            if key_d in named:
                return f"({self._value_expr(named[key_d])} / 2)"
            if len(pos) > idx:
                return self._value_expr(pos[idx])
            return None
        r1 = diam("r1", "d1", 1)
        r2 = diam("r2", "d2", 2)
        # uniform r / d
        runi = None
        if "r" in named:
            runi = self._value_expr(named["r"])
        elif "d" in named:
            runi = f"({self._value_expr(named['d'])} / 2)"
        if r1 is None:
            r1 = runi
        if r2 is None:
            r2 = runi if runi is not None else r1
        if r1 is None:
            r1 = "1"
        if r2 is None:
            r2 = r1
        center = named.get("center", pos[3] if len(pos) > 3 else None)
        centered = self._truthy(center)
        # OpenSCAD cylinder default: base at z=0; build123d Cylinder/Cone is
        # centered. Shift down-half unless center=true.
        if r1 == r2:
            base = f"Cylinder({r1}, {h_s})"
        else:
            base = f"Cone({r1}, {r2}, {h_s})"
        if centered is True:
            return base
        return f"Pos(0, 0, {h_s} / 2) * {base}"

    def _polyhedron(self, pos, named):
        # OpenSCAD polyhedron(points, faces|triangles).
        pts = named.get("points")
        faces = named.get("faces", named.get("triangles"))
        if pts is None or faces is None:
            self.note("unsupported", "polyhedron without points/faces")
            return "_raise('polyhedron needs points and faces')"
        pts_s = self._value_expr(pts)
        faces_s = self._value_expr(faces)
        self.note("approx",
                  "polyhedron -> Solid(Shell(faces)); OCCT may reject "
                  "non-planar / non-manifold meshes (mesh-backend territory)")
        return f"_polyhedron({pts_s}, {faces_s})"

    # ---- 2D primitive emitters -------------------------------------------

    def _square(self, pos, named):
        size = named.get("size", pos[0] if pos else None)
        center = named.get("center", pos[1] if len(pos) > 1 else None)
        if size is None:
            w = h = "1"
        elif isinstance(size, sc.List):
            comps = [self._value_expr(p) for p in size.parts]
            w, h = (comps + ["1", "1"])[:2]
        else:
            s = self._value_expr(size)
            w = h = s
        if self._truthy(center) is True:
            return f"Rectangle({w}, {h})"
        return f"Rectangle({w}, {h}, align=(Align.MIN, Align.MIN))"

    def _circle(self, pos, named):
        fn = self._fn_value(named)
        r = self._radius(pos, named)
        r = r if r else "1"
        if fn is not None and isinstance(fn, int) and 3 <= fn <= 12:
            # Small explicit $fn -> intentional facets -> regular polygon.
            self.note("approx",
                      f"circle($fn={fn}) -> RegularPolygon (intentional facets)")
            return f"RegularPolygon({r}, {fn})"
        self._note_fn(named, "circle")
        return f"Circle({r})"

    def _polygon(self, pos, named):
        pts = named.get("points", pos[0] if pos else None)
        paths = named.get("paths", pos[1] if len(pos) > 1 else None)
        if pts is None:
            return "_raise('polygon needs points')"
        pts_s = self._value_expr(pts)
        if paths is not None:
            self.note("approx",
                      "polygon(paths=...) multi-contour -> first path as "
                      "outline, holes subtracted via _polygon_paths")
            return f"_polygon_paths({pts_s}, {self._value_expr(paths)})"
        return f"Polygon(*{pts_s})"

    def _text(self, pos, named):
        txt = named.get("text", pos[0] if pos else None)
        if txt is None:
            return "_raise('text needs a string')"
        kw = []
        # build123d Text requires font_size; OpenSCAD text() defaults to 10.
        kw.append(f"font_size={self._value_expr(named['size'])}"
                  if "size" in named else "font_size=10")
        if "font" in named:
            kw.append(f"font={self._value_expr(named['font'])}")
        # halign/valign -> Align
        ha = self._str_const(named.get("halign"))
        va = self._str_const(named.get("valign"))
        amap_h = {"left": "Align.MIN", "center": "Align.CENTER",
                  "right": "Align.MAX"}
        amap_v = {"bottom": "Align.MIN", "baseline": "Align.MIN",
                  "center": "Align.CENTER", "top": "Align.MAX"}
        if ha or va:
            kw.append(f"align=({amap_h.get(ha, 'Align.MIN')}, "
                      f"{amap_v.get(va, 'Align.MIN')})")
        self.note("approx",
                  "text -> Text: font metrics / spacing / direction differ "
                  "from OpenSCAD's FreeType stack")
        args = self._value_expr(txt)
        if kw:
            args += ", " + ", ".join(kw)
        return f"Text({args})"

    # ---- transforms -------------------------------------------------------

    def _transform_pos(self, mi, pos, named):
        v = named.get("v", pos[0] if pos else None)
        child = self._child_geom(mi)
        vec = self._vec3(v)
        return f"(Pos({vec}) * {child})"

    def _transform_rot(self, mi, pos, named):
        a = named.get("a", pos[0] if pos else None)
        v = named.get("v", pos[1] if len(pos) > 1 else None)
        child = self._child_geom(mi)
        if v is not None:
            # rotate(angle, axis-vector)
            self.note("approx",
                      "rotate(a, v) about arbitrary axis -> "
                      "Rotation via axis (best-effort)")
            ax = self._vec3(v)
            ang = self._value_expr(a)
            return (f"(Rotation(0,0,0).rotate(Axis((0,0,0), ({ax})), {ang}) "
                    f"* {child})")
        if isinstance(a, sc.List):
            comps = [self._value_expr(p) for p in a.parts]
            x, y, z = (comps + ["0", "0", "0"])[:3]
            return f"(Rot({x}, {y}, {z}) * {child})"
        if a is not None:
            # rotate(scalar) == rotate about Z
            return f"(Rot(0, 0, {self._value_expr(a)}) * {child})"
        return child

    def _transform_scale(self, mi, pos, named):
        v = named.get("v", pos[0] if pos else None)
        child = self._child_geom(mi)
        if isinstance(v, sc.List):
            comps = [self._value_expr(p) for p in v.parts]
            if len(set(comps)) == 1:
                return f"scale({child}, by={comps[0]})"
            self.note("approx",
                      "non-uniform scale() -> build123d scale(by=(x,y,z)); "
                      "exact on BREP primitives, lossy on faceted meshes")
            x, y, z = (comps + ["1", "1", "1"])[:3]
            return f"scale({child}, by=({x}, {y}, {z}))"
        if v is not None:
            return f"scale({child}, by={self._value_expr(v)})"
        return child

    def _transform_mirror(self, mi, pos, named):
        v = named.get("v", pos[0] if pos else None)
        child = self._child_geom(mi)
        if v is None:
            return child
        nx, ny, nz = self._vec3_tuple(v)
        # OpenSCAD mirror normal -> build123d mirror plane through origin.
        return (f"mirror({child}, about=Plane(origin=(0,0,0), "
                f"normal=({nx}, {ny}, {nz})))")

    def _transform_multmatrix(self, mi, pos, named):
        m = named.get("m", pos[0] if pos else None)
        child = self._child_geom(mi)
        self.note("approx",
                  "multmatrix -> Location from 4x4 matrix; only the rigid "
                  "(rotation+translation) part is preserved by Location, "
                  "shear/projective components are lost")
        return f"(_location_from_matrix({self._value_expr(m)}) * {child})"

    def _transform_color(self, mi, pos, named):
        c = named.get("c", pos[0] if pos else None)
        alpha = named.get("alpha", pos[1] if len(pos) > 1 else None)
        child = self._child_geom(mi)
        if c is None:
            return child
        if isinstance(c, sc.List):
            comps = [self._value_expr(p) for p in c.parts]
            if len(comps) == 3:
                comps.append(self._value_expr(alpha) if alpha is not None
                             else "1.0")
            color = f"Color({', '.join(comps)})"
        else:
            # named color string or hex string
            color = f"Color({self._value_expr(c)})"
        return f"_colored({child}, {color})"

    # ---- extrusion --------------------------------------------------------

    def _linear_extrude(self, mi, pos, named):
        height = named.get("height", pos[0] if pos else None)
        h = self._value_expr(height) if height is not None else "1"
        center = named.get("center")
        twist = named.get("twist")
        scale_arg = named.get("scale")
        child = self._child_geom(mi)
        if twist is not None and not (self._is_number(twist) and
                                      self._const_value(twist) == 0):
            self.note("unsupported",
                      "linear_extrude(twist=...) has no build123d equivalent "
                      "(would need a lofted/swept helix)")
            return (f"_raise('linear_extrude twist not supported')")
        taper = ""
        if scale_arg is not None:
            self.note("approx",
                      "linear_extrude(scale=...) -> extrude(taper=...) only "
                      "approximates uniform scalar scale")
        base = f"extrude({child}, amount={h})"
        if self._truthy(center) is True:
            base = f"Pos(0, 0, -({h}) / 2) * {base}"
        return base

    def _rotate_extrude(self, mi, pos, named):
        angle = named.get("angle", pos[0] if pos else None)
        child = self._child_geom(mi)
        self._note_fn(named, "rotate_extrude")
        if angle is not None:
            ang = self._value_expr(angle)
            return f"revolve({child}, axis=Axis.Z, revolution_arc={ang})"
        return f"revolve({child}, axis=Axis.Z)"

    def _offset(self, mi, pos, named):
        child = self._child_geom(mi)
        if "r" in named:
            self.note("approx", "offset(r=...) -> offset(kind=Kind.ARC)")
            return (f"offset({child}, amount={self._value_expr(named['r'])}, "
                    f"kind=Kind.ARC)")
        if "delta" in named:
            return (f"offset({child}, "
                    f"amount={self._value_expr(named['delta'])}, "
                    f"kind=Kind.INTERSECTION)")
        if pos:
            return f"offset({child}, amount={self._value_expr(pos[0])})"
        return child

    def _unsupported_op(self, name, mi):
        self.note("unsupported",
                  f"'{name}' has no clean build123d equivalent")
        # still recurse so the children get transpiled (in a dead branch)
        return (f"_raise(\"OpenSCAD '{name}' is not transpilable to build123d "
                f"(needs a mesh backend)\")")

    def _user_module_call(self, name, mi, pos, named):
        d = self.user_modules[name]
        args = [self._value_expr(a) for a in pos]
        for k, v in named.items():
            args.append(f"{k}={self._value_expr(v)}")
        children = self._child_statements(mi)
        if children and self._references_children(d.body):
            kids = self._child_geom_list(mi)
            args.append(f"_children={kids}")
        pyname = self.module_pynames.get(name, name)
        return f"{pyname}({', '.join(args)})"

    def _child_geom_list(self, mi):
        children = self._child_statements(mi)
        exprs = []
        for c in children:
            if isinstance(c, sc.ModuleInstantiation):
                e = self._geom_expr(c)
                if e is not None:
                    exprs.append(e)
        return "[" + ", ".join(exprs) + "]"

    # ======================================================================
    # VALUE EXPRESSION emission (numbers, lists, arithmetic, calls)
    # ======================================================================

    def _value_expr(self, n):
        if n is None:
            return "None"
        if isinstance(n, sc.Constant):
            return repr(n.value)
        if isinstance(n, sc.Ident):
            nm = n.name
            if nm == "true":
                return "True"
            if nm == "false":
                return "False"
            if nm == "undef":
                return "None"
            if nm == "PI":
                return "math.pi"
            if nm.startswith("$"):
                self.note("approx",
                          f"special variable {nm} read as a plain global")
            return self._pyname(nm)
        if isinstance(n, sc.List):
            return "[" + ", ".join(self._value_expr(p) for p in n.parts) + "]"
        if isinstance(n, sc.BinOp):
            return self._binop(n)
        if isinstance(n, sc.UnaryOp):
            op = {"!": "not ", "-": "-", "+": "+"}.get(n.op, n.op)
            return f"({op}{self._value_expr(n.operand)})"
        if isinstance(n, sc.TernaryOp):
            return (f"({self._value_expr(n.left)} if "
                    f"{self._value_expr(n.cond)} else "
                    f"{self._value_expr(n.right)})")
        if isinstance(n, sc.ParenExpr):
            return f"({self._value_expr(n.expr)})"
        if isinstance(n, sc.Index):
            return f"{self._value_expr(n.expr)}[{self._value_expr(n.index)}]"
        if isinstance(n, sc.Range):
            return self._range_expr(n)
        if isinstance(n, sc.FunctionCall):
            return self._funccall(n)
        if isinstance(n, sc.Let):
            # let(a=...) body  ->  immediately-invoked lambda
            binds = ", ".join(f"{a.var.name}={self._value_expr(a.value)}"
                              for a in n.assignments)
            return f"(lambda {binds}: {self._value_expr(n.body)})()"
        if isinstance(n, (sc.ForComprehension, sc.Each, sc.IfExpression)):
            return self._comprehension(n)
        self.note("unsupported", f"value expression {type(n).__name__}")
        return f"_raise('unsupported expression {type(n).__name__}')"

    def _binop(self, n):
        opmap = {
            "&&": "and", "||": "or", "==": "==", "!=": "!=",
            "<": "<", ">": ">", "<=": "<=", ">=": ">=",
            "+": "+", "-": "-", "*": "*", "/": "/", "%": "%", "^": "**",
        }
        op = opmap.get(n.op, n.op)
        return (f"({self._value_expr(n.left)} {op} "
                f"{self._value_expr(n.right)})")

    def _range_expr(self, n):
        start = self._value_expr(n.start)
        end = self._value_expr(n.end)
        if n.step is not None:
            step = self._value_expr(n.step)
        else:
            step = "1"
        # OpenSCAD ranges are INCLUSIVE of end. Represent as a helper.
        return f"_orange({start}, {end}, {step})"

    def _iterable_expr(self, n):
        if isinstance(n, sc.Range):
            return self._range_expr(n)
        return self._value_expr(n)

    def _funccall(self, n):
        name = n.name.name
        pos, named = self._split_args(n.args)
        # OpenSCAD math builtins -> Python.
        MATHMAP = {
            "sin": "_sin", "cos": "_cos", "tan": "_tan",
            "sqrt": "math.sqrt", "abs": "abs", "floor": "math.floor",
            "ceil": "math.ceil", "ln": "math.log", "exp": "math.exp",
            "pow": "pow", "len": "len", "min": "min", "max": "max",
            "round": "round", "norm": "_norm", "cross": "_cross",
            "atan2": "_atan2", "atan": "_atan", "asin": "_asin",
            "acos": "_acos", "log": "math.log10",
        }
        if name in MATHMAP and not named:
            args = ", ".join(self._value_expr(a) for a in pos)
            return f"{MATHMAP[name]}({args})"
        if name == "str" and not named:
            args = ", ".join(self._value_expr(a) for a in pos)
            return f"_ostr({args})"
        if name == "concat":
            args = ", ".join(self._value_expr(a) for a in pos)
            return f"_concat({args})"
        if name == "version":
            return "[2024, 1, 0]"
        if name in ("is_undef", "is_list", "is_num", "is_string", "is_bool"):
            self.note("info", f"OpenSCAD predicate {name}() -> Python helper")
            args = ", ".join(self._value_expr(a) for a in pos)
            return f"_{name}({args})"
        if name == "lookup":
            self.note("approx", "lookup() -> linear-interpolation helper")
            args = ", ".join(self._value_expr(a) for a in pos)
            return f"_lookup({args})"
        if name in self.user_functions:
            args = [self._value_expr(a) for a in pos]
            for k, v in named.items():
                args.append(f"{k}={self._value_expr(v)}")
            return f"{name}({', '.join(args)})"
        # unknown function
        self.note("unsupported", f"unknown function '{name}'")
        args = ", ".join(self._value_expr(a) for a in pos)
        return f"_raise('unknown OpenSCAD function: {name}')"

    def _comprehension(self, n):
        if isinstance(n, sc.Each):
            return self._value_expr(n.value)
        if isinstance(n, sc.IfExpression):
            # only valid inside list comp; approximate
            return (f"({self._value_expr(n.body)} if "
                    f"{self._value_expr(n.cond)} else "
                    f"{self._value_expr(n.else_body) if n.else_body else 'None'})")
        if isinstance(n, sc.ForComprehension):
            clauses = " ".join(
                f"for {a.var.name} in {self._iterable_expr(a.value)}"
                for a in n.assignments)
            body = self._value_expr(n.body)
            cond = f" if {self._value_expr(n.cond)}" if n.cond else ""
            return f"[{body} {clauses}{cond}]"
        return "_raise('comprehension')"

    # ---- small helpers ----------------------------------------------------

    def _split_args(self, args):
        pos, named = [], {}
        for a in args:
            if isinstance(a, sc.NamedArg):
                named[a.name.name] = a.value
            else:
                pos.append(a)
        return pos, named

    def _vec3(self, v):
        if v is None:
            return "0, 0, 0"
        if isinstance(v, sc.List):
            comps = [self._value_expr(p) for p in v.parts]
            comps = (comps + ["0", "0", "0"])[:3]
            return ", ".join(comps)
        # a variable holding a vector
        e = self._value_expr(v)
        return f"*_v3({e})"

    def _vec3_tuple(self, v):
        if isinstance(v, sc.List):
            comps = [self._value_expr(p) for p in v.parts]
            return tuple((comps + ["0", "0", "0"])[:3])
        e = self._value_expr(v)
        return (f"_v3({e})[0]", f"_v3({e})[1]", f"_v3({e})[2]")

    def _truthy(self, n):
        """Return True/False if statically known, else None."""
        if n is None:
            return None
        if isinstance(n, sc.Constant):
            return bool(n.value)
        if isinstance(n, sc.Ident):
            if n.name == "true":
                return True
            if n.name == "false":
                return False
        return None

    def _is_number(self, n):
        return isinstance(n, sc.Constant) and isinstance(n.value, (int, float))

    def _const_value(self, n):
        if isinstance(n, sc.Constant):
            return n.value
        if isinstance(n, sc.UnaryOp) and n.op == "-" and self._is_number(n.operand):
            return -n.operand.value
        return None

    def _str_const(self, n):
        if isinstance(n, sc.Constant) and isinstance(n.value, str):
            return n.value
        return None

    def _fn_value(self, named):
        """Return $fn as an int if statically known."""
        fn = named.get("$fn")
        if fn is None:
            return None
        v = self._const_value(fn)
        if isinstance(v, (int, float)):
            return int(v)
        return None

    def _note_fn(self, named, prim):
        if "$fn" in named or "$fa" in named or "$fs" in named:
            self.note("info",
                      f"{prim}: $fn/$fa/$fs ignored -> emitted exact BREP "
                      f"primitive (intent fidelity)")


# ==========================================================================
# Runtime preamble appended to every generated file (small, self-contained).
# ==========================================================================

RUNTIME_PREAMBLE = '''
# ---- generated runtime helpers (OpenSCAD value/geometry semantics) ----

def _raise(msg):
    raise NotImplementedError(msg)

def _group(parts):
    """OpenSCAD implicit aggregation (top level, `for`, module bodies, union of
    disjoint parts) -> a build123d Compound.

    OpenSCAD's CSG tree only fuses geometry into one mesh at F6 *render* time;
    the script-level tree is a *group*. Folding N parts with pairwise OCCT
    booleans (`a + b + c + ...`) is the O(n^2) build123d performance cliff and
    is also unnecessary for the disjoint case. A Compound is O(n), exports
    correctly to STL, and is the faithful representation of the CSG group node.
    Explicit `union()` of overlapping solids still uses real `+` booleans.
    """
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return Compound(children=parts)

def _v3(v):
    v = list(v)
    while len(v) < 3:
        v.append(0)
    return v[:3]

def _orange(start, end, step):
    """OpenSCAD range [start:step:end] - inclusive of end."""
    out, x = [], start
    if step == 0:
        return out
    if step > 0:
        while x <= end + 1e-9:
            out.append(x)
            x += step
    else:
        while x >= end - 1e-9:
            out.append(x)
            x += step
    return out

def _norm(v):
    return math.sqrt(sum(c * c for c in v))

def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]

def _atan2(y, x):
    return math.degrees(math.atan2(y, x))

def _atan(x):
    return math.degrees(math.atan(x))

def _asin(x):
    return math.degrees(math.asin(x))

def _acos(x):
    return math.degrees(math.acos(x))

def _ostr(*args):
    return "".join(str(a) for a in args)

def _concat(*args):
    out = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(a)
        else:
            out.append(a)
    return out

def _colored(shape, color):
    if shape is not None:
        shape.color = color
    return shape

def _location_from_matrix(m):
    """Best-effort: rigid (rotation+translation) part of a 4x4 multmatrix."""
    import numpy as np
    a = np.array(m, dtype=float)
    if a.shape == (3, 4):
        a = np.vstack([a, [0, 0, 0, 1]])
    loc = Location()
    loc.wrapped  # touch
    from build123d import Matrix
    return Location(Matrix(a.tolist()))

def _polyhedron(points, faces):
    """OpenSCAD polyhedron -> Solid(Shell(faces)). Fragile for non-planar."""
    occ_faces = []
    for f in faces:
        pts = [points[i] for i in f]
        wire = Wire.make_polygon([tuple(p) for p in pts], close=True)
        occ_faces.append(Face(wire))
    shell = Shell(occ_faces)
    try:
        return Solid(shell)
    except Exception:
        return shell

def _polygon_paths(points, paths):
    """OpenSCAD polygon with explicit paths -> outer face minus holes."""
    faces = []
    for path in paths:
        pts = [tuple(points[i]) for i in path]
        faces.append(Polygon(*pts))
    if not faces:
        return None
    result = faces[0]
    for hole in faces[1:]:
        result = result - hole
    return result

def _is_undef(x):
    return x is None

def _is_list(x):
    return isinstance(x, (list, tuple))

def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)

def _is_string(x):
    return isinstance(x, str)

def _is_bool(x):
    return isinstance(x, bool)

def _lookup(key, table):
    table = sorted(table, key=lambda kv: kv[0])
    if key <= table[0][0]:
        return table[0][1]
    if key >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        x0, y0 = table[i]
        x1, y1 = table[i + 1]
        if x0 <= key <= x1:
            t = (key - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return table[-1][1]
'''


def transpile_file(scad_path):
    """Parse a .scad file and return generated build123d Python source."""
    units = parse_units(scad_path)
    unit = units[scad_path]
    em = Emitter(os.path.basename(scad_path))
    body = em.transpile(unit)
    # Splice the runtime preamble in right after the imports/shims.
    marker = "# ---- OpenSCAD compatibility shims ----"
    lines = body.split("\n")
    out = []
    spliced = False
    for ln in lines:
        out.append(ln)
        if not spliced and ln.startswith('def _tan('):
            out.append(RUNTIME_PREAMBLE)
            spliced = True
    src = "\n".join(out)
    return src, em.notes


def main(argv):
    if len(argv) < 1:
        print("usage: scad2b3d.py input.scad [output.py|-]", file=sys.stderr)
        return 2
    scad_path = os.path.abspath(argv[0])
    src, notes = transpile_file(scad_path)
    if len(argv) >= 2 and argv[1] != "-":
        with open(argv[1], "w") as f:
            f.write(src)
        print(f"wrote {argv[1]}", file=sys.stderr)
    elif len(argv) >= 2 and argv[1] == "-":
        sys.stdout.write(src)
    else:
        sys.stdout.write(src)
    for n in notes:
        print(f"  [{n.kind}] {n.msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
