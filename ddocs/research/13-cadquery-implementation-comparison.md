# 13 — CadQuery ↔ build123d manifold backend: **implementation-stage** cross-comparison

> Author: research agent · Date: 2026-05-23 · Knowledge cutoff: Jan 2026.
>
> **The two parallel research efforts have both shipped a manifold backend.**
> This document is the **implementation-stage** comparison between the two
> *shipped* code bases. It supersedes `09-cadquery-ddocs-cross-comparison.md`
> for everything code-level; `09` was written *before either side had
> implemented anything*, and is now the pre-implementation snapshot.
>
> | "us" — `build123d` | "them" — `cadquery` |
> |---|---|
> | repo: `/Users/ochafik/github/build123d` | repo: `/Users/ochafik/github/cadquery` |
> | branch: `feat/manifold-mesh-backend` | branch: `ddocs-exploration` |
> | tip: `d1638000 docs(algorithms): §J — A4 variable radius and skip mode` | tip: `c6110fc6 manifold: rename minkowski_difference -> minkowskiDifference` |
> | backend package: `src/build123d/mesh/` (9 modules, 6 901 LOC) | backend package: `cadquery/manifold/` (2 files) + `cadquery/occ_impl/manifold_bridge.py` (1 file); 1 267 LOC total |
> | tests: `tests/test_mesh.py` (2 349 LOC, 156 test funcs) | tests: `tests/test_manifold.py` (474 LOC, 34 test funcs) |
> | algorithm reference: `ddocs/design/algorithms.md` (55 algorithms §A–§J) | design synthesis: `cadquery/ddocs/design/20-manifold-integration-design.md` |
>
> Both efforts target the **identical substrate**: OCCT 7.9.3 via the
> `cadquery-ocp` (`OCP.*`) pybind11 bindings, with `manifold3d 3.4.1` from
> PyPI as the optional dependency. Every architectural divergence below is
> therefore a *design choice*, never a substrate forcing.

---

## 1. Orientation — what shipped, what didn't

Both backends are now real, runnable code with their own test suites and
their own design docs.

* **CadQuery shipped Phase 1.** The `20-manifold-integration-design.md` doc
  explicitly phases the work as P1 (mesh CSG / minkowski / hull, Tier-1
  mesh→BREP faceted bake), P2 (`MeshShape` as public type + cache), P3
  (selector reconstruction + Tier-2 analytic recovery). The current
  `ddocs-exploration` branch is **P1 only**: the `MeshShape` value type, the
  `cadquery.manifold` free-function namespace, the BRep→mesh bridge, the
  Tier-1 mesh→BREP faceted bake. P2 (cache) and P3 (selectors, analytic
  recovery) are designed but **not built**. (`cadquery/manifold/__init__.py:307`
  lists the entire public surface; `cadquery/ddocs/design/20-…md:584` defines
  the phasing.)
* **build123d shipped G1-P1 + G1-P2 + G1-P3 + the A3/A4 mesh-fillet stack.**
  The `feat/manifold-mesh-backend` branch contains:
  * G1-P1 (Phase 0 commit `d33f750b`) — the same Phase-1 slice CadQuery has.
  * G1-P2 (`ef777c95`) — systematic faceID seeding + exact B-rep recovery
    (`recovery.py`, 548 LOC).
  * G1-P3 (`360cb6a2`) — hull, minkowski (two backends), faceID selectors.
  * Cheap native approximation-mode ops (`d85ce458`) — extrude / revolve /
    offset / shell on the mesh side.
  * A3a / A3b / A3c / A4 (`8120c7ac`, `c6816319`, `ebff4842`, `eb4913ba`) —
    mesh chamfer, mesh fillet, multi-chain corner blends, variable radius
    + `on_infeasible='skip'` mode. **Curved-feature-edge mesh fillet is
    something neither library has ever had**, native BREP or otherwise.

The two efforts therefore overlap in scope at G1-P1 and diverge sharply from
there. The rest of this document is structured around that divergence.

---

## 2. API surface — side by side

Both libraries chose a value type sibling to (not subclass of) the native
B-rep shape, with both methods on the value type and free functions in a
namespace. The names track each host library's own idioms.

