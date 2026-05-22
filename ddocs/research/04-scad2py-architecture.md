# scad2py Architecture — Deep Dive

> Research note for the effort to make `scad2py` use `build123d` as a backend / transpilation target.
> Project root: `/Users/ochafik/github/scad2py/`. Package: `/Users/ochafik/github/scad2py/scad2py/`.
> All line references are `file.py:line` relative to that package unless noted.

`scad2py` is an experimental OpenSCAD → Python transpiler and renderer. It does **two distinct things**:

1. **Transpile**: parse a `.scad` file into an OpenSCAD AST, lower it to a Python AST, and emit Python source. The emitted source `import`s `scad2py` and calls `@module`-decorated runtime functions.
2. **Render**: execute that Python. Execution builds a `csg.Node` tree (an in-memory CSG graph), which a `ManifoldRenderer` visitor turns into `manifold3d.Manifold` / `CrossSection` geometry, then exports via `trimesh`.

The geometry library is **manifold3d** (CUDA/TBB-accelerated CSG). `trimesh` is used for mesh I/O and a couple of primitive constructions (`revolve`). There is no current build123d / OpenCASCADE involvement anywhere.

---

## 1. Overall pipeline — tracing a `.scad` file end-to-end

### Entry points

- `scad2py/__main__.py` — three lines: `from scad2py.cli import run_cli; run_cli(sys.argv[1:])`.
- `scad2py/cli.py` — `run_cli(argv)`: the top-level orchestrator.
- `scad2py/main.py` — `run(main, argv=...)`: the *runtime* driver used by transpiled code's `if __name__ == "__main__"` block.
- `scad2py/args.py` — a hand-rolled argv parser (`parse_args`) producing an `Args` dataclass; understands `-o`, `--export-format`, `-Dkey=value` params, `.scad`/`.py` positional args, and `--enable=feature` flags via `get_feature()`.
- `scad2py/transpiler/__main__.py` — `python -m scad2py.transpiler file.scad`: parse + transpile + `print` the Python. Pure transpile, no rendering.

### `run_cli` flow (`cli.py:9-80`)

```
[file, *args] = argv
import scad2py.parser, scad2py.transpiler
units   = scad2py.parser.parse_units(file)                 # dict[path -> Unit AST]
transpiled = scad2py.transpiler.transpile(units[file], units, output_main=True)  # -> py.Unit
code    = str(transpiled)                                  # Python source string
```

Then a branch on the requested output:

- **Transpile-to-`.py` mode**: if `args.outfile` ends in `.py` (or is `-` with `--export-format py`), the Python source is written to that file (or printed) and that's it (`cli.py:46-51`).
- **Run mode** (everything else): the code is written to `tmp.py` in the cwd, then `exec("import tmp; from scad2py import run; run(tmp.main)")` (`cli.py:70-72`). So even "run" mode round-trips through a real file `tmp.py` and imports it as a module. Optional `cProfile` wrapper gated by `SCAD2PY_PROFILE=1`.

Two debug env vars dump intermediate stages to stderr: `SCAD2PY_DEBUG_AST=1` (the OpenSCAD ASTs) and `SCAD2PY_DEBUG_CODE=1` (the transpiled Python).

### `run` flow (`main.py:6-73`)

`run(main, argv=None)` is what the generated `if __name__ == "__main__"` calls. It:

1. `parse_args` again to get outfile/format/`-D` params.
2. Opens a `scad2py.vars(**params)` global-variable context.
3. Special-cases `export_format == 'csg'`: pushes a fresh `csg.Group` context, calls `main()`, and serializes the csg tree with `node.to_str('')` — this is the OpenSCAD `.csg` dump path, no geometry rendered.
4. Otherwise: `with render(preview=..., allow_color=...) as r: main()`. `render()` (in `contexts.py`) is the rendering context manager. Calling `main()` *populates* a `csg.Node` tree as a side effect.
5. After the `with` block, `r.output` triggers actual geometry rendering. The result is reported (3D facet count / 2D segment count) and either `r.export(outfile, format)` or `r.show()`.

So the pipeline is: **`.scad` text → tokens → OpenSCAD AST (`Unit`) → resolved/inlined AST → Python AST (`py.Unit`) → Python source → exec → `csg.Node` tree → manifold3d geometry → trimesh export**.

---

## 2. Parser — `scad2py/parser/`

PLY (Python Lex-Yacc) based. Files:

- `tokrules.py` — the lexer rules + token list.
- `__init__.py` — the yacc grammar (all `p_*` functions) + `parse`/`parse_units` drivers.
- `params.py` — a tiny `ParamEvaluator` to evaluate `-Dkey=value` param expressions into Python values.
- `comments.py` — `CommentsAttacher`, attaches comments to AST nodes post-parse.
- `parsetab.py` — generated PLY parse table (only written when `SCAD2PY_WRITE_PARSER_TABLES=1`, `__init__.py:78`).

`parser/__init__.py:1-2` carries a comment crediting the two reference grammars it was ported against:
```python
# https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/OpenSCAD/importCSG.py#L192
# https://github.com/openscad/openscad/blob/master/src/core/parser.y
```
The same two URLs head `scadast.py`.

### Lexer (`tokrules.py`)

- `reserved` (lines 1-16): `include`, `use`, `true`, `false`, `undef`, `if`, `else`, `for`, `each`, `function`, `module`, `let`, `assert`, `echo` — 14 keywords. Each is its own token.
- Simple-regex tokens for all operators/punctuation. Notable:
  - `t_TOK_PATH = r'<[^=>\s]+>'` — include paths.
  - `t_TOK_NUMBER = r'[0-9]*[\.]*[0-9]+([eE][+-]?[0-9]+)*'`.
  - `t_TOK_STRING = r'"(\\"|[^"])*"'`.
  - `t_TOK_MODIFIERDEBUG = r'\#'` — the `#` debug modifier needs its own token because `%` is already `TOK_MOD`.
