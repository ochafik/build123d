"""to_solid() bake scaling harness -- perforated panel, mesh-native primitives.

Question: bp17-style workload (all bore mouths filleted, coarse tessellation)
has mesh_fillet running fast (10.6s for 1800 chains) but to_solid() dominating
the wall (652.7s for 679k faces). Is the bake ~linear in faceted-triangle
count with a large constant, or super-linear? Sweep grid 2..8 and time the
to_solid() call in isolation from mesh_cut/fillet.

Usage:
    PYTHONPATH=<worktree>/src <venv>/bin/python -u ddocs/design/bake_scaling_v1.py
"""
import signal
import sys
import time

from build123d import Location
from build123d.mesh import MeshPart, mesh_cut
from build123d.mesh.bridge import read_result
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer


class Timeout(Exception):
    pass


def _alarm(_sig, _frame):
    raise Timeout()


signal.signal(signal.SIGALRM, _alarm)


def log(msg):
    print(msg, flush=True)


def count_faces(shape) -> int:
    count = 0
    explorer = TopExp_Explorer(shape.wrapped, TopAbs_FACE)
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def run(grid_n, pitch=9.0, thickness=4.0, radius=0.5, bake_cap_s=180):
    n_holes = grid_n * grid_n
    span = (grid_n - 1) * pitch
    side = span + 30.0

    t0 = time.time()
    panel = MeshPart.box(side, side, thickness)
    start = -span / 2.0
    holes = [
        MeshPart.cylinder(radius=2.0, height=thickness * 3).move(
            Location((start + i * pitch, start + j * pitch, 0))
        )
        for i in range(grid_n)
        for j in range(grid_n)
    ]
    t_build = time.time() - t0

    t0 = time.time()
    drilled = mesh_cut(panel, *holes)
    _ = drilled.manifold.volume()
    t_cut = time.time() - t0

    t0 = time.time()
    chains = drilled.feature_edges()
    n_chains = len(chains)
    t_edges = time.time() - t0

    t0 = time.time()
    result = drilled.fillet(chains, radius=radius, on_infeasible="skip")
    t_fillet = time.time() - t0
    rep = getattr(result, "last_fillet_report", None)
    n_skipped = rep.total_skipped if rep else 0

    # Triangle count feeding the bake -- computed once, outside the timed
    # to_solid() call, so the sweep can correlate bake time with mesh size.
    result_mesh = read_result(result.manifold)
    n_triangles = len(result_mesh.triangles)

    t0 = time.time()
    ok = True
    n_faces = "-"
    try:
        signal.alarm(bake_cap_s)
        solid = result.to_solid()
        signal.alarm(0)
        n_faces = count_faces(solid)
    except Timeout:
        ok = False
        n_faces = f">cap({bake_cap_s}s)"
    except Exception as exc:  # noqa: BLE001
        signal.alarm(0)
        ok = False
        n_faces = f"ERR:{type(exc).__name__}:{exc}"
    t_bake = time.time() - t0

    log(
        f"  grid {grid_n:2d} ({n_holes:4d} holes, {n_chains:5d} chains, "
        f"tri={n_triangles:7d}): build={t_build:5.1f}s cut={t_cut:5.1f}s "
        f"edges={t_edges:5.1f}s fillet={t_fillet:6.1f}s(skip={n_skipped}) "
        f"BAKE={t_bake:7.1f}s faces={n_faces} ok={ok}"
    )
    return {
        "grid": grid_n,
        "holes": n_holes,
        "chains": n_chains,
        "triangles": n_triangles,
        "t_bake": t_bake,
        "n_faces": n_faces,
        "ok": ok,
    }


if __name__ == "__main__":
    log("bake_scaling_v1 -- to_solid() wall time vs faceted-triangle count")
    log("=" * 88)
    results = []
    for grid_n in (2, 3, 4, 5, 6, 8):
        try:
            r = run(grid_n)
        except Exception as exc:  # noqa: BLE001
            log(f"  grid {grid_n}: run failed: {type(exc).__name__}: {exc}")
            break
        results.append(r)
        if not r["ok"]:
            log(f"  (stopping ladder -- grid {grid_n} bake was capped/failed)")
            break
    log("=" * 88)
    done = [r for r in results if r["ok"] and isinstance(r["n_faces"], int)]
    if len(done) >= 2:
        log("pairwise bake-time growth vs triangle-count growth:")
        for prev, cur in zip(done, done[1:]):
            tri_ratio = cur["triangles"] / max(prev["triangles"], 1)
            time_ratio = cur["t_bake"] / max(prev["t_bake"], 1e-9)
            exponent = (
                (time_ratio and tri_ratio > 1)
                and (__import__("math").log(time_ratio) / __import__("math").log(tri_ratio))
                or float("nan")
            )
            log(
                f"  grid {prev['grid']:2d}->{cur['grid']:2d}: "
                f"tri x{tri_ratio:5.2f}  bake x{time_ratio:6.2f}  "
                f"local exponent={exponent:4.2f}"
            )
    sys.exit(0)