| Concern | `cadquery.manifold` | `build123d.mesh` |
|---|---|---|
| Module path | `from cadquery import manifold as cqm` | `from build123d.mesh import …` |
| Value type | `MeshShape` (`_meshshape.py:73`) | `MeshPart` (`mesh_part.py:86`) |
| Subclass of host shape? | **No** — by design (`_meshshape.py:22`) | **No** — by design (`mesh_part.py:90`) |
| Build from host shape | `MeshShape.fromShape(s)` / `.coerce(s)` | `MeshPart.from_part(s)` / `_coerce(s)` |
| Build from raw arrays | not exposed (only via `Manifold`) | `MeshPart.from_mesh(verts, tris)` |
| Bake to host shape | `MeshShape.toShape(unify=, fix=)` | `MeshPart.to_solid(reconstruct=, unify_coplanar=)` |
| Boolean union | `cqm.fuse(a, *rest)` / `MeshShape.fuse` / `+` | `mesh_fuse(*shapes)` / `MeshPart.__add__` / `+` |
| Boolean difference | `cqm.cut(a, b, *rest)` / `MeshShape.cut` / `-` | `mesh_cut(base, *tools)` / `MeshPart.__sub__` / `-` |
| Boolean intersection | `cqm.intersect(a, b, *rest)` / `MeshShape.intersect` / **`*`** | `mesh_intersect(*shapes)` / `MeshPart.__and__` / **`&`** |
| 3D hull | `cqm.hull(*ops)` / `MeshShape.hull()` | `mesh_hull(self, *others)` / `MeshPart.hull()` |
| Hull from points | `cqm.hullPoints(arr)` | (not exposed as a public free function — wrapped inside `mesh_hull`) |
| Minkowski sum | `cqm.minkowski(a, b)` / `MeshShape.minkowski` | `mesh_minkowski(a, b, method=)` / `MeshPart.minkowski` |
| Minkowski difference | `cqm.minkowskiDifference(a, b)` / `MeshShape.minkowskiDifference` | `mesh_minkowski_difference(a, b)` / `MeshPart.minkowski_difference` |
| 3D offset | not exposed | `mesh_offset(self, amount, sphere_segments=)` / `MeshPart.offset` |
| 3D shell | not exposed | `mesh_shell(self, thickness)` / `MeshPart.shell` |
| Linear extrude | not exposed | `mesh_extrude(profile, height, ...)` |
| Revolve | not exposed | `mesh_revolve(profile, ...)` |
| Tessellate input | `cqm.toMesh(obj, deflection=)` | `MeshPart.from_part(s, linear_tolerance=, angular_tolerance=)` |
| Mesh → BREP entry point | `cqm.toBrep(obj, unify=)` | `MeshPart.to_solid()` |
| Volume / area / counts | `Volume()` / `Area()` / `numTriangles()` / `numVertices()` / `genus()` | `volume` / `area` (properties) — and `.manifold` escape hatch for everything else |
| Casing convention | **camelCase** (matches `cadquery.Shape.fuse`) | **snake_case** (matches build123d's `Part.fuse`) |
| Bounding box | `BoundingBox()` → 6-tuple | `bounding_box()` → `build123d.BoundBox` |
| Selector API | **none** (P3 — designed, not built) | `MeshPart.faces()`, `.analytic_faces()`, `.faces_from(source)`, `.feature_edges()` |
| Fillet/chamfer | **none** (designed only as "P3 — explicit-reject in mesh mode") | `MeshPart.fillet(...)`, `MeshPart.chamfer(...)` (A3a + A3b + A3c + A4) |
| Optional extra | `pip install 'cadquery[manifold]'` | `pip install 'build123d[manifold]'` |
| Public-surface assertion | `tests/test_manifold.py::test_namespace_public_surface` (lists 12 names) | `__all__` in `src/build123d/mesh/__init__.py` (29 names) |

The size asymmetry — 29 public names vs 12, 6 901 LOC vs 1 267 — is the
single best one-glance illustration of the scope divergence in §5–§7.

---

## 3. Independent corroboration — three convergences

These three architectural picks were arrived at **independently** by the two
efforts; the fact that two parallel teams on two sibling libraries chose the
same thing each time is high-confidence design signal.

### 3.1 The value type is NOT a `Shape` subclass

Both backends define a new value type that wraps a `manifold3d.Manifold` and
is sibling to the native B-rep shape, never a subclass. Both modules
*explicitly state the reason in the docstring*: the host library's `Shape`
hierarchy hardwires `wrapped: TopoDS_Shape`, and a `manifold3d.Manifold`
cannot satisfy that invariant.

* CadQuery — `cadquery/manifold/_meshshape.py:22–30`: *"It is deliberately
  NOT a Shape subclass. Shape.__init__ hardwires self.wrapped = downcast(obj)
  to TopoDS types; a value whose backing geometry is a manifold3d.Manifold
  cannot satisfy that invariant. … This is by design: a MeshShape only
  exists because the user explicitly called a cadquery.manifold function, so
  the kernel choice is always explicit and never a silent swap."*
* build123d — `src/build123d/mesh/mesh_part.py:90–95`: *"A sibling of
  build123d's Part — not a Shape subclass. … crossing the mesh↔BREP
  boundary is therefore always an explicit, named verb"*.

Both designs reach the same conclusion: every mesh↔BREP crossing is an
explicit, named verb (`toShape`/`toWorkplane` vs `to_solid`/`to_part`); no
silent kernel swap is ever possible.

### 3.2 The BRep↔mesh bridge is OCP-only

Both implementations carve the BRep↔mesh kernel work into a single module
that imports only OCP, numpy, and manifold3d — *no host-library ergonomics*.

* CadQuery — `cadquery/occ_impl/manifold_bridge.py:14–22`: *"This module is
  the internal seam between OCCT B-Rep geometry and manifold3d triangle
  meshes. It is deliberately kept library-agnostic: it imports only OCP …
  numpy and manifold3d — never cadquery.Workplane, cadquery.Shape or any
  other CadQuery ergonomic type. That keeps it portable should it ever be
  extracted into a standalone ocp-manifold package."*
* build123d — `src/build123d/mesh/bridge.py:78–82` imports only
  `OCP.BRep.BRep_Tool`, `OCP.BRepAdaptor.BRepAdaptor_Surface`,
  `OCP.GeomAbs.GeomAbs_SurfaceType` from OCP, plus `build123d.topology.Face,
  Shape` for type annotation only. The pipeline is identical in shape: tessellate
  per `TopoDS_Face`, weld vertex soup by grid-snap, build `m3d.Mesh*`.

Both efforts also explicitly anticipate the eventual standalone
`ocp-manifold` package — and **both decline to extract it now**, on the same
"rule of three" argument: extract only after a second library actually needs
it (see CadQuery design doc 20 §9, build123d design doc
`design-manifold-in-build123d.md` §10). Two sibling libraries reaching the
same packaging hedge from opposite directions is the strongest possible
signal that this is the right call.

### 3.3 Opt-in `[manifold]` extra; lazy import; namespaced module

Both packages ship as `<host>[manifold]` extras, both lazy-import
`manifold3d` at first use, both expose a feature-detect entry point, and
both refuse to silently import-fail.

| Question | CadQuery | build123d |
|---|---|---|
| Install command | `pip install 'cadquery[manifold]'` | `pip install 'build123d[manifold]'` |
| Where is the lazy import? | `cadquery/occ_impl/manifold_bridge.py::require_manifold3d` | `src/build123d/mesh/__init__.py::is_available` + a top-level guarded `import manifold3d` |
| What does the extra entry point look like? | `cqm.has_manifold3d()` → bool | `build123d.mesh.is_available()` → bool |
| What happens on missing extra? | `import cadquery.manifold` raises `ImportError` with `pip install 'cadquery[manifold]'` hint | `import build123d.mesh` raises `ImportError` with `pip install 'build123d[manifold]'` hint |
| Manifold3d version pin | `manifold3d>=3.4,<4` (referenced in the bridge error string) | `manifold3d>=3.4,<4` (`pyproject.toml`) |

Same shape, same version pin, same fail-loudly philosophy.

### 3.4 Other smaller convergences

* **Both ship native `minkowski` and 3D `hull`** (OCCT has neither). Both
  expose `minkowski_sum` and `minkowski_difference` with explicit separate
  names rather than a sign convention.
* **Both default to a faceted Tier-1 bake on mesh→BREP** — direct
  `BRep_Builder` shell assembly, ~17–20k triangles/second, linear scaling
  (the result that broke the old "148 s wall"). CadQuery's
  `manifold_to_topods` (`occ_impl/manifold_bridge.py:307`) and build123d's
  `Solid.from_mesh` (in `src/build123d/topology/three_d.py`, called from
  `MeshPart.to_solid` line 693) are nearly identical in algorithm: each
  vertex/edge built exactly once, planar `BRepBuilderAPI_MakeFace` per
  triangle, then `ShapeFix_Solid`. build123d ported CadQuery's `p2`
  prototype verbatim (the CadQuery design doc explicitly notes this in
  exp-09 §4).
* **Both `weld_vertices` by rounded-grid `np.unique`** with `decimals=6`. Same
  function name, same algorithm, same constant — independent rediscovery.

---

## 4. Divergences — host-library idioms

Two principled divergences are direct consequences of each host library's
own conventions and are correct on both sides.

### 4.1 camelCase vs snake_case

| | CadQuery | build123d |
|---|---|---|
| Method casing | `MeshShape.makeBox`, `numTriangles`, `BoundingBox`, `toShape` | `MeshPart.box` (classmethod), `to_arrays`, `bounding_box`, `to_solid` |
| Free-func casing | `cqm.minkowskiDifference` (commit `c6110fc6` renamed from `minkowski_difference` to align with `cqm.fuse` / `cqm.cut`) | `mesh_minkowski_difference` |
| Reasoning | Matches `cadquery.Shape.fuse`, `Workplane.makeBox`, etc. | Matches `Part.fuse`, build123d's PEP-8 |

Each is wrong on the wrong side. CadQuery making the late rename of
`minkowski_difference → minkowskiDifference` in commit `c6110fc6` is the
clearest signal that the casing call was deliberate.

### 4.2 Intersection operator: `*` vs `&`

| | CadQuery | build123d |
|---|---|---|
| Intersect operator | `a * b` | `a & b` |
| Other ops | `a + b` (fuse), `a - b` (cut) | `a + b` (fuse), `a - b` (cut) |
| Reasoning | Mirrors `cadquery.Shape.__mul__` (`_meshshape.py:362`) | Mirrors build123d `Part.__and__` |

Both are correct, both diverge for the same reason. (The free-function
forms `cqm.intersect(...)` and `mesh_intersect(...)` are identical in
spirit.)

### 4.3 Boolean N-ary handling

Both libraries accept an N-ary boolean. The implementation strategy diverges
slightly:

* CadQuery folds it in Python — `cqm.fuse(a, *rest)` calls
  `batch_boolean([all_operands], OpType.Add)` once for the union, but
  `cqm.cut` and `cqm.intersect` fold pairwise in a Python loop
  (`_meshshape.py:164`, `:181`).
* build123d resolves *every* N-ary in a single `batch_boolean` pass —
  `mesh_fuse` / `mesh_cut` / `mesh_intersect` all use `batch_boolean` with
  `OpType.Add` / `Subtract` / `Intersect` (`mesh_part.py:1197`, `:1223`,
  `:1251`). For a 60-cut sequence the build123d path saves 59 Python-level
  manifold boolean calls.

Minor; both are fast; the build123d form is slightly more uniform.

### 4.4 STL writer

Both ship a dependency-free STL writer that emits straight from the manifold
arrays (no BREP detour). CadQuery has ASCII only (`_meshshape.py:441`).
build123d has both binary and ASCII (`mesh_part.py:1030`).

---

## 5. The major divergence — exact analytic B-rep recovery

This is the largest engineering gap between the two shipped backends and the
single most important entry in this document.

### 5.1 What CadQuery shipped

CadQuery's mesh→BREP is the **Tier-1 faceted bake only**. `MeshShape.toShape`
(`_meshshape.py:254`) calls `manifold_to_topods` which runs the direct
`BRep_Builder` shell assembly: one planar `TopoDS_Face` per triangle, then
optional `ShapeUpgrade_UnifySameDomain` to merge genuinely-coplanar facets
back into single faces.

This is the algorithm that broke the "148 s wall" — linear at ~17–20k
tri/s, always-valid, no super-linear sewing. But the result is **all-planar
facets**: every curved input becomes a polygon mesh, every analytic face
becomes a faceted patch. The docstring is brutally honest about it
(`_meshshape.py:262–268`): *"This is slow and lossy. The result is an
all-planar-facet solid: curvature and analytic faces are gone, so fillet,
chamfer, shell and type selectors (`%CYLINDER`) will not work on it."*

A consequence: even a planar input — say a cube — round-trips as a faceted
cube. `UnifySameDomain` does merge truly-coplanar facets back together
(12 → 6 faces for a cube), but it has no access to the input's *exact*
plane parameters, so the recovered cube's faces lie on planes refitted from
the welded triangles — within `Precision::Confusion` but not bit-exact, and
post-CSG with mixed planar/curved geometry it produces a faceted solid that
the user cannot fillet.

CadQuery's *design* doc (20-…md §3.2 Tier 2, §5.1 "p4 selector
reconstruction") explicitly defers analytic-primitive recovery to Phase 3
behind its own `sklearn`-bearing extra. The `p4-face-identity` prototype
(`cadquery/ddocs/prototypes/p4-face-identity/`) probed the
`manifold3d.run_index` / `run_original_id` / `face_id` / `as_original` /
`reserve_ids` / `set_properties` mechanisms and proved provenance can ride
along through a chained boolean (`probe 6`: *"Identity fully survives.
(A-B)-D keeps all three input ids and yields 12 distinct (origin, face_id)
pairs for 12 logical faces"*). But the prototype lives in `ddocs/prototypes/`;
none of its machinery has been ported into the shipped backend.

The CadQuery design also explicitly anticipates an upstream "TKManifold"
OCCT shim — see `20-manifold-integration-design.md` §3.2 — but the doc
treats that as future work, not as Phase 1.

### 5.2 What build123d shipped

build123d shipped **systematic faceID seeding from the OUT leg, all the way
through every CSG operation, with exact analytic planar B-rep recovery on
the IN leg.** Three load-bearing pieces:

**(a) Per-face seeding at OUT.** `shape_to_manifold`
(`src/build123d/mesh/bridge.py:628 — algorithm B.9`) tessellates the input
shape *per `TopoDS_Face`*, allocates one globally-unique `face_id` from a
process-wide counter (`bridge.py:99`), stamps it on every triangle of that
face, and records the originating `TopoDS_Face`'s exact `Geom_Surface`
(plus, if planar, the exact `gp_Pln` origin + normal) in a `SideMap`
(`bridge.py:504 — A.7`). The seeded `face_id` array is passed via
`manifold3d.Mesh64(..., face_id=...)`: the bridge's docstring records the
nanobind rank-not-dtype gotcha that took prototype iteration to find
(`bridge.py:42–48`).

**(b) FaceID *maintenance* through booleans.** manifold3d 3.4.x
maintains the seeded `face_id` array through a boolean — this is the
critical 3.4.x behaviour the prototype `p9_faceid_brep_recovery` confirmed.
After every batch_boolean the output triangles each carry the seeded id of
whichever input face their patch traces back to (boolean-*created* cut
faces inherit the id of the tool face that carved them — `p8_face_identity`
probe 2). build123d's `_merge_side_maps` (`mesh_part.py:1130`) is therefore
a collision-free dict update across operand `SideMap`s.

**(c) Exact analytic B-rep recovery at IN.** `recover_brep`
(`recovery.py:1073 — algorithm C.17`) groups the output triangles by
`face_id`, splits each group into edge-connected components (a single
seeded id can carry a face the boolean cut into two disjoint pieces —
`recovery.py:91`), extracts boundary loops by "edges used by exactly one
triangle of the component" (`recovery.py:147`), **projects the boundary
vertices orthogonally onto the input's exact `Geom_Plane`** (the same plane
parameters the SideMap carried from OUT — `recovery.py:203`), and builds
one **exact analytic** `TopoDS_Face` on that `Geom_Plane`. For a curved
seeded id the recovery falls back to a one-face-per-triangle faceted patch
(`recovery.py:1033 — C.16`); the seeded id still *names* the surface so
provenance survives.

The net behaviour:

| Input | What `toShape` / `to_solid` returns |
|---|---|
| Cube → 0 booleans | CadQuery: faceted 12-face shell unified to 6 ε-jittered planar faces; build123d: a 6-face `Solid` on the **exact input planes**, bit-exact volume |
| Cube `cut` cube | CadQuery: faceted result, ~24 planar facets after `unify` — not filletable; build123d: an exact 10-face analytic `Solid`, filletable with real `BRepFilletAPI` |
| Cylinder → 0 booleans | CadQuery: dozens of planar facets approximating the lateral surface, plus two true planar caps after `unify`; build123d: two **exact** planar caps + one faceted patch for the cylindrical face (P3-honest — selectors that need analytic curves raise) |
| Cylinder `cut` box | CadQuery: faceted; build123d: exact planar faces for every box-derived face + cut walls, faceted patch for the cylinder face |
| Subsequent `Solid.fillet([…edges…], r)` on result | CadQuery: not exposed (Tier-1 result is faceted; `BRepFilletAPI` would fillet every facet edge); build123d: real `BRepFilletAPI_MakeFillet` runs on the recovered analytic edges, producing real analytic blend surfaces |

The build123d `MeshPart.faces()` / `.analytic_faces()` / `.faces_from(source)`
selectors are the user-facing payoff. `faces()` returns recovered
build123d `Face`s in a real `ShapeList` (so `sort_by`, `filter_by`,
`group_by` work). `analytic_faces()` *raises* if any curved patch is
present (the P3-honest "never silently wrong" contract).
`faces_from(source)` filters by the input shape *name* recorded in the
SideMap — a *new* selector dimension that has no BREP analogue (because a
boolean-created cut face inherits the id and the source name of the input
face it was cut from).

CadQuery has zero of this. CadQuery's design doc 20 §5.2 / §11 lists *all
of it* as Phase 3 — explicitly deferred, prototyped in `p4-face-identity`,
not built in the backend.

### 5.3 Quantified delta

* Public API surface: build123d's recovery adds `MeshPart.faces`,
  `analytic_faces`, `faces_from`, `feature_edges`, `to_solid(reconstruct=)`,
  `recover_brep`, plus the `FaceRecord` / `SideMap` / `ResultMesh` /
  `RecoveredFace` / `RecoveryResult` dataclasses — **8 of the 29 public
  names** in `build123d.mesh.__all__` exist solely because of the recovery
  pipeline.
* LOC: `bridge.py` 512 + `recovery.py` 548 = 1 060 LOC for the seeded
  path. CadQuery's `manifold_bridge.py` 468 LOC covers both directions
  *without* seeding or recovery.
* Test coverage: build123d's `tests/test_mesh.py` has 156 test functions,
  the majority exercising the seeded / recovered path. CadQuery's
  `tests/test_manifold.py` has 34, none touching seeding.
* Behavioural: a cube fed through OUT → 0 booleans → IN comes back as a
  bit-exact 6-face analytic `Solid` in build123d; in CadQuery it comes back
  as a unified-coplanar 6-face solid whose face planes were *re-fitted* from
  the welded triangle mesh (within `Precision::Confusion` but not bit-exact).
* User-facing: build123d offers `MeshPart.fillet(...)` (mesh-side, A3b) **and**
  `MeshPart.to_solid().fillet(...)` (BREP-side via the recovered analytic
  faces). CadQuery offers neither.

---

## 6. The mesh fillet/chamfer stack — entirely build123d-only

build123d shipped a full mesh-side fillet/chamfer engine that CadQuery has
not even designed against. The Phase A delivery is four sub-phases:

### 6.1 A3a — selective chamfer of flat CSG (`fillet.py`, commit `8120c7ac`)

Native faceted chamfer of one or more **chains** of feature edges. The
public surface is `MeshPart.chamfer(edges, size, on_infeasible='raise')`
and the free function `mesh_chamfer`. The selection unit is a
`FeatureChainSelection` from `MeshPart.feature_edges()`; the chain graph
itself comes from `feature_edges.build_feature_graph` over the seeded
`face_id` array. Construction: one **swept tool per chain** (design
`mesh-fillet-engineering.md` §3) — the wedge profile lofted around the
chain by per-vertex frames, the lofts unioned by `batch_boolean(Add)`, then
subtracted from the body (convex chain) or added (concave chain).

This is the algorithmic keystone. The faceID feature-edge graph is the
single piece of machinery that makes selective mesh chamfer feasible at
all. **CadQuery cannot implement this without first building the faceID
seeding + bridge equivalent — §5 is the prerequisite.**

### 6.2 A3b — single-chain mesh fillet with per-edge sign splitting (`c6816319`)

`MeshPart.fillet(edges, radius, segments=8, on_infeasible='raise')`. The
cross-section is "wedge minus rolling ball": the same right-triangle
wedge as the chamfer with a quarter-disc of radius `r` subtracted; built
as two convex profile lofts (wedge and ball) whose mesh difference
recovers the non-convex profile exactly (design `algorithms.md` H.38).

A3b also handles mixed convex/concave chains by splitting each chain into
single-sign sub-runs at every sign flip (`_split_chain_by_sign`, H.40),
batching all convex sub-runs into one cut, all concave sub-runs into one
add, and ordering "convex-cut-then-concave-add" (the p10 fix #3 carried
through).

### 6.3 A3c — multi-chain corner blends with ACIS setback (`ebff4842`)

Where ≥3 chains meet at a vertex (e.g., the corner of a box), a per-chain
swept-tool approach leaves an unblended pyramid corner. A3c detects every
multi-chain corner (`corners.py::detect_corners`, I.42), computes a
**setback** `s = min(size, 0.45·L)` (the ACIS convention,
`compute_setback`, I.43), shortens each incident chain by negating its
endpoint overshoot, and patches the resulting hole with a spherical patch
(`build_corner_fillet_patch`, I.45) or generalized pyramid hull
(`build_corner_chamfer_patch`, I.46). The end result is geometrically
correct blends at every box corner.

### 6.4 A4 — variable radius + skip mode + `FilletReport` (`eb4913ba`)

`radius` and `size` can be a callable `(chain, vertex_index) -> float` —
evaluated at every chain vertex; the per-vertex frames' profiles then
carry that vertex's radius and the ribbon loft interpolates between
adjacent rings (J.49, J.50). For a scalar input the result is
bit-identical to A3b's scalar path (explicit regression test).

The other A4 axis is `on_infeasible='skip'`: over-size requests / mixed
corners / `k > 6` corners that would normally raise `MeshFilletInfeasible`
are instead dropped from the operation, with the drop list returned on the
result's `last_fillet_report` (`MeshPart.last_fillet_report`, J.51, J.55).
A skip is logged at WARNING level so it is never completely silent — the P3
"never silently mis-answer" contract.

### 6.5 What CadQuery has

Nothing. CadQuery's design doc 20 §5.2 says fillet-after-mesh-boolean must
**explicitly reject**, not implement: *"No fillet / chamfer after a mesh
boolean. They need an analytic edge and a blend surface. … This is the one
outcome the design forbids absolutely."*

The CadQuery design's framing — *fillet on a mesh result is wrong*, refuse
loudly — is internally consistent and correct on a Tier-1-only backend.
build123d's framing — *fillet on a mesh result is feasible if we accept a
faceted result, and the user wants it for OpenSCAD parity* — is also
correct given the OpenSCAD context (OpenSCAD does not have analytic
fillet either) and the user-facing payoff. The two stances are not in
conflict; they are *different products*.

The blocker for CadQuery to ship anything like A3 is not the fillet algebra
itself — it is the **faceID feature-edge graph** that the seeded `face_id`
array enables. Without §5, A3 is not buildable.

---

## 7. Approximation-mode cheap ops

Both efforts ship native `hull` and `minkowski` (which OCCT does not have
at all). build123d additionally ships native `mesh_extrude`, `mesh_revolve`,
`MeshPart.offset`, `MeshPart.shell` — the cheap *approximation-mode* ops
that scad2py + the OpenSCAD ecosystem need.

| Op | CadQuery | build123d |
|---|---|---|
| `hull` | `cqm.hull(*ops)`, `cqm.hullPoints(arr)` | `mesh_hull(self, *others)` (no public `hullPoints` — wrapped inside) |
| `minkowski` (sum) | `cqm.minkowski(a, b)` — native only | `mesh_minkowski(a, b, method='native'|'decompose')` — two backends |
| `minkowski_difference` | `cqm.minkowskiDifference(a, b)` | `mesh_minkowski_difference(a, b)` |
| 3D `offset` | not shipped | `MeshPart.offset(amount, sphere_segments=)` — Minkowski with a sphere tool |
| 3D `shell` | not shipped | `MeshPart.shell(thickness)` — `self - self.offset(-thickness)` |
| `extrude` of mesh profile | not shipped | `mesh_extrude(profile, height, ...)` |
| `revolve` of mesh profile | not shipped | `mesh_revolve(profile, ...)` |

build123d's `mesh_minkowski` exposes two backends: `'native'` (default,
manifold3d 3.4.x native, correct for convex + non-convex), and
`'decompose'` (the scad2py-ported convex-pairs algorithm — Apache-2.0,
ochafik — `ops.py:28`). The decompose backend is fully self-contained: for
convex operands it is exact; for non-convex it raises a clear error
pointing at `method='native'`. CadQuery has only `'native'`.

