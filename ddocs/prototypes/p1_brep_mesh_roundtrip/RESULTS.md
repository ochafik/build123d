# P1 — BREP ⇄ mesh round-trip: measured results & verdict

> Prototype that derisks the foundational interop primitive between
> **build123d** (OpenCASCADE BREP kernel) and **manifold3d** (robust faceted
> mesh-CSG kernel). All numbers below were *measured* by running the scripts in
> this directory; nothing is estimated.

## Environment

| | |
|---|---|
| Machine | Apple Mac14,5 (M-series), 12 logical cores, macOS 24.6 (arm64) |
| Python | 3.10.12 |
| build123d | `0.1.dev2749+gd5290b1ea` (editable fork install) |
| manifold3d | **3.4.1** (NOT the 2.3.1 of research doc 03 — API differs, see below) |
| numpy | 2.2.6 |

### manifold3d 3.4.1 API reality vs research doc 03

Doc 03 introspected manifold3d **2.3.1**; the venv ships **3.4.1**. Confirmed differences relevant here:

- The float32 data struct is `manifold3d.Mesh` (= old `MeshGL`). There is **also** a double-precision `manifold3d.Mesh64`. **This prototype uses `Mesh64` for the OUT leg** so OCC's `double` vertices survive without float32 truncation.
- `Manifold(mesh)` constructor: welds positions *only per explicit merge vectors* — it does **not** weld by distance. An un-welded mesh with no merge vectors is therefore rejected.
- `Manifold.to_mesh()` returns a `Mesh` whose `vert_properties` is `float32 (N,≥3)` and `tri_verts` is `uint32 (M,3)`.
- 3.4.1 has `batch_boolean`, `minkowski_sum/difference`, `set_tolerance`, etc. — not used here but available for a real backend.

---

## Files

| File | What it does |
|---|---|
| `bridge.py` | The interop primitive. `solid_to_manifold()` (OUT leg) and `manifold_to_solid()` (IN leg). Reference-quality, commented. |
| `roundtrip_bench.py` | Headline benchmark: 8 test shapes through the full round-trip, with counts / validity / volume error / timings. |
| `scaling_cliff.py` | Drives the IN leg from 2k → 130k triangles to expose the mesh→Solid scaling cliff. |
| `tessellation_fidelity.py` | Sweeps OCC linear/angular deflection; mesh size vs volume error. |

Run any of them with `/Users/ochafik/github/.ddocs-venv/bin/python <file>.py`.

---

## 1. OUT leg — build123d Solid → manifold3d Manifold

**Works cleanly and fast for every shape tried.** The pipeline is
`Shape.tessellate()` → **weld coincident vertices** → `Mesh64` → `Manifold`.

### The welding requirement is real and mandatory

OCC tessellation (`Shape.tessellate`, `shape_core.py:2241`) concatenates
per-face triangulations with **no cross-face vertex dedup** — a box comes out as
24 vertices, not 8. manifold3d's `Manifold(mesh)` does not weld by distance, so
the raw soup is **rejected**. Verified directly:

```
Box:    RAW  -> status=Error.NotManifold  is_empty=True
Box:    WELD -> status=Error.NoError      is_empty=False   verts 24 -> 8
Sphere: RAW  -> status=Error.NotManifold  is_empty=True
Sphere: WELD -> status=Error.NoError      is_empty=False   verts 5153 -> 5089
```

The weld is a grid-snap to 6 decimal places (matching `Mesher._create_3mf_mesh`,
TOLERANCE 1e-6), implemented with one `np.unique(axis=0)` pass; the un-snapped
coordinates of each cluster are averaged back so no grid bias is introduced.

### Per-shape OUT-leg result (default tessellation 1e-3 / 0.1 rad)

