"""m2b -- fast mesh -> build123d Solid reconstruction.

Closes the #1 named blocker for a manifold3d backend in build123d: turning a
triangle mesh BACK into a build123d BREP `Solid` *fast*.

Earlier build123d prototype work (p1_brep_mesh_roundtrip) measured ONLY the
slow path: one planar `TopoDS_Face` per triangle, all fed into a single
`BRepBuilderAPI_Sewing`. `Sewing` does spatial vertex/edge matching across
every face, so its cost is super-linear in triangle count -- unusable above
~1e4-1e5 triangles.

This module implements and benchmarks the FAST alternative, ported from the
parallel CadQuery effort (cadquery/ddocs/prototypes/p2-fast-mesh2brep). The OCC
kernel is identical for build123d, so the technique transfers; here it is
implemented and verified against build123d's own `Solid` / `Shell` / `Shape`
API.

Three approaches under test:

  A. SLOW BASELINE -- `mesh_to_solid_sewing`
     per-triangle planar face + `BRepBuilderAPI_Sewing`. The p1 path.

  B. FAST LEAD -- `mesh_to_solid_direct`
     The mesh from manifold3d is ALREADY welded: we know the unique vertices
     and the triangle->vertex index arrays. So we can assemble the BREP shell
     with NO spatial search:
       * build each `TopoDS_Vertex` exactly once,
       * build each `TopoDS_Edge` exactly once, keyed by the sorted
         vertex-index pair, and SHARE it between the two triangles using it,
       * build one triangular `TopoDS_Face` per triangle from those edges,
       * `BRep_Builder.Add` every face into a single `TopoDS_Shell`,
       * close the shell into a `TopoDS_Solid`.
     Connectivity comes straight from the index arrays -> near-linear.
     `fix=True` runs `ShapeFix_Shell` to repair face orientation.

  C. FAST LEAD, NO FIX -- `mesh_to_solid_direct_nofix`
     Same as B but skips `ShapeFix_Shell`. manifold3d guarantees consistent
     outward winding, so the raw shell is already a valid closed solid.
     `ShapeFix_Shell` is itself super-linear, so this is the only variant
     that scales to 1e6 triangles.

All three yield an all-planar-facet BREP -- curved geometry is gone either way
(the input is already a mesh). The question here is purely reconstruction
SPEED and downstream usability of the result.

Returns build123d `Solid` objects throughout (or `Shell` / `None` on failure),
so callers get the real build123d type, not a bare OCC handle.
"""
from __future__ import annotations

import numpy as np

from build123d import Solid, Shell

from OCP.gp import gp_Pnt
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_MakeVertex,
)
from OCP.TopoDS import TopoDS, TopoDS_Shell
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.ShapeFix import ShapeFix_Solid, ShapeFix_Shell


# --------------------------------------------------------------------------
# Degenerate-triangle filter
# --------------------------------------------------------------------------
# OCCT's mesher emits a few degenerate triangles (repeated vertex index /
# zero area) near sphere poles etc. The direct approach rejects them naturally
# (MakeWire fails); for a fair comparison the sewing baseline gets the same
# clean input.

