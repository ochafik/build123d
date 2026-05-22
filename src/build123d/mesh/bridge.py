"""
build123d mesh

name: bridge.py

desc:

The OUT leg of the mesh backend: convert a build123d :class:`~build123d.Shape`
into a ``manifold3d.Manifold``.

The pipeline tessellates the shape **per** :class:`~build123d.Face`, stamps every
triangle of each face with a globally unique integer id, welds the per-face
vertex soup into a single indexed mesh, and builds a ``manifold3d.Mesh64`` whose
seeded ``face_id`` array carries that identity. Two facts make the weld and the
seeding mandatory:

* OCCT triangulates each face independently, so a raw tessellation is a vertex
  *soup* — seam vertices are duplicated and the mesh is topologically open
  everywhere (a box yields 24 vertices, not 8). ``manifold3d`` merges vertices
  only per explicit merge vectors; it does not weld by distance. An un-welded
  soup is rejected as ``Error.NotManifold`` and silently produces an *empty*
  boolean result.
* If ``face_id`` is left unseeded, ``manifold3d`` fills it from its own coplanar
  face calculation — which *shatters* every curved face into one id per facet
  strip (a single cylinder → dozens of ids). Seeding ``face_id`` with one id per
  input ``TopoDS_Face`` makes ``manifold3d`` *maintain* that identity through the
  boolean, which is what enables exact B-rep recovery (see
  :mod:`build123d.mesh.recovery`).

``Mesh64`` (double precision) is used so OCC's ``double`` vertex coordinates
survive without float32 truncation. After construction the manifold status is
asserted; a non-``NoError`` status raises rather than silently yielding an empty
body.

Alongside the ``Manifold`` the bridge returns a :class:`SideMap`: the
``faceID → provenance`` dictionary that records, for every seeded id, the
originating build123d :class:`~build123d.Face`, its exact ``Geom_Surface``,
whether it is planar (and if so the exact plane parameters), and the name of the
source shape. :class:`~build123d.mesh.recovery` consumes the side-map to rebuild
an exact, analytic B-rep.

KEY MANIFOLD3D 3.4.x FACT
-------------------------
``manifold3d.Mesh64`` accepts a ``face_id`` kwarg. The "incompatible function
arguments" rejection seen previously is **not** a dtype problem — nanobind
silently auto-casts dtype and contiguity. It is a *rank* problem: ``face_id``
must be a flat ``(N,)`` array. A ``(N, 1)`` column vector or a Python list is
rejected. The fix is ``np.ascontiguousarray(face_id.ravel(), dtype=np.uint64)``.

license:

    Copyright 2026 Gumyr

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Optional

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_SurfaceType

from build123d.topology import Face, Shape

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

# Default tessellation tolerances for mesh CSG. build123d's exporters default to
# a *relative* deflection, which is wrong for CSG where parts of different sizes
# must align on a common grid; an absolute deflection is used instead.
DEFAULT_LINEAR_TOLERANCE = 0.1  # absolute, model units (mm)
DEFAULT_ANGULAR_TOLERANCE = 0.2  # radians (~11 degrees)

# Grid-snap precision for the cross-face vertex weld. Matches build123d's
# ``TOLERANCE = 1e-6`` contract (six decimal places).
_WELD_DECIMALS = 6

# A process-wide counter so face ids are globally unique across every shape ever
# seeded — booleans can then merge two side-maps with a plain ``dict.update``.
_FACE_ID_COUNTER = itertools.count()


# ---------------------------------------------------------------------------
# side map -- faceID -> provenance
# ---------------------------------------------------------------------------

_SURFACE_KIND = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "PLANE",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "CYLINDER",
    GeomAbs_SurfaceType.GeomAbs_Cone: "CONE",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "SPHERE",
    GeomAbs_SurfaceType.GeomAbs_Torus: "TORUS",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "BEZIER",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "BSPLINE",
}


@dataclass
class FaceRecord:
    """Provenance of one seeded face id.

    Everything :mod:`build123d.mesh.recovery` needs to rebuild an exact face for
    a seeded id: the originating build123d :class:`~build123d.Face`, its exact
    analytic ``Geom_Surface``, whether it is planar, and — for a planar face —
    the exact plane origin and unit normal so boundary vertices can be projected
    back onto the analytic plane.

    Attributes:
        face_id (int): the globally unique seeded id.
        b3d_face (Face): the originating build123d Face.
        surface_kind (str): ``"PLANE"``, ``"CYLINDER"``, ``"SPHERE"``, etc.
        geom_surface (object): the exact ``Geom_Surface`` handle.
        source (str): a name for the source shape (for provenance reporting).
        plane_origin (np.ndarray | None): exact plane origin (planar faces only).
        plane_normal (np.ndarray | None): exact unit normal (planar faces only).
    """

    face_id: int
    b3d_face: Face
    surface_kind: str
    geom_surface: object
    source: str
    plane_origin: Optional[np.ndarray] = None
    plane_normal: Optional[np.ndarray] = None

    @property
    def is_planar(self) -> bool:
        """True if this face lies on an exact ``Geom_Plane``."""
        return self.surface_kind == "PLANE"

    def transformed(self, matrix: np.ndarray) -> "FaceRecord":
        """Return a copy with planar parameters moved by a 3x4 affine transform.

        Face *identity* is transform-invariant, but a planar record's exact
        plane origin and normal live in a fixed frame; when the owning mesh body
        is transformed they must follow so :mod:`build123d.mesh.recovery` keeps
        projecting onto the correct analytic plane. The normal is moved by the
        rotation part only (translation does not apply to a direction).

        Args:
            matrix (np.ndarray): a ``(3, 4)`` affine transform — the first three
                columns are the linear part, the last is the translation.

        Returns:
            FaceRecord: a copy with transformed plane parameters (a non-planar
            record is returned unchanged).
        """
        if not self.is_planar:
            return self
        linear = matrix[:, :3]
        translation = matrix[:, 3]
        assert self.plane_origin is not None and self.plane_normal is not None
        moved_origin = linear @ self.plane_origin + translation
        moved_normal = linear @ self.plane_normal
        return FaceRecord(
            face_id=self.face_id,
            b3d_face=self.b3d_face,
            surface_kind=self.surface_kind,
            geom_surface=self.geom_surface,
            source=self.source,
            plane_origin=moved_origin,
            plane_normal=moved_normal,
        )


class SideMap:
    """The ``faceID → provenance`` map produced by :func:`shape_to_manifold`.

    A :class:`SideMap` is a thin dictionary of :class:`FaceRecord` keyed by the
    globally unique seeded face id. Because ids come from a process-wide counter,
    two side-maps from different shapes never collide; a boolean simply merges
    them with :meth:`merged`.
    """

    __slots__ = ("records",)

    def __init__(self, records: Optional[dict[int, FaceRecord]] = None):
        """Create a side-map.

        Args:
            records (dict[int, FaceRecord] | None): initial records. Defaults to
                an empty map.
        """
        self.records: dict[int, FaceRecord] = dict(records) if records else {}

    def add_face(self, face: Face, source: str) -> int:
        """Analyse ``face``, assign it a fresh global id, and store the record.

        Args:
            face (Face): the build123d Face being seeded.
            source (str): a name for the source shape.

        Returns:
            int: the newly assigned globally unique face id.
        """
        face_id = next(_FACE_ID_COUNTER)
        self.records[face_id] = _analyse_face(face_id, face, source)
        return face_id

    def merged(self, other: "SideMap") -> "SideMap":
        """Return a new side-map combining this map's records with ``other``'s.

        Face ids are globally unique, so the merge is a plain dictionary update
        with no risk of collision. Used by :class:`~build123d.mesh.MeshPart`
        booleans to thread provenance through a CSG chain.

        Args:
            other (SideMap): the side-map to merge in.

        Returns:
            SideMap: a new side-map with the union of both record sets.
        """
        combined = dict(self.records)
        combined.update(other.records)
        return SideMap(combined)

    def transformed(self, matrix: np.ndarray) -> "SideMap":
        """Return a copy with every planar record moved by a 3x4 affine transform.

        Used by :class:`~build123d.mesh.MeshPart` transforms: the manifold and
        its provenance must move together so the recovered B-rep lands in the
        transformed frame.

        Args:
            matrix (np.ndarray): a ``(3, 4)`` affine transform matrix.

        Returns:
            SideMap: a new side-map with transformed plane parameters.
        """
        return SideMap(
            {fid: rec.transformed(matrix) for fid, rec in self.records.items()}
        )

    def __getitem__(self, face_id: int) -> FaceRecord:
        return self.records[int(face_id)]

    def __contains__(self, face_id: object) -> bool:
        try:
            return int(face_id) in self.records  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return False

    def __len__(self) -> int:
        return len(self.records)

    def __bool__(self) -> bool:
        return bool(self.records)

    def __repr__(self) -> str:
        return f"SideMap({len(self.records)} face records)"


def _analyse_face(face_id: int, face: Face, source: str) -> FaceRecord:
    """Extract the exact analytic surface of a build123d Face.

    Args:
        face_id (int): the seeded id this face was assigned.
        face (Face): the build123d Face to analyse.
        source (str): the source-shape name.

    Returns:
        FaceRecord: the provenance record, with plane parameters filled when the
        face is planar.
    """
    adaptor = BRepAdaptor_Surface(face.wrapped)
    kind = _SURFACE_KIND.get(adaptor.GetType(), "OTHER")
    geom_surface = BRep_Tool.Surface_s(face.wrapped)

    record = FaceRecord(
        face_id=face_id,
        b3d_face=face,
        surface_kind=kind,
        geom_surface=geom_surface,
        source=source,
    )
    if kind == "PLANE":
        axis = adaptor.Plane().Axis()
        location = axis.Location()
        direction = axis.Direction()
        record.plane_origin = np.array(
            [location.X(), location.Y(), location.Z()], dtype=np.float64
        )
        record.plane_normal = np.array(
            [direction.X(), direction.Y(), direction.Z()], dtype=np.float64
        )
    return record


# ---------------------------------------------------------------------------
# weld -- merge per-face duplicated tessellation vertices
# ---------------------------------------------------------------------------


def _weld(
    vertices: np.ndarray, triangles: np.ndarray, decimals: int = _WELD_DECIMALS
) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident vertices by grid-snap and re-index the triangles.

    build123d tessellates each Face independently, so a vertex on a shared edge
    exists once per incident face. ``manifold3d`` needs a single shared index or
    it reports ``NotManifold``. The snap is to build123d's ``TOLERANCE``; the
    un-snapped coordinates are averaged back per cluster so no grid bias is
    introduced.

    Args:
        vertices (np.ndarray): ``(N, 3)`` per-face vertex soup.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices into
            ``vertices``.
        decimals (int): number of decimal places for the grid-snap.

    Returns:
        tuple[np.ndarray, np.ndarray]: welded ``(K, 3)`` vertices and re-indexed
        ``(M, 3)`` triangles.
    """
    quantized = np.round(vertices, decimals)
    unique, inverse = np.unique(quantized, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    welded = np.zeros((len(unique), 3), dtype=np.float64)
    counts = np.zeros(len(unique), dtype=np.int64)
    np.add.at(welded, inverse, vertices)
    np.add.at(counts, inverse, 1)
    welded /= counts[:, None]
    return welded, inverse[triangles]


# ---------------------------------------------------------------------------
# forward bridge -- build123d Shape -> seeded manifold3d.Manifold
# ---------------------------------------------------------------------------


def shape_to_manifold(
    shape: Shape,
    *,
    source: str = "shape",
    linear_tolerance: float = DEFAULT_LINEAR_TOLERANCE,
    angular_tolerance: float = DEFAULT_ANGULAR_TOLERANCE,
) -> tuple[m3d.Manifold, SideMap]:
    """Convert a build123d Shape into a seeded ``manifold3d.Manifold``.

    Tessellates the shape **per** :class:`~build123d.Face`, stamps every triangle
    of each face with a globally unique integer id, welds the per-face vertex
    soup into a single indexed mesh, and builds a double-precision ``Mesh64``
    whose ``face_id`` array carries the seeded identity. The manifold's status is
    checked; a non-``NoError`` status raises :class:`ValueError` so an
    un-watertight tessellation cannot silently produce an empty boolean.

    Returns both the ``Manifold`` and a :class:`SideMap` recording, for every
    seeded id, the originating build123d face and its exact ``Geom_Surface`` —
    the provenance :mod:`build123d.mesh.recovery` needs to rebuild an exact
    B-rep.

    Args:
        shape (Shape): any build123d 3D shape (``Solid``, ``Part``,
            ``Compound``).
        source (str): a name for the source shape, recorded on every
            :class:`FaceRecord` for provenance reporting. Defaults to
            ``"shape"``.
        linear_tolerance (float): absolute linear deflection of the
            tessellation, in model units. Defaults to 0.1.
        angular_tolerance (float): angular deflection in radians. Defaults
            to 0.2.

    Returns:
        tuple[manifold3d.Manifold, SideMap]: a guaranteed watertight,
        2-manifold body whose triangles carry seeded face ids, and the
        ``faceID → provenance`` side-map.

    Raises:
        ValueError: if the shape tessellates to an empty mesh, or the welded
            mesh does not import as a valid manifold.
    """
    side_map = SideMap()
    vertex_blocks: list[np.ndarray] = []
    triangle_blocks: list[np.ndarray] = []
    face_id_blocks: list[np.ndarray] = []
    vertex_base = 0

    for face in shape.faces():
        face_id = side_map.add_face(face, source)
        face_vertices, face_triangles = face.tessellate(
            linear_tolerance, angular_tolerance
        )
        if not face_triangles:
            continue
        vertex_blocks.append(
            np.array([(v.X, v.Y, v.Z) for v in face_vertices], dtype=np.float64)
        )
        block_triangles = np.array(face_triangles, dtype=np.int64) + vertex_base
        triangle_blocks.append(block_triangles)
        face_id_blocks.append(np.full(len(block_triangles), face_id, dtype=np.uint64))
        vertex_base += len(face_vertices)

    if not triangle_blocks:
        raise ValueError("Shape tessellated to an empty mesh; cannot build a Manifold")

    vertices = np.concatenate(vertex_blocks, axis=0)
    triangles = np.concatenate(triangle_blocks, axis=0)
    face_ids = np.concatenate(face_id_blocks, axis=0)

    welded_vertices, welded_triangles = _weld(vertices, triangles)

    # Drop triangles that became degenerate after welding (a sliver collapsed by
    # the grid-snap); keep face_ids aligned with the surviving triangles.
    corner_a = welded_triangles[:, 0]
    corner_b = welded_triangles[:, 1]
    corner_c = welded_triangles[:, 2]
    keep = (corner_a != corner_b) & (corner_b != corner_c) & (corner_a != corner_c)
    welded_triangles = welded_triangles[keep]
    face_ids = face_ids[keep]

    manifold = _build_manifold(welded_vertices, welded_triangles, face_ids)

    status = manifold.status()
    if status != m3d.Error.NoError:
        raise ValueError(
            f"Tessellated shape did not import as a valid manifold: {status}. "
            "The welded mesh is not a closed, oriented 2-manifold; try a finer "
            "linear_tolerance."
        )
    if manifold.is_empty():
        raise ValueError("Tessellated shape produced an empty manifold (zero volume).")
    return manifold, side_map


def _build_manifold(
    vertices: np.ndarray, triangles: np.ndarray, face_ids: np.ndarray
) -> m3d.Manifold:
    """Build a double-precision ``Manifold`` carrying a seeded ``face_id``.

    The ``ravel`` below is the actual fix for the "incompatible function
    arguments" rejection: ``face_id`` must be a flat ``(N,)`` C-contiguous array;
    dtype is auto-cast by nanobind.

    Args:
        vertices (np.ndarray): ``(N, 3)`` welded vertex coordinates.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices.
        face_ids (np.ndarray): ``(M,)`` seeded id per triangle.

    Returns:
        manifold3d.Manifold: the seeded manifold (status unchecked).
    """
    mesh = m3d.Mesh64(
        vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
        tri_verts=np.ascontiguousarray(triangles, dtype=np.uint64),
        face_id=np.ascontiguousarray(np.asarray(face_ids).ravel(), dtype=np.uint64),
    )
    return m3d.Manifold(mesh)


# ---------------------------------------------------------------------------
# read identity back out of a boolean result
# ---------------------------------------------------------------------------


@dataclass
class ResultMesh:
    """A manifold boolean result plus its per-triangle seeded ids.

    Attributes:
        vertices (np.ndarray): ``(V, 3)`` float64 vertex coordinates (already
            welded by ``manifold3d``).
        triangles (np.ndarray): ``(T, 3)`` int64 triangle vertex indices.
        face_id (np.ndarray): ``(T,)`` int64 seeded id per triangle.
    """

    vertices: np.ndarray
    triangles: np.ndarray
    face_id: np.ndarray

    @property
    def distinct_ids(self) -> list[int]:
        """The sorted list of distinct seeded ids present in the result."""
        return sorted(set(self.face_id.tolist()))

    def triangles_of(self, face_id: int) -> np.ndarray:
        """Return the triangle indices whose seeded id equals ``face_id``."""
        return np.where(self.face_id == face_id)[0]


def read_result(manifold: m3d.Manifold) -> ResultMesh:
    """Extract vertices, triangles and the seeded ``face_id`` from a manifold.

    Args:
        manifold (manifold3d.Manifold): a (possibly boolean) manifold result.

    Returns:
        ResultMesh: the mesh data plus the per-triangle seeded ids.
    """
    mesh = manifold.to_mesh()
    vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    triangles = np.asarray(mesh.tri_verts, dtype=np.int64)
    face_id = np.asarray(mesh.face_id, dtype=np.int64)
    return ResultMesh(vertices=vertices, triangles=triangles, face_id=face_id)
