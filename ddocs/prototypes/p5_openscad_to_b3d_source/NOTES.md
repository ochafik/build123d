# p5 — OpenSCAD → build123d *source* transpiler — findings

Prototype for **Goal: make scad2py emit readable build123d Python source** from
`.scad` files (build123d as a *transpilation target*, distinct from p4's
*runtime backend*).

- `scad2b3d.py` — the transpiler. `.scad` → build123d `.py` source string.
- `examples/*.scad` — inputs (copied from `scad2py/examples/`, plus a synthetic
  `polyhedron.scad`).
- `examples/*.py` — generated build123d source. All 8 run with the venv python.
- `examples/CSG.stl` — STL exported by running `examples/CSG.py`.

Environment: `/Users/ochafik/github/.ddocs-venv-transpiler/` (Python 3.10) with
`build123d` (editable), `ply`, `numpy`, `typeguard`.

---

## 1. How it works

`scad2b3d.py` reuses **only scad2py's front end**:

- `scad2py.parser.parse_units(file)` → OpenSCAD AST (`scadast.Unit`).
- `scad2py.scadast` — the AST node classes + `collect`/`traverse` helpers.

It does **not** touch scad2py's transpiler / runtime / renderer. To import the
parser without dragging in scad2py's heavy package `__init__.py` (which imports
`trimesh`, `manifold3d`, the whole runtime), the prototype registers an *empty*
`scad2py` package object in `sys.modules` first; Python then loads only the
submodules `scad2py.parser`, `scad2py.scadast`, `scad2py.typechecking`. This
shim is ~6 lines and worked first try — **the front end is cleanly separable**.

The emitter is a hand-written visitor over the OpenSCAD AST that returns
build123d **algebra-API** source. Algebra API (`Box(...) + Sphere(...)`,
`Pos(v) * shape`) was chosen over the Builder API because it is
expression-oriented and mirrors OpenSCAD's nested-call structure 1:1 — a
`difference() { a(); b(); }` becomes literally `(a) - (b)`.

Each generated file = a header (`from build123d import *`), a small **runtime
shim** of OpenSCAD-semantics helpers, the user functions/modules, the top-level
geometry, and a `__main__` block that prints + `export_stl`s.

---

## 2. Coverage

| OpenSCAD construct | build123d emission | Status |
|---|---|---|
| `cube` | `Box(l,w,h, align=MIN\|CENTER)` | ✅ faithful |
| `sphere` | `Sphere(r)` | ✅ (intent fidelity — see §4) |
| `cylinder` (r1=r2 / r1≠r2 / d / center) | `Cylinder` / `Cone` (+ `Pos` z-shift) | ✅ faithful |
| `square`, `circle`, `polygon` | `Rectangle`, `Circle`, `Polygon` | ✅ faithful |
| `circle($fn=3..12)` | `RegularPolygon(r, n)` | ✅ heuristic (small `$fn` = intentional facets) |
| `polyhedron` | `Solid(Shell(Face...))` via fan-triangulation | ⚠️ works on clean convex input; OCCT-fragile otherwise |
| `text` | `Text(...)` | ⚠️ runs; glyph metrics differ from OpenSCAD |
| `union` / `difference` / `intersection` | `+` / `-` / `&` | ✅ faithful |
| top-level / `for` / module-body aggregation | `Compound(children=[...])` | ✅ — see §3 |
| `translate` / `rotate` / `mirror` | `Pos(v)*x` / `Rot(...)*x` / `mirror(...)` | ✅ faithful |
| `rotate(a,v)` (axis-angle) | `Rotation().rotate(Axis(...))` | ⚠️ best-effort |
| `scale` (uniform & non-uniform) | `scale(x, by=...)` | ✅ |
| `multmatrix` | `Location` from 4×4 matrix | ⚠️ rigid part only |
| `color` | `_colored(x, Color(...))` → `shape.color` | ✅ faithful |
| `linear_extrude` (plain) | `extrude(sketch, amount=h)` | ✅ faithful |
| `linear_extrude(twist=)` | — | ❌ `raise NotImplementedError` |
| `rotate_extrude` | `revolve(sketch, Axis.Z, revolution_arc=)` | ✅ faithful |
| `offset` | `offset(x, amount=, kind=)` | ✅ |
| `for` / `if` (geometry) | Python `for` / `if` | ✅ faithful |
| `function` | Python `def ...: return expr` | ✅ faithful |
| `module` | Python `def` returning a Shape | ✅ for object modules; ⚠️ operator modules (see §5) |
| `let`, list comprehensions, `each`, ranges | IIFE-lambda / list-comp / `_orange` | ✅ |
| degree trig `sin/cos/...` | `_sin = lambda: sin(radians(...))` shims | ✅ faithful |
| `echo` / `assert` | `print('ECHO:',...)` / `assert` | ✅ |
| `$fn/$fa/$fs` | dropped (exact BREP) or `RegularPolygon` heuristic | ⚠️ no dynamic scoping |
| `hull` | — | ❌ `raise NotImplementedError` |
| `minkowski` | — | ❌ `raise NotImplementedError` |
| `include`/`use` | — | ❌ not inlined (single-file prototype) |
| `surface`, `projection`, `fill`, `resize` | — | ❌ `raise NotImplementedError` |

