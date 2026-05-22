# Design — scad2py × build123d Integration (Goal 2)

**Author:** integration-architect agent for ochafik · **Date:** 2026-05-22
**Status:** Design — research and prototyping complete; ready for build.
**Scope:** Goal 2 — make ochafik's `scad2py` OpenSCAD parser/interpreter/transpiler
use **build123d** as a geometry backend and as a transpilation target.

> **Companion design doc.** Goal 2 depends on **Goal 1**, the manifold3d mesh
> backend inside build123d, designed concurrently in
> `ddocs/design/design-manifold-in-build123d.md`. Wherever this doc says
> "the manifold backend", it means the deliverable of that doc. At the time of
> writing that file does not yet exist; this design references it by name and
> relies on the research docs (`research/03`, `research/05` §5, `p2`, `p3`) for
> the manifold-backend facts it needs.

> **Inputs.** This design synthesizes: `research/04-scad2py-architecture.md`,
> `research/05-openscad-vs-build123d-model.md`,
> `research/01-build123d-architecture.md`, `research/06-license-analysis.md`,
> `research/08-caching-infrastructure.md`,
> `research/09-cadquery-ddocs-cross-comparison.md`,
> `design/scad2py-gpl-remediation.md`, and prototypes
> `prototypes/p4_scad2py_to_b3d/`, `prototypes/p5_openscad_to_b3d_source/`,
> `prototypes/p6_clean_room_scad2py_math/`. All `file.py:line` references were
> taken from those docs (which read the sources directly).

> **Not legal advice.** §6 summarizes a license analysis; items marked
> "→ counsel" need a lawyer's sign-off before public distribution.

---

## 0. TL;DR

- **Ship two products, both already prototyped.** **Option A — a runtime
  backend** (`Build123dRenderer`, a sibling of scad2py's `ManifoldRenderer`,
  `csg.Node` → build123d `Shape`) for users who keep authoring in OpenSCAD and
  want exact BREP / STEP output. **Option B — a source transpiler**
  (`.scad` → readable build123d Python) for users doing a one-time migration
  off OpenSCAD. They share scad2py's entire front end and diverge only at the
  back: A walks the `csg.Node` tree, B walks the OpenSCAD AST and emits text.
- **Build order: Phase 0 (GPL remediation) → Option A → Option B.** Option A is
  the smaller change, slots into one well-defined seam (`render_geom`), reuses
  the front end unchanged, and unlocks exact `revolve`/`offset`/`text` + STEP
  export for *all* input. Option B is lower-risk per se (tolerant of partial
  coverage) but lower payoff and still needs a runtime shim — it is a second,
  separate product. `p4` and `p5` both recommend this order.
- **The runtime backend must be a *hybrid*, not a replacement.** `minkowski`,
  3D `hull` of curved solids, and large/dense CSG trees have **no faithful or
  affordable BREP form**. Those `csg.Node` subtrees route to the **manifold
  backend** (Goal 1); the clean exact/parametric subset routes to build123d
  BREP. The router picks per-subtree and converts at the boundary
  (tessellate → mesh; mesh → sew). This is the central architectural decision.
- **GPL is Phase 0 and gates everything.** `scad2py/calc.py` and
  `scad2py/colors.py` are GPL-2.0+ ports of OpenSCAD C++; until they are
  swapped for the `p6` clean-room rewrites, scad2py is a GPL combined work and
  cannot be merged into / co-distributed with Apache-2.0 build123d. See
  `design/scad2py-gpl-remediation.md`.
- **Merge mechanics:** ship scad2py as a **sibling Apache-2.0 package
  (`scad2py`) that depends on `build123d`**, not as code merged into the
  build123d tree — see §6.3 for the reasoning.
- **Top risks:** (1) `minkowski`/3D-`hull` gaps — mitigated by the hybrid;
  (2) the OCCT O(n²) boolean cliff — mitigated by `Compound`-for-implicit-union
  and routing big trees to manifold; (3) the mixed-dimension `Compound` STEP
  sign bug — mitigated by a dimension-aware scene model; (4) the four scad2py
  front-end bugs `p4` surfaced; (5) non-uniform scale / shear losing exact BREP.

---

## 1. Goals & non-goals

### 1.1 Goals

- **G1.** Give scad2py a build123d **runtime geometry backend** so an OpenSCAD
  file rendered through scad2py produces exact OpenCASCADE BREP and can export
  **STEP** (which manifold3d cannot do), with exact analytic `cylinder`/
  `circle`/`revolve`/`offset`.
- **G2.** Give scad2py a build123d **source-transpilation target** so a `.scad`
  file can be converted to readable, editable build123d Python a user owns.
- **G3.** Keep scad2py's front end (parser, AST, transpiler, runtime, `csg`
  tree) **unchanged and shared** by both products and by the existing
  manifold3d render path — three back ends, one front end.
- **G4.** Be **honest about gaps**: where build123d's kernel cannot faithfully
  represent an OpenSCAD operation, either route it to the manifold backend or
  fail loudly — never silently produce wrong geometry.
- **G5.** Make scad2py **legally mergeable** with Apache-2.0 build123d
  (Phase 0).
- **G6.** Preserve scad2py's content-addressed caching where it still pays off.

### 1.2 Non-goals

- **Not** vertex-for-vertex reproduction of OpenSCAD's faceted STL. The
  build123d backend's value is *exact* geometry; faithful-mesh fidelity is the
  manifold path's job and is an explicit opt-in resolution mode, not the
  default (§4.4).
- **Not** replacing scad2py's manifold3d renderer. It stays — both as the
  fast/robust default for mesh-shaped workloads and as the hybrid's fallback.
- **Not** a build123d → OpenSCAD direction. build123d geometry is a strict
  superset of OpenSCAD's; the reverse mapping is out of scope.
- **Not** finishing scad2py's dead typer (`transpiler/typer.py` is a 6-line
  stub). Useful but orthogonal — see §10 Q6.
- **Not** modifying OpenSCAD or shipping any OpenSCAD code.
- **Not** the WASM/browser packaging story (tracked separately; cross-ref
  `research/09` §5.4 — note GPL Phase 0 is a hard prerequisite for any WASM
  bundle because static linking removes the separate-process argument).

---

## 2. The two products

scad2py "does two distinct things" (`research/04` §intro): **transpile** and
**render**. A build123d integration therefore has two clean insertion points,
and they answer two different user needs. Both were prototyped; both work; both
should ship.

### 2.1 Option A — runtime backend (`Build123dRenderer`)

**What it is.** A new CSG renderer, `Build123dRenderer(csg.DefaultVisitor)`,
parallel to scad2py's `ManifoldRenderer` (`manifold_renderer.py:98`). It walks
the `csg.Node` tree scad2py's runtime builds and produces build123d `Shape`
objects (exact OCCT BREP) instead of `manifold3d.Manifold` / `CrossSection`
meshes. Selected via a backend flag through the existing `get_feature`
mechanism (`args.py`, e.g. `--enable=build123d`).

**Who it is for.** Users who **keep authoring in OpenSCAD** but want
build123d-quality output: exact BREP, valid `Solid`s, STEP export, exact
`revolve`/`offset`/`text`. Every run, fully automatic, zero rewriting of the
`.scad` source.

