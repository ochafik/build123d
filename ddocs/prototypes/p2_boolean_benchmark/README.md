# P2 - Boolean benchmark: build123d/OCC vs manifold3d

Proves (or disproves) the core value proposition for bringing manifold3d into
build123d: that manifold3d mesh booleans are dramatically faster and more
robust than build123d's native OpenCASCADE (OCC) booleans.

## Files

| File | Purpose |
|---|---|
| `bridge.py` | build123d <-> manifold3d conversion + subprocess timeout harness |
| `workloads.py` | the 4 boolean workloads, each in an OCC and a manifold variant |
| `run_benchmark.py` | driver: runs every workload x size with a hard timeout |
| `make_tables.py` | renders `results.json` into the markdown tables in RESULTS.md |
| `results.json` | raw measurements (full sweep) |
| `results_quick.json` | raw measurements (`--quick` smoke run) |
| `RESULTS.md` | benchmark tables + scaling/robustness story + verdict |

## Run

```sh
VENV=/Users/ochafik/github/.ddocs-venv/bin/python
cd ddocs/prototypes/p2_boolean_benchmark

$VENV run_benchmark.py            # full sweep (minutes; large OCC runs are slow)
$VENV run_benchmark.py --quick    # small sizes only (fast smoke run)
$VENV make_tables.py results.json # regenerate the markdown tables
```

## Method notes

- **OCC side (A)**: build123d `Box`/`Sphere`/`Cylinder` `Part` objects combined
  with `+` / `-` / `&`. Routes through `Shape._bool_op` ->
  `BRepAlgoAPI_*` + `ShapeUpgrade_UnifySameDomain` (the auto-clean on every op).
- **manifold side (B)**: primitives built natively as `manifold3d.Manifold`,
  booleans via `batch_boolean(..., OpType)` or `+`/`-`/`^`. `volume()`/`num_tri()`
  force the lazy evaluation so timings are real. The optional back-conversion to
  a BREP `Solid` (`manifold_to_solid`, the per-triangle `BRepBuilderAPI_Sewing`
  bake) is timed separately - manifold timings are reported both **without** and
  **with** the bake.
- **Timeouts**: every single run executes in a `spawn` child process killed
  after a per-run timeout (120 s full / 60 s quick). A hanging OCC boolean or a
  manifold segfault is recorded as `TIMEOUT` / `CRASH`, not a stall.
- **Warmup**: the first OCC boolean and the first sew-bake in a fresh process
  pay a one-time lazy-import cost (~2 s for the sew modules). Each workload calls
  a `_warmup_*` helper once *outside* the measured region so the comparison is
  apples-to-apples.
- **Vertex weld**: build123d `tessellate()` emits a non-indexed soup with
  duplicate seam vertices; manifold3d rejects that as `NotManifold`. `bridge.py`
  grid-snaps to 1e-6 and dedups (same scheme as `Mesher._create_3mf_mesh`).

manifold3d here is **3.4.1** (research doc 03 introspected 2.3.1) - it has
`batch_boolean`, `OpType`, double-precision kernel; `Manifold.volume()` is a
method.