**8/8 example files transpile, run with the venv python, and export an STL**
(`cube`, `cylinder`, `CSG`, `example`, `colors`, `CSG-modules`, `extrusions`,
`polyhedron`). Run times: 2.5–4.5 s except `example.py` at ~31 s (2091 boxes —
see §3).

Honesty policy held: every untranslatable construct emits a `# TODO` note
**and** a `raise NotImplementedError(...)` in the generated code, so a file is
never silently wrong — it imports and runs up to the unsupported node.

---

## 3. The performance cliff — and the fix that is also more faithful

The first cut folded sibling geometry with `sum(parts[1:], parts[0])` — i.e.
pairwise `+` boolean unions. On `colors.scad` (37×37 = **1369** boxes) and
`example.scad` (**2091** boxes) the generated file **hung**: 1368 sequential
OCCT `BRepAlgoAPI_Fuse` calls, each on a growing solid, each followed by
`ShapeUpgrade_UnifySameDomain` — the exact O(n²) cliff documented in
`01-build123d-architecture.md §6.4`.

The fix is also the *more correct* semantics. OpenSCAD's top level, `for`
bodies, and module bodies are an **implicit CSG group node**, not a boolean
union — disjoint parts are fused into one mesh only at F6 *render*. The
transpiler now emits those aggregations as `Compound(children=[...])` (O(n),
no kernel boolean) and reserves real `+` for *explicit* `union()`. After this
change `colors.py` (1369 parts) runs in 4.5 s and `example.py` completes.

**Finding:** a build123d source target must *not* naively map OpenSCAD's
"everything implicitly unions" onto `+`. Map implicit aggregation to `Compound`
and only explicit `union()` to `+`. Even then, an explicit `union()` of
hundreds of overlapping faceted parts (`hull`-heavy BOSL2-style code) will hit
the cliff — that is build123d's problem, not the transpiler's, and is the
motivation for the manifold3d backend (the *other* half of this project).

---

## 4. The `$fn` / two-stage-evaluation gap

OpenSCAD `circle(r, $fn=6)` **is a hexagon** — `$fn` is geometric truth, not a
render hint (`05-openscad-vs-build123d-model.md §3.1`). build123d's `Circle` is
an exact circle. The prototype takes the **intent-fidelity** policy: emit exact
`Circle`/`Sphere`/`Cylinder` and *ignore* `$fn/$fa/$fs`, with one heuristic —
a small explicit `$fn` (3–12) on a `circle` is treated as intentional faceting
and emitted as `RegularPolygon`. This is a documented, surfaced choice; the
generated code is cleaner BREP but does **not** reproduce OpenSCAD's mesh
vertex-for-vertex.

`$fn` is also **dynamically scoped** in OpenSCAD (`$fn` on a parent applies to
all descendants). The prototype does *not* model this — `$fn`/`$fa`/`$fs`
assignments become plain Python globals (`_dollar_fn`), with a note. A faithful
transpiler would have to thread resolved facet counts down the tree (scad2py's
runtime does this with `_fn/_fa/_fs` kwargs + a dynamic-scope shim). For an
*exact-BREP* target this gap mostly does not matter; for a *faithful-mesh*
target it is essential and hard.