- `t_TOK_ID` (lines 100-103) matches `[$]?[a-zA-Z_]\w*` and re-classifies reserved words via `reserved_map`. The leading `$` lets `$fn`, `$fa`, `$fs`, `$t`, `$preview`, `$children` be ordinary identifiers.
- `t_comment1` (lines 92-98) matches `//…` and `/*…*/`, increments `lineno`, and stashes each comment on `lexer.unclaimed_comments` for later attachment.

### Grammar (`parser/__init__.py`)

`precedence` (lines 18-32) — standard OpenSCAD precedence, lowest first: `?:`, `||`, `&&`, equality, comparison, `+ -`, `* /`, `%`, `^`, then unary `-`/`+`/`!`.

Productions of note:
- `p_input` — top level is a list of `modifiable_statement`.
- `p_modifiable_statement` (171-181) — `modifier statement | statement`; a leading `*`/`#`/`!`/`%` wraps the statement in `ModifiedStatement`.
- `p_module_instantiation` (425-435) — `ident ( arg_list ) ;` or `ident ( arg_list ) modifiable_statement`. The single-child-statement form (no braces) is folded into the same node — `children` is `None` for `;`, otherwise the child statement.
- `p_bin_op` folds the ternary `?:` into the same production (TernaryOp).
- `p_unary_op` (296-306) constant-folds `-<number>` into a `Constant` immediately.
- `p_for_comprehension`, `p_list`, `p_each_expr`, `p_range` handle list comprehensions, `each`, and `[start:step:end]` ranges.
- `p_error` (131-148) produces a 3-line source-context error message with a `^` caret.

### Drivers

- `parse(filename)` (69-96): `lex.lex(module=tokrules)`, `yacc.yacc(...)`, parse with `tracking=True` (so `lexspan` positions are available), wrap result in a `Unit(source, body)`, then run `CommentsAttacher`.
- `parse_units(filename)` (44-67): recursively parses the entry file plus every `Include` it references (`collect(u, lambda n: isinstance(n, Include))`), returning `dict[path -> Unit]`. This is the multi-file unit set the transpiler consumes.
- `parse_string(s, start=...)` — parse an in-memory string; used for `-D` param values and the IPython magic.

`CommentsAttacher` (`comments.py`) groups unclaimed comments by line; for each `Statement` it attaches a same-line trailing comment as `post_comments` and column-1 comments on the immediately preceding lines as `pre_comments`. These feed the customizer (see §8).

---

## 3. OpenSCAD AST — `scad2py/scadast.py`

A dataclass hierarchy (all `@dataclass(kw_only=True)` + `@optionally_typechecked`). Base `Node` (`scadast.py:240-256`) carries `tpe` (inferred type), `symbol` (resolved declaration or callable builtin), `scope`, `pos` (`Position`), `parent`, and `pre_comments`/`post_comments`. `Node.defines_scope` is a property defaulting `False`.

Three abstract subtypes: `Expression`, `Statement`, `Declaration(Statement)`.

Concrete node classes (with `defines_scope=True` marked **[S]**):

| Category | Nodes |
|---|---|
| Structural | `Unit`, `Include`, `Block` **[S]**, `ModifiedStatement` |
| Statements | `ModuleInstantiation`, `Assignment`, `For` **[S]**, `If`, `AssertStatement`, `EchoStatement` |
| Declarations | `ModuleDefinition` **[S]**, `FunctionDefinition` **[S]** |
| Expressions | `Ident`, `Select`, `Constant`, `BinOp`, `UnaryOp`, `TernaryOp`, `Range`, `List`, `Index`, `FunctionCall`, `ParenExpr`, `Let` **[S]**, `FunctionLiteral` **[S]**, `IfExpression`, `ForComprehension` **[S]**, `Each`, `AssertExpr`, `EchoExpr`, `NamedArg` |
| Aux | `Param` |

There is also an unused **type lattice** (`Type`, `ListType`, `ScalarType`, `ConstantScalarType`, `RangeType`, `AnyType`, `UnionType`, `FunctionType`, `ModuleType`, plus `ListLen`/`ScalarTypeEnum`/`GeometryGenus` enums, lines 595-662) — the intended target of a typer that was never finished (§4).

### Visitor pattern

`Visitor` (26-58) declares one `visitXxx` per node class. `DefaultVisitor` (61-238) is the workhorse:

- `_get_children_field_names(t)` (66-75): introspects `dataclasses.fields(t)`, excluding metadata fields (`pos`, `scope`, `tpe`, `parent`, `symbol`, `pre_comments`, `post_comments`). Cached per type.
- `_visitChildren` (84-110): recurses into every child `Node` and every `list[Node]`. If `transform_children=True`, it writes back the visitor's return value, flattening returned lists (so a visitor can splice or delete nodes).
- The class routes each `visitXxx` through `visitExpression`/`visitStatement`/`visitDeclaration`/`visitNode`, so a subclass can override at a coarse granularity.

Helpers: `InclusiveVisitor` (664-682) — follows `Include`s into other units (used by scoping). `traverse(root, cb)` / `collect(root, predicate)` (684-702) — pre-order callbacks. `_Traverser` wraps a callback as a visitor.

---

## 4. Transpiler — `scad2py/transpiler/` and `scad2py/pyast/`

### Python AST — `pyast/__init__.py`

A *second* dataclass AST, this one for Python. Each node implements `to_py(indent='') -> str`; `__str__` delegates to `to_py()`. There is **no use of CPython's `ast` module** — codegen is direct string emission with manual indentation.

Node classes: `Unit`, `Block`, `Import`/`ImportedSymbol`, `Ident`, `Select`, `AliasedExpression`, `WithStatement`, `Constant`, `BinOp`, `UnaryOp`, `TernaryOp`, `If`, `NamedArg`, `Call`, `Assignment`, `ParenExpr`, `For`, `ForComprehensionPart`, `ForComprehension`, `GeneratorExpression`, `List`, `Param`, `FunctionDefinition` (with `annotations` for decorators), `Lambda`, `WalrusDeclaration`, `Index`, `Assert`, `Return`, `Yield`, `CommentedOutStatement`.

