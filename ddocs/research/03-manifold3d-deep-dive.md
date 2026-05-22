# Manifold3D Deep Dive

> Research note for bringing `manifold3d` mesh operations into `build123d`, and for
> retargeting `scad2py` onto a build123d backend.
>
> **Introspection target:** `manifold3d` **2.3.1** (`cp310-cp310-macosx_11_0_arm64`
> wheel) installed at
> `/opt/homebrew/Caskroom/miniforge/base/lib/python3.10/site-packages/manifold3d.cpython-310-darwin.so`.
> All signatures and snippets below were run against that wheel unless explicitly
> marked **[upstream]**, in which case they describe the current 3.4.x line
> (latest release 3.4.1, March 2026) verified via the project repo / docs.

---

## 0. What Manifold is, in one paragraph

[Manifold](https://github.com/elalish/manifold) (by Emmett Lalish, Apache-2.0) is a
C++ geometry kernel whose entire reason for existing is a **mesh Boolean algorithm
that is provably robust and produces a guaranteed-manifold result**. A "manifold"
triangle mesh is one that bounds a solid: every edge is shared by exactly two
triangles, the surface is consistently oriented, and there are no self-intersections
left dangling. Robust mesh Booleans were an open problem for years; Manifold solves it
with an ε-tolerance numeric model plus careful topology bookkeeping. It is the engine
inside **OpenSCAD's modern backend** (replacing CGAL), and is also used by IFC.js,
Grid.Space, Blender add-ons, etc. The Python wheel is `manifold3d` (built with
nanobind in current versions; the 2.3.1 wheel still used pybind11-era scikit-build).
It is *not* a BREP kernel — there are no exact curves, no NURBS, no fillet/chamfer;
everything is faceted triangles. It is ~1000x faster than CGAL on typical CSG.

The unit of comparison for build123d is OpenCASCADE (OCC): OCC is an exact BREP
kernel (NURBS surfaces, exact circles, fillets). Manifold is a fast faceted
triangle-soup kernel. They are complementary, not substitutes — see
**§8 Implications for build123d**.

---

## 1. Core types

`import manifold3d` exposes exactly these public names (2.3.1):

```python
>>> [x for x in dir(manifold3d) if not x.startswith('_')]
['CrossSection', 'Error', 'FillRule', 'JoinType', 'Manifold', 'Mesh',
 'get_circular_segments', 'set_circular_segments',
 'set_min_circular_angle', 'set_min_circular_edge_length', 'triangulate']
```

There is **no `__version__` attribute** — get the version from package metadata
(`importlib.metadata.version("manifold3d")`). No `.pyi` stub ships with the wheel;
the only authoritative API surface is the docstrings baked into the `.so` (which is
what this document is mostly distilled from).

### 1.1 `Manifold` — the 3D solid

`Manifold` is a handle to an immutable 3D solid. Operations return new `Manifold`s;
nothing mutates in place. Crucially, **transforms and many operations are lazy** —
they are accumulated and only evaluated when geometry data is actually demanded
(`to_mesh()`, `volume()`, `num_tri()`, a Boolean, etc.). This is why a chain of 500
Booleans can "build" in 6 ms and then take 0.43 s when you finally call `to_mesh()`.

Constructors:

```python
Manifold()                                    # empty
Manifold(mesh: Mesh, property_tolerance=[])    # import a triangle mesh
Manifold.cube(size=[1,1,1], center=False)
Manifold.cylinder(height, radius_low, radius_high=-1, circular_segments=0, center=False)
Manifold.sphere(radius, circular_segments=0)   # geodesic, refined octahedron
Manifold.tetrahedron()
```

Query/measurement methods (all observed signatures):

| Method | Signature | Notes |
|---|---|---|
| `is_empty` | `() -> bool` | No triangles? |
| `num_vert` / `num_edge` / `num_tri` | `() -> int` | Topology counts |
| `num_prop` | `() -> int` | Properties per vertex (incl. 3 for position) |
| `num_prop_vert` | `() -> int` | ≥ `num_vert`; verts split on property seams |
| `volume` | `() -> float` | Signed volume; clamped to 0 within precision |
| `surface_area` | `() -> float` | |
| `bounding_box` | `() -> tuple` | `(xmin,ymin,zmin,xmax,ymax,zmax)` |
| `genus` | `() -> int` | # handles; call `decompose()` first |
| `precision` | `() -> float` | ε — see §3 |
| `status` | `() -> Error` | Why an imported mesh became empty |
| `original_id` | `() -> int` | mesh ID if "original", else `-1` |

### 1.2 `CrossSection` — the 2D region

`CrossSection` is the 2D analog: a set of closed polygon contours (outer + holes)
with no self-intersections. Backed by the **Clipper2** library. It is the bridge
between 2D and 3D — `extrude`/`revolve` turn a `CrossSection` into a `Manifold`,
and `slice`/`project` go the other way.

```python
CrossSection()                                          # empty
CrossSection(contours: list[FloatNx2], fillrule=FillRule.Positive)
CrossSection.square(size=[x,y], center=False)
CrossSection.circle(radius, circular_segments=0)
```

The contour constructor *runs a boolean union* with the given fill rule, so the
output is always clean (no self-intersections) even if your input polygons overlap.

### 1.3 `Mesh` — the raw data exchange struct

`Mesh` (2.3.1) is the GL-style interleaved-buffer struct — it *is* what upstream
calls `MeshGL`. **There is no separate `MeshGL` class in 2.3.1.** It is a plain data
container: numpy arrays in, numpy arrays out, no geometric operations.

Constructor (full observed signature):

```python
Mesh(vert_properties: ndarray[float32, (N, num_prop)],
     tri_verts:       ndarray[uint32,  (M, 3)],
     merge_from_vert: ndarray[uint32, (*,)]  | None = None,
     merge_to_vert:   ndarray[uint32, (*,)]  | None = None,
     run_index:       ndarray[uint32, (*,)]  | None = None,
     run_original_id: ndarray[uint32, (*,)]  | None = None,
     run_transform:   ndarray[float32,(*,4,3)] | None = None,
     face_id:         ndarray[uint32, (*,)]  | None = None,
     halfedge_tangent:ndarray[float32,(*,3,4)] | None = None,
     precision: float = 0) -> None
```

Read-back properties (all read-only views):

| Field | Type out | Meaning |
|---|---|---|
| `vert_properties` | `float32 (N, num_prop)` | Interleaved per-vertex data. **Cols 0–2 are X,Y,Z**; cols 3+ are user properties (color, normals, curvature…). |
| `tri_verts` | `int32 (M, 3)` | Triangle → vertex indices (CCW). |
| `merge_from_vert` / `merge_to_vert` | `list[int]` | Pairs telling Manifold which property-split verts are the *same physical vertex*. Used to reconstruct topology on import without re-welding by distance. |
| `run_index` | `list[int]` | Offsets into `tri_verts` delimiting **runs** — contiguous triangle ranges that came from one original mesh / material. Length = #runs + 1. |
| `run_original_id` | `list[int]` | The `original_id` for each run — i.e. *which source solid* those triangles came from. This is the multi-material / provenance channel. |
| `run_transform` | `float32 (#runs, 4, 3)` | The accumulated affine transform applied to each run's source mesh. |
| `face_id` | `list[int]` | Per-triangle ID grouping coplanar triangles into logical "faces" of the original input. |
| `halfedge_tangent` | `float32 (#tri, 3, 4)` | Bézier tangents for smooth surfaces (present after `smooth()`); empty otherwise. |
| `precision` | `float` | ε of this mesh (called `tolerance` in newer versions). |

`num_prop` is inferred from `vert_properties.shape[1]`.

**Concrete round-trip** (verified):

```python
import manifold3d as m
u = m.Manifold.cube([2,2,2], True) + m.Manifold.sphere(1.3, 32)
mesh = u.to_mesh()
mesh.vert_properties.shape   # (398, 3)   float32
mesh.tri_verts.shape         # (792, 3)   int32
list(mesh.run_index)         # [0, 720, 2376]   -> two runs
list(mesh.run_original_id)   # [2, 4]            -> cube id 2, sphere id 4
mesh.run_transform.shape     # (2, 4, 3)
m.Manifold(mesh).volume()    # 9.903  -> lossless round-trip
```

`to_mesh(normal_idx=[0,0,0])` — if your input meshes carried normal vectors in
specific property channels, pass their indices and the output normals will be
re-derived consistent with the applied transforms and front/back orientation.

**[upstream]** 3.x split this into `MeshGL` (float32 / uint32) and `MeshGL64`
(float64 / uint64), because the *internal kernel switched to double precision*.
`Manifold.to_mesh()` / `to_mesh64()` and matching constructors exist. `MeshGL`
also gained `merge()`, `backside(run)`, `has_normals(run)` helper methods and a
`run_flags` array. **For build123d this matters: 3.x removes the single-precision
limitation of 2.3.1** (see §5/§6).

### 1.4 numpy data flow summary

Everything crosses the boundary as numpy arrays. Going **in**: build `float32`
`(N,P)` vertex array + `uint32` `(M,3)` triangle array → `Mesh` → `Manifold`.
Going **out**: `Manifold.to_mesh()` → read `.vert_properties` / `.tri_verts`.
The read-back arrays are zero-copy *views* into kernel memory and are
non-writable; copy them (`np.array(...)`) before mutating.

---

## 2. Operations

### 2.1 Booleans (3D)

```python
a + b      # union          (__add__)
a - b      # difference     (__sub__)
a ^ b      # intersection   (__xor__)
```

In **2.3.1 the only Boolean API is the three operators** — there is no `boolean()`
or `batch_boolean()` method (`hasattr(Manifold,'boolean') == False`). `compose()`
exists for a *lazy union without resolving intersections* (purely topological — you
must guarantee non-overlap yourself).

**[upstream]** 3.x adds `Manifold.batch_boolean(manifolds, op: OpType)` and an
`OpType` enum (`Add`, `Subtract`, `Intersect`). `batch_boolean` is materially
faster than folding `+`/`-` in Python because it resolves N solids in one pass and
avoids rebuilding intermediate results — relevant for scad2py, which emits
`union()`/`difference()` with many children.

```python
# verified 2.3.1: deep boolean tree, lazy until to_mesh()
result = m.Manifold.cube([20,20,20], True)
for i in range(500):
    result = result - m.Manifold.sphere(1.2, 24).translate([...])
mesh = result.to_mesh()      # forces eval: 0.43 s, 107k triangles
```

`split(cutter) -> (intersection, difference)` and
`split_by_plane(normal, origin_offset) -> (positive_side, negative_side)` do two
Booleans in one pass. `trim_by_plane(normal, origin_offset)` keeps only the positive
half-space — the cheap way to cut a solid with a plane.

### 2.2 Convex hull

```python
a.hull()                          -> Manifold        # hull of one solid
Manifold.batch_hull([a, b, c])    -> Manifold        # hull enveloping many
Manifold.hull_points(pts: FloatNx3) -> Manifold      # hull of a point cloud
```

Uses the `quickhull` library. `hull_points` of < 4 points or coplanar points →
empty. `CrossSection` has the 2D equivalents (`hull`, `batch_hull`, `hull_points`).

**This is the building block for Minkowski sums** — see §6.

### 2.3 Transforms

All transforms are **lazy and chainable** (combined into one matrix, applied at
eval time). Rotations in **degrees**, with exact paths for multiples of 90°.

| Method | Signature |
|---|---|
| `translate` | `(t: Floatx3) -> Manifold` |
| `rotate` | `(v: Floatx3) -> Manifold` — Euler XYZ degrees |
| `scale` | `(v: Floatx3) -> Manifold` *or* `(s: float)` |
| `transform` | `(m: Float3x4) -> Manifold` — 3×3 + translation column |
| `mirror` | `(normal: Floatx3) -> Manifold` — reflect over plane through origin |
| `warp` | `(f: Callable[[vec3], vec3]) -> Manifold` — per-vertex Python callback |
| `warp_batch` | `(f: Callable[[FloatNx3], None]) -> Manifold` — in-place numpy mutate |

`warp` does **not** add topology — it only moves existing vertices, so refine
first if you want a smooth deformation. `warp_batch` hands you the whole `(N,3)`
array to mutate in place — much faster than per-vertex `warp`. Verified twist:

```python
cube = m.Manifold.cube([2,2,4], True).refine(8)
def twist(verts):                       # verts: float32 (N,3), mutate in place
    for i in range(len(verts)):
        ang = verts[i,2] * 0.4
        x, y = verts[i,0], verts[i,1]
        verts[i,0] =  x*np.cos(ang) - y*np.sin(ang)
        verts[i,1] =  x*np.sin(ang) + y*np.cos(ang)
twisted = cube.warp_batch(twist)         # 386 verts, vol 15.76
```

### 2.4 Refinement & smoothing

| Method | Signature | Effect |
|---|---|---|
| `refine` | `(n: int) -> Manifold` | Split every edge into `n` → `n²` triangles per face. Coplanar unless tangents present. |
| `smooth` *(static)* | `(mesh, sharpened_edges=[], edge_smoothness=[]) -> Manifold` | Builds Bézier `halfedge_tangent`s for a smooth interpolation. Resolution unchanged — combine with `refine`. Per-halfedge crease control. |
| `calculate_curvature` | `(gaussian_idx, mean_idx) -> Manifold` | Stores Gaussian & mean curvature into named property channels. |

`smooth` + `refine` together is the standard "make this faceted thing curved"
recipe; tangents make `refine` push new verts onto the interpolated surface:

```python
sm  = m.Manifold.smooth(cube.to_mesh())   # adds halfedge_tangent (12,3,4)
ref = sm.refine(4)                        # 192 tris, rounded corners
```

**[upstream]** 3.x adds `smooth_out(min_sharp_angle=60, min_smoothness=0)` (auto
smoothing without manually listing edges), `smooth_by_normals(normal_idx)`,
`calculate_normals(normal_idx=0, min_sharp_angle=60)`, `refine_to_length(length)`
and `refine_to_tolerance(tolerance)`. None of these exist in 2.3.1
(`hasattr` all `False`).

### 2.5 2D → 3D and 3D → 2D

```python
CrossSection.extrude(height, n_divisions=0, twist_degrees=0, scale_top=(1,1)) -> Manifold
CrossSection.revolve(circular_segments=0, revolve_degrees=360.0)              -> Manifold
Manifold.slice(height: float)   -> CrossSection   # planar section at Z=height
Manifold.project()              -> CrossSection   # shadow onto XY plane
```

Note `extrude`/`revolve` are **methods on `CrossSection`** in 2.3.1, not on
`Manifold`. (Upstream 3.x *also* exposes `Manifold.extrude(cs, ...)` /
`Manifold.revolve(cs, ...)` static forms — both work.) `extrude` supports a twist
and an independent top-scale (`scale_top=(0,0)` gives a cone with a single apex).
`revolve` spins the cross-section about its Y-axis; only the +X side is used if the
profile crosses the axis. Verified:

```python
m.CrossSection.circle(1.0, 64).extrude(3.0, twist_degrees=45, n_divisions=10)
# -> Manifold, 768 verts, volume 9.413
m.CrossSection.square([1,2]).translate([1,0]).revolve(circular_segments=48)
# -> Manifold, 192 verts, volume 18.796
```

### 2.6 CrossSection 2D operations

| Method | Signature | Notes |
|---|---|---|
| `+ - ^` | union / difference / intersection | Clipper2-backed |
| `offset` | `(delta, join_type, miter_limit=2.0, circular_segments=0) -> CrossSection` | Inflate/deflate; `JoinType` ∈ {Miter, Round, Square}. Positive grows outer, shrinks holes. |
| `simplify` | `(epsilon=1e-6) -> CrossSection` | Drop near-collinear / near-dup verts. Recommended after `offset`. |
| `hull` / `batch_hull` / `hull_points` | as 3D | |
| `decompose` | `() -> list[CrossSection]` | Split into topologically separate regions. |
| `to_polygons` | `() -> list[FloatNx2]` | Contour read-back. |
| `area` / `bounds` / `num_vert` / `num_contour` / `is_empty` | queries | |

`offset` is the 2D engine you'd use for 2D fillet-ish rounding (`JoinType.Round`)
and for the 2D side of Minkowski. Verified: a centered 2×2 square offset by 0.5
(Round) → area 8.78, 1 contour.

### 2.7 Composition, decomposition, level sets

```python
Manifold.compose([a, b, ...])  -> Manifold        # lazy topological union
m.decompose()                  -> list[Manifold]  # connected components
Mesh.level_set(f, bounds, edgeLength, level=0.0) -> Mesh   # SDF -> mesh
```

`level_set` is a **static method on `Mesh`** that runs Marching Tetrahedra over a
body-centered cubic grid (chosen over Marching Cubes specifically because it yields
manifold output). `f` is a Python `def sdf(x,y,z) -> float` (positive inside).
`edgeLength` controls grid density and dominates runtime. Verified:

```python
def sphere_sdf(x,y,z): return 1.0 - (x*x+y*y+z*z)**0.5
ls  = m.Mesh.level_set(sphere_sdf, [-1.5]*3+[1.5]*3, edgeLength=0.1)
man = m.Manifold(ls)        # 7958 verts, 15912 tris, vol 4.178, 87 ms
```

This is a clean SDF→solid path that OCC has no equivalent for. Caveat: if the
isosurface exits `bounds`, you get an egg-crate cap from the grid.

### 2.8 `as_original`, identity & multi-material

```python
m.original_id()  -> int     # this solid's mesh ID, or -1 if it's a boolean product
m.as_original()  -> Manifold # collapse coplanar faces, become a fresh "original"
Manifold.reserve_ids(n) -> int  # reserve n sequential unique mesh IDs
```

An "original" mesh has a stable `original_id`; a Boolean *product* has `-1`.
`reserve_ids` + setting `run_original_id` lets you tag triangle runs by source —
this is exactly how OpenSCAD tracks per-object material/color through Booleans.
`as_original` re-merges coplanar faces and resets identity (keeps properties; an
edge is preserved if properties differ across it). Verified:

```python
cube = m.Manifold.cube()
cube.original_id()                 # 2
(cube + sphere).original_id()      # -1   (a product)
cube.as_original().original_id()   # 13   (new original)
```

---

## 3. Robustness model

This is the heart of Manifold and the reason to adopt it.

### 3.1 The manifoldness guarantee

Every operation that *produces* a `Manifold` produces a watertight, consistently
oriented 2-manifold triangle mesh — or an empty one. Booleans never emit
self-intersections, T-junctions, or non-2-manifold edges. This is a *guarantee*,
not a heuristic. The flip side: **the guarantee is on the output, conditional on
input**. Importing an arbitrary mesh can fail.

### 3.2 ε-tolerance / precision

Manifold's numeric model is **ε-valid**: each `Manifold` carries a `precision()`
value ε, an upper bound on accumulated rounding error from every transform and
operation that produced it. Any triangle that is collinear/degenerate *within ε*
is treated as degenerate and removed. `volume()`/`surface_area()` clamp to 0
within ε. Verified — ε **scales with model size**:

```python
m.Manifold.cube([0.001]*3).precision()   #  1.0e-08
m.Manifold.cube([1,1,1]).precision()     #  ~2.0e-07  (after a boolean)
m.Manifold.cube([1000]*3).precision()    #  1.0e-02
```

So ε is roughly `bbox_size * machine_epsilon`-ish, growing through operations.
In 2.3.1 `precision` is computed by the kernel and read-only. **[upstream]** 3.x
renames the concept to *tolerance*, exposes `set_tolerance(t)` / `get_tolerance()`,
and lets you *deliberately raise* tolerance to simplify a mesh (collapse small
features). `Mesh` already has a `precision`/`tolerance` constructor argument in
2.3.1 — you can declare how uncertain your imported coordinates are.

`property_tolerance` (a per-property-channel vector passed to `Manifold(mesh,…)`)
is the analogous ε for *non-position* properties: it sets how much interpolation
error is allowed before two coplanar triangles are considered to straddle a
property boundary (and thus that edge is kept). Default 1e-5, tuned for
properties in [-1,1] (e.g. normals, colors).

### 3.3 `status` / `Error` enum

`Manifold.status()` returns why an *imported* mesh became empty. The full enum
(verified, with integer values):

| Value | # | Meaning |
|---|---|---|
| `NoError` | 0 | OK (or legitimately empty) |
| `NonFiniteVertex` | 1 | NaN/Inf coordinate |
| `NotManifold` | 2 | Open mesh / non-2-manifold edge / inconsistent orientation |
| `VertexOutOfBounds` | 3 | Triangle indexes a nonexistent vertex |
| `PropertiesWrongLength` | 4 | `vert_properties` size mismatch |
| `MissingPositionProperties` | 5 | `num_prop < 3` |
| `MergeVectorsDifferentLengths` | 6 | `merge_from`/`merge_to` length mismatch |
| `MergeIndexOutOfBounds` | 7 | merge index invalid |
| `TransformWrongLength` | 8 | `run_transform` size wrong |
| `RunIndexWrongLength` | 9 | `run_index` size wrong |
| `FaceIDWrongLength` | 10 | `face_id` size wrong |
| `InvalidConstruction` | 11 | other constructor failure |

**[upstream]** 3.x adds `ResultTooLarge`, `InvalidTangents`, `Cancelled`.

Important subtlety from the docstring: `status` **only applies to a Manifold
freshly created from an imported `Mesh`**. Once that Manifold is combined into a
new one via operations, `status` reverts to `NoError` and the bad input is simply
treated as empty geometry. So *check `status()` immediately after import*.

### 3.4 How bad input is handled — verified

```python
# open mesh (single triangle, not a solid)
m.Manifold(m.Mesh(tri_pts, [[0,1,2]])).status()      # Error.NotManifold,        is_empty True
# collinear / zero-area triangle
m.Manifold(m.Mesh(colinear, [[0,1,2]])).status()     # Error.NotManifold,        is_empty True
# NaN vertex
m.Manifold(m.Mesh(nan_pts, tris)).status()           # Error.NonFiniteVertex,    is_empty True
# triangle index out of range
m.Manifold(m.Mesh(pts, [[0,1,99]])).status()         # Error.VertexOutOfBounds,  is_empty True
```

So Manifold **does not throw on bad mesh input** — it returns an empty Manifold and
sets `status`. (It *does* throw if you, e.g., call `smooth()` on a mesh that already
has tangents.) Degenerate (collinear, zero-area) triangles in an *otherwise valid*
solid are silently collapsed. `is_empty()`/`genus()`/`precision()` are the cheap
health checks; `genus` is only meaningful per connected component, so
`decompose()` first.

### 3.5 Stability caveat observed

During benchmarking, one script that built deep boolean trees plus several other
operations in a single process produced an intermittent **segfault (exit 139)** that
did not reproduce when the same operations were run in isolation. Likely a
TBB-threading / lifetime edge case in this specific 2.3.1 build. Worth flagging:
for a long-lived build123d process, isolate heavy Manifold work and prefer the
current 3.x line, which has had many numerical-stability fixes (3.4.1 explicitly
reverts a problematic lazy-collider change).

---

## 4. Properties, colors & materials

`vert_properties` columns 3+ are arbitrary per-vertex floats. Colors are just
3 (RGB) or 4 (RGBA) extra channels — there is no dedicated color type. This is the
key mechanism for carrying **scad2py colors** and **build123d `Color`** through a
mesh pipeline.

### 4.1 `set_properties`

```python
Manifold.set_properties(
    new_num_prop: int,
    f: Callable[[vec3 position, ndarray[float32] old_props], object]
) -> Manifold
```

`f` receives the vertex position and its current extra-properties array, and
returns the new property tuple of length `new_num_prop`. Channels can be added or
removed. Verified — paint a cube red, a sphere blue, union them:

```python
red_cube  = m.Manifold.cube([2,2,2], True).set_properties(3, lambda p, old: (1,0,0))
blue_sph  = m.Manifold.sphere(1.3, 24).set_properties(3, lambda p, old: (0,0,1))
u = red_cube + blue_sph
u.num_prop()                          # 3  (RGB) -- vert_properties is (N,6)
np.unique(u.to_mesh().vert_properties[:,3:6], axis=0)
# -> [[0,0,1], [1,0,0], ...]   both colors survive the boolean
```

### 4.2 How properties propagate through Booleans — important nuance

Properties **do** survive Booleans, and where two source solids touch you get a
property seam. But the verified union above also produced colors like
`[1.0000001, 0, 0]` and `[0, 0, 0.99999994]` — i.e. **values are interpolated at
the new cut edges and pick up float32 rounding**. Consequences for build123d /
scad2py:

* Do **not** rely on exact equality to recover a color. Quantize / snap on
  read-back (`np.round`), or
* Use **integer-coded material IDs** in `run_original_id` instead — that channel
  is exact and is the proper "which object did this triangle come from" record.
  Pair `reserve_ids` with a Python `id -> Color` table.
* Per-vertex color is interpolated linearly across triangles; a hard color
  boundary needs the two regions to be separate property-vertices (Manifold splits
  property-verts at seams automatically, which is why `num_prop_vert >= num_vert`).

`set_properties` can also read position to make gradients
(`lambda p, old: (p[0], p[1], p[2])` verified working). `calculate_curvature`
writes curvature into chosen channels (`num_prop` grows automatically).

### 4.3 Recommended scheme for scad2py / build123d

* **Solid color** (one color per OpenSCAD `color()` subtree): tag with a unique
  `original_id` via `reserve_ids`, keep the `id → Color` map in Python, read
  `run_index`/`run_original_id` back to recolor triangle runs after Booleans. Exact.
* **Per-vertex / gradient color**: use `set_properties` RGBA channels; accept
  interpolation at seams; snap on read-back.

---

## 5. Performance & parallelism

* **Threading: Intel TBB.** Compiled with `MANIFOLD_PAR=TBB`, the kernel
  parallelizes across cores; below a problem-size threshold it falls back to a
  serial path automatically. The serial-only build (`MANIFOLD_PAR=NONE`) and the
  WASM build are still fast.
* **GPU: gone.** Manifold *used to* have CUDA (and OpenMP) backends via NVIDIA
  Thrust. **Both CUDA and OMP backends have been removed.** The METADATA in the
  installed wheel states this verbatim: *"Note: OMP and CUDA backends are now
  removed."* Manifold is **CPU-only today.** Don't plan around a GPU path.
* **Precision:** 2.3.1's kernel is **single-precision float32** internally.
  **[upstream]** 3.x moved the *internal kernel to double precision* and added
  `MeshGL64` for double-precision I/O — a significant robustness/accuracy upgrade.
* **Speed:** the project advertises ~**1000× faster than CGAL** on CSG. Verified
  on this machine (12-core, 2.3.1): **500 sphere subtractions from a cube → forced
  full evaluation in 0.43 s**, output 107k triangles. A 7958-vertex SDF sphere
  level-set in 87 ms. For comparison, OCC BREP Booleans on comparable complexity
  are typically 10²–10³× slower and can outright fail on tricky tangencies.
* **Laziness:** transforms, and to a degree primitive construction, are deferred.
  `num_tri()` / `to_mesh()` / `volume()` force evaluation. Benchmark *after* a
  forcing call or you will measure nothing.

---

## 6. Limitations

The honest list — these are exactly the impedance mismatches with build123d/OCC.

1. **Faceted only — no exact geometry.** No NURBS, no exact circles/arcs, no
   analytic surfaces. A "circle" is a polygon; a "sphere" is a refined octahedron.
   Curve quality is governed globally by `set_circular_segments` /
   `set_min_circular_angle` (default 10°) / `set_min_circular_edge_length`
   (default 1.0). Every export from build123d→manifold is a *tessellation* and is
   lossy; the result cannot be perfectly converted back to a BREP.
2. **No fillet / chamfer.** Manifold has no edge-blend operation at all. The
   nearest tools are `CrossSection.offset(JoinType.Round)` in 2D and Minkowski-with-
   a-sphere in 3D — both approximate and faceted. Real fillets must stay on the OCC
   side.
3. **No native Minkowski in 2.3.1.** `minkowski_sum`/`minkowski_difference` do
   **not** exist in the installed wheel. OpenSCAD/scad2py implement Minkowski as:
   *for a convex B, hull each pair (translate B to each vertex of A's triangles)
   and union; for a non-convex B, decompose into convex parts first.* `batch_hull`
   is the primitive that makes this tractable. **[upstream]** 3.x reportedly adds
   `minkowski_sum`/`minkowski_difference` directly — verify before relying on it,
   and expect it to still be a faceted approximation.
4. **Single precision (2.3.1).** float32 vertices → ε grows with model size
   (`cube([1000]³)` has ε≈0.01). Mixing very large and very tiny features in one
   model loses the small ones. Mitigated by upgrading to 3.x (double precision).
5. **Mesh-only / solids-only.** No wires, no open surfaces, no points as
   first-class objects. Input *must* be a closed oriented 2-manifold or it is
   rejected (§3.4). No assemblies, no construction geometry.
6. **No measurement beyond volume/area/bbox/curvature.** No mass properties with
   density, no moments of inertia, no distance-to-surface in 2.3.1 (`min_gap` is
   3.x-only), limited topology queries.
7. **No history / parametric features.** Each `Manifold` is a dead mesh; there is
   no feature tree, no named faces that survive editing. `face_id`/`original_id`
   give *provenance tags*, not editable features.

---

## 7. Build, install & packaging

* **Wheel:** `manifold3d` on PyPI. Installed wheel is
  `manifold3d-2.3.1 / cp310-cp310-macosx_11_0_arm64`, generated by
  **scikit-build-core 0.7.1**, `Root-Is-Purelib: false` (a compiled extension —
  one `manifold3d.cpython-310-darwin.so`, ~1.7 MB). The only runtime dependency is
  **numpy** (`>=1.26.0b1` on Python ≥3.12). License **Apache-2.0**.
* **Platform coverage:** prebuilt wheels for macOS (x86_64 10.14+, arm64 11.0+),
  Windows amd64, Linux manylinux_2_28/2_27 x86_64; Python 3.8–3.12. `pip install
  manifold3d` is a no-compile install on all mainstream platforms — friendly for a
  build123d dependency.
* **Bindings:** the 2.3.1 wheel predates the nanobind switch; **current 3.x
  bindings are nanobind** (`bindings/python/manifold3d.cpp`) with custom type
  casters mapping `la::vec`/`la::mat` ↔ Python tuples/numpy and exposing internal
  `VecView`s as zero-copy read-only numpy arrays. There is no Python-side wrapper
  module — the `.so` *is* the package, so all docs live in baked-in docstrings.
* **C++ library & other bindings:** the upstream C++ library (CMake; needs only
  CMake + a C++ compiler + Python) also ships a **C FFI** (`MANIFOLD_CBIND`) and a
  **JavaScript/WASM** binding (`manifold-3d` on npm, used by ManifoldCAD.org and
  embeddable in Pyodide-style browser stacks). Dependencies: GLM (vectors),
  Clipper2 (2D), quickhull (3D hull), TBB (optional, threading), Thrust headers.
  CMake flags: `MANIFOLD_PYBIND`, `MANIFOLD_CBIND`, `MANIFOLD_JSBIND`,
  `MANIFOLD_PAR=[NONE|TBB]`, `MANIFOLD_DEBUG` (turns on internal assertions/
  exceptions), `MANIFOLD_EXPORT` (GLB via assimp), `MANIFOLD_TEST`.
* **Embedding options for build123d:** simplest is to just depend on the
  `manifold3d` PyPI wheel — no build step. If build123d ever needs C++-level
  integration it could link the C++ lib or the C FFI, but the Python wheel covers
  the use case here. For a browser/Pyodide build123d, the WASM binding exists but
  is a separate (JS) artifact.

---

## 8. Implications for build123d

### 8.1 What Manifold cleanly provides that OCC lacks or does poorly

* **Bulletproof, fast Booleans.** OCC `BRepAlgoAPI_{Fuse,Cut,Common}` is exact but
  slow and *fails* on tangent/coincident-face configurations — a constant source of
  build123d user pain. Manifold's Boolean is guaranteed-manifold and ~1000× faster.
  A tessellate → Manifold-boolean → keep-as-mesh path is a credible escape hatch
  for "OCC won't fuse these" cases, and the right engine for scad2py's heavy
  CSG-tree evaluation.
* **SDF → solid (`level_set`).** OCC has nothing comparable. Implicit modeling,
  lattices, gyroids, voxel/field-driven shapes all become easy.
* **Robust mesh repair-ish import.** Feeding an arbitrary STL through
  `Manifold(mesh)` either yields a guaranteed-solid or a precise `status` reason —
  a useful validator/normalizer for messy imported meshes.
* **Cheap geometric queries** — `volume`, `surface_area`, `genus`, `bounding_box`,
  `decompose` (connected components) — fast and never throw.
* **Mesh-level offset / hull / Minkowski-via-hull** — practical for the scad2py
  `offset()`/`minkowski()` operations that OCC handles awkwardly or not at all.
* **`warp` / `warp_batch` free-form deformation** with topology preserved.
* **2D `CrossSection`** with Clipper2-quality polygon Booleans + `offset` —
  a clean, fast 2D kernel for sketch-region work and the 2D half of Minkowski.

### 8.2 The impedance mismatches

* **Exact ↔ faceted.** build123d/OCC objects are BREP with exact curves; Manifold
  is triangles. Every crossing is a *tessellation* (lossy, governed by circular-
  segment settings). A Manifold result re-imported into OCC is a faceted shell, not
  a smooth BREP — fillets/draft/exact-curve operations are gone. Decide per-object
  whether it lives in the "exact" world or the "mesh" world; round-tripping
  degrades it.
* **Named faces / topology naming.** build123d selectors rely on persistent OCC
  topology. Manifold's `face_id`/`original_id`/`run_index` are *provenance tags*,
  not OCC `TopoDS_Face`s — a translation layer is needed to map them back to
  build123d face selectors, and it will be approximate.
* **Color/material model.** build123d `Color` is per-face/per-object; Manifold
  color is per-vertex floats subject to interpolation rounding at seams (§4.2).
  Use `run_original_id` (exact integer tags) for per-object color, reserve
  `set_properties` RGBA for genuine per-vertex gradients, and snap on read-back.
* **Precision.** 2.3.1 is float32 with size-dependent ε — a build123d model in mm
  spanning a meter loses sub-10-µm detail. Strongly prefer pinning **manifold3d ≥
  3.x** (double-precision kernel, `set_tolerance`) for any CAD-grade use.
* **No fillet/chamfer/draft** — those *must* remain OCC operations; Manifold can't
  participate. A scad2py `minkowski` with a sphere is a faceted fake fillet at
  best.
* **Units & orientation** match (both right-handed, Manifold has no intrinsic
  units), so transforms map cleanly; the matrix layout differs (`Float3x4`
  row-of-columns) and needs a small adapter to/from OCC `gp_Trsf` and build123d
  `Location`.

### 8.3 Suggested integration shape

1. A `build123d ⇄ manifold3d` bridge: `Shape.tessellate()` → `Mesh` →
   `Manifold`, and `Manifold.to_mesh()` → OCC `Poly_Triangulation` /
   `BRepBuilderAPI_Sewing` → build123d `Solid` (faceted).
2. Route scad2py's CSG evaluation (`union`/`difference`/`intersection`/`hull`/
   `minkowski`/`offset`/`linear_extrude`/`rotate_extrude`) **entirely through
   Manifold** — it mirrors the OpenSCAD API closely and is the same engine
   OpenSCAD itself uses, so behavior parity is high. Only convert to OCC at the
   boundary where the user wants exact-CAD features.
3. Carry color via `run_original_id` integer tags + a Python `id → Color` map.
4. Pin `manifold3d>=3.x` for double precision and `batch_boolean`/`min_gap`.

---

## 9. Open questions

* **2.3.1 vs 3.x API drift.** This document introspected 2.3.1 (the installed
  wheel). 3.4.x adds `MeshGL`/`MeshGL64`, `batch_boolean`+`OpType`,
  `smooth_out`/`smooth_by_normals`/`calculate_normals`,
  `refine_to_length`/`refine_to_tolerance`, `set_tolerance`/`get_tolerance`,
  `min_gap`, `ray_cast`, `ExecutionContext` (cancellation/progress), possibly
  `minkowski_sum`/`minkowski_difference`. **Before building the bridge, upgrade
  the env to current `manifold3d` and re-introspect** — several signatures here
  are 2.3.1-only.
* **Native Minkowski in 3.x** — does `minkowski_sum` actually exist in the latest
  wheel, and how does its quality/perf compare to the hull-decomposition approach
  OpenSCAD uses? Needs verification on a 3.x install.
* **Intermittent segfault** seen when many operations run in one 2.3.1 process
  (§3.5) — does it persist on 3.x? If so, build123d must sandbox Manifold calls.
* **Tessellation fidelity round-trip:** what segment density (`set_circular_
  segments`) is acceptable so that build123d→Manifold→build123d doesn't visibly
  degrade models, and what is the cost? Needs an empirical study.
* **Face-selector mapping:** can `face_id` + coplanar grouping reliably reconstruct
  build123d face selectors after a Boolean, or is the mapping fundamentally
  many-to-one and lossy?
* **Color seam interpolation:** is `property_tolerance` tuning enough to keep hard
  color boundaries crisp through Booleans, or is `run_original_id` tagging strictly
  required?
* **Double-precision `MeshGL64` cost:** memory/perf overhead of 64-bit I/O vs
  float32 for typical build123d model sizes.
* **Threading interaction:** does Manifold's TBB pool contend with anything OCC or
  build123d already runs, and is there a way to cap/share the thread pool?

---

### Appendix A — versions & provenance

* Introspected wheel: **manifold3d 2.3.1**, released 2024-01-08, Apache-2.0,
  author Emmett Lalish (a Google "20% project", not an official Google product).
* Latest upstream at time of writing: **3.4.1** (March 2026). 2.x→3.x is a major
  line with a double-precision kernel rewrite and a meaningfully larger API.
* Project: <https://github.com/elalish/manifold> ·
  C++/JS/Python docs: <https://manifoldcad.org/docs/html/> ·
  algorithm wiki: <https://github.com/elalish/manifold/wiki/Manifold-Library>.
* Dependencies (C++): GLM, Clipper2, quickhull, TBB (optional), Thrust headers.
* All numbers/signatures marked as "verified" were produced by running Python
  against the installed 2.3.1 `.so`; items marked **[upstream]** come from the
  project repository / release notes for the 3.x line and should be re-checked
  against whatever wheel build123d ultimately pins.
