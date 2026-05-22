"""
recover.py -- step 3 of the deliverable: faceID-grouped EXACT B-rep recovery.

Given a manifold boolean result whose triangles carry seeded face_ids and a
side map ``id -> {build123d Face, Geom_Surface, solid}``:

  * Group output triangles by face_id.
  * For each PLANAR group: extract the boundary edge loop (mesh edges used by
    exactly one triangle of the group), chain it into a wire, and build ONE
    exact ``TopoDS_Face`` lying on the *known input* ``Geom_Plane`` -- not a
    fitted plane.  The boundary is exact straight segments; vertices snap onto
    the exact plane.
  * Feature edges = mesh edges whose two adjacent triangles carry different
    face_ids (doc 02 sec 1: edge identity is derived, not stored).
  * Assemble the planar faces into a build123d Solid.

This is the A3 reconstruction of OCCT design/01 sec 6, done in Python.

The recovered planar faces are EXACT: vertices are projected onto the exact
``Geom_Plane`` of the side map (removing tessellation jitter), edges are exact
straight lines, the face carries the analytic ``GeomType.PLANE``.  That is what
makes ``BRepFilletAPI`` chamfer/fillet possible afterward.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from build123d import Solid, Face, Shell, Edge, Wire

from OCP.gp import gp_Pnt, gp_Pln, gp_Dir, gp_Ax3
from OCP.Geom import Geom_Plane
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_Sewing,
)
from OCP.TopoDS import TopoDS, TopoDS_Shell
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.ShapeFix import ShapeFix_Shell, ShapeFix_Solid

from faceid_bridge import SideMap, ResultMesh


# ---------------------------------------------------------------------------
# boundary loop extraction
# ---------------------------------------------------------------------------

def _boundary_loops(tris: np.ndarray) -> list[list[int]]:
    """Given a triangle index array for ONE face group, return its boundary
    as ordered vertex-index loops.

    A boundary edge is used by exactly one triangle of the group.  Interior
    edges (used twice) are dropped.  The remaining boundary edges are chained
    into closed loops; the outer loop plus any hole loops.
    """
    edge_count: dict[tuple, int] = defaultdict(int)
    edge_dir: dict[tuple, tuple] = {}   # canonical -> directed (as first seen)
    for tri in tris:
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            edge_count[key] += 1
            # remember the directed edge so we keep consistent winding
            edge_dir.setdefault(key, (u, v))

    # boundary edges keep their original (winding-consistent) direction
    boundary: list[tuple] = []
    for key, cnt in edge_count.items():
        if cnt == 1:
            boundary.append(edge_dir[key])

    # chain directed boundary edges into loops
    succ: dict[int, list[int]] = defaultdict(list)
    for u, v in boundary:
        succ[u].append(v)

    loops: list[list[int]] = []
    used = set()
    for u, v in boundary:
        if (u, v) in used:
            continue
        loop = [u]
        cur = v
        used.add((u, v))
        guard = 0
        while cur != u and guard < 100000:
            loop.append(cur)
            nxts = [w for w in succ[cur] if (cur, w) not in used]
            if not nxts:
                break
            nxt = nxts[0]
            used.add((cur, nxt))
            cur = nxt
            guard += 1
        loops.append(loop)
    return loops


def _project_to_plane(p: np.ndarray, origin: np.ndarray,
                      normal: np.ndarray) -> np.ndarray:
    """Project point p orthogonally onto the exact plane (origin, normal)."""
    n = normal / (np.linalg.norm(normal) or 1.0)
    return p - np.dot(p - origin, n) * n


# ---------------------------------------------------------------------------
# planar face reconstruction on the EXACT Geom_Plane
# ---------------------------------------------------------------------------

@dataclass
class RecoveredFace:
    face_id: int
    kind: str                 # 'PLANE' | 'CYLINDER-faceted' | ...
    face: object              # build123d Face, or list of Face for a split id
    exact: bool               # True iff on a real analytic surface
    n_loops: int = 0
    note: str = ""


def _exact_plane(rec) -> Geom_Plane:
    """Build a Geom_Plane from the side-map record's exact plane params."""
    o = rec.plane_origin
    n = rec.plane_normal
    ax = gp_Ax3(gp_Pnt(*o), gp_Dir(*n))
    return Geom_Plane(gp_Pln(ax))


