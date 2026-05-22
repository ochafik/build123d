# ddocs — Design exploration: manifold3d in build123d + scad2py as a build123d consumer

This folder is a **deep technical exploration**, not shipping code. It investigates
two substantial pieces of work the owner (ochafik) wants to pursue, and de-risks them
with research, prototypes, and concrete design proposals.

> **Branch:** `ddocs-manifold-scad2py-research` (local only — nothing here is pushed
> or merged). All artifacts live under `ddocs/`; no build123d source was modified.

## The two goals

1. **Goal 1 — manifold3d mesh operations in build123d.** build123d's geometry kernel
   is OpenCASCADE (OCC), a BREP kernel. It has no fast/robust mesh-CSG. The
   [manifold3d](https://github.com/elalish/manifold) library provides exactly that.
   Goal 1 is to bring it in as an optional, hybrid mesh backend.

2. **Goal 2 — scad2py on build123d.** ochafik's `scad2py` (an OpenSCAD
   parser / interpreter / transpiler) currently renders via manifold3d directly.
   Goal 2 is to make build123d a *backend* (run OpenSCAD, get BREP output) and a
   *transpilation target* (turn `.scad` into editable build123d Python).

**Start here:** [`EXECUTIVE-SUMMARY.md`](EXECUTIVE-SUMMARY.md) — the verdict on both
goals. Then [`ROADMAP.md`](ROADMAP.md) — the phased plan.

## How this was produced

18 research/prototyping sub-agents across 4 waves. Every prototype was *run* against a
real venv (`build123d` editable install + `manifold3d` 3.4.x); all numbers below are
measured, not estimated. A **parallel effort analysed the same two goals for CadQuery**
(build123d's sibling library, same OCC kernel) — its findings independently corroborate
this one and are cross-referenced in `research/09`.

## Index

### `research/` — deep-dive investigations

| Doc | Topic |
|---|---|
| [`01-build123d-architecture.md`](research/01-build123d-architecture.md) | build123d topology, the `Shape`/OCP wrapping, the two APIs, extension seams. No kernel abstraction exists. |
| [`02-build123d-mesh-paths.md`](research/02-build123d-mesh-paths.md) | Every path mesh data enters/leaves build123d; tessellation; the mesh→BREP "wall". |
| [`03-manifold3d-deep-dive.md`](research/03-manifold3d-deep-dive.md) | The manifold3d API, robustness model, properties/colors, limits. *(Introspected 2.3.1; prototypes use 3.4.x — see note in doc.)* |
| [`04-scad2py-architecture.md`](research/04-scad2py-architecture.md) | scad2py's full pipeline: parser → AST → transpiler → runtime → `csg.Node` → renderer. |
| [`05-openscad-vs-build123d-model.md`](research/05-openscad-vs-build123d-model.md) | OpenSCAD vs build123d computational models; the full primitive mapping table. |
| [`06-license-analysis.md`](research/06-license-analysis.md) | Apache-2.0 / LGPL-2.1+exception / GPL analysis; the scad2py GPL finding (§6.4). |
| [`07-prior-art-ecosystem.md`](research/07-prior-art-ecosystem.md) | Prior art; **build123d issue #1228 already requests a manifold extra.** |
| [`08-caching-infrastructure.md`](research/08-caching-infrastructure.md) | Caching in build123d (≈none), OpenSCAD, and scad2py (`FastKey`); a design sketch. |
| [`09-cadquery-ddocs-cross-comparison.md`](research/09-cadquery-ddocs-cross-comparison.md) | Compares this effort to the parallel CadQuery effort: convergences, gaps. |
| [`10-occ-kernel-failure-modes.md`](research/10-occ-kernel-failure-modes.md) | Why OCC booleans are slow/fragile; what OCC does *well* (frames the hybrid). |
| [`11-wasm-pyodide-packaging.md`](research/11-wasm-pyodide-packaging.md) | Browser/WASM feasibility; `OCP.wasm`; the GPL-static-linking constraint. |

### `design/` — design proposals

| Doc | Topic |
|---|---|
| [`design-manifold-in-build123d.md`](design/design-manifold-in-build123d.md) | **Goal 1 design.** `MeshPart` value type + free-function API, opt-in extra, phasing, risks. |
| [`design-scad2py-build123d.md`](design/design-scad2py-build123d.md) | **Goal 2 design.** Two products (runtime backend + source transpiler), hybrid routing, phasing. |
| [`scad2py-gpl-remediation.md`](design/scad2py-gpl-remediation.md) | The GPL blocker audit + clean-room remediation plan (prototyped in `p6`). |

### `prototypes/` — runnable, verified prototypes

| Proto | What it proves |
|---|---|
| [`p1_brep_mesh_roundtrip`](prototypes/p1_brep_mesh_roundtrip/) | Solid→mesh is easy; mesh→BREP via per-triangle sewing is quadratic/unusable. |
| [`p2_boolean_benchmark`](prototypes/p2_boolean_benchmark/) | manifold3d booleans **20–134× faster** than OCC; OCC times out at N=1000. |
| [`p3_meshsolid`](prototypes/p3_meshsolid/) | A `MeshSolid`/`MeshPart` value-type API; a boolean chain ran **71× faster**. |
| [`p4_scad2py_to_b3d`](prototypes/p4_scad2py_to_b3d/) | scad2py `csg.Node` → build123d renderer (Goal 2 Option A). Viable; hybrid needed. |
| [`p5_openscad_to_b3d_source`](prototypes/p5_openscad_to_b3d_source/) | `.scad` → readable build123d Python source (Goal 2 Option B). 8/8 examples run. |
| [`p6_clean_room_scad2py_math`](prototypes/p6_clean_room_scad2py_math/) | Clean-room rewrite of scad2py's 2 GPL files; **3308 cases bit-exact**. |
| [`p7_fast_mesh2brep`](prototypes/p7_fast_mesh2brep/) | Direct shell assembly: mesh→BREP **18× faster, linear** — clears the #1 blocker. |
| [`p8_face_identity`](prototypes/p8_face_identity/) | Provenance survives manifold booleans; planar faces/selectors recoverable. |

## Reproducing the prototypes

A shared venv was used: `/Users/ochafik/github/.ddocs-venv` (build123d editable +
manifold3d 3.4.x + trimesh + numpy). Each prototype's `RESULTS.md`/`NOTES.md` records
the exact commands and real output. The venv lives *outside* this repo and is gitignored.

## Bottom line

Both goals are **viable**. Goal 1's one hard blocker (fast mesh→BREP) is **solved**
(`p7`). Goal 2's one *legal* blocker (two GPL-contaminated scad2py files) is **solved**
(`p6`). See the executive summary and roadmap.
