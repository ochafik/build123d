# build123d Mesh Interop Paths — every way triangles enter/leave the kernel

> Research doc 02. Focus: bridging **manifold3d** (indexed triangle meshes) and
> **build123d** (BREP / OpenCASCADE solids). All file/line references are against
> `/Users/ochafik/github/build123d/src/build123d/` as of 2026-05-22.
>
> TL;DR: build123d → triangles is robust, fast, and lossless-enough (it is just
> OCCT tessellation). triangles → build123d is the hard direction and has **three
> distinct mechanisms** of very different quality: (a) a fake "reference" Face
> (`import_stl`), (b) a per-triangle sewn Shell/Solid (`Mesher`), and (c) an
> analytic primitive reconstructor (`brep_from_stl.detect_primitives`). None of
> them is a clean, fast, validity-guaranteed mesh→Solid path. This is the crux of
> the whole project.

---

## 0. Map of the territory

| Path | Direction | Entry point | Output topology | Speed | Fidelity |
|---|---|---|---|---|---|
| OCCT tessellation | Solid → mesh | `Shape.tessellate()`, `Mesher._mesh_shape` | numpy-ish vertex/triangle lists | fast | approximation, controlled by deflection |
| STL/3MF/glTF export | Solid → mesh file | `export_stl`, `export_gltf`, `Mesher` | file on disk | fast | approximation |
| `import_stl` | mesh → "Shape" | `importers.import_stl` | a **single fake `Face`** carrying a `Poly_Triangulation` | very fast | no real BREP — a reference object only |
| `Mesher.read` | mesh → Shape | `mesher.Mesher` | per-triangle `Shell` or `Solid` (thousands of planar faces) | slow (minutes) | exact vertices, but topology bloat |
| `brep_from_stl.detect_primitives` | mesh → analytic Faces | `brep_from_stl` | `ShapeList[Face]` of fitted planes/cylinders/spheres + leftovers | medium | analytic refit, lossy, no closed solid |
| `import_step` / `import_brep` | BREP file → Shape | `importers` | real `Solid`/`Compound` | fast | exact (not a mesh path) |

The two genuinely interesting paths for a manifold3d backend are **OCCT
tessellation** (out) and **`Mesher.read` / `brep_from_stl`** (in). Everything
else is plumbing.

---

## 1. BREP → triangles (tessellation)

### 1.1 The kernel call: `BRepMesh_IncrementalMesh`

Every tessellation path in build123d ultimately invokes OCCT's incremental
mesher. There is no custom triangulator. The mesher *mutates the shape in place*:
it attaches a `Poly_Triangulation` to every `TopoDS_Face`, retrievable later via
`BRep_Tool.Triangulation_s`.

`Shape.mesh()` — `topology/shape_core.py:1604`:

```python
def mesh(self, tolerance: float, angular_tolerance: float = 0.1):
    """Generate triangulation if none exists."""
    if self._wrapped is None:
        raise ValueError("Cannot mesh an empty shape")
    if not BRepTools.Triangulation_s(self.wrapped, tolerance):
        BRepMesh_IncrementalMesh(
            self.wrapped, tolerance, True, angular_tolerance, True
        )
```

Note `BRepTools.Triangulation_s(shape, tolerance)` is a *guard*: it returns
`True` if a triangulation already exists at that or finer tolerance, so `mesh()`
is idempotent and cheap on re-call.

The five `BRepMesh_IncrementalMesh` arguments are: `theShape`,
`theLinDeflection`, `isRelative=True`, `theAngDeflection`, `isInParallel=True`.

- **`theLinDeflection` (linear/"tolerance")** — max chord distance between the
  true surface/curve and the facet. Default `1e-3` everywhere
  (`export_stl`, `export_gltf`, `Mesher.add_shape`, `tessellate`).
- **`isRelative=True`** — *critical detail*: deflection is scaled by edge size,
  not absolute. So `tolerance=1e-3` means "0.1% of each edge's length", not
  "1e-3 mm". A 100 mm part and a 1 mm part get proportionally similar mesh
  density. This matters if a manifold3d backend wants a *uniform absolute*
  mesh resolution — it would have to pass `isRelative=False` itself.
- **`theAngDeflection` (angular)** — max angle between adjacent facet normals on
  curved surfaces. Default `0.1` rad ≈ 5.7°.
- **`isInParallel=True`** — per-face triangulation runs on a thread pool.

A `BRepMesh_IncrementalMesh(...)` constructor call triggers the meshing
immediately; `export_stl` additionally calls `.Perform()` (redundant but
harmless — `exporters3d.py:446-449`).

### 1.2 Gathering per-face triangulation into a global mesh

Two near-identical implementations exist. The canonical one is
`Shape.tessellate()` — `topology/shape_core.py:2241`:

```python
def tessellate(self, tolerance, angular_tolerance=0.1):
    """General triangulated approximation"""
    self.mesh(tolerance, angular_tolerance)
    vertices: list[Vector] = []
    triangles: list[tuple[int, int, int]] = []
    offset = 0
    for face in self.faces():
        loc = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face.wrapped, loc)
        trsf = loc.Transformation()
        reverse = face.wrapped.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        vertices += [
            Vector(v.X(), v.Y(), v.Z())
            for v in (poly.Node(i).Transformed(trsf)
                      for i in range(1, poly.NbNodes() + 1))
        ]
        triangles += [
            (t.Value(1)+offset-1, t.Value(3)+offset-1, t.Value(2)+offset-1)
            if reverse else
            (t.Value(1)+offset-1, t.Value(2)+offset-1, t.Value(3)+offset-1)
            for t in poly.Triangles()
        ]
        offset += poly.NbNodes()
    return vertices, triangles
```

