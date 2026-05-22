"""The four boolean workloads, each in an OCC variant and a manifold variant.

Every function here is a *top-level* function (picklable for spawn) that
returns a small JSON-able dict of measurements. They are launched inside the
subprocess-timeout harness in `bridge.run_with_timeout`, so a hang or a
segfault in any single run cannot stall the benchmark.

Each variant builds the SAME geometric workload two ways:
  occ_*      -- native build123d Part objects + `+ - &` operators (OCC).
  manifold_* -- tessellate inputs once, do the booleans as manifold3d ops.
                Reports timing with and without the back-conversion to a
                BREP Solid (the "bake").
"""

from __future__ import annotations

import math
import time

# tessellation tolerance used for every OCC->mesh conversion
TESS_TOL = 5e-2


def _warmup_bake():
    """Force the first-call lazy import of OCP sewing modules.

    The very first `manifold_to_solid` in a fresh process pays a ~2 s one-time
    cost importing OCP.BRepBuilderAPI / OCP.BRepGProp etc. That is process
    startup, not algorithm time -- so each manifold workload that times the
    bake calls this once up front, outside the measured region.
    """
    import manifold3d as mf

    from bridge import manifold_to_solid

    manifold_to_solid(mf.Manifold.cube([1, 1, 1], True))


def _warmup_occ():
    """Force the first-call lazy init of the OCC boolean kernel.

    The first BRepAlgoAPI boolean in a fresh process pays a one-time kernel
    warmup. Each OCC workload calls this once before the measured region so
    the comparison against the manifold side is apples-to-apples.
    """
    from build123d import Box, Pos

    _ = Box(1, 1, 1) + Pos(0.5, 0, 0) * Box(1, 1, 1)
    _ = Box(1, 1, 1) - Pos(0.5, 0, 0) * Box(1, 1, 1)


# Above this triangle count the per-triangle OCC sew bake takes minutes; we
# skip it (and report so) rather than blow the per-run timeout. The bake is
# measured linear in triangle count from the smaller cases, so the cost there
# is extrapolatable -- the point of the benchmark is the boolean itself.
BAKE_TRI_CAP = 15000


def _maybe_bake(man):
    """Bake a manifold to an OCC Solid, but only if it is small enough.

    Returns (t_bake, bake_info). When skipped, t_bake is None and bake_info
    records why.
    """
    from bridge import manifold_to_solid

    tris = man.num_tri()
    if tris > BAKE_TRI_CAP:
        return None, {
            "tris": tris,
            "result": f"bake skipped (>{BAKE_TRI_CAP} tris)",
            "skipped": True,
        }
    t0 = time.perf_counter()
    _solid, bake_info = manifold_to_solid(man)
    return time.perf_counter() - t0, bake_info


# ===========================================================================
# helpers
# ===========================================================================


def _grid_positions(n: int, spacing: float):
    """n positions on a roughly-cubic 3D lattice."""
    side = max(1, round(n ** (1 / 3)))
    pts = []
    for i in range(side):
        for j in range(side):
            for k in range(side):
                if len(pts) >= n:
                    return pts
                pts.append((i * spacing, j * spacing, k * spacing))
    # top up if rounding left us short
    extra = side
    while len(pts) < n:
        pts.append((extra * spacing, 0.0, 0.0))
        extra += 1
    return pts


# ===========================================================================
# WORKLOAD 1 -- union of N overlapping boxes in a grid
# ===========================================================================
# spacing 1.6 with size-2 boxes => neighbours overlap by 0.4 on each axis.


def occ_union_grid(n: int):
    from build123d import Box, Pos

    _warmup_occ()
    t0 = time.perf_counter()
    pos = _grid_positions(n, 1.6)
    parts = [Pos(*p) * Box(2, 2, 2) for p in pos]
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    acc = parts[0]
    for p in parts[1:]:
        acc = acc + p  # OCC fuse + ShapeUpgrade_UnifySameDomain each step
    t_bool = time.perf_counter() - t0

    return {
        "n": n,
        "t_build": t_build,
        "t_bool": t_bool,
        "wall": t_build + t_bool,
        "volume": acc.volume,
        "valid": bool(acc.is_valid),
        "faces": len(acc.faces()),
    }


