"""
scaling_cliff.py -- expose the mesh->Solid scaling cliff.

Doc 02 claims `BRepBuilderAPI_Sewing` over N triangle-faces is super-linear and
"minutes-slow" at 1e4-1e5 triangles, making the IN leg "the wall".  This script
measures it directly.

NOTE on the test driver: OCC tessellation of a sphere SATURATES at a fixed
triangle count -- linear deflection is *relative* (doc 02 sec 1.1) and angular
tolerance dominates on a uniform-curvature sphere, so finer `tolerance` does NOT
keep adding triangles.  To get a clean controlled triangle sweep we instead
take ONE base sphere Manifold and `refine(n)` it: refine(n) splits every edge
into n, multiplying triangle count by n^2 while keeping the mesh a guaranteed
valid 2-manifold.  We then run only the IN leg (mesh->Solid sewing) on each.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python scaling_cliff.py
"""

from __future__ import annotations

import math
import time

from build123d import Sphere
from bridge import solid_to_manifold, manifold_to_solid


# refine factors -> triangle counts roughly 8k, 32k, 130k, 290k, 515k, 1.16M
REFINE_FACTORS = [1, 2, 4, 6, 8]

# Cap per-IN-leg wall time; a stage exceeding this stops the sweep -- and that
# stop point IS the headline finding.
TIME_BUDGET_S = 300.0


def main():
    print("=" * 100)
    print("mesh->Solid SCALING CLIFF   (sphere r=10, manifold3d 3.4.1)")
    print("IN leg = BRepBuilderAPI_Sewing of N planar triangle-faces (the Mesher path)")
    print("triangle count scaled via manifold3d refine(n) on one base sphere")
    print("=" * 100)
    print()

    # One coarse base sphere; refine() multiplies its triangle count.
    base, base_info = solid_to_manifold(Sphere(10), tolerance=2e-2,
                                        angular_tolerance=0.2)
    src_volume = Sphere(10).volume
    print(f"base sphere manifold: {base.num_tri()} tris, "
          f"{base.num_vert()} verts, volume {base.volume():.4f}")
    print()

    cols = [
        ("refine",    "refine",     7),
        ("tris",      "tris",      10),
        ("faces_s",   "buildF(s)", 11),
        ("sew_s",     "SEW(s)",    11),
        ("solid_s",   "mkSolid(s)",11),
        ("in_s",      "IN.tot(s)", 11),
        ("us_per_tri","us/tri",     9),
        ("kind",      "result",     9),
        ("valid",     "valid",      6),
        ("vol_err",   "vol.relErr",12),
    ]
    header = "  ".join(f"{h:>{w}}" for _, h, w in cols)
    print(header)
    print("-" * len(header))

    rows = []
    prev_tris = prev_sew = None

    for nf in REFINE_FACTORS:
        man = base if nf == 1 else base.refine(nf)
        tris = man.num_tri()

        t0 = time.perf_counter()
        back, in_info = manifold_to_solid(man)
        in_s = time.perf_counter() - t0

        vol_err = abs(back.volume - src_volume) / src_volume
        row = dict(
            refine=nf, tris=tris,
            faces_s=in_info["t_build_faces"],
            sew_s=in_info["t_sewing"],
            solid_s=in_info["t_make_solid"],
            in_s=in_s,
            us_per_tri=in_s / tris * 1e6,
            kind=in_info["result_kind"],
            valid=back.is_valid,
            vol_err=vol_err,
        )

        if prev_tris is not None and prev_sew and row["sew_s"]:
            row["sew_exponent"] = (math.log(row["sew_s"] / prev_sew)
                                   / math.log(tris / prev_tris))
        prev_tris, prev_sew = tris, row["sew_s"]
        rows.append(row)

        def f(x):
            if isinstance(x, float):
                if x != 0 and (abs(x) < 1e-3 or abs(x) >= 1e6):
                    return f"{x:.2e}"
                return f"{x:.4f}"
            return str(x)

        print("  ".join(f"{f(row[k]):>{w}}" for k, _, w in cols))

        if in_s > TIME_BUDGET_S:
            print(f"\n  [stopped: a single IN-leg exceeded the {TIME_BUDGET_S}s "
                  f"budget -- THIS IS THE CLIFF]")
            break

    print()
    print("### Sewing-time scaling exponent  (sew_time ~ tris^k)")
    print("    k ~ 1.0 => linear;  k > 1.0 => super-linear (the cliff).")
    for row in rows:
        if "sew_exponent" in row:
            print(f"    {row['tris']:>9} tris : k = {row['sew_exponent']:.2f}")
    print()
    return rows


if __name__ == "__main__":
    main()
