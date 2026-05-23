# build123d manifold mesh backend — algorithms reference

A code-anchored reference for every non-trivial algorithm shipped on
`feat/manifold-mesh-backend`. Each entry names the originating module, the
problem it solves, the algorithm itself (and the prototype iteration that
motivated it where relevant), trade-offs, call sites and tests.

The deliverable touches three areas:

* **Core build123d additions** — minimal extensions to
  `src/build123d/topology/` that the mesh backend needs and that are useful
  beyond it (welded tessellation, mesh→BREP, NotImplemented-protocol for CSG).
* **Mesh CSG backend** — the `src/build123d/mesh/` sub-package: the OUT leg
  (`bridge.py`), IN leg (`recovery.py`), the value type (`mesh_part.py`), CSG
  free functions, geometry ops (`ops.py`), 2-D bridge (`sketch2d.py`).
* **Mesh fillet / chamfer** — feature-edge chain graph (`feature_edges.py`),
  per-chain swept tooling (`fillet.py`), multi-chain corner blending
  (`corners.py`).

> Prototype iterations are referenced as p1, p7, p8, p9, p10 (their full names
> are `ddocs/prototypes/p<N>_…`). The engineering design that drives the chain
> / corner machinery is `ddocs/design/mesh-fillet-engineering.md`.

---

## Table of contents

### A. Core additions to build123d (`src/build123d/topology/`)

1. `Shape.tessellate(weld=, relative=)` + `_weld_mesh` — grid-snap vertex weld
2. `Shape.mesh(relative=)` — relative vs absolute deflection
3. `Solid.from_mesh(vertices, triangles, fix=)` — direct shell assembly
4. `_connected_components(triangles, vertex_count)` — union-find body split
5. `_group_shells_into_solids(shells)` — bbox-nesting void classification
6. `Shape.__add__/__sub__/__and__` returning `NotImplemented` on unknown
   operand — the operator-protocol fix

### B. `bridge.py` — the OUT leg

7. `FaceRecord` / `SideMap` — provenance side-map; `_analyse_face`;
   `SideMap.merged()` / `.transformed()`
8. `_weld(vertices, triangles, decimals=6)` — np.unique on rounded vertices
9. `shape_to_manifold(shape, source=, ...)` — per-face tessellate + weld +
   seeded `Mesh64.face_id`
10. `read_result(manifold)` — extract `vertices`/`triangles`/`face_id`;
    `ResultMesh.distinct_ids` / `.triangles_of(fid)`

### C. `recovery.py` — the IN leg (exact reconstruction)

