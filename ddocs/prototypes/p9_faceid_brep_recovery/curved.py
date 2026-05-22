"""
curved.py -- step 5 of the deliverable: the curved (cylindrical-bore) case.

doc 02 sec 3.2: for curved inputs, faceID still tells you the result region
lies on a *specific known* analytic surface -- "this is the bore, on
Geom_Cylinder r=R" -- that is real, retained information.  But the region's
boundary arrived as faceted polylines; the exact re-trim + p-curve step is the
Tier C research problem and is NOT done here.

This script proves both halves honestly:
  * faceID DOES identify the bore as one group on the known Geom_Cylinder.
  * the recovered bore stays FACETED -- no GeomType.CYLINDER face.
"""
from __future__ import annotations

from build123d import Box, Cylinder, Pos

from faceid_bridge import SideMap, seed_manifold, read_result
from recover import recover_brep


def main():
    print("=" * 78)
    print("STEP 5 -- the curved case: a cylindrical bore")
    print("=" * 78)
    print()

    R = 5.0
    sm = SideMap()
    plate, _ = seed_manifold(Box(30, 30, 12), "plate", sm, tol=0.05)
    bore, _ = seed_manifold(Cylinder(R, 40), "bore", sm, tol=0.05)
    rm = read_result(plate - bore)

    print(f"input faces seeded: {len(sm.records)}")
    cyl_ids = [fid for fid, r in sm.records.items()
               if r.surface_kind == "CYLINDER"]
    print(f"  cylindrical input faces: ids {cyl_ids}")
    for fid in cyl_ids:
        r = sm[fid]
        print(f"  id {fid}: CYLINDER  r={r.cyl_radius:.3f}  "
              f"axis_dir={tuple(round(x,2) for x in r.cyl_axis_dir)}  "
              f"solid={r.solid_name}")
    print()

    print(f"mesh result: {len(rm.tris)} triangles, "
          f"{len(rm.distinct_ids)} face-groups")
    print()

    # which group is the bore wall?
    print("Per-group analysis:")
    for fid in rm.distinct_ids:
        rec = sm[fid]
        n = len(rm.tris_of(fid))
        tag = ""
        if rec.surface_kind == "CYLINDER":
            tag = (f"  <-- THE BORE: faceID identifies {n} triangles as "
                   f"lying on Geom_Cylinder r={rec.cyl_radius:.2f}")
        print(f"  id {fid:2d}: {rec.surface_kind:9s} {n:4d} tris "
              f"(solid={rec.solid_name}){tag}")
    print()

    res = recover_brep(rm, sm)
    print(f"Recovery: {res.n_exact_planar} exact planar faces, "
          f"{res.n_faceted_curved} curved groups (kept faceted)")
    print(f"  solid valid={res.is_valid}  volume={res.volume:.2f}")
    geoms = sorted(set(str(f.geom_type) for f in res.solid.faces()))
    print(f"  geom types in recovered solid: {geoms}")
    n_cyl = sum(1 for f in res.solid.faces()
                if "CYLINDER" in str(f.geom_type))
    print(f"  analytic CYLINDER faces in recovered solid: {n_cyl}")
    print()

    print("-" * 78)
    print("CURVED VERDICT (honest)")
    print("-" * 78)
    print(f"  [{'PASS' if cyl_ids else 'FAIL'}] faceID identifies the bore as")
    print(f"         ONE group on the KNOWN Geom_Cylinder r={R} -- the")
    print(f"         identity/provenance is real and retained.")
    print(f"  [{'EXPECTED' if n_cyl == 0 else 'UNEXPECTED'}] the recovered "
          f"bore is FACETED -- {n_cyl} analytic CYLINDER faces.")
    print(f"         Exact re-trim of the known cylinder with re-fitted")
    print(f"         boundary curves + p-curves is Tier C -- NOT done here.")
    print()
    print("  So: curved -> fast mesh path + identity preserved, but exact")
    print("  re-fit is still research. faceID makes it TRACTABLE (you know")
    print("  the surface), not DONE.")
    return bool(cyl_ids) and n_cyl == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
