"""p10 -- the full test matrix for selective mesh fillet/chamfer.

Runs both approaches over every test case in the brief and exports STLs:

  box          -- chamfer 1 edge; fillet 1 edge; fillet all 12 edges
  L-shape      -- convex AND concave edge in one part
  corner       -- 3 feature edges meeting at one corner (the hard case)
  big radius   -- fillet radius >= the local feature (the hard case)

For each: manifold status, volume sanity, triangle quality, STL export.

Run with the venv python:
    /Users/ochafik/github/.ddocs-venv/bin/python run_tests.py
"""

from __future__ import annotations

import os

import numpy as np
import build123d.mesh as bm
from build123d import Box, Pos

from feature_graph import (
    extract_feature_edges,
    build_chains,
    chains_by_pair,
    triangle_normals,
)
from explicit import selective_chamfer, selective_fillet, edge_convexity
from sdf_approach import (
    BoxSDF,
    selective_box_sdf,
    selective_box_sdf_all_edges,
    remesh_sdf,
)
from measure import report

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)


def _graph(manifold):
    """Return (ResultMesh, chains, chains_by_pair) for a manifold."""
    rm = bm.bridge.read_result(manifold)
    feats = extract_feature_edges(rm.vertices, rm.triangles, rm.face_id)
    chains = build_chains(feats)
    return rm, chains, chains_by_pair(chains)


def _stl(manifold, name: str) -> None:
    """Export a Manifold to out/<name>.stl as binary STL."""
    try:
        mp = bm.MeshPart(manifold)
    except Exception:
        return
    path = os.path.join(OUT, f"{name}.stl")
    mp.export_stl(path)
    print(f"      wrote {os.path.relpath(path)}")


def _pick_chain(by_pair, want_convex=None, vertices=None):
    """Pick a feature chain; optionally require a given convexity."""
    for pair in sorted(by_pair):
        for ch in by_pair[pair]:
            if want_convex is None:
                return ch
            c, *_ = edge_convexity(vertices, ch.edges[0])
            if (c > 0) == want_convex and abs(c) > 1e-6:
                return ch
    return None


# ---------------------------------------------------------------------------


def case_box():
    print("\n" + "=" * 78)
    print("CASE 1 -- BOX  (chamfer 1 edge; fillet 1 edge; fillet all 12)")
    print("=" * 78)
    base = bm.MeshPart.from_part(Box(20, 20, 20)).manifold
    report("box (input)", base, expected_vol=8000.0)
    rm, chains, by_pair = _graph(base)
    print(f"  feature graph: {sum(len(c.edges) for c in chains)} edges, "
          f"{len(by_pair)} faceID pairs (a box has 12 edges)")

    # ---- explicit ----
    one = _pick_chain(by_pair)
    ch = selective_chamfer(base, rm.vertices, one, distance=4.0)
    report("explicit chamfer 1 edge (d=4)", ch)
    _stl(ch, "box_chamfer1_explicit")

    fl = selective_fillet(base, rm.vertices, one, radius=4.0, segments=12)
    report("explicit fillet 1 edge (r=4)", fl)
    _stl(fl, "box_fillet1_explicit")

    # fillet all 12 edges -- one batch boolean over every chain at once
    allf = selective_fillet(base, rm.vertices, chains, radius=3.0, segments=8)
    report("explicit fillet ALL 12 edges (r=3)", allf)
    _stl(allf, "box_filletall_explicit")

    # ---- SDF ----
    box_sdf = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])
    b = (-13, -13, -13, 13, 13, 13)
    sdf1 = selective_box_sdf(box_sdf, (0, 2), k=4.0, kind="chamfer")
    man = remesh_sdf(sdf1, b, edge_length=1.0)
    report("SDF chamfer 1 edge (k=4)", man)
    _stl(man, "box_chamfer1_sdf")

    sdf2 = selective_box_sdf(box_sdf, (0, 2), k=4.0, kind="fillet")
    man = remesh_sdf(sdf2, b, edge_length=1.0)
    report("SDF fillet 1 edge (k=4)", man)
    _stl(man, "box_fillet1_sdf")

    sdf3 = selective_box_sdf_all_edges(box_sdf, k=3.0, kind="fillet")
    man = remesh_sdf(sdf3, b, edge_length=1.0)
    report("SDF fillet ALL edges (k=3)", man)
    _stl(man, "box_filletall_sdf")


