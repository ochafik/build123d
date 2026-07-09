"""cProfile the to_solid() bake in isolation for one grid size.

Usage:
    PYTHONPATH=<worktree>/src <venv>/bin/python -u ddocs/design/bake_profile_v1.py <grid_n>
"""
import cProfile
import pstats
import sys
import time

from build123d import Location
from build123d.mesh import MeshPart, mesh_cut


def log(msg):
    print(msg, flush=True)


def build(grid_n, pitch=9.0, thickness=4.0, radius=0.5):
    span = (grid_n - 1) * pitch
    side = span + 30.0
    panel = MeshPart.box(side, side, thickness)
    start = -span / 2.0
    holes = [
        MeshPart.cylinder(radius=2.0, height=thickness * 3).move(
            Location((start + i * pitch, start + j * pitch, 0))
        )
        for i in range(grid_n)
        for j in range(grid_n)
    ]
    drilled = mesh_cut(panel, *holes)
    _ = drilled.manifold.volume()
    chains = drilled.feature_edges()
    result = drilled.fillet(chains, radius=radius, on_infeasible="skip")
    return result


if __name__ == "__main__":
    grid_n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    log(f"building grid {grid_n} (drilled + filleted mesh)...")
    t0 = time.time()
    result = build(grid_n)
    log(f"  build+cut+fillet took {time.time() - t0:.1f}s")

    log("profiling to_solid()...")
    profiler = cProfile.Profile()
    t0 = time.time()
    profiler.enable()
    solid = result.to_solid()
    profiler.disable()
    t_bake = time.time() - t0
    log(f"  to_solid() took {t_bake:.1f}s")

    stats = pstats.Stats(profiler)
    stats.sort_stats("cumulative")
    out_path = f"/tmp/bake_profile_grid{grid_n}.pstats"
    stats.dump_stats(out_path)
    log(f"  dumped profile to {out_path}")
    stats.print_stats(25)
    log("--- sorted by tottime ---")
    stats.sort_stats("tottime")
    stats.print_stats(25)