def manifold_union_grid(n: int):
    import manifold3d as mf

    _warmup_bake()
    t0 = time.perf_counter()
    pos = _grid_positions(n, 1.6)
    # tessellation-free: build the primitives natively in manifold
    mans = [mf.Manifold.cube([2, 2, 2], True).translate(list(p)) for p in pos]
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    acc = mf.Manifold.batch_boolean(mans, mf.OpType.Add)
    vol = acc.volume()  # force lazy evaluation
    tris = acc.num_tri()
    t_bool = time.perf_counter() - t0

    t_bake, bake_info = _maybe_bake(acc)

    return {
        "n": n,
        "t_build": t_build,
        "t_bool": t_bool,
        "t_bake": t_bake,
        "wall": t_build + t_bool,
        "wall_with_bake": (t_build + t_bool + t_bake) if t_bake is not None else None,
        "volume": vol,
        "tris": tris,
        "bake": bake_info,
    }


# ===========================================================================
# WORKLOAD 2 -- deep CSG tree, alternating difference / union
# ===========================================================================
# depth d: start from a base cube, then at each level alternately subtract a
# sphere and add a smaller cube, all jittered so cuts are non-trivial.


def _csg_step_specs(depth: int):
    """Deterministic list of (op, kind, params) for a depth-`depth` tree."""
    specs = []
    for i in range(depth):
        ang = i * 0.7
        off = (
            3.0 * math.cos(ang),
            3.0 * math.sin(ang),
            0.6 * (i - depth / 2),
        )
        if i % 2 == 0:
            specs.append(("sub", "sphere", (2.4, off)))
        else:
            specs.append(("add", "cube", (3.0, off)))
    return specs


def occ_csg_tree(depth: int):
    from build123d import Box, Pos, Sphere

    _warmup_occ()
    specs = _csg_step_specs(depth)
    t0 = time.perf_counter()
    acc = Box(12, 12, 12)
    for op, kind, (size, off) in specs:
        if kind == "sphere":
            tool = Pos(*off) * Sphere(size)
        else:
            tool = Pos(*off) * Box(size, size, size)
        acc = acc - tool if op == "sub" else acc + tool
    t_bool = time.perf_counter() - t0

    return {
        "n": depth,
        "wall": t_bool,
        "volume": acc.volume,
        "valid": bool(acc.is_valid),
        "faces": len(acc.faces()),
    }


def manifold_csg_tree(depth: int):
    import manifold3d as mf

    _warmup_bake()
    specs = _csg_step_specs(depth)
    t0 = time.perf_counter()
    acc = mf.Manifold.cube([12, 12, 12], True)
    for op, kind, (size, off) in specs:
        if kind == "sphere":
            tool = mf.Manifold.sphere(size, 32).translate(list(off))
        else:
            tool = mf.Manifold.cube([size, size, size], True).translate(list(off))
        acc = acc - tool if op == "sub" else acc + tool
    vol = acc.volume()
    tris = acc.num_tri()
    t_bool = time.perf_counter() - t0

    t_bake, bake_info = _maybe_bake(acc)

    return {
        "n": depth,
        "wall": t_bool,
        "wall_with_bake": (t_bool + t_bake) if t_bake is not None else None,
        "t_bake": t_bake,
        "volume": vol,
        "tris": tris,
        "bake": bake_info,
    }


# ===========================================================================
# WORKLOAD 3 -- coincident / degenerate cases (OCC fragility probe)
# ===========================================================================
# Each case is a (name, builder) where the boolean involves exactly coplanar
# or exactly touching faces -- OCC's classic weak spot.


