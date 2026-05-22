"""
b3d_manifold.py -- the build123d <-> manifold3d bridge used by the p8 probes.

This module is the load-bearing infrastructure for the face-identity study.
It does three things, and *only* these three:

  1. b3d_to_manifold(shape)        build123d Shape  -> manifold3d.Manifold
  2. boolean + identity tagging    via manifold3d run_original_id / face_id
  3. manifold_to_b3d_solid(mesh)   manifold3d.Mesh  -> build123d Solid (sewn)

WHY A SEPARATE BRIDGE
---------------------
build123d has *no* manifold backend. `Shape.tessellate(tol)` exists
(shape_core.py:2241) and `Mesher._get_shape` (mesher.py:460) sews triangles
back into a Solid -- so the round trip is build123d -> triangles -> Manifold
-> triangles -> Solid. This module is that round trip, with the manifold
identity channels (run_original_id, face_id) threaded through.

A CRITICAL GOTCHA, verified live
--------------------------------
build123d's `Shape.tessellate()` returns triangles with **per-face duplicated
vertices** -- a 10mm box tessellates to 24 verts / 12 tris, i.e. each of the 8
corners appears 3x (once per incident face). manifold3d's `Manifold(Mesh(...))`
constructor treats that as a NON-MANIFOLD mesh (every edge is used by one
triangle only, because the neighbouring triangle references a *different*
vertex index at the same position). Result: `status() == Error.NotManifold`,
`volume() == 0`, an empty boolean.

=> We MUST weld coincident vertices (snap-round to a grid, dedupe) before
handing the mesh to manifold3d. `weld()` below does this. Without it the whole
pipeline silently produces nothing.

Run this file directly for a self-test.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import manifold3d as m

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_MakeSolid,
)
from OCP.gp import gp_Pnt
from OCP.TopoDS import TopoDS

from build123d import Solid, Shell


# --------------------------------------------------------------------------
# build123d  ->  manifold3d
# --------------------------------------------------------------------------

def weld(vp: np.ndarray, tv: np.ndarray, decimals: int = 6):
    """Merge coincident vertices (snap-round + dedupe).

    build123d tessellation duplicates a vertex once per incident face;
    manifold3d needs a single shared index or it reports NotManifold.
    Returns (welded_positions float32, remapped_tris uint32).
    """
    key: dict[tuple, int] = {}
    remap = np.empty(len(vp), dtype=np.uint32)
    welded: list[tuple] = []
    for i, p in enumerate(map(tuple, np.round(vp, decimals).tolist())):
        idx = key.get(p)
        if idx is None:
            idx = len(welded)
            key[p] = idx
            welded.append(p)
        remap[i] = idx
    return np.asarray(welded, dtype=np.float32), remap[tv]


def b3d_to_manifold(shape, tol: float = 0.1, angular_tol: float = 0.2):
    """Tessellate a build123d Shape and build a manifold3d.Manifold.

    `tol` is the linear deflection passed to `Shape.tessellate`. Smaller =
    finer mesh = curved surfaces better approximated.
    """
    verts, tris = shape.tessellate(tol, angular_tol)
    vp = np.array([[v.X, v.Y, v.Z] for v in verts], dtype=np.float64)
    tv = np.array(tris, dtype=np.uint32)
    wvp, wtv = weld(vp, tv)
    man = m.Manifold(m.Mesh(wvp, wtv))
    if man.status() != m.Error.NoError:
        raise RuntimeError(f"manifold build failed: {man.status()} "
                           f"({len(wvp)} verts, {len(wtv)} tris)")
    return man


def tagged_original(shape, tol: float = 0.1):
    """build123d Shape -> manifold 'original' (its surface gets a stable id).

    Returns (manifold, original_id). The id identifies the *input solid* and
    rides every output triangle's run_original_id through booleans.
    """
    man = b3d_to_manifold(shape, tol).as_original()
    return man, man.original_id()


# --------------------------------------------------------------------------
# per-triangle identity extraction from a manifold result
# --------------------------------------------------------------------------

def triangle_origins(mesh: m.Mesh) -> np.ndarray:
    """Per-triangle run_original_id, expanded from the run table.

    run_index is in HALFEDGE units (3 per triangle) -- divide by 3.
    """
    ri = np.asarray(mesh.run_index)
    roid = np.asarray(mesh.run_original_id)
    n_tri = np.asarray(mesh.tri_verts).shape[0]
    origin = np.zeros(n_tri, dtype=np.int64)
    for r in range(len(roid)):
        origin[ri[r] // 3: ri[r + 1] // 3] = roid[r]
    return origin


def triangle_geometry(mesh: m.Mesh):
    """(unit normals, centroids) per triangle."""
    vp = np.asarray(mesh.vert_properties)[:, :3]
    tv = np.asarray(mesh.tri_verts)
    v0, v1, v2 = vp[tv[:, 0]], vp[tv[:, 1]], vp[tv[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = n / np.where(ln == 0, 1.0, ln)
    cen = (v0 + v1 + v2) / 3.0
    return n, cen


# --------------------------------------------------------------------------
# manifold3d  ->  build123d Solid  (sewing)
# --------------------------------------------------------------------------

def manifold_to_b3d_solid(mesh: m.Mesh):
    """Sew a manifold3d Mesh's triangles into a build123d Solid.

    This mirrors `Mesher._get_shape` (mesher.py:460): one BRep face per
    triangle, sewn together. The result is a valid SOLID but every triangle
    is its own analytic PLANE face -- the face count explodes (this is the
    'loss' probe 1 quantifies).
    """
    vp = np.asarray(mesh.vert_properties)[:, :3]
    tv = np.asarray(mesh.tri_verts)
    sew = BRepBuilderAPI_Sewing()
    for tri in tv:
        poly = BRepBuilderAPI_MakePolygon()
        for vi in tri:
            poly.Add(gp_Pnt(*(float(x) for x in vp[vi])))
        poly.Close()
        face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
        sew.Add(face)
    sew.Perform()
    sewed = sew.SewedShape()
    if sewed.ShapeType().name == "TopAbs_SHELL":
        solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(sewed)).Solid()
        return Solid(solid)
    return Shell(sewed)


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    from build123d import Box

    print("self-test: Box(10,10,10) round trip")
    box = Box(10, 10, 10)
    print(f"  build123d Box        : {len(box.faces())} faces")
    man, oid = tagged_original(box)
    print(f"  -> Manifold          : status={man.status()} "
          f"vol={man.volume():.1f} original_id={oid}")
    solid = manifold_to_b3d_solid(man.to_mesh())
    print(f"  -> sewn Solid        : {len(solid.faces())} faces, "
          f"valid={solid.is_valid}, vol={solid.volume:.1f}")
    print("  (6 -> 12: the loss. quantified in probe_loss.py)")