**Prototype evidence (`p4`).** `Build123dRenderer` runs end-to-end.
**15/33 scad2py examples** render to valid build123d with clean STEP roundtrips
(`p4/batch_results.txt`). `cube.scad`: build123d volume + bbox match scad2py's
manifold3d output **exactly**, STEP re-opens valid. `polyhedron.scad`: a raw
OpenSCAD `polyhedron(points,faces)` was sewn into a **valid OCCT `Solid`** with
clean STEP roundtrip. Of the 18 non-OK examples, **9 are not the renderer's
fault** (4 scad2py front-end bugs, 4 OCCT perf timeouts, 1 missing input); the
9 genuine geometry failures are the *expected* BREP-kernel gaps —
`hull`/`minkowski` — that motivate the hybrid (§3.4).

**Why ship it.** It is the smaller, lower-risk change: it reuses scad2py's
**entire front end unchanged** and the `csg` tree as a sufficient IR, and slots
into **one seam** (`render_geom`). It immediately fixes the embarrassing
shell-out to a hard-coded `OpenSCAD-2023.08.27.app` path
(`manifold_renderer.py:348-373`) for `text`/`rotate_extrude`, and adds STEP
export. `p4` NOTES: *"Option A is the right integration path."*

### 2.2 Option B — source transpiler (`scad2b3d`)

**What it is.** A transpiler that emits **readable build123d Python source**
from a `.scad` file — build123d as a *transpilation target*. It reuses
scad2py's parser/AST only; it does **not** touch the runtime or `csg` tree.
The emitter is a visitor over the OpenSCAD AST that returns build123d
**algebra-API** source (`Box(...) + Sphere(...)`, `Pos(v) * shape`).

**Who it is for.** Users doing a **one-time migration off OpenSCAD** — porting
a `.scad` design into a build123d codebase they will then edit, fillet,
parameterize, and export themselves. Human-in-the-loop; tolerant of gaps.

**Prototype evidence (`p5`).** `scad2b3d.py` works: **8/8 example files
transpile, run, and export STL** (`cube`, `cylinder`, `CSG`, `example`,
`colors`, `CSG-modules`, `extrusions`, `polyhedron`). The generated code is
*genuinely readable and idiomatic* for the clean subset (`p5` §6 sample).
Untranslatable constructs emit a `# TODO` **and** a `raise NotImplementedError`
— a file is never silently wrong (`p5` §2 honesty policy).

**Why ship it.** It is a genuinely different product with **opposite
robustness bars** (`p5` §7). The runtime backend (A) must execute *every*
construct of *every* input or it is broken — one unsupported node aborts a
render. The source transpiler (B) is *allowed* to hand the user a 95%-complete
file with a `NotImplementedError` for `hull`/`minkowski`/`twist` to finish by
hand — that is an acceptable, expected outcome for a migration tool. B is the
lower-effort deliverable per unit of coverage: no `csg` tree, no renderer, no
caching layer.

### 2.3 Why both, and how they share the front end

The two are **not redundant** and **not mutually exclusive**. They target
different users (keep-authoring-OpenSCAD vs leave-OpenSCAD), have opposite
robustness contracts, and share almost nothing in their *back* halves — A walks
`csg.Node`, B walks the OpenSCAD AST and emits text. What they **do** share is
scad2py's front end:

```
                       ┌────────────────── shared front end ──────────────────┐
.scad ──▶ PLY parser ──▶ OpenSCAD AST ──▶ resolve/inline/rename ──▶ (csg tree IR)
              │            (scadast.py)      (transpiler 5 passes)        │
              │                                                          │
   Option B taps the AST here ──────────────┐                            │
                                            ▼                            ▼
                              scad2b3d emitter            scad2py runtime builds csg.Node
                                            │                            │
                                            ▼                            ▼
                              build123d .py source         render_geom seam ──┬─▶ ManifoldRenderer (existing)
                                                                              ├─▶ Build123dRenderer (Option A, new)
                                                                              └─▶ hybrid router (§3.4)
```

**Shared-vocabulary rule.** Option B's emitted call set and Option A's
`csg.Node → build123d` mapping **must agree** on the same build123d API
vocabulary (same primitive choices, same `Compound`-for-implicit-union rule,
same `$fn` heuristic). Build A's mapping first as the *reference semantics*;
B's codegen then targets the same vocabulary. `research/04` §"recommendation
framing" and `p5` §7 both make this point.

**Build/ship order — recommended:**

1. **Phase 0 — GPL remediation** (§5). Gates everything.
2. **Option A — runtime backend.** Bigger payoff, one seam, reuses the whole
   front end. The `csg→build123d` mapping built here is the reference for B.
3. **Option B — source transpiler.** A second, separate product for migration,
   reusing A's mapping knowledge.

---

## 3. Architecture — Option A (runtime backend)

### 3.1 The `render_geom` seam

`render_geom` (`scad2py/rendering/rendering.py:26-73`) is **the single place**
that instantiates `ManifoldRenderer`. It is the one renderer seam. Today its
pipeline is **dedup → scene-transform → render** (`research/08` §3.5):

```python
caching = CachingVisitor()                                  # tree → DAG dedup
scene   = SceneTransformer(..., is_reused=caching.is_reused) # bubble colors/transforms
render  = ManifoldRenderer(is_reused=caching.is_reused)      # → manifold3d geometry
```

Option A adds a **backend selector** at this seam. A new `Args` field
(`--enable=build123d` via `get_feature`, `args.py`) chooses which renderer +
which scene model + which `Rendering` wrapper to instantiate:

```python
backend = args.get_feature("backend") or "manifold"   # "manifold" | "build123d"
if backend == "build123d":
    caching = CachingVisitor()
    scene   = B3DSceneModel(is_reused=caching.is_reused)   # dimension-aware, §3.3
    render  = HybridRenderer(is_reused=caching.is_reused)  # router, §3.4
else:
    ...existing manifold path unchanged...
```

`contexts.Rendering` (`contexts.py:28-45`) — whose `.output` calls
`render_geom` then flattens groups — gets a `Build123dRendering` sibling that
returns build123d `Shape`s and whose `.export(outfile, format)` understands
`step`/`stl`/`gltf`/`brep` via build123d's `exporters3d.py`. The existing
manifold-specific `SceneTransformer` color-baking
(`rendering/rendering.py:112-293`) is **not reused** by the build123d backend
(see §3.3) — it bakes color into manifold vertex properties, which build123d
does not need (build123d `Shape`s carry `Color` natively, `research/01` §4).

**Key property:** scad2py's parser, transpiler, runtime, and `csg` tree are
**untouched**. `p4` confirms this — it imported only the front end + `csg` +
runtime, never scad2py's `rendering` package, and *"no scad2py changes were
needed to render"*.

### 3.2 The `Build123dRenderer` visitor

A `csg.DefaultVisitor` with one `visitXxx` per `csg.Node` subtype, mirroring
`ManifoldRenderer`'s structure (`research/04` §7; `p4/build123d_renderer.py`).
Each method returns a build123d `Shape` (a 3D `Part`/`Solid`/`Compound` or a
2D `Sketch`/`Face`/`Compound`).

Critical design points carried from the `p4` prototype:

- **Do not call the lazy mesh builders.** scad2py lowers `cube`/`sphere`/
  `cylinder` to `csg.Polyhedron` leaves carrying a *mesh-builder lambda*
  (`Polyhedron.geom = lambda: Manifold.cube(...)`, `research/04` §5). The
  renderer **must not** invoke that lambda. It reads `name_` + `args` + `kwargs`
  off the leaf and emits an *exact* build123d primitive
  (`Box`/`Sphere`/`Cylinder`/`Cone`). `csg.Polygon` (`square`/`circle`/
  `polygon`) is handled the same way. (This resolves `research/04`
  open-question #7: the build123d backend bypasses the manifold-bound builders
  entirely rather than duplicating them.)