def _degenerate_cases_occ():
    from build123d import Box, Cylinder, Pos

    cases = {}

    # (a) two boxes sharing an exact face -> union should be one 2x1x1 block
    cases["coplanar_touch_union"] = lambda: (
        Box(1, 1, 1) + Pos(1, 0, 0) * Box(1, 1, 1)
    )

    # (b) cut a box with a tool whose top face is exactly flush with the box top
    cases["flush_face_cut"] = lambda: (
        Box(4, 4, 4) - Pos(0, 0, 1) * Box(2, 2, 2)
    )

    # (c) subtract a box exactly equal to the target -> empty result
    cases["self_subtract_empty"] = lambda: (Box(2, 2, 2) - Box(2, 2, 2))

    # (d) two boxes touching along an edge only (measure-zero contact)
    cases["edge_touch_union"] = lambda: (
        Box(1, 1, 1) + Pos(1, 1, 0) * Box(1, 1, 1)
    )

    # (e) cylinder cut where the cylinder wall is tangent to the box wall
    cases["tangent_wall_cut"] = lambda: (
        Box(4, 4, 4) - Pos(2, 0, 0) * Cylinder(2, 6)
    )

    # (f) intersection of two identical boxes -> the box itself
    cases["identical_intersect"] = lambda: (Box(2, 2, 2) & Box(2, 2, 2))

    # (g) cut leaving a zero-thickness wall (tool face flush with two box walls)
    cases["zero_wall_cut"] = lambda: (
        Box(4, 4, 4) - Pos(1, 0, 0) * Box(2, 4, 4)
    )

    # (h) OpenSCAD-style zero-gap tower: 40 unit cubes stacked face-to-face,
    #     every internal interface is an exact coplanar shared face.
    def stack_tower():
        acc = Box(1, 1, 1)
        for i in range(1, 40):
            acc = acc + Pos(0, 0, i) * Box(1, 1, 1)
        return acc

    cases["coplanar_stack_40"] = stack_tower

    # (i) near-coincident cut: tool is offset from a box face by 1e-7 (below
    #     OCC TOLERANCE 1e-6) -- a sliver that should not exist.
    cases["sub_tolerance_offset_cut"] = lambda: (
        Box(4, 4, 4) - Pos(0, 0, 2 + 1e-7) * Box(2, 2, 2)
    )

    # (j) two cylinders sharing an exact tangent line (point/line contact)
    cases["cylinder_tangent_union"] = lambda: (
        Cylinder(1, 4) + Pos(2, 0, 0) * Cylinder(1, 4)
    )

    return cases


def _degenerate_cases_manifold():
    import manifold3d as mf

    C = mf.Manifold.cube

    def coplanar_touch_union():
        return C([1, 1, 1], True) + C([1, 1, 1], True).translate([1, 0, 0])

    def flush_face_cut():
        return C([4, 4, 4], True) - C([2, 2, 2], True).translate([0, 0, 1])

    def self_subtract_empty():
        return C([2, 2, 2], True) - C([2, 2, 2], True)

    def edge_touch_union():
        return C([1, 1, 1], True) + C([1, 1, 1], True).translate([1, 1, 0])

    def tangent_wall_cut():
        return C([4, 4, 4], True) - mf.Manifold.cylinder(6, 2, 2, 64, True).translate(
            [2, 0, 0]
        )

    def identical_intersect():
        return C([2, 2, 2], True) ^ C([2, 2, 2], True)

    def zero_wall_cut():
        return C([4, 4, 4], True) - C([2, 4, 4], True).translate([1, 0, 0])

    def coplanar_stack_40():
        acc = C([1, 1, 1], True)
        for i in range(1, 40):
            acc = acc + C([1, 1, 1], True).translate([0, 0, i])
        return acc

    def sub_tolerance_offset_cut():
        return C([4, 4, 4], True) - C([2, 2, 2], True).translate([0, 0, 2 + 1e-7])

    def cylinder_tangent_union():
        cyl = mf.Manifold.cylinder(4, 1, 1, 64, True)
        return cyl + cyl.translate([2, 0, 0])

    return {
        "coplanar_touch_union": coplanar_touch_union,
        "flush_face_cut": flush_face_cut,
        "self_subtract_empty": self_subtract_empty,
        "edge_touch_union": edge_touch_union,
        "tangent_wall_cut": tangent_wall_cut,
        "identical_intersect": identical_intersect,
        "coplanar_stack_40": coplanar_stack_40,
        "sub_tolerance_offset_cut": sub_tolerance_offset_cut,
        "cylinder_tangent_union": cylinder_tangent_union,
        "zero_wall_cut": zero_wall_cut,
    }