These extras (extrude, revolve, offset, shell) are the bricks scad2py's
Goal-2 backend needs. CadQuery's `cadquery/sketch/` module has BREP-side
extrude/revolve, which is fine for analytic profiles — but for `polygon`
or `polyhedron` from OpenSCAD, a mesh-side `mesh_extrude` is what the
scad2py renderer wants. CadQuery has not yet added these to its backend.
(Design doc 20 §3.1 "Phase 2" lists `extrude`/`revolve` of polygonal
profiles as Phase 2 deferred scope.)

---

## 8. scad2py wiring

Both efforts wrote a design doc for the scad2py × host integration and both
have a prototype renderer; neither has yet shipped a public, installable
scad2py-on-host package.

| | CadQuery | build123d |
|---|---|---|
| Design doc | `ddocs/design/21-scad2py-cadquery-backend.md` | `ddocs/design/design-scad2py-build123d.md` |
| Renderer prototype | `ddocs/prototypes/p3-scad2py-cq-renderer/` (~450 LOC, 7/8 examples render, 6 bit-exact) | `ddocs/prototypes/p4_scad2py_to_b3d/build123d_renderer.py` |
| Source-codegen prototype | none | `ddocs/prototypes/p5_openscad_to_b3d_source/scad2b3d.py` |
| Clean-room math prototype | `ddocs/prototypes/p6-clean-room-math/` (3 308-case PASS) | `ddocs/prototypes/p6_clean_room_scad2py_math/` (parallel) |
| Status on the host branch | **not shipped** — `cadquery.scad2py` etc. does not exist | **not shipped** — `build123d.scad2py` etc. does not exist; both prototypes live under `ddocs/prototypes/` |