def drop_degenerate(verts, tris):
    """Return (tris_clean, n_dropped): triangles with a repeated vertex index
    or near-zero area removed."""
    t = np.asarray(tris)
    ok = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    p = np.asarray(verts)[t]
    area = 0.5 * np.linalg.norm(
        np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    ok &= area > 1e-12
    return t[ok], int((~ok).sum())


# --------------------------------------------------------------------------
# Approach A -- SLOW BASELINE: per-triangle face + Sewing  (the p1 path)
# --------------------------------------------------------------------------

def mesh_to_solid_sewing(verts, tris, sew_tol=1e-6):
    """Per-triangle planar face, sewn together. Returns (build123d Solid|Shell|None, info).

    This is the path p1_brep_mesh_roundtrip / build123d's `Mesher` use. Sewing
    spatially matches coincident vertices/edges across ALL faces -> super-linear.
    """
    sew = BRepBuilderAPI_Sewing(sew_tol)
    n_fail = 0
    for tri in tris:
        p0 = gp_Pnt(*[float(x) for x in verts[tri[0]]])
        p1 = gp_Pnt(*[float(x) for x in verts[tri[1]]])
        p2 = gp_Pnt(*[float(x) for x in verts[tri[2]]])
        poly = BRepBuilderAPI_MakePolygon(p0, p1, p2, True)
        if not poly.IsDone():
            n_fail += 1
            continue
        mf = BRepBuilderAPI_MakeFace(poly.Wire())
        if not mf.IsDone():
            n_fail += 1
            continue
        sew.Add(mf.Face())
    sew.Perform()
    sewed = sew.SewedShape()

    result = None
    kind = "None"
    exp = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
    if exp.More():
        occ_shell = TopoDS.Shell_s(exp.Current())
        mk = BRepBuilderAPI_MakeSolid(occ_shell)
        if mk.IsDone():
            sf = ShapeFix_Solid(mk.Solid())
            sf.Perform()
            result = Solid(TopoDS.Solid_s(sf.Solid()))
            kind = "Solid"
        else:
            result = Shell(occ_shell)
            kind = "Shell"
    return result, {"face_fail": n_fail, "result_kind": kind}


# --------------------------------------------------------------------------
# Approach B -- FAST LEAD: direct shell assembly with shared topology
# --------------------------------------------------------------------------

def mesh_to_solid_direct(verts, tris, fix=True):
    """Build a build123d Solid directly from welded mesh connectivity.

    No spatial matching: vertices built once, edges built once and shared by
    index, faces added straight to a `TopoDS_Shell`.

    Parameters
    ----------
    verts : (N,3) float array of UNIQUE (welded) vertices
    tris  : (M,3) int array of triangle vertex indices
    fix   : if True, run `ShapeFix_Shell` to repair face orientation
            (super-linear; only use up to ~1e5 triangles)

    Returns (build123d Solid|Shell|None, info).
    """
    builder = BRep_Builder()
    verts = np.ascontiguousarray(verts, dtype=np.float64)
    tris = np.ascontiguousarray(tris, dtype=np.int64)
    n_verts = len(verts)

    # 1. one TopoDS_Vertex per unique mesh vertex (built lazily).
    occ_verts = [None] * n_verts

    def get_vertex(i):
        v = occ_verts[i]
        if v is None:
            x, y, z = verts[i]
            v = BRepBuilderAPI_MakeVertex(gp_Pnt(float(x), float(y), float(z))).Vertex()
            occ_verts[i] = v
        return v

    # 2. one TopoDS_Edge per unique vertex-index pair, shared between triangles.
    #    key = (min, max); store the edge built FORWARD (a<b). The second
    #    triangle that uses it reuses the SAME edge REVERSED.
    edges = {}

    def get_edge(a, b):
        key = (a, b) if a < b else (b, a)
        e = edges.get(key)
        if e is None:
            me = BRepBuilderAPI_MakeEdge(get_vertex(key[0]), get_vertex(key[1]))
            if not me.IsDone():
                return None
            e = me.Edge()
            edges[key] = e
        return e if a == key[0] else TopoDS.Edge_s(e.Reversed())

    shell = TopoDS_Shell()
    builder.MakeShell(shell)

    n_face_fail = 0
    n_edge_fail = 0
    for tri in tris:
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        e_ab = get_edge(a, b)
        e_bc = get_edge(b, c)
        e_ca = get_edge(c, a)
        if e_ab is None or e_bc is None or e_ca is None:
            n_edge_fail += 1
            continue
        mw = BRepBuilderAPI_MakeWire(e_ab, e_bc, e_ca)
        if not mw.IsDone():
            n_face_fail += 1
            continue
        mf = BRepBuilderAPI_MakeFace(mw.Wire(), True)  # OnlyPlane=True
        if not mf.IsDone():
            n_face_fail += 1
            continue
        builder.Add(shell, mf.Face())

    shell.Closed(True)

    result = None
    kind = "None"
    if fix:
        sfs = ShapeFix_Shell()
        sfs.Init(shell)
        sfs.Perform()
        fixed = sfs.Shell()
        mk = BRepBuilderAPI_MakeSolid(fixed)
        if mk.IsDone():
            sf = ShapeFix_Solid(mk.Solid())
            sf.Perform()
            result = Solid(TopoDS.Solid_s(sf.Solid()))
            kind = "Solid"
        else:
            result = Shell(fixed)
            kind = "Shell"
    else:
        mk = BRepBuilderAPI_MakeSolid(shell)
        if mk.IsDone():
            result = Solid(TopoDS.Solid_s(mk.Solid()))
            kind = "Solid"
        else:
            result = Shell(shell)
            kind = "Shell"

    return result, {
        "n_verts": n_verts,
        "n_unique_edges": len(edges),
        "n_tris": len(tris),
        "edge_fail": n_edge_fail,
        "face_fail": n_face_fail,
        "result_kind": kind,
    }


def mesh_to_solid_direct_nofix(verts, tris):
    """Direct assembly with NO ShapeFix -- isolates raw build cost and is the
    only variant that scales to 1e6 triangles.

    Relies on manifold3d's guarantee of consistent outward winding, so the
    bare shell is already a valid closed solid.
    """
    return mesh_to_solid_direct(verts, tris, fix=False)
