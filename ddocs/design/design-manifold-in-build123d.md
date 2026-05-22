# Design: manifold3d mesh operations in build123d

> **Goal 1 integration design.** This is the definitive, opinionated design for
> bringing `manifold3d`'s fast, robust mesh-CSG kernel into build123d. It
> synthesizes the research corpus (`ddocs/research/01–11`) and the prototypes
> (`ddocs/prototypes/p1,p2,p3,p7,p8`) into a concrete, phased, actionable plan.
>
> **Status:** design, ready for owner review. **Author:** architecture agent,
> 2026-05-22. **Implements:** build123d issue
> [#1228](https://github.com/gumyr/build123d/issues/1228) ("optional
> `build123d[manifold]` extra").
>
> All `file.py:line` references are against
> `/Users/ochafik/github/build123d/src/build123d/`. All performance numbers are
> *measured* by the cited prototype, never estimated.

---

## 0. Executive summary

build123d's booleans route through OpenCASCADE's BOPAlgo. That kernel is **slow
on many-operand CSG and scales super-linearly** — a 1000-hole iterated drill
*does not finish in 120 s* (`p2` Workload 4), while the same job in `manifold3d`
takes **0.5 s**. manifold3d booleans are **20–134× faster** and scale ~linearly
(`p2`). But OCC's exact BREP — analytic surfaces, fillet/chamfer, lofts,
STEP — is irreplaceable and must be kept.

The design is therefore a **hybrid, not a replacement**, with one headline
architectural decision:

> **manifold3d enters build123d as a standalone mesh value type, `MeshPart`,
> exposed through free functions — NOT as a `Shape` subclass and NOT as an
> implicit `.wrapped` swap.** mesh↔BREP conversion is always an explicit,
> user-visible verb. The feature ships as an opt-in `build123d[manifold]` extra
> with zero impact when not installed.

Three prototype findings make this both safe and valuable:

1. **Solid→mesh is easy.** `Shape.tessellate()` + a vertex-weld pass is
   sub-100 ms and production-ready (`p1` §1).
2. **The mesh→BREP blocker is cleared.** Per-triangle sewing is quadratic and
   unusable, but **direct `TopoDS_Shell` assembly (`p7`) is ~18× faster, dead
   linear at ~19 000 tri/s, turns 1M triangles into a valid `Solid` in ~53 s
   with 0.0000 % volume error.**
3. **Face identity is partially recoverable.** Provenance survives every
   boolean via manifold `run_original_id`; planar faces rebuild into genuine
   build123d `Face` objects (`p8`); curved/analytic selectors and
   fillet-after-boolean are permanently lost and must fail loudly.

**Phasing:** Phase 0 ships the free-function API (`mesh_fuse/cut/intersect`)
and the fast bake — the minimal valuable thing, zero core changes. Later phases
add the `MeshPart` value type, face-identity selectors, caching, and the WASM
story.

---

## 1. Goals & non-goals

### 1.1 Goals

- **G1 — Fast, robust bulk CSG.** Give build123d users a path to do
  many-operand unions/cuts/intersections and deep CSG trees 1–2 orders of
  magnitude faster than native OCC, and *robustly* on coincident geometry.
- **G2 — A mesh value type.** A first-class `MeshPart` that wraps a
  `manifold3d.Manifold`, so users can *stay in mesh space* across a long CSG
  chain and pay one BREP conversion at the end (if any).
- **G3 — Explicit, fast, lossless-on-vertices BREP bake.** A
  `Solid.from_mesh()` / `MeshPart.to_solid()` that turns a manifold mesh into a
  valid build123d `Solid` quickly (the `p7` direct-shell-assembly path).
- **G4 — Interop with native shapes.** `MeshPart` and native `Part`/`Solid`
  combine via operators and free functions; tessellation of BREP operands is
  automatic and under the hood.
- **G5 — Mesh-origin geometry that is still selectable** where it can be:
  provenance tags survive booleans; planar faces are rebuildable into real
  selectors (`p8`).
- **G6 — Capabilities OCC simply lacks.** 3D convex hull, Minkowski sum, SDF /
  level-set → solid — all native in manifold3d 3.x, none in OCCT.
- **G7 — Opt-in, zero-footprint when absent.** `pip install build123d` is
  unchanged; `manifold3d` is an optional extra; absence degrades to a clear
  `ImportError`-with-guidance, never a crash.
- **G8 — A clean foundation for the scad2py backend (Goal 2)** and for a
  lightweight browser build (doc 11): the mesh path is the natural target for
  OpenSCAD-style CSG and is ~22× smaller in WASM than OCP.wasm.

### 1.2 Non-goals

- **NG1 — Replacing the OCC kernel.** OCC stays the default and only kernel for
  primitives, fillet/chamfer, loft/sweep/offset/shell/draft, STEP/IGES, and
  exact metrology. manifold3d's job is *narrow*: bulk CSG and the ops OCC lacks.
- **NG2 — A polymorphic `.wrapped`.** `Shape.__init__` unconditionally calls
  `downcast(obj)`, and `downcast`/`shapetype` (`shape_core.py:3684`/`:3762`)
  are hardwired to `TopoDS`. Making `.wrapped` hold a non-`TopoDS` object is a
  deep, risky core change for an *optional* feature. Out of scope. (`p3` §3.)
- **NG3 — Implicit / silent mesh↔BREP conversion.** Every crossing of the
  boundary is a verb the user typed. An implicit coercion would hide
  minutes-long sews and silent geometry degradation behind innocent attribute
  access.
- **NG4 — Recovering analytic geometry.** BREP→mesh→BREP is permanently
  faceted. A round-tripped sphere is a polyhedron. Analytic refit
  (`brep_from_stl.detect_primitives`) is a *separate* problem and out of scope
  here (it is incomplete anyway — doc 02 §5).
- **NG5 — `fillet`/`chamfer`/`loft`/`sweep` on mesh shapes.** They need analytic
  edges, surfaces, and blend geometry. Undefined on a faceted mesh — `MeshPart`
  must not pretend otherwise.
- **NG6 — A 2D mesh subsystem in Phase 0–2.** manifold3d's `CrossSection`
  exists; a `MeshSketch` is plausible but deferred (see §11, open question).
  2D stays on OCC `Face`/`Sketch` for now.
- **NG7 — Mutating `Shape` operators to accept `MeshPart`.** `native_part -
  mesh_part` cannot work without patching `Shape.__sub__`/`_bool_op`; that is
  core surgery for an optional extra. The asymmetry is documented, not fixed
  (§4.5).

---

## 2. Design principles

These are load-bearing. Every API decision below is justified against them.

### P1 — Stay in mesh space across CSG chains

The whole value proposition collapses if you bake to BREP between operations.
`p2` Workload 2 measured it: a deep CSG tree where the boolean speedup is 20–47×
drops to **0.2×–1.1× once the per-step BREP back-conversion is paid** — i.e.
*no faster than, or slower than, OCC*. CSG must happen entirely on
`manifold3d.Manifold`, which is itself lazy (transforms accumulate; evaluation
is forced only by touching geometry). Convert *out* at most once.

### P2 — mesh↔BREP conversion is always explicit and user-visible

There is no implicit coercion. `MeshPart` deliberately has no `.wrapped` and is
not a `Shape`, so it *cannot* be silently fed to BREP code. Crossing the
boundary is `MeshPart.from_part(...)` / `MeshPart.to_solid(...)` /
`Solid.from_mesh(...)` / `mesh_fuse(...)` — always a named verb. (`p3` §4.)

### P3 — Never silently degrade exact geometry

BREP→mesh is *lossy* (curves → facets) and *irreversible* (`p1` §3, `p8`
POINT 1). The API must make that loss visible: the conversion is explicit (P2),
the docstrings shout about it, and mesh-mode must **refuse** operations that
need analytic geometry (`geom_type == CYLINDER`, `fillet`) rather than
mis-answer. `p8`'s "one outcome to forbid" is a *silent wrong result* — a
268-face triangle soup whose `geom_type` still lies "PLANE".

### P4 — Opt-in, zero impact when not installed

`manifold3d` is never a core dependency (issue #1228 prescribes exactly this,
and the maintainer who filed it blessed the shape). `import build123d` must not
import `manifold3d`. With the extra absent, the manifold surface raises a clear,
actionable `ImportError`; nothing else changes.

### P5 — Bake once, late, unvalidated

When a BREP *is* required, convert exactly once, as late as possible, and skip
per-step `BRepCheck_Analyzer` validation — at 1M faces `BRepCheck` costs ~76 s
and ~15 GB (`p7` §7). manifold3d *guarantees* watertight, consistently-wound
2-manifold output; trust that guarantee on the trusted manifold→BREP path
instead of re-validating.

### P6 — Reuse the one clean seam; don't invent a kernel abstraction

build123d has **no kernel abstraction layer** — OCP is assumed in every module
(doc 01 §7.1). Do not build one. The single clean extension seam is the
`composite_factories` registry (`shape_core.py:196,937`), which lets new code
produce correctly-typed `Part`/`Solid` results without import cycles. Reuse it;
touch nothing else in core that an opt-in extra shouldn't.

### P7 — Operation ordering: where fillet/chamfer sit relative to the mesh stage

The mesh backend replaces *booleans* (and adds hull / Minkowski / SDF); it does
not itself perform *finishing* operations — `fillet`, `chamfer`, `loft`,
`sweep`, exact `shell`/`offset`, draft — which are BREP-only. What matters is
what survives a trip *through* the mesh stage, and `p9` proved the answer splits
cleanly on planar vs curved:

- **All-planar CSG → fillet/chamfer *after* the mesh stage is a supported
  path.** With systematic faceID seeding (§3.4), an all-planar manifold result
  reconstructs to an *exact, analytic* B-rep — planar faces on the known
  `Geom_Plane`, exact straight edges, bit-exact volume. Real `BRepFilletAPI`
  fillet/chamfer run on it and yield genuine analytic blend faces (`p9` payoff:
  same face/edge count as the native boolean, 17/17 tests, STEP-valid). For
  flat-faced parts the mesh path is *not* "faceted only."
- **Curved geometry → fillet/chamfer must happen *before* the mesh stage.** A
  curved face that is tessellated and meshed returns faceted; faceID still
  identifies "this region lies on `Geom_Cylinder r=R`" but exact re-trim is
  unsolved (Tier C). A `fillet` on a curved mesh-origin region has no analytic
  surface to blend — it must raise (P3), never mis-answer.

So the rule is not a blanket "fillet before meshing"; it is: **curved finishing
before the mesh stage; planar finishing may come after.**

```
exact BREP modelling ─► curved fillet/chamfer ─► mesh backend (bulk CSG,
   hull, Minkowski) ─► faceID-seeded bake ─► planar fillet/chamfer ─► export
                                                │  planar → exact analytic B-rep
                                                │  curved → faceted (id kept)
```

---

## 3. Architecture

### 3.1 Layering

The integration is a new sub-package, `build123d.mesh`, plus two small,
self-contained additions to core. Imports flow strictly upward; nothing in core
imports `manifold3d`.

```
                      ┌──────────────────────────────────────────┐
   user / scad2py ───►│  build123d.mesh  (the build123d[manifold] │
                      │                   optional extra)         │
                      └──────────────────────────────────────────┘
                          │            │             │
        ┌─────────────────┘            │             └──────────────────┐
        ▼                              ▼                                ▼
  free functions              MeshPart value type              face identity
  mesh_fuse / mesh_cut /      wraps manifold3d.Manifold;        ReFacer: tag-and-
  mesh_intersect /            +/-/& operators; primitives;      remerge → real
  mesh_hull / mesh_minkowski  to_solid()/from_part()            build123d Faces
        │                              │                                │
        └──────────────┬───────────────┴────────────────┬───────────────┘
                       ▼                                 ▼
              ┌──────────────────┐            ┌────────────────────────┐
              │   the bridge     │            │  manifold provenance   │
              │ tessellate+weld  │            │ run_original_id / runs │
              │  (OUT leg, p1)   │            └────────────────────────┘
              └──────────────────┘
                       │
        ┌──────────────┴───────────────────────────┐
        ▼                                          ▼
┌─────────────────────────┐         ┌─────────────────────────────────┐
│  CORE addition #1        │         │  CORE addition #2               │
│  Shape.tessellate(...)   │         │  Solid.from_mesh(verts, tris)   │
│  + a welded variant      │         │  direct shell assembly (p7)     │
│  (already exists @2241)  │         │  ~19k tri/s, linear             │
└─────────────────────────┘         └─────────────────────────────────┘
        │                                          │
        └────────────────────  OCP / OpenCASCADE  ──┘
```

- **`build123d.mesh`** — the entire optional surface. Only this sub-package
  imports `manifold3d`. It is shipped under `build123d[manifold]`.
- **Core addition #1 — a canonical welded tessellation.** `Shape.tessellate()`
  already exists (`shape_core.py:2241`) but emits a non-indexed *soup* —
  duplicate vertices at every face seam (a box → 24 verts, not 8). manifold3d
  rejects soup as `Error.NotManifold` (`p1` §1, `p8` "the one bridge gotcha").
  Add a small welding helper. This is the only OUT-leg work and it is tiny.
- **Core addition #2 — `Solid.from_mesh(verts, tris)`.** The fast
  direct-shell-assembly reconstructor from `p7`. It belongs in core (it is pure
  OCP, useful beyond manifold, and resolves issue
  [#835](https://github.com/gumyr/build123d/issues/835) "Create
  Solid.make_mesh"'s inverse). `build123d.mesh` calls it; it does not depend on
  `manifold3d`.

### 3.2 The mesh value type — `MeshPart` (name TBD)

The prototype calls it `MeshSolid` (`p3`) / `MeshPart` (`p8`). **Recommendation:
`MeshPart`**, because it mirrors build123d's 3D composite name `Part` and a
manifold can be a disjoint multi-body (a manifold "Solid" is misleading; `Part`
is build123d's word for "a 3D thing, possibly compound"). It lives in
`build123d.mesh`.

`MeshPart` is a **standalone value type, not a `Shape` subclass.** Justification
(this is the most important architectural call — restated and defended):

- **The hard blocker.** `Shape.__init__` (`shape_core.py:293-311`)
  unconditionally calls `downcast(obj)`; `downcast`/`shapetype`
  (`:3684`/`:3762`) are free functions hardwired to the `TopoDS` LUTs. A
  `MeshPart` whose payload is a `manifold3d.Manifold` blows up at construction
  if routed through `Shape.__init__`. (`p3` §3.1 — "the single biggest
  integration friction".)
- **The cost of fixing it.** Making those polymorphic touches `downcast`,
  `shapetype`, `Shape.__init__`, `copy`/`deepcopy`, `persistence.py` pickling,
  `__hash__`/`__eq__`, `location`. That is deep core surgery — for an *optional*
  feature. Out of scope per P4/P6 and `p3` recommendation 3.
- **Why a sibling is actually correct, not just expedient.** A `Shape` is by
  contract an exact-BREP entity: it has `geom_type`, exact `.edges()`, can be
  filleted, exported to STEP. A `MeshPart` is *none of those things*. Making it
  an `isinstance(x, Shape)` would be a lie that propagates into every consumer
  (`BuildPart`, `ShapeList`, joints, `export_step`). A separate type makes the
  capability boundary honest and statically visible.
- **The `composite_factories` registry does not rescue this.** It solves
  *result typing* (producing a `Part` vs `Solid` without an import cycle); it
  does not solve the `.wrapped`-is-`TopoDS`-everywhere assumption. (`p3` rec 3.)

`MeshPart` therefore *interoperates with* `Shape` through explicit conversion,
and does **not** subtype it. The asymmetry that produces (§4.5) is a documented,
accepted cost.

### 3.3 The free-function API — the primary surface

The lowest-friction, lowest-risk, zero-core-change deliverable, and Phase 0:

```python
mesh_fuse(*shapes)      -> MeshPart
mesh_cut(base, *tools)  -> MeshPart
mesh_intersect(*shapes) -> MeshPart
mesh_hull(*shapes)      -> MeshPart      # 3D convex hull — OCC has none
mesh_minkowski(a, b)    -> MeshPart      # Minkowski sum — OCC has none
```

Each accepts any mix of build123d `Shape`/`Part`/`Solid` and `MeshPart`;
`Shape` operands are tessellated-and-welded under the hood (the bridge OUT leg).
The result is a `MeshPart` — **not** a `Shape` — which keeps the user in mesh
space (P1) and makes the BREP bake an explicit later step (P2). A user who
wants a `Solid` back types `.to_solid()`.

> **Design note — why the result is `MeshPart`, not `Shape`.** An earlier
> prototype iteration (`p3` recommendation 1) returned `Shape` from the free
> functions, sewing under the hood. `p2` Workload 2 then showed that an
> implicit per-call bake erases the entire speedup. Returning `MeshPart`
> enforces P1 structurally: the user *cannot* accidentally bake between
> operations. The conversion is one explicit `.to_solid()` at the end.

### 3.4 The bridge — OUT leg (`Shape` → `Manifold`)

`Shape.tessellate(tolerance, angular_tolerance)` → **weld** → `manifold3d.Mesh`
(double-precision `Mesh64` — see §8) → `manifold3d.Manifold`.

- **Welding is mandatory.** `tessellate()` concatenates per-face triangulations
  with no cross-face vertex dedup (`shape_core.py:2241`). manifold3d does not
  weld by distance; raw soup → `Error.NotManifold` → silent empty boolean
  (`p1` §1, `p8`). The weld is a grid-snap to build123d's `TOLERANCE = 1e-6`
  (6 decimals — exactly `Mesher._create_3mf_mesh`'s scheme), one
  `np.unique(axis=0)` pass, un-snapped coordinates averaged back per cluster so
  no grid bias is introduced. `p1` verified: box 24→8 verts, sphere 5153→5089,
  every test shape `Error.NoError`, genus preserved.
- **Cost: production-ready.** Sub-100 ms for every shape `p1` tried except a
  31k-triangle torus (0.47 s — it is just a big mesh). The OUT leg is the easy
  half.
- **`Mesh.merge()` vs hand-rolled weld.** manifold3d 3.x's `Mesh.merge()` can
  weld open edges (`p3` §5 used it). Either works; the hand-rolled grid-snap is
  preferred because it is explicit, version-independent, and matches build123d's
  existing `Mesher` tolerance contract. Pick one and apply it consistently both
  directions (doc 02 §9.6 — "pin a single weld tolerance").

### 3.5 The bake-out — IN leg (`Manifold` → `Solid`)

This was the **named #1 blocker**. It is now **cleared** by `p7`.

- **The slow path is dead.** Per-triangle `BRepBuilderAPI_Sewing` (what
  `Mesher._get_shape` `mesher.py:460` and `p1` use) is super-linear: throughput
  *falls* 4827 → 1059 tri/s as the mesh grows; 100k triangles = 95 s;
  extrapolated 1M ≈ 1 hour (`p7` §3, `p1` §4 measured k≈2.08).
- **The fast path: direct `TopoDS_Shell` assembly.** Exploit the fact that a
  welded manifold mesh *already encodes connectivity*: build each
  `TopoDS_Vertex` once, each `TopoDS_Edge` once (keyed by sorted vertex-index
  pair, shared reversed between its two triangles), one `TopoDS_Face` per
  triangle, `BRep_Builder.Add` into one `TopoDS_Shell`, `MakeSolid`. **No
  spatial search.** `p7` measured: dead linear (k = 1.00/0.99/1.02), steady
  **~19 000 tri/s**, **1M triangles → valid `Solid` in ~53 s** (warm),
  **0.0000 % volume error**, `BRepCheck` valid.
- **Skip `ShapeFix_Shell`.** The `+fix` variant is itself super-linear (`p7`
  §3): 42 s vs 5 s at 100k. manifold3d *guarantees* consistent outward winding,
  so the `nofix` path is correct and ~8× faster. (Untrusted meshes still need
  `+fix` — but the manifold→BREP path is trusted.)
- **This is `Solid.from_mesh(verts, tris, *, fix=False)` in core** (addition
  #2). `MeshPart.to_solid()` calls it.

### 3.6 What routes where — the kernel boundary

| Operation | Kernel | Why |
|---|---|---|
| Many-operand union / cut / intersect | **manifold3d** | the O(n²) cliff; 20–134× faster, ~linear (`p2`) |
| Deep CSG trees, transpiled OpenSCAD | **manifold3d** | same; plus guaranteed-manifold kills silent corruption |
| Booleans on dense / imported meshes | **manifold3d** | operates on triangles directly; no invalid-BREP reconstruction |
| 3D convex hull, Minkowski, SDF/level-set | **manifold3d** | OCCT has *no implementation at all* |
| Fillet / chamfer | **OCC** | exact blend surfaces; a mesh cannot represent them |
| Loft / sweep / pipe / offset / shell / draft | **OCC** | exact swept/offset surfaces |
| STEP / IGES export, exact metrology | **OCC** | BREP-native; a mesh degrades these |
| Primitives, exact transforms | **OCC** (or manifold) | both work; native primitives stay OCC |
| One-shot boolean of a few exact primitives | **OCC** | already sub-10 ms; conversion overhead would not pay off |

The boundary is clean: **manifold for bulk/chained/mesh-origin CSG and the ops
OCC lacks; OCC for exact-surface modelling and data exchange.**

---

## 4. API specification

All names below live in `build123d.mesh` and are re-exported so
`from build123d.mesh import *` works. They are **not** added to
`build123d.__init__`'s wildcard (P4 — keep core import clean of `manifold3d`).

### 4.1 `MeshPart` — the value type

```python
class MeshPart:
    """A manifold3d-backed 3D mesh body. A sibling of build123d Part —
    NOT a Shape subclass. Booleans are fast and robust; geometry is faceted."""

    # --- construction -----------------------------------------------------
    def __init__(self, manifold: manifold3d.Manifold,
                 *, label: str = "", color: Color | None = None): ...

    @classmethod
    def from_part(cls, shape: Shape | Part | Solid, *,
                  linear_tolerance: float = 0.1,      # absolute mm — see §8
                  angular_tolerance: float = 0.2,
                  label: str = "", color: Color | None = None) -> MeshPart:
        """Tessellate (OUT leg) + weld + build a Manifold. The lossy crossing."""

    @classmethod
    def from_mesh(cls, vertices, triangles, *,
                  weld: bool = True, color: Color | None = None) -> MeshPart:
        """Wrap a raw triangle array (e.g. an imported STL). Welds by default."""

    @classmethod
    def from_stl(cls, path) -> MeshPart:
        """Robust STL import: read triangles, weld, build a guaranteed-manifold
        body — or raise with the manifold3d Error reason. (Issue #1228 use case.)"""

    # --- primitives (origin-centred, mirror build123d defaults) -----------
    @classmethod
    def box(cls, length, width, height, *, align=Align.CENTER) -> MeshPart: ...
    @classmethod
    def sphere(cls, radius, *, circular_segments=0) -> MeshPart: ...
    @classmethod
    def cylinder(cls, radius, height, *, circular_segments=0) -> MeshPart: ...

    # --- booleans (build123d operator conventions) ------------------------
    def __add__(self, other) -> MeshPart:   # union
    def __sub__(self, other) -> MeshPart:   # difference
    def __and__(self, other) -> MeshPart:   # intersection  (NB manifold uses ^)
    def __radd__(self, other) -> MeshPart:  # see §4.5 asymmetry

    @staticmethod
    def fuse_all(parts) -> MeshPart:        # one-pass batch_boolean
    def hull(self) -> MeshPart: ...
    def minkowski(self, other) -> MeshPart: ...

    # --- transforms (lazy, accumulated by manifold3d) ---------------------
    def translate(self, offset) -> MeshPart: ...
    def rotate(self, x=0, y=0, z=0) -> MeshPart: ...     # degrees
    def scale(self, factor) -> MeshPart: ...
    def moved(self, location: Location) -> MeshPart: ...  # build123d Location

    # --- queries (cheaper than Shape — manifold3d caches) -----------------
    @property
    def volume(self) -> float: ...
    @property
    def area(self) -> float: ...
    def bounding_box(self) -> BoundBox: ...
    @property
    def is_valid(self) -> bool: ...     # manifold status() == NoError
    @property
    def manifold(self) -> manifold3d.Manifold: ...   # escape hatch

    # --- the explicit BREP boundary (P2) ---------------------------------
    def to_solid(self, *, unify_coplanar: bool = False) -> Solid:
        """Bake to a build123d Solid via direct shell assembly (p7). EXPENSIVE
        and faceted — see docstring. ~19k tri/s. unify_coplanar collapses the
        facet explosion (p7 §6) when the result will be hand-edited / shipped."""
    def to_part(self) -> Part: ...

    # --- face identity (Phase 2 — see §6) --------------------------------
    def faces(self, *, crease_deg: float = 20.0) -> ShapeList[Face]: ...
    def faces_from(self, origin: str) -> ShapeList[Face]: ...

    # --- export ----------------------------------------------------------
    def export_stl(self, path, *, ascii=False) -> None:   # cheap — no bake
    def export_3mf(self, path) -> None:                   # cheap — no bake
    def export_step(self, path) -> None:                  # bakes (STEP is BREP)
```

### 4.2 Free functions

```python
def mesh_fuse(*shapes: Shape | MeshPart) -> MeshPart: ...
def mesh_cut(base: Shape | MeshPart, *tools: Shape | MeshPart) -> MeshPart: ...
def mesh_intersect(*shapes: Shape | MeshPart) -> MeshPart: ...
def mesh_hull(*shapes: Shape | MeshPart) -> MeshPart: ...
def mesh_minkowski(a: Shape | MeshPart, b: Shape | MeshPart) -> MeshPart: ...
```

Each tessellates any `Shape` operand and returns a `MeshPart`. These are the
**primary surface** (Phase 0): they need zero changes to the `Shape` hierarchy
and slot into existing code — `result = mesh_cut(block, *holes)`.

### 4.3 Core additions (not in `build123d.mesh`, no `manifold3d` dependency)

```python
# build123d.topology.three_d  (Solid)
@classmethod
def Solid.from_mesh(cls, vertices, triangles, *, fix: bool = False) -> Solid:
    """Build a Solid from a triangle mesh via direct TopoDS_Shell assembly.
    Linear, ~19k tri/s (p7). fix=True runs ShapeFix_Shell for untrusted
    (non-manifold-guaranteed) input; leave False for manifold3d output."""

# build123d.topology.shape_core  (Shape)
def Shape.tessellate(self, tolerance, angular_tolerance=0.1, *,
                     weld: bool = False, relative: bool = True):
    """... existing behaviour ... weld=True returns an indexed, cross-face-
    deduplicated mesh (a box -> 8 verts). relative=False uses absolute
    deflection (see §8)."""
```

`Solid.from_mesh` is justified in core because it is pure OCP, is the natural
implementation of the inverse of issue #835, and is reusable by `import_stl`
(which today produces a useless fake `Face` — doc 02 §4.1). The `weld` kwarg on
`tessellate` is a tiny, backward-compatible addition.

### 4.4 Realistic usage

**(a) Free-function API — the 90% case (Phase 0):**

```python
from build123d import *
from build123d.mesh import mesh_cut

block = Box(100, 100, 10)
holes = [Cylinder(2, 12).moved(Pos(x, y, 0))
         for x in range(-40, 41, 8) for y in range(-40, 41, 8)]

drilled = mesh_cut(block, *holes)        # ~0.5 s — OCC would time out (p2 W4)
drilled.export_stl("plate.stl")          # cheap — no BREP bake

solid = drilled.to_solid()               # explicit, expensive, faceted (P2)
export_step("plate.step", solid)
```

**(b) `MeshPart` value type — staying in mesh space (Phase 1):**

```python
from build123d.mesh import MeshPart

body = MeshPart.box(50, 50, 50)
for i in range(200):                     # 200 fast booleans, all in mesh space
    body -= MeshPart.sphere(3).translate((i % 10 * 5, i // 10 * 5, 25))
print(body.volume)                       # forces lazy eval — still milliseconds
result = body.to_solid()                 # ONE bake, at the end (P1, P5)
```

**(c) Mixed-operand interop:**

```python
native = Box(20, 20, 10)                 # a build123d Part
mesh   = MeshPart.cylinder(4, 30)

combined = mesh_cut(native, mesh)        # Part tessellated under the hood -> MeshPart
also     = mesh + native                 # MeshPart.__add__ coerces the Part
# native + mesh  -> see §4.5: the mesh operand must be on the LEFT
```

**(d) Capabilities OCC lacks:**

```python
from build123d.mesh import mesh_hull, mesh_minkowski
hull   = mesh_hull(*[Sphere(2).moved(Pos(*p)) for p in points])
rounded = mesh_minkowski(Box(20, 20, 5), MeshPart.sphere(2))   # faceted "fillet"
```

### 4.5 Operator interop with native `Part`/`Solid` — the asymmetry

`MeshPart` operators coerce a `Shape` operand by tessellating it:

- `mesh_part - native_part` — **works.** `MeshPart.__sub__` sees the `Part`,
  calls `MeshPart.from_part` on it.
- `native_part - mesh_part` — **does not work.** This hits `Shape.__sub__` →
  `_bool_op`, which expects a `TopoDS` operand and cannot see a `MeshPart`.
  `__radd__` papers over `+` only.

This asymmetry is **inherent** — full symmetry needs patching `Shape`'s
operators, which is core surgery for an optional extra (NG7, `p3` §3.3).
**Resolution:** document loudly that *the mesh operand must be on the left*, and
steer users to the free-function API (`mesh_cut(native, mesh)`) where operand
direction is explicit and the asymmetry vanishes. The free functions are the
recommended interop surface for exactly this reason.

### 4.6 Color & property handling

manifold3d carries per-vertex float properties (channels 3+) and exact
per-triangle-run integer tags (`run_original_id`). Two mechanisms, two uses:

- **Per-object color → `run_original_id` integer tags** (the recommended
  default). `reserve_ids` + a Python `{id: Color}` map. Exact, survives every
  boolean without float interpolation (`p8` TAG 1–5). On bake-out, the dominant
  run's color is applied to the `Solid`; with the `ReFacer` (§6) each rebuilt
  `Face` gets its origin's color.
- **Per-vertex / gradient color → `set_properties` RGBA channels.** Survives
  booleans but is interpolated at cut seams and picks up float32 rounding
  (`doc 03` §4.2). `p3` §5 found that collapsing per-vertex RGB to a single
  `Color` is lossy (red box + blue sphere reported "blue" because the sphere
  has more vertices). **Reserve per-vertex properties for genuine gradients
  only;** use `run_original_id` for solid color.

`MeshPart.color` getter/setter operate on the `run_original_id` channel.

### 4.7 Export

| Method | Bakes to BREP? | Cost |
|---|---|---|
| `MeshPart.export_stl` / `export_3mf` | **no** | cheap — writes triangles directly |
| `MeshPart.export_step` | **yes** | expensive — STEP is a BREP format |
| `export_stl(solid, ...)` after `to_solid()` | (already baked) | the bake dominated |

For cheap mesh export, `build123d.mesh` writes STL/3MF straight from the
manifold triangle arrays. **Core change recommended** (`p3` rec 5): add
`Mesher.add_mesh(verts, tris, color)` so `Mesher` accepts raw arrays — today it
only accepts `Shape`s, which forces a needless bake for 3MF export. Small,
self-contained, Phase 2.

---

## 5. The mesh↔BREP boundary

### 5.1 When conversion happens — precise rules

| Crossing | Trigger | Implicit? |
|---|---|---|
| `Shape` → mesh (OUT) | `MeshPart.from_part()`, any `mesh_*` free function with a `Shape` operand | explicit verb; cheap (<100 ms typical) |
| mesh → `Solid` (IN) | `MeshPart.to_solid()` / `to_part()` / `export_step()`, `Solid.from_mesh()` | **explicit verb, never implicit** (P2) |
| mesh → mesh-file | `MeshPart.export_stl/3mf` | explicit; no BREP involved |

The OUT leg *may* be cheap-and-implicit-feeling (a `mesh_cut(native_box, ...)`
tessellates `native_box` silently) — that is acceptable because BREP→mesh is
cheap and the *result* is a `MeshPart` so the user knows they are in mesh land.
The IN leg is **never** implicit: there is no `MeshPart` method or attribute
access that silently sews. (`p3` §4 — "keep the bake a verb the user types".)

### 5.2 The "bake once, late" pattern

This is P1 + P5 made operational. The canonical pipeline:

```
Shape leaves ──tessellate+weld──► Manifold ──N booleans (mesh space)──►
                                                        Manifold
                                                            │
                                              ONE Solid.from_mesh()  ◄── bake
                                                            │
                                                    Solid (faceted)
```

Never bake mid-chain. `p2` Workload 2 proved an implicit per-step bake erases
the 20–47× boolean speedup down to 0.2–1.1×. The `MeshPart`-returning API
(§3.3) enforces this structurally — the user *cannot* bake between operations
because the intermediate values are `MeshPart`, not `Shape`.

### 5.3 What `to_solid()` costs

Measured (`p7`, direct shell assembly, `nofix`):

| Triangles | Bake time | Throughput |
|---:|---:|---:|
| 512 | 0.027 s | 18 964 tri/s |
| 8 192 | 0.434 s | 18 886 tri/s |
| 100 352 | 5.16 s | 19 463 tri/s |
| 991 232 | 52.8 s (warm) | 18 778 tri/s |

Dead linear at ~19k tri/s. A typical few-k-triangle CSG result bakes in
~0.1–0.5 s; a 100k-triangle result in ~5 s; 1M in under a minute. **The bake is
no longer the blocker.** `to_solid()`'s docstring states the cost and the
faceting; it is an expensive, named, opt-in verb — not a hidden one.

### 5.4 The huge-BREP downstream cost — `p7`'s key finding

`p7` §7 found the ceiling has *moved downstream of reconstruction*. A
1M-triangle mesh bakes fine (~53 s), but the resulting 1M-*face* `Solid` is an
unwieldy BREP:

- `BRepCheck_Analyzer` validation: **~76 s, ~15 GB RAM** at 1M faces.
- STEP export: **~147 s, a 2.5 GB file, ~20 GB peak RSS.**
- Subsequent OCC booleans on a million-face faceted solid are themselves slow —
  OCC is not built for million-face inputs.

**Design consequences (all already principles above):**

- **Stay in mesh space (P1).** Do every intermediate boolean in manifold3d, not
  on a baked faceted `Solid`.
- **Bake once, late (P5).** Convert at the export boundary or on explicit user
  request — not per operation.
- **Do not validate intermediates (P5).** Skip `BRepCheck` on the trusted
  manifold→BREP path; trust manifold3d's watertight-2-manifold guarantee.
- **`unify_coplanar` is opt-in.** `ShapeUpgrade_UnifySameDomain` collapses the
  facet explosion (cube 12→6 faces, cylinder 188→50, holed plate 208→54 — `p7`
  §6) and is cheap on small/medium meshes. Run it when the baked `Solid` is
  user-facing (hand-editing, STEP shipping); skip it for throwaway intermediate
  operands. It cannot un-facet a sphere (0% reduction on fully-curved geometry).

The honest framing for users: a `MeshPart` is *cheap and fast*; a baked `Solid`
of mesh origin is *correct but heavy*. Keep geometry in `MeshPart` form for as
long as possible.

---

## 6. Face identity & selectors

build123d's entire UX is selector-driven (`.faces()`, `sort_by`, `filter_by`,
`group_by`, then `fillet`/`chamfer`/joint placement — doc 01 §2.4). A triangle
mesh has no faces, only triangles. `p8` derisked this — the named top
integration risk — end-to-end against build123d's *real* `ShapeList` selectors.

### 6.1 The verdict: partially recoverable

**What is permanently lost** (a hard ceiling — `p8` POINT 1, VERDICT):

- **Analytic curved-face selectors.** A cylindrical bore becomes a fan of flat
  triangle-pairs; `filter_by(GeomType.CYLINDER)` drops from 1 to **0**.
  `geom_type` on a curved patch is meaningless.
- **`fillet`/`chamfer` after a mesh boolean.** They need an analytic edge
  shared by two analytic faces plus an exact blend surface. Undefined on a
  faceted patch — *undefined*, not merely unsupported.
- **Exact feature edges/vertices.** Only facet edges remain.

**What survives** (robust, 15/15 `p8` tests pass):

- **Provenance is exact and total.** Every output triangle — *including every
  boolean-created cut face* — is attributable to an input solid (and, with
  `reserve_ids`, to an input *face*) via `run_original_id`. Survives union,
  difference, intersection, and chained booleans. Cut faces inherit the *tool*
  solid's id; nothing is orphaned.
- **Planar faces rebuild losslessly into REAL build123d `Face` objects.**

### 6.2 Manifold's metadata channels — ids vs properties

What survives a boolean (§6.1) is not BREP topology — it is *tags*. manifold3d
exposes three metadata channels, and choosing the right one is load-bearing:

| Channel | Granularity | Through booleans | Use for |
|---|---|---|---|
| `run_original_id` (+ `reserve_ids`, `as_original`) | per-triangle run → input **solid**; per input **face** with `reserve_ids` | **exact** — union/diff/intersect/chained; cut faces inherit the tool id | discrete identity / provenance |
| `face_id` | per-triangle → coplanar-region group | partial — **unique only within a run, collides across runs** | a *hint* for coplanar grouping, never a global key |
| `vert_properties` cols 3+ | per-**vertex** floats (color, UV, normals) | carried, but **linearly interpolated at cut vertices** | smoothly-varying data only |

**The rule: discrete identity goes in the integer id channels, never in
`vert_properties`.** A vertex created on a cut edge gets an *interpolated*
property value — fine for a color gradient, corrupting for a discrete face/part
tag (tag `3` averaged with `7` becomes `5`). `research/03` §2.8 and `p8` both
hit this with color.

`face_id` is tempting as a ready-made "face" tag, but `p8` found it unusable as
a global key: it is unique only *within a run*, and a face cut into two disjoint
pieces keeps a single id. The `ReFacer` (§6.3) therefore ignores raw `face_id`
and reconstructs faces from `run_original_id` + welded-edge connectivity +
dihedral angle.

Crucially, none of these channels is *topology*. There is **no usable edge
channel** (`p8` notes a halfedge-level original-id analogue exists but is
unexposed), no adjacency graph, and no surface equations. They are a *labelled
partition of the triangle soup* — enough to rebuild planar `Face`s and drive
provenance selectors (§6.3–6.5), not enough to recover analytic geometry or to
support a fillet (which needs the exact edge and the exact adjacent surfaces,
not a tag — §6.5, P7).

### 6.3 The tag-and-remerge strategy (the `ReFacer`)

`p8`'s `refacer.py` is the reference algorithm. **Tag-and-rebuild, never
preserve** — identity is *reconstructed*, not free (`p8` rec 2):

1. **Tag inputs.** `as_original()` each input *leaf* only — **never a boolean
   product** (that flattens the run table). Keep an `{origin_id: name/Color}`
   map. For per-face tagging, `reserve_ids(n)` and stamp one run per input face.
2. **Run the boolean** in manifold3d. `run_original_id` rides every output
   triangle. (`run_index` is in **halfedge units** — ÷3 for triangle ranges; a
   wrong assumption silently mis-attributes every triangle.)
3. **Region-grow.** Two triangles join the same face iff they (a) share a
   welded edge, (b) share an origin id, and (c) have a dihedral angle below
   `crease_deg` (default 20°). This correctly splits a face cut into two
   disconnected pieces (manifold's raw `face_id` would wrongly keep them as
   one).
4. **Rebuild faces.** Each *planar* cluster is sewn and run through
   `ShapeUpgrade_UnifySameDomain`, which merges its coplanar triangles into a
   **single** build123d `Face` with correct area and `geom_type == PLANE`.

`p8` measured: a plate with a punched square hole → region-grow → exactly **10**
rebuilt `Face` objects, matching the BREP boolean's 10. build123d's *own*
`sort_by(Axis.Z)`, `filter_by(Plane.XY)`, `filter_by(GeomType.PLANE)`,
`filter_by(callable)`, `group_by(SortBy.AREA)` all work on them — not a mock
API, the real `ShapeList`.

`MeshPart.faces(crease_deg=20.0)` exposes this. `crease_deg` is the one tunable
the re-facer must surface: too small fragments faceted curves; too large merges
genuinely distinct faces.

### 6.4 Provenance as a new selector dimension

Mesh tagging is **strictly additive** on one axis: `MeshPart.faces_from(name)`
returns "all faces originating from input solid `name`", including
boolean-created cut faces. This has **no BREP equivalent** — it is a genuine new
capability. (build123d's BREP `_bool_op` *could* expose `BRepAlgoAPI_*.History()`
for the analytic analogue — `p8` POINT 4 — but that is an orthogonal core
improvement, not this design.)

### 6.5 The planar-recoverable / curved-lost split — the API contract

- **Planar-dominated CSG** (transpiled OpenSCAD, plate/bracket work): the
  tag-and-remerge path is **fully sufficient** — exact `Face` counts, all
  directional and plane selectors.
- **Curved geometry:** **provenance-only.** The bore is one provenance cluster
  (addressable by `faces_from`), but `geom_type`/curved selectors are
  unavailable.

**The contract (P3):** `MeshPart.faces()` returns rebuilt planar faces and
faceted patches for curved regions. A curved-analytic selector
(`filter_by(GeomType.CYLINDER)`, `fillet` on a mesh-origin face) must raise a
**clear, explanatory error** — never silently return an empty `ShapeList` or a
2 mm sliver. *The one outcome to forbid is a silent wrong answer.*

---

## 7. Caching

Doc 08's recommendations, applied to this design.

### 7.1 The opportunity

build123d has **no geometry cache** (doc 08 §1 — only a tiny `_color_cache` and
OCCT's fragile implicit per-face triangulation). A manifold backend changes the
calculus: **meshes are cheap to hash, cheap to copy, cheap to serialize** —
unlike OCCT `Shape`s, whose `__hash__` is identity-based and useless as a
content key (doc 08 §1.5). Caching becomes feasible *because* the cached
artifact is a mesh.

### 7.2 In-memory: content-hash-keyed cache of meshes & boolean results

- **The key — content-addressed, FastKey-style.** Adopt OpenSCAD's and
  scad2py's independently-converged idea: **hash a canonical textual
  serialization of the CSG recipe.** scad2py already ships `FastKey` (an
  interned hash of the CSG-subtree text — doc 08 §3.1); the scad2py→build123d
  backend (Goal 2) gets this for free. For direct build123d use without a
  recipe, fall back to `sha256(BinTools.Write_s bytes)` of a `Shape`, memoized
  on the instance. **Never** use Python's `hash(TopoDS_Shape)` — it is
  process-randomized and identity-based (doc 08 §4.5).
- **What to cache: the manifold mesh, not a `Solid`.** A `manifold3d.Mesh` is a
  flat vertex/triangle buffer — cheap `hash`, cheap `clone`, cheap `npz`
  serialize. An OCCT `Solid` is none of those. Caching `MeshPart`/`Manifold`
  results keyed by recipe is high-hit-rate on repetitive CSG (the typical
  OpenSCAD model: a `for` loop instantiating the same part N times collapses to
  one render + N cheap clones).
- **Gate on reuse.** Mirror scad2py's `is_reused` predicate: only cache subtrees
  that appear ≥ 2× (doc 08 §3.4) — keeps the cache small and the hit rate high.
- **Tessellation cache.** Key `(content_hash, linear_tol, angular_tol)` →
  `(verts, tris)`. The OUT-leg `tessellate()` is the recurring expensive op;
  this cache survives `Shape` copies and OCCT's `Clean_s`. Tolerance **must** be
  part of the key.

### 7.3 On-disk: content-addressed blob store

Plausible and the serializers exist (`persistence.serialize_shape`,
`export_brep`), but value depends on the workflow:

- **High value for the scad2py-driven path** — scad2py re-runs the whole
  pipeline every invocation; a persistent geometry cache keyed by `node.key`
  makes the second run of an unchanged model near-instant.
- **Marginal for interactive build123d** — a long-lived process; the in-memory
  layer already captures the repeated-render benefit.

Recommended shape (deferred to Phase 3): `~/.cache/build123d/` of
content-addressed blobs — `.npz` (verts/tris) for meshes, `.brep` for exact
shapes — opt-in via `BUILD123D_CACHE_DIR` (mirroring OpenSCAD's `--cache=file`),
with **LRU eviction by total bytes from day one** (OpenSCAD's persistent-cache
project omitted eviction and that was its known defect — doc 08 §2.5). Tag blobs
with `(occt_version, manifold_version, format_version)` and invalidate on
mismatch.

### 7.4 One cache, not two

OpenSCAD keeps `GeometryCache` + `CGALCache` because preview (PolySet) and
render (Nef) are different representations. A manifold backend collapses that:
manifold booleans are exact *and* fast, so preview and final geometry are the
*same* manifold mesh — **one cache suffices** (doc 08 §4.6). Keep a small,
separate BREP-keyed cache only if exact-STEP-with-fillets export becomes a
measured bottleneck.

---

## 8. Packaging

### 8.1 The optional extra

```toml
# pyproject.toml — [project.optional-dependencies]
manifold = ["manifold3d >= 3.4, < 4"]
```

- `pip install build123d[manifold]` pulls `manifold3d`. Plain
  `pip install build123d` is unchanged (issue #1228's prescription; the
  maintainer who filed it blessed exactly this shape — doc 07 §1.1).
- **Pin `manifold3d >= 3.4, < 4`.** The 3.x line is mandatory: it has the
  **double-precision kernel** (no size-dependent ε loss — 2.3.1 was float32),
  `batch_boolean` (one-pass N-way booleans — used by `fuse_all`),
  `Mesh.merge()`, `minkowski_sum/difference`, and `Mesh64` for
  double-precision I/O. The 2.3.1 wheel doc 03 introspected lacks all of these.
  `< 4` guards against the next major API break.
- **PyPI wheel only — never a git fork.** `manifold3d` ships no-compile wheels
  for macOS/Windows/Linux, Python 3.8–3.13 (doc 03 §7). Do **not** depend on
  `ochafik/manifold` or any GPL mesh lib (`pymeshlab`, CGAL) — Apache+Apache is
  clean (doc 07 §8).

### 8.2 Runtime detection

`build123d.mesh.__init__` does a guarded import:

```python
try:
    import manifold3d as _m3d
except ImportError as exc:
    raise ImportError(
        "build123d's manifold mesh features require the optional extra:\n"
        "    pip install 'build123d[manifold]'"
    ) from exc
```

`import build123d` must **not** import `build123d.mesh` — the extra is dormant
until explicitly imported (P4). A `build123d.mesh.is_available()` helper lets
callers feature-detect without catching `ImportError`.

### 8.3 The manifold3d version / API drift

Research doc 03 introspected **2.3.1**; every prototype (`p1`,`p2`,`p3`,`p7`,
`p8`) ran against **3.4.x**. The 3.x reality, confirmed by the prototypes:

- The float32 data struct is `manifold3d.Mesh` (old `MeshGL`); there is also a
  double-precision `Mesh64`. **Use `Mesh64` for the OUT leg** so OCC's `double`
  vertices survive without float32 truncation (`p1` §"API reality").
- `Manifold(mesh)` welds *only per explicit merge vectors* — it does **not**
  weld by distance. An un-welded soup with no merge vectors is rejected.
  Welding before construction is mandatory (§3.4).
- `batch_boolean`, `OpType`, `minkowski_sum/difference`, `set_tolerance`,
  `Mesh.merge()`, `Manifold.volume()`-as-method all exist in 3.4.x.
- **The pinned API surface must be re-verified against the exact wheel at
  build time** — doc 03's signatures are 2.3.1; treat its `[upstream]` notes as
  *confirmed* by the prototypes (doc 09 §4.1), but introspect the live wheel
  before finalizing the bridge.
- **The 2.3.1 intermittent segfault** doc 03 §3.5 flagged was **not observed**
  on 3.4.x by any prototype. Treat as resolved; spot-check under a long-lived
  process during Phase 0 hardening.

### 8.4 Tessellation tolerance is a public, documented contract

`from_part()` takes `linear_tolerance` / `angular_tolerance` and passes
**absolute** linear deflection. build123d's exporters default to *relative*
(`isRelative=True` — deflection scales per-edge), which is **wrong** for CSG
where parts of different sizes must align on a common grid (`p3` rec 8, doc 02
§1.1). Default: **0.1 mm absolute** linear, **0.2 rad** angular — `p1` §5 found
`1e-3`/`0.1` is the fidelity sweet spot (<0.2 % volume error) but that is
*relative*; the absolute equivalent for typical part sizes is ~0.1 mm. This is a
documented, tunable contract: it sets the fidelity floor for everything
downstream and is the build123d analogue of OpenSCAD's `$fn`.

### 8.5 WASM / browser (forward-looking, not Phase 0–3)

A manifold-backed build is **materially better for the browser** than the OCP
path (doc 11 §5): a `manifold3d` emscripten wheel is ~0.5–1 MB vs OCP.wasm's
~22 MB; manifold *is* a WASM-first library (it powers the OpenSCAD web
playground). The `build123d.mesh` layer is the natural lightweight browser
geometry backend. Not on the critical path, but the design should not preclude
it — keeping `build123d.mesh` free of OCP-only dependencies (it depends only on
`Shape.tessellate` and `Solid.from_mesh`, both thin) keeps a manifold-only
browser build feasible.

---

## 9. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Un-welded tessellation → silent empty boolean.** `Shape.tessellate()` emits a 24-vert soup; manifold rejects it as `NotManifold`, `volume()==0`, boolean silently empty. | **High** (silent wrong result) | Mandatory weld pass in the bridge (§3.4). Verified by `p1` §1, `p8` test. The weld is the *first* thing `from_part()` does; assert `status()==NoError` after construction and raise if not. |
| R2 | **Implicit per-step bake erases the speedup.** `p2` W2: 20–47× boolean win → 0.2–1.1× with per-op back-conversion. | **High** | Free functions and `MeshPart` operators return `MeshPart`, never `Shape` (§3.3). The user *structurally cannot* bake mid-chain. Bake is one explicit `.to_solid()` (P1, §5.2). |
| R3 | **Silent geometry degradation.** A naive `tessellate→boolean→sew` yields a 268-face soup whose `geom_type` still lies "PLANE" and whose `sort_by(Axis.Z)[-1]` is a 2 mm sliver. | **High** | P3. mesh→BREP is explicit (P2). `MeshPart.faces()` rebuilds planar faces; curved-analytic selectors **raise a clear error** (§6.5). Never mis-answer. |
| R4 | **Curvature & fillets permanently lost** through BREP→mesh→BREP. | **Medium** (inherent, not a bug) | Document prominently; `to_solid()` docstring shouts. Route fillet/chamfer/STEP-grade work through OCC and never let it leave (NG4, NG5). Not "fixable" — communicated. |
| R5 | **Huge-BREP downstream cost.** A 1M-face baked `Solid`: `BRepCheck` ~76 s/15 GB, STEP ~147 s/2.5 GB (`p7` §7). | **Medium** | P5: bake once/late, do not validate intermediates, optional `unify_coplanar`. Keep geometry in `MeshPart` form; warn in `to_solid()` above ~10⁵ triangles. |
| R6 | **`Mesher._get_shape` void-detection bug** mis-classifies a multi-body mesh as one Solid-with-fake-void (`p1` §2). | **Medium** | `Solid.from_mesh` (core addition #2) uses bbox-nesting classification, not "largest = outer, rest = voids". `p1`'s `bridge.py` already has the fix to port. Latent build123d bug — document, fix in the new code path, do not silently inherit. |
| R7 | **Operator interop asymmetry** — `native - mesh` does not work. | **Low** | Documented; steer to the free-function API where direction is explicit (§4.5). Inherent to a non-`Shape` value type (NG7). |
| R8 | **manifold3d API drift** — doc 03 is 2.3.1; prototypes are 3.4.x; a future 3.x bump could move signatures. | **Low–Medium** | Pin `>= 3.4, < 4` (§8.1). Re-introspect the live wheel at build time. The bridge is small and isolated in `build123d.mesh` — one place to patch. |
| R9 | **Per-vertex color is lossy at seams** — interpolated, float32 rounding; modal collapse picks the wrong color (`p3` §5). | **Low** | Use `run_original_id` integer tags for solid color (exact, §4.6); reserve per-vertex properties for genuine gradients only. |
| R10 | **Intermittent 2.3.1 segfault** (doc 03 §3.5) in a long-lived process. | **Low** | Not reproduced on 3.4.x by any prototype (doc 09 §4.1). Pin 3.x; spot-check under a long-lived process in Phase 0 hardening. |
| R11 | **`MeshPart` cannot enter `BuildPart`/`ShapeList`/joints** — not a `Shape`. | **Low** (by design) | Accepted (NG2, §3.2). Explicit `to_solid()` is the bridge into builder contexts. The capability boundary is *intentionally* visible. |
| R12 | **No pickling of `MeshPart`.** `persistence.modify_copyreg` patches OCP types only; `manifold3d.Manifold` picklability is unconfirmed. | **Low** | Pickle the raw `Mesh` arrays (`npz`-style), not the `Manifold` object (doc 08 open Q2). Add a `__reduce__` to `MeshPart` that round-trips through `to_arrays()`. |
| R13 | **`crease_deg` mis-tuning** in the ReFacer over/under-merges faces. | **Low** | Expose `crease_deg` as the one documented tunable (§6.3, `p8` rec 6); default 20° verified by `p8`. |
| R14 | **Threading contention** — manifold3d's TBB pool vs OCC's. | **Low** | Both are CPU pools; no measured contention in prototypes. Monitor; manifold3d falls back to serial below a size threshold. |

---

## 10. Phased implementation plan

Each phase is **independently shippable** and ordered by value/risk. Phase 0 is
the minimal valuable thing.

### Phase 0 — Free-function CSG + the fast bake (the MVP)

**Goal:** the 90% case — fast bulk booleans — with **zero changes to the
`Shape` hierarchy**.

- `build123d.mesh` sub-package; the optional extra in `pyproject.toml`; runtime
  detection (§8.1–8.2).
- The bridge OUT leg: `tessellate` + weld + `Mesh64` + `Manifold` (§3.4).
- `Solid.from_mesh(verts, tris)` in core — direct shell assembly (`p7`, §3.5).
  Includes the `p1` multi-body bbox-nesting fix (R6).
- Free functions `mesh_fuse`, `mesh_cut`, `mesh_intersect` returning a
  *minimal* `MeshPart` (a thin `Manifold` wrapper with `.to_solid()`,
  `.volume`, `.export_stl`). Internally uses `batch_boolean`.
- Tests (per `CONTRIBUTING.md`: docstrings, `mypy`, `pylint`, `black`,
  `pytest -n auto`), the `p1`/`p2`/`p7` benchmarks as regression checks.

**Ships:** `result = mesh_cut(block, *holes); result.to_solid()`. Directly fixes
the OCC pain (mandatory `ShapeUpgrade` clean per op, O(n²) accumulation, the
1000-hole timeout). **Value: high. Risk: low** — no core surgery beyond the two
small additive helpers.

### Phase 1 — The `MeshPart` value type

**Goal:** let users *stay in mesh space* across a long CSG chain (G2, P1).

- Full `MeshPart` per §4.1: primitives, `+`/`-`/`&`, transforms, queries,
  `from_part`/`from_mesh`/`from_stl`, `to_solid`/`to_part`, `fuse_all`.
- Operator coercion of `Shape` operands; the documented left-operand asymmetry
  (§4.5).
- `mesh_hull` / `mesh_minkowski` and `MeshPart.hull/minkowski` (G6).
- Color via `run_original_id` (§4.6).

**Ships:** the `p3` prototype, hardened. **Value: high. Risk: low** —
standalone class, no `Shape` changes.

### Phase 2 — Face identity, selectors, cheap export

**Goal:** make mesh-origin geometry selectable where it can be (G5).

- The `ReFacer`: tag-and-remerge → real build123d `Face` objects (§6, `p8`).
- `MeshPart.faces(crease_deg=...)` and `MeshPart.faces_from(name)`.
- Curved-analytic selectors raise clear errors (§6.5, P3).
- `Mesher.add_mesh(verts, tris, color)` in core → genuinely cheap STL/3MF
  export without a bake (§4.7, `p3` rec 5).

**Ships:** `mesh_cut(...).faces().sort_by(Axis.Z)[-1]` for planar work.
**Value: medium–high** (unblocks selector-driven workflows). **Risk: medium**
(the ReFacer is heuristic; `crease_deg` tuning).

### Phase 3 — Caching

**Goal:** doc 08 — content-addressed caching for repetitive CSG.

- In-memory recipe cache (FastKey-style) of `Manifold`/`MeshPart` results,
  gated by `is_reused`; the scad2py→build123d backend reuses scad2py's
  `CachingVisitor`/`@renderer` wholesale (doc 08 §4.5 Layer 1).
- In-memory tessellation cache keyed `(content_hash, tol, angular_tol)` (Layer
  2).
- Optional on-disk blob store under `BUILD123D_CACHE_DIR`, LRU-by-bytes,
  version-tagged (Layer 3 — only if scad2py re-run latency proves a real
  complaint).

**Value: medium** (high for the scad2py path, marginal for interactive).
**Risk: low–medium.**

### Phase 4 (forward-looking, not committed) — WASM browser build

A manifold-only `build123d.mesh` browser build (doc 11 §5): rebuild a current
`manifold3d` emscripten wheel, validate `build123d.mesh` in pyodide. Off the
critical path; the design above keeps it feasible (§8.5).

---

## 11. Open questions / decisions needing the owner

1. **Name.** `MeshPart` vs `MeshSolid` vs `MeshBody`. This doc recommends
   **`MeshPart`** (mirrors `Part`; a manifold can be multi-body). Owner to
   ratify — it is a public API name.

2. **Issue #1228 ownership & coordination.** `jdegenstein` (a maintainer) filed
   #1228 and prescribed the `build123d[manifold]` shape. Should the owner
   comment on #1228 *before* significant work, to (a) confirm a PR from the
   fork is welcome and (b) align on the name and the free-function-vs-value-type
   split? Recommended: yes — maximizes merge odds (doc 07 §9 Q1).

3. **Should `Solid.from_mesh` and the `weld` kwarg on `tessellate` go to
   upstream build123d core independently of the manifold extra?** They are pure
   OCP, useful on their own (`from_mesh` is the inverse of issue #835), and
   carry no `manifold3d` dependency. Recommended: yes — submit them as a
   separate, smaller core PR that the extra then builds on. Owner to decide
   sequencing.

4. **2D — is a `MeshSketch` over `manifold3d.CrossSection` in scope?** Deferred
   here (NG6). OpenSCAD's 2D subsystem (`square`/`circle`/`polygon` +
   `linear_extrude`/`rotate_extrude`) maps cleanly onto `CrossSection`, and the
   scad2py backend (Goal 2) will want it. Decision: keep 2D on OCC for Phases
   0–3, or add `MeshSketch` in Phase 2? (Affects the Goal 2 design.)

5. **Auto-fallback when an OCC boolean times out / `IsDone()==False`?** doc 10
   open Q2 raises it. An automatic mesh fallback changes result *type*
   (analytic → faceted) *silently* — which violates P3. Recommendation:
   **no auto-fallback**; mesh mode is always explicit. Owner to confirm this is
   acceptable (it means a slow OCC boolean stays slow unless the user opts into
   `mesh_*`).

6. **Should build123d's BREP `_bool_op` also surface `BRepAlgoAPI_*.History()`?**
   `p8` POINT 4 found it is the exact, lossless BREP analogue of manifold's
   `run_original_id` and is currently unused (`shape_core.py:2459`). It is an
   *orthogonal* core improvement (better BREP-mode provenance), not part of this
   design — but the owner may want to bundle it. Out of scope here; flagged.

7. **Default tessellation tolerance — relative or absolute, and what value?**
   §8.4 recommends **absolute, 0.1 mm linear / 0.2 rad angular**. This is the
   user-visible fidelity floor and the build123d analogue of `$fn`. Owner to
   ratify the default and confirm absolute (the prototypes used absolute;
   build123d's exporters default to relative).

8. **`UnifySameDomain` on bake — default off or on?** §5.4 / `p7` §6: it
   collapses the facet explosion (cube 12→6 faces) but is wasted on throwaway
   intermediate operands and cannot un-facet curves. Recommendation: default
   **off** (`to_solid(unify_coplanar=False)`); on when the `Solid` is
   user-facing. Owner to confirm the default.

---

## Appendix A — prototype evidence index

| Claim in this doc | Prototype | Key measured numbers |
|---|---|---|
| OUT leg is fast & robust | `p1` §1 | sub-100 ms typical; every shape `Error.NoError`, genus preserved |
| Welding is mandatory | `p1` §1, `p8` "gotcha" | box 24→8 verts; raw soup → `NotManifold`, empty boolean |
| Round-trip adds no vertex error | `p1` §3 | <0.15 % volume error, all from tessellation; box exact to 2.3e-16 |
| Per-triangle sewing is the wall | `p1` §4, `p7` §3 | k≈2.08; 130k tris = 3.4 min; 100k = 95 s; 1M ≈ 1 hr |
| Direct shell assembly clears the blocker | `p7` §3,§7 | k≈1.0, ~19k tri/s, 1M tris → valid Solid in ~53 s, 0.0000 % vol err |
| manifold booleans 20–134× faster | `p2` W1,W4 | grid union 27–134×; the win grows with N |
| OCC times out at N=1000 drill | `p2` W4 | OCC >120 s timeout; manifold 0.51 s |
| Per-step bake erases the speedup | `p2` W2 | 20–47× boolean → 0.2–1.1× with `+bake` |
| OCC 7.9 handles coincident geometry | `p2` W3 | all 10 degenerate cases correct & valid — speed, not correctness, is OCC's weakness |
| `MeshSolid` cannot be a `Shape` | `p3` §3 | `downcast`/`shapetype` hardwired to `TopoDS` |
| 71× speedup, valid bake, color survives | `p3` §2 | 72 sphere ops: 35 ms vs 2499 ms; baked Solid `is_valid=True` |
| Provenance survives every boolean | `p8` POINT 2 | union/diff/intersect/chained; cut faces tagged to tool solid |
| Planar faces rebuild into real `Face`s | `p8` POINT 3 | holed plate → exactly 10 faces, build123d selectors work |
| Curved selectors & fillet permanently lost | `p8` POINT 1, VERDICT | `filter_by(GeomType.CYLINDER)` → 0 |

---

## Appendix B — key build123d source references

| Symbol | Location | Relevance |
|---|---|---|
| `Shape.__init__` → `downcast(obj)` | `shape_core.py:293-311` | unconditional `TopoDS` downcast — why `MeshPart` cannot be a `Shape` |
| `downcast` / `shapetype` | `shape_core.py:3684` / `:3762` | hardwired to `TopoDS` LUTs (NG2) |
| `composite_factories` / `register_composite_factory` / `make_composite` | `shape_core.py:196` / `:937` / `:945` | the one clean extension seam (P6) |
| `Shape.tessellate` | `shape_core.py:2241` | OUT leg; emits un-welded soup — needs the `weld` kwarg |
| `Shape.mesh` | `shape_core.py:1604` | `BRepMesh_IncrementalMesh`; `isRelative=True` default (§8.4) |
| `Shape._bool_op` | `shape_core.py:2459` | OCC boolean engine; auto-`UnifySameDomain` per op |
| `Mesher._get_shape` | `mesher.py:460` | the slow per-triangle sewing path being replaced |
| `Mesher._create_3mf_mesh` | `mesher.py:312` | grid-snap weld scheme the bridge mirrors |
| `Solid` (constructor takes raw `TopoDS_Solid`) | `topology/three_d.py` | `Solid.from_mesh` builds and wraps directly |
| `import_stl` → fake `Face` | `importers.py:270` | today's useless mesh import — `from_mesh` could fix it |
| `[project.optional-dependencies]` | `pyproject.toml:64` | where `manifold = [...]` goes |
| `cadquery-ocp-novtk >= 7.9, < 8.0` | `pyproject.toml:36` | OCCT 7.9 — the kernel kept for exact BREP |

---

*End of design. This document is the deliverable; no build123d source was
modified.*