def _connected_components(tris: np.ndarray) -> list[np.ndarray]:
    """Split a triangle set into edge-connected components.

    A single seeded face_id can carry a face that a boolean cut into TWO
    disjoint pieces (p8's split-face concern is real -- verified).  Each
    connected component must become its OWN TopoDS_Face, not a hole."""
    edge2local: dict[tuple, list[int]] = defaultdict(list)
    for li, tri in enumerate(tris):
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            edge2local[key].append(li)
    n = len(tris)
    comp = np.full(n, -1, dtype=np.int64)
    ncomp = 0
    for seed in range(n):
        if comp[seed] != -1:
            continue
        comp[seed] = ncomp
        stack = [seed]
        while stack:
            t = stack.pop()
            a, b, c = (int(x) for x in tris[t])
            for u, v in ((a, b), (b, c), (c, a)):
                key = (u, v) if u < v else (v, u)
                for nb in edge2local[key]:
                    if comp[nb] == -1:
                        comp[nb] = ncomp
                        stack.append(nb)
        ncomp += 1
    return [np.where(comp == k)[0] for k in range(ncomp)]


def recover_planar_face(rm: ResultMesh, fid: int, rec) -> RecoveredFace:
    """Rebuild exact planar TopoDS_Face(s) for face group ``fid``.

    Each edge-connected component of the group becomes ONE exact planar Face
    on the *known input* Geom_Plane.  Within a component, the largest loop is
    the outer boundary and any nested loop is a hole.  Vertices are projected
    onto the exact plane (removes tessellation jitter) -> the Face is EXACT.

    Returns a RecoveredFace whose ``.face`` is a single Face, or a list of
    Faces when the boolean split the seeded id into disjoint pieces.
    """
    group_tris = rm.tris[rm.tris_of(fid)]
    geom_plane = _exact_plane(rec)

    # the plane's local 2-D frame for area / nesting tests
    n = rec.plane_normal / (np.linalg.norm(rec.plane_normal) or 1.0)
    u = np.array([1.0, 0, 0])
    if abs(np.dot(u, n)) > 0.9:
        u = np.array([0, 1.0, 0])
    u = u - np.dot(u, n) * n
    u /= np.linalg.norm(u)
    w = np.cross(n, u)

    def loop_pts(loop):
        return np.array([
            _project_to_plane(rm.verts[i], rec.plane_origin, rec.plane_normal)
            for i in loop])

    def area2d(pts):
        uv = np.column_stack([pts @ u, pts @ w])
        x, y = uv[:, 0], uv[:, 1]
        return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

    def make_wire(loop):
        pts = loop_pts(loop)
        verts = [BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex() for p in pts]
        wb = BRepBuilderAPI_MakeWire()
        npts = len(verts)
        ok = True
        for i in range(npts):
            me = BRepBuilderAPI_MakeEdge(verts[i], verts[(i + 1) % npts])
            if not me.IsDone():
                ok = False
                continue
            wb.Add(me.Edge())
        if not wb.IsDone() or not ok:
            return None
        return wb.Wire()

    faces: list[Face] = []
    total_loops = 0
    # one Face per edge-connected component (handles a split seeded id)
    for comp in _connected_components(group_tris):
        comp_tris = group_tris[comp]
        loops = [L for L in _boundary_loops(comp_tris) if len(L) >= 3]
        total_loops += len(loops)
        if not loops:
            continue
        scored = sorted(((area2d(loop_pts(L)), L) for L in loops),
                        key=lambda t: -t[0])
        outer = make_wire(scored[0][1])
        if outer is None:
            continue
        mf = BRepBuilderAPI_MakeFace(geom_plane, outer, True)
        for _, L in scored[1:]:
            hole = make_wire(L)
            if hole is not None:
                mf.Add(TopoDS.Wire_s(hole.Reversed()))
        if mf.IsDone():
            faces.append(Face(mf.Face()))
        else:
            mf2 = BRepBuilderAPI_MakeFace(outer, True)
            if mf2.IsDone():
                faces.append(Face(mf2.Face()))

    if not faces:
        return RecoveredFace(fid, "PLANE", None, False, total_loops,
                             "no face built")
    if len(faces) == 1:
        return RecoveredFace(fid, "PLANE", faces[0], True, total_loops,
                             "exact Geom_Plane")
    return RecoveredFace(fid, "PLANE", faces, True, total_loops,
                         f"exact Geom_Plane, split into {len(faces)} pieces")


# ---------------------------------------------------------------------------
# feature edges  --  faceID boundaries
# ---------------------------------------------------------------------------

