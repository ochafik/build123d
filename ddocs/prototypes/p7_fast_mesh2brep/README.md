# p7 — fast mesh → build123d Solid

Closes the #1 named blocker for a **manifold3d backend in build123d**: turning a
triangle mesh back into a build123d BREP `Solid` *fast*.

Earlier prototype `p1_brep_mesh_roundtrip` measured only the **slow path** — one
planar `TopoDS_Face` per triangle, all fed into a single `BRepBuilderAPI_Sewing`.
`Sewing` does spatial vertex/edge matching across every face, so its cost is
super-linear in triangle count and unusable above ~1e4–1e5 triangles.

This prototype implements and benchmarks the **fast alternative**, ported from
the parallel CadQuery effort (`cadquery/ddocs/prototypes/p2-fast-mesh2brep`):
**direct shell assembly**. The mesh from manifold3d is already welded, so we
know the unique vertices and the triangle→vertex index arrays — connectivity
needs *no* spatial search.

## Files

| File | What it does |
|---|---|
| `m2b.py` | The three converters: `mesh_to_solid_sewing` (slow baseline), `mesh_to_solid_direct` (fast, `+fix`), `mesh_to_solid_direct_nofix` (fast, scales to 1M). All return build123d `Solid`. |
| `common.py` | Mesh generation (`sphere_mesh*`), welding, volume/validity/topology probes, timing. |
| `probe_correctness.py` | All three converters on small meshes — volume, validity, Euler characteristic. |
| `probe_scaling.py` | Scaling benchmark 1k / 10k / 100k / 1M triangles — time + throughput tables. |
| `probe_phases.py` | Phased breakdown of the direct-nofix build — confirms near-linearity, isolates BRepCheck cost. |
| `probe_validity.py` | Downstream usability: `.faces()`/`.edges()`, booleans, STEP round-trip, winding robustness. |
| `probe_unify.py` | `ShapeUpgrade_UnifySameDomain` coplanar-merge post-pass — face-count reduction. |
| `probe_million.py` | Dedicated 1M-triangle measurement, phase-separated to avoid measurement-overhead noise. |
| `RESULTS.md` | Benchmark tables, real output, verdict. |

## Run

```sh
VP=/Users/ochafik/github/.ddocs-venv/bin/python
cd /Users/ochafik/github/build123d/ddocs/prototypes/p7_fast_mesh2brep
$VP -u probe_correctness.py
$VP -u probe_scaling.py
$VP -u probe_phases.py
$VP -u probe_validity.py
$VP -u probe_unify.py
$VP -u probe_million.py --validity --step
```