Key facts an implementer must know:

1. **Per-face triangulations live in face-local coordinates.** Each
   `Poly_Triangulation` node is in the coordinate frame of its `TopoDS_Face`'s
   `TopLoc_Location`. You **must** apply `loc.Transformation()` to every node
   (`.Transformed(trsf)`) to get world coordinates. Forgetting this is the
   classic OCCT meshing bug.
2. **No vertex dedup across faces.** Each face contributes its own node block;
   `offset` simply concatenates. Two faces meeting at a shared edge produce
   *duplicate, coincident vertices* — once per face. The output is therefore a
   **non-indexed soup at face seams**: vertices are shared *within* a face but
   never *between* faces. A box → ~24 vertices, not 8.
3. **OCCT triangle indices are 1-based**; the `+offset-1` converts to 0-based
   global indices.
4. **Orientation handling**: if the `TopoDS_Face` is `TopAbs_REVERSED`, triangle
   winding is swapped (1,2,3 → 1,3,2) so the *outward* normal is consistent. The
   `Poly_Triangulation` itself stores winding for the face's *forward*
   orientation; build123d compensates at gather time.
5. **No normals are emitted.** `tessellate()` returns only positions +
   connectivity. `Poly_Triangulation` *can* carry per-node normals
   (`poly.HasNormals()` / `poly.Normal(i)`) but build123d never reads them.
   Consumers recompute normals from triangle winding (see VTK path below).

### 1.3 The other copy: `Mesher._mesh_shape`

`mesher.py:272-310` is a second, independent implementation of the same gather.
Differences from `tessellate()`:

- It is a `@staticmethod` taking the wrapped OCP shape.
- Returns plain `(x,y,z)` tuples, not `Vector`s.
- `BRepMesh_IncrementalMesh` is constructed but **`.Perform()` is not called** —
  the OCCT constructor already triggers meshing, so this is fine.
- Reversed-face winding uses `order = [1, 3, 2]`.
- Reuses one `TopLoc_Location()` object across all faces (re-filled per
  `Triangulation_s` call).

This duplication is a code smell; a manifold3d backend should add **one**
canonical `Shape -> (np.ndarray verts, np.ndarray tris)` helper and route
everything through it.

### 1.4 VTK / Jupyter display path

`vtk_tools.py` builds a `vtkPolyData` by calling `Mesher._mesh_shape` directly
(`vtk_tools.py:89`) — note the cyclic-ish dependency `vtk_tools → mesher`. It
then runs `vtkTriangleFilter` and, optionally, `vtkPolyDataNormals`
(`SetFeatureAngle(360)`, both point + cell normals) to *recompute* normals from
geometry. `jupyter_tools.py` wraps that into an HTML/JS viewer. Not relevant to
manifold3d interop except as confirmation that **normals are always recomputed
downstream**, never trusted from OCCT.

### 1.5 Tessellation as used by exporters

- **`export_stl`** (`exporters3d.py:419`) — meshes with
  `BRepMesh_IncrementalMesh`, then `StlAPI_Writer().Write(shape, path)`. OCCT
  writes the triangulation itself; build123d does not touch vertex arrays.
  `ascii_format` toggles ASCII vs binary STL.
- **`export_gltf`** (`exporters3d.py:254`) — meshes *every node* in the assembly
  tree (`node.mesh(linear_deflection, angular_deflection)`), builds an XDE
  document, writes via `RWGltf_CafWriter`. Important: it temporarily rotates the
  shape −90° about X to convert OCCT's +Z-up to glTF's +Y-up, and
  **`BRepTools.Clean_s(shape)` afterward strips the cached triangulation**. So
  glTF export is *non-destructive* w.r.t. the triangulation cache; `export_stl`
  is *destructive* (leaves the mesh attached).
- **`export_step` / `export_brep`** — exact BREP, no tessellation. Not a mesh
  path.

---

## 2. `mesher.py` — the `Mesher` class (3MF + STL via lib3mf)

`Mesher` is build123d's *editable* mesh bridge. It uses the 3MF Consortium's
`lib3mf` for file I/O (both `.3mf` and `.stl` go through lib3mf's reader/writer,
`mesher.py:528, 568`).

### 2.1 Solid → mesh (`add_shape`)

`add_shape` (`mesher.py:371`) per shape:
1. `copy.deepcopy(shape)` then `Mesher._mesh_shape(...)` → `(vertices, triangles)`.
2. `Mesher._create_3mf_mesh` (`mesher.py:312`) — **this is where vertex dedup
   happens**. It rounds every vertex to `digits = -round(log10(TOLERANCE))`
   decimal places (TOLERANCE = `1e-6` → 6 digits) and builds a
   `(rx,ry,rz) -> index` map. So the dedup tolerance is a fixed grid snap, *not*
   a true spatial merge. Triangles whose three mapped indices are not all
   distinct (degenerate after snap) are dropped.
3. Builds `lib3mf` `Position` (3×`c_float`) and `Triangle` (3×`c_uint`) arrays
   and calls `mesh_3mf.SetGeometry(vertices_3mf, triangles_3mf)`.
4. Color: `_add_color` (`mesher.py:359`) maps `shape.color` to a 3MF
   `BaseMaterialGroup` with a single material, `SetObjectLevelProperty`.
5. Validity: `mesh_3mf.IsValid()` → `RuntimeError` if false;
   `mesh_3mf.IsManifoldAndOriented()` → only a `warnings.warn` if false.

So 3MF export *deduplicates* vertices (grid-snapped), unlike raw `tessellate()`.