- **Transforms via `.matrix`.** Every `AbstractTransform` (`Translate`,
  `Rotate`, `Scale`, `Mirror`, `Multmatrix`) exposes a 4×4 numpy `.matrix`.
  *Rigid* matrices → a cheap build123d `Location` (`gp_Trsf`, no BREP rewrite).
  *Non-rigid* matrices (non-uniform scale, shear) → `Matrix` +
  `transform_geometry` (`gp_GTrsf`), which **rewrites** the BREP and degrades
  exact curved surfaces — emitted with a warning, not refused (`p4` fidelity
  findings; see Risk R5).
- **`@renderer`-style caching** keyed by `csg.Node.key` (`FastKey`), gated by
  `is_reused` — see §7.
- **Honest failure.** `csg` subtypes OCCT cannot faithfully render
  (`Minkowski`, 3D `Hull` of curved solids) **do not** fake a result in the
  pure-build123d path — they raise `UnsupportedOperation`. In the *hybrid*
  (§3.4) they are instead routed to the manifold backend rather than raised.

The `csg.Node → build123d` mapping is consolidated in §4.

### 3.3 The dimension-aware scene model (`B3DSceneModel`)

**The problem (`p4` real finding).** `CSG.scad` renders fine and `is_valid` is
True, but the **re-imported STEP reports a negative volume**. Cause: the
top-level `csg.Group` mixes 3D solids with a 2D `linear_extrude` of `text`;
`p4`'s `run.py` extruded the 2D member to a 1 mm slab and dropped everything
into one flat `Compound`. A **heterogeneous `Compound`** (mixed-dimension
members) confuses OCCT's STEP writer. This is the dimension-mixing STEP sign
bug (Risk R3).

**The design.** Replace the flat-`Compound` output with a `B3DSceneModel` — the
build123d-flavoured analogue of scad2py's `SceneTransformer`
(`rendering/rendering.py:112-293`), but mesh-free:

- It normalizes the rendered tree into a **scene**: a top-level grouping that
  keeps **2D and 3D members in separate, dimension-homogeneous `Compound`s**
  (a `Sketch`-Compound and a `Part`-Compound, never mixed). build123d's
  `Compound`/`Curve`/`Sketch`/`Part` are dimension-typed (`_dim` 1/2/3,
  `research/01` §2.1) — exploit that: the scene model produces a `Part` and a
  `Sketch`, not one ambiguous `Compound`.
- `csg.Node.dim` (`csg.py:359`, lazily inferred 2D/3D) drives the partition.
- It bubbles `Color` to `Shape.color` (build123d carries color on the shape
  natively — no vertex-property baking, unlike manifold; `research/04`
  open-question #6 resolved: the manifold-specific color-partitioning into
  single-colored CSG pieces is **unnecessary** for build123d + STEP/GLTF and is
  dropped).
- It flattens `Group`/`Root`/implicit-`Union` per the `Compound`-for-implicit
  rule (§4.3).
- Modifier nodes: `!` (root) prunes the tree to that subtree (do at scene-model
  time); `*` (disable) is already dropped upstream by the transpiler
  (`CommentedOutStatement`); `%` (background) → a separate non-participating
  `Compound` (excluded from booleans, optionally exported as a distinct group);
  `#` (debug) → `Shape.color`/`label`.

**Why a new model rather than reusing `SceneTransformer`:** `SceneTransformer`
exists *because* mesh formats cannot carry per-region color and manifold needs
2D/3D mismatch removal — both manifold-specific. `research/04` open-question #3
("how much of `SceneTransformer` survives?") is answered: **the dimension
partition survives and is essential; the color-partitioning does not and is
dropped.**

### 3.4 The hybrid manifold / build123d router