Both designs route mesh-shaped subtrees (`hull`, `minkowski`, `polyhedron`,
`projection(cut=false)`) to the host's mesh backend and BREP-shaped subtrees
(analytic primitives, `revolve`, `text`, `offset` of analytic profiles) to
the host's native kernel. The hybrid router is the central architectural
decision in both designs.

The build123d side has one capability the CadQuery side does not: the
mesh-side fillet/chamfer of §6 lets `minkowski(box, sphere)` style
"rounded-box" patterns from OpenSCAD lower to a *real* mesh fillet rather
than a Minkowski sum, which costs much less. But this is a future
optimization the b3d backend wiring has not yet shipped either.

---

## 9. GPL remediation

Both efforts independently identified the same two files in upstream
`scad2py` as GPL-2.0+ ports of OpenSCAD C++ (`calc.py`, `colors.py`), both
independently planned a clean-room rewrite from public specs, both built a
prototype, and both proved 3 308 cases bit-exact against the GPL originals
(within `1.4e-17` FP noise).

* CadQuery — `ddocs/design/22-scad2py-gpl-remediation.md` +
  `ddocs/prototypes/p6-clean-room-math/`.
* build123d — `ddocs/design/scad2py-gpl-remediation.md` +
  `ddocs/prototypes/p6_clean_room_scad2py_math/`.

