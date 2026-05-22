"""
test_p8.py -- the p8 findings as executable assertions.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -m pytest test_p8.py -v
"""
import numpy as np
import manifold3d as m
import pytest

from build123d import Box, Cylinder, Pos, Axis, Plane, GeomType, ShapeList, Face
from b3d_manifold import (b3d_to_manifold, tagged_original, weld,
                          triangle_origins, manifold_to_b3d_solid)
from refacer import ReFacer


# -- the bridge ------------------------------------------------------------

def test_unwelded_tessellation_is_nonmanifold():
    """build123d tessellation has duplicate verts -> manifold rejects it."""
    verts, tris = Box(10, 10, 10).tessellate(0.1)
    vp = np.array([[v.X, v.Y, v.Z] for v in verts], dtype=np.float32)
    tv = np.array(tris, dtype=np.uint32)
    bad = m.Manifold(m.Mesh(vp, tv))
    assert bad.status() != m.Error.NoError   # NotManifold


def test_welding_makes_it_manifold():
    verts, tris = Box(10, 10, 10).tessellate(0.1)
    vp = np.array([[v.X, v.Y, v.Z] for v in verts])
    tv = np.array(tris, dtype=np.uint32)
    wvp, wtv = weld(vp, tv)
    assert len(wvp) == 8                      # 24 -> 8 corners
    good = m.Manifold(m.Mesh(wvp, wtv))
    assert good.status() == m.Error.NoError
    assert abs(good.volume() - 1000.0) < 1e-3


def test_roundtrip_volume_preserved():
    man, _ = tagged_original(Box(10, 10, 10))
    assert abs(man.volume() - 1000.0) < 1e-3


# -- the loss --------------------------------------------------------------

def test_box_loses_face_count_through_mesh():
    """6 named faces -> 12 triangle-faces after a naive round trip."""
    box = Box(10, 10, 10)
    assert len(box.faces()) == 6
    man, _ = tagged_original(box)
    sewn = manifold_to_b3d_solid(man.to_mesh())
    assert len(sewn.faces()) == 12            # the loss
    assert sewn.is_valid


def test_cylinder_geomtype_is_destroyed():
    """A BREP boolean keeps an analytic CYLINDER face; the mesh path does not."""
    brep = Box(20, 20, 10) - Cylinder(4, 30)
    assert len(brep.faces().filter_by(GeomType.CYLINDER)) == 1
    A, _ = tagged_original(Box(20, 20, 10))
    B, _ = tagged_original(Cylinder(4, 30))
    sewn = manifold_to_b3d_solid((A - B).to_mesh())
    assert len(sewn.faces().filter_by(GeomType.CYLINDER)) == 0   # LOST


# -- tagging through booleans ----------------------------------------------

def test_run_original_id_survives_difference():
    A, idA = tagged_original(Box(20, 20, 10))
    B, idB = tagged_original(Box(8, 8, 30))
    o = triangle_origins((A - B).to_mesh())
    assert set(o.tolist()) == {idA, idB}


def test_cut_faces_inherit_tool_id():
    """Boolean-created hole walls carry the TOOL solid's id (not orphaned)."""
    A, idA = tagged_original(Box(20, 20, 10))
    B, idB = tagged_original(Box(8, 8, 30))
    mesh = (A - B).to_mesh()
    o = triangle_origins(mesh)
    vp = np.asarray(mesh.vert_properties)[:, :3]
    tv = np.asarray(mesh.tri_verts)
    cen = (vp[tv[:, 0]] + vp[tv[:, 1]] + vp[tv[:, 2]]) / 3
    on_wall = np.any(np.isclose(np.abs(cen[:, :2]), 4.0, atol=1e-3), axis=1)
    assert on_wall.sum() > 0
    assert (o[on_wall] == idB).all()


def test_identity_survives_union_and_intersection():
    for op in (lambda a, b: a + b, lambda a, b: a ^ b):
        A, idA = tagged_original(Box(10, 10, 10))
        B, idB = tagged_original(Sphere_(6.5))
        o = triangle_origins(op(A, B).to_mesh())
        assert set(o.tolist()) == {idA, idB}


def Sphere_(r):
    from build123d import Sphere
    return Sphere(r)


def test_identity_survives_chained_boolean():
    A, idA = tagged_original(Box(20, 20, 20))
    B, idB = tagged_original(Box(8, 8, 30))
    C, idC = tagged_original(Pos(7, 0, 0) * Cylinder(3, 40))
    o = triangle_origins(((A - B) - C).to_mesh())
    assert set(o.tolist()) == {idA, idB, idC}