| Shape | raw verts | welded verts | dedup× | tris | m3d status | genus |
|---|---|---|---|---|---|---|
| Box | 24 | 8 | 3.00 | 12 | NoError | 0 |
| Sphere(r5) | 5153 | 5089 | 1.01 | 10174 | NoError | 0 |
| Cylinder(r5,h20) | 506 | 252 | 2.01 | 500 | NoError | 0 |
| Cone(r8,h15) | 903 | 766 | 1.18 | 1528 | NoError | 0 |
| Torus(R20,r5) | 16129 | 15876 | 1.02 | 31752 | NoError | **1** |
| FilletedBox(20mm,r3) | 5552 | 4728 | 1.17 | 9452 | NoError | 0 |
| Compound(box+sphere) | 4090 | 4010 | 1.02 | 8012 | NoError | 0 |
| Tricky (box−tunnel) | 530 | 260 | 2.04 | 520 | NoError | **1** |

Every shape produced a clean 2-manifold (`Error.NoError`, non-empty). Genus is
preserved: the torus and the tunnelled box both come back genus 1. OUT-leg wall
time is **sub-100 ms** for everything except the Torus (0.47 s — it is just a
big mesh). **The OUT leg is production-ready.**

---

## 2. IN leg — manifold3d Manifold → build123d Solid

Uses the **only** mechanism in build123d that yields a real `Solid` from an
arbitrary mesh: `Mesher._get_shape`'s approach (`mesher.py:460`) — one planar
`TopoDS_Face` per triangle, all fed to a single `BRepBuilderAPI_Sewing`,
`Perform()`, then `BRepBuilderAPI_MakeSolid` on the sewn shell.

**It works — produces a valid Solid — but it is slow, and the cost is the whole story.**

### Per-shape IN-leg result

| Shape | tris in | IN result | rt valid? | rt faces | IN total (s) | of which sewing (s) |
|---|---|---|---|---|---|---|
| Box | 12 | Solid | ✅ | 12 | 0.002 | 0.001 |
| Sphere | 10174 | Solid | ✅ | 10174 | 2.22 | 1.47 |
| Cylinder | 500 | Solid | ✅ | 500 | 0.071 | 0.036 |
| Cone | 1528 | Solid | ✅ | 1528 | 0.251 | 0.132 |
| Torus | 31752 | Solid | ✅ | 31752 | 11.54 | 8.65 |
| FilletedBox | 9452 | Solid | ✅ | 9452 | 2.51 | 1.59 |
| Compound | 8012 | **Compound of 2 Solids** | ✅ | 8012 | 1.69 | 1.03 |
| Tricky (tunnel) | 520 | Solid | ✅ | 520 | 0.094 | 0.064 |

Two findings beyond raw cost:

- **Topology bloat is 1:1 with triangles.** The round-tripped Solid has exactly
  one planar face per mesh triangle (`rt faces` == `tris`). A round-tripped
  sphere is a 10 174-face polyhedron `Solid`. All curvature is permanently
  faceted — a manifold sphere never comes back as an analytic sphere.