Pretty-printing has light heuristics: `_is_semi_trivial_node` (186-198) decides whether a `Call`/`List` is emitted multi-line. `CommentedOutStatement.to_py` (205-207) prefixes every line with `# ` — this is how the OpenSCAD `*` ("disable") modifier is rendered.

### `transpile()` orchestration — `transpiler/__init__.py:713-742`

```python
def transpile(entry_point, units, output_main=True):
  global_scope = Scope(vars={}, functions={}, modules={})
  attacher = ParentAttacher()
  scope_creator = ScopeCreationVisitor(global_scope, units)
  for unit in units.values():
    unit.accept(attacher)        # set Node.parent everywhere
    unit.accept(scope_creator)   # build Scope tree, record shadowed decls
  overwritten_decl_remover = NodesRemover(scope_creator.overwritten_declarations)
  for unit in units.values():
    unit.accept(overwritten_decl_remover)   # delete shadowed declarations
  resolver = Resolver(global_scope, units)
  entry_point.accept(resolver)              # set Node.symbol on every Ident
  inlined = entry_point.accept(Inliner(units=units))   # splice include/use bodies
  pick_unique_names(inlined)                # alpha-rename to avoid collisions
  return entry_point.accept(Transpiler(units=units, output_main=output_main))
```

So five passes over the OpenSCAD AST before codegen: **parent attach → scope creation → shadow removal → symbol resolution → include inlining → unique renaming → transpile**.

### `resolver.py`

- `Scope` — three dicts: `vars` (`Assignment`/`Param`), `functions`, `modules`. OpenSCAD has three separate namespaces, mirrored here.
- `ScopedVisitor` — maintains a stack of `Scope`s, pushing/popping on `defines_scope` nodes.
- `ScopeCreationVisitor` — assigns a fresh `Scope` to each scope-defining node; records into `overwritten_declarations` any declaration shadowed by a later same-name one (OpenSCAD's "last assignment wins" semantics within a scope).
- `Resolver` — for every `Ident`, sets `n.symbol` to the declaration it refers to. Picks the right namespace by inspecting `n.parent` (a `FunctionCall.name` → functions, `ModuleInstantiation.name` → modules, else vars). Unresolved names fall back to a `scad2py` builtin via `getattr(sys.modules['scad2py'], builtin_name)` after a small alias map `_builtin_names` (`resolver.py:101-110`) that maps `sin→sin_degrees`, …, `import→load`.
- `ParentAttacher` — sets `Node.parent` (a stack-based pre-order walk).
- `Inliner` (218-252) — replaces `Include` nodes with the body of the included `Unit`. For `use <...>` it sets `skip_geometry=True` so only declarations (not top-level geometry) are spliced in. Runs with `transform_children=True`.
- `NodesRemover` (254-264) — returns `None` for any node in a target id-set; combined with `transform_children` list-flattening this deletes the shadowed declarations.
- `pick_unique_names` (`__init__.py:745-784`) — three sub-passes: collect builtin names, alpha-rename every non-global `Assignment`/`ModuleDefinition`/`FunctionDefinition` to a globally-unique name (`x`, `x_1`, `x_2`, …), then fix up every `Ident` that referenced a renamed symbol. This is why scoped OpenSCAD `a` becomes `a2`, `a3` in the README's mapping table — Python has no block scope so each block-local must get a unique name.

### `Transpiler` — `transpiler/__init__.py:38-613`

A `DefaultVisitor` over the OpenSCAD AST that *returns* `pyast` nodes. Key decisions:

- **`visitUnit`** (137-174): splits body into "non-geom" (`FunctionDefinition`/`Assignment`/`ModuleDefinition`) and "geom" (everything else). Emits `from scad2py import *`, then the non-geom declarations, then wraps all geometry statements in a `def main(): …` decorated `@module`, then optionally a `if __name__ == "__main__": run(main)` block.
- **`visitStatementsInOrder`** (114-134): re-orders a statement list so all `FunctionDefinition`s come first, then `Assignment`s, then `ModuleDefinition`s, then geometry — Python needs names defined before use; OpenSCAD does not.
- **`visitModuleInstantiation`** (198-234): `name(args)`. If there are `children`, it becomes a `with name(args): <children>` (`WithStatement`). Nested `with`s are collapsed into one `with a, b, c:`. `children()` / `children(i)` map to a `children` parameter passed implicitly.
- **`visitModuleDefinition`** (554-561) and **`visitFunctionDefinition`** (527-534): become `def`s decorated `@module()` / `@function()` respectively (`_make_module_annot` / `_make_function_annot`, 563-567).
- **`visitModifiedStatement`** (583-607): `!`→`with root()`, `#`→`with debug()`, `%`→`with background()`, `*`→`CommentedOutStatement`.
- **Numeric vs. non-numeric ops** (`visitBinOp`, 397-423): if the typer can't prove both operands are numbers, `*` → `scad2py.mult`, `/` → `scad2py.div`, `+` (possibly string) → `scad2py.plus`, `==`/`!=` → `scad2py.equal`. Otherwise plain Python operators (`&&`→`and`, `||`→`or`, `^`→`**`).
- **Lists** (`visitList`, 282-321): a homogeneous numeric list becomes `wrap_list([...])` (→ a numpy array, see §5); `each`/comprehensions become real Python comprehensions; mixed lists use `scad2py.concat`. `wrap_list` is skipped via `without_list_wrapping` when the value provably won't escape (e.g. it's a comprehension iterable).
- **Globals** (`visitIdent`, 357-376): top-level `Assignment` targets become `vars.a = …`; reads of a global, or any `$fn/$fa/$fs/$t/$preview`, become `vars.a()` (a *call* — `vars` returns getters, see §5). `$children` → `len(children)`.
- **`Let`** → an immediately-invoked lambda `(lambda a=…: body)()`. **`Range`** → either Python `range(...)` (when iterated and integral) or `scad2py.Range(start, inclusive_end=…, step=…)`.
- `_make_global_var_ref_prelude` (510-525): for params whose default references a global, emits `if callable(p): p = p()` at function entry — because such defaults are passed as un-evaluated lambdas.