def case_l_shape():
    print("\n" + "=" * 78)
    print("CASE 2 -- L-SHAPE  (convex AND concave edges in one part)")
    print("=" * 78)
    big = Box(30, 30, 12)
    notch = Pos(10, 10, 0) * Box(16, 16, 16)
    L = bm.mesh_cut(big, notch)
    base = L.manifold
    report("L-shape (input)", base)
    rm, chains, by_pair = _graph(base)

    nconv = nconc = 0
    for ch in chains:
        for fe in ch.edges:
            c, *_ = edge_convexity(rm.vertices, fe)
            if c > 1e-6:
                nconv += 1
            elif c < -1e-6:
                nconc += 1
    print(f"  feature graph: {nconv} convex + {nconc} concave feature edges")

    # explicit: fillet a convex edge, then a concave edge
    conv = _pick_chain(by_pair, want_convex=True, vertices=rm.vertices)
    conc = _pick_chain(by_pair, want_convex=False, vertices=rm.vertices)
    if conv is not None:
        fl = selective_fillet(base, rm.vertices, conv, radius=3.0, segments=8)
        report("explicit fillet a CONVEX edge", fl)
        _stl(fl, "L_fillet_convex_explicit")
    if conc is not None:
        rm2, _, _ = _graph(base)
        fl = selective_fillet(base, rm2.vertices, conc, radius=3.0, segments=8)
        report("explicit fillet a CONCAVE edge", fl)
        _stl(fl, "L_fillet_concave_explicit")

    # explicit: chamfer ALL feature edges of the L (convex carved, concave filled)
    allc = selective_chamfer(base, rm.vertices, chains, distance=2.0)
    report("explicit chamfer ALL L edges (d=2)", allc)
    _stl(allc, "L_chamferall_explicit")


def case_corner():
    print("\n" + "=" * 78)
    print("CASE 3 -- 3 EDGES MEETING AT A CORNER  (the hard case)")
    print("=" * 78)
    base = bm.MeshPart.from_part(Box(20, 20, 20)).manifold
    rm, chains, by_pair = _graph(base)

    # the corner at (+x,+y,+z): find the 3 chains whose edges all touch that
    # corner vertex -- selectivity via faceID, the 3 chains share that vertex.
    corner_pt = np.array([10.0, 10.0, 10.0])
    dists = np.linalg.norm(rm.vertices - corner_pt, axis=1)
    corner_v = int(np.argmin(dists))
    touching = [
        ch
        for ch in chains
        if any(corner_v in (fe.v0, fe.v1) for fe in ch.edges)
    ]
    print(f"  corner vertex {corner_v} at {rm.vertices[corner_v]}; "
          f"{len(touching)} feature chains meet there")

    # explicit: fillet all 3 corner chains together (one batch boolean)
    cur = selective_fillet(base, rm.vertices, touching, radius=4.0, segments=8)
    report("explicit fillet 3-edge corner (r=4)", cur)
    _stl(cur, "corner_fillet_explicit")

    cur = selective_chamfer(base, rm.vertices, touching, distance=4.0)
    report("explicit chamfer 3-edge corner (d=4)", cur)
    _stl(cur, "corner_chamfer_explicit")

    # SDF: round the 3 edges of one corner -- blend faces (0,2),(0,4),(2,4)
    box_sdf = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])

    def corner_sdf(x, y, z):
        from sdf_approach import smin_round
        p = np.array([x, y, z])
        k = 4.0
        d0 = box_sdf.face_sdf(p, 0)  # +x
        d2 = box_sdf.face_sdf(p, 2)  # +y
        d4 = box_sdf.face_sdf(p, 4)  # +z
        # smooth-min the three corner faces together
        d_corner = smin_round(smin_round(d0, d2, k), d4, k)
        d_corner = smin_round(d_corner, smin_round(d2, d4, k), k)
        rest = min(box_sdf.face_sdf(p, i) for i in (1, 3, 5))
        return min(d_corner, rest)

    man = remesh_sdf(corner_sdf, (-13, -13, -13, 13, 13, 13), edge_length=0.8)
    report("SDF fillet 3-edge corner", man)
    _stl(man, "corner_fillet_sdf")