- **The `Mesher._get_shape` void-detection logic is buggy for multi-body
  meshes.** `Mesher` treats the largest-bbox shell as outer and *all* others as
  internal voids — wrong for a `Compound` of disjoint solids (they are separate
  bodies, not voids). `bridge.py` fixes this with bbox-nesting classification:
  the `Compound` test correctly comes back as a Compound of 2 valid Solids
  rather than one invalid Solid-with-fake-void. (This is a genuine latent bug in
  build123d's mesh import; documented, not patched, per house rules.)

### Sewing did NOT silently degrade to a Shell

Doc 02 warned the IN leg can silently return a `Shell` instead of a `Solid` if
sewing tolerance vs vertex precision drops a seam. **In this prototype it never
did** — every one of the 8 shapes, and every refinement level in §3, sewed into
a closed manifold Solid. Reason: manifold3d *guarantees* watertight, 2-manifold,
consistently-oriented output, and we hand OCC the exact same vertex coordinates
manifold3d used (`Mesh64`, double precision). When the input mesh is genuinely
watertight, `BRepBuilderAPI_Sewing` closes it reliably. The "silent Shell" risk
is real for *arbitrary* STL but not for *manifold3d-origin* meshes.

---

## 3. Full round-trip fidelity — Solid → Manifold → Solid

Volume and bbox error after the complete round-trip (default tessellation):

| Shape | src volume | rt volume | volume rel err | bbox abs err | total time (s) |
|---|---|---|---|---|---|
| Box | 1000.0 | 1000.0 | 2.3e-16 | 0.0 | 0.011 |
| Sphere | 523.6 | 522.9 | 1.4e-3 | 3.0e-3 | 2.32 |
| Cylinder | 1570.8 | 1570.1 | 4.1e-4 | 1.6e-3 | 0.085 |
| Cone | 1005.3 | 1004.2 | 1.1e-3 | 2.5e-3 | 0.28 |
| Torus | 9869.6 | 9861.4 | 8.3e-4 | 7.8e-3 | 12.01 |
| FilletedBox | 7780.6 | 7779.9 | 8.9e-5 | 0.0 | 2.66 |
| Compound | 1523.6 | 1522.6 | 6.3e-4 | 1.6e-3 | 1.79 |
| Tricky (tunnel) | 7411.5 | 7410.8 | 1.0e-4 | 1.8e-15 | 0.12 |

- **Volume error is purely the tessellation facet error** — a faceted polyhedron
  is inscribed in the true surface so it always *under*-estimates volume. All
  errors are < 0.15% at default tessellation; planar shapes (Box) are exact to
  float epsilon.
- **The round trip adds no error of its own.** Vertices are carried losslessly
  through (`Mesh64` double precision, exact sew). The only loss is the original
  Solid→mesh tessellation; the mesh→Solid leg is geometrically exact on the
  vertices it is given.
- **Wall time is dominated entirely by the IN leg**, which is dominated by
  `BRepBuilderAPI_Sewing` — see §4.

---

## 4. The mesh→Solid scaling cliff (`scaling_cliff.py`)

One base sphere Manifold, `refine(n)`'d to multiply triangle count, IN leg only:

| refine | triangles | build faces (s) | **sewing (s)** | make solid (s) | IN total (s) | µs / triangle |
|---|---|---|---|---|---|---|
| 1 | 2 020 | 0.25 | 0.32 | 0.03 | 0.67 | 331 |
| 2 | 8 080 | 0.83 | 1.20 | 0.09 | 2.21 | 273 |
| 4 | 32 320 | 3.26 | **11.35** | 0.30 | 15.24 | 472 |
| 6 | 72 720 | 5.84 | **57.15** | 0.80 | 64.63 | 889 |
| 8 | 129 280 | 11.06 | **189.32** | 1.04 | **204.14** | 1579 |

**Sewing-time scaling exponent** (`sew_time ∝ tris^k`, fitted between rows):

| at … triangles | k |
|---|---|
| 8 080 | 0.96 (≈linear) |
| 32 320 | 1.62 |
| 72 720 | 1.99 |
| 129 280 | **2.08** |

`BRepBuilderAPI_Sewing` is **~linear up to a few thousand triangles, then turns
quadratic**. At 130k triangles the IN leg takes **3.4 minutes**; extrapolating
the k≈2 regime, a 500k-triangle mesh (routine manifold3d boolean output) would
take **roughly 50 minutes**. Face construction itself is linear and cheap (~85
µs/tri); `make_solid` is negligible. **The cliff is entirely `BRepBuilderAPI_Sewing`.**

This **confirms research doc 02's verdict**: mesh→Solid is "the wall."

---

## 5. Tessellation fidelity knobs (`tessellation_fidelity.py`)

OCC exposes two knobs via `Shape.tessellate(tolerance, angular_tolerance)`:
linear deflection (max chord error, *relative* to edge length by default) and
angular deflection (max normal angle between facets). Sphere(r=10), exact
volume 4188.79:

| setting | lin defl | ang defl | verts | tris | mesh volume | rel err |
|---|---|---|---|---|---|---|
| very coarse | 1e-1 | 0.8 | 67 | 130 | 3754.14 | 10.4% |
| coarse | 3e-2 | 0.4 | 265 | 526 | 4078.05 | 2.6% |
| medium | 1e-2 | 0.2 | 1012 | 2020 | 4158.80 | 0.72% |
| default | 1e-3 | 0.1 | 5089 | 10174 | 4182.76 | 0.14% |
| fine | 1e-4 | 0.05 | 50261 | 100518 | 4188.17 | 0.015% |
| very fine | 1e-4 | 0.02 | 100780 | 201556 | 4188.48 | 0.0073% |

Cylinder and Torus behave identically (see script output). Findings:

- **Faceting always under-estimates volume** (inscribed polyhedron) at every
  setting — error is one-sided.
- **Angular deflection is the dominant knob** on uniform-curvature solids.
  Loosening linear deflection alone barely moves the triangle count; the angular
  budget caps facet size. A backend that wants a predictable triangle budget
  should tune *angular* deflection.
- **Volume error falls ~quadratically as facets shrink — but triangle count
  rises just as fast.** Going from `default` to `fine` cuts sphere error 10×
  (0.14% → 0.015%) at the cost of **10× the triangles** (10k → 100k). Combined
  with §4's quadratic sewing cost, that 10× triangles is ~100× the IN-leg time.
- **`default` (1e-3 / 0.1 rad) is the sweet spot**: < 0.2% volume error on every
  curved shape, mesh small enough that the IN leg stays in the linear regime.

---

## 6. Honest verdict

### Where it works

- **OUT leg (Solid → Manifold): production-ready.** Fast (sub-100 ms typical),
  robust on every shape tried — primitives, curved solids, a filleted box, a
  multi-body Compound, a genus-1 tunnelled part. The *only* non-obvious
  requirement is welding the OCC seam-duplicated vertices before handing the
  mesh to manifold3d; a single `np.unique` pass does it. manifold3d confirms a
  clean 2-manifold (`Error.NoError`, correct genus) every time.
- **Round-trip fidelity is excellent on vertices.** The round trip itself adds
  *no* error — `Mesh64` double precision + exact per-triangle sewing carry every
  vertex losslessly. Total volume error is purely the one-time Solid→mesh
  tessellation (< 0.15% at default settings).
- **mesh→Solid produces a genuinely valid Solid** for clean manifold3d-origin
  meshes — `is_valid` and `is_manifold` both true for all 8 shapes; the feared
  "silent Shell" degradation never occurred (because manifold3d guarantees
  watertight input and we preserve exact coordinates).

### Where it breaks

- **mesh→Solid does not scale — this is the wall.** `BRepBuilderAPI_Sewing` is
  ~linear to a few thousand triangles then **quadratic** (k≈2.0). 130k triangles
  = 3.4 min; a typical 500k-triangle manifold3d boolean result would be ~50 min.
  For any interactive or batch use this is **unusable** as a general path.
- **All curvature is permanently lost.** A round-tripped sphere is a
  10k-planar-face polyhedron `Solid`, not an analytic sphere. The topology bloat
  (one BREP face per triangle) makes any *subsequent* build123d operation
  (boolean, fillet, STEP export) on the result pathologically slow and fragile.
- **`Mesher._get_shape`'s void detection is buggy for multi-body meshes** — it
  would turn a Compound of disjoint solids into one invalid Solid-with-fake-void.
  `bridge.py` works around it with bbox-nesting classification, but the bug is
  real in build123d today.

### Confirm / refute doc 02's "mesh→Solid is the wall"

**CONFIRMED, with numbers.** Doc 02 predicted per-triangle sewing would be
"minutes-slow at 10⁴–10⁵ triangles" and produce topology-bloated faceted Solids.
Measured: quadratic sewing, 3.4 min at 1.3×10⁵ triangles, one BREP face per
triangle, curvature gone. The OUT leg is the easy half and is solid; the IN leg
via `BRepBuilderAPI_Sewing` is correct but **does not scale and degrades
geometry** — it is fine as a deliberate, small-mesh "bake" step and unusable as
an implicit conversion.

### Implication for a manifold3d backend

Treat the two kernels as **parallel representations**: keep CSG in mesh space
(manifold3d — fast, guaranteed-manifold), keep exact features in BREP space
(OCC), and convert only at the boundary. The OUT leg can be an implicit, cheap
conversion. The IN leg (`Solid.from_mesh`) must be an **explicit, opt-in, small-
mesh-only "bake"** with a loud warning above ~10⁴ triangles — never an automatic
step. For clean STEP output of mesh-origin geometry, per-triangle sewing is the
wrong tool; analytic refit (`brep_from_stl.detect_primitives`) is the only path
that avoids the bloat, and it is incomplete (see doc 02 §5).
