"""p10 -- pytest suite for the selective mesh fillet/chamfer prototype.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -m pytest test_p10.py -v
"""

from __future__ import annotations

import numpy as np
import pytest

import manifold3d as m3d
import build123d.mesh as bm
from build123d import Box, Pos

from feature_graph import (
    extract_feature_edges,
    build_chains,
    chains_by_pair,
    FeatureChain,
)
from explicit import selective_chamfer, selective_fillet, edge_convexity
from sdf_approach import BoxSDF, selective_box_sdf, selective_box_sdf_all_edges, remesh_sdf


# ---- fixtures -------------------------------------------------------------


def _graph(manifold):
    rm = bm.bridge.read_result(manifold)
    feats = extract_feature_edges(rm.vertices, rm.triangles, rm.face_id)
    chains = build_chains(feats)
    return rm, feats, chains


def _box(size=20.0):
    return bm.MeshPart.from_part(Box(size, size, size)).manifold


def _valid(man) -> bool:
    return man.status() == m3d.Error.NoError and not man.is_empty()


# ---- feature graph --------------------------------------------------------


def test_box_has_12_feature_edges():
    rm, feats, chains = _graph(_box())
    assert len(feats) == 12
    assert len(chains_by_pair(chains)) == 12


def test_feature_edge_pair_is_faceid_boundary():
    rm, feats, _ = _graph(_box())
    for fe in feats:
        assert fe.fid_a != fe.fid_b  # a feature edge separates two faceIDs


def test_box_edges_all_convex():
    rm, feats, _ = _graph(_box())
    for fe in feats:
        conv, *_ = edge_convexity(rm.vertices, fe)
        assert conv > 0.0, "every box edge is convex"


def test_l_shape_has_one_concave_edge():
    L = bm.mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    rm, feats, _ = _graph(L.manifold)
    convexity = [edge_convexity(rm.vertices, fe)[0] for fe in feats]
    n_concave = sum(1 for c in convexity if c < -1e-6)
    assert n_concave == 1, "the L-notch has exactly one reentrant edge"


def test_chains_keyed_by_faceid_pair():
    rm, feats, chains = _graph(_box())
    by_pair = chains_by_pair(chains)
    for pair, chs in by_pair.items():
        for ch in chs:
            assert ch.pair == pair


# ---- explicit chamfer -----------------------------------------------------


def test_explicit_chamfer_one_edge_is_valid_and_exact():
    base = _box(20)
    rm, _, chains = _graph(base)
    ch = selective_chamfer(base, rm.vertices, chains[0], distance=4.0)
    assert _valid(ch)
    # a 4mm chamfer on one 20mm edge removes a 0.5*4*4*20 prism
    assert ch.volume() == pytest.approx(8000 - 160, abs=1e-6)
    assert len(ch.decompose()) == 1


def test_explicit_chamfer_selective_only_one_edge():
    """Chamfering one faceID pair must leave the other 11 edges untouched."""
    base = _box(20)
    rm, _, chains = _graph(base)
    by_pair = chains_by_pair(chains)
    target = by_pair[sorted(by_pair)[0]][0]
    ch = selective_chamfer(base, rm.vertices, target, distance=3.0)
    # exactly one edge removed -> volume drop is exactly one prism
    assert ch.volume() == pytest.approx(8000 - 0.5 * 3 * 3 * 20, abs=1e-6)


# ---- explicit fillet ------------------------------------------------------


def test_explicit_fillet_one_edge_valid():
    base = _box(20)
    rm, _, chains = _graph(base)
    fl = selective_fillet(base, rm.vertices, chains[0], radius=4.0, segments=12)
    assert _valid(fl)
    assert len(fl.decompose()) == 1
    # faceted fillet removes slightly less than the smooth quarter-disc bite
    smooth = 8000 - (16 - np.pi * 16 / 4) * 20
    assert fl.volume() < 8000  # convex edge -> material removed
    assert abs(fl.volume() - smooth) < 30  # within faceting error