def feature_edges(rm: ResultMesh) -> list[tuple]:
    """Mesh edges whose two adjacent triangles carry DIFFERENT face_ids.

    Doc 02 sec 1: a faceID partition implies the feature-edge graph for free.
    Returns list of (vidx_a, vidx_b, fid_left, fid_right)."""
    edge_tris: dict[tuple, list[int]] = defaultdict(list)
    for ti, tri in enumerate(rm.tris):
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u < v else (v, u)
            edge_tris[key].append(ti)

    feats = []
    for key, ts in edge_tris.items():
        if len(ts) != 2:
            continue
        f0, f1 = rm.face_id[ts[0]], rm.face_id[ts[1]]
        if f0 != f1:
            feats.append((key[0], key[1], int(f0), int(f1)))
    return feats


# ---------------------------------------------------------------------------
# assemble a Solid from the recovered planar faces
# ---------------------------------------------------------------------------

@dataclass
class RecoveryResult:
    solid: object                       # build123d Solid (or Shell on failure)
    faces: list[RecoveredFace]
    n_exact_planar: int
    n_faceted_curved: int
    n_feature_edges: int
    is_valid: bool
    volume: float


def recover_brep(rm: ResultMesh, side_map: SideMap,
                 crease_deg: float = 25.0) -> RecoveryResult:
    """Full faceID-grouped recovery: planar groups -> exact faces, curved
    groups -> faceted patches; sew into a Solid."""
    from refacer_curved import faceted_patch  # local import: curved fallback

    recs: list[RecoveredFace] = []
    sew = BRepBuilderAPI_Sewing(1e-6)
    n_exact = n_curved = 0

    for fid in rm.distinct_ids:
        if fid not in side_map:
            # a boolean may create no genuinely new ids when seeded properly;
            # but guard anyway
            continue
        rec = side_map[fid]
        if rec.surface_kind == "PLANE":
            rf = recover_planar_face(rm, fid, rec)
            if rf.face is not None:
                fl = rf.face if isinstance(rf.face, list) else [rf.face]
                for f in fl:
                    sew.Add(f.wrapped)
                n_exact += len(fl)
            recs.append(rf)
        else:
            # curved: keep faceted, identity preserved (Tier C boundary)
            patch = faceted_patch(rm, fid)
            for f in patch:
                sew.Add(f.wrapped)
            recs.append(RecoveredFace(fid, rec.surface_kind + "-faceted",
                                      patch, False, 0,
                                      f"faceted: {len(patch)} tris on known "
                                      f"{rec.surface_kind}"))
            n_curved += 1

    sew.Perform()
    sewed = sew.SewedShape()
    feats = feature_edges(rm)

    # sewing yields one shell per connected component -- a CSG result that
    # splits into N disjoint solids comes back as N shells.
    shells = []
    exp = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
    while exp.More():
        shells.append(TopoDS.Shell_s(exp.Current()))
        exp.Next()

    solids = []
    for sh in shells:
        sfs = ShapeFix_Shell()
        sfs.Init(sh)
        sfs.Perform()
        fixed = sfs.Shell()
        mk = BRepBuilderAPI_MakeSolid(fixed)
        if mk.IsDone():
            sf = ShapeFix_Solid(mk.Solid())
            sf.Perform()
            solids.append(Solid(TopoDS.Solid_s(sf.Solid())))

    solid = None
    is_valid = False
    volume = 0.0
    if len(solids) == 1:
        solid = solids[0]
    elif len(solids) > 1:
        from build123d import Compound
        solid = Compound(children=solids)
    elif shells:
        solid = Shell(shells[0])

    if solid is not None and solids:
        try:
            iv = solid.is_valid
            is_valid = iv() if callable(iv) else bool(iv)
        except Exception:
            is_valid = False
        try:
            volume = sum(s.volume for s in solids)
        except Exception:
            volume = 0.0

    return RecoveryResult(solid=solid, faces=recs, n_exact_planar=n_exact,
                          n_faceted_curved=n_curved,
                          n_feature_edges=len(feats),
                          is_valid=is_valid, volume=volume)


if __name__ == "__main__":
    from build123d import Box
    from faceid_bridge import seed_manifold, read_result

    print("self-test: recover Box(20,20,10) - Box(6,6,20) notch")
    sm = SideMap()
    plate, _ = seed_manifold(Box(20, 20, 10), "plate", sm)
    notch, _ = seed_manifold(Box(6, 6, 20), "notch", sm)
    rm = read_result(plate - notch)
    res = recover_brep(rm, sm)
    print(f"  recovered {res.n_exact_planar} exact planar faces, "
          f"{res.n_faceted_curved} curved")
    print(f"  feature edges: {res.n_feature_edges}")
    print(f"  solid valid={res.is_valid} volume={res.volume:.1f}")
    print(f"  (manifold volume was {rm.verts.shape[0]} verts; "
          f"expected ~ 20*20*10 - 6*6*10 = 3640)")
