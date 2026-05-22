"""Performance scaling: time each mesh->Solid approach at
~1k / 10k / 100k / 1M triangles. Produces throughput + time tables.

Triangle count is controlled with manifold3d's refine(n) on an r=20 sphere:
refine multiplies triangle count by n^2 while keeping the mesh a guaranteed
valid 2-manifold (the p1 driver's trick -- OCC sphere tessellation saturates
at a fixed triangle count, so deflection sweeps do NOT work).

Each approach has its own size cap, set from MEASURED behaviour:
  * A sewing        -- super-linear; capped (minutes already at 1e5).
  * B direct+fix    -- ShapeFix_Shell is super-linear; capped.
  * B direct nofix  -- near-linear; run all the way to 1e6.

Output is flushed per-line so partial progress is never lost.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -u probe_scaling.py
"""
import sys
import gc

from common import (sphere_mesh_manifold, volume, is_valid, count_distinct,
                    timed)
from m2b import (mesh_to_solid_sewing, mesh_to_solid_direct,
                 mesh_to_solid_direct_nofix)


SEWING_CAP = 150_000      # sewing beyond this would take many minutes
FIX_CAP = 150_000         # ShapeFix_Shell beyond this is super-linear

TARGETS = [1_000, 10_000, 100_000, 1_000_000]


def log(*a):
    print(*a)
    sys.stdout.flush()


def run(label, fn, verts, tris, man_vol, want_facecount=True):
    gc.collect()
    (solid, info), dt = timed(fn, verts, tris)
    ntri = len(tris)
    tps = ntri / dt if dt else 0
    if solid is None:
        log(f"  {label}: {dt:9.2f} s  -> NO SOLID")
        return (dt, tps, None, None, None)
    valid = is_valid(solid)
    try:
        vol = volume(solid)
        verr = abs(vol - man_vol) / man_vol * 100 if man_vol else float("nan")
    except Exception:
        vol, verr = float("nan"), float("nan")
    nf = count_distinct(solid, "face") if want_facecount else -1
    log(f"  {label}: {dt:9.2f} s  {tps:10.0f} tri/s  "
        f"valid={valid!s:5s}  faces={nf}  vol_err={verr:.4f}%")
    return (dt, tps, valid, vol, nf)


def main():
    log("=== mesh -> build123d Solid scaling benchmark ===")
    log("(r=20 sphere, triangle count via manifold3d refine())\n")

    rows = []
    for target in TARGETS:
        v, t, man = sphere_mesh_manifold(target)
        ntri = len(t)
        man_vol = man.volume()
        log(f"--- target {target}: {ntri} triangles, {len(v)} verts, "
            f"manifold vol={man_vol:.3f} ---")

        res = {}
        if ntri <= SEWING_CAP:
            res["sew"] = run("A sewing       ", mesh_to_solid_sewing,
                             v, t, man_vol)
        else:
            log(f"  A sewing       : SKIPPED (>{SEWING_CAP} tris -- minutes)")
            res["sew"] = None

        if ntri <= FIX_CAP:
            res["fix"] = run("B direct+fix   ", mesh_to_solid_direct,
                             v, t, man_vol)
        else:
            log(f"  B direct+fix   : SKIPPED (ShapeFix_Shell super-linear)")
            res["fix"] = None

        # at 1e6 the face-count map allocation is memory-heavy; still run it
        res["nofix"] = run("B direct nofix ", mesh_to_solid_direct_nofix,
                           v, t, man_vol, want_facecount=(ntri <= 200_000))
        log("")
        rows.append((target, ntri, res))

    # ---- tables --------------------------------------------------------
    log("\n=== TIME TABLE (seconds, lower is better) ===\n")
    hdr = (f"{'target':>9s} {'tris':>9s} | {'A sewing':>16s} | "
           f"{'B direct+fix':>16s} | {'B direct nofix':>16s}")
    log(hdr)
    log("-" * len(hdr))
    for target, ntri, res in rows:
        cells = []
        for key in ["sew", "fix", "nofix"]:
            r = res.get(key)
            cells.append(f"{'skipped':>16s}" if r is None
                         else f"{r[0]:>16.3f}")
        log(f"{target:>9d} {ntri:>9d} | {cells[0]} | {cells[1]} | {cells[2]}")

    log("\n=== THROUGHPUT TABLE (tri/s, higher is better) ===\n")
    log(hdr)
    log("-" * len(hdr))
    for target, ntri, res in rows:
        cells = []
        for key in ["sew", "fix", "nofix"]:
            r = res.get(key)
            cells.append(f"{'skipped':>16s}" if r is None
                         else f"{r[1]:>16.0f}")
        log(f"{target:>9d} {ntri:>9d} | {cells[0]} | {cells[1]} | {cells[2]}")

    # scaling exponent for the nofix path: time ~ tris^k
    log("\n=== direct-nofix scaling exponent (time ~ tris^k; k~1 => linear) ===\n")
    import math
    prev = None
    for target, ntri, res in rows:
        r = res.get("nofix")
        if r is None:
            continue
        if prev is not None:
            pt, pdt = prev
            if pdt and r[0]:
                k = math.log(r[0] / pdt) / math.log(ntri / pt)
                log(f"  {pt:>8d} -> {ntri:>8d} tris : k = {k:.2f}")
        prev = (ntri, r[0])


if __name__ == "__main__":
    main()