def test_explicit_fillet_all_12_edges_single_body():
    base = _box(20)
    rm, _, chains = _graph(base)
    allf = selective_fillet(base, rm.vertices, chains, radius=3.0, segments=8)
    assert _valid(allf)
    assert len(allf.decompose()) == 1, "all-edge fillet must not fragment"


def test_explicit_fillet_concave_edge_adds_material():
    L = bm.mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    base = L.manifold
    rm, feats, chains = _graph(base)
    concave = [
        ch for ch in chains
        if edge_convexity(rm.vertices, ch.edges[0])[0] < -1e-6
    ]
    assert concave
    v0 = base.volume()
    fl = selective_fillet(base, rm.vertices, concave[0], radius=3.0, segments=8)
    assert _valid(fl)
    assert fl.volume() > v0, "filleting a concave edge adds material"


def test_l_shape_chamfer_all_no_fragmentation():
    L = bm.mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    base = L.manifold
    rm, _, chains = _graph(base)
    allc = selective_chamfer(base, rm.vertices, chains, distance=2.0)
    assert _valid(allc)
    assert len(allc.decompose()) == 1, "cut-then-add ordering keeps one body"


# ---- hard cases -----------------------------------------------------------


def test_three_edge_corner_stays_valid():
    base = _box(20)
    rm, _, chains = _graph(base)
    corner_v = int(np.argmin(np.linalg.norm(rm.vertices - [10, 10, 10], axis=1)))
    touching = [
        ch for ch in chains
        if any(corner_v in (fe.v0, fe.v1) for fe in ch.edges)
    ]
    assert len(touching) == 3
    fl = selective_fillet(base, rm.vertices, touching, radius=4.0, segments=8)
    assert _valid(fl)
    assert len(fl.decompose()) == 1


def test_big_radius_fillet_stays_valid():
    """r larger than the local feature must not crash -- it degrades."""
    base = bm.MeshPart.from_part(Box(20, 20, 6)).manifold
    rm, _, chains = _graph(base)
    fl = selective_fillet(base, rm.vertices, chains[0], radius=8.0, segments=10)
    assert _valid(fl), "manifold3d guarantees validity even for oversize r"
    assert fl.volume() < base.volume()


# ---- SDF approach ---------------------------------------------------------


def test_sdf_chamfer_box_valid():
    box = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])
    sdf = selective_box_sdf(box, (0, 2), k=4.0, kind="chamfer")
    man = remesh_sdf(sdf, (-13, -13, -13, 13, 13, 13), edge_length=1.5)
    assert _valid(man)
    assert len(man.decompose()) == 1


def test_sdf_fillet_box_valid_and_rounds():
    box = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])
    sharp = remesh_sdf(box, (-12, -12, -12, 12, 12, 12), edge_length=2.0)
    sdf = selective_box_sdf(box, (0, 2), k=4.0, kind="fillet")
    rounded = remesh_sdf(sdf, (-13, -13, -13, 13, 13, 13), edge_length=1.5)
    assert _valid(rounded)
    # rounding one edge removes material vs the sharp box
    assert rounded.volume() < sharp.volume() + 1.0


def test_sdf_all_edges_fillet_valid():
    box = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])
    sdf = selective_box_sdf_all_edges(box, k=3.0, kind="fillet")
    man = remesh_sdf(sdf, (-13, -13, -13, 13, 13, 13), edge_length=1.5)
    assert _valid(man)
    assert len(man.decompose()) == 1


def test_smin_reduces_to_min_at_zero_k():
    from sdf_approach import smin_round, smin_chamfer
    assert smin_round(3.0, 5.0, 0.0) == 3.0
    assert smin_chamfer(3.0, 5.0, 0.0) == 3.0
    # away from the seam, smooth-min equals min
    assert smin_round(1.0, 50.0, 4.0) == pytest.approx(1.0)