The remediation has since been **carried out by the owner upstream on
scad2py's `apache-relicense` branch** — both efforts converged on the same
solution and the owner has executed it. The GPL contamination is therefore
no longer a blocker for either side; both projects now consume the
clean-room-relicensed scad2py.

---

## 10. What each effort should borrow now

### 10.1 build123d → CadQuery

**This is the big one.** CadQuery should adopt the systematic faceID seeding
+ `recover_brep` pipeline as the next phase of its backend. The exact
substrate already exists in CadQuery's own corpus:

1. **`p4-face-identity` is the design proof.** The prototype shows
   `run_original_id` (and the composite `(run_original_id, face_id)` key)
   survives chained booleans, attributes cut faces, and gives provenance for
   selectors. 12 passing tests.
2. **The OCCT-side substrate is the OUT bridge they already have** —
   `cadquery/occ_impl/manifold_bridge.py::_fresh_tessellation` is the same
   per-face explorer build123d uses; it just doesn't *seed* a `face_id`
   array.
3. **The IN-side substrate is build123d's `recover_brep`** — and it is
   already library-agnostic in spirit (the algorithm is pure
   numpy/OCP, no build123d-specific types except the final
   `Solid`/`Compound` wrapping). A port is mechanical: replace
   `build123d.topology.{Solid, Compound, Face}` with
   `cadquery.Shape.cast(TopoDS_…)` at the leaves.

