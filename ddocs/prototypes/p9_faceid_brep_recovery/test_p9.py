"""
test_p9.py -- pytest suite for the faceID-seeded B-rep recovery prototype.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -m pytest test_p9.py -v
"""
from __future__ import annotations

import os

import numpy as np
import manifold3d as m3d
import pytest

from build123d import Box, Cylinder, Pos, Axis, GeomType, import_step, export_step

from faceid_bridge import SideMap, seed_manifold, read_result, _build_manifold64
from recover import (recover_brep, recover_planar_face, feature_edges,
                     _connected_components, _boundary_loops)
from refacer_curved import faceted_patch

from OCP.BRepCheck import BRepCheck_Analyzer


# ---------------------------------------------------------------------------
# the manifold3d face_id channel itself
# ---------------------------------------------------------------------------

def test_mesh_face_id_kwarg_works():
    """manifold3d.Mesh DOES accept a face_id kwarg (flat ndarray)."""
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                  [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=np.float32)
    t = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5],
                  [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6],
                  [3, 0, 4], [3, 4, 7]], dtype=np.uint32)
    fid = np.array([0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5], dtype=np.uint32)
    mesh = m3d.Mesh(v, t, face_id=fid)
    man = m3d.Manifold(mesh)
    assert man.status() == m3d.Error.NoError
    assert abs(man.volume() - 1.0) < 1e-9


def test_face_id_rank_bug_is_the_real_failure():
    """The 'incompatible function arguments' error is a RANK bug: face_id
    must be flat (N,), not (N,1).  Dtype is auto-cast by nanobind."""
    v = np.zeros((3, 3), dtype=np.float32)
    t = np.array([[0, 1, 2]], dtype=np.uint32)
    # (N,1) column vector -> rejected
    with pytest.raises(TypeError):
        m3d.Mesh(v, t, face_id=np.array([[7]], dtype=np.uint32))
    # flat (N,) with a "wrong" dtype -> auto-cast, accepted
    m3d.Mesh(v, t, face_id=np.array([7], dtype=np.int64))


# ---------------------------------------------------------------------------
# seeding & survival
# ---------------------------------------------------------------------------

def test_seed_box_has_six_face_ids():
    sm = SideMap()
    man, info = seed_manifold(Box(10, 10, 10), "box", sm)
    assert info["status"] == "Error.NoError"
    rm = read_result(man)
    assert rm.distinct_ids == list(range(6))


def test_seeded_id_survives_difference():
    sm = SideMap()
    plate, _ = seed_manifold(Box(20, 20, 10), "plate", sm)
    notch, _ = seed_manifold(Box(6, 6, 20), "notch", sm)
    rm = read_result(plate - notch)
    # 12 input faces; the cut consumes some -> count tracks input faces, not tris
    assert len(rm.distinct_ids) <= 12
    assert len(rm.distinct_ids) >= 6
    assert len(rm.tris) >= len(rm.distinct_ids)


def test_group_count_tracks_input_faces_not_triangles():
    """The OCCT doc sec 4 result: distinct-id count ~ input face count."""
    sm = SideMap()
    sph, _ = seed_manifold(Cylinder(8, 16), "core", sm, tol=0.05)
    rm = read_result(sph)
    # a 16-radius cylinder is hundreds of tris but only 3 input faces
    assert len(rm.tris) > 50
    assert len(rm.distinct_ids) == 3


def test_unseeded_face_id_shatters():
    """p8's finding: WITHOUT a seed, manifold's coplanar face_id shatters
    a curved surface into many ids."""
    cyl = Cylinder(5, 20)
    vs, ts = cyl.tessellate(0.05, 0.2)
    vp = np.array([[v.X, v.Y, v.Z] for v in vs], dtype=np.float64)
    tv = np.array(ts, dtype=np.int64)
    from faceid_bridge import _weld
    wv, wt, _ = _weld(vp, tv)
    a, b, c = wt[:, 0], wt[:, 1], wt[:, 2]
    wt = wt[(a != b) & (b != c) & (a != c)]
    mesh = m3d.Mesh64(vert_properties=np.ascontiguousarray(wv),
                      tri_verts=np.ascontiguousarray(wt.astype(np.uint64)))
    man = m3d.Manifold(mesh)
    out = man.to_mesh()
    n_ids = len(set(np.asarray(out.face_id).tolist()))
    assert n_ids > 10, "unseeded face_id should shatter the cylinder"


# ---------------------------------------------------------------------------
# planar recovery
# ---------------------------------------------------------------------------

def test_recover_planar_notch_exact_volume():
    sm = SideMap()
    plate, _ = seed_manifold(Box(20, 20, 10), "plate", sm)
    notch, _ = seed_manifold(Box(6, 6, 20), "notch", sm)
    rm = read_result(plate - notch)
    res = recover_brep(rm, sm)
    assert res.is_valid
    # exact: 20*20*10 - 6*6*10 = 3640, NO faceting error
    assert abs(res.volume - 3640.0) < 1e-6


def test_recovered_faces_are_analytic_planes():
    sm = SideMap()
    plate, _ = seed_manifold(Box(40, 30, 12), "plate", sm)
    pocket, _ = seed_manifold(Pos(0, 0, 3) * Box(14, 10, 12), "pocket", sm)
    rm = read_result(plate - pocket)
    res = recover_brep(rm, sm)
    for f in res.solid.faces():
        assert f.geom_type == GeomType.PLANE