The deeper "two-stage evaluation gap": OpenSCAD first *executes the script*
(unrolling `for`/`if`/comprehensions, expanding modules) to build a CSG tree,
*then* renders. build123d has no such split — every call eagerly hits the
kernel. The transpiler bridges this by emitting **Python control flow**
(`for`, `if`, comprehensions, `def`) for stage-1 constructs and **build123d
calls** for the geometry leaves. This works well because Python *is* an
imperative language with the control flow OpenSCAD's stage 1 needs — stage-1
constructs simply become ordinary Python and never reach build123d. The gap is
real only for `$fn` scoping and for OpenSCAD's "assignments are pulled up,
last wins" rule (the prototype preserves textual order instead — correct for
the common case, wrong for genuinely out-of-order `.scad`).

---

## 5. What is fundamentally hard

- **Operator modules with `children()`.** OpenSCAD operator modules
  (`module frame() { difference(){ children(); ... } }`) re-instantiate their
  children lazily. The prototype lowers a module to a Python `def` and, if it
  references `children()`, passes a `_children` list parameter. This works for
  simple cases but OpenSCAD's `children(i)` indexing, `$children` count, and
  *lazy* re-instantiation (a child used twice is evaluated twice with different
  `$`-vars) do not have a clean Python-function equivalent. **Object** modules
  (leaf geometry) transpile perfectly; **operator** modules are best-effort.

- **Nested module scoping.** `CSG-modules.scad` defines `module line()` *inside*
  `module helpers()`. The prototype hoists *all* module/function defs to file
  level (Python needs names defined before use). This is correct only while
  names don't collide; a production transpiler must alpha-rename, exactly as
  scad2py's `pick_unique_names` pass does. The prototype mangles a module that
  collides with a same-named function (`_mod_foo`) but does not handle two
  modules of the same name in different scopes.

- **`hull` / `minkowski`.** No BREP algorithm. `hull` *could* be approximated
  via `ConvexPolyhedron` of sampled vertices (faceted, loses spherical caps);
  `minkowski` has no route at all. Both emit `raise NotImplementedError`.
  `box.scad` and `a11y.scad` from scad2py's examples use `hull()` heavily and
  are therefore **not** transpilable by this prototype — an honest finding.

- **`polyhedron` robustness.** Fan-triangulation + `Solid(Shell(faces))` works
  for the clean convex `polyhedron.scad` test, but OCCT rejects non-planar /
  non-manifold / degenerate faces that Manifold ingests happily.

- **OpenSCAD value semantics.** Degree trig, inclusive ranges, `concat`/`str`,
  the type predicates — all needed a small **runtime shim** baked into every
  generated file (`_sin`, `_orange`, `_concat`, `_is_num`, ...). "Pure
  build123d source, zero dependencies" is **not** achievable; "build123d for
  geometry + a ~120-line self-contained shim for OpenSCAD value semantics" is.
  The shim is self-contained (no scad2py import), which keeps generated files
  portable.

---

## 6. Code-quality assessment — sample

The generated code is **genuinely readable and idiomatic** for the clean
subset. `examples/CSG.scad` →

```python
# ---- top-level geometry ----
_parts = []
_parts.append((Pos(-24, 0, 0) * ((Box(15, 15, 15)) + (Sphere(10)))))
_parts.append(((Box(15, 15, 15)) & (Sphere(10))))
_parts.append((Pos(24, 0, 0) * ((Box(15, 15, 15)) - (Sphere(10)))))
_parts.append((Pos(0, -30, -12) * extrude(
    Text('Python OpenSCAD', font_size=10,
         align=(Align.CENTER, Align.CENTER)), amount=1)))
result = _group(_parts)
```

A human can read that and immediately keep editing it in build123d. `cube.scad`
→ `Box(1, 1, 1, align=(Align.MIN, Align.MIN, Align.MIN))` — exactly what a
build123d author would hand-write. `CSG-modules.scad` (nested modules, `if`,
booleans, color) produces a clean module-per-`def` file that runs.

