"""Dedicated 1M-triangle measurement for the direct-nofix approach.

The combined scaling benchmark's 1M row can be dominated by *measurement*
overhead (BRepCheck_Analyzer + a TopTools map over ~1M faces) rather than the
reconstruction we care about. This script separates the phases:

  1. reconstruction  -- mesh -> build123d Solid (the thing that matters)
  2. volume          -- BRepGProp.VolumeProperties (cheap)
  3. face count      -- TopExp_Explorer walk (no map allocation)
  4. validity        -- BRepCheck_Analyzer (OPTIONAL, very memory-heavy)
  5. STEP export     -- can we even write 1M faces?

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -u probe_million.py [--validity] [--step]
"""
import sys
import time
import gc
import os
import tempfile

from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum

from common import sphere_mesh_manifold, timed
from m2b import mesh_to_solid_direct_nofix


def log(*a):
    print(*a)
    sys.stdout.flush()


def face_count_walk(solid):
    exp = TopExp_Explorer(solid.wrapped, TopAbs_ShapeEnum.TopAbs_FACE)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def main():
    do_validity = "--validity" in sys.argv
    do_step = "--step" in sys.argv
    log("=== 1M-triangle direct reconstruction (no-fix) ===\n")

    log("generating r=20 sphere mesh, ~1M triangles via manifold3d refine ...")
    (v, t, man), t_gen = timed(sphere_mesh_manifold, 1_000_000)
    log(f"  mesh: {len(t)} triangles, {len(v)} verts  "
        f"(gen {t_gen:.1f}s)  manifold vol={man.volume():.3f}")

    gc.collect()
    log("\nreconstructing (direct, no ShapeFix) ...")
    (solid, info), t_recon = timed(mesh_to_solid_direct_nofix, v, t)
    ntri = len(t)
    if solid is None:
        log(f"  -> NO SOLID after {t_recon:.1f}s  info={info}")
        return
    log(f"  reconstruction : {t_recon:.2f} s  -> {ntri/t_recon:.0f} tri/s")
    log(f"  result kind    : {info['result_kind']}  "
        f"unique edges built: {info['n_unique_edges']}  "
        f"face_fail={info['face_fail']} edge_fail={info['edge_fail']}")

    props = GProp_GProps()
    t0 = time.perf_counter()
    BRepGProp.VolumeProperties_s(solid.wrapped, props)
    vol = props.Mass()
    verr = abs(vol - man.volume()) / man.volume() * 100
    log(f"  volume         : {vol:.3f}  err={verr:.4f}%  "
        f"({time.perf_counter()-t0:.2f} s)")

    t0 = time.perf_counter()
    nf = face_count_walk(solid)
    log(f"  face count     : {nf}  ({time.perf_counter()-t0:.2f} s, "
        f"explorer walk)")

    if do_validity:
        from OCP.BRepCheck import BRepCheck_Analyzer
        log("\n  running BRepCheck_Analyzer (memory-heavy at 1M faces) ...")
        t0 = time.perf_counter()
        valid = BRepCheck_Analyzer(solid.wrapped).IsValid()
        log(f"  BRepCheck valid: {valid}  ({time.perf_counter()-t0:.1f} s)")
    else:
        log("\n  (validity check skipped -- pass --validity; very slow at 1M)")

    if do_step:
        from build123d import export_step
        path = os.path.join(tempfile.gettempdir(), "p7_million.step")
        log("\n  STEP export of the 1M-face solid ...")
        ok, t_w = timed(export_step, solid, path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        log(f"  STEP export    : ok={ok}  {size/1024/1024:.1f} MB  "
            f"({t_w:.1f} s)")
    else:
        log("\n  (STEP export skipped -- pass --step)")


if __name__ == "__main__":
    main()