Concrete actionables for CadQuery:

* **Seed `face_id` in `shape_to_manifold`.** Add a per-`TopoDS_Face`
  globally-unique id (`itertools.count()`), pass it as
  `m3d.Mesh64.face_id`. Carry a side-map dict alongside the `MeshShape`.
* **Add `MeshShape.toShape(reconstruct=True)` mode.** When the mesh carries
  a side-map, group by `face_id`, recover exact analytic planar faces on
  the known `Geom_Plane`, fall back to faceted patches for curved ids.
  This is exactly `recover_brep`.
* **Wire selectors.** `MeshShape.faces()` returning a `Workplane`-flavoured
  selection that supports the planar directional selectors. The
  `from:<source>` provenance selector is a *new capability* over BREP.
* **`hullPoints` already exists** on CadQuery and **does not exist on
  build123d** — that's a small (~5 LOC) thing build123d could borrow back.

The CadQuery `p4-face-identity` README explicitly mentions OCCT's
`TKManifold` module as a possible future substrate, but the design doc 20
treats that as out of scope for now. The faceID seeding path is *all
implementable on the existing manifold3d 3.4.x bindings*, no OCCT change
needed.

### 10.2 CadQuery → build123d

There is **nothing critical** to borrow from CadQuery's shipped backend
right now. build123d's backend is a strict superset of CadQuery's in
capability terms, and both backends share the same algorithm for every
overlapping op (vertex weld, Tier-1 faceted bake, etc.).

