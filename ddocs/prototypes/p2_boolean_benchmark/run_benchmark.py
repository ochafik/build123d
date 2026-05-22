"""Boolean benchmark driver: OCC (native build123d) vs manifold3d.

Runs every workload at every size in a subprocess with a hard timeout, so a
hanging OCC boolean or a manifold3d segfault is recorded as a failure instead
of stalling the run. Emits a JSON results file and prints progress.

Usage:
    python run_benchmark.py            # full sweep
    python run_benchmark.py --quick    # small sizes only (fast smoke run)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from bridge import run_with_timeout
import workloads as W

HERE = Path(__file__).parent
TIMEOUT_S = 120.0  # per single run

# size sweeps -------------------------------------------------------------
UNION_NS = [10, 50, 200, 1000, 2000]
CSG_DEPTHS = [4, 8, 12, 16, 20]
DRILL_NS = [10, 50, 200, 500, 1000]
# (case_name, expected_volume) -- expected is the geometrically correct answer
DEGEN_CASES = [
    ("coplanar_touch_union", 2.0),
    ("flush_face_cut", 56.0),
    ("self_subtract_empty", 0.0),
    ("edge_touch_union", 2.0),
    ("tangent_wall_cut", None),  # curved, tolerance-dependent
    ("identical_intersect", 8.0),
    ("zero_wall_cut", 32.0),
    ("coplanar_stack_40", 40.0),
    ("sub_tolerance_offset_cut", None),  # 56.0 ideally; sliver behaviour varies
    ("cylinder_tangent_union", None),  # curved
]

QUICK = "--quick" in sys.argv
if QUICK:
    UNION_NS = [10, 50, 200]
    CSG_DEPTHS = [4, 8]
    DRILL_NS = [10, 50]
    TIMEOUT_S = 60.0


def measure(label, fn, arg):
    """Run one (method, workload, size) cell with timeout; print + return."""
    t0 = time.perf_counter()
    status, payload = run_with_timeout(fn, (arg,), TIMEOUT_S)
    elapsed = time.perf_counter() - t0
    cell = {"label": label, "arg": arg, "status": status, "outer_wall": elapsed}
    if status == "ok":
        cell["data"] = payload
        wall = payload.get("wall_with_bake") or payload.get("wall") or 0.0
        vol = payload.get("volume", float("nan"))
        msg = f"  {label:28s} arg={arg:<6} OK   wall={wall:8.4f}s vol={vol:.2f}"
    elif status == "timeout":
        msg = f"  {label:28s} arg={arg:<6} TIMEOUT (>{TIMEOUT_S:.0f}s)"
    elif status == "crash":
        cell["exitcode"] = payload
        msg = f"  {label:28s} arg={arg:<6} CRASH exit={payload}"
    else:  # err
        cell["traceback"] = payload
        first = payload.strip().splitlines()[-1] if payload else "?"
        msg = f"  {label:28s} arg={arg:<6} ERROR {first}"
    print(msg, flush=True)
    return cell


def main():
    results = {
        "meta": {
            "quick": QUICK,
            "timeout_s": TIMEOUT_S,
            "tess_tol": W.TESS_TOL,
        },
        "workloads": {},
    }

    print("=" * 70)
    print("WORKLOAD 1: union of N overlapping boxes (grid)")
    print("=" * 70)
    cells = []
    for n in UNION_NS:
        cells.append(measure("occ_union_grid", W.occ_union_grid, n))
        cells.append(measure("manifold_union_grid", W.manifold_union_grid, n))
    results["workloads"]["union_grid"] = cells

    print("=" * 70)
    print("WORKLOAD 2: deep CSG tree (alternating diff/union)")
    print("=" * 70)
    cells = []
    for d in CSG_DEPTHS:
        cells.append(measure("occ_csg_tree", W.occ_csg_tree, d))
        cells.append(measure("manifold_csg_tree", W.manifold_csg_tree, d))
    results["workloads"]["csg_tree"] = cells

    print("=" * 70)
    print("WORKLOAD 3: coincident / degenerate cases")
    print("=" * 70)
    cells = []
    for c, expected in DEGEN_CASES:
        occ_cell = measure("occ_degenerate", W.occ_degenerate, c)
        occ_cell["expected_volume"] = expected
        man_cell = measure("manifold_degenerate", W.manifold_degenerate, c)
        man_cell["expected_volume"] = expected
        cells.append(occ_cell)
        cells.append(man_cell)
    results["workloads"]["degenerate"] = cells

    print("=" * 70)
    print("WORKLOAD 4: one body minus N drilled holes")
    print("=" * 70)
    cells = []
    for n in DRILL_NS:
        cells.append(measure("occ_drill", W.occ_drill, n))
        cells.append(measure("manifold_drill", W.manifold_drill, n))
    results["workloads"]["drill"] = cells

    out = HERE / ("results_quick.json" if QUICK else "results.json")
    out.write_text(json.dumps(results, indent=2, default=str))
    print()
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