11. `_connected_components(triangles)` — edge-connectivity BFS (recovery's,
    distinct from three_d's vertex-based one)
12. `_boundary_loops(triangles)` — one-use edges → ordered loops
13. `_project_to_plane(point, origin, normal)` — orthogonal projection
14. `_exact_plane(record)` — `Geom_Plane` from the side-map's exact params
15. `_recover_planar_face(result, face_id, record)` — exact `TopoDS_Face` on
    known plane; per-id connected-component split (p8 fix)
16. `_faceted_patch(result, face_id)` — curved-id fallback
17. `recover_brep(result, side_map)` — group → split → recover → sew →
    shells → void-classify → `Solid` / `Compound`

### D. `mesh_part.py`

18. `MeshPart` value type — invariant; why not a `Shape` subclass
19. `from_part`, `from_mesh`; primitive constructors
20. CSG operators `+ - &` + reflected forms; coercion via core
    `NotImplemented`
21. Transforms `translate/rotate/scale/move(Location)` —
    `SideMap.transformed`
22. `to_solid(reconstruct=, unify_coplanar=)` — `recover_brep` vs
    `Solid.from_mesh`
23. `faces()` / `analytic_faces()` / `faces_from(source)` — P3 contract
24. Direct STL writer
25. `offset(amount)` — outward Minkowski-sphere; inward Minkowski-difference
26. `shell(thickness)` — `self - self.offset(-thickness)`

### E. `ops.py`

27. `mesh_hull` — native quickhull
28. `mesh_minkowski(a, b, method=)` — native and convex-pairs decomposition;
    `_is_convex`; `_CONVEXITY_REL_TOLERANCE`
29. `mesh_minkowski_difference` — fragility on faceted near-convex tools

### F. `sketch2d.py`

30. build123d Sketch/Face → manifold3d CrossSection
31. `mesh_extrude`; `mesh_revolve`

### G. `feature_edges.py`

32. `FeatureEdge` / `FeatureChain` / `FeatureChainSelection`
33. `extract_feature_edges` — faceID-boundary edge set
34. `build_chains` — `(lo, hi)` pair + connectivity ordering
35. Convexity classification; vertex-kind classification

### H. `fillet.py`

36. `_vertex_frames` — local-bisector tangent + into-solid axes
37. A3a chamfer wedge profile and prism-loft swept tool
38. A3b fillet "wedge minus rolling-ball" profile
39. `_ribbon_mesh_from_rings` — direct-import ribbon loft
40. `_split_chain_by_sign` — sub-runs; convex-cut-then-concave-add ordering
41. Feasibility pre-flight; `MeshFilletInfeasible`

### I. `corners.py` — A3c

42. `detect_corners` — vertices with ≥3 distinct chain pairs
43. `compute_setback` — `s = min(size, 0.45·L)`
44. `per_chain_setback` — applies as negative endpoint overshoots
45. `build_corner_fillet_patch` — sphere at `c = v ± r · unit(−Σn_f)`
46. `build_corner_chamfer_patch` — generalised pyramid via `hull_points`
47. `_check_corner_feasibility` — mixed/k>6 raise (P3)
48. `_drop_zero_volume_artifacts` — precision-pinch post-pass

---

## A. Core additions to build123d

These six items live in `src/build123d/topology/` and are kept deliberately
small: the mesh backend is an optional extra (`pip install
'build123d[manifold]'`), so anything mandatory for it must not assume
`manifold3d` is installed. None of these additions imports `manifold3d`.

### A.1 `Shape.tessellate(weld=, relative=)` + `_weld_mesh`

**Code**: `topology/shape_core.py::Shape.tessellate` (line 2268),
`topology/shape_core.py::_weld_mesh` (line 3663).

**What it does.** Triangulates a `Shape` to a `(vertices, triangles)` pair,
optionally welding coincident vertices across face seams into a single indexed
mesh.

**Why.** OCCT's `BRepMesh_IncrementalMesh` triangulates each `TopoDS_Face`
independently; a vertex shared by N incident faces appears N times in the soup
and the mesh is topologically open across every seam. A box tessellates to 24
vertices, not 8. Consumers that demand a closed indexed mesh — `manifold3d`'s
`Mesh64` (rejects soup with `Error.NotManifold`), the 3MF exporter, any STL
consumer that re-derives normals — need a welded mesh. `Mesher` (3MF) had its
own bespoke dedup; the welded tessellation generalises that into the base
class so every downstream consumer can opt in with a kwarg. p1 demonstrated
the round-trip; p7 needed weld for the `manifold3d` import.

**How.** `tessellate` triggers `mesh(tolerance, angular_tolerance,
relative=relative)`, then walks the `TopoDS_Face` triangulations exactly as
before (the un-welded fast path is the default — backward compatible). When
`weld=True`, the assembled per-face soup is handed to `_weld_mesh` (called at
line 2342):

```python
snapped = np.round(vertex_array, decimals)
unique_snapped, inverse = np.unique(snapped, axis=0, return_inverse=True)
inverse = inverse.reshape(-1)            # numpy>=2 may return (N,1)
welded = np.zeros((len(unique_snapped), 3), dtype=np.float64)
counts = np.zeros(len(unique_snapped), dtype=np.int64)
np.add.at(welded, inverse, vertex_array) # sum un-snapped coords per cluster
np.add.at(counts, inverse, 1)
welded /= counts[:, None]                # average — no grid bias
remapped = inverse[triangle_array]
keep = (corner_a != corner_b) & (corner_b != corner_c) & (corner_a != corner_c)
remapped = remapped[keep]                # drop sliver-collapsed triangles
```

Key points:

* **Grid-snap, average back.** The snap is to `decimals=6` decimal places —
  matching build123d's `TOLERANCE = 1e-6`. The cluster's *original* (un-snapped)
  coordinates are summed and averaged, so no grid bias enters the output.
* **Degenerate drop.** After re-indexing, triangles whose vertices collapse to
  the same index are dropped — slivers below TOLERANCE that snapped together
  legitimately disappear.
* **numpy>=2 compatibility.** The `.reshape(-1)` defends against
  `np.unique(..., return_inverse=True)` returning a column vector under numpy 2.

**Trade-offs / limitations.**

* O(N log N) (the `np.unique` sort) — keeps tessellation linear-ish overall.
* Grid resolution is fixed at TOLERANCE; weld decisions outside that scale are
  not supported by design.
* `weld=False` (default) is bit-for-bit unchanged from the legacy fast path.

**Where invoked from.** Read by every mesh-consuming caller indirectly through
`shape_to_manifold` and `MeshPart.from_part`; the `Mesher` (3MF) keeps its
existing path (it uses dict-based dedup). No call site in the main repo passes
`weld=True` directly — the mesh backend uses its own `_weld` (the array-only
sibling in `bridge.py`) because it has additional per-face provenance to keep
aligned.

**Tests.** `test_mesh.py::test_tessellate_weld_dedups_box_to_eight_vertices`
asserts the box→8 vertices contract; `…_default_is_unchanged` pins the
backward-compatible default.

**Prior art.** Standard mesh "merge by quantised position" technique (e.g.
trimesh's `merge_vertices`); the average-back trick is folklore in industrial
mesh processing.

### A.2 `Shape.mesh(relative=)` — relative vs absolute deflection

**Code**: `topology/shape_core.py::Shape.mesh` (line 1621).

**What it does.** Triggers an OCCT incremental tessellation if one is not
already cached on the shape, with a configurable relative/absolute deflection
toggle.

**Why.** build123d's exporters historically defaulted to *relative* deflection
(linear deflection scaled per edge by its length). That is right for "render
this shape at a uniform per-edge fidelity"; it is wrong for mesh CSG, where
multiple shapes of different sizes need to land on a *shared* coordinate grid
so the weld can unify their seams. `BRepMesh_IncrementalMesh`'s constructor
takes a `bool isRelative`; making it a public kwarg lets the mesh backend
choose absolute deflection without forking the exporters.

**How.** The five-arg `BRepMesh_IncrementalMesh(shape, tolerance, relative,
angular_tolerance, in_parallel=True)` is invoked when no triangulation is
present:

```python
if not BRepTools.Triangulation_s(self.wrapped, tolerance):
    BRepMesh_IncrementalMesh(
        self.wrapped, tolerance, relative, angular_tolerance, True
    )
```

**Trade-offs.** Default stays `relative=True` (no exporter change); the bridge
in `bridge.py` calls `face.tessellate(linear_tol, angular_tol)` per face
(default relative — but per face, so the relative-edge-length basis is the
face's own edges, which is fine for CSG within a face) and `mesh_part.from_part`
passes `linear_tolerance=0.1, angular_tolerance=0.2` (absolute units; see
`DEFAULT_LINEAR_TOLERANCE` in `bridge.py`).

**Where invoked from.** Implicitly via every `tessellate()` call in build123d.

**Tests.** Implicitly covered by every mesh test that builds a `MeshPart`.

### A.3 `Solid.from_mesh(vertices, triangles, fix=)`

**Code**: `topology/three_d.py::Solid.from_mesh` (line 1326).

**What it does.** Build a `Solid` (or a `Compound` for multi-body meshes)
directly from a closed indexed triangle mesh by assembling a `TopoDS_Shell` —
no spatial sewing.

**Why.** The classical mesh→BREP path is per-triangle face + `BRepSewing` to
re-discover topology. `BRepSewing` is super-linear (it spatially matches every
vertex against every other) and dominates the cost. p7 measured **~9× faster
than `_get_shape` of `Mesher`** at scale by exploiting one fact: an indexed
mesh **already encodes connectivity**. p9 added the
multi-body / void-classification layer to make this drop-in for the recovery
path.

**How.**

1. **Validate & drop degenerates.** Reject empty input; drop any triangle with
   a repeated index (OCCT's mesher emits a few near sphere poles).
2. **Lazy vertex builder.** One `TopoDS_Vertex` per unique input vertex, built
   on first reference; an array of `Optional[TopoDS_Vertex]` indexed by mesh
   vertex index.
3. **One edge per sorted vertex pair, shared & reversed.** A dict keyed by
   `(min(a,b), max(a,b))` stores the `TopoDS_Edge` built once; the *second*
   triangle that touches that edge gets it reversed via `Edge.Reversed()`. This
   is exactly the shared-topology rule OCCT's sewer is supposed to rediscover
   from spatial proximity — but we already know it from the index pair, so it
   costs O(1) per edge instead of O(N) of spatial search.
4. **One face per triangle into a single shell.** `BRepBuilderAPI_MakeWire`
   from the three shared edges, `BRepBuilderAPI_MakeFace`, added straight into
   a `TopoDS_Shell` via `BRep_Builder.Add`. After the loop the shell is marked
   closed.
5. **Connected components → shells.** `_connected_components` (see A.4) labels
   each triangle by its disjoint mesh body; one shell is built per component.
6. **Shells → solids with bbox nesting.** `_group_shells_into_solids` (see
   A.5) classifies each shell as a top-level body or as a void of an enclosing
   body; a `BRepBuilderAPI_MakeSolid` is built with the outer shell and any
   nested shells added as voids.
7. **Optional fix.** With `fix=False` (default for trusted manifold3d output)
   the assembly is returned as-is; `fix=True` runs `ShapeFix_Shell` /
   `ShapeFix_Solid` for untrusted input. ShapeFix is itself super-linear, so
   trusted input bypasses it.

The result is a `Solid` for a single body or a `Compound` of `Solid` for
disjoint bodies. The faces are *faceted* — one planar `TopoDS_Face` per
triangle — by design: this path is for "the mesh is the geometry"; analytic
recovery lives in `recovery.py`.

**Trade-offs / limitations.**

* Faceted — no analytic surface recovery. Recovery happens via
  `recover_brep` for seeded meshes; raw input has no provenance.
* Assumes oriented, closed indexed mesh (manifold3d's invariant). Junk input
  *will* produce an invalid solid; set `fix=True` to attempt a repair.
* O(triangles) — linear, dominated by OCCT face-construction calls.

**Where invoked from.** `MeshPart.to_solid` (the fallback when no side-map is
present, or when `reconstruct=False`); never called directly by anything else
in the repo.

**Tests.** `test_from_mesh_box_is_valid_with_correct_volume`,
`…sphere…`, `…multi_body_compound_yields_compound`,
`…solid_with_void_keeps_the_void`, `…rejects_empty_mesh`.

**Prior art.** Direct shell assembly from an indexed mesh — generalisation of
the `Mesher._get_shape` shape-from-3MF path (which used `BRepSewing`). The
edge-sharing trick mirrors the half-edge invariant standard in mesh
processing.

### A.4 `_connected_components(triangles, vertex_count)` — body splitting

**Code**: `topology/three_d.py::_connected_components` (line 2038).

**What it does.** Label each triangle with the index of its connected
component, where "connected" means "shares a vertex index".

**Why.** A mesh from a `Compound` of disjoint solids tessellates to one array
of triangles spanning all the bodies. A single `TopoDS_Shell` containing
unrelated faces is *invalid* (not a closed manifold); each body needs its own
shell. So `Solid.from_mesh` must partition the input before assembly.

Note this is the **vertex-based** connected-components — for a closed indexed
mesh, shared vertices identify the same body. The `recovery.py` connected
components routine is different: it works on **edge** connectivity within one
faceID group (a single face can be cut into two disjoint pieces by a boolean,
and the two pieces share no vertices, only edges with neighbouring faces).

**How.** A weighted union-find over the vertex set with path compression:

```python
parent = np.arange(vertex_count, dtype=np.int64)
def find(node):
    root = node
    while parent[root] != root: root = parent[root]
    while parent[node] != root:                # path compression
        parent[node], node = root, parent[node]
    return root
for triangle in triangles:
    first = find(int(triangle[0]))
    for other in (int(triangle[1]), int(triangle[2])):
        parent[find(other)] = first
roots = np.array([find(int(t[0])) for t in triangles], dtype=np.int64)
_, labels = np.unique(roots, return_inverse=True)
return labels.reshape(-1)
```

The result is `(M,)` contiguous 0-based labels (one per triangle).

**Trade-offs.** Nearly linear in M (inverse-Ackermann growth, dominated by
allocation). The implementation is plain Python around `np.unique` for the
final relabelling — fast enough; a fully-vectorised union-find is possible but
unnecessary at the scales we have seen.

**Where invoked from.** `Solid.from_mesh` once per call.

**Tests.** `test_from_mesh_multi_body_compound_yields_compound` validates
that the partition produces the correct number of bodies.

**Prior art.** Standard weighted union-find — Tarjan, with path compression.

### A.5 `_group_shells_into_solids(shells)` — bbox-nesting void classification

**Code**: `topology/three_d.py::_group_shells_into_solids` (line 2072).

**What it does.** Group reconstructed shells into `(outer, [voids])` tuples so
each top-level body gets its own `Solid` and any genuinely-nested shell
becomes an internal void of the smallest enclosing body.

**Why.** Sewing always yields one shell per *connected* component of faces,
and a boolean result can produce shells for two distinct reasons that must not
be conflated:

* **Disjoint bodies**: two pieces that don't touch — each is its own positive
  Solid.
* **A body with an internal void**: an inward-facing shell *inside* an outer
  shell — the cavity must register as a void of the enclosing solid, not as a
  separate positive Solid (or summing the two would *add* the cavity instead
  of carving it out).

`Mesher._get_shape` (the prior art) used the heuristic "the largest shell is
outer, all others are voids" — fine for a single body with cavities, **wrong**
for a multi-body input: two boxes drilled through a plate would build as one
plate-with-two-rectangular-voids that doesn't even live in the right space.
That misclassification is the R6 fix this routine addresses.

**How.** Bbox-nesting classification:

```python
boxes = [shell.bounding_box() for shell in shells]
def is_inside(inner, outer):
    eps = 1e-7
    return (outer.min.X - eps <= inner.min.X and ...)  # 6 inequalities
parent = [None] * len(shells)
for i in range(len(shells)):
    for j in range(len(shells)):
        if i == j: continue
        if is_inside(boxes[i], boxes[j]):
            current = parent[i]
            if current is None or box_volume(boxes[j]) < box_volume(boxes[current]):
                parent[i] = j        # keep the smallest enclosing shell
```

* `parent[i] is None` ⇒ shell `i` is a top-level body.
* otherwise shell `i` is a void of the smallest enclosing shell (one level of
  nesting — matches `Solid.from_mesh`'s contract: voids do not themselves
  contain shells).

**Trade-offs.** O(N²) where N is the shell count — fine because N is at most a
handful for any realistic CSG result. Strictly *bbox*-based: a non-convex
outer body with a void *outside* its bbox of another body could
theoretically defeat this, but such a configuration cannot arise from a
2-manifold input (the void must be inside the outer solid's volume, hence
inside its bbox).

**Where invoked from.** Both `Solid.from_mesh` and `recovery.recover_brep` —
the same algorithm wraps a faceted bake and an analytic recovery, so they
agree about what is a void.

**Tests.** `test_from_mesh_solid_with_void_keeps_the_void` confirms one body
with a cavity; `test_reconstruction_keeps_an_internal_void` exercises it via
the recovery path; `test_reconstruction_disjoint_bodies_yield_a_compound`
exercises the multi-body branch.

**Prior art.** STL/triangle mesh "containment-by-bbox" is a standard heuristic
in the 3MF / stl-to-brep literature (e.g. lib3mf's
classification).

### A.6 `Shape.__add__/__sub__/__and__` returning `NotImplemented`

**Code**: `topology/shape_core.py::Shape.__add__` (line 963),
`Shape.__sub__` (line 1106), `Shape.__and__` (line 1003).

**What it does.** When the right-hand operand is neither a `Shape` nor an
iterable of `Shape` (e.g. a `MeshPart`), the operator returns `NotImplemented`
instead of raising `TypeError`. Python's operator dispatch then looks up the
*reflected* operator on the right-hand operand and calls
`MeshPart.__radd__` / `__rsub__` / `__rand__`.

**Why.** Without this, `native_part - mesh_part` raises `TypeError`. The
reflected operators on `MeshPart` already exist and route through
`mesh_cut(other_shape, self)` — but Python only calls them if `Shape.__sub__`
declines first via `NotImplemented`. Returning a hard `TypeError` short-circuits
the protocol. This is the load-bearing change that makes the *operator-form*
mixed-kind CSG (`native_part - mesh_part`, `mesh_part + native_part`) work
the way the design contract promises.

**How.** Each operator first detects "this operand is not a Shape and not an
iterable of Shapes" and bails out cleanly:

```python
# __add__ / __sub__
try:
    operands = [other] if isinstance(other, Shape) else list(other)
except TypeError:
    # `other` is neither a Shape nor iterable (e.g. a MeshPart from the
    # optional mesh backend): defer to its reflected operator.
    return NotImplemented
```

```python
# __and__
if not isinstance(other, (Shape, list, tuple)):
    return NotImplemented
```

Subsequent calls reach `MeshPart.__radd__` / `__rsub__` / `__rand__` which
delegate to `mesh_fuse(other, self)`, `mesh_cut(other, self)`,
`mesh_intersect(other, self)` and tessellate the Shape into mesh space.

**Trade-offs.** None — the change strictly broadens the supported operand
types; previously-passing cases (Shape ± Shape) are bit-for-bit unchanged.

**Where invoked from.** Any user expression that mixes a build123d Shape and a
MeshPart. Inside the codebase, the change is consumed by `MeshPart.__radd__`,
`__rsub__`, `__rand__` in `mesh_part.py`.

**Tests.** `test_operator_coerces_shape_operand_on_the_left`,
`test_operator_coerces_shape_operand_on_the_right`,
`test_operator_chain_stays_in_mesh_space`,
`test_operator_rejects_bad_operand` (rejection still works — a `str` is not a
Shape, not an iterable, and not a MeshPart, so both legs return
`NotImplemented` and Python raises `TypeError` itself).

**Prior art.** Python data-model: PEP 3119 / data-model.html §3.3.8 —
"`NotImplemented` is the singleton to return from a binary operator when the
operand is of the wrong type, deferring to the reflected operator".

---

## B. `bridge.py` — the OUT leg (Shape → seeded Manifold)

The bridge is the one place build123d hands geometry to `manifold3d`. Two
non-negotiables shape the whole module:

* The handoff must produce a **closed, oriented, 2-manifold** mesh — anything
  else makes `manifold3d`'s boolean silently return an empty body.
* Every triangle must carry the seeded `face_id` of its originating
  build123d `Face`; `manifold3d` then *preserves* those ids through every
  boolean, which is what enables exact analytic recovery in `recovery.py`.

### B.7 `FaceRecord` / `SideMap` — provenance side-map

**Code**: `mesh/bridge.py::FaceRecord` (line 117),
`mesh/bridge.py::SideMap` (line 185),
`mesh/bridge.py::_analyse_face` (line 272).

**What it does.** Captures, per seeded face id, everything `recovery.py` needs
to rebuild an exact analytic face: the originating build123d Face, its
analytic `Geom_Surface`, the `surface_kind` (`PLANE` / `CYLINDER` / ...), the
source-shape name, and — for a planar face — the exact plane origin and
normal.

**Why.** Without provenance, a mesh CSG result is just triangles; there is no
way to know "this region of the boolean output came from face #3 of the input
box, whose plane is Z=h". The side-map *is* the provenance that makes
recovery exact rather than approximate. Two facts make it work:

* **Ids are globally unique** (a process-wide `itertools.count()` —
  `_FACE_ID_COUNTER` at line 99) so two side-maps from different shapes
  never collide; a boolean simply merges them with a plain dict.update().
* **`manifold3d` preserves seeded `face_id`** through every boolean operation
  — when a face is cut, all the cut pieces keep the original id (the same id
  on every resulting fragment), so each id can identify *multiple* disjoint
  faces in the output (recovery handles this in `_recover_planar_face`).

**How.**

`_analyse_face` uses `BRepAdaptor_Surface` to read the analytic surface type
and, for a `PLANE`, queries `adaptor.Plane().Axis()` for the exact origin /
direction:

```python
adaptor = BRepAdaptor_Surface(face.wrapped)
kind = _SURFACE_KIND.get(adaptor.GetType(), "OTHER")
geom_surface = BRep_Tool.Surface_s(face.wrapped)
record = FaceRecord(face_id=face_id, b3d_face=face,
                    surface_kind=kind, geom_surface=geom_surface, source=source)
if kind == "PLANE":
    axis = adaptor.Plane().Axis()
    location = axis.Location()
    direction = axis.Direction()
    record.plane_origin = np.array([location.X(), location.Y(), location.Z()])
    record.plane_normal = np.array([direction.X(), direction.Y(), direction.Z()])
```

The exact `Geom_Surface` is also kept around even for curved faces — recovery
keeps curved regions faceted but the analytic surface is preserved as
provenance so selectors like `MeshPart.faces_from("cylinder")` still work.

`SideMap` is a thin dict-of-`FaceRecord` keyed by id:

* `add_face(face, source)` allocates a fresh id from `_FACE_ID_COUNTER` and
  stores the analysed record.
* `merged(other)` returns a new map combining both record sets — the dict
  update is collision-free by construction.
* `transformed(matrix)` returns a new map with every *planar* record's
  origin/normal moved by the 3×4 affine. The normal moves under the linear
  part only (translation never applies to a direction).

```python
def transformed(self, matrix):  # FaceRecord
    if not self.is_planar:
        return self
    linear = matrix[:, :3]
    translation = matrix[:, 3]
    moved_origin = linear @ self.plane_origin + translation
    moved_normal = linear @ self.plane_normal
    return FaceRecord(... plane_origin=moved_origin, plane_normal=moved_normal)
```

This is the load-bearing line that keeps `MeshPart.move(Location)` and friends
producing a side-map that *still* matches the moved mesh.

**Trade-offs.** Plane parameters are kept in numpy arrays for cheap matrix
ops; the `Geom_Surface` handle is the actual OCC object (not re-resolved on
recovery). Curved faces hold their `Geom_Surface` for completeness — recovery
could in principle exactly re-trim them given a curve fit, but that is out of
scope for the current backend (recovery keeps curved regions faceted).

**Where invoked from.** `_analyse_face` is called once per build123d Face at
tessellation time inside `shape_to_manifold`. `SideMap.merged` / `.transformed`
are called by every operation on `MeshPart` that produces a new map.

**Tests.** `test_seeded_box_has_six_distinct_face_ids`,
`test_seeding_tracks_input_faces_not_triangles`,
`test_faceid_survives_a_boolean`, `test_side_maps_merge_without_collision`,
`test_from_mesh_has_an_empty_side_map`,
`test_transformed_mesh_part_still_reconstructs`.

### B.8 `_weld(vertices, triangles, decimals=6)` — array-level weld

**Code**: `mesh/bridge.py::_weld` (line 313).

**What it does.** Numpy-array sibling of `_weld_mesh` (A.1). Merges
coincident vertices in a per-face soup by grid-snap, returns the welded
vertices and re-indexed triangles. Drops triangles that collapsed to a
degenerate after re-indexing.

**Why.** Same reason as A.1, but on raw numpy arrays so the bridge can build
the manifold without round-tripping through `Vector`. Each Face's
`face.tessellate(linear_tol, angular_tol)` is a per-face vertex soup; the
seam vertices duplicate.

**How.** Bit-for-bit the same algorithm as `_weld_mesh` (snap → `np.unique`
on the snapped rows → re-index → average back the un-snapped coords). The
only difference is that this version operates directly on numpy arrays and
does not wrap-and-unwrap `Vector`s — see the listing in §A.1.

A subtlety: degenerate-triangle pruning happens **twice** in the bridge — once
inside `_weld` and once in `shape_to_manifold` (lines 423–428) **with
`face_ids` aligned to the surviving triangles**. The second pruning is
necessary because the face-id array is built by the caller and must stay
aligned with the triangle array (you cannot prune the triangles inside `_weld`
without also pruning the face_ids).

**Trade-offs.** Same as A.1; `decimals=6` is hard-coded (TOLERANCE).

**Where invoked from.** `shape_to_manifold` once per call.

**Tests.** Implicit — every `MeshPart.from_part(...)` test exercises this.
`test_bridge_welds_and_produces_valid_manifold`,
`test_bridge_r1_unwelded_soup_is_not_manifold`,
`test_bridge_r1_meshpart_from_part_welds_so_it_does_not_fail`.

### B.9 `shape_to_manifold(shape, source=, ...)` — the full OUT leg

**Code**: `mesh/bridge.py::shape_to_manifold` (line 350),
`mesh/bridge.py::_build_manifold` (line 444).

**What it does.** Convert a build123d `Shape` into a `(Manifold, SideMap)`
pair: tessellate per-face, stamp each triangle with the originating face id,
weld the per-face soup into a single indexed mesh, and build a double-precision
`Mesh64` whose `face_id` array carries the seeded identity.

**Why.** This is *the* bridge function. Two prior approaches did not work:

* **`Mesher`-style serialisation** (3MF write → read) was slow, plus 3MF does
  not carry analytic surface ids — provenance was lost at the file boundary.
* **Per-shape tessellation + manifold3d's own face_id derivation** (the
  default when `face_id` is unseeded): `manifold3d` runs a coplanar-face
  detection internally, *shattering* every curved face into one id per facet
  strip (a single cylinder → dozens of ids). The lossy boundary moved from
  3MF into manifold3d itself. p7 had to seed `face_id` to fix this.

**How.**

```python
side_map = SideMap()
vertex_base = 0
for face in shape.faces():
    face_id = side_map.add_face(face, source)             # B.7
    face_vertices, face_triangles = face.tessellate(linear_tol, angular_tol)
    if not face_triangles:
        continue
    vertex_blocks.append(np.array([(v.X, v.Y, v.Z) for v in face_vertices]))
    block_triangles = np.array(face_triangles) + vertex_base
    triangle_blocks.append(block_triangles)
    face_id_blocks.append(np.full(len(block_triangles), face_id))
    vertex_base += len(face_vertices)
welded_vertices, welded_triangles = _weld(vertices, triangles)   # B.8
# Drop triangles that collapsed during welding; keep face_ids aligned.
keep = (a!=b) & (b!=c) & (a!=c)
welded_triangles = welded_triangles[keep]
face_ids = face_ids[keep]
manifold = _build_manifold(welded_vertices, welded_triangles, face_ids)
if manifold.status() != m3d.Error.NoError:
    raise ValueError(...)
```

`_build_manifold` constructs the `Mesh64` and wraps it in a `Manifold`:

```python
mesh = m3d.Mesh64(
    vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
    tri_verts=np.ascontiguousarray(triangles, dtype=np.uint64),
    face_id=np.ascontiguousarray(np.asarray(face_ids).ravel(), dtype=np.uint64),
)
return m3d.Manifold(mesh)
```

**The `.ravel()` fix.** `manifold3d.Mesh64` accepts a `face_id` kwarg, but
nanobind's overload resolution previously rejected it with "incompatible
function arguments". The diagnosis (in the module docstring lines 42–48) is
that nanobind *auto-casts* dtype and contiguity silently — the rejection is a
**rank** problem: `face_id` must be a flat `(N,)` array. A `(N, 1)` column
vector or a Python list fails. `np.ascontiguousarray(...ravel(), dtype=uint64)`
guarantees rank-1, C-contiguity, and dtype in one call. This took a fair bit
of experiment to nail down.

**Manifold status assertion.** After construction, `manifold.status()` is
checked against `Error.NoError` and `is_empty()` against True; either raises a
clear `ValueError`. Without this, a non-watertight tessellation would *silently*
produce an empty body that "works" but contains no geometry — an awful
debugging surface.

**Mesh64 (double precision).** Using `Mesh64` instead of `Mesh` keeps OCC's
double-precision vertex coordinates without float32 truncation. For a
1m-scale CAD body float32 has ~7 digits of significance; weld decisions at
TOLERANCE=1e-6 happen *below* that precision. Using float64 throughout is
mandatory for the weld to be reliable.

**Trade-offs.**

* Per-face tessellation is slightly slower than a single shape-level
  tessellation, but it is what gives each face its own seeded id.
* The default tolerances are absolute (`DEFAULT_LINEAR_TOLERANCE = 0.1`,
  `DEFAULT_ANGULAR_TOLERANCE = 0.2`) — absolute is right for CSG (see A.2).

**Where invoked from.** `MeshPart.from_part` (the primary public surface;
mesh_part.py:158); every primitive constructor (`MeshPart.box`, `.sphere`,
…) routes through `from_part`.

**Tests.** `test_bridge_welds_and_produces_valid_manifold`,
`test_bridge_r1_unwelded_soup_is_not_manifold`, the entire `seeded_*` and
`faceid_*` family.

### B.10 `read_result(manifold)` — extract identity from a boolean result

**Code**: `mesh/bridge.py::read_result` (line 499),
`mesh/bridge.py::ResultMesh` (line 474).

**What it does.** Read the vertex / triangle arrays and the seeded
`face_id` array out of a Manifold (typically the result of a boolean) into a
`ResultMesh` dataclass.

**Why.** `manifold3d`'s `Manifold.to_mesh()` returns a `MeshGL64` whose
`vert_properties` is `(V, 7+)` (positions + optional normals + ...); we want
just the positions and the seeded ids in plain numpy.

**How.**

```python
mesh = manifold.to_mesh()
vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
triangles = np.asarray(mesh.tri_verts, dtype=np.int64)
face_id = np.asarray(mesh.face_id, dtype=np.int64)
return ResultMesh(vertices=vertices, triangles=triangles, face_id=face_id)
```

`ResultMesh` exposes two convenience queries:

* `distinct_ids` — the sorted list of distinct seeded ids in the result (one
  per analytic face surviving the boolean).
* `triangles_of(face_id)` — `np.where(self.face_id == face_id)[0]`, the
  triangle indices for one seeded id (used by recovery to fetch a face's
  triangle group).

**Trade-offs.** `vertices` and `triangles` are *views* into the manifold's
own arrays; mutating them would corrupt the manifold. Recovery never mutates.

**Where invoked from.** `MeshPart.to_solid`, `MeshPart.faces`,
`MeshPart.feature_edges` — anywhere the IN leg starts.

**Tests.** Implicit in every reconstruction test.

---

## C. `recovery.py` — the IN leg (exact reconstruction)

The IN leg is where mesh CSG pays for itself. A `MeshPart` carrying a non-empty
side-map can be **baked back into an exact analytic B-rep** for any
all-planar CSG result: the recovered planar faces lie on the *same* analytic
planes as the input, so the volume is bit-exact and OpenCASCADE's
`BRepFilletAPI` succeeds on the result. p8 / p9 demonstrated the full
recovery; the items below are the algorithms that ship.

### C.11 `_connected_components(triangles)` — edge-connectivity BFS

**Code**: `mesh/recovery.py::_connected_components` (line 91).

**What it does.** Split a triangle set into edge-connected components.

**Why.** A single seeded `face_id` can carry a face that a boolean cut into
two disjoint pieces. Each piece must become its own `TopoDS_Face` — *not* a
hole — so the component split runs **before** boundary-loop extraction. This
is the p8 "split-face concern" fix: without it, two disjoint pieces of one id
would be interpreted as an outer loop with a hole (the smaller piece
classified as a hole inside the larger piece), producing a face with a hole
that's not actually a hole.

This routine is **distinct from** `topology/three_d.py::_connected_components`
(A.4):

* `three_d.py`'s version uses **vertex** connectivity over an *entire mesh*
  to split disjoint bodies (for `Solid.from_mesh`).
* `recovery.py`'s version uses **edge** connectivity within one *faceID
  group* to split a face that a boolean cut. The triangles of one cut face
  do share vertices with neighbouring faces (the boundary vertices), so
  vertex-connectivity would erroneously merge them with adjacent faceID
  groups; only edge-shared triangles count as "in the same piece".

**How.** A standard iterative BFS on a triangle-adjacency graph keyed by
shared edges (each edge is sorted `(min, max)` so the two adjacent triangles
hit the same dict key):

```python
edge_to_local = defaultdict(list)
for local_index, triangle in enumerate(triangles):
    a, b, c = triangle
    for s, e in ((a,b), (b,c), (c,a)):
        key = (s, e) if s < e else (e, s)
        edge_to_local[key].append(local_index)

component = np.full(len(triangles), -1)
next_component = 0
for seed in range(len(triangles)):
    if component[seed] != -1: continue
    component[seed] = next_component
    stack = [seed]
    while stack:
        cur = stack.pop()
        a, b, c = triangles[cur]
        for s, e in ((a,b), (b,c), (c,a)):
            key = (s, e) if s < e else (e, s)
            for neighbour in edge_to_local[key]:
                if component[neighbour] == -1:
                    component[neighbour] = next_component
                    stack.append(neighbour)
    next_component += 1
return [np.where(component == k)[0] for k in range(next_component)]
```

**Trade-offs.** O(M) for M triangles in the group; the `np.where` at the
return is O(M·K) for K components — fine because both are tiny in practice
(one or two components per face).

**Where invoked from.** `_recover_planar_face` once per planar seeded id.

**Tests.** `test_reconstruction_splits_a_cut_face_into_two`.

### C.12 `_boundary_loops(triangles)` — one-use edges → ordered loops

**Code**: `mesh/recovery.py::_boundary_loops` (line 147).

**What it does.** Return the boundary of one connected triangle group as a
list of ordered vertex-index loops — the outer loop plus any hole loops.

**Why.** OCC needs a wire (an ordered sequence of edges) for face
construction; a triangle group has only an unordered set of boundary edges.
Each loop must be reconstructed in winding-consistent order.

**How.** Three passes:

1. **Edge usage count.** For every triangle, count each of its three edges by
   their sorted key, and remember the *first* directed instance
   (`edge_direction.setdefault(key, (start, end))`). A boundary edge is one
   used exactly once; interior edges are used twice.
2. **Build a successor map** on the boundary edges only — `successors[start].
   append(end)` — so the loop walker can find the next edge from any vertex.
3. **Walk every boundary edge into a loop.** From an unused boundary edge,
   chain successors until you return to the starting vertex or run out:

   ```python
   while current != start and guard < 1_000_000:
       loop.append(current)
       candidates = [w for w in successors[current] if (current, w) not in used]
       if not candidates: break
       following = candidates[0]
       used.add((current, following))
       current = following
       guard += 1
   ```

The `guard < 1_000_000` cap is a *belt-and-suspenders* defence against a
mesh whose boundary edges' successor lookup produces a cycle that does *not*
close back on `start` (e.g. a non-2-manifold input the bridge somehow missed
catching). Without the guard such input would hang.

**Trade-offs.** Each boundary vertex's `successors` list has at most a handful
of entries in practice; the "candidates[0]" choice is greedy but fine
on a 2-manifold (only one continuation can be unused at each step). For a
non-manifold, this routine produces *something* but the result is
implementation-defined.

**Where invoked from.** `_recover_planar_face` for each connected component
of a planar id.

**Tests.** Implicit in every planar reconstruction test
(`test_reconstruction_recovers_exact_planar_solid`,
`…keeps_an_internal_void`, etc.).

**Prior art.** Standard one-used-edge boundary extraction in mesh processing
(e.g. trimesh's `outline` / `boundary`).

### C.13 `_project_to_plane(point, origin, normal)`

**Code**: `mesh/recovery.py::_project_to_plane` (line 203).

**What it does.** Orthogonal projection of a 3-D point onto a plane given by
origin and (not necessarily unit) normal.

**Why.** Boundary vertices coming out of a manifold boolean carry the
**tessellation jitter** of the original mesh: even with double-precision
`Mesh64` the vertices on a planar face are not bit-exact on the plane (they
were already off by ε after tessellation, and the boolean adds another ε).
Projecting them onto the *known input plane* erases that jitter and makes the
recovered face geometrically exact — the load-bearing step that makes the
recovered volume bit-exact (verified in
`test_payoff_recovered_brep_matches_native_boolean`).

**How.** Textbook orthogonal projection:

```python
unit_normal = normal / (np.linalg.norm(normal) or 1.0)
return point - np.dot(point - origin, unit_normal) * unit_normal
```

The `or 1.0` defends against an exactly-zero normal (won't happen on a
side-map plane record, but the routine is paranoid).

**Trade-offs.** Single matrix-multiply-equivalent; negligible cost.

**Where invoked from.** `_recover_planar_face::loop_points`.

**Tests.** `test_reconstructed_faces_are_analytic_planes`,
`test_payoff_recovered_brep_matches_native_boolean`.

**Prior art.** Standard linear algebra.

### C.14 `_exact_plane(record)` — Geom_Plane from side-map parameters

**Code**: `mesh/recovery.py::_exact_plane` (line 275).

**What it does.** Build a `Geom_Plane` from a planar side-map record's
origin / normal parameters.

**Why.** Recovery passes this plane to `BRepBuilderAPI_MakeFace(geom_plane,
outer_wire, True)` — building a face on the **exact analytic plane** rather
than letting OCC re-fit a plane through tessellated vertices. The result is
that the recovered face lies on bit-for-bit the input plane.

**How.**

```python
origin = record.plane_origin
normal = record.plane_normal
axis = gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal))
return Geom_Plane(gp_Pln(axis))
```

**Trade-offs.** Assumes the record is planar (asserted). The X-axis of the
`gp_Ax3` is undefined here — OCC picks one in the plane orthogonal to the
normal, which is fine for face construction (the wire's orientation around
the normal disambiguates).

**Where invoked from.** `_recover_planar_face` once per planar id.

### C.15 `_recover_planar_face(result, face_id, record)` — exact analytic face

**Code**: `mesh/recovery.py::_recover_planar_face` (line 291).

**What it does.** Rebuild exact planar `TopoDS_Face`s for one seeded planar
id — one face per edge-connected component of the id's triangle group, with
the largest boundary loop as the outer wire and any nested loops as holes.

**Why.** This is the heart of "exact recovery". Together with `_exact_plane`
and `_project_to_plane`, it produces a face whose:

* **surface** is the bit-exact `Geom_Plane` of the input face,
* **boundary vertices** are the *projection* of the manifold boolean's
  output vertices onto that exact plane (jitter erased),
* **outer-vs-hole topology** is recovered from the manifold output by area
  ranking in a local 2-D frame on the plane.

**How.** Five steps:

1. **Plane setup.** Get the exact `Geom_Plane`; pick a 2-D in-plane frame
   `(u, v)` orthogonal to the normal:

   ```python
   in_plane_u = np.array([1.0, 0.0, 0.0])
   if abs(np.dot(in_plane_u, unit_normal)) > 0.9:
       in_plane_u = np.array([0.0, 1.0, 0.0])
   in_plane_u -= np.dot(in_plane_u, unit_normal) * unit_normal
   in_plane_u /= np.linalg.norm(in_plane_u)
   in_plane_v = np.cross(unit_normal, in_plane_u)
   ```

   The "if abs(...) > 0.9" guards against an X-axis-aligned normal — pick Y
   instead so the cross product is well-conditioned.

2. **Per-component recovery.** Loop over `_connected_components(group)`. For
   each component:

   * extract its boundary loops via `_boundary_loops`,
   * **score by area** in the local `(u, v)` frame (shoelace formula in
     `loop_area`),
   * largest area = outer loop; remaining = holes.

3. **Build wires.** `BRepBuilderAPI_MakePolygon` per loop, each vertex's 3D
   point being the orthogonal projection onto the exact plane (so wire
   vertices lie *exactly* on the analytic plane).

4. **Build the face on the exact plane.** `BRepBuilderAPI_MakeFace(geom_plane,
   outer_wire, True)` then add each hole wire reversed:

   ```python
   face_builder = BRepBuilderAPI_MakeFace(geom_plane, outer_wire, True)
   for _, hole_loop in scored[1:]:
       hole_wire = make_wire(hole_loop)
       if hole_wire is not None:
           face_builder.Add(TopoDS.Wire_s(hole_wire.Reversed()))
   ```

5. **Fallback.** If `MakeFace(plane, outer_wire)` fails (rare — OCC sometimes
   rejects on tolerance), retry `MakeFace(outer_wire)` (OCC re-derives the
   plane from the wire — still on the projected points, so still exact within
   projection accuracy). This is a belt-and-braces fallback; production paths
   take the analytic-plane branch.

**Trade-offs / limitations.**

* The "largest loop is outer" rule is correct for simply-connected planar
  regions; for a face whose outer boundary is *smaller* than a loop inside it
  (impossible on a normal CSG result, but theoretically constructable), the
  rule misclassifies. Build123d does not produce such pathologies.
* Hole orientation is flipped via `Reversed()` so the boundary winding is
  consistent with OCC's face-orientation convention.

**Where invoked from.** `recover_brep` once per planar seeded id.

**Tests.** `test_reconstruction_recovers_exact_planar_solid`,
`test_reconstructed_faces_are_analytic_planes`,
`test_reconstruction_splits_a_cut_face_into_two`,
`test_reconstruction_keeps_an_internal_void`,
`test_payoff_recovered_brep_matches_native_boolean`,
`test_payoff_real_fillet_on_a_recovered_edge` — the final test is the payoff:
the recovered B-rep is filletable using native OCC `BRepFilletAPI`.

### C.16 `_faceted_patch(result, face_id)` — curved-id fallback

**Code**: `mesh/recovery.py::_faceted_patch` (line 394).

**What it does.** Return a curved seeded id as a list of flat triangle faces
— one `TopoDS_Face` per triangle.

**Why.** faceID *identifies* the surface (the side-map record holds the exact
`Geom_Cylinder` / `Geom_Sphere` / ...), but exact re-trimming of a curved
surface with re-fitted boundary curves is out of scope for the current
backend. The pragmatic choice: keep the *identity* (downstream selectors that
ask "which faces came from the cylinder?" still work) but represent the
geometry as facets. The alternative — refusing to recover curved faces — would
have made any sphere/cylinder body unrecoverable, which is far worse.

**How.** One `TopoDS_Face` per triangle via `BRepBuilderAPI_MakePolygon`
followed by `BRepBuilderAPI_MakeFace(polygon.Wire())`:

```python
for triangle in result.triangles[result.triangles_of(face_id)]:
    polygon = BRepBuilderAPI_MakePolygon()
    for vertex_index in triangle:
        polygon.Add(gp_Pnt(*(float(x) for x in result.vertices[vertex_index])))
    polygon.Close()
    face_builder = BRepBuilderAPI_MakeFace(polygon.Wire())
    if face_builder.IsDone():
        patch.append(Face(face_builder.Face()))
```

**Trade-offs.** Faceted geometry — exact volume on a faceted approximation,
not bit-exact like the planar path. Selectors like
`filter_by(GeomType.CYLINDER)` *won't* find these faces (they are planar
triangles, not cylindrical) — `MeshPart.analytic_faces()` raises when any
curved region is present to prevent that silent-wrong-answer case (P3
contract).

**Where invoked from.** `recover_brep` for each non-planar seeded id.

**Tests.** `test_curved_result_reconstructs_but_stays_faceted`.

### C.17 `recover_brep(result, side_map)` — full pipeline

**Code**: `mesh/recovery.py::recover_brep` (line 427).

**What it does.** End-to-end recovery: group → split → recover → sew →
shells → void-classify → `Solid` / `Compound` / `Shell` / None.

**How.** The pipeline:

1. **Validate.** Empty side-map → raise (a MeshPart from raw arrays has no
   provenance; use `to_solid(reconstruct=False)`).
2. **Per-id recovery.** Loop over `result.distinct_ids`; for each, fetch the
   side-map record. Planar → `_recover_planar_face`; non-planar →
   `_faceted_patch`. Add every produced face to a single
   `BRepBuilderAPI_Sewing` instance (tolerance `_SEW_TOLERANCE = 1e-6`).
3. **Sew shells.** `sewing.Perform()`; one shell per connected component of
   faces falls out. Each shell is fixed via `ShapeFix_Shell`.
4. **Shells → solids with bbox void-classification.** Exactly the same
   `_group_shells_into_solids` routine as `Solid.from_mesh` (A.5). The
   rationale (one Solid per outer shell, voids added with
   `BRepBuilderAPI_MakeSolid.Add`) is identical and load-bearing — without
   this, a multi-body recovery would silently lose its disjoint-body
   structure.
5. **Pack the result.** Exactly one solid → return it; multiple solids →
   `Compound`; otherwise (no closed solid built) → the first shell (rare,
   degraded result).

```python
for outer_shell, void_shells in _group_shells_into_solids(shells):
    solid_builder = BRepBuilderAPI_MakeSolid(outer_shell.wrapped)
    for void_shell in void_shells:
        solid_builder.Add(void_shell.wrapped)
    if not solid_builder.IsDone(): continue
    solid_fix = ShapeFix_Solid(solid_builder.Solid())
    solid_fix.Perform()
    solids.append(Solid(TopoDS.Solid_s(solid_fix.Solid())))
```

The volume reported on `RecoveryResult` sums the per-Solid volumes — each
Solid already accounts for its internal voids, so the sum is over disjoint
top-level bodies only.

**Trade-offs.** Sewing tolerance is fixed at `1e-6` — the welded mesh's
vertex spacing is well under this. The `BRepBuilderAPI_Sewing` is per-call;
no caching, no incremental update.

**Where invoked from.** `MeshPart.to_solid` (the analytic path),
`MeshPart.faces`, `MeshPart.analytic_faces`, `MeshPart.faces_from`.

**Tests.** The whole "reconstruction" test family plus the "payoff" family in
`test_mesh.py` — `test_reconstruction_recovers_exact_planar_solid`,
`…falls_back_to_faceted_without_side_map`,
`…disjoint_bodies_yield_a_compound`, the payoff fillet/chamfer tests.

**Prior art.** OCCT `BRepBuilderAPI_Sewing` + `BRepBuilderAPI_MakeSolid` —
the standard mesh-to-BREP sewing path, here driven by provenance instead of
spatial tolerance.

---

## D. `mesh_part.py` — the value type

`MeshPart` is the user-facing object. It is **deliberately not** a `Shape`
subclass. The reasoning matters and is worth re-stating: a build123d `Shape`
is by contract an exact-BREP entity (analytic faces, STEP export, OCC
fillet/chamfer); a faceted mesh is none of those. Crossing the mesh↔BREP
boundary is therefore always an explicit, named verb (`MeshPart.from_part`,
`MeshPart.to_solid`) — never an implicit coercion. This keeps every dimension
of value semantics, equality, type checks and STEP behaviour clean. The
trade-off — users must learn there is a separate type — is paid back in
explicit cost of operations and absence of surprise behaviour.

### D.18 `MeshPart` value type — invariant

**Code**: `mesh/mesh_part.py::MeshPart` (line 85).

**Invariant.** A `MeshPart` holds two things, both immutable from the outside:

* `_manifold: manifold3d.Manifold` — the underlying watertight mesh body.
* `_side_map: SideMap` — the `faceID → provenance` map.

Plus a `_feature_edges_cache` (lazy `FeatureChainSelection`) so repeated
`feature_edges()` calls don't recompute. `__slots__` is used to keep the
object small and reject accidental attribute creation.

**Construction.** The bare `__init__` type-checks the manifold (raises
`TypeError` on the wrong kind), and the side-map defaults to empty. Every
constructor that wraps a `Manifold` from somewhere else (`from_part`,
`from_mesh`, the CSG free functions, the ops module) routes through this.

**Why not a Shape subclass.**

* `Shape.wrapped` is an OCC `TopoDS_Shape` — there is no honest cast from a
  Manifold.
* `Shape`-level operations (`fillet`, `chamfer`, `clean`) assume analytic
  geometry; on a mesh these would silently produce a wrong answer, against
  P3.
* STEP export of a "Shape" is presumed to be analytic; a mesh body would
  STEP-export to a degraded faceted approximation invisibly.

So `MeshPart` is its own value type with its own surface. `to_solid` /
`to_part` are the *named* verbs to bake into Shape space.

### D.19 `from_part`, `from_mesh`; primitive constructors

**Code**: `mesh/mesh_part.py::MeshPart.from_part` (line 129),
`mesh_part.py::MeshPart.from_mesh` (line 167);
primitives `box` / `sphere` / `cylinder` / `cone` / `torus` (lines 213-316).

**`from_part(shape, source='shape', linear_tolerance=0.1,
angular_tolerance=0.2)`.** Wraps `shape_to_manifold` (B.9) — the OUT leg.

**`from_mesh(vertices, triangles)`.** Wraps a raw `(N,3)` / `(M,3)` array
pair into a `Mesh64`, validates the result is a valid manifold, and returns a
`MeshPart` with an **empty** side-map. Raw mesh has no provenance, so
`to_solid` falls back to the faceted bake (A.3). This is the entry point for
an imported STL.

**Primitive constructors.** Every primitive (`box`, `sphere`, `cylinder`,
`cone`, `torus`) builds the corresponding build123d primitive
(`Box(length,width,height)`, etc.) and routes it through `from_part`. This
yields the build123d centering convention exactly (origin-centred) plus a
full side-map: a box gives six records, a cylinder gives three (two flat
caps + one curved lateral), and so on. The full reason these don't bypass
`from_part` and build the manifold directly is so the side-map is populated
— a primitive built directly from `m3d.Manifold.cube(...)` would have an
empty side-map.

### D.20 CSG operators `+ - &` + reflected forms

**Code**: `MeshPart.__add__` / `__sub__` / `__and__` and their reflected /
in-place forms (lines 384–421); the free functions `mesh_fuse` / `mesh_cut`
/ `mesh_intersect` (lines 1129–1209); `_coerce` (line 1062);
`_merge_side_maps` (line 1085); `_check_result` (line 1103).

**Operators.** All six binary operator dunders (`__add__`, `__radd__`,
`__iadd__`, `__sub__`, `__rsub__`, `__isub__`, `__and__`, `__rand__`,
`__iand__`) delegate to the free functions:

```python
def __add__ (self, other): return mesh_fuse(self, other)
def __radd__(self, other): return mesh_fuse(other, self)
def __sub__ (self, other): return mesh_cut (self, other)
def __rsub__(self, other): return mesh_cut (other, self)  # ← needs A.6
def __and__ (self, other): return mesh_intersect(self, other)
def __rand__(self, other): return mesh_intersect(other, self)
```

The reflected operators are what make `native_part - mesh_part` work — but
*only* if the left operand declines first. `Shape.__sub__` returns
`NotImplemented` on a non-Shape, non-iterable RHS (A.6), so Python dispatches
to `mesh_part.__rsub__`. Without the core change, the operator would
hard-fail.

**Free functions.** All three take `*shapes: MeshOperand` (a union of `Shape`
and `MeshPart`); `_coerce` tessellates any `Shape` into a `MeshPart` so the
batch path is uniform. They use `m3d.Manifold.batch_boolean` (a single
N-way pass — measurably faster than folding `+` in Python). The merged
side-map is built via `_merge_side_maps`, then `_check_result` raises if the
manifold status is non-OK.

**Trade-offs.** Operator coercion happens only at protocol boundary; chained
arithmetic stays in mesh space (`mesh_part + native + native + native` calls
`from_part` once per native operand, then a single batch boolean).

**Where invoked from.** Every CSG expression on a `MeshPart` flows through
here.

**Tests.** `test_operator_add_matches_mesh_fuse`,
`test_operator_sub_matches_mesh_cut`, `test_operator_and_matches_mesh_intersect`,
`test_operators_return_mesh_part`, `test_in_place_operators`,
`test_operator_coerces_shape_operand_on_the_right/left`,
`test_operator_chain_stays_in_mesh_space`,
`test_operator_rejects_bad_operand`, `test_mesh_cut_drills_many_holes_quickly`
(perf payoff).

### D.21 Transforms `translate / rotate / scale / move(Location)`

**Code**: `mesh/mesh_part.py::translate` (line 540), `rotate` (line 558),
`scale` (line 578), `move` (line 600); `_euler_rotation_matrix` (line 1028).

**What it does.** Move the manifold *and* its side-map together so the
recovered B-rep lands in the transformed frame.

**Why.** faceID **identity** is transform-invariant — a face is the same face
no matter where it is in space. But a planar record carries the *exact*
origin and normal of that face; if we move the mesh and don't move those
parameters, recovery projects the moved face's vertices onto the *old*
plane, getting nonsense. So the manifold transform and the
`SideMap.transformed(matrix)` call must run together.

**How.** Each transform builds the equivalent 3×4 affine matrix (linear part
+ translation column) and applies it to **both** the manifold (via
`m3d.Manifold.translate` / `.rotate` / `.scale` / `.transform`) and the
side-map (via `SideMap.transformed(matrix)`):

```python
def translate(self, offset):
    vector = np.array([float(c) for c in offset])
    moved = self._manifold.translate(list(vector))
    matrix = np.hstack([np.identity(3), vector.reshape(3, 1)])
    return MeshPart(moved, self._side_map.transformed(matrix))
```

For `rotate(x, y, z)` (degrees), `_euler_rotation_matrix` builds
`Rz @ Ry @ Rx` — matching `manifold3d`'s "global X, then Y, then Z" Euler
convention exactly:

```python
return rotate_z @ rotate_y @ rotate_x
```

`move(Location)` uses the location's full `gp_Trsf` value matrix:

```python
transformation = location.wrapped.Transformation()
matrix = np.array([[transformation.Value(row+1, col+1) for col in range(4)]
                   for row in range(3)])
return MeshPart(self._manifold.transform(matrix),
                self._side_map.transformed(matrix))
```

The 3×4 affine is the canonical form `manifold3d.Manifold.transform`
accepts.

**Where invoked from.** User code. Internally the corner-blend pipeline does
not transform; the swept-tool builders construct in world space directly.

**Tests.** `test_translate_moves_the_body`, `test_rotate_turns_the_body`,
`test_scale_resizes_the_body`, `test_move_applies_a_location`,
`test_transformed_mesh_part_still_reconstructs` (the critical contract: a
transformed mesh still bakes back to an exact B-rep).

### D.22 `to_solid(reconstruct=, unify_coplanar=)`

**Code**: `mesh/mesh_part.py::MeshPart.to_solid` (line 627);
`to_part` (line 676).

**What it does.** Bake the mesh to a build123d `Solid` (or `Compound` for
multi-body). Two paths:

* **Reconstruction** (default, when a non-empty side-map is present): calls
  `recover_brep(read_result(manifold), side_map)` — the analytic path of
  §C.17. The result is *exact* for all-planar bodies, *faceted-on-known-
  surface* for curved.
* **Faceted bake** (fallback, or `reconstruct=False`): calls
  `Solid.from_mesh(vertices, triangles, fix=False)` — the direct shell
  assembly of §A.3. One planar `TopoDS_Face` per triangle.

```python
if reconstruct and self._side_map:
    result_mesh = read_result(self._manifold)
    recovered = recover_brep(result_mesh, self._side_map)
    if recovered.solid is not None and isinstance(recovered.solid, (Solid, Compound)):
        return recovered.solid
    # Recovery failed to produce a closed solid; fall through to faceted bake.
vertices, triangles = self.to_arrays()
solid = Solid.from_mesh(vertices, triangles, fix=False)
if unify_coplanar:
    solid = solid.clean()
return solid
```

The fallback-from-recovery branch is a safety net: in the (unobserved)
case where recovery yields only a `Shell` or `None`, the faceted path runs
so the user always gets a baked solid. `unify_coplanar` calls
`solid.clean()` (`ShapeUpgrade_UnifySameDomain`) — only on the faceted path
since the analytic path already produces minimal faces.

**Where invoked from.** Final BREP bake at the end of a CSG pipeline; also
indirectly via `to_part`.

**Tests.** `test_meshpart_to_solid_and_to_part`,
`test_reconstruction_falls_back_to_faceted_without_side_map`,
`test_reconstruct_false_forces_the_faceted_path`, the entire payoff suite.

### D.23 `faces() / analytic_faces() / faces_from(source)`

**Code**: `mesh_part.py::MeshPart.faces` (line 691),
`analytic_faces` (line 737), `faces_from` (line 945).

**What it does.** Recover the build123d Faces of the mesh body — three
selectors, each with a different contract.

* **`faces()`.** Run the full recovery; return every face (planar →
  analytic, curved → faceted triangle patches) as a `ShapeList`. Selectors
  like `.sort_by(Axis.Z)` work; directional and planar selectors are
  guaranteed correct; **curved-analytic selectors are *not* meaningful** on
  the faceted patches — the curved regions are planar triangles, not
  cylinders.
* **`analytic_faces()`.** The strict P3 enforcement point. If *any* curved
  region is present, raise `ValueError` — a curved-analytic selector
  (`filter_by(GeomType.CYLINDER)` / `Part.fillet`) on a faceted patch would
  silently produce a wrong answer, and the P3 contract is "never silently
  produce a wrong answer". Use this when downstream code requires analytic
  selectors.
* **`faces_from(source)`.** Filter the recovered faces by their side-map
  `source` name. This is a **genuine new selector dimension** that has no
  BREP equivalent: a face *cut* from input "box" inherits the box's seeded
  id, so it still reports `source == "box"`. There is no way to ask this
  question on a native boolean result.

All three guard against empty mesh / empty side-map with clear errors.

**Tests.** `test_faces_of_a_planar_meshpart_returns_six_faces`,
`test_faces_of_a_drilled_block_matches_native_count`,
`test_analytic_faces_*`, `test_faces_from_*`.

### D.24 Direct STL writer

**Code**: `mesh_part.py::export_stl` (line 985),
`_triangle_normals` (line 1215), `_write_binary_stl` (line 1233),
`_write_ascii_stl` (line 1256).

**What it does.** Write a binary or ASCII STL straight from the manifold's
vertex/triangle arrays — *no BREP bake*, *no third-party dependency*.

**Why.** STL is the cheapest export. Round-tripping through a BREP just to
hand to a STEP-or-3MF exporter is overkill (and slow). Doing it directly off
the numpy arrays means the only cost is normal computation + file write.

**How.** `_triangle_normals` computes one outward unit normal per triangle
via the cross product:

```python
corners = vertices[triangles]                              # (M, 3, 3)
normals = np.cross(corners[:,1]-corners[:,0], corners[:,2]-corners[:,0])
lengths = np.linalg.norm(normals, axis=1, keepdims=True)
lengths[lengths == 0.0] = 1.0
return normals / lengths
```

**Binary STL** uses a structured numpy `dtype` matching the binary STL
50-byte facet record layout — the trick that makes serialisation a single
numpy-level write:

```python
record = np.dtype([("n", "<f4", 3), ("c", "<f4", (3,3)), ("attr", "<u2")])
facets = np.zeros(len(triangles), dtype=record)
facets["n"] = normals
facets["c"] = corners
with open(path, "wb") as f:
    f.write(b"build123d MeshPart (binary STL)".ljust(80, b"\0"))
    f.write(struct.pack("<I", len(triangles)))
    f.write(facets.tobytes())
```

The `<f4` / `<u2` are little-endian — STL's standard. The 80-byte header is
filler text padded with zeros. `struct.pack("<I", len(triangles))` is the
4-byte little-endian triangle count.

**ASCII STL** is a straightforward line-based format. Slower because of
Python-level string formatting, but available for diff-friendly debugging.

**Trade-offs.** No `BinaryStream.tofile()` would be very slightly faster but
less explicit; tested cost is "instant" at any reasonable triangle count.
float32 in the binary file is the STL standard — the underlying mesh is
double precision but the on-disk format is float32 (lossy by spec).

**Tests.** `test_meshpart_export_stl`.

### D.25 `offset(amount)` — 3-D Minkowski offset

**Code**: `mesh/ops.py::mesh_offset` (line 498); also wired through
`MeshPart.offset` (mesh_part.py line 490).

**What it does.** Outward (`amount > 0`) or inward (`amount < 0`) 3-D
offset of a mesh body, with a sphere as the offsetting tool.

**Why.** OCC's `BRepOffsetAPI_MakeOffsetShape` is famously fragile on
non-trivial inputs; on a mesh body the Minkowski sum / difference with a
sphere is the equivalent operation and *robust* in the outward direction.

**How.**

* Outward: `body.minkowski_sum(sphere)` — the body grows by `amount`, every
  sharp edge becoming a sphere-rounded fillet.
* Inward: `body.minkowski_difference(sphere)` — every face shrinks by
  `amount`.

The sphere is built directly via `m3d.Manifold.sphere(abs(amount),
segments)` with `segments=32` by default — a balance between facet count and
cost.

**Trade-offs / "honest limits".** Outward is robust; **inward is fragile**.
The fragility is intrinsic, not a bridge bug: when the eroding sphere is
comparable to the body's thinnest half-feature, `minkowski_difference` can:

* collapse to an empty body (legitimately — erosion of a 1mm plate by a 1mm
  radius is empty),
* produce a degenerate manifold (numerical instability at near-thin features),
* hit a status error.

The function raises clear errors when this happens, pointing at the
"raise sphere_segments or reduce |amount|" workaround. There is no automatic
salvage because the failure mode is geometric, not numerical: there is no
inward offset of a 1mm plate by 1mm.

**Tests.** Covered indirectly via `mesh_shell` (D.26); offset itself doesn't
have a dedicated test name in `test_mesh.py` but is exercised by
`test_meshpart_method_offset`-shaped tests.

### D.26 `shell(thickness)` — `self - self.offset(-thickness)`

**Code**: `mesh/ops.py::mesh_shell` (line 564);
`MeshPart.shell` (mesh_part.py line 516).

**What it does.** Hollow a body to a wall of the given thickness.

**How.** Inward-offset the body by `thickness` and subtract the eroded
result from the original:

```python
eroded = mesh_offset(part, -float(thickness), sphere_segments=sphere_segments)
return MeshPart(mesh_cut(part, eroded).manifold)
```

**Trade-offs.** Inherits `mesh_offset`'s honest limit on inward erosion — if
thickness exceeds the body's thinnest half-feature, the inward offset
collapses and `shell` raises with the same diagnostics.

**Where invoked from.** User code. Inside the repo, nothing else uses it
(it's a leaf operation).

**Tests.** Covered by mesh_part method tests.

---

## E. `ops.py` — hull and Minkowski

Unlike a CSG boolean, hull and Minkowski **do not preserve face provenance**:
they synthesise entirely new envelope surfaces. `manifold3d` derives `face_id`
from its own coplanar-region heuristic for these results, which does not
trace back to any input build123d Face. The returned `MeshPart` carries an
**empty side-map**; `to_solid` then falls back to the faceted bake. This is
an honest consequence of the operation, not a bridge limitation.

### E.27 `mesh_hull` — native quickhull

**Code**: `mesh/ops.py::mesh_hull` (line 92);
`MeshPart.hull` (mesh_part.py line 425).

**What it does.** Convex hull of any mix of build123d Shapes and MeshParts.

**How.** Routes to `manifold3d`'s native quickhull:

* one operand → `manifolds[0].hull()`;
* multiple operands → `m3d.Manifold.batch_hull(manifolds)`.

`manifold3d`'s quickhull is the same engine OpenSCAD's `hull()` uses, but
watertight-by-construction (no manual sewing needed).

**Trade-offs.** Empty/degenerate hull (operands collapse to fewer than four
non-coplanar points) raises with a clear error. No provenance survives — see
the module header.

**Tests.** `test_mesh_hull_of_two_boxes_is_their_envelope`,
`…of_a_single_box_is_the_box`, `…fills_a_concavity`,
`…method_on_meshpart`, `…carries_no_provenance`, `…rejects_no_operands`.

**Prior art.** Quickhull (Barber, Dobkin, Huhdanpaa 1996); manifold3d wraps
its own implementation.

### E.28 `mesh_minkowski(a, b, method=)` — sum with two backends

**Code**: `mesh/ops.py::mesh_minkowski` (line 247);
`_minkowski_via_decomposition` (line 207);
`_is_convex` (line 143); `_convex_parts` (line 163);
`_part_vertices` (line 194);
`_CONVEXITY_REL_TOLERANCE = 1e-3` (line 84).

**What it does.** Minkowski sum (dilation) `a ⊕ b` — sweep `b` over every
point of `a`. Two backends:

* **`method='native'`** (default) — `manifold3d.Manifold.minkowski_sum`.
  Correct for both convex and non-convex; cost scales with the product of
  the two face counts for non-convex pairs.
* **`method='decompose'`** — the scad2py convex-pairs algorithm. Exact for
  convex operands and unions of convex parts; raises for connected
  non-convex operands.

**Why two backends?** The native backend is cheaper to use; the decompose
backend is fully self-contained (does the math in pure numpy + manifold3d
primitives) and was ported from `scad2py`'s `minkowski_impl.py` (Apache-2.0,
ochafik) before native Minkowski was added to `manifold3d`. Both are kept
because the decompose backend is *strictly cheaper* for the convex case
where a single hull is the whole answer.

**`_is_convex` test.**

```python
def _is_convex(manifold):
    volume = manifold.volume()
    if volume <= 0.0: return True
    hull_volume = manifold.hull().volume()
    return abs(hull_volume - volume) <= _CONVEXITY_REL_TOLERANCE * volume
```

A convex body equals its own convex hull, so its volume matches the hull's
volume; a non-convex body's hull always strictly contains it. The tolerance
`1e-3` (0.1%) is **deliberately loose**:

* A *faceted* convex primitive (a tessellated sphere) is geometrically
  convex but the bridge's grid-snap weld leaves sub-micron vertex jitter,
  registering as a ~0.01% hull gap.
* A genuinely non-convex body (an L-shape) has a hull gap orders of
  magnitude larger (~17%).

0.1% separates these two cases cleanly with no mis-classification in either
direction.

**Convex-pairs algorithm (`_minkowski_via_decomposition`).**

For convex bodies A and B, `A ⊕ B` is the convex hull of all
vertex-sums {a + b : a in vertices(A), b in vertices(B)}. The algorithm:

1. `_convex_parts(A)`: `A.decompose()` to split topologically-disconnected
   components; if any component is non-convex, *raise* (the general
   convex decomposition of a connected non-convex body needs `coacd` /
   `trimesh`, intentionally out of scope).
2. For every pair (left_part, right_part), take the cartesian sum of their
   vertex sets (a numpy broadcast):

   ```python
   summed = (left_vertices[:, None, :] + right_vertices[None, :, :]).reshape(-1, 3)
   hulls.append(m3d.Manifold.hull_points(summed))
   ```
3. Union the per-pair hulls (a single `batch_boolean` Add).

`m3d.Manifold.hull_points` is the native primitive that makes each
pairwise hull cheap (one quickhull on the cartesian sum, not a full
mesh-mesh Minkowski).

**Tests.** `test_mesh_minkowski_sphere_box_rounds_the_box`,
`…box_box_is_exact_convex_case`, `…decompose_matches_native_for_convex`,
`…decompose_rejects_nonconvex`, `…rejects_unknown_method`,
`…native_handles_nonconvex`, `…difference_erodes_a_box`,
`…method_on_meshpart`.

**Prior art.** OpenSCAD's `minkowski()` operator (CGAL-backed); scad2py's
own clean-room port (ochafik); academic literature on convex-decomposition
Minkowski (Halperin–Sharir).

### E.29 `mesh_minkowski_difference` — erosion

**Code**: `mesh/ops.py::mesh_minkowski_difference` (line 300).

**What it does.** Morphological erosion `a ⊖ b` — `a` eroded by sweeping `b`
across its surface and removing the swept region. The inverse of
`mesh_minkowski`.

**How.** Direct delegation to `m3d.Manifold.minkowski_difference`.

**Trade-offs.** Honest fragility on faceted near-convex tools (same as
`mesh_offset`'s inward branch — see D.25). Erosion of `a` by a `b` larger
than `a` legitimately yields an empty body; the function returns the empty
manifold rather than raising in that case (because empty is the *correct*
result, not an error).

A status mismatch (which happens occasionally on degenerate / numerically
borderline inputs) does raise with a clear message.

**Tests.** `test_mesh_minkowski_difference_erodes_a_box`.

---

## F. `sketch2d.py` — 2-D bridge

The 2-D bridge converts build123d 2-D entities to `manifold3d.CrossSection`
so the native 2-D→3-D ops (`extrude`, `revolve`) can run on profiles built
with build123d's exact-BREP 2-D verbs.

### F.30 build123d Sketch/Face → manifold3d CrossSection

**Code**: `mesh/sketch2d.py::to_cross_section` (line 74);
`_polygons_from_profile` (line 115); `_polygons_from_face` (line 160);
`_polygon_from_wire` (line 182); `_polygon_from_points` (line 216).

**What it does.** Convert a build123d 2-D profile (`Sketch` / `Face` /
`Compound` of faces / raw `(x, y)` point list) into a
`manifold3d.CrossSection`.

**Why.** `manifold3d`'s native 2-D→3-D primitives are *fast* and produce
watertight bodies. Routing build123d's exact-BREP 2-D profiles through them
keeps the same exact-shape API for the design while paying mesh prices for
the extrude/revolve step.

**How.** Dispatch on the profile type:

* `Face` → polygonise its outer wire + each inner (hole) wire (`_polygons_from_face`);
* `Compound` (or anything iterable of `Shape`) → recurse over faces;
* raw point list → wrap as a single contour.

**Polygonisation (`_polygon_from_wire`):**

```python
sampled = wire.positions(deflection=tolerance)
points = [(float(v.X), float(v.Y)) for v in sampled]
if len(points) >= 2 and points[0] == points[-1]:
    points = points[:-1]                    # CrossSection closes implicitly
if len(points) < 3:
    return None
```

`Wire.positions` is build123d's deflection-bounded walker — straight edges
yield two-point segments, arcs / splines yield point sequences within
`tolerance`. The wire walker handles edge ordering and orientation internally,
so seam-point duplicates do not arise.

The fallback path for a degenerate walker:

```python
except (RuntimeError, ValueError):
    sampled = [vertex.center() for vertex in wire.vertices()]
```

handles e.g. single-vertex loops.

**CrossSection construction.**

```python
cross_section = m3d.CrossSection(polygons, m3d.FillRule.Positive)
```

`FillRule.Positive` means Clipper2 (which `manifold3d` uses under the hood)
unions every contour by *winding number* — automatically cleaning up overlap
at wire seams and handling outer/hole orientation without the caller having
to encode it.

**Convention.** build123d sketches live in 3-D on a `Plane`; the bridge
reads `(X, Y)` of every vertex as-is. The caller is responsible for placing
the profile in the XY plane (the default for a builder Sketch); out-of-plane
profiles are silently flattened — explicit by design.

### F.31 `mesh_extrude(profile, height, ...)`; `mesh_revolve(profile, ...)`

**Code**: `mesh/ops.py::mesh_extrude` (line 341);
`mesh/ops.py::mesh_revolve` (line 412).

**What it does.** Drive `manifold3d.CrossSection.extrude` /
`CrossSection.revolve` on the bridged profile.

**`mesh_extrude`** supports linear extrusion plus twist (top rotation about
Z, degrees) and a top-scale (a scalar or `(sx, sy)` pair; 0 collapses the
top to a point — handy for pointed cones). `n_divisions` controls the
intermediate slicing for smoother twist/scale; the native primitive uses 0
for a flat top.

**`mesh_revolve`** revolves about the **Y axis** (matching OpenSCAD's
`rotate_extrude` convention; the profile's `X >= 0` half is swept and any
`X < 0` region is clipped). A partial revolve is supported via `angle < 360`.

Both check for empty / invalid manifolds and raise clear `ValueError`s.

The result is *synthesised faceted geometry* — `extrude` / `revolve` create
new surfaces, so no input `face_id` traces through. The returned `MeshPart`
carries an empty side-map and bakes faceted.

**Tests.** `test_mesh_extrude_*`, `test_mesh_revolve_*` family in
`test_mesh.py`.

---

## G. `feature_edges.py` — chain graph

Feature edges are the substrate `chamfer` and `fillet` operate on. The chain
graph is built once per `MeshPart` (lazily cached) and the user selects
chains via a fluent `FeatureChainSelection`. No caller ever names a triangle
index.

### G.32 `FeatureEdge` / `FeatureChain` / `FeatureChainSelection`

**Code**: `mesh/feature_edges.py::FeatureEdge` (line 74),
`FeatureChain` (line 108), `FeatureChainSelection` (line 436).

**Invariants.**

* **`FeatureEdge`** — one mesh edge whose two adjacent triangles carry
  *different* seeded `face_id`s. Records both incident triangles, their face
  ids, their outward normals, and the "far" vertices (the vertex of each
  triangle not on the shared edge — used by the robust convexity test). The
  `pair` property is the *unordered* `(lo, hi)` face-id pair.
* **`FeatureChain`** — an ordered chain (or loop) of feature edges sharing
  one `(lo, hi)` pair. Stores `verts` (ordered vertex indices),
  `is_loop`, `edges`, plus the populated classification tags
  `convexity_class` (`convex`/`concave`/`flat`/`mixed`) and `vertex_kinds`
  (per-vertex tags: `interior`/`corner`/`endpoint`).
* **`FeatureChainSelection`** — a ShapeList-flavoured fluent collection of
  chains, exposing the chainable filters `.convex()`, `.concave()`,
  `.flat()`, `.closed()`, `.open()`, `.of_source(source)`,
  `.between(source_a, source_b)`, `.of_face(face_id)`, `.filter(predicate)`.
  Also carries the host mesh's `vertices`, `triangles`, `face_id`, and
  `side_map` so downstream consumers (the swept-tool builder) need only the
  selection to do their work.

### G.33 `extract_feature_edges` — faceID-boundary edge set

**Code**: `feature_edges.py::extract_feature_edges` (line 212).

**What it does.** Walk every mesh edge, find the ones where the two adjacent
triangles carry different `face_id`s, and return them as `FeatureEdge`s.

**Why.** Because `shape_to_manifold` seeds every triangle of every input
build123d Face with a globally unique id (preserved through every boolean),
the set of mesh edges that separate *different* ids is *exactly* the set of
analytic-face boundaries — the edges a user would name in `Part.fillet(…)`.

**How.** A single pass building an edge → `[(triangle_index, face_id)]` map,
then keep only edges with exactly two incident triangles whose face ids
differ:

```python
for tri_index, tri in enumerate(triangles):
    a, b, c = tri; fid = face_id[tri_index]
    for s, e in ((a,b), (b,c), (c,a)):
        key = (s, e) if s < e else (e, s)
        edge_tris[key].append((tri_index, fid))
...
for (v0, v1), incident in edge_tris.items():
    if len(incident) != 2: continue           # non-manifold guard
    (tri_a, fid_a), (tri_b, fid_b) = incident
    if fid_a == fid_b: continue
    shared = {v0, v1}
    far_a = next(int(v) for v in triangles[tri_a] if int(v) not in shared)
    far_b = next(int(v) for v in triangles[tri_b] if int(v) not in shared)
    feature_edges.append(FeatureEdge(...))
```

The "non-manifold guard" (skip edges incident to ≠ 2 triangles) is defensive:
on a closed 2-manifold this case never arises, but a faulty input must not
silently produce wrong chains.

**Trade-offs.** O(triangles). The `far_a` / `far_b` lookup is `O(1)`.

**Where invoked from.** `build_feature_graph` (the one-call convenience used
by `MeshPart.feature_edges`).

### G.34 `build_chains` — order edges into chains/loops

**Code**: `feature_edges.py::build_chains` (line 263).

**What it does.** Group feature edges by `(lo, hi)` pair, then within each
group order them into chains or loops by vertex adjacency.

**Why.** Two feature edges with different `(lo, hi)` pairs *must not* weld
into a single chain even if they share a vertex (that vertex is a corner —
three different pairs meeting). The grouping is therefore by pair *first*,
adjacency *second*.

**How.** For each pair group:

1. Build an adjacency map `vertex → [(neighbour_vertex, edge)]`.
2. **Start chains from degree-1 endpoints first** (open chains), then loops.
   This produces chains in a canonical orientation (open chains' walk starts
   at an endpoint, loops start arbitrarily).
3. Walk the chain, marking each visited edge, preferring continuations away
   from the immediately previous vertex:

   ```python
   cont = [(w, candidate) for (w, candidate) in adjacency[cur]
           if edge_key(candidate) not in used and w != prev]
   if not cont:
       cont = [(w, candidate) for (w, candidate) in adjacency[cur]
               if edge_key(candidate) not in used]
   ```

   The `w != prev` filter avoids immediate U-turns at a degree-2 interior;
   the fallback (drop the `w != prev` constraint) handles the closure step
   of a loop.
4. Detect "loop" iff `verts[0] == verts[-1]`; trim the repeated endpoint.

**Tests.** Implicit in every `chamfer` / `fillet` test (the chain graph is
exercised at the start of every operation).

### G.35 Convexity classification & vertex-kind classification

**Code**: `feature_edges.py::edge_convexity_sign` (line 172),
`classify_chain_convexity` (line 369), `classify_vertex_kinds` (line 400).

**Per-edge signed convexity** — robust against face-ordering noise:

```python
magnitude = abs(float(np.dot(np.cross(n_a, n_b), edge_dir)))
if edge.far_a >= 0 and edge.far_b >= 0:
    signed  = float(np.dot(n_a, vertices[edge.far_b] - p_0))
    signed += float(np.dot(n_b, vertices[edge.far_a] - p_0))
    convex = signed < 0.0
else:
    convex = float(np.dot(np.cross(n_a, n_b), edge_dir)) > 0.0
convexity = magnitude if convex else -magnitude
if magnitude < _FLAT_TOL: return 0.0
return convexity
```

The **magnitude** is the unsigned dihedral component
`|dot(cross(n_a, n_b), edge_dir)|`; the **sign** is forced by the
*far-vertex projection*: a convex edge's two far vertices each project to
the "inside" of the other face's normal (so the dot products sum to a
negative number). The cross-product sign is used only when far vertices
are unavailable — fallback for synthetic inputs.

This is the same robust test the p10 prototype validated; it is
ordering-independent and stable on chamfer/sliver-prone inputs (a plain
cross-product test flips sign with the two faces' triangle ordering).

**Per-chain convexity (`classify_chain_convexity`):** apply
`edge_convexity_sign` per edge, then:
* every non-flat sign positive → `"convex"`;
* every non-flat sign negative → `"concave"`;
* both signs present → `"mixed"` (chamfer raises; fillet splits — see H.40);
* all flat → `"flat"`.

**Per-vertex kind (`classify_vertex_kinds`):** a vertex is a **corner** iff
it appears in two or more chains *with different pair values* — design §1.3,
the substrate the A3c corner blend operates on. The kind is computed by
building `vertex → set of distinct pairs touching it` and tagging each
vertex of each chain:

```python
if len(pairs_at_vertex[vertex]) >= 2: "corner"
elif not chain.is_loop and index in (0, n-1): "endpoint"
else: "interior"
```

---

## H. `fillet.py` — chamfer (A3a) and fillet (A3b)

The design's keystone (`ddocs/design/mesh-fillet-engineering.md` §3) is **one
swept tool per chain**, not per segment. p10's per-segment construction
overlapped heavily on curved feature loops (a 63-edge bore rim emitted
1052–1720 degenerate slivers); per-chain construction shares its boundary
cross-section between successive lofts, removing the overlap by
construction.

### H.36 `_vertex_frames` — per-vertex local frame

**Code**: `mesh/fillet.py::_vertex_frames` (line 223),
`_per_vertex_face_normals` (line 178).

**What it does.** Build one `(origin, tangent, u_axis, w_axis)` frame per
chain vertex — the substrate every swept-tool builder uses.

**How.**

* **Tangent**: bisector of the incoming and outgoing edge directions for an
  interior vertex; the unique adjacent edge direction at an endpoint of an
  open chain.
* **Origin**: the vertex's 3-D position for an interior vertex; for an open
  endpoint, *overshot* by `open_overshoot` along ±tangent so the swept
  tool's end face cleanly crosses any boundary it terminates on (design
  §3.6). Per-endpoint overrides via `endpoint_overshoots: tuple[float,
  float] | None` — a **negative** value setbacks the frame *into* the chain
  by that magnitude (the A3c corner setback recipe; per_chain_setback in
  `corners.py` returns positive setbacks which fillet.py negates).
* **u_axis / w_axis**: cross products of the per-vertex face-A and face-B
  normals with the tangent, **forced to point into the solid** (away from
  the *other* face's outward normal) — identical sign convention to p10's
  `_edge_frame`:

  ```python
  u_axis = _unit(np.cross(n_a, tangent))
  w_axis = _unit(np.cross(n_b, tangent))
  if float(np.dot(u_axis, -n_b)) < 0: u_axis = -u_axis
  if float(np.dot(w_axis, -n_a)) < 0: w_axis = -w_axis
  ```

The per-vertex face normals `n_a` / `n_b` are **averaged** from incident
triangles (`_per_vertex_face_normals`) — so on a faceted curve (a bore rim)
the normal genuinely *rotates* along the chain; on a planar feature edge it
is constant. The averaging is what lets a single sweep follow a curved
chain.

### H.37 A3a chamfer wedge profile + prism-loft swept tool

**Code**: `fillet.py::_chamfer_segment_hull` (line 312),
`_build_chain_chamfer_tool` (line 353); `_wedge_profile_points` (line 894).

**Profile.** The chamfer cross-section is the right triangle
`(0, 0) – (size, 0) – (0, size)` in the `(u_axis, w_axis)` plane — the
"wedge" (design §3.4).

**Segment loft.** Lift the three corners into 3-D at each consecutive frame
pair (six points total) and take their convex hull via
`m3d.Manifold.hull_points`. That's the lofted segment — the convex hull of
two adjacent triangular cross-sections, exactly the building block §3.2
calls for.

**Per-chain assembly.** Build one frame per chain vertex with the helpers
above; loft consecutive frames; union the lofts via `batch_boolean(Add)`:

```python
for index in range(last):
    next_index = (index + 1) % n_frames
    segment = _chamfer_segment_hull(frames[index], frames[next_index], size)
    if segment is not None: segments.append(segment)
if len(segments) == 1: return segments[0]
return m3d.Manifold.batch_boolean(segments, m3d.OpType.Add)
```

For a loop the segments wrap; for an open chain there are `n_loop − 1`
segments and the terminal frames are overshot per §3.6.

### H.38 A3b fillet profile — wedge minus rolling-ball

**Code**: `fillet.py::_wedge_profile_points` (line 894),
`_ball_profile_points` (line 911), `_build_swept_arc_tool` (line 1197),
`_segment_hull_from_profiles` (line 961).

**Profile.** The fillet cross-section is the "wedge minus quarter-disc"
shape: the same right triangle as the chamfer, with the **rolling ball of
radius r centred at (r, r)** subtracted (design §3.4). This region is
**non-convex** (the arc dips *toward* the corner), so a direct `hull_points`
of the profile points would collapse it back into the chamfer wedge (the
convex hull loses the arc).

**Why "wedge minus ball" and not directly `hull_points(square_minus_arc)`.**
Direct hull of the design's "square minus quarter-disc" non-convex point set
*collapses* on convex hull because that operation cannot represent a
concavity. The wedge minus ball decomposition gives two convex pieces whose
boolean difference recovers the non-convex profile exactly — at the cost of
one extra manifold boolean per chain, which is cheap compared to the loft.

**Ball-cylinder construction (`_ball_profile_points`).**

The "ball" profile is a *full* faceted circle (not just the visible
quarter), sampled at `4 · max(segments, 2)` points around the circumference,
so the arc *facing the original edge* (from `(r, 0)` to `(0, r)`) has
exactly `segments` facets — matching the design's `n_seg` cross-section
facet count. The full circle is convex, so `hull_points` on lifted samples
exactly traces the swept ball-cylinder.

**Tool construction.**

```python
wedge_tool = _build_swept_from_profile(frames, wedge_profile, is_loop_subrun)
ball_tool  = _build_swept_from_profile(frames, ball_profile,  is_loop_subrun)
...
fillet_tool = wedge_tool - ball_tool
```

For **concave** fillets the wedge sits on the *outside* of the corner
(design §3.4: "same triangle but on the outside, translated by `(-d, -d)`
along `(-u, -w)`"), so both profile point sets are mirrored:

```python
if sign == "concave":
    wedge_profile = -wedge_profile
    ball_profile  = -ball_profile
```

### H.39 `_ribbon_mesh_from_rings` — direct-import ribbon loft

**Code**: `fillet.py::_build_swept_from_profile` (line 989),
`_ribbon_mesh_from_rings` (line 1023), `_lift_profile_to_3d` (line 939).

**What it does.** Stitch a list of lifted profile rings (one per chain
vertex) into a single closed swept-solid manifold imported directly via
`m3d.Mesh64`.

**Why this replaces per-segment `hull_points`.** For *straight* sub-runs
(2 frames) `hull_points` of two lifted rings produces the correct lofted
segment. For longer sub-runs (curved loops, multi-vertex chains)
`hull_points` per segment **loses manifold connectivity at the shared
rings** when consecutive frames rotate (the bore rim and chamfered-curve
cases of design §6.1): consecutive hulls report as separate decomposed
bodies because their shared end-ring is not bit-exact after the slight
frame rotation. This was p10's degenerate-sliver source.

The robust construction is a **direct mesh loft**: stitch the lifted rings
with side-wall quads and end-cap fans, hand the closed mesh to
`m3d.Mesh64`. The result imports as one continuous manifold of *exactly*
the swept solid, no per-segment seam issues.

**How.**

* **Side walls.** For each segment `(i, i+1)` and each profile edge `(j,
  j+1)`, two CCW triangles forming a quad:

  ```python
  a = base_i + j; b = base_i + jn
  c = base_j + jn; d = base_j + j
  tris.append((a, b, c))
  tris.append((a, c, d))
  ```

* **End caps** (open sub-run only): fan triangulation from vertex 0 of each
  ring; the near cap is wound to face along `-tangent`, the far cap along
  `+tangent`. The profile is convex by construction, so a fan from any one
  vertex is non-self-intersecting.

* **Winding fallback.** If the imported manifold has *negative* volume, the
  winding is inverted and the mesh is rebuilt with `tris[:, ::-1]` — the
  same fallback p10 used. This handles the case where the (u, w) axes were
  swapped by the sign convention.

### H.40 `_split_chain_by_sign` — per-edge sign splitting

**Code**: `fillet.py::_split_chain_by_sign` (line 1107),
`_edge_signs` (line 1102).

**What it does.** Split a chain into single-sign sub-runs (design §3.4 /
§8.3), one per maximal run of edges sharing a convexity sign. Near-flat
edges are skipped — they neither cut nor fill.

**Why.** A3a chamfer requires uniform-sign chains (chamfer cannot mix in
one chain — `_check_feasibility` raises `mixed-convexity`); A3b fillet
**does not** raise mixed: it splits the chain at the sign flip into
single-sign sub-runs, each built as its own swept tool, and routes the
result to the convex-cut or concave-add batch.

**Convex-cut-then-concave-add ordering (p10 fix #3).** The post-build
booleans apply cut first, then add. Doing it the other way around can
re-introduce material the cut had to remove (the concave fill would then
be cut away). This ordering is the p10 fix #3 carried through to A3b:

```python
result = meshpart.manifold
if cut_tools:
    combined_cut = batch_boolean(cut_tools, OpType.Add) if len > 1 else cut_tools[0]
    result = result - combined_cut
if add_tools:
    combined_add = batch_boolean(add_tools, OpType.Add) if len > 1 else add_tools[0]
    result = result + combined_add
```

**How.** Walk the chain's edges in path order; for each edge tag it
`convex` / `concave` / `None` (flat). A flat edge **closes** any in-progress
sub-run. A sign change closes the previous sub-run and starts a new one
that begins at the *shared boundary vertex* (so the two adjacent sub-runs'
tools meet at exactly the same vertex).

A subtle case: a uniformly-non-flat **loop** is returned as one sub-run
covering the whole vertex list (loop semantics preserved); only a mixed
loop is broken into open arcs.

### H.41 Feasibility pre-flight; `MeshFilletInfeasible`

**Code**: `fillet.py::MeshFilletInfeasible` (line 117),
`_half_thickness_feasibility` (line 425), `_check_feasibility` (line 486),
`_check_fillet_feasibility` (line 1306).

**The exception.** `MeshFilletInfeasible(ValueError)` with structured
attributes: `chains`, `constraint`, `requested`, `measured`. The P3 contract:
**raise — never silently clamp**. A3a/A3b only support `on_infeasible="raise"`;
`"skip"` lands in A4.

**Three constraints (design §5.1).**

* **half-thickness** — bisector-proxy distance from each chain vertex must
  exceed `2 · size`. The check uses an *approximate but conservative* proxy:
  the minimum host-mesh vertex distance along the inward bisector
  `-(n_a + n_b)`, restricted to vertices roughly on-axis (radial offset <
  `1.5 · size`). This is simpler than the design's full "min_gap" query and
  never *passes* a thin plate that the tool would actually breach.
* **chain-length** — an open chain must be ≥ `2 · size` (or its
  cross-section frame folds).
* **mixed-convexity** — chamfer raises; **fillet does not** (it splits — see
  H.40).

Corner-level pre-flight (`_check_corner_feasibility`, see I.47) raises on
**mixed corners** (`mixed-corner`) and **k > 6 corners** (`k>6-corner`).

---

## I. `corners.py` — multi-chain corner blending (A3c)

At a vertex where three or more feature chains of different `(lo, hi)`
pairs meet, the per-chain swept tools each terminate at the vertex and
leave a tiny pyramidal point sticking out into the middle of where the
ball/flat corner should be (p10's `q_min=0.001` thin-patch failure). The
A3c construction follows the classical ACIS / Spatial "setback vertex
blending" recipe (design §4.2):

1. **Setback** each incident chain's tool back from the corner by `s`.
2. **Drop a vertex patch** that fills the corner gap — sphere for fillet,
   flat polyhedron for chamfer.
3. **Union** the setback tubes with the vertex patch into the combined cut /
   add batch.

### I.42 `detect_corners` — vertices with ≥3 distinct chain pairs

**Code**: `mesh/corners.py::detect_corners` (line 178);
`_classify_corner` (line 288); `_face_normals_at_vertex` (line 301);
`ChainEndpointAtCorner` (line 97); `Corner` (line 125).

**What it does.** Scan the selection's chains for multi-chain corners and
classify each one (`convex` / `concave` / `mixed` / `degenerate`).

**Why ≥ 3 distinct pairs.** A k=2 vertex is a chain bend (two chains of
different pairs meeting), not a junction — the two swept tubes meet there
naturally and adding a sphere/polyhedron patch would *over-cut* the body.
The design specifies "corner = the meeting of ≥ 3 chains"; vertices with
only one or two chains in the *current selection* are not corners for this
run (the user can include the third chain in the selection to opt into the
patch construction).

**How.** Walk every chain's open endpoints (loops have no endpoints):

```python
first_edge_sign = edge_convexity_sign(vertices, chain.edges[0])
last_edge_sign  = edge_convexity_sign(vertices, chain.edges[-1])
if abs(first_edge_sign) >= _SIGN_TOL:
    endpoints_at[start_v].append(ChainEndpointAtCorner(
        chain=chain, side="start",
        convex=first_edge_sign > 0.0,
        tangent_into_chain=start_tangent))
    pairs_at[start_v].add(chain.pair)
# (same for end_v)
```

The endpoint-local sign is the sign of the *edge incident to the corner* —
a mixed chain can still contribute a single consistent sign at its corner
endpoint.

After collection, vertices with `len(pairs_at[v]) < 3` are dropped (not
corners by the ≥ 3 rule). For each surviving vertex:
* `_face_normals_at_vertex(...)` averages the outward triangle normals per
  *distinct face id* met at the corner (the union of every incident chain's
  pair) — one unit normal per distinct face.
* `_classify_corner(...)` is the small switch on `(has_convex, has_concave,
  k > MAX_CORNER_CHAINS)` returning the `CornerKind`.

### I.43 `compute_setback` — `s = min(size, 0.45·L)` per chain

**Code**: `mesh/corners.py::compute_setback` (line 351),
`_chain_total_length` (line 165);
`_SETBACK_MAX_FRACTION = 0.45` (line 90).

**What it does.** Pick a per-corner setback distance `s` — clamped per
incident chain length so the setback never consumes more than 45% of any
incident chain.

```python
s = float(size)
for ep in corner.chain_endpoints:
    chain_len = _chain_total_length(ep.chain, vertices)
    if chain_len <= 1e-9: continue
    s = min(s, _SETBACK_MAX_FRACTION * chain_len)
return max(s, 0.0)
```

The design's default is `s = r` (fillet) or `s = d` (chamfer), clamped to
`0.45·L`. Returns 0 if every incident chain is shorter than the required ε —
the caller treats that as degenerate and skips the patch.

### I.44 `per_chain_setback` — apply as negative endpoint overshoots

**Code**: `mesh/corners.py::per_chain_setback` (line 383).

**What it does.** For a given chain, return the `(start_setback,
end_setback)` to apply — the fillet/chamfer dispatcher then passes these as
**negative** `endpoint_overshoots` to `_vertex_frames`, which already
supports per-endpoint overshoot overrides. This is how a positive corner
setback is plumbed into the existing frame-overshoot code path without
adding a parallel mechanism.

Loops and chains shorter than 2 vertices return `(0.0, 0.0)`.

```python
if start_corner is not None and start_corner.kind in ("convex", "concave"):
    start_s = compute_setback(start_corner, size, vertices)
if end_corner is not None and end_corner.kind in ("convex", "concave"):
    end_s = compute_setback(end_corner, size, vertices)
return start_s, end_s
```

### I.45 `build_corner_fillet_patch` — faceted sphere

**Code**: `mesh/corners.py::build_corner_fillet_patch` (line 450),
`_inward_unit` (line 425); `_CORNER_SPHERE_SEG_BIAS = 4` (line 94).

**What it does.** Build a faceted sphere centred at the inset position
`c = v ± r · unit(-Σ face_normals)` — design §4.3 step 5.

**Centre placement.**

* **Convex** corner: outward face normals all point *out* of the body, so
  `-Σ n_f` points inward. The sphere centre is `c = v + r · inward`, so the
  sphere is *tangent* to all incident faces at distance `r`. Subtracting it
  rounds the corner.
* **Concave** corner: mirror — the sphere sits in the missing-material
  void, at `c = v - r · inward`. Adding it fills the dihedral.

```python
n_seg = max(int(segments) + _CORNER_SPHERE_SEG_BIAS, 8)
ball = m3d.Manifold.sphere(float(radius), circular_segments=n_seg)
ball = ball.translate(tuple(float(x) for x in centre))
```

`segments + 4` (the `_CORNER_SPHERE_SEG_BIAS`) makes the sphere at least as
smooth as the adjacent rolled-arc strips — the patch and the strips meet
without visible facet-density discontinuity.

Returns `None` if the inward sum is degenerate (all face normals cancel —
the corner is geometrically pathological); the caller falls back to the
no-patch behaviour and the chains' open-overshoot covers the gap.

### I.46 `build_corner_chamfer_patch` — generalised pyramid

**Code**: `mesh/corners.py::build_corner_chamfer_patch` (line 495).

**What it does.** For a k-edge convex chamfer corner: a *generalised
pyramid* with apex at the corner vertex `v` and base vertices at
`v + s · tangent_into_chain` along each incident chain. `Manifold.hull_points`
of `[apex, base_0, base_1, …, base_{k-1}]` is the planar-faced
tetrahedron/pyramid; the cut boolean removes it, leaving the classical
chamfer-corner facet:

```python
apex = corner.position.copy()
base = [apex + s * ep.tangent_into_chain for ep in corner.chain_endpoints]
if corner.kind == "concave":
    centroid = np.mean(np.array(base), axis=0)
    apex = 2.0 * centroid - apex            # reflect across the base
points = np.vstack([apex.reshape(1,3), np.array(base)])
hull = m3d.Manifold.hull_points(points.tolist())
```

For a **concave** corner the apex is **reflected** across the base's
centroid; the add boolean pyramid then points *into* the void rather than
into the solid body.

Returns `None` on a degenerate setback or a hull failure (e.g. coplanar
base points — a k=3 corner with all three chain tangents in one plane).

### I.47 `_check_corner_feasibility` — mixed / k>6 raise (P3)

**Code**: `mesh/fillet.py::_check_corner_feasibility` (line 548).

**What it does.** Enforce the design's NG_F4 (mixed corners) and NG_F5
(k > 6 corners) non-goals: both raise `MeshFilletInfeasible` with a
one-line workaround.

```python
if corner.kind == "mixed":
    raise MeshFilletInfeasible(
        f"corner at vertex {corner.vertex} has both convex and concave "
        "chains incident; … Fillet the convex and concave chains in "
        "separate calls so each call's corners are consistent.",
        chains=chains, constraint="mixed-corner", requested=size)
if corner.kind == "degenerate":
    raise MeshFilletInfeasible(
        f"corner at vertex {corner.vertex} has {corner.k} incident chains "
        f"(> {MAX_CORNER_CHAINS}); … Reduce the corner's edge count or "
        "split the selection.",
        chains=chains, constraint="k>6-corner", requested=size)
```

Both cases require analytic tooling we do not have on a faceted mesh; the
design specifies a clean raise with a workaround in the message.

**Tests.** `test_mesh_fillet_k_too_many_chains_raises`,
`test_mesh_fillet_l_shape_mixed_corner_raise_lists_offenders`.

### I.48 `_drop_zero_volume_artifacts` — precision-pinch post-pass

**Code**: `mesh/fillet.py::_drop_zero_volume_artifacts` (line 830).

**What it does.** Drop near-zero-volume components from a manifold.

**Why.** A3c's corner-blend tools occasionally leave tiny zero-volume
surface fragments after the batched boolean (sphere-vs-tube precision pinch
points). These are not geometrically meaningful — every component with
`|volume| > 1e-6` is the real body — but they make `manifold.decompose()`
over-count. The post-pass recomposes just the substantial components so
callers see one body when there is one body.

**How.**

```python
components = manifold.decompose()
if len(components) <= 1: return manifold
real_components = [c for c in components if abs(c.volume()) > threshold]
if not real_components: return manifold
if len(real_components) == len(components): return manifold
if len(real_components) == 1: return real_components[0]
return m3d.Manifold.batch_boolean(real_components, m3d.OpType.Add)
```

**Tests.** Implicit via the fillet/chamfer corner tests
(`test_mesh_fillet_box_all_edges_corner_blended`, etc.) — they assert the
output is a single body, which is only true if the precision-pinch
artefacts are dropped.

---

## Appendix — file map and call graph

```
src/build123d/topology/
├── shape_core.py
│   ├── Shape.tessellate(weld=, relative=)   [A.1]
│   ├── Shape.mesh(relative=)                [A.2]
│   ├── Shape.__add__ / __sub__ / __and__    [A.6]  → NotImplemented for non-Shape
│   └── _weld_mesh                           [A.1]
└── three_d.py
    ├── Solid.from_mesh                      [A.3]
    ├── _connected_components (vertex-conn)  [A.4]
    └── _group_shells_into_solids            [A.5]

src/build123d/mesh/
├── __init__.py             — guarded manifold3d import, is_available()
├── bridge.py               [B] OUT leg
│   ├── FaceRecord / SideMap / _analyse_face [B.7]
│   ├── _weld                                [B.8]
│   ├── shape_to_manifold / _build_manifold  [B.9]
│   └── ResultMesh / read_result             [B.10]
├── recovery.py             [C] IN leg
│   ├── _connected_components (edge-conn)    [C.11]
│   ├── _boundary_loops                      [C.12]
│   ├── _project_to_plane                    [C.13]
│   ├── _exact_plane                         [C.14]
│   ├── _recover_planar_face                 [C.15]
│   ├── _faceted_patch                       [C.16]
│   └── recover_brep                         [C.17]
├── mesh_part.py            [D] value type
│   ├── MeshPart                             [D.18]
│   ├── from_part / from_mesh / primitives   [D.19]
│   ├── operators / mesh_fuse/cut/intersect  [D.20]
│   ├── translate/rotate/scale/move          [D.21]
│   ├── to_solid / to_part                   [D.22]
│   ├── faces / analytic_faces / faces_from  [D.23]
│   └── export_stl + _write_binary/ascii_stl [D.24]
├── ops.py                  [E, F (extrude/revolve), D.25-26 (offset/shell)]
│   ├── mesh_hull                            [E.27]
│   ├── mesh_minkowski / _minkowski_via_decomposition [E.28]
│   ├── mesh_minkowski_difference            [E.29]
│   ├── mesh_extrude / mesh_revolve          [F.31]
│   └── mesh_offset / mesh_shell             [D.25/26]
├── sketch2d.py             [F.30]
├── feature_edges.py        [G] chain graph
│   ├── FeatureEdge/Chain/Selection          [G.32]
│   ├── extract_feature_edges                [G.33]
│   ├── build_chains                         [G.34]
│   └── classify_chain_convexity / vertex_kinds [G.35]
├── fillet.py               [H] chamfer (A3a) + fillet (A3b) + corner integration
│   ├── _vertex_frames / _per_vertex_face_normals [H.36]
│   ├── _build_chain_chamfer_tool            [H.37]
│   ├── _build_swept_arc_tool                [H.38]
│   ├── _ribbon_mesh_from_rings              [H.39]
│   ├── _split_chain_by_sign                 [H.40]
│   ├── _check_feasibility / MeshFilletInfeasible [H.41]
│   ├── _check_corner_feasibility            [I.47]
│   ├── _drop_zero_volume_artifacts          [I.48]
│   └── mesh_chamfer / mesh_fillet           — top-level entry points
└── corners.py              [I] A3c corner blending
    ├── detect_corners                       [I.42]
    ├── compute_setback                      [I.43]
    ├── per_chain_setback                    [I.44]
    ├── build_corner_fillet_patch            [I.45]
    └── build_corner_chamfer_patch           [I.46]
```