### `typer.py`

`transpiler/typer.py` is a **6-line stub** — `class TypeFlower(DefaultVisitor)` with an empty body and an import (`import scad2py.ast as sc`) that points at a non-existent module path. The type lattice in `scadast.py` is therefore dead code. The `_is_number_only`, `_could_be_string`, `_can_be_zero` helpers in `transpiler/__init__.py` are *syntactic* approximations standing in for a real typer: they only inspect `Constant` nodes. This is a significant unfinished area.

---

## 5. Runtime — `scad2py/runtime/`, `runtime_utils.py`, `decoration.py`, `contexts.py`

The transpiled code does `from scad2py import *`. `scad2py/__init__.py` re-exports everything from `runtime.functions`, `runtime.maths`, `runtime.modules`, `runtime_utils`, `contexts`, `decoration`, `main`, `runtime.variables`, and sets `vars._preview/_fn/_fa/_fs`.

### `decoration.py` — the `@module` / `@function` decorators

The keystone of execution semantics. `module(func, builds_group=False)` (`decoration.py:51-80`) wraps a function so that **calling it appends a `csg.Node` to the current context**:

```python
def wrapper(*args, **kwargs):
    args, kwargs = helper.process_args(args, kwargs)
    container = csg.Context.current()
    geom = func(*args, **kwargs)
    if geom is not None:
        container.children.append(geom)
    return geom
```

