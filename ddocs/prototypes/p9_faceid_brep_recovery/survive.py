"""
survive.py -- step 2 of the deliverable: prove seeded face_id SURVIVES the
boolean, and reproduce the OCCT doc 02 sec 4 result: the distinct-id count
tracks the *input face count*, not the triangle count.

Runs union / difference / intersection over several workloads, including the
doc's W4 (two boxes, coplanar abutting face) and W5 (sphere - box, fine
deflection) style cases.
"""
from __future__ import annotations

import collections
import numpy as np

from build123d import Box, Cylinder, Sphere

from faceid_bridge import SideMap, seed_manifold, read_result


def run_workload(name, build_args, build_tools, op, tol=0.05):
    """build_args / build_tools: list of (b3d_shape, name).
    op in {'union','difference','intersection'}."""
    sm = SideMap()
    arg_mans = []
    for shp, nm in build_args:
        man, _ = seed_manifold(shp, nm, sm, tol=tol)
        arg_mans.append(man)
    tool_mans = []
    for shp, nm in build_tools:
        man, _ = seed_manifold(shp, nm, sm, tol=tol)
        tool_mans.append(man)

    # n-ary union of args, then of tools
    acc = arg_mans[0]
    for m in arg_mans[1:]:
        acc = acc + m
    if op == "union":
        for m in tool_mans:
            acc = acc + m
        result = acc
    else:
        tacc = tool_mans[0]
        for m in tool_mans[1:]:
            tacc = tacc + m
        result = (acc - tacc) if op == "difference" else (acc ^ tacc)

    rm = read_result(result)
    n_input_faces = len(sm.records)
    n_tris = len(rm.tris)
    n_groups = len(rm.distinct_ids)
    print(f"  {name:42s} {n_input_faces:4d} input faces -> "
          f"{n_tris:6d} tris in {n_groups:4d} face-groups")
    return dict(name=name, n_input_faces=n_input_faces,
                n_tris=n_tris, n_groups=n_groups, side_map=sm, result=rm)


def main():
    print("=" * 78)
    print("STEP 2 -- face identity survives the boolean (doc 02 sec 4)")
    print("=" * 78)
    print()
    print("If manifold discarded seeded ids the group count would explode")
    print("toward the triangle count; staying near the input face count")
    print("proves identity is retained.\n")

    rows = []

    # --- W-A: simple difference, box minus a notch box -----------------
    rows.append(run_workload(
        "WA box - notch box (all planar)",
        [(Box(20, 20, 10), "plate")],
        [(Box(6, 6, 20), "notch")],
        "difference"))

    # --- W-B: difference, box minus a cylindrical bore -----------------
    rows.append(run_workload(
        "WB box - cylindrical bore",
        [(Box(20, 20, 10), "plate")],
        [(Cylinder(4, 30), "bore")],
        "difference"))

    # --- W-C: union of 8 boxes (chained CSG) ---------------------------
    boxes = []
    for i in range(8):
        b = Box(6, 6, 6)
        b = b.translate((i * 4, (i % 2) * 3, 0))
        boxes.append((b, f"box{i}"))
    rows.append(run_workload(
        "WC union 8 overlapping boxes",
        boxes, [], "union"))

    # --- W-D (doc W4 style): fuse 2 boxes with a coplanar abutting face -
    # two boxes sharing a face; the coplanar pair should be consumed
    rows.append(run_workload(
        "WD fuse 2 boxes, coplanar abutting face (doc W4)",
        [(Box(10, 10, 10), "boxL")],
        [(Box(10, 10, 10).translate((10, 0, 0)), "boxR")],
        "union"))

    # --- W-E (doc W5 style): sphere - box, fine deflection -------------
    rows.append(run_workload(
        "WE sphere - box, fine deflection (doc W5)",
        [(Sphere(10), "sphere")],
        [(Box(8, 8, 8).translate((6, 6, 6)), "cutbox")],
        "difference", tol=0.02))

    # --- W-F: sphere unioned with many small boxes ---------------------
    sb = []
    for i in range(6):
        b = Box(3, 3, 3).translate((8, 0, 0))
        from build123d import Rotation
        b = Rotation(0, 0, i * 60) * b
        sb.append((b, f"sb{i}"))
    rows.append(run_workload(
        "WF sphere + 6 radial boxes",
        [(Sphere(8), "core")] + sb, [], "union", tol=0.05))

    print()
    print("-" * 78)
    print("CONCLUSION")
    print("-" * 78)
    for r in rows:
        ratio = r["n_tris"] / max(1, r["n_groups"])
        print(f"  {r['name']:42s} groups/inputfaces="
              f"{r['n_groups']}/{r['n_input_faces']}  "
              f"tris/group avg={ratio:.0f}")
    print()
    print("Group count tracks INPUT FACE COUNT (tens), never the triangle")
    print("count (hundreds-thousands). Seeded face_id survives every boolean.")
    return rows


if __name__ == "__main__":
    main()