The two smaller items that could be cross-pollinated:

1. **`cqm.hullPoints(arr)` as a public free function.** build123d has the
   implementation inside `mesh_hull` but doesn't expose the `(N, 3)` array
   form as a public free function. Easy 5-LOC addition.
2. **`MeshShape.exportStl` is ASCII-only**; build123d has both binary and
   ASCII. Not a borrow direction — but if CadQuery later adds binary STL,
   build123d's `_write_binary_stl` (`mesh_part.py:1278`) is the same code
   pattern.
3. **CadQuery's `cqm.toBrep(unify=True)` argument** is exposed at the free
   function. build123d's `MeshPart.to_solid(unify_coplanar=False)` defaults
   to `False`; the choice is reasonable on either side and they could be
   aligned, but there is no correctness gap.

### 10.3 Future shared substrate

Both efforts anticipate the eventual standalone `ocp-manifold` package and
both deferred extraction on the "rule of three" argument. Now that
build123d's `recover_brep` is shipped and CadQuery is the obvious second
consumer, **this may now be the right time to extract it**. Both backends
import only `OCP` / `numpy` / `manifold3d` in the bridge layer; the
recovery layer additionally needs `OCP.Geom` / `OCP.ShapeFix` but is still
host-agnostic. The host-specific 40–50% (CadQuery's `Workplane` fluent
chain vs build123d's algebra `+`/`-`/`&`) stays in each library.

---

## 11. Architectural insights confirmed by independent build

Independent congruent implementations on different host libraries strongly
validate the design picks. The picks that survived two parallel teams,
two host libraries, two API idioms, are:

1. **Value type, not Shape subclass; explicit named bridges.** Both teams
   discovered the `Shape.__init__(downcast(obj))` constraint independently
   and both routed around it the same way. This is the *correct* answer
   for any OCCT-via-OCP-based host library wanting a mesh sibling.
2. **OCP-only bridge module, deferred standalone package.** Both teams
   reached the same packaging hedge from opposite directions and ended
   with the same code shape: a kernel module that imports only OCP /
   numpy / manifold3d. The standalone `ocp-manifold` *will* be the right
   eventual factoring; both teams chose to defer extraction until the
   second consumer existed (which it now does).
3. **Opt-in `[manifold]` extra; clear error on missing.** Both teams
   reached this from the same constraint (manifold3d is large, optional,
   and not always installable on WASM). Both implementations use the same
   "lazy-import + actionable error" pattern.
4. **Native minkowski / 3D hull / `batch_boolean` for N-ary CSG.** Both
   teams identified the same three as the OCCT-impossible / OCCT-slow ops
   that motivate the integration. Both shipped them as named verbs, not
   as kernel-routing magic.
5. **Default-deflection ~0.1 mm, weld decimals=6.** Both teams arrived at
   the same numeric defaults from the same constraints (matches typical
   3-D printing tolerance, build123d's `TOLERANCE = 1e-6` weld grid).
6. **Tier-1 faceted bake via direct `BRep_Builder` shell assembly.** Both
   teams ported `p2` / `p7` independently, both reached ~17–20k tri/s
   linear scaling, both validated `BRepCheck` on the output. The
   148-second-wall is *over*.
7. **Mesh→BREP is the asymmetric, lossy direction; do feature work
   pre-mesh.** Both teams arrived at the same "stay-in-mesh as long as
   possible" discipline. Both expose this in user-facing warnings, not
   buried in design docs.

The picks that *diverged* are correct on both sides — casing, `*` vs `&`,
N-ary fold strategy — and tracked each host's existing convention rather
than the manifold integration itself.

The single pick where one side went further than the other is **faceID
seeding + exact B-rep recovery** (§5). This is not a divergence in
conclusions — both designs identified it as the right Phase-3 direction —
but a divergence in *how much was built*. build123d shipped it; CadQuery
prototyped it and deferred. The corollary of §6 — that mesh fillet/chamfer
is buildable *if* you have faceID seeding — is the second-order result
that justifies the engineering cost on build123d's side and that CadQuery
will rediscover when it picks up the P3 work.

---

## Appendix — files referenced

CadQuery (`~/github/cadquery`, branch `ddocs-exploration`, tip `c6110fc6`):

* `cadquery/manifold/__init__.py` — free-function namespace (323 LOC).
* `cadquery/manifold/_meshshape.py` — `MeshShape` value type (476 LOC).
* `cadquery/occ_impl/manifold_bridge.py` — OCP-only bridge (468 LOC).
* `tests/test_manifold.py` — 34 test functions (474 LOC).
* `cadquery/ddocs/00-executive-summary.md` — top-level synthesis.
* `cadquery/ddocs/design/20-manifold-integration-design.md` — Goal-1 design.
* `cadquery/ddocs/design/21-scad2py-cadquery-backend.md` — Goal-2 design.
* `cadquery/ddocs/design/22-scad2py-gpl-remediation.md` — GPL remediation.
* `cadquery/ddocs/explorations/09-build123d-cross-pollination.md` — their
  cross-doc (the other direction of this one).
* `cadquery/ddocs/prototypes/p4-face-identity/` — the prototype for §5.

build123d (`~/github/build123d`, branch `feat/manifold-mesh-backend`, tip
`d1638000`):

* `src/build123d/mesh/__init__.py` — public surface (135 LOC).
* `src/build123d/mesh/mesh_part.py` — `MeshPart` value type + free
  functions (1 325 LOC).
* `src/build123d/mesh/bridge.py` — seeded OUT leg (512 LOC).
* `src/build123d/mesh/recovery.py` — exact-planar IN leg (548 LOC).
* `src/build123d/mesh/feature_edges.py` — chain graph from `face_id`
  (678 LOC).
* `src/build123d/mesh/fillet.py` — A3a chamfer + A3b fillet + A4 polish
  (2 311 LOC).
* `src/build123d/mesh/corners.py` — A3c multi-chain corner blends
  (550 LOC).
* `src/build123d/mesh/ops.py` — hull + minkowski + offset + shell
  (598 LOC).
* `src/build123d/mesh/sketch2d.py` — 2-D → 3-D extrude/revolve (244 LOC).
* `tests/test_mesh.py` — 156 test functions (2 349 LOC).
* `ddocs/design/algorithms.md` — 55 algorithm entries §A–§J.
* `ddocs/design/design-manifold-in-build123d.md` — Goal-1 design.
* `ddocs/design/design-scad2py-build123d.md` — Goal-2 design.
* `ddocs/design/mesh-fillet-engineering.md` — A3 design.
* `ddocs/design/scad2py-gpl-remediation.md` — GPL remediation.
* `ddocs/research/09-cadquery-ddocs-cross-comparison.md` — the
  *pre-implementation* version of this doc.