def case_big_radius():
    print("\n" + "=" * 78)
    print("CASE 4 -- FILLET RADIUS LARGER THAN LOCAL FEATURE  (the hard case)")
    print("=" * 78)
    # a thin 20 x 20 x 6 plate; ask for r=8 (> half-thickness 3) on a top edge.
    base = bm.MeshPart.from_part(Box(20, 20, 6)).manifold
    report("thin plate (input, 6mm thick)", base, expected_vol=2400.0)
    rm, chains, by_pair = _graph(base)

    # find a vertical-ish... pick a top edge (a chain between top face and a side)
    one = _pick_chain(by_pair)
    print("  requesting r=8 on a part whose local thickness is 6mm")
    fl = selective_fillet(base, rm.vertices, one, radius=8.0, segments=10)
    report("explicit fillet r=8 (> 6mm feature)", fl)
    _stl(fl, "bigR_fillet_explicit")

    # SDF version -- a strong smooth-min on a thin box
    box_sdf = BoxSDF(center=[0, 0, 0], half=[10, 10, 3])
    sdf = selective_box_sdf(box_sdf, (0, 4), k=8.0, kind="fillet")
    man = remesh_sdf(sdf, (-13, -13, -13, 13, 13, 13), edge_length=0.7)
    report("SDF fillet k=8 (> 6mm feature)", man)
    _stl(man, "bigR_fillet_sdf")

    # SDF all edges with big k on the thin plate -- the pathological case
    sdf_all = selective_box_sdf_all_edges(box_sdf, k=8.0, kind="fillet")
    man = remesh_sdf(sdf_all, (-14, -14, -14, 14, 14, 14), edge_length=0.7)
    report("SDF fillet ALL edges k=8 (pathological)", man)
    _stl(man, "bigR_filletall_sdf")


def case_curved_loop():
    print("\n" + "=" * 78)
    print("CASE 5 -- CURVED (FACETED) FEATURE-EDGE LOOP  (the quality break)")
    print("=" * 78)
    # a box with a cylindrical bore: the bore wall is faceted, so the rim is a
    # genuine multi-segment feature-edge LOOP, not a single straight edge.
    from build123d import Cylinder

    part = bm.mesh_cut(Box(30, 30, 12), Cylinder(radius=6, height=12))
    base = part.manifold
    report("bored box (input)", base)
    rm, chains, by_pair = _graph(base)
    loops = [c for c in chains if c.is_loop]
    print(f"  feature graph: {len(loops)} closed loops "
          f"(the two bore rims, ~{loops[0].verts.__len__() if loops else 0} "
          f"segments each)")

    longest = max(chains, key=lambda c: len(c.edges))
    fl = selective_fillet(base, rm.vertices, longest, radius=2.0, segments=6)
    report("explicit fillet bore-rim LOOP (r=2)", fl)
    _stl(fl, "loop_fillet_explicit")
    ch = selective_chamfer(base, rm.vertices, longest, distance=1.5)
    report("explicit chamfer bore-rim LOOP (d=1.5)", ch)
    _stl(ch, "loop_chamfer_explicit")


if __name__ == "__main__":
    case_box()
    case_l_shape()
    case_corner()
    case_big_radius()
    case_curved_loop()
    print("\nAll STLs written to:", OUT)