def test_recovered_face_count_matches_native_brep():
    """The recovered solid has the SAME face/edge count as a native boolean."""
    plate, pocket = Box(40, 30, 12), Pos(0, 0, 3) * Box(14, 10, 12)
    native = plate - pocket
    sm = SideMap()
    pm, _ = seed_manifold(plate, "plate", sm)
    km, _ = seed_manifold(pocket, "pocket", sm)
    res = recover_brep(read_result(pm - km), sm)
    assert len(res.solid.faces()) == len(native.faces())
    assert len(res.solid.edges()) == len(native.edges())


def test_feature_edges_are_faceid_boundaries():
    sm = SideMap()
    plate, _ = seed_manifold(Box(20, 20, 10), "plate", sm)
    notch, _ = seed_manifold(Box(6, 6, 20), "notch", sm)
    rm = read_result(plate - notch)
    feats = feature_edges(rm)
    assert len(feats) > 0
    for a, b, f0, f1 in feats:
        assert f0 != f1


# ---------------------------------------------------------------------------
# the payoff -- real fillet / chamfer
# ---------------------------------------------------------------------------

def _recover_pocket():
    sm = SideMap()
    pm, _ = seed_manifold(Box(40, 30, 12), "plate", sm)
    km, _ = seed_manifold(Pos(0, 0, 3) * Box(14, 10, 12), "pocket", sm)
    return recover_brep(read_result(pm - km), sm).solid


def test_real_fillet_on_recovered_edge():
    solid = _recover_pocket()
    edge = [e for e in solid.edges().filter_by(Axis.Z)
            if abs(e.length - 9.0) < 1e-3][0]
    filleted = solid.fillet(2.0, [edge])
    assert BRepCheck_Analyzer(filleted.wrapped).IsValid()
    # a real fillet makes an analytic cylinder blend face
    assert any(f.geom_type == GeomType.CYLINDER for f in filleted.faces())


def test_real_chamfer_on_recovered_edge():
    solid = _recover_pocket()
    edge = [e for e in solid.edges().filter_by(Axis.Z)
            if abs(e.length - 9.0) < 1e-3][0]
    chamfered = solid.chamfer(1.5, None, [edge])
    assert BRepCheck_Analyzer(chamfered.wrapped).IsValid()


def test_fillet_all_four_pocket_edges():
    solid = _recover_pocket()
    edges = [e for e in solid.edges().filter_by(Axis.Z)
             if abs(e.length - 9.0) < 1e-3]
    assert len(edges) == 4
    filleted = solid.fillet(2.0, edges)
    assert BRepCheck_Analyzer(filleted.wrapped).IsValid()
    n_cyl = sum(1 for f in filleted.faces()
                if f.geom_type == GeomType.CYLINDER)
    assert n_cyl == 4


def test_step_export_roundtrip(tmp_path):
    solid = _recover_pocket()
    edge = [e for e in solid.edges().filter_by(Axis.Z)
            if abs(e.length - 9.0) < 1e-3][0]
    filleted = solid.fillet(2.0, [edge])
    p = str(tmp_path / "p9.step")
    export_step(filleted, p)
    assert os.path.getsize(p) > 0
    re = import_step(p)
    assert BRepCheck_Analyzer(re.wrapped).IsValid()


# ---------------------------------------------------------------------------
# split-face (p8's concern)
# ---------------------------------------------------------------------------

def test_split_face_becomes_two_faces():
    """A face cut clean into two pieces keeps ONE seeded id but recovers as
    two separate connected components -> two TopoDS_Faces."""
    sm = SideMap()
    bar, _ = seed_manifold(Box(24, 8, 4), "bar", sm)
    cut, _ = seed_manifold(Box(4, 20, 20), "cutter", sm)
    rm = read_result(bar - cut)
    split_found = False
    for fid in rm.distinct_ids:
        tris = rm.tris[rm.tris_of(fid)]
        if len(_connected_components(tris)) > 1:
            split_found = True
            rf = recover_planar_face(rm, fid, sm[fid])
            assert isinstance(rf.face, list)
            assert len(rf.face) == 2
    assert split_found, "expected at least one split seeded id"


def test_split_csg_recovers_two_solids():
    sm = SideMap()
    bar, _ = seed_manifold(Box(24, 8, 4), "bar", sm)
    cut, _ = seed_manifold(Box(4, 20, 20), "cutter", sm)
    res = recover_brep(read_result(bar - cut), sm)
    # bar 24*8*4=768 - slice 4*8*4=128 -> 640
    assert abs(res.volume - 640.0) < 1e-6


# ---------------------------------------------------------------------------
# curved (Tier C boundary)
# ---------------------------------------------------------------------------

def test_curved_bore_is_identified_but_faceted():
    sm = SideMap()
    plate, _ = seed_manifold(Box(30, 30, 12), "plate", sm)
    bore, _ = seed_manifold(Cylinder(5, 40), "bore", sm)
    rm = read_result(plate - bore)
    cyl_ids = [fid for fid, r in sm.records.items()
               if r.surface_kind == "CYLINDER"]
    assert cyl_ids, "the bore must be seeded as a CYLINDER record"
    # faceID identifies all bore triangles as one group
    bore_id = cyl_ids[0]
    assert len(rm.tris_of(bore_id)) > 50
    # but recovery keeps it faceted -- no analytic cylinder face
    res = recover_brep(rm, sm)
    n_cyl = sum(1 for f in res.solid.faces()
                if f.geom_type == GeomType.CYLINDER)
    assert n_cyl == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
