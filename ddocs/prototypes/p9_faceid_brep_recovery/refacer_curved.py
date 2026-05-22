"""
refacer_curved.py -- the curved (Tier C) fallback.

For a face group whose side-map surface is NOT a plane (cylinder, sphere,
NURBS), faceID still tells us *which* analytic surface the region lies on --
that is real, retained information.  But the region's boundary arrived as
faceted polylines, and re-trimming the known surface with re-fitted boundary
curves + p-curves is the hard part of a B-rep BOP (doc 02 sec 3.2).

So this prototype is HONEST about the limit: curved groups stay FACETED --
one planar TopoDS_Face per triangle -- and we record that the identity
("this is the bore on Geom_Cylinder r=R") is known but the exact re-trim is
not done.  That is the Tier C research boundary.
"""
from __future__ import annotations

import numpy as np

from build123d import Face

from OCP.gp import gp_Pnt
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeFace,
)

from faceid_bridge import ResultMesh


def faceted_patch(rm: ResultMesh, fid: int) -> list[Face]:
    """Return the curved group as a list of flat triangle Faces (faceted).

    Identity is preserved (every triangle is known to belong to this fid /
    this analytic surface) but the geometry stays faceted -- no exact re-trim.
    """
    tris = rm.tris[rm.tris_of(fid)]
    out: list[Face] = []
    for tri in tris:
        poly = BRepBuilderAPI_MakePolygon()
        for vi in tri:
            poly.Add(gp_Pnt(*(float(x) for x in rm.verts[vi])))
        poly.Close()
        if not poly.IsDone():
            continue
        mf = BRepBuilderAPI_MakeFace(poly.Wire())
        if mf.IsDone():
            out.append(Face(mf.Face()))
    return out
