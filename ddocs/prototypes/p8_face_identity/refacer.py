"""
refacer.py -- rebuild build123d-style Faces from a tagged manifold mesh.

Investigation point 3: can we group result triangles back into build123d
`Face`s so `.faces()` and `filter_by(Plane)` work again?

STRATEGY (lifted from CadQuery p4's tagged_mesh.py, adapted for build123d):
two triangles join the same face iff they
  (1) share a welded edge,
  (2) come from the same origin id (run_original_id), and
  (3) have a dihedral angle below `crease_deg`.
Region-growing this yields one cluster per logical face: a flat face -> one
'planar' cluster; a smooth curved surface -> one 'curved' cluster of facets.

Each planar cluster is then turned into a *real* build123d `Face` by sewing
its triangles and (best-effort) merging coplanar triangles via
`ShapeUpgrade_UnifySameDomain` -- so build123d's own `.geom_type`,
`filter_by(Plane)`, `sort_by(Axis)` work on it.

Curved clusters CANNOT become a single analytic Face -- they stay a faceted
Shell. That asymmetry is the core finding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
import numpy as np
import manifold3d as m

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_Sewing,
)
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.gp import gp_Pnt
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS

from build123d import Face, Shell
from b3d_manifold import triangle_origins, triangle_geometry


@dataclass
class ReFace:
    """One reconstructed logical face from the triangle soup."""
    label: int
    origin_id: int
    tri_indices: np.ndarray
    normal: np.ndarray         # mean unit normal
    centroid: np.ndarray
    kind: str                  # 'planar' | 'curved'

    @property
    def n_tris(self) -> int:
        return len(self.tri_indices)

    @property
    def key(self) -> tuple:
        return (self.origin_id, self.label)


class ReFacer:
    """Region-grows a manifold result mesh back into ReFace clusters and,
    on request, into real build123d Faces."""

    def __init__(self, mesh: m.Mesh, tags: dict[int, str] | None = None):
        self.mesh = mesh
        self.tv = np.asarray(mesh.tri_verts)
        self.origin = triangle_origins(mesh)
        self.normal, self.centroid = triangle_geometry(mesh)
        self.tags = dict(tags or {})
        vp = np.asarray(mesh.vert_properties)[:, :3]
        self.vp = vp
        # weld vertices by rounded position so shared edges are detectable
        key: dict = {}
        weld = np.empty(vp.shape[0], dtype=np.int64)
        for i, p in enumerate(map(tuple, np.round(vp, 6).tolist())):
            weld[i] = key.setdefault(p, len(key))
        self._wtv = weld[self.tv]
        self._edge2tri = None

    def name_of(self, oid: int) -> str:
        return self.tags.get(oid, f"id{oid}")

    def _adjacency(self):
        if self._edge2tri is not None:
            return
        e2t: dict[tuple, list[int]] = defaultdict(list)
        for t in range(len(self._wtv)):
            a, b, c = sorted(int(x) for x in self._wtv[t])
            for e in ((a, b), (a, c), (b, c)):
                e2t[e].append(t)
        self._edge2tri = e2t

    def cluster(self, crease_deg: float = 20.0,
                planar_atol: float = 1e-3) -> list[ReFace]:
        """Region-grow output triangles into ReFace clusters."""
        self._adjacency()
        n = len(self.tv)
        cos_thr = np.cos(np.radians(crease_deg))
        label = np.full(n, -1, dtype=np.int64)
        nlab = 0
        for seed in range(n):
            if label[seed] != -1:
                continue
            label[seed] = nlab
            stack = [seed]
            while stack:
                t = stack.pop()
                a, b, c = sorted(int(x) for x in self._wtv[t])
                for e in ((a, b), (a, c), (b, c)):
                    for u in self._edge2tri[e]:
                        if label[u] != -1:
                            continue
                        if self.origin[u] != self.origin[t]:
                            continue
                        if float(np.dot(self.normal[t],
                                        self.normal[u])) < cos_thr:
                            continue
                        label[u] = nlab
                        stack.append(u)
            nlab += 1
        out: list[ReFace] = []
        for L in range(nlab):
            tris = np.where(label == L)[0]
            ns = self.normal[tris]
            planar = bool(np.allclose(ns, ns[0], atol=planar_atol))
            mean_n = ns.mean(0)
            mean_n = mean_n / (np.linalg.norm(mean_n) or 1.0)
            out.append(ReFace(
                label=L, origin_id=int(self.origin[tris[0]]),
                tri_indices=tris, normal=mean_n,
                centroid=self.centroid[tris].mean(0),
                kind="planar" if planar else "curved"))
        return out

    def to_b3d_face(self, rf: ReFace):
        """Turn ONE planar ReFace into a real build123d Face.

        Sews the cluster's triangles, then runs ShapeUpgrade_UnifySameDomain
        to merge the coplanar triangles into a single face. A planar cluster
        collapses to ONE Face. A curved cluster cannot -- returns a Shell.
        """
        sew = BRepBuilderAPI_Sewing()
        for ti in rf.tri_indices:
            tri = self.tv[ti]
            poly = BRepBuilderAPI_MakePolygon()
            for vi in tri:
                poly.Add(gp_Pnt(*(float(x) for x in self.vp[vi])))
            poly.Close()
            sew.Add(BRepBuilderAPI_MakeFace(poly.Wire()).Face())
        sew.Perform()
        sewed = sew.SewedShape()
        unifier = ShapeUpgrade_UnifySameDomain(sewed, True, True, True)
        unifier.Build()
        merged = unifier.Shape()
        # collect resulting faces
        faces = []
        exp = TopExp_Explorer(merged, TopAbs_FACE)
        while exp.More():
            faces.append(Face(TopoDS.Face_s(exp.Current())))
            exp.Next()
        if rf.kind == "planar" and len(faces) == 1:
            return faces[0]
        # planar-but-split, or curved -> a Shell of >1 face
        return faces

    def b3d_faces(self, crease_deg: float = 20.0):
        """All reconstructed faces as build123d Face / list-of-Face objects."""
        return [(rf, self.to_b3d_face(rf))
                for rf in self.cluster(crease_deg)]