### 2.2 mesh → Solid (`Mesher.read` → `_get_shape`)

`_get_shape` (`mesher.py:460`) is the **central mesh→BREP reconstruction** in
build123d, and it is brute-force:

```python
gp_pnts = [gp_Pnt(*p.Coordinates[0:3]) for p in mesh_3mf.GetVertices()]
shell_builder = BRepBuilderAPI_Sewing()
for i in range(mesh_3mf.GetTriangleCount()):
    tri_indices = mesh_3mf.GetTriangle(i).Indices[0:3]
    ocp_vertices = [gp_pnts[tri_indices[i]] for i in range(3)]
    polygon_builder = BRepBuilderAPI_MakePolygon(
        ocp_vertices[0], ocp_vertices[1], ocp_vertices[2], Close=True)
    face_builder = BRepBuilderAPI_MakeFace(polygon_builder.Wire())
    facet = face_builder.Face()
    # drop zero-area facets
    BRepGProp.SurfaceProperties_s(facet, facet_properties)
    if facet_properties.Mass() != 0:
        shell_builder.Add(facet)
shell_builder.Perform()
occ_sewed_shape = downcast(shell_builder.SewedShape())
```

i.e. **one planar `TopoDS_Face` per triangle**, all fed into a single
`BRepBuilderAPI_Sewing`. After `Perform()`:

- If the sewn result is a `TopoDS_Compound`, it explores out every
  `TopAbs_SHELL` → multiple `Shell`s (handles parts with internal voids).
- Otherwise a single `Shell`.
- The largest-bounding-box shell is the *outer* shell; the rest are *inner*.
- If the outer shell **`is_manifold`** (every edge shared by exactly 2 faces —
  see §6.4) it builds a `Solid` via
  `BRepBuilderAPI_MakeSolid(outer_shell)` + `.Add(inner_shell)` for voids.
- If **not** manifold, it returns the bare `Shell` (a Solid cannot be built).

Color is read back from the 3MF base material and set as `shape.color`.

**Consequences for round-tripping (this is the crux):**
- A mesh with N triangles becomes a BREP with **N planar faces, 3N edges**
  (pre-merge), each face a 3-vertex polygon. `BRepBuilderAPI_Sewing` merges
  coincident edges/vertices within its `Tolerance` so the final shell has
  ~3N/2 unique edges. This is *enormous* topology — a 50k-triangle STL → a Solid
  with 50k faces. Booleans, fillets, offsets on such a Solid are pathologically
  slow and numerically fragile.
- The docstring of `import_stl` explicitly warns: *"creating an editable model
  (with Mesher) may take minutes depending on the size of the STL file."*
- `BRepBuilderAPI_Sewing` is the only manifold-recovery mechanism. If the mesh
  has any non-watertight defect, T-junctions, or duplicated/flipped triangles,
  sewing fails to close it and you get a `Shell`, not a `Solid` — silently.
- The vertices are *exact* (no geometric loss), but the surface is faceted: all
  faces are planar. A sphere stays a 50k-facet polyhedron.

### 2.3 Other `Mesher` API

- `triangle_counts`, `vertex_counts`, `mesh_count` — per-mesh stats.
- `add_meta_data` / `add_code_to_metadata` / `get_meta_data*` — 3MF metadata
  (can embed the generating Python source as `python`-typed metadata).
- `get_mesh_properties` — name/part-number/type/uuid per mesh.
- `write` / `write_stream` — `.3mf`/`.stl` to file or `BytesIO`.

---

## 3. `exporters3d.py` — export surface

Covered in §1.5. Summary of tessellation behaviour:

| Function | Tessellates? | Deflection params | Notes |
|---|---|---|---|
| `export_stl` | yes, `BRepMesh_IncrementalMesh` | `tolerance=1e-3`, `angular_tolerance=0.1` | leaves mesh cached on shape |
| `export_gltf` | yes, per-node `Shape.mesh()` | `linear_deflection=1e-3`, `angular_deflection=0.1` | +Y-up rotation; `BRepTools.Clean_s` after |
| `export_step` | no | — | exact BREP via XDE/STEPCAF |
| `export_brep` | no | — | `BRepTools.Write_s` |

There is no glTF/STL *importer* in `exporters3d.py`; import lives in
`importers.py` (STL) and `Mesher.read` (3MF/STL).

---

## 4. `importers.py` — what an STL actually becomes

### 4.1 `import_stl` → a single fake `Face`

`importers.py:270`:

```python
reader = RWStl.ReadFile_s(fsdecode(file_name))   # -> Poly_Triangulation
# optional unit scaling: transform each Node
face = TopoDS_Face()
BRep_Builder().MakeFace(face, reader)
return Face.cast(face)
```

This is a **trap for the unwary**. `RWStl.ReadFile_s` returns a
`Poly_Triangulation`. `BRep_Builder().MakeFace(face, triangulation)` builds a
`TopoDS_Face` that has **a triangulation but no underlying `Geom_Surface` and no
wire/edge boundary**. The docstring is candid: *"return it as a Face reference
object… importing with this method and creating a reference is very fast."*

Implications:
- It is geometrically a *mesh in a Face costume*. It has no surface, so it
  cannot be lofted, offset, filleted, booleaned, or extruded meaningfully.
- `face.faces()` returns just `[self]`; `face.edges()` is empty. So you **cannot**
  feed an `import_stl` result into `detect_primitives` / `MeshIndex.from_shape`
  expecting per-triangle faces — `MeshIndex` needs real per-triangle `Face`s
  (which is why the `brep_from_stl` tests build their input via `Mesher().read`,
  not `import_stl`; see §5.6).