def occ_degenerate(case_name: str):
    _warmup_occ()
    cases = _degenerate_cases_occ()
    builder = cases[case_name]
    t0 = time.perf_counter()
    res = builder()
    wall = time.perf_counter() - t0
    try:
        vol = res.volume
    except Exception as e:  # noqa: BLE001
        vol = float("nan")
    try:
        valid = bool(res.is_valid)
    except Exception:  # noqa: BLE001
        valid = None
    try:
        nfaces = len(res.faces())
    except Exception:  # noqa: BLE001
        nfaces = None
    return {"case": case_name, "wall": wall, "volume": vol, "valid": valid,
            "faces": nfaces}


def manifold_degenerate(case_name: str):
    # warm up the manifold kernel so sub-millisecond cases are honest
    import manifold3d as _mf

    _ = (_mf.Manifold.cube([1, 1, 1], True) - _mf.Manifold.cube([1, 1, 1])).volume()
    cases = _degenerate_cases_manifold()
    builder = cases[case_name]
    t0 = time.perf_counter()
    res = builder()
    vol = res.volume()
    tris = res.num_tri()
    wall = time.perf_counter() - t0
    return {"case": case_name, "wall": wall, "volume": vol, "tris": tris,
            "empty": res.is_empty()}


# ===========================================================================
# WORKLOAD 4 -- one body minus N drilled holes
# ===========================================================================


def _hole_positions(n: int, plate: float):
    """n hole centres on a grid inside a `plate`-sized square plate."""
    side = max(1, math.ceil(math.sqrt(n)))
    step = plate / (side + 1)
    pts = []
    for i in range(side):
        for j in range(side):
            if len(pts) >= n:
                return pts
            x = -plate / 2 + step * (i + 1)
            y = -plate / 2 + step * (j + 1)
            pts.append((x, y))
    return pts


def occ_drill(n: int):
    from build123d import Box, Cylinder, Pos

    _warmup_occ()
    plate = 100.0
    t0 = time.perf_counter()
    body = Box(plate, plate, 10)
    holes = [Pos(x, y, 0) * Cylinder(2.0, 20) for (x, y) in _hole_positions(n, plate)]
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    res = body
    for h in holes:
        res = res - h
    t_bool = time.perf_counter() - t0

    return {
        "n": n,
        "t_build": t_build,
        "t_bool": t_bool,
        "wall": t_build + t_bool,
        "volume": res.volume,
        "valid": bool(res.is_valid),
        "faces": len(res.faces()),
    }


def manifold_drill(n: int):
    import manifold3d as mf

    _warmup_bake()
    plate = 100.0
    t0 = time.perf_counter()
    body = mf.Manifold.cube([plate, plate, 10], True)
    holes = [
        mf.Manifold.cylinder(20, 2.0, 2.0, 48, True).translate([x, y, 0])
        for (x, y) in _hole_positions(n, plate)
    ]
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    res = mf.Manifold.batch_boolean([body] + holes, mf.OpType.Subtract)
    vol = res.volume()
    tris = res.num_tri()
    t_bool = time.perf_counter() - t0

    t_bake, bake_info = _maybe_bake(res)

    return {
        "n": n,
        "t_build": t_build,
        "t_bool": t_bool,
        "t_bake": t_bake,
        "wall": t_build + t_bool,
        "wall_with_bake": (t_build + t_bool + t_bake) if t_bake is not None else None,
        "volume": vol,
        "tris": tris,
        "bake": bake_info,
    }