So `cube()` *runs* `cube`, which returns a `csg.Polyhedron`, and the decorator appends it to `Context.current().children`. The `group_wrapper` variant (used when `builds_group=True`) instead pushes a fresh `csg.Group`, runs the body inside it, and returns the group — this is how user-defined modules accumulate their children. (`runtime/modules.py` decorates the builtins with `builds_group=False`; user modules from the transpiler get `@module()` which defaults to `False` too — children are attached via the `with` statement's context push, see below.)

`_Helper.process_args` (17-49) drops kwargs that aren't real parameters (with a stderr warning) and would wrap list args. `function()` (82-93) is a thin wrapper with no CSG side effect — pure value functions.

### `contexts.py` and `csg.Context`

`csg.Context` (`csg.py:808-846`) is a global stack of `csg.Node`s. `Node.__enter__`/`__exit__` (`csg.py:201-209`) push/pop `self`. So:

```python
with translate([1,0,0]):     # translate() returns a Translate node, appends it, __enter__ pushes it
    cube()                   # cube() appends Polyhedron to the Translate's children
```

`render()` context manager (`contexts.py:95-127`) creates a `Rendering`, whose `.node` is a top-level `csg.Group` (lazy-union on) or `csg.Union`, pushes it as the context, yields, pops, and forces `r.output`.

`Rendering.output` (`contexts.py:28-45`) calls `render_geom(self.node, …)` then flattens groups into a flat list of `Manifold`/`CrossSection`/`AttributedNode`.

### `runtime/modules.py` — the OpenSCAD builtin modules

All `@module(builds_group=False)`. Each returns a `csg.Node`:

- CSG ops: `union`, `group`, `difference`, `intersection` → `csg.Union/Group/Difference/Intersection`.
- Transforms: `multmatrix`, `translate`, `rotate`, `scale`, `mirror`.
- Primitives 3D: `cube` → `csg.Polyhedron(geom=lambda: Manifold.cube(...))`; `sphere`/`cylinder` build a `trimesh.creation.revolve` linestring then `trimesh2manifold`; `polyhedron`.
- Primitives 2D: `square`, `circle` → `csg.Polygon` with a `CrossSection`; `polygon`.
- `linear_extrude`, `rotate_extrude`, `offset`, `hull`, `minkowski`, `text`, `load` (= OpenSCAD `import`).
- Modifiers: `debug`/`background`/`root` → `csg.Modified`.
- `echo` writes to stderr.

`$fn/$fa/$fs` flow in as keyword args `_fn/_fa/_fs` and get resolved against `vars` by `_resolve_specials` (`modules.py:60-67`).

### `runtime/maths.py`, `runtime/functions.py`

`maths.py` — OpenSCAD math builtins. Trig functions are degree-based (`sin_degrees` etc.). `functions.py` — `str` (OpenSCAD value-to-string), `version`, the `is_*` type predicates.

### `runtime/variables.py` — the `vars` global object

`Globals` (`variables.py:14-56`) overrides `__getattribute__` to **return a getter function**, not the value: `vars.a` is `lambda: <value>`, hence transpiled reads are `vars.a()`. `__setattr__` stores the value (unless it's a CLI `-D` `_Override`, which is immutable). `vars(**kwargs)` returns a new `Globals` layering `_Override`s, usable as a context manager (`__enter__`/`__exit__` swap the module-global `vars`). This realizes OpenSCAD's special-variable dynamic scoping.

### `runtime_utils.py`

`wrap_list(a)` (286-306) — the central list policy: `_materialize_with_shape` walks a (possibly nested, possibly generator) list, computes a uniform shape + numeric dtype, and returns a `np.ndarray` if homogeneous-numeric, else a `ListWrapper` (a `list` subclass with element-wise `+ - * /`). `Range` (165-206) is a lazy OpenSCAD range with OpenSCAD's exact `__len__` semantics (ported from OpenSCAD's C++). `concat`, `mult`, `div`, `plus`, `equal`, `assert_expr` are the runtime fallbacks the transpiler emits when types are unknown.

---

## 6. CSG layer — `scad2py/csg.py` (and `csg2.py`)

`csg.py` is the in-memory CSG graph the runtime builds and the renderer consumes. Independent of `scadast.py` and `pyast` — a third tree type.

### Node hierarchy

`Node` (`csg.py:152-321`) — base dataclass with `args`, `kwargs`, `children`, cached `_str` and `_key`. Key behaviours:
- `__call__(children)` appends children and returns self.
- `__enter__`/`__exit__` push/pop `Context`.
- `to_str(indent)` serializes to OpenSCAD `.csg` syntax (`call_to_str()` + `children_to_str()`); `__str__` caches it.
- `key` property → `FastKey.intern(str(self))` — content-addressed identity used for caching/dedup.
- `dim` / `colorful` — abstract; computed per subclass.

Subclasses:

| Group | `Modified` (modifier `BACKGROUND/DEBUG/ROOT`), `AbstractGroupNode` → `Root`, `Union`, `Group`, `Intersection`, `Difference` |
| Transforms | `AbstractTransform` → `Multmatrix`, `Translate`, `Mirror`, `Scale`, `Rotate` — each exposes a `.matrix` (4×4 numpy); `to_str` re-expresses as `multmatrix` |
| Ops | `Minkowski`, `Hull`, `Fill`, `Resize`, `LinearExtrude` (with `height/center/convexity/twist/slices/scale`), `RotateExtrude`, `Offset` (`r/delta/chamfer`), `Projection`, `Color` (carries `color` ndarray) |
| Leaves | `Leaf` → `Text`, `Polygon` (carries a `CrossSection` or builder), `Polyhedron` (a `Manifold` or builder), `Import`, `Surface` |

`AbstractGroupNode.dim` (358-374) infers 2D vs 3D by `max` over children's `dim`; `colorful` is `any(child.colorful)`. Leaves carry geometry **lazily** — `Polygon.geom`/`Polyhedron.geom` may be a callable, evaluated on first `to_geom()` (so geometry is only computed when actually rendered).

`FastKey` lives in `utils.py:56-81`: a `HashedString` wrapper, interned in a global dict, identity-comparable — fast set/dict keys for the caching visitor.

### Visitor

`Visitor`/`DefaultVisitor` (`csg.py:23-142`) — same shape as the scadast visitor: one method per node type, routed through `visitNode`/`visitAbstractTransform`/`visitLeaf`. `visitChildren` recurses; `transform_children` writes back. `traverse`/`collect` helpers.

### `csg2.py`

`csg2.py` is a **31-line abandoned alternative** CSG representation: a single flat `Node` dataclass with an `op: NodeType` enum (`PARALLEL/UNION/DIFFERENCE/INTERSECTION/OFFSET/MINKOWSKI/…`) and inline `color`/`transform`/`geom`. The file header lists algebraic CSG rewrite rules ("color(a + b) = color(a) + color(b)", etc.) — evidently a design sketch for an optimizer. Nothing imports `csg2`; it is dead code. The algebraic-rewrite ideas it sketches were partially realized in `SceneTransformer` instead.

---

## 7. Rendering — `scad2py/rendering/`

The csg.Node tree → manifold3d geometry. Files: `rendering.py`, `manifold_renderer.py`, `caching.py`, `modifiers_rendering.py`. `geom_types.py` defines `Geometry = Union[Manifold, CrossSection, AttributedNode]` and `AttributedNode` (a `{color, transform, child}` wrapper used to bubble attributes up the tree).

### `render_geom` — `rendering.py:26-73`

The driver. Given a `csg.Node`:

1. `ModifiersVisitor` (`modifiers_rendering.py:47-86`) collects `%`/`#`/`!` nodes: background, debug, and root nodes (each captured with its accumulated transform). A `!` ("root") node, if present, replaces the whole tree.
2. `CachingVisitor` (`caching.py:26-48`) walks the tree keyed by `FastKey`; nodes seen more than once get `refcount > 1`, and `is_reused(n)` reports that.
3. For each renderable sub-tree, three stacked passes:
   - `n.accept(caching)` — dedup identical sub-trees to *shared node objects*.
   - `n.accept(SceneTransformer)` — see below.
   - `n.accept(ManifoldRenderer)` — produce geometry.
4. Background/debug nodes (in `preview` mode) are rendered separately, wrapped in `csg.Color` with `background_node_color` / `debug_node_color`.
5. `NonRenderableModifiedNodesRemover` strips modifier nodes that shouldn't appear in the final geometry.

### `SceneTransformer` — `rendering.py:112-293`

A `DefaultVisitor(transform_children=True)`. Its docstring (113-132) is the spec: it normalizes the tree into a "scene" — a top-level `Group` of optionally-`Color`ed nodes — by **bubbling colors and transforms upward**, merging same-color siblings, flattening groups/unions, pushing transforms down so colored unions can bubble up, and (when colors aren't allowed for the output format) *partitioning* the scene into single-colored pieces using CSG subtraction. `traverseAbstractGroup` (155-200) is the core: it recursively descends through `Group/Union/Color/AbstractTransform`, accumulating `(color, transform)` and yielding `(Attributes, Leaf)` pairs. This is where the algebraic CSG rules from `csg2.py`'s header comment actually live.

### `ManifoldRenderer` — `manifold_renderer.py:98-455`

A `csg.DefaultVisitor` that "blindly executes" csg ops into `manifold3d` geometry. Output type: `Manifold | CrossSection | AttributedNode | csg.Group[Geometry]`.

- `visitUnion` → `+`, `visitDifference` → `-`, `visitIntersection` → `^` on Manifold/CrossSection.
- `visitAbstractTransform` / `visitTranslate` / `visitScale` / `visitRotate` → `Manifold.transform/translate/scale/rotate`. When the render stack is all transforms/colors/groups it instead produces `AttributedNode`s (deferred transform/color) so attributes bubble to export.
- `visitColor` → either `set_properties(4, …)` baking color into vertex props, or an `AttributedNode`.
- `visitHull` → `Manifold.batch_hull` / `CrossSection.batch_hull`.
- `visitLinearExtrude` → `CrossSection.extrude(...)` (asserts `center==False` — `center=True` is a TODO, line 417).
- `visitOffset` → `CrossSection.offset(...)`.
- `visitLeaf` → `n.to_geom()` (evaluates the lazy builder).

**The `@renderer` decorator** (70-96) wraps each method with caching: if `is_reused(n)` it returns a `clone()` of the previously-rendered geometry keyed by `n.key`; `clone` (34-50) deep-copies a `Manifold`/`AttributedNode`/`Group` (a `CrossSection` is treated as immutable and returned as-is).

**Fallback to the OpenSCAD binary.** `visitNode` (375-376) — the *default* for any node without a dedicated method — calls `fallbackToOpenSCADBinary` (348-373): it exports each child to `tmp_input_N.stl`/`.svg`, writes a tiny `.scad` snippet `minkowski() { import(...); ... }`, and shells out to a **hard-coded path** `/Applications/OpenSCAD-2023.08.27.app/Contents/MacOS/OpenSCAD`, then loads `tmp_output.*` back via trimesh. The nodes that hit this fallback: `Minkowski` (unless `--enable=approx-minkowski`, which routes to `minkowski_impl.py`), `RotateExtrude`, `Fill`, `Resize`, `Projection`, `Text`, `Surface`. So those OpenSCAD features are **not implemented natively** — they require a local OpenSCAD install at that exact path.

`render_to_trimesh_scene` / `render_to_pyrender_scene` (295-309) convert the geometry list into a viewable/exportable scene; `ensure_3d` (457-465) extrudes 2D `CrossSection`s into thin slabs for 3D viewers.

---

## 8. I/O & misc

- **`io.py`** — `trimesh2manifold` / `manifold2trimesh` conversions (Trimesh ↔ `manifold3d.Manifold`, Path2D ↔ `CrossSection`), color handling, the `load()` importer. `io_fast.py` is a partial, broken (`load_xyz`, `_ply_exporters`, `..exceptions` are all undefined) experiment to build a leaner export table with lazy imports — not wired in.
- **`colors.py`** — `parse_color` (CSS hex / named colors / `[r,g,b(,a)]`), ported from OpenSCAD's `ColorNode.cc`; defines `color_scheme`, `default_color`, `background_node_color`, `debug_node_color`.
- **`calc.py`** — geometry math ported from OpenSCAD's `calc.cc`: `get_fragments_from_r` ($fn/$fa/$fs → segment count), `rotation_matrix`, `mirror_matrix`, `matrix3d_to_2d`.
- **`minkowski_impl.py`** — an experimental native Minkowski sum via convex decomposition (`trimesh.decomposition.convex_decomposition` / CoACD) + pairwise convex Minkowski. Used only under `--enable=approx-minkowski`.
- **`lazy.py`** (864 lines) — **not part of the geometry pipeline at all.** It is a custom import system: an `ImportManager` inserted into `sys.meta_path` that installs lazy module loading and "fake values" (`_FakeValue`, `_LazyModule2`, `LazyAndFakesLoader2`). Goal (per the header benchmarks): defer importing heavy deps (`numpy`, `trimesh`, `scipy`, `manifold3d`) so small models start faster — "1.7x faster" claimed. It tracks which modules actually get used and writes a "skiplist" of non-fake modules (`nonfakes*.txt` in the repo root are its output). `lazy copy.py` is an older simpler variant. Commented-out `# import scad2py.lazy` lines at the top of `__main__.py`/`cli.py` show it is currently disabled. It is an optimization side-project, orthogonal to a build123d backend.
- **`codec/register.py`** + **`openscad.pth`** — a Python *codec* import hook. `openscad.pth` contains `scad2py.codec.register`, so on interpreter start Python imports that module, which registers an `openscad` codec. A `.py` file (or module) declared with `# -*- coding: openscad -*-` would be transparently run through `transpile()` at import time — i.e. you can `import` a `.scad`-syntax file directly. The `-m scad2py.codec.register` entrypoint runs a module/script with the codec active. (Note: `register.py` still has Python-2-isms like `print >>sys.stderr` — partially bit-rotted.)
- **`customizer/`** — a Gradio web UI for OpenSCAD's "Customizer" parameters. `customizer/__init__.py:get_controls` reads the `pre_comments`/`post_comments` that `CommentsAttacher` attached to top-level `Assignment`s and turns `// [Tab]` markers and `// [min:max]` control comments into `Tab`/`Slider`/`Checkbox`/`Dropdown` controls (`controls.py`). `customizer/parser.py` + `pseudo_parser.py` + `tokrules.py` + `parsetab.py` are a **second, separate PLY parser** just for control-comment syntax. `gradio.py` builds the live UI: edit a control → re-transpile → re-render.
- **`ipython/__init__.py`** — an `%%openscad` cell magic: parse the cell, transpile, `run_cell` the generated Python; supports `{{var}}` substitution.

### Examples

`scad2py/examples/` has 33 `.scad` files, each with a committed transpiled `.py` (produced by `transpile_examples.sh`). `CSG.scad` / `CSG.py` traced below; `CSG-modules.scad` exercises modules, `if`, `color`, `$fs/$fa`; `scopes.scad` shows the alpha-renaming; `minkowski.scad`, `extrusions.scad`, `bosl2_*` stress the harder features.

### Worked example — `examples/CSG.scad` → `examples/CSG.py`

Source:
```openscad
translate([-24,0,0]) { union() { cube(15, center=true); sphere(10); } }
intersection() { cube(15, center=true); sphere(10); }
translate([24,0,0]) { difference() { cube(15, center=true); sphere(10); } }
translate([0, -30, -12]) linear_extrude(1) text("Python OpenSCAD", halign="center", valign="center");
echo(version=version());
```
Transpiled (verbatim from `CSG.py`):
```python
from scad2py import *

@module
def main():
  with translate(wrap_list([-24, 0, 0])):
    with union():
      cube(15, center=True)
      sphere(10)
  with intersection():
    cube(15, center=True)
    sphere(10)
  with translate(wrap_list([24, 0, 0])):
    with difference():
      cube(15, center=True)
      sphere(10)
  with translate(wrap_list([0, -30, -12])), linear_extrude(1): text("Python OpenSCAD", halign="center", valign="center")
  echo(version=version())

if __name__ == "__main__":
  run(main)
```
Trace of run mode:
1. `run(main)` → `with render() as r:` pushes a top-level `csg.Group` onto `Context`.
2. `main()` executes. `translate([-24,0,0])` → `@module` `translate` runs, builds `csg.Translate`, appends it to the Group, and `with` `__enter__` pushes it. `union()` → `csg.Union` appended + pushed. `cube(15, center=True)` → `csg.Polyhedron(geom=lambda: Manifold.cube([15,15,15], True))` appended to the Union's children. `sphere(10)` similarly. The `with`s pop back out.
3. The literal `[-24,0,0]` was emitted as `wrap_list([-24,0,0])` → `np.array([-24,0,0])`.
4. `text(...)` builds a `csg.Text`; `linear_extrude(1)` a `csg.LinearExtrude`. `echo(version=version())` writes `ECHO: ...` to stderr.
5. After `main()` the Group tree is: `Group[Translate[Union[cube,sphere]], Intersection[cube,sphere], Translate[difference[cube,sphere]], Translate[LinearExtrude[Text]]]`.
6. `r.output` → `render_geom`: `ModifiersVisitor` (no modifiers here), `CachingVisitor` (the two `cube(15,center=true)` and two `sphere(10)` sub-trees share a `FastKey`, so they dedup), `SceneTransformer` (flatten, push transforms), `ManifoldRenderer` (`cube`→`Manifold.cube`, `union`→`+`, `Translate`→`.translate`, `intersection`→`^`, `difference`→`-`). The `Text` node has no `visitText` → **falls back to the OpenSCAD binary**.
7. Result exported via trimesh, or shown.

---

## 9. Status — complete vs stubbed

**Working**: parser (full OpenSCAD grammar incl. comprehensions, `let`, `each`, function literals, asserts, echo); transpiler (5-pass resolve/inline/rename + codegen); runtime; csg layer; the `ManifoldRenderer` for booleans, transforms, color, hull, `linear_extrude` (center=false only), `offset`, primitives (`cube/sphere/cylinder/polyhedron/square/circle/polygon`), `import`; caching/dedup; trimesh export; the customizer UI; the IPython magic.

**Stubbed / missing / fragile**:
- `text`, `rotate_extrude`, `minkowski` — README explicitly lists these as missing. In the renderer they hit `fallbackToOpenSCADBinary` (a hard-coded `OpenSCAD-2023.08.27.app` path). Also fallback-only: `fill`, `resize`, `projection`, `surface`.
- `linear_extrude(center=True)` — `assert n.center == False` (`manifold_renderer.py:417`).
- `rotate(v=...)` (rotation about an axis vector) — `raise Exception("TODO: rotate(v=...)")` (lines 306, 334).
- The **typer is a 6-line stub** (`transpiler/typer.py`); the whole `Type` lattice in `scadast.py` is unused. Numeric-vs-non-numeric decisions are syntactic guesses.
- `transpiler/__init__.py` has several `...`-bodied visitors: `visitIfExpression`, `visitForComprehension`, `visitEach`, `visitEchoExpr` — these only work because the relevant constructs are normalized into other forms before reaching them (e.g. `each`/comprehensions are handled inside `visitList`), but standalone they'd fail.
- `io_fast.py` references undefined names — broken.
- `csg2.py` — abandoned alternative representation, unused.
- `lazy.py` — disabled (the `import scad2py.lazy` lines are commented out).
- `codec/register.py` has Python-2 syntax remnants.
- The renderer writes scratch files (`tmp.py`, `tmp_input_*.stl`, `tmp_output.*`) into the cwd.

---

## Implications for a build123d backend

build123d is a BREP/OpenCASCADE-based CAD library — `Solid`, `Compound`, `Sketch`, `Part`, with `fuse`/`cut`/`intersect` boolean ops, `extrude`, `revolve`, `loft`, `offset`, `fillet`/`chamfer`, `make_text`, and exact (not mesh) geometry. There are two clean places a build123d backend could slot in, and they answer different goals.

### Option A — a new renderer alongside `ManifoldRenderer` (`csg.Node` → build123d objects)

Write a `Build123dRenderer(csg.DefaultVisitor)` parallel to `ManifoldRenderer`, plus a `Build123dRendering` parallel to `contexts.Rendering`, returning build123d `Shape`s instead of `Manifold`/`CrossSection`.

- **Where it plugs in**: `render_geom` (`rendering/rendering.py:26-73`) is the only place that instantiates `ManifoldRenderer`. Add a backend selector (e.g. `--enable=build123d`, via the existing `get_feature`) that swaps the renderer and skips/relaxes the manifold-specific `SceneTransformer` color-baking.
- **Mapping**: `Union/Difference/Intersection` → `fuse/cut/intersect`; `AbstractTransform` → build123d `Location`/`Rotation`/`Pos`/`Scale` (note: OCCT has no non-uniform scale on solids — `csg.Scale` with non-equal factors is a problem); `Polyhedron` primitives → `Box`/`Sphere`/`Cylinder`; `Polygon` → `Sketch` primitives; `LinearExtrude` → `extrude`; `Hull` → no direct build123d equivalent (would need a convex-hull helper); `Color` → build123d's `Color` on shapes/`Compound`.
- **Big wins**: this is where build123d *most* helps — it would let scad2py natively support the currently-stubbed features. `rotate_extrude` → build123d `revolve` (exact). `text` → `make_text` / `Text` sketch. `linear_extrude(center=True, twist=…)` → `extrude` with proper params. `minkowski` is still hard (OCCT has no Minkowski sum), but `offset`/`fillet`/`chamfer` become exact. It also gives exact BREP export (STEP) — something manifold3d cannot do.
- **Costs**: build123d works in BREP, not meshes. The whole `csg.Node` `dim`/`colorful` machinery, the `FastKey` content-hash caching (`@renderer`/`clone`), and `SceneTransformer`'s color-partitioning were all designed around mesh semantics and manifold3d's API; much of `SceneTransformer` (color baking into vertex properties, 2D/3D mismatch removal) is manifold-specific and would need a build123d-flavored variant. The lazy `Polygon.geom`/`Polyhedron.geom` builders currently return `CrossSection`/`Manifold` — they'd need build123d-producing variants, or the builders move into the renderer.
- **Effort**: medium. It reuses the entire front end (parser, transpiler, runtime, csg tree) unchanged. The new code is ~one visitor file + one rendering wrapper + a backend flag.

### Option B — a new transpilation target emitting build123d Python source

Make the transpiler emit Python that imports `build123d` directly instead of `scad2py`, e.g. `with BuildPart(): with Locations((-24,0,0)): Box(15,15,15)` or the algebraic `Box(...) + Sphere(...)` style.

- **Where it plugs in**: a new `Transpiler` subclass (or a parameterized one) in `transpiler/__init__.py`. Most of the front end (`parser`, `resolver`, `Inliner`, `pick_unique_names`, `pyast`) is reusable as-is. The changes are concentrated in `visitModuleInstantiation`, `visitModuleDefinition`, the `_make_*_annot` decorators, `visitUnit`'s prelude, and the runtime-helper references (`mult`/`div`/`wrap_list`/`vars`).
- **The hard mismatch**: scad2py's whole runtime model is *imperative side effects* — `@module` functions append to a `Context` stack; `with` statements push context. build123d has two paradigms: the **Builder API** (also context-manager / stack based — `with BuildPart(): …`, which actually maps *naturally* onto scad2py's `with translate(): …`) and the **Algebra API** (pure `+`/`-`/`*` on objects). The Builder API is the closer fit, so Option B is more feasible than it first looks. But scad2py-specific runtime semantics — OpenSCAD special-variable dynamic scoping via `vars`, the `wrap_list`/`ListWrapper`/numpy list policy, OpenSCAD `Range` semantics, degree-based trig, `concat`/`mult`/`div` fallbacks — have **no build123d equivalent** and would still need a thin scad2py runtime shim imported alongside build123d. So "pure build123d source with no scad2py dependency" is not realistic; "build123d for geometry + small scad2py runtime for OpenSCAD value semantics" is.
- **Effort**: medium-high, and it produces *less* than Option A per unit of work — it gives readable build123d source (nice for users who want to "graduate" off OpenSCAD), but the actual rendering quality/feature coverage still depends on build123d, exactly as Option A does.

### Recommendation framing

The two options are not mutually exclusive and target different audiences:

- **Option A is the smaller, lower-risk change and unlocks the missing features fastest.** It treats build123d purely as a geometry kernel, keeps the entire scad2py front end, and slots in at one well-defined seam (`render_geom`). It immediately fixes `text`/`rotate_extrude`/exact-`offset` and adds STEP export, removing the embarrassing shell-out to a hard-coded OpenSCAD binary. Recommended as the first step.
- **Option B is a product feature** ("export my model as clean build123d code"), valuable for migration off OpenSCAD, but it does not improve rendering on its own and still needs a scad2py runtime shim. Worth doing after A, reusing A's csg→build123d mapping knowledge.

A natural intermediate: implement Option A's `csg.Node → build123d` mapping as a standalone, well-tested module first; Option B's codegen can then target the *same* build123d call vocabulary, and the csg-tree renderer doubles as the reference semantics for the codegen.

---

## Open questions

1. **Non-uniform scale**: OpenSCAD `scale([2,1,1])` and shear via `multmatrix` are routine; OCCT/build123d cannot non-uniformly scale a `Solid` while keeping it a valid BREP. How should a build123d backend handle `csg.Scale`/`csg.Multmatrix` with non-rigid matrices — mesh-and-rescale (losing BREP), `GpsTransform` approximations, or reject?
2. **Minkowski**: build123d has no Minkowski sum. Keep the manifold3d/`minkowski_impl.py` path for `minkowski` even when the build123d backend is selected (a hybrid renderer), or drop it?
3. **2D model**: scad2py uses `manifold3d.CrossSection` for 2D; build123d uses `Sketch`/`Face`. The whole `dim` inference and 2D/3D mismatch handling in `SceneTransformer` is manifold-shaped — how much survives?
4. **Caching**: the `FastKey` content-hash dedup (`@renderer`/`clone`) gives big speedups on repetitive models. build123d/OCCT `Shape`s are not cheaply cloneable the way `Manifold` meshes are — does the caching layer still pay off, or does it need rethinking?
5. **The typer**: a real `typer.py` would let the transpiler emit cleaner Python (fewer `mult`/`div`/`wrap_list` wrappers) and could also inform the build123d backend (e.g. statically knowing a `scale` is uniform). Is finishing the typer a prerequisite, or orthogonal?
6. **Color semantics**: manifold3d bakes color into vertex properties; build123d attaches `Color` to shapes/`Compound`s. `SceneTransformer`'s color-partitioning (cutting the scene into single-colored CSG pieces) is needed only because mesh formats can't carry per-region color — with build123d + STEP/GLTF this partitioning may be unnecessary. Can it be skipped for the build123d backend?
7. **Lazy geometry builders**: `Polygon.geom`/`Polyhedron.geom` callables currently close over `Manifold`/`CrossSection` constructors. For Option A they must be made backend-agnostic (return a description, not a `Manifold`) or duplicated. Which?
8. **Tessellation for preview/export**: build123d/OCCT must tessellate to mesh for the trimesh/pyrender viewer and for STL/GLB. What tessellation tolerance, and does `$fn/$fa/$fs` (currently consumed by `calc.get_fragments_from_r`) still mean anything, or does it map to OCCT deflection parameters?