- Its main legitimate use is *display* / as a visual reference under a real
  model.
- Unit scaling (`model_unit`) transforms every `Node` in place before
  `MakeFace`.

### 4.2 `import_step` → real `Solid`/`Compound`

`importers.py:135` — full XDE assembly walk (`STEPCAFControl_Reader`), recovers
the real solid topology, names, colors, per-component locations. Returns a
`Compound` tree (unwrapped to a single Shape if there's one free shape). Exact
BREP — **not** a mesh path, but the gold-standard target topology.

### 4.3 `import_brep` → real Shape

`importers.py:111` — `BRepTools.Read_s` → `Compound.cast`. Exact BREP.

### 4.4 `import_dxf` / `import_svg` — 2D only, irrelevant here.

---

## 5. `brep_from_stl.py` — analytic primitive reconstruction (66 KB)

This is the most ambitious mesh→BREP path: instead of one-face-per-triangle, it
**fits analytic primitives** (planes, cylinders, spheres) to *groups* of mesh
triangles. Authored "by gumyr with codex gpt-5.4", dated April 2026 — clearly a
recent, experimental module.

### 5.1 What it consumes and produces

- **Input**: a `Shape` whose `.faces()` are the individual mesh triangles —
  i.e. the output of `Mesher().read(...)` (a Solid/Shell of N triangle Faces).
  *Not* an `import_stl` Face.
- **Entry point**: `detect_primitives(mesh) -> (ShapeList[Face], ShapeList[Face],
  list[str])` (`brep_from_stl.py:1864`) = `(primitives, leftovers, code_lines)`.
- **Output**: a `ShapeList[Face]` of *analytic* faces (planar `Rectangle`s,
  cylindrical faces, spherical faces), a `ShapeList[Face]` of unclaimed mesh
  triangles ("leftovers"), and a list of build123d *source-code strings* that
  reconstruct each primitive.

Crucially it returns **loose `Face`s, never a sewn `Shell` or closed `Solid`**.
The commented-out `support_shell = Shell(support_faces)` at lines 1936-1938 shows
the author *considered* assembling shells and backed off. So `detect_primitives`
is a *surface segmenter / CAD-feature recognizer*, not a solid reconstructor.

### 5.2 Data model (`brep_from_stl.py:68-174`)

- `FaceSample` — `(index, face, center, normal)` cached per mesh triangle.
- `PlanePatch` — `face_indices`, `origin`, `normal`, `(u/v)_min/max`, `residual`.
- `CylinderPatch` — `face_indices`, `axis_point`, `axis_direction`, `radius`,
  `normal_sign`, `residual`; `.axis` property → `Axis`.
- `SpherePatch` — `face_indices`, `center`, `radius`, `residual`.
- `MeshIndex` — `faces`, `face_samples`, `face_key_lookup`
  (sorted-rounded-vertex-tuple → index), lazily-built `adjacent_face_indices`.
  - `from_shape` calls `face.center()` and `face.normal_at()` per triangle.
  - `ensure_adjacency()` builds an edge→faces map keyed by `_edge_key` (an
    order-independent rounded-endpoint key) — adjacency via *shared edge keys*,
    so it depends on triangles having *exactly coincident* vertices (rounded to
    9 digits, `_rounded_vertex_key`). This is fine for a `Mesher`-reconstructed
    shape whose triangles came from a clean mesh.

### 5.3 The pipeline (`detect_primitives`, ordered for evidence strength)

```
mesh_index = MeshIndex.from_shape(mesh)
1. clean_plane_patches  = detect_planes_from_clean_proxy(mesh, mesh_index)
2. sphere_patches       = detect_spheres(mesh, mesh_index, clean_plane_indices)
3. cylinder_patches     = detect_cylinders(mesh, mesh_index,
                                           clean_plane_indices | sphere_indices)
4. normal_plane_patches = detect_planes_from_normals(mesh, mesh_index,
                                           blocked = planes|spheres|cylinders)
```

Each stage *blocks* the faces already claimed by earlier (stronger-evidence)
stages, so a face is assigned to at most one primitive. Then every patch is
turned into a `Face` (`build_plane_face` / `build_cylinder_face` /
`build_sphere_face`), unclaimed triangles → `leftovers`, and `shapes_to_code`
emits source strings, sorted by `plane_sort_key`.

### 5.4 The four detectors

**(1) `detect_planes_from_clean_proxy`** (`brep_from_stl.py:770`) — the
high-confidence plane finder. It runs `copy.deepcopy(shape).clean()` (OCCT
`ShapeUpgrade`/unify) to get *merged* "proxy" faces, keeps proxy faces with
`>= 4` edges and `area >= 0.5 * max_area`. For each proxy face it collects mesh
triangles whose normal matches (`1 - |n·n_proxy| <= 1e-3`), whose center is
within `plane_tolerance` (`0.002 * bbox_diagonal`) of the proxy plane, whose
bbox overlaps, and which `proxy_face.is_inside(center)`. Tolerances scale with
`shape.bounding_box().diagonal`.

**(2) `detect_spheres`** (`brep_from_stl.py:1616`):
- `_sphere_like_face_components` — for each triangle, computes a *radius
  signature* from circumradii of `(center, neighbor_center, edge_midpoint)`
  triplets (`_face_radius_signature`); a triangle is "sphere-like" if the
  relative spread of those radii is small (locally isotropic curvature).
- Connected components of sphere-like triangles, further split by
  `Face.sew_faces` (see §6.2).
- `fit_local_sphere` — algebraic sphere fit: solves the linear system
  `[x y z 1]·[a b c d]ᵀ = -(x²+y²+z²)` via `np.linalg.lstsq`, center
  `=(-a/2,-b/2,-c/2)`, validates radius std-ratio and normal error.
- `grow_curved_patch` — BFS across adjacent faces that still fit
  (`_sphere_face_error`), then re-fits radius/residual as means.
- `suppress_duplicate_spheres` removes overlapping detections.

**(3) `detect_cylinders`** (`brep_from_stl.py:1495`) — the most elaborate:
- Two seeding strategies: (a) faces grouped by *equal area* (`_group_indices_by_area`),
  each group `Face.sew_faces`'d into components; (b) `_cylinder_like_face_indices`
  (anisotropic radius signature) + BFS local patches.
- `fit_local_cylinder` (`brep_from_stl.py:1093`): axis direction from clustering
  `n_a × n_b` cross products of triangle-normal pairs (cosine-metric `DBSCAN`);
  radius/axis-point from intersecting projected 2D normal lines and clustering
  the intersection points (`DBSCAN`); validates radius std and residual.
- `_finalize_cylinder_patch` → `grow_curved_patch` → refit → grow again;
  optionally `validate_bounded_cylinder` (checks end caps are parallel planes,
  consistent end-circle radii, radius stddev bounds).
- `merge_equivalent_cylinders` fuses coaxial same-radius patches.
- Has a `_cylinder_patch_looks_spherical` guard: rejects a cylinder if a sphere
  fits the same faces with `<= 0.35×` the residual.

**(4) `detect_planes_from_normals`** (`brep_from_stl.py:1249`) — fallback:
groups *remaining* faces by quantized normal (`round(normal, 3)`), connected
components, `_build_plane_patch` SVD-fits a plane (`_fit_plane_to_points` =
SVD, smallest singular vector = normal), validates max point-to-plane distance
and per-face normal error.

### 5.5 Primitive → `Face` and → code

- `build_plane_face` — `Plane(...) * Pos(...) * Rectangle(u_size, v_size)`, then
  `_as_face(...)`. So a detected plane becomes a finite **rectangular** planar
  face sized to the patch's u/v extent — *not* the true polygonal outline of
  the region. Holes / non-rectangular boundaries are lost.
- `build_cylinder_face` — builds a `Cylinder(radius, height)` solid, filters
  `faces().filter_by(GeomType.CYLINDER)[0]`, flips sign if needed. A *full*
  cylindrical face of the fitted radius/extent — partial arcs are not honored.
- `build_sphere_face` — `Pos(center) * Sphere(radius)` → its spherical face.
- `shapes_to_code` (`brep_from_stl.py:1763`) emits build123d source strings,
  snapping to canonical planes (`Plane.XY`, etc.) where the axis aligns. This is
  the feature that makes `brep_from_stl` useful as an **STL→build123d-script**
  tool.

### 5.6 Robustness, speed, output topology — honest assessment

- **Robustness**: heuristic and tolerance-heavy. Tolerances scale with bbox
  diagonal (good), but the pipeline depends on `sklearn`'s `DBSCAN`, SVD/lstsq
  fits, mesh adjacency from *exactly coincident* rounded vertices, and OCCT
  `clean()`. The test suite (`tests/test_brep_from_stl.py`) only exercises clean
  synthetic inputs (`Box`, `Sphere`, `Cylinder`, filleted box) re-meshed through
  `Mesher`. Behaviour on real scanned/noisy STL is unproven.
- **Hard dependencies**: `numpy` *and* `scikit-learn` (`from sklearn.cluster
  import DBSCAN`). This is the **only** build123d module needing sklearn.
- **Speed**: combinatorial in places — `fit_local_cylinder` does
  `combinations(samples, 2)` over cross-products and again over 2D-line
  intersections (capped at 64 samples each via `_evenly_spaced_subset`), plus
  DBSCAN. `MeshIndex.from_shape` calls `.center()`/`.normal_at()` per triangle
  (OCCT calls). For large meshes this is *medium-slow* and there is no progress
  reporting.
- **Output topology**: a *flat list of disconnected analytic `Face`s plus
  leftover triangle Faces*. There is **no sewing into a `Shell`, no `Solid`, no
  guarantee the primitives even abut**. Adjacent primitives are fitted
  independently, so a detected plane and a detected cylinder generally do **not**
  share an exact edge — their boundaries will not sew without trimming/extension.
- **Coverage gaps**: only plane/cylinder/sphere. Cones, tori, fillets-as-tori,
  swept/freeform surfaces all fall into `leftovers`. The `Box`-with-fillets test
  *expects* fillet edges→12 cylinders and corners→8 spheres, i.e. fillets are
  approximated as cylinder+sphere patches, not true blend surfaces.

So `brep_from_stl` is best understood as a **CAD feature-recognition / reverse-
engineering aid that emits build123d code**, not a watertight Solid builder. To
get a *Solid* from its output you would still need to: extend/trim each face to
its neighbors, sew, and `MakeSolid` — none of which it does.

---

## 6. triangles → BREP: every available mechanism, compared

build123d offers no single "mesh → Solid" function. Here is the complete
inventory.

### 6.1 Per-triangle polygon faces + `BRepBuilderAPI_Sewing` (the `Mesher` way)

The mechanism in `Mesher._get_shape` (§2.2): one `BRepBuilderAPI_MakeFace(
BRepBuilderAPI_MakePolygon(p0,p1,p2,Close=True).Wire())` per triangle, all
`Add`'d to one `BRepBuilderAPI_Sewing`, `Perform()`, then `BRepBuilderAPI_MakeSolid`
on a manifold shell. **This is the only path that yields a true `Solid` from an
arbitrary mesh.**

- *Validity guarantee*: only if the sewn outer shell `is_manifold` (§6.4).
  Otherwise you silently get a `Shell`.
- *Speed*: O(N) face construction + sewing; sewing is the bottleneck and is
  super-linear. Minutes for tens of thousands of triangles.
- *Fidelity*: vertices exact; surface is fully faceted (all planar faces). No
  curvature recovered. Topology bloat (N faces).

### 6.2 `Face.sew_faces` / `Shell([faces...])` — `_sew_topods_faces`

`shape_core.py:3607`:

```python
def _sew_topods_faces(faces):
    shell_builder = BRepBuilderAPI_Sewing()
    for face in faces:
        shell_builder.Add(face)
    shell_builder.Perform()
    return downcast(shell_builder.SewedShape())
```

- `Shell.__init__` with an iterable of `Face`s calls `TopoDS.Shell(
  _sew_topods_faces(...))` (`two_d.py:2723`) — **will raise `TypeError` if the
  sewn result is not a single `Shell`** (e.g. a `Compound` of disjoint shells).
- `Face.sew_faces` (`two_d.py:1694`) is the multi-component-tolerant version:
  returns `list[ShapeList[Face]]`, one group per sewn component.
- `brep_from_stl` uses `Face.sew_faces` purely for *region grouping* (not for
  building shells).

Same `BRepBuilderAPI_Sewing` engine as §6.1 — same validity caveats.

### 6.3 `Solid(Shell)` / `Solid._make_solid`

`three_d.py:982`:

```python
@classmethod
def _make_solid(cls, shell):
    return ShapeFix_Solid().SolidFromShell(shell.wrapped)
```

`Solid.__init__(obj)` auto-calls `_make_solid` when `obj` is a `Shell`
(`three_d.py:754`). `ShapeFix_Solid().SolidFromShell` builds a `TopoDS_Solid` and
fixes orientation, but it **does not close gaps** — if the shell is open you get
an invalid solid. `Mesher._get_shape` instead uses the lower-level
`BRepBuilderAPI_MakeSolid` so it can attach void shells.

### 6.4 `is_manifold` — the gate

`shape_core.py:396`: builds an edge→faces ancestor map
(`TopExp.MapShapesAndAncestors_s`) and returns `False` if any non-degenerate
edge is *not* shared by exactly 2 faces. This is the manifold/watertight test
that `Mesher._get_shape` and `Shell.volume` rely on. It is a *topological* test
on the sewn result — it does **not** detect self-intersection or inverted
normals. A mesh that sews into a topologically-2-manifold shell passes even if
geometrically degenerate.

### 6.5 Polygon faces directly

`BRepBuilderAPI_MakePolygon` + `BRepBuilderAPI_MakeFace` (used in `Mesher` and
available generally) builds an arbitrary planar polygon face. For non-triangular
mesh facets (quads, n-gons) this works as long as the polygon is planar and
non-self-intersecting; a triangle is always planar so triangle meshes are safe.

### 6.6 `brep_from_stl.detect_primitives`

Analytic, lossy, surface-only (§5). Not a solid builder.

### 6.7 Summary table — triangles → BREP

| Mechanism | Output | True Solid? | Validity guarantee | Speed | Geometry fidelity |
|---|---|---|---|---|---|
| `Mesher._get_shape` (per-tri sew) | Shell or Solid | yes, if manifold | manifold→Solid else silently Shell | slow (minutes @ 10⁴–10⁵ tris) | exact verts, fully faceted, N faces |
| `Shell([Face…])` / `_sew_topods_faces` | Shell | no | raises if not one shell | sewing-bound | as input faces |
| `Solid(Shell)` / `ShapeFix_Solid` | Solid | only if shell already closed | no gap-closing | fast | as input shell |
| `detect_primitives` | loose analytic Faces | no | none | medium (DBSCAN/fits) | analytic refit, lossy, plane/cyl/sphere only |

**There is no fast, validity-guaranteed mesh→Solid in build123d today.**

---

## 7. trimesh / numpy interop

- **No `trimesh` dependency** anywhere in build123d.
- **numpy is used internally** (`brep_from_stl.py`, `geometry.py`,
  `topology/one_d.py`, `objects_curve.py`, `operations_sketch.py`) but **build123d
  exposes no public numpy-array mesh representation**. `Shape.tessellate()`
  returns `list[Vector]` + `list[tuple[int,int,int]]` — Python lists, not
  `np.ndarray`. `Mesher._mesh_shape` returns lists of tuples.
- The only numpy *mesh* arrays are private to `brep_from_stl`
  (`_vector_rows`, `_point_rows` → `np.ndarray` for SVD/lstsq/DBSCAN).
- `vtk_tools.to_vtk_poly_data` produces a `vtkPolyData` (VTK's own arrays), not
  numpy.

**Implication**: a manifold3d backend would have to *add* the numpy bridge
itself. manifold3d's `Mesh`/`MeshGL` are numpy-array based
(`vert_properties: float32[N,3+]`, `tri_verts: uint32[M,3]`); converting to/from
build123d means `np.asarray(tessellate()[0])` style glue on the way out and a
`Mesher`-style or custom sewing routine on the way in.

---

## 8. Round-trip feasibility — Solid → mesh → Solid

### 8.1 The out leg (Solid → mesh): solid

`tessellate()` / `BRepMesh_IncrementalMesh` is robust, parallel, tolerance-
controlled, and fast. Caveats only:
- `isRelative=True` means deflection is per-edge relative — a manifold3d backend
  wanting uniform absolute resolution must call `BRepMesh_IncrementalMesh`
  directly with `isRelative=False`.
- Output has **duplicate vertices at face seams** (no cross-face dedup in
  `tessellate()`); `Mesher._create_3mf_mesh` *does* dedup (grid-snap to 6
  digits). For manifold3d, which *requires* a properly indexed merged mesh
  (`Merge()` exists but you want clean input), do a spatial vertex weld
  yourself — grid-snapping at TOLERANCE works and matches `Mesher`'s approach.
- Normals are not exported; recompute from winding.
- Curved surfaces become facets — expected and acceptable for CSG.

**Verdict: the out leg is production-ready.** This is the easy half.

### 8.2 The in leg (mesh → Solid): fragile

Three options, all flawed:

1. **`Mesher`-style per-triangle sew** — the only true Solid path. But:
   - *Speed*: `BRepBuilderAPI_Sewing` on 10⁴–10⁵ triangle faces takes minutes.
     manifold3d routinely produces meshes of that size from a few booleans.
   - *Validity*: depends entirely on the mesh being watertight & 2-manifold.
     manifold3d *guarantees* watertight, 2-manifold, consistently-oriented
     output — so in principle sewing *should* always close. In practice OCCT
     sewing tolerance vs. manifold3d's vertex precision can still drop the result
     to a `Shell` if vertices don't merge.
   - *Topology bloat*: the resulting Solid has one planar face per triangle.
     Any subsequent build123d operation (boolean, fillet, export STEP) on it is
     slow and fragile. STEP export of such a Solid yields a huge, ugly file.
   - *Geometry loss*: all curvature is gone — a manifold3d sphere comes back as
     a faceted polyhedron Solid, permanently.

2. **`detect_primitives`** — recovers analytic geometry, but returns *loose
   unsewn Faces*, only plane/cylinder/sphere, with independently-fitted
   boundaries that don't abut. Reassembling a Solid from them is unsolved. Also
   pulls in `scikit-learn`.

3. **`import_stl` fake Face** — not a Solid at all; useless for round-trip.

### 8.3 Where round-trip breaks down

| Failure mode | Cause | Severity |
|---|---|---|
| mesh→Solid is minutes-slow | `BRepBuilderAPI_Sewing` over N triangle faces | blocking for interactive use |
| Solid has N faces | per-triangle face, no surface merge | makes downstream BREP ops unusable |
| Curvature permanently lost | all faces planar | sphere/cylinder never recovered as analytic |
| Silent `Shell` (not `Solid`) | sewing tolerance vs vertex precision | downstream code expecting `Solid` breaks |
| `detect_primitives` output not sewable | independent per-patch fits | cannot rebuild Solid |
| seam vertices duplicated | `tessellate()` no cross-face dedup | needs explicit weld before manifold3d |

**Overall verdict**: Solid → mesh round-trips *cleanly enough*. mesh → Solid is
the genuine wall. A naive "mesh → `Mesher`-sew → Solid" round-trip is
*technically* lossless in vertices and *topologically* a Solid, but is so slow
and so topologically bloated that it is unusable as a general pipeline. The
"nice" mesh → analytic-BREP path (`detect_primitives`) is incomplete and emits
unsewn faces.

---

## 9. Design implications — for a manifold3d backend

1. **Use OCCT tessellation as the sole out path.** Add one canonical
   `shape_to_meshgl(shape, lin_defl, ang_defl, relative=False) ->
   (np.float32[N,3], np.uint32[M,3])` helper, replacing the two divergent
   copies (`Shape.tessellate`, `Mesher._mesh_shape`). Weld vertices (grid-snap
   at `TOLERANCE`, exactly as `Mesher._create_3mf_mesh`) so manifold3d gets a
   clean indexed mesh. Expose `relative` so callers can pick uniform-absolute
   resolution.

2. **Do not route booleans/CSG through BREP if the inputs originated as
   meshes.** The whole point of a manifold3d backend is to do CSG *in mesh
   space* (manifold3d is fast and guarantees manifoldness). Keep build123d
   `Shape`s and manifold `Manifold`s as parallel representations; convert only
   at the boundary.

3. **Treat mesh → BREP `Solid` as a deliberate, expensive "bake" step**, not an
   implicit conversion. The only correct existing mechanism is the
   `Mesher`-style per-triangle sew (`BRepBuilderAPI_Sewing` + `MakeSolid`).
   Surface it explicitly (e.g. `Solid.from_mesh(verts, tris)`), document its
   cost, and warn when the mesh is large. Reuse `Mesher._get_shape`'s
   manifold-check / void-shell logic.

4. **For STEP/clean-BREP export of mesh-origin geometry, prefer `detect_primitives`
   over per-triangle sew** — but invest in the missing "trim/extend adjacent
   primitives + sew into a Solid" step. Today `detect_primitives` stops at loose
   Faces; a backend that wants clean STEP out of manifold CSG must close that
   gap. Also note the `scikit-learn` dependency it adds.

5. **scad2py mapping**: OpenSCAD is itself CSG-of-meshes (it uses CGAL/manifold
   internally). Mapping scad2py → build123d-with-manifold-backend is *natural*:
   each OpenSCAD primitive → a `Manifold`, each `union/difference/intersection`
   → a manifold boolean, and only `import`/`render`/export → a build123d Shape
   via the sew bake. Pure-mesh OpenSCAD models never need to touch BREP at all.

6. **Vertex precision contract.** manifold3d works in `float`/`double` with its
   own epsilon; build123d/OCCT `TOLERANCE = 1e-6`. Pin a single weld tolerance
   at the boundary and apply it consistently both directions, or sewing will
   intermittently fail to close shells.

7. **Normals**: never trust OCCT triangulation normals (build123d never reads
   them) and never trust them into manifold3d either — recompute from winding,
   keeping the `TopAbs_REVERSED` winding-swap logic so orientation is correct.

---

## 10. Open questions

1. **Sewing scalability** — does `BRepBuilderAPI_Sewing` on a *guaranteed*
   watertight 2-manifold manifold3d mesh always produce a single closed Shell,
   or does OCCT tolerance still fragment it? Needs an empirical test at 10³,
   10⁴, 10⁵ triangles with timings.
2. **Is per-triangle Solid ever actually useful downstream?** A Solid with 50k
   planar faces — can build123d even fillet/offset/STEP-export it in reasonable
   time, or is it effectively write-only?
3. **`detect_primitives` on noisy meshes** — all tests use clean synthetic
   re-meshed primitives. How does DBSCAN clustering / SVD fitting degrade on
   scanned data or manifold3d output with sliver triangles?
4. **Closing the `detect_primitives` gap** — what is the right algorithm to
   trim/extend independently-fitted analytic faces so they share exact edges and
   sew into a Solid? Is this in scope, or should the backend just accept faceted
   Solids for non-primitive geometry?
5. **Cones / tori** — `detect_primitives` has no cone or torus detector;
   fillets come back as cylinder+sphere approximations. For OpenSCAD's
   `cylinder()` with `r1 != r2` (a cone) the analytic path is missing entirely.
6. **`isRelative` deflection** — should the manifold3d bridge default to
   relative (per-edge) or absolute deflection? Relative matches build123d's
   exporters; absolute matches what mesh-CSG users expect.
7. **Two tessellation implementations** — `Shape.tessellate` and
   `Mesher._mesh_shape` have drifted (Vector vs tuple, `.Perform()` vs not,
   winding order var). Safe to unify before building on top? (Recommended: yes.)
8. **`import_stl`'s fake Face** — is there any appetite to make `import_stl`
   optionally produce a real Solid (delegating to the sew bake), or should it
   stay a fast display-only reference and push users to `Mesher`?

---

## Appendix A — key OCP symbols and where they live

| OCP symbol | File:line | Role |
|---|---|---|
| `BRepMesh_IncrementalMesh` | `shape_core.py:1618`, `exporters3d.py:446`, `mesher.py:280` | tessellate BREP → `Poly_Triangulation` |
| `BRepTools.Triangulation_s` | `shape_core.py:1617` | check existing triangulation (idempotency guard) |
| `BRep_Tool.Triangulation_s` | `shape_core.py:2257`, `mesher.py:293` | fetch a face's `Poly_Triangulation` + `TopLoc_Location` |
| `TopLoc_Location` / `.Transformation()` | `shape_core.py:2256`, `mesher.py:279` | face-local → world transform for nodes |
| `BRepTools.Clean_s` | `exporters3d.py:323` | drop cached triangulation after glTF export |
| `RWStl.ReadFile_s` | `importers.py:292` | STL file → `Poly_Triangulation` |
| `BRep_Builder().MakeFace(face, triangulation)` | `importers.py:321` | wrap triangulation in a fake `TopoDS_Face` |
| `StlAPI_Writer` | `exporters3d.py:451` | write meshed shape → STL |
| `RWGltf_CafWriter` | `exporters3d.py:309` | write XDE doc → glTF |
| `BRepBuilderAPI_Sewing` | `mesher.py:466`, `shape_core.py:3609` | merge faces into Shell(s) |
| `BRepBuilderAPI_MakePolygon` | `mesher.py:473` | 3 points → triangular wire |
| `BRepBuilderAPI_MakeFace` | `mesher.py:476` | wire → planar face |
| `BRepBuilderAPI_MakeSolid` | `mesher.py:505` | shell(s) → Solid (with voids) |
| `ShapeFix_Solid().SolidFromShell` | `three_d.py:984` | shell → Solid (no gap closing) |
| `lib3mf.Lib3MF` | `mesher.py:114` | 3MF/STL file I/O, vertex/triangle/material arrays |
| `DBSCAN` (sklearn) | `brep_from_stl.py:54` | cluster normals/axes/points in primitive fitting |

## Appendix B — call graph (mesh-relevant)

```
export_stl ─────────────► BRepMesh_IncrementalMesh ─► StlAPI_Writer
export_gltf ─► node.mesh ─► BRepMesh_IncrementalMesh ─► RWGltf_CafWriter ─► BRepTools.Clean_s
Shape.tessellate ─► Shape.mesh ─► BRepMesh_IncrementalMesh
                  └► per-face BRep_Tool.Triangulation_s ─► (vertices, triangles)
Mesher.add_shape ─► Mesher._mesh_shape ─► BRepMesh_IncrementalMesh
                  └► Mesher._create_3mf_mesh (grid-snap dedup) ─► lib3mf SetGeometry
Mesher.read ─► lib3mf reader ─► Mesher._get_shape
             └► per-tri MakePolygon→MakeFace ─► BRepBuilderAPI_Sewing
             └► is_manifold ? BRepBuilderAPI_MakeSolid : Shell
import_stl ─► RWStl.ReadFile_s ─► BRep_Builder.MakeFace ─► fake Face
detect_primitives ─► MeshIndex.from_shape (needs per-triangle Faces, i.e. Mesher.read output)
                   ├► detect_planes_from_clean_proxy / _from_normals
                   ├► detect_spheres  (fit_local_sphere = lstsq)
                   ├► detect_cylinders (fit_local_cylinder = DBSCAN + 2D line intersect)
                   └► build_{plane,cylinder,sphere}_face ─► loose ShapeList[Face] + code
```
