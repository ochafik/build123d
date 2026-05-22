"""
faceid_bridge.py -- build123d <-> manifold3d bridge that SEEDS manifold's
per-triangle ``face_id`` from the input topology.

This is the Python prototype of the OCCT C++ design in
``OCCT/ddocs/design/02-attributes-and-feature-recovery.md``: seed
``MeshGL64::faceID`` with one unique id per input ``TopoDS_Face`` so that the
identity survives the boolean and lets us recover an exact, filletable B-rep.

The whole game (doc 02 sec 2): if you let manifold compute ``face_id`` from
coplanarity, a tessellated cylinder *shatters* into one id per facet strip and
identity is useless. If you *seed* it -- one id per input ``Face``, stamped on
every triangle of that face -- manifold *maintains* it through the boolean.

KEY MANIFOLD3D 3.4.x FINDINGS (probed live, see NOTES.md)
---------------------------------------------------------
* ``manifold3d.Mesh.__init__`` DOES take a ``face_id`` kwarg. It works.
* The "incompatible function arguments" rejection the prior effort hit is NOT
  a dtype problem -- nanobind silently auto-casts int64->uint32, int32->uint32,
  float64->float32, and accepts non-contiguous arrays. It is a **rank/shape**
  problem: ``face_id`` must be a flat ``(N,)`` array. A ``(N,1)`` column vector
  (or a Python list) is rejected. ``np.ascontiguousarray(fid.ravel())`` fixes it.
* ``Mesh`` wants uint32 ids + float32 verts; ``Mesh64`` wants uint64 ids +
  float64 verts. We use ``Mesh64`` so OCC's double-precision verts survive.
* ``to_mesh()`` returns ``tri_verts`` as int32 and ``face_id`` as int64, and
  ``.face_id`` is read-only -- to re-seed you must build a fresh ``Mesh``.

So: the ``face_id`` channel works in pure Python. We use it (not the
``reserve_ids``/``run_original_id`` fallback) -- see NOTES.md sec "channel
choice" for why it is the better fit here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import manifold3d as m3d

from build123d import Shape, Face

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType


# ---------------------------------------------------------------------------
# side map  --  id -> provenance
# ---------------------------------------------------------------------------

@dataclass
class FaceRecord:
    """Everything we know about one seeded face id."""
    face_id: int
    b3d_face: Face                 # the originating build123d Face
    surface_kind: str              # 'PLANE' | 'CYLINDER' | 'SPHERE' | ...
    geom_surface: object           # the exact Geom_Surface handle (Geom_Plane, ...)
    solid_name: str                # which source solid it belongs to
    # plane params (filled for planes): point + unit normal
    plane_origin: Optional[np.ndarray] = None
    plane_normal: Optional[np.ndarray] = None
    # cylinder params (filled for cylinders): axis point, axis dir, radius
    cyl_axis_pnt: Optional[np.ndarray] = None
    cyl_axis_dir: Optional[np.ndarray] = None
    cyl_radius: Optional[float] = None


class SideMap:
    """The ``id -> {build123d Face, Geom_Surface, source solid}`` map of doc 02
    sec 3.3.  A running global counter assigns each input Face a unique id."""

    def __init__(self):
        self._counter = 0
        self.records: dict[int, FaceRecord] = {}

    def new_id(self) -> int:
        i = self._counter
        self._counter += 1
        return i

    def add_face(self, face: Face, solid_name: str) -> int:
        fid = self.new_id()
        rec = _analyse_face(fid, face, solid_name)
        self.records[fid] = rec
        return fid

    def __getitem__(self, fid: int) -> FaceRecord:
        return self.records[int(fid)]

    def __contains__(self, fid: int) -> bool:
        return int(fid) in self.records


_SURF_NAME = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "PLANE",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "CYLINDER",
    GeomAbs_SurfaceType.GeomAbs_Cone: "CONE",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "SPHERE",
    GeomAbs_SurfaceType.GeomAbs_Torus: "TORUS",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "BEZIER",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "BSPLINE",
}


def _analyse_face(fid: int, face: Face, solid_name: str) -> FaceRecord:
    """Extract the exact analytic surface of a build123d Face."""
    adaptor = BRepAdaptor_Surface(face.wrapped)
    stype = adaptor.GetType()
    kind = _SURF_NAME.get(stype, "OTHER")
    geom = BRep_Tool.Surface_s(face.wrapped)

    rec = FaceRecord(face_id=fid, b3d_face=face, surface_kind=kind,
                     geom_surface=geom, solid_name=solid_name)

    if kind == "PLANE":
        pln = adaptor.Plane()
        ax = pln.Axis()
        loc = ax.Location()
        d = ax.Direction()
        rec.plane_origin = np.array([loc.X(), loc.Y(), loc.Z()])
        rec.plane_normal = np.array([d.X(), d.Y(), d.Z()])
    elif kind == "CYLINDER":
        cyl = adaptor.Cylinder()
        ax = cyl.Axis()
        loc = ax.Location()
        d = ax.Direction()
        rec.cyl_axis_pnt = np.array([loc.X(), loc.Y(), loc.Z()])
        rec.cyl_axis_dir = np.array([d.X(), d.Y(), d.Z()])
        rec.cyl_radius = cyl.Radius()
    return rec


# ---------------------------------------------------------------------------
# weld  --  merge per-face duplicated tessellation vertices
# ---------------------------------------------------------------------------

def _weld(verts: np.ndarray, tris: np.ndarray, decimals: int = 7):
    """Merge coincident vertices by grid-snap; re-index triangles.

    build123d tessellates each Face independently, so a vertex on a shared
    edge exists once per incident face.  manifold3d needs a single shared
    index or it reports NotManifold.  Returns (welded_verts, remapped_tris,
    old->new map)."""
    quantized = np.round(verts, decimals)
    uniq, inverse = np.unique(quantized, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    welded = np.zeros((len(uniq), 3), dtype=np.float64)
    counts = np.zeros(len(uniq), dtype=np.int64)
    np.add.at(welded, inverse, verts)
    np.add.at(counts, inverse, 1)
    welded /= counts[:, None]
    new_tris = inverse[tris]
    return welded, new_tris, inverse


# ---------------------------------------------------------------------------
# forward bridge  --  build123d Shape  ->  seeded manifold3d.Manifold
# ---------------------------------------------------------------------------

def seed_manifold(shape: Shape, solid_name: str, side_map: SideMap,
                  tol: float = 0.05, angular_tol: float = 0.2):
    """Tessellate a build123d Shape PER FACE, weld, and build a Manifold whose
    every triangle carries the side-map id of its originating Face.

    This is step 1 of the deliverable: seed ``face_id`` per ``TopoDS_Face``.

    Returns (Manifold, info dict).
    """
    all_verts: list[np.ndarray] = []
    all_tris: list[np.ndarray] = []
    all_fids: list[np.ndarray] = []
    vbase = 0
    per_face_ids: list[int] = []

    for face in shape.faces():
        fid = side_map.add_face(face, solid_name)
        per_face_ids.append(fid)
        fverts, ftris = face.tessellate(tol, angular_tol)
        vp = np.array([[v.X, v.Y, v.Z] for v in fverts], dtype=np.float64)
        tv = np.array(ftris, dtype=np.int64)
        if len(tv) == 0:
            continue
        all_verts.append(vp)
        all_tris.append(tv + vbase)
        all_fids.append(np.full(len(tv), fid, dtype=np.int64))
        vbase += len(vp)

    verts = np.concatenate(all_verts, axis=0)
    tris = np.concatenate(all_tris, axis=0)
    fids = np.concatenate(all_fids, axis=0)

    # weld duplicate seam vertices
    wverts, wtris, _ = _weld(verts, tris)

    # drop triangles degenerate after welding (keep face_id aligned!)
    a, b, c = wtris[:, 0], wtris[:, 1], wtris[:, 2]
    keep = (a != b) & (b != c) & (a != c)
    wtris = wtris[keep]
    fids = fids[keep]

    man = _build_manifold64(wverts, wtris, fids)
    info = dict(
        n_input_faces=len(per_face_ids),
        n_verts=len(wverts),
        n_tris=len(wtris),
        status=str(man.status()),
        volume=man.volume() if not man.is_empty() else 0.0,
    )
    if man.status() != m3d.Error.NoError:
        raise RuntimeError(f"manifold build failed for {solid_name}: "
                           f"{man.status()} ({info})")
    return man, info


def _build_manifold64(verts: np.ndarray, tris: np.ndarray,
                      face_id: np.ndarray) -> m3d.Manifold:
    """Build a double-precision Manifold carrying a seeded ``face_id``.

    The flatten/contiguity dance below is the actual fix for the
    "incompatible function arguments" rejection: face_id must be flat (N,)
    and C-contiguous; dtype is auto-cast by nanobind."""
    vp = np.ascontiguousarray(verts, dtype=np.float64)
    tv = np.ascontiguousarray(tris, dtype=np.uint64)
    fid = np.ascontiguousarray(np.asarray(face_id).ravel(), dtype=np.uint64)
    mesh = m3d.Mesh64(vert_properties=vp, tri_verts=tv, face_id=fid)
    return m3d.Manifold(mesh)


# ---------------------------------------------------------------------------
# read identity back out of a result
# ---------------------------------------------------------------------------

@dataclass
class ResultMesh:
    """A manifold boolean result plus its per-triangle seeded ids."""
    verts: np.ndarray      # (V,3) float64 -- already welded by manifold
    tris: np.ndarray       # (T,3) int64
    face_id: np.ndarray    # (T,)  int64  -- the seeded id per triangle

    @property
    def distinct_ids(self) -> list[int]:
        return sorted(set(self.face_id.tolist()))

    def tris_of(self, fid: int) -> np.ndarray:
        return np.where(self.face_id == fid)[0]


def read_result(man: m3d.Manifold) -> ResultMesh:
    """Extract verts/tris/face_id from a manifold boolean result."""
    mesh = man.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    fid = np.asarray(mesh.face_id, dtype=np.int64)
    return ResultMesh(verts=verts, tris=tris, face_id=fid)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from build123d import Box

    print("self-test: seed a Box(10,10,10), check ids survive identity")
    sm = SideMap()
    box = Box(10, 10, 10)
    man, info = seed_manifold(box, "box", sm)
    print(f"  seeded   : {info}")
    print(f"  side map : {len(sm.records)} face records")
    for fid, rec in sm.records.items():
        print(f"    id {fid}: {rec.surface_kind:9s} solid={rec.solid_name}")
    rm = read_result(man)
    print(f"  result   : {len(rm.tris)} tris, distinct ids = {rm.distinct_ids}")
    assert rm.distinct_ids == list(range(6)), "expected 6 face ids"
    print("  OK -- 6 input faces -> 6 distinct face_ids on the mesh")
