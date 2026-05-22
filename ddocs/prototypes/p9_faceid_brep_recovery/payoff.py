"""
payoff.py -- step 4 of the deliverable: THE HEADLINE CLAIM.

Take an all-planar CSG result (a Box with a rectangular notch cut out) via the
manifold mesh path; recover the exact B-rep with faceID-grouped reconstruction
(recover.py); then run a REAL build123d ``fillet()`` and ``chamfer()`` on a
recovered feature edge.

If this works, it proves: mesh-CSG + faceID seeding -> exact filletable B-rep,
in pure Python.  That is the claim doc 02 sec 3.2 makes for the C++ prototype;
here we verify it honestly at the Python level.
"""
from __future__ import annotations

import os

from build123d import Box, Pos, export_step
from build123d import GeomType, Axis, SortBy

from faceid_bridge import SideMap, seed_manifold, read_result
from recover import recover_brep

from OCP.BRepCheck import BRepCheck_Analyzer

OUT = os.path.dirname(os.path.abspath(__file__))


def check(label, shape):
    """Run BRepCheck and report validity + volume."""
    ana = BRepCheck_Analyzer(shape.wrapped)
    ok = ana.IsValid()
    try:
        vol = shape.volume
    except Exception:
        vol = float("nan")
    print(f"    {label:34s} valid={ok}  volume={vol:.3f}")
    return ok


def main():
    print("=" * 78)
    print("STEP 4 -- THE PAYOFF: real fillet/chamfer on a mesh-CSG result")
    print("=" * 78)
    print()

    # The workload: a 40x30x12 plate with a 14x10 blind rectangular pocket
    # cut into the top.  The pocket has 4 CONCAVE vertical feature edges --
    # exactly the edges a mechanical part gets filleted/chamfered on.
    def plate():
        return Box(40, 30, 12)

    def pocket():
        # centered, raised so it cuts only the top -> a blind pocket
        return Pos(0, 0, 3) * Box(14, 10, 12)

    # --- 1. native build123d reference (what we are trying to match) ----
    ref = plate() - pocket()
    print("Reference (native build123d boolean):")
    check("native Box - pocket", ref)
    print(f"    faces={len(ref.faces())}  edges={len(ref.edges())}")
    print()

    # --- 2. the manifold mesh path -------------------------------------
    print("Manifold mesh-CSG path with seeded face_id:")
    sm = SideMap()
    plate_man, i1 = seed_manifold(plate(), "plate", sm)
    notch_man, i2 = seed_manifold(pocket(), "pocket", sm)
    rm = read_result(plate_man - notch_man)
    print(f"    mesh result: {len(rm.tris)} triangles, "
          f"{len(rm.distinct_ids)} face-groups (input faces={len(sm.records)})")
    print()

    # --- 3. faceID-grouped exact recovery ------------------------------
    print("faceID-grouped exact B-rep recovery:")
    res = recover_brep(rm, sm)
    solid = res.solid
    print(f"    {res.n_exact_planar} exact planar faces, "
          f"{res.n_faceted_curved} curved patches")
    print(f"    {res.n_feature_edges} feature edges (faceID boundaries)")
    ok_rec = check("recovered Solid", solid)
    print(f"    geom types present: "
          f"{sorted(set(str(f.geom_type) for f in solid.faces()))}")
    # exactness: volume must match the native reference, not approximate it
    err = abs(solid.volume - ref.volume)
    print(f"    volume error vs native B-rep: {err:.6f}  "
          f"({'EXACT' if err < 1e-6 else 'APPROX'})")
    print()

    # the 4 concave vertical feature edges of the pocket are length 9
    # (pocket depth); the 4 outer box corners are length 12.
    def pocket_corner_edges(s):
        return [e for e in s.edges().filter_by(Axis.Z)
                if abs(e.length - 9.0) < 1e-3]

    # --- 4. the headline: a REAL fillet on a recovered feature edge ----
    print("REAL build123d fillet() on a recovered pocket feature edge:")
    corners = pocket_corner_edges(solid)
    print(f"    found {len(corners)} concave pocket feature edges "
          f"(expected 4)")
    target = corners[0]
    print(f"    target edge: vertical, length={target.length:.2f}, "
          f"center={tuple(round(x,1) for x in (target.center().X, target.center().Y, target.center().Z))}")

    filleted = solid.fillet(2.0, [target])
    ok_fil = check("filleted Solid (r=2.0)", filleted)
    fil_geoms = sorted(set(str(f.geom_type) for f in filleted.faces()))
    print(f"    geom types after fillet: {fil_geoms}")
    has_cyl = any("CYLINDER" in g for g in fil_geoms)
    print(f"    fillet produced an analytic CYLINDER blend face: {has_cyl}")
    print()

    # --- 5. a REAL chamfer on another recovered feature edge -----------
    print("REAL build123d chamfer() on a recovered pocket feature edge:")
    target2 = corners[1]
    print(f"    target edge: vertical, length={target2.length:.2f}, "
          f"center={tuple(round(x,1) for x in (target2.center().X, target2.center().Y, target2.center().Z))}")
    chamfered = solid.chamfer(1.5, None, [target2])
    ok_cham = check("chamfered Solid (1.5)", chamfered)
    print()

    # --- 6. fillet ALL FOUR pocket edges at once -----------------------
    print("Combined: fillet all 4 pocket feature edges at once:")
    combo = solid.fillet(2.0, pocket_corner_edges(solid))
    ok_combo = check("4-edge filleted Solid", combo)
    combo_cyl = sum(1 for f in combo.faces()
                    if "CYLINDER" in str(f.geom_type))
    print(f"    analytic CYLINDER blend faces created: {combo_cyl} "
          f"(expected 4)")
    print()

    # --- 7. STEP export + validity -------------------------------------
    print("STEP export of the recovered + filleted solid:")
    step_path = os.path.join(OUT, "recovered_filleted.step")
    export_step(filleted, step_path)
    size = os.path.getsize(step_path)
    print(f"    wrote {step_path} ({size} bytes)")
    # re-import to confirm the STEP is readable & valid
    from build123d import import_step
    reimported = import_step(step_path)
    ok_step = check("re-imported STEP", reimported)
    print()

    print("-" * 78)
    print("PAYOFF VERDICT")
    print("-" * 78)
    results = {
        "recovered solid valid": ok_rec,
        "volume EXACT (err<1e-6)": err < 1e-6,
        "fillet succeeded & valid": ok_fil,
        "fillet made analytic CYLINDER blend": has_cyl,
        "chamfer succeeded & valid": ok_cham,
        "fillet+chamfer combined valid": ok_combo,
        "STEP exported & re-imports valid": ok_step,
    }
    for k, v in results.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    allpass = all(results.values())
    print()
    print(f"  ==> {'ALL PASS -- ' if allpass else 'SOME FAIL -- '}"
          f"faceID-seeded exact B-rep recovery + real fillet/chamfer "
          f"{'WORKS' if allpass else 'is INCOMPLETE'} in pure Python.")
    return allpass


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