def test_per_face_tag_recovery():
    """A reserved-id run per box face survives a boolean and is recoverable."""
    box = Box(12, 12, 12)
    verts, tris = box.tessellate(0.1)
    vp = np.array([[v.X, v.Y, v.Z] for v in verts])
    tv = np.array(tris)
    wvp, wtv = weld(vp, tv)
    cen = (wvp[wtv[:, 0]] + wvp[wtv[:, 1]] + wvp[wtv[:, 2]]) / 3
    base = m.Manifold.reserve_ids(6)
    face_of = np.full(len(wtv), -1, dtype=np.int64)
    half, tol = 6.0, 1e-3
    for fi, mask in [(0, cen[:, 0] < -half + tol), (1, cen[:, 0] > half - tol),
                     (2, cen[:, 1] < -half + tol), (3, cen[:, 1] > half - tol),
                     (4, cen[:, 2] < -half + tol), (5, cen[:, 2] > half - tol)]:
        face_of[mask] = fi
    order = np.argsort(face_of, kind="stable")
    starts = [0]
    oids = []
    for fi in range(6):
        starts.append(starts[-1] + int((face_of[order] == fi).sum()))
        oids.append(base + fi)
    mesh = m.Mesh(wvp.astype(np.float32), wtv[order].astype(np.uint32),
                  run_index=np.array([s * 3 for s in starts], dtype=np.uint32),
                  run_original_id=np.array(oids, dtype=np.uint32))
    box_man = m.Manifold(mesh)
    assert box_man.status() == m.Error.NoError
    cyl, _ = tagged_original(Cylinder(3, 30))
    o = triangle_origins((box_man - cyl).to_mesh())
    # +Z face id = base+5: all its triangles must be on the z=+6 plane
    plus_z = base + 5
    sel = np.where(o == plus_z)[0]
    assert len(sel) > 0
    rmesh = (box_man - cyl).to_mesh()
    vp2 = np.asarray(rmesh.vert_properties)[:, :3]
    tv2 = np.asarray(rmesh.tri_verts)
    cen2 = (vp2[tv2[sel][:, 0]] + vp2[tv2[sel][:, 1]] + vp2[tv2[sel][:, 2]]) / 3
    assert np.allclose(cen2[:, 2], 6.0, atol=1e-3)


# -- the re-facer ----------------------------------------------------------

def test_planar_faces_remerge_into_single_b3d_faces():
    """A plate + square hole: 10 logical faces -> 10 reconstructed Faces."""
    A, idA = tagged_original(Box(20, 20, 10))
    B, idB = tagged_original(Box(6, 6, 30))
    rf = ReFacer((A - B).to_mesh(), tags={idA: "plate", idB: "punch"})
    clusters = rf.cluster(crease_deg=20.0)
    assert len(clusters) == 10
    faces = [rf.to_b3d_face(c) for c in clusters]
    assert all(isinstance(f, Face) for f in faces)        # all merged to one
    assert all(f.geom_type == GeomType.PLANE for f in faces)
    brep = Box(20, 20, 10) - Box(6, 6, 30)
    assert len(brep.faces()) == len(clusters)             # 10 == 10


def test_recovered_faces_work_with_b3d_selectors():
    A, idA = tagged_original(Box(20, 20, 10))
    B, idB = tagged_original(Box(6, 6, 30))
    rf = ReFacer((A - B).to_mesh(), tags={idA: "plate", idB: "punch"})
    faces = ShapeList([rf.to_b3d_face(c) for c in rf.cluster()])
    # build123d's OWN operators must work on the reconstructed faces
    top = faces.sort_by(Axis.Z)[-1]
    assert isinstance(top, Face)
    assert len(faces.filter_by(Plane.XY)) == 2            # top + bottom
    assert len(faces.filter_by(GeomType.PLANE)) == 10


def test_curved_face_does_not_remerge():
    """A cylindrical bore cannot be rebuilt as one analytic Face."""
    A, idA = tagged_original(Box(20, 20, 10))
    B, idB = tagged_original(Cylinder(4, 30))
    rf = ReFacer((A - B).to_mesh(), tags={idA: "plate", idB: "bore"})
    clusters = rf.cluster(crease_deg=20.0)
    curved = [c for c in clusters if c.kind == "curved"]
    assert len(curved) == 1                               # the bore is 1 cluster
    f = rf.to_b3d_face(curved[0])
    assert not isinstance(f, Face)                        # -> many flat facets
    assert len(f) > 1


def test_split_face_becomes_two_clusters():
    """A bar sliced through: the +Z face splits into 2 separate clusters."""
    A, idA = tagged_original(Box(20, 4, 4))
    B, idB = tagged_original(Box(4, 8, 8))
    rf = ReFacer((A - B).to_mesh(), tags={idA: "bar", idB: "saw"})
    clusters = rf.cluster(crease_deg=20.0)
    topz = [c for c in clusters if c.kind == "planar" and c.normal[2] > 0.99]
    assert len(topz) == 2


# -- BREP contrast ---------------------------------------------------------

def test_brep_keeps_face_identity_via_issame():
    """An untouched face is the literal same TopoDS_Face after a boolean."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.TopTools import TopTools_ListOfShape
    from build123d import Solid
    box = Box(20, 20, 10)
    bottom = box.faces().sort_by(Axis.Z)[0]
    notch = Pos(0, 0, 5) * Box(4, 4, 4)
    args = TopTools_ListOfShape()
    args.Append(box.wrapped)
    tools = TopTools_ListOfShape()
    tools.Append(notch.wrapped)
    op = BRepAlgoAPI_Cut()
    op.SetArguments(args)
    op.SetTools(tools)
    op.SetToFillHistory(True)
    op.Build()
    result = Solid(op.Shape())
    assert not op.IsDeleted(bottom.wrapped)
    assert op.Modified(bottom.wrapped).Size() == 0        # not reshaped
    same = [f for f in result.faces() if f.wrapped.IsSame(bottom.wrapped)]
    assert len(same) == 1                                 # SAME object


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