**This is the central architectural decision.** A pure-build123d backend is not
viable: `research/05` §5 and both `p4`/`p5` prove that `minkowski`, 3D `hull`
of curved solids, raw `polyhedron`/`surface`, degenerate booleans, and
large/dense CSG trees **have no faithful or affordable BREP form**. The backend
must be a **hybrid** that routes each `csg.Node` subtree to whichever kernel can
serve it (`p4` recommendation #1; `research/05` §7).

**Routing classification.** Before rendering, a `BackendClassifier` visitor
walks the deduped `csg` DAG and tags each subtree **BREP-capable** or
**mesh-required**:

| `csg.Node` subtree contains… | Route to | Why |
|---|---|---|
| `Minkowski` | **manifold** | No BREP algorithm exists; definitionally a mesh op (convex-decompose + per-pair hull + union). `p4`: `car.scad`, `minkowski.scad`. |
| `Hull` over **3D** children | **manifold** | Hull of curved solids has spherical caps + ruled patches OCCT will not synthesize. `p4`: 5 examples (`box`,`torus`,`a11y`,`scalemail`,`simplecustom`). |
| `Hull` over **2D** children | build123d BREP | `make_hull` (`operations_sketch.py:232`) is exact for 2D. `p4` confirms. |
| `Polyhedron` with non-planar / non-manifold / degenerate faces | **manifold** | OCCT `Solid(Shell(faces))` rejects what Manifold ingests happily (`research/05` §3.3). Clean planar `polyhedron` → build123d (`p4`: `polyhedron.scad` valid). |
| `Surface` heightmap, mesh `import` (STL/3MF/OFF) | **manifold** | Naturally a mesh; `import_stl` even returns a single `Face`, not a solid (`research/05` §4.5). |
| Large/dense CSG (node count above a threshold, or `for`-replicated faceted parts) | **manifold** | OCCT pairwise-boolean O(n²) cliff: `p4` times out at 90 s on `colors`(1369 cubes), `menger`, `condensed-matter`, `example`. manifold does these in ms (`research/01` §6.4; `research/09` §3.2). |
| Everything else — `cube`/`sphere`/`cylinder`/clean `polyhedron`, `union`/`difference`/`intersection`, transforms, `linear_extrude`, `rotate_extrude`, `offset`, `text`, clean `polygon` | **build123d BREP** | Exact analytic geometry, valid `Solid`s, STEP export — the whole point of Option A. |

**Boundary conversion.** When a boolean mixes a BREP operand and a mesh operand,
convert at the boundary:

- **BREP → mesh:** `Shape.tessellate(tolerance)` (`shape_core.py:2241`) — the
  deviation derives from the resolved `$fn/$fa/$fs` (§4.4). Already exists.
- **mesh → BREP:** sew triangles into a `Solid`. The slow path is
  `Mesher._get_shape` (`mesher.py:460`); the **fast path** is the
  direct-shell-assembly reconstructor designed in
  `design-manifold-in-build123d.md` (cross-ref `research/09` §5.2 — port
  CadQuery's `p2-fast-mesh2brep`). **Prefer keeping a mesh-backend result a
  mesh through to export** and only sew if a downstream BREP-only op
  (fillet/chamfer/exact STEP) demands it — `research/05` §7.4.

**`HybridRenderer` structure.** It is a thin dispatcher: for each subtree it
consults the `BackendClassifier` tag and delegates to either `Build123dRenderer`
or scad2py's existing `ManifoldRenderer` (reused **as-is** for mesh-required
subtrees — exactly the "two backends behind one interface" recommendation,
`research/05` §7.2). The manifold backend invoked here is **Goal 1's**
`build123d[manifold]` mesh path where one exists; scad2py's own
`ManifoldRenderer` + `minkowski_impl.py` is the immediate, already-working
implementation for Phase A1 (see §9).

**Failure-driven fallback (optional, Phase A4).** Beyond the static
classifier, an OCCT boolean that *fails or hangs* on degenerate input can
auto-fall-back to the manifold backend for that subtree. `research/05`
open-question #5 flags the tradeoff: most robust, but makes the result
non-deterministic w.r.t. backend. Recommendation: **static classification
first; failure-driven fallback as an opt-in later phase** with a warning when
it triggers.

---

## 4. The OpenSCAD → build123d mapping

Consolidating the comprehensive table in `research/05` §4 into firm design
decisions. Three buckets: **exact-BREP** (build123d does it faithfully),
**manifold-fallback** (route to Goal 1's backend), **honest failure** (no
faithful route — `NotImplementedError` in B, `UnsupportedOperation` or
manifold-route in A).

### 4.1 Exact-BREP — build123d does these faithfully

| OpenSCAD | build123d | Decision / notes |
|---|---|---|
| `cube` | `Box(l,w,h, align=…)` | `center` → `Align.CENTER`/`MIN`. Exact. `p4` verified. |
| `sphere` | `Sphere(r)` | Exact sphere. `$fn` caveat — §4.4. |
| `cylinder` | `Cylinder` (`r1==r2`) / `Cone` (`r1≠r2`) | Exact analytic surface — *better* than scad2py's own faceted renderer (`p4`). `$fn` caveat — §4.4. |
| `polyhedron` (clean, planar faces) | `Solid(Shell(Faces))` | `p4`: sewn into a valid `Solid`, clean STEP. Non-planar/degenerate → manifold (§4.2). |
| `square`/`circle`/`polygon` | `Rectangle`/`Circle`/`Polygon` | Exact. Multi-path holes via boolean subtraction. |
| `text` | `Text(font_size, align)` | `halign`/`valign` → `Align`. Glyph metrics differ from OpenSCAD (FreeType vs build123d `text.py`) — accepted minor fidelity loss. |
| `union`/`difference`/`intersection` | `+`/`-`/`&` (`fuse`/`cut`/`intersect`) | Direct. Cliff risk on large trees → §4.3, manifold route. |
| `translate`/`rotate`/`mirror` | `Location`/`Rotation`/`mirror` | Rigid → cheap `Location`. Exact. |
| `scale` (uniform) | `scale(by=k)` | Exact. |
| `multmatrix` (rigid part) | `Location` from 4×4 | Exact for the rigid component. |
| `linear_extrude` (plain, `scale=`, `center`) | `extrude` / `loft` | Plain → exact `extrude`; `scale=` → exact ruled `loft` base→scaled-top. `center` handled. (Fixes scad2py's own `center==True` TODO, `manifold_renderer.py:417`.) |
| `rotate_extrude` | `revolve(profile, Axis.Z, revolution_arc=)` | Exact surface of revolution — fixes scad2py's shell-out. Blocked today only by a scad2py front-end bug (Risk R4 / §3 bug 1). |
| `offset(r=)` / `offset(delta=,chamfer=)` | `offset(kind=Kind.ARC/INTERSECTION/TANGENT)` | `ARC` matches OpenSCAD `r`. Exact. |
| `color` | `Shape.color` (RGBA) | Carried on the shape; survives STEP/GLTF (manifold3d cannot). |
| `projection(cut=true)` | `section()` | Planar cross-section. |
| `fill` | `make_face` of the outer wire | Drop inner wires. |
| `resize` | non-uniform `scale` to a bounding box | `bounding_box` to measure; autoscale-0-axis needs explicit logic. |
| 2D `hull` | `make_hull` | Exact. |

### 4.2 Manifold-fallback — route to Goal 1's mesh backend

| OpenSCAD | Why no BREP route | Routing |
|---|---|---|
| `minkowski` (2D & 3D) | No BREP algorithm. Definitionally a mesh op. | manifold backend (`minkowski_sum` for convex; `minkowski_impl.py` convex-decomposition for non-convex — note non-convex is *approximate* even in scad2py, `research/05` §4.6). |
| 3D `hull` of curved solids | Spherical caps + ruled patches OCCT will not synthesize. | manifold `hull`/`batch_hull` over tessellated vertices. |
| `polyhedron` non-planar / non-manifold / degenerate | OCCT `Shell` sewing is fragile; Manifold ingests triangle soup directly. | manifold `Manifold(Mesh(...))`. |
| `surface` heightmap | Structurally a mesh. | manifold mesh. |
| mesh `import` (STL/3MF/OFF) | OCCT does not want large meshes. | keep as mesh end-to-end. |
| Large/dense / `$fn`-heavy CSG trees | OCCT O(n²) boolean cliff (`p4` 4 timeouts). | route the whole subtree to manifold's fast multithreaded booleans. |
| degenerate / coincident-face booleans | OCCT BOPAlgo fails/hangs; Manifold robust by contract. | manifold (static route, or failure-driven fallback §3.4). |

### 4.3 The implicit-union → `Compound` rule

**The single most important codegen/rendering rule** (`p5` §3 — *"the fix that
is also more faithful"*). OpenSCAD's top level, `for` bodies, and module bodies
are an **implicit CSG group node**, *not* a boolean union — disjoint parts are
fused into one mesh only at F6 render time. Naively mapping "everything
implicitly unions" onto build123d `+` triggers the OCCT O(n²) boolean cliff:
`p5`'s first cut **hung** on `colors.scad` (1369 boxes) doing 1368 sequential
`BRepAlgoAPI_Fuse` + `ShapeUpgrade_UnifySameDomain` calls.

**Decision (both products):**

- **Implicit aggregation** (`csg.Group`, `csg.Root`, `for`-body collection,
  module-body collection, the top-level scene) → build123d
  **`Compound(children=[...])`** — O(n), **no kernel boolean**.
- **Explicit `union()`** (`csg.Union`) → real `+` / `fuse`.

After this fix `p5`'s `colors.py` (1369 parts) runs in **4.5 s** instead of
hanging. Option A's `B3DSceneModel` (§3.3) and Option B's emitter both apply
this rule. *Even then*, an explicit `union()` of hundreds of overlapping
faceted parts will hit the cliff — that subtree routes to manifold (§4.2, last
two rows).

### 4.4 The `$fn` / resolution strategy

OpenSCAD has no exact curves: every circle/sphere/cylinder is faceted at a
count resolved from `$fn/$fa/$fs` (`research/05` §1.4). `circle($fn=6)` **is a
hexagon** — geometric truth, not a render hint. build123d's `Circle` is an
exact circle. There are two conflicting fidelity targets (`research/05` §3.1):
**intent fidelity** (designer wanted a circle; `$fn` was tuning → emit exact)
vs **output fidelity** (reproduce OpenSCAD's STL vertex-for-vertex → emit a
faceted polygon).

**Decision — default to the hybrid heuristic, `--resolution` flag-overridable**
(`research/05` §6 policy B; both prototypes converged on this):

- **`--resolution=exact` (recommended default).** Emit exact
  `Circle`/`Sphere`/`Cylinder`/`revolve`. `$fn/$fa/$fs` are recorded as
  **export-time tessellation hints** only (mapped to OCCT deflection), not
  baked into the model.
- **`--resolution=hybrid`.** A small explicit `$fn` (heuristic threshold —
  `p4` uses `_FACET_INTENT_THRESHOLD = 24`; `p5` uses 3–12 for `circle`) is
  treated as *intentional faceting* → emit `RegularPolygon` / an N-gon prism.
  Large/unset `$fn` → exact primitive. **`sphere` has no faceted build123d
  primitive** — a faceted `sphere($fn=6)` is emitted as an exact sphere **with
  a warning** (`p4`): an honest, documented fidelity loss.
- **`--resolution=faithful`.** Facet every curved primitive via the resolved
  fragment count. This throws away build123d's advantages and is **best paired
  with the manifold backend** — feeding faceted geometry into OCCT is the worst
  of both worlds (`research/05` §6 policy C). In practice, `faithful` mode
  should *route curved primitives to manifold*.

**Single source of truth for fragment counts:** the **clean-room
`fragment_count`** from `p6` (`prototypes/p6_clean_room_scad2py_math/
fragments.py`) — *not* the GPL `calc.get_fragments_from_r` (§5). It implements
the same documented `$fn>0 ? max($fn,3) : ceil(max(min(360/$fa,2πr/$fs),5))`
formula and is verified bit-exact over 2970 cases.

**Dynamic scoping.** `$fn/$fa/$fs` are dynamically scoped — a parent's value
applies to descendants. scad2py's runtime already threads them as `_fn/_fa/_fs`
kwargs + a dynamic-scope shim (`runtime/variables.py`, `_resolve_specials`
`modules.py:60`). **Option A inherits this for free** — the values are already
resolved onto the `csg` leaves before the renderer sees them. **Option B must
model it explicitly** (`p5` §4 flags this as an unsolved gap in the prototype —
the production version threads resolved facet counts down the AST, as scad2py's
runtime does).

### 4.5 Always carry `$fn/$fa/$fs` as metadata

Whichever resolution mode, **attach the resolved `$fn/$fa/$fs` to the generated
build123d objects** (build123d `Shape`s hold `label`/attributes, `research/01`
§2.2) so a later export can reproduce OpenSCAD's mesh density on demand even
when the in-memory model is exact BREP (`research/05` §6).

---

## 5. Phase 0 — GPL remediation (the prerequisite)

**This gates everything.** `scad2py/calc.py` and `scad2py/colors.py` carry
verbatim OpenSCAD GPL-2.0+ headers (copyright Clifford Wolf / Marius Kintel)
and are, by their own in-file comments, **line-by-line ports of OpenSCAD C++**
(`calc.cc`, `TransformNode.cc`, `degree_trig.cc`, `ColorNode.cc`,
`ColorMap.cc`). A C++→Python port is a derivative work, so both files are
**GPL-2.0-or-later**, copyright is *not* ochafik's, and **he cannot relicense
them** (`research/06` §6.4). They are on the geometry hot path — imported by
`csg.py`, `io.py`, `runtime/modules.py`, and all three `rendering/*` modules.
**Therefore scad2py as it stands today is a GPL combined work** and cannot be
merged into / co-distributed with Apache-2.0 build123d (the Apache↔GPL
incompatibility, `research/06` §5).

**The fix is done in prototype form.** `design/scad2py-gpl-remediation.md` is
the full audit + plan; `prototypes/p6_clean_room_scad2py_math/` is the
**clean-room rewrite** — both files re-implemented from public specs (OpenSCAD
User Manual formula, W3C CSS Color 4 named-color table, textbook matrix math)
by an agent that **never opened the GPL files**, only ran them. Verification:
**3308 comparison cases bit-equivalent** to the originals (FP noise ≤ 1.4e-17).
A repo-wide scan of all 52 `.py` files found **no other** contamination — the
parser only *references* OpenSCAD's `parser.y` as a URL comment and is an
independent PLY reimplementation (a clean reimplementation of a language/
interface, *Google v. Oracle*).

**Phase 0 actions** (the gating subset of `design/scad2py-gpl-remediation.md`
§6 — full checklist there; do not reproduce it here):

1. Swap `scad2py/calc.py` → `p6/fragments.py` + `p6/transforms.py`; swap
   `scad2py/colors.py` → `p6/colors.py`. (Reuse the `p6` modules directly —
   do **not** re-do the clean-room rewrite, which would risk re-contamination
   by a second agent; `research/09` §7.2.)
2. Repoint the call sites (the rename map is in the remediation doc §6 — e.g.
   `calc.get_fragments_from_r` → `fragment_count`,
   `calc.matrix3d_to_2d` → `matrix_4x4_to_2x3`). The `csg.RotateExtrude`
   constructor and `csg.Import.dim` bugs (§3 bugs) are *separate* — fix
   alongside, see Risk R4.
3. `git rm` the two GPL files so the headers leave the working tree.
4. Add an Apache-2.0 `LICENSE` to scad2py (ochafik is sole owner of the
   *remaining* code; this is his to do unilaterally — `research/06` §8.1).
5. Confirm `grep -ri "GPL\|GNU General Public\|ported from"` over `scad2py/`
   returns nothing.

**Do not start Option A or B coding against an un-remediated scad2py.** The
prototypes `p4`/`p5` were allowed to (research/derisking, no distribution);
the *product* must not be.

---

## 6. Licensing & merge mechanics

### 6.1 The license picture (post-Phase-0)

Once Phase 0 lands, the whole stack is a **pure permissive combination**:
build123d (Apache-2.0), manifold3d (Apache-2.0), scad2py's own code
(Apache-2.0, ochafik sole owner), plus `ply` (BSD), `trimesh` (MIT), `shapely`/
`numpy`/`scipy`/`rtree` (BSD/MIT). OCCT (LGPL-2.1 + Open CASCADE Exception
1.0) arrives only as the separate dynamically-linked `cadquery-ocp-novtk`
wheel — a "work that uses the Library", compliant with notice (`research/06`
§3, §10). **No copyleft inbound.**

### 6.2 Required `LICENSE` / `NOTICE` actions

- **scad2py:** add Apache-2.0 `LICENSE` + `NOTICE`; re-activate `license=` in
  packaging metadata; SPDX headers (the `p6` files already have them).
- **build123d `NOTICE`:** add attributions for everything scad2py and the
  manifold backend pull in — `ply` (BSD), `trimesh` (MIT), `shapely` (BSD),
  `rtree` (MIT), `manifold3d` (Apache-2.0) + its bundled Clipper2 (Boost-1.0)
  and quickhull, and the OCCT "prominent notice" the exception requires
  (`research/06` §9.3).
- **Examples:** the BOSL2-derived `examples/bosl2_*.scad` are BSD-2-Clause —
  add attribution **or drop them**; audit `a11y.scad` and other third-party
  `.scad` files (`research/06` §8).
- **Dependency hygiene:** scad2py currently pins a personal trimesh fork
  (`git+https://…/ochafik/trimesh@ochafik-svg-io-color`). A shipped product
  **must not** depend on an unreleased personal branch — upstream the patch,
  publish the fork, or vendor it (`research/06` §6.3). Keep `coacd` optional
  (`extras_require`).

### 6.3 Merge vs sibling package — **recommend a sibling package**

`design/scad2py-gpl-remediation.md` §6 leaves this as a decision; both options
are legal once Phase 0 is done. **Recommendation: ship scad2py as a separate
Apache-2.0 package, `scad2py`, that depends on `build123d`** — *not* code
merged into the build123d source tree.

Reasoning:

1. **Dependency direction.** scad2py depends on build123d, not vice versa.
   build123d should **not** acquire a hard dependency on a PLY parser, a
   transpiler, `gradio` (scad2py's customizer UI), or an OpenSCAD grammar.
   Goal 1's `build123d[manifold]` is the *only* thing scad2py needs from
   build123d's side, and that is an opt-in extra.
2. **Release cadence.** scad2py (an experimental transpiler with stubbed areas
   — `research/04` §9) and build123d (a mature CAD library) have very different
   stability and release rhythms. Coupling them in one repo forces the slower
   cadence on the faster project.
3. **Upstream signal.** build123d issue #1228 asks for `build123d[manifold]`
   *as an optional extra* — the project's appetite is for opt-in extras, not
   for absorbing a transpiler into core.
4. **The sibling model is still a clean Apache-2.0 story** — `research/06`
   §9.2 and the remediation doc §6 both confirm "separate package that depends
   on build123d" is fully legal post-Phase-0, and it is also the right shape
   for the future WASM build.

**Concretely:** scad2py keeps its own repo and PyPI package. It declares
`build123d` (and `build123d[manifold]` for the hybrid) as dependencies. The
`Build123dRenderer` / `B3DSceneModel` / `HybridRenderer` (Option A) and
`scad2b3d` (Option B) live **in the scad2py package**, importing build123d's
public API. Nothing is contributed into build123d's tree except, separately,
Goal 1's manifold backend.

---

## 7. Caching

### 7.1 What scad2py already has

scad2py ships a working, content-addressed in-memory cache (`research/08` §3):

- **`FastKey`** (`utils.py:56-81`) — an interned hash of the OpenSCAD-syntax
  text dump of a `csg.Node` subtree. `FastKey.__eq__` is identity (sound
  because interning guarantees one object per distinct string). Same idea as
  OpenSCAD's `Tree::getIdString` — independently converged (`research/08` §2.2).
- **`CachingVisitor`** (`rendering/caching.py:26-48`) — walks the `csg` tree,
  collapses structurally identical subtrees into shared node objects (**tree →
  DAG**), tracks `refcount`; `is_reused(n)` reports `refcount > 1`.
- **`@renderer` decorator** (`manifold_renderer.py:70-96`) — caches rendered
  geometry by `FastKey`, **gated by `is_reused`** (only bothers to cache
  subtrees that appear ≥ 2×). Stores and returns `clone()`s.

### 7.2 How it adapts when the artifact is a build123d `Shape`

**Reuse `FastKey` + `CachingVisitor` wholesale** — the keying scheme is correct
and backend-agnostic (`research/08` §5 recommendation 1; `research/09` §6.1).
`CachingVisitor` runs **before** the renderer (it is part of the `render_geom`
pipeline, §3.1) and dedups the `csg` DAG identically regardless of backend.

The **`@renderer` caching is the part that needs care.** `research/08`
open-question / `research/04` open-question #4 / `p4` recommendation #4 all
flag the same thing: `@renderer`'s `clone()` is cheap *because manifold meshes
are a flat buffer*. **OCCT `Shape`s are not cheaply cloneable** — `deepcopy`
goes through `BRepBuilderAPI_Copy`, `Shape.__hash__` is identity-based and
useless as a content key (`research/01` §2.2; `research/08` §1.5).

**Decision — split the cache by what the hybrid produced:**

- **Mesh-side subtrees** (routed to manifold, §3.4): cache the **manifold mesh**
  (`MeshGL` arrays / verts+tris), keyed by `csg.Node.key`, gated by
  `is_reused`. `clone()` stays cheap. This is scad2py's existing mechanism
  essentially unchanged — `research/08` recommendation 2 ("make the cached
  artifact a mesh, not an OCCT `Shape`").
- **BREP-side subtrees** (build123d `Shape`s): a `Shape` *can* be cached, but
  `clone()` must be `share_topo()`-aware or a `BRepBuilderAPI_Copy`. Because
  the build123d backend's value is the exact/parametric *clean* subset (which
  is rarely the highly-repetitive part — repetition is what `for` loops produce
  and those are usually the bulk-CSG that routes to mesh), **cache BREP shapes
  only when `is_reused` flags them, and accept the more expensive clone.** If
  profiling shows BREP-side caching does not pay, **drop it for the BREP side**
  and keep it mesh-only — `p4` recommendation #4 explicitly allows "rethink or
  drop" for the BREP backend.

**Mutation hazard.** `CachingVisitor` produces a DAG with *shared* nodes;
`@renderer` defends with `clone()` on store and retrieve. The `Build123dRenderer`
must therefore be **fully copy-on-use** — never mutate a `Shape` in place
(`shape.wrapped.Location(...)`) — or a shared cached subtree is corrupted
(`research/08` open-question #4). build123d's algebra ops (`+`/`-`/`&`) are pure
and return new shapes, so this is naturally satisfied if the renderer avoids
in-place `.locate()`/`.move()` on cached shapes.

### 7.3 Out of scope for this design

An **on-disk persistent cache** (keyed by `node.key`, blobs under
`~/.cache/build123d/`) is high-value for scad2py re-runs (`research/08` §4.3)
but is a **separate deliverable** — it belongs to the manifold-backend /
build123d caching design, not to the scad2py integration. Note it as a
follow-up; do not build it in Goal 2.

---

## 8. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| **R1** | **`minkowski` / 3D `hull` have no BREP form.** A pure-build123d backend cannot render `car.scad`, `minkowski.scad`, `box.scad`, `torus.scad`, `a11y.scad`, `scalemail.scad`, `simplecustom.scad` — 7/33 `p4` examples. | **High** | The hybrid router (§3.4): these subtrees route to the manifold backend (Goal 1). Goal 2's hybrid *depends on* Goal 1. Until Goal 1's mesh backend exists, scad2py's own `ManifoldRenderer` + `minkowski_impl.py` serve as the immediate fallback (Phase A1, §9). |
| **R2** | **OCCT O(n²) boolean cliff.** `p4` times out at 90 s on `colors`(1369 cubes), `menger`, `condensed-matter`, `example`. `p5`'s naive `+`-fold hung on the same. Every OCCT boolean also runs `ShapeUpgrade_UnifySameDomain` (`research/01` §6.4). | **High** | (a) The implicit-union → `Compound` rule (§4.3) — O(n), no kernel boolean — fixed `p5`'s hang. (b) Large/dense CSG subtrees route to manifold (§4.2). (c) Node-count threshold in the `BackendClassifier`. |
| **R3** | **Mixed-dimension `Compound` STEP sign bug.** `p4`: `CSG.scad` renders valid but the re-imported STEP has *negative volume* — a flat `Compound` mixing 2D and 3D members confuses OCCT's STEP writer. | **Medium** | The dimension-aware `B3DSceneModel` (§3.3): partition the scene into separate dimension-homogeneous `Compound`s (a `Part` and a `Sketch`), never one mixed `Compound`. Exploit build123d's dimension-typed `Compound`/`Sketch`/`Part`. |
| **R4** | **scad2py front-end bugs block the renderer.** `p4` found 4: `csg.RotateExtrude.__init__` rejects `angle=/fn=/fa=/fs=` (`runtime/modules.py:255` vs the dataclass) → `rotate_extrude` *cannot reach any renderer*; `csg.Import.dim` checks `ext in ('stl',…)` but `splitext` yields `'.stl'` → `import.scad` breaks; `bosl2_math`/`bosl2_utility`/`runtime` fail in the transpiler/runtime (`name 'valid_range' is not defined`, `'function' object is not subscriptable`). | **Medium** | Fix these in scad2py during Phase A1 — they are small, upstream of the renderer, and `revolve` mapping is *already implemented* in `p4` but unexercisable until bug 1 is fixed. Track as scad2py issues; they are scad2py's own defects, not build123d's. |
| **R5** | **Non-uniform scale / shear loses exact BREP.** OpenSCAD `scale([2,1,1])` and shear `multmatrix` are routine; OCCT cannot non-uniformly scale a `Solid` while keeping it a valid BREP. `p4` routes these through `transform_geometry` (`gp_GTrsf`) — works but rewrites the BREP and degrades curved surfaces. | **Medium** | `transform_geometry` + a **warning** (not refusal) — `p4`'s approach. Non-uniform scale of an *exact circle* yields an exact ellipse (faithful, `research/05` §4.4); of a curved solid it degrades. Document it; surface the warning. A finished typer (§10 Q6) could statically prove a scale uniform and pick the cheap path. |
| **R6** | **`polyhedron` robustness.** OCCT `Solid(Shell(faces))` rejects non-planar / non-manifold / T-junction / reversed-winding faces that Manifold ingests happily — *common* in generated `.scad`. `p4` worked only on clean planar input. | **Medium** | Clean planar `polyhedron` → build123d; everything else → manifold (§4.2). Detect non-planar faces in the `BackendClassifier`. Optionally fall back on OCCT sewing failure (§3.4 failure-driven fallback). |
| **R7** | **`$fn` faceting mismatch.** `circle($fn=6)` *is* a hexagon in OpenSCAD; build123d's `Circle` is exact. Wrong choice silently changes the model. `sphere` has no faceted build123d primitive. | **Medium** | The `--resolution` flag with the hybrid heuristic default (§4.4); a faceted `sphere` emits a **warning**. Use `p6/fragment_count` for fragment counts. Always carry `$fn` as export metadata (§4.5). |
| **R8** | **Caching layer is mesh-shaped.** `@renderer`/`clone()` assume cheap-to-copy meshes; OCCT `Shape`s are not. | **Low–Medium** | §7.2: mesh-side caching unchanged; BREP-side caching gated by `is_reused` with an honest "drop it if it doesn't pay" escape. Renderer must be copy-on-use to protect shared DAG nodes. |
| **R9** | **Operator modules / `children()` in Option B.** OpenSCAD operator modules re-instantiate `children()` lazily; `children(i)` indexing and `$children` count have no clean Python-`def` equivalent. Object modules transpile perfectly; operator modules are best-effort (`p5` §5). | **Low–Medium** (B-only) | Acceptable for a migration tool (B's robustness bar permits `# TODO`). Lower a module to a `def` taking a `_children` list; emit `NotImplementedError` for the cases that genuinely don't map. Option A is unaffected — the runtime already expanded modules before the `csg` tree. |
| **R10** | **GPL contamination ships by accident.** Building/releasing against an un-remediated scad2py would distribute GPL code in an Apache product. | **High (legal)** | Phase 0 gates all other phases (§5, §9). CI check: `grep -ri "GPL\|ported from"` must be clean before any build123d-backed scad2py artifact is published. |
| **R11** | **Goal 1 not ready when Goal 2 needs it.** The hybrid (§3.4) depends on `build123d[manifold]`. | **Medium** | Phase A1 uses scad2py's *existing* `ManifoldRenderer` as the mesh side (it already works) — Goal 2 is not blocked on Goal 1's *completion*, only its eventual availability for the cleaner `build123d[manifold]` integration in a later phase. |
| **R12** | **Trimesh personal-fork dependency.** scad2py pins `git+https://…/ochafik/trimesh`. | **Low** | Upstream / publish / vendor before any release (§6.2). Engineering hygiene, not a blocker for development. |

---

## 9. Phased implementation plan

Each phase is independently shippable and ordered by value-over-risk.
**Phase 0 gates all of them.**

### Phase 0 — GPL remediation *(prerequisite, gates everything)*

Swap `calc.py`/`colors.py` for the `p6` clean-room modules, repoint call sites,
add the Apache-2.0 `LICENSE`, verify the grep is clean. Per
`design/scad2py-gpl-remediation.md` §6. **Deliverable:** an Apache-2.0-clean
scad2py. **Until this lands, no other phase may produce a distributable
artifact.**

### Phase A1 — Option A core: `Build123dRenderer` + backend flag

Productionize `p4`'s `build123d_renderer.py` into a `Build123dRenderer` inside
the scad2py package; add the `--enable=build123d` backend selector at the
`render_geom` seam (§3.1); add a `Build123dRendering` wrapper with STEP/STL/
GLTF/BREP export. **Mesh-required subtrees route to scad2py's *existing*
`ManifoldRenderer`** — the hybrid is wired but its mesh side is scad2py's own,
not yet Goal 1's. Fix the four scad2py front-end bugs (R4) so `rotate_extrude`/
`import` can reach the renderer. **Deliverable:** `.scad` → exact BREP → STEP
for the clean subset; `p4`'s 15/33 becomes the regression baseline.

### Phase A2 — the dimension-aware scene model

Implement `B3DSceneModel` (§3.3): dimension-homogeneous `Compound`s, color → 
`Shape.color`, modifier handling, the implicit-union → `Compound` rule (§4.3).
**Deliverable:** the `CSG.scad` STEP sign bug (R3) is fixed; mixed-2D/3D files
export valid STEP.

### Phase A3 — the `BackendClassifier` + hybrid router hardening

Implement the static `BackendClassifier` (§3.4): tag subtrees BREP-capable vs
mesh-required (`minkowski`, 3D `hull`, non-planar `polyhedron`, `surface`, mesh
`import`, large/dense CSG by node-count threshold). Implement
tessellate↔mesh boundary conversion. **Deliverable:** `minkowski`/3D-`hull`
examples render again (via the mesh side); the perf-cliff timeouts (R2) no
longer hang — large subtrees route to manifold.

### Phase A4 — `build123d[manifold]` integration + failure-driven fallback

Once Goal 1's `build123d[manifold]` mesh backend exists, route the
mesh-required subtrees through *it* (instead of scad2py's `ManifoldRenderer`)
and use Goal 1's fast mesh→BREP reconstructor for boundary conversion. Add the
opt-in failure-driven fallback (an OCCT boolean failure auto-routes the subtree
to mesh, with a warning). **Deliverable:** a single coherent
`build123d[manifold]`-based hybrid; `$fn=faithful` mode routes curved
primitives to mesh.

### Phase A5 — caching

Wire `CachingVisitor` into the build123d pipeline; mesh-side `@renderer`
caching unchanged; BREP-side caching gated by `is_reused`, profiled, kept or
dropped per §7.2. **Deliverable:** repetitive models render once + cheap
reuse on the mesh side.

### Phase B1 — Option B: `scad2b3d` source transpiler

Productionize `p5`'s `scad2b3d.py`: port scad2py's `Inliner` (for `include`/
`use`) and `pick_unique_names` (alpha-renaming — `p5` §8 flags these as the
prototype's gaps); thread `$fn` resolution through the AST (§4.4); package the
runtime shim as an importable `scad2py_b3d_runtime` module instead of inlining
~120 lines into every file. **Emit the same build123d vocabulary as Option A's
mapping** (§2.3). **Deliverable:** `.scad` → readable, runnable build123d
`.py`; `p5`'s 8/8 is the regression baseline.

### Phase B2 — Option B polish

Pretty-printing pass (kill redundant parentheses, the `_parts.append`
accumulator); operator-module `children()` best-effort handling (R9); honest
`NotImplementedError` for `hull`/`minkowski`/`twist`/`surface`. **Deliverable:**
migration-quality output.

**Dependency graph:** `0 → A1 → A2 → A3 → A4`; `A5` after `A1`; `B1 → B2`
after `0` (B reuses only the front end, so B can start as soon as Phase 0 is
done — but per §2.3 the recommended order builds A's mapping first as B's
reference).

---

## 10. Open questions / decisions needing the owner

1. **Merge vs sibling — confirm.** §6.3 recommends scad2py as a **sibling
   Apache-2.0 package depending on build123d**, not code merged into build123d's
   tree. The owner should confirm (it sets the repo/release/packaging shape).

2. **Default resolution mode.** §4.4 recommends **`--resolution=exact`** as the
   default (cleanest BREP, exact STEP). Is the more common user intent "I want
   exact CAD geometry" (→ `exact`) or "I want what OpenSCAD showed me"
   (→ `hybrid`)? This single choice also decides whether the mesh backend is the
   *default* or the *fallback* (`research/05` §8 Q1).

3. **`$fn` intent threshold.** The hybrid heuristic needs a cutoff between
   "render tuning" (→ exact) and "intentional facets" (→ polygon). `p4` uses 24,
   `p5` uses 3–12 for `circle`. Pick one, or find a better signal than a magic
   number (`research/05` §8 Q2).

4. **Large-CSG node-count threshold.** §3.4 routes "large/dense" CSG to
   manifold. What node count is "large"? Needs a benchmark on the
   `colors`/`menger`/`condensed-matter` regime to set the `BackendClassifier`
   threshold (`research/01` open-question; `research/05` §8 Q9).

5. **Failure-driven fallback — on or off by default?** §3.4 / R6: an OCCT
   boolean failure can auto-route the subtree to mesh. Most robust, but makes
   output non-deterministic w.r.t. backend (`research/05` §8 Q5).
   Recommendation: opt-in, with a warning when it triggers — confirm.

6. **Finish the typer?** scad2py's `transpiler/typer.py` is a 6-line stub; the
   `Type` lattice in `scadast.py` is dead. A real typer would let Option B emit
   cleaner Python (fewer `mult`/`div`/`wrap_list` wrappers) and could statically
   prove a `scale` uniform (cheap-path R5). Prerequisite, or orthogonal
   follow-up? (`research/04` open-question #5.) Recommendation: **orthogonal** —
   do not block Goal 2 on it.

7. **`%` background geometry in build123d output.** Emit it as a separate
   non-participating `Compound` (so STEP/GLTF can carry it as a distinct group),
   or drop it entirely? (`research/05` §8 Q6.)

8. **scad2py front-end bug ownership.** R4's four bugs are scad2py's own
   defects. Fix them in scad2py as part of Phase A1 (this design assumes so), or
   track them as separate upstream scad2py issues first? They block
   `rotate_extrude`/`import` from reaching *any* renderer, so Phase A1 needs
   them fixed regardless.

9. **On-disk persistent cache scope.** §7.3 defers it to the manifold-backend /
   build123d caching design. Confirm it is out of Goal 2's scope (recommended —
   it is high-value but separable).

10. **Counsel sign-off.** Two well-settled but interpretation-dependent points
    need a lawyer's written confirmation before public distribution: (a) a
    Python translation of GPL C++ is a derivative work; (b) the `p6` clean-room
    rewrite of formula-only routines is sufficient. Both are mainstream
    positions (`design/scad2py-gpl-remediation.md` §6, §8).

---

## Appendix — key source references

**scad2py** (`/Users/ochafik/github/scad2py/scad2py/`) — seams Goal 2 touches:
- `rendering/rendering.py:26-73` — `render_geom`, the **single renderer seam**
  Option A plugs into.
- `rendering/manifold_renderer.py:98` — `ManifoldRenderer`, the sibling
  `Build123dRenderer` mirrors; `:70-96` — the `@renderer` caching decorator;
  `:348-373` — the hard-coded-path OpenSCAD-binary shell-out Option A removes;
  `:417` — the `linear_extrude(center==True)` TODO Option A fixes.
- `rendering/rendering.py:112-293` — `SceneTransformer` (manifold-specific;
  `B3DSceneModel` replaces it for the build123d backend).
- `rendering/caching.py:26-48` — `CachingVisitor` (reused wholesale, §7).
- `utils.py:56-81` — `FastKey` (reused wholesale, §7).
- `csg.py:152-321` — `csg.Node` base; `:359` — lazy `dim`; the concrete CSG
  node classes Option A's visitor dispatches on.
- `contexts.py:28-45` — `Rendering.output` (`Build123dRendering` siblings it).
- `args.py` — `get_feature` (the `--enable=build123d` backend flag).
- *GPL — do not open:* `calc.py`, `colors.py` (Phase 0 swaps them out).

**build123d** (`/Users/ochafik/github/build123d/src/build123d/`):
- `objects_part.py` — `Box:125`, `Cone:169`, `ConvexPolyhedron:222`,
  `Cylinder:377`, `Sphere:472`.
- `objects_sketch.py` — `Circle:115`, `Polygon:187`, `Rectangle:226`,
  `RegularPolygon:302`, `Text:554`.
- `operations_part.py` — `extrude:112`, `revolve:514`, `section:569`.
- `operations_generic.py` — `offset:551`, `mirror:498`, `scale:844`.
- `operations_sketch.py` — `make_hull:232`, `make_face:197`.
- `shape_core.py` — `_bool_op:2459` (the boolean engine, O(n²) cliff source);
  `tessellate:2241` (BREP→mesh boundary); `composite_factories:196` /
  `make_composite:944` (the clean extension seam).
- `mesher.py:460` — `_get_shape` (slow mesh→BREP sew; Goal 1 supplies the fast
  reconstructor).
- `exporters3d.py` — `export_step`/`export_stl`/`export_gltf`/`export_brep`.

**ddocs:**
- `design/scad2py-gpl-remediation.md` — Phase 0 (the GPL prerequisite).
- `design/design-manifold-in-build123d.md` — Goal 1; the hybrid's mesh backend.
- `prototypes/p4_scad2py_to_b3d/` — Option A derisking (15/33; STEP roundtrip).
- `prototypes/p5_openscad_to_b3d_source/` — Option B derisking (8/8; the
  `Compound`-for-implicit-union fix).
- `prototypes/p6_clean_room_scad2py_math/` — the GPL-clean `fragment_count`,
  `colors`, `transforms` (3308-case verified).
- `research/04` — scad2py architecture; `research/05` — the OpenSCAD↔build123d
  semantic gap + mapping table; `research/06` — license analysis;
  `research/08` — caching; `research/09` — the CadQuery cross-comparison.

---

*End of design. Goal 2 is two products sharing one front end, gated on a GPL
fix, built on a hybrid build123d/manifold router. Option A first, Option B
second; both already prototyped end-to-end.*
