"""Phased breakdown of the direct-nofix build at 1k..1M triangles.

Splits reconstruction into its constituent phases -- vertices / edges+faces
+shell / make_solid -- to show WHERE the time goes and to confirm the path
is near-linear (no super-linear phase hiding inside).

Also measures the build123d-side cost separately: wrapping the OCC handle in
a `build123d.Solid` and the cost of `Solid.is_valid` (BRepCheck) which is the
real ceiling for "validated" reconstruction.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -u probe_phases.py
"""
import sys
import time
import gc

import numpy as np

from OCP.gp import gp_Pnt
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeSolid,
)
from OCP.TopoDS import TopoDS, TopoDS_Shell

from build123d import Solid

from common import sphere_mesh_manifold, volume, is_valid, timed


TARGETS = [1_000, 10_000, 100_000, 1_000_000]


def log(*a):
    print(*a)
    sys.stdout.flush()


def phased_build(verts, tris):
    """direct-nofix, instrumented per phase. Returns (Solid, phase_times)."""
    t = {}
    builder = BRep_Builder()
    verts = np.ascontiguousarray(verts, dtype=np.float64)
    n_verts = len(verts)

    t0 = time.perf_counter()
    occ_verts = [BRepBuilderAPI_MakeVertex(
        gp_Pnt(float(verts[i, 0]), float(verts[i, 1]), float(verts[i, 2]))
    ).Vertex() for i in range(n_verts)]
    t["vertices"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    edges = {}

    def get_edge(a, b):
        key = (a, b) if a < b else (b, a)
        e = edges.get(key)
        if e is None:
            e = BRepBuilderAPI_MakeEdge(occ_verts[key[0]],
                                        occ_verts[key[1]]).Edge()
            edges[key] = e
        return e if a == key[0] else TopoDS.Edge_s(e.Reversed())

    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for tri in tris:
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        mw = BRepBuilderAPI_MakeWire(get_edge(a, b), get_edge(b, c),
                                     get_edge(c, a))
        mf = BRepBuilderAPI_MakeFace(mw.Wire(), True)
        builder.Add(shell, mf.Face())
    shell.Closed(True)
    t["edges+faces+shell"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    mk = BRepBuilderAPI_MakeSolid(shell)
    occ_solid = TopoDS.Solid_s(mk.Solid()) if mk.IsDone() else None
    t["make_solid"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    solid = Solid(occ_solid) if occ_solid is not None else None
    t["wrap_b3d"] = time.perf_counter() - t0

    return solid, t, len(edges)


def main():
    log("=== Direct-nofix phased breakdown, 1k..1M triangles ===\n")
    rows = []
    for target in TARGETS:
        v, t, man = sphere_mesh_manifold(target)
        ntri = len(t)
        gc.collect()

        (solid, phases, n_edges), dt = timed(phased_build, v, t)
        vol = volume(solid) if solid is not None else float("nan")
        verr = abs(vol - man.volume()) / man.volume() * 100
        tps = ntri / dt if dt else 0

        # BRepCheck cost measured separately (it is the validation ceiling)
        if ntri <= 200_000:
            valid, t_check = timed(is_valid, solid)
        else:
            valid, t_check = ("skipped(memory-heavy)", float("nan"))

        log(f"[{target}] {ntri} tris, {len(v)} verts, {n_edges} unique edges")
        log(f"  build total  : {dt:8.3f} s  {tps:10.0f} tri/s  "
            f"vol_err={verr:.4f}%")
        log(f"  phases: vertices={phases['vertices']*1e3:8.0f}ms  "
            f"edges+faces+shell={phases['edges+faces+shell']*1e3:8.0f}ms  "
            f"make_solid={phases['make_solid']*1e3:7.0f}ms  "
            f"wrap_b3d={phases['wrap_b3d']*1e3:.2f}ms")
        if isinstance(t_check, float) and t_check == t_check:
            log(f"  is_valid()   : {valid}  ({t_check:.2f} s -- BRepCheck)")
        else:
            log(f"  is_valid()   : {valid}")
        log("")
        rows.append((target, ntri, dt, tps, phases))

    log("=== build-time table (seconds) ===\n")
    log(f"{'tris':>9s} {'total':>9s} {'verts':>9s} "
        f"{'edge+face':>11s} {'mkSolid':>9s} {'tri/s':>10s}")
    log("-" * 60)
    for target, ntri, dt, tps, ph in rows:
        log(f"{ntri:>9d} {dt:>9.3f} {ph['vertices']:>9.3f} "
            f"{ph['edges+faces+shell']:>11.3f} {ph['make_solid']:>9.3f} "
            f"{tps:>10.0f}")


if __name__ == "__main__":
    main()