Rough edges: extra parentheses (`((Box(15,15,15)) + (Sphere(10)))`); the
`_parts.append(...)` accumulator pattern instead of a single expression; the
runtime shim is prepended to *every* file (~120 lines of boilerplate — a
production version would `import scad2py_b3d_runtime` instead). None of these
affect correctness; a light pretty-printing pass and a shared runtime module
would close the gap.

---

## 7. Recommendation — is "build123d as a transpilation target" worthwhile?

**Yes, conditionally — and it is a genuinely different product from p4.**

The prototype derisks the core question: the OpenSCAD → build123d *source*
mapping is **mostly clean and the generated code is genuinely usable**. The
front end separates cleanly; the algebra API is the right target; stage-1
constructs fall out as ordinary Python; the common geometry subset
(primitives, booleans, transforms, extrude/revolve, `for`/`if`, functions,
object modules) transpiles faithfully and runs.

It is worthwhile **as a one-time migration / "graduate off OpenSCAD" tool**,
not as a render path:

- **Use the source transpiler (p5) when** the user wants to *leave* OpenSCAD —
  port a `.scad` design into a build123d codebase they will edit, fillet,
  parameterize, and export to STEP. The output is a readable starting point a
  human owns. One-time, human-in-the-loop, tolerant of the gaps.
- **Use the runtime backend (p4) when** the user wants to *keep writing
  OpenSCAD* but render/​export through build123d (exact `revolve`, exact
  `offset`, STEP export) without rewriting anything. Every run, fully
  automatic, must handle 100 % of the input or fail loudly.

Consequently they have **opposite robustness bars**. p4 (runtime backend) must
faithfully execute *every* construct of *every* input file or it is broken — a
single unsupported node aborts a render. p5 (source transpiler) is allowed to
emit a `# TODO` / `NotImplementedError` for `hull`/`minkowski`/`twist` and hand
the user a 95 %-complete file to finish by hand — that is an acceptable, even
expected, outcome for a migration tool. p5 is therefore the **lower-risk, lower
total-effort** deliverable: it reuses scad2py's parser unchanged, needs no csg
tree, no renderer, no caching layer, and tolerates partial coverage.

**Recommended path:** build the runtime backend (p4) first if the goal is
"better OpenSCAD rendering" — it is the smaller change against scad2py's
existing architecture and unlocks the missing features for *all* input. Add the
source transpiler (p5) as a *second, separate product* for migration. They
share almost nothing in implementation (p4 walks scad2py's `csg.Node` tree; p5
walks the OpenSCAD AST and emits text) — but p5's emission vocabulary and p4's
csg→build123d mapping should agree on the same build123d call set so the two
stay consistent.

**The decisive caveat for p5:** the generated code is only as good as
build123d's kernel. The O(n²) boolean cliff (§3) is real — mitigated here by
emitting `Compound` for implicit aggregation, but an explicit `union()` of
hundreds of overlapping faceted solids will still be slow or fail. Faceted-mesh
fidelity (`$fn` hexagons) and `hull`/`minkowski` are unreachable without a
mesh backend. So even the "clean export" product benefits from the manifold3d
backend being available underneath build123d.

---

## 8. Honest limitations of this prototype

- Single file only — no `include`/`use` inlining (scad2py's `Inliner` would
  port directly).
- No alpha-renaming — relies on no scope/name collisions (scad2py's
  `pick_unique_names` is the fix).
- `$fn/$fa/$fs` dynamic scoping not modeled; intent-fidelity only.
- OpenSCAD "assignments pulled up, last-wins" not modeled — textual order kept.
- `hull`, `minkowski`, `linear_extrude(twist)`, `surface`, `projection`,
  `fill`, `resize` emit `NotImplementedError` by design.
- `multmatrix` keeps only the rigid part; shear is lost.
- `polyhedron` is OCCT-fragile on non-planar / non-manifold input.
- Operator-module `children()` is best-effort; `children(i)`/`$children`
  partial.
- The runtime shim is duplicated into every generated file rather than
  imported.
