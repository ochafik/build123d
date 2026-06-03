"""
build123d mesh

name: recovery.py

desc:

faceID-grouped **exact** B-rep recovery: turn a seeded ``manifold3d`` boolean
result back into an analytic build123d :class:`~build123d.Solid`.

Given a manifold boolean result whose triangles carry seeded ``face_id`` values
(from :func:`build123d.mesh.bridge.shape_to_manifold`) and the matching
:class:`~build123d.mesh.bridge.SideMap`, recovery builds a single
topologically-shared shell keyed by *result-mesh vertex index*, mirroring the
direct-assembly pattern of :meth:`build123d.Solid.from_mesh`:

* **One** ``TopoDS_Vertex`` per result-mesh vertex index, built once and reused.
* **One** ``TopoDS_Edge`` per unordered mesh vertex-index pair, built once and
  shared (reversed) between its two incident faces.

This shared topology is the whole point: a planar face and an adjacent faceted
patch that meet at a seam reference the *same* ``TopoDS_Edge`` objects, so the
assembled shell is **valid by construction** — no free or non-manifold seam
edges — even when exact-planar and faceted regions abut. It replaces the older
"sew independently built faces, fall back to fully faceted if the seam is
invalid" stopgap, under which a single unseeded (hull / Minkowski / …) region
forced the *entire* solid to lose its exact planar faces.

The per-id handling, on top of the shared topology:

* Group output triangles by ``face_id``.
* Split each group into edge-connected components — a single seeded id can carry
  a face the boolean cut into two disjoint pieces, and each piece must become
  its own :class:`~build123d.Face`.
* For each **planar** component: extract the boundary edge loops (mesh edges used
  by exactly one triangle of the component) and build one **exact** planar
  ``TopoDS_Face`` on the *known input* ``Geom_Plane``. The boundary wire is
  subdivided at every mesh vertex along the seam — each segment is the shared
  per-pair ``TopoDS_Edge`` — so its edges line up one-to-one with any adjacent
  faceted patch instead of spanning the seam as one long straight edge. The
  largest loop is the outer boundary; any nested loop is a hole.
* For each **curved** seeded component and each **unseeded** id: keep it faceted
  (one flat face per triangle, built from the same shared edges). faceID
  *identifies* a curved surface but exact re-trimming of a known cylinder /
  sphere with re-fitted boundary curves is out of scope; unseeded ids carry no
  surface claim at all.
* Group the assembled faces into shells by shared vertex index, classify
  void/disjoint shells with
  :func:`build123d.topology.utils.group_shells_into_solids`, and emit a
  :class:`~build123d.Solid` — or a :class:`~build123d.Compound` for several
  disjoint bodies.

Seam-vertex placement rule: a mesh vertex incident to **exactly one** planar
group is projected orthogonally onto that group's exact ``Geom_Plane`` (erasing
tessellation jitter, keeping the planar face bit-exact). A vertex incident to
**several** planar groups, or to any faceted group as well, keeps its raw mesh
position — projecting onto one plane would pull it off the others and tear the
shared seam.

For an all-planar CSG result this yields an exact, analytic, *filletable* B-rep:
the recovered faces lie on the input analytic planes, so the volume is bit-exact
and ``BRepFilletAPI`` fillet/chamfer succeed and produce real analytic blend
surfaces. For a mixed-provenance result the planar regions stay exact while only
genuinely-curved / unseeded regions remain faceted — and the whole shell is
still valid.

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

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
)
from OCP.Geom import Geom_Plane
from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shell, TopoDS_Vertex, TopoDS_Wire

from build123d.topology import Compound, Face, Shell, Solid
from build123d.topology.utils import (
    connected_components_by_vertex,
    group_shells_into_solids,
)

from .bridge import FaceRecord, ResultMesh, SideMap

# ---------------------------------------------------------------------------
# connectivity -- split a seeded id into edge-connected components
# ---------------------------------------------------------------------------


def _connected_components(triangles: np.ndarray) -> list[np.ndarray]:
    """Split a triangle set into edge-connected components.

    A single seeded ``face_id`` can carry a face that a boolean cut into two
    disjoint pieces. Each connected component must become its own
    ``TopoDS_Face``, not a hole — so the component split runs *before* boundary
    loop extraction.

    Args:
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices for one
            seeded id.

    Returns:
        list[np.ndarray]: one array of local triangle indices per component.
    """
    edge_to_local: dict[tuple[int, int], list[int]] = defaultdict(list)
    for local_index, triangle in enumerate(triangles):
        corner_a, corner_b, corner_c = (int(x) for x in triangle)
        for start, end in (
            (corner_a, corner_b),
            (corner_b, corner_c),
            (corner_c, corner_a),
        ):
            key = (start, end) if start < end else (end, start)
            edge_to_local[key].append(local_index)

    count = len(triangles)
    component = np.full(count, -1, dtype=np.int64)
    next_component = 0
    for seed in range(count):
        if component[seed] != -1:
            continue
        component[seed] = next_component
        stack = [seed]
        while stack:
            current = stack.pop()
            corner_a, corner_b, corner_c = (int(x) for x in triangles[current])
            for start, end in (
                (corner_a, corner_b),
                (corner_b, corner_c),
                (corner_c, corner_a),
            ):
                key = (start, end) if start < end else (end, start)
                for neighbour in edge_to_local[key]:
                    if component[neighbour] == -1:
                        component[neighbour] = next_component
                        stack.append(neighbour)
        next_component += 1
    return [np.where(component == k)[0] for k in range(next_component)]


# ---------------------------------------------------------------------------
# boundary loop extraction
# ---------------------------------------------------------------------------


def _boundary_loops(triangles: np.ndarray) -> list[list[int]]:
    """Return the boundary of one triangle group as ordered vertex-index loops.

    A boundary edge is used by exactly one triangle of the group; interior edges
    (used twice) are dropped. The remaining boundary edges are chained — keeping
    their winding-consistent direction — into closed loops: the outer loop plus
    any hole loops.

    Args:
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices for one
            connected component.

    Returns:
        list[list[int]]: each loop as an ordered list of vertex indices.
    """
    edge_count: dict[tuple[int, int], int] = defaultdict(int)
    edge_direction: dict[tuple[int, int], tuple[int, int]] = {}
    for triangle in triangles:
        corner_a, corner_b, corner_c = (int(x) for x in triangle)
        for start, end in (
            (corner_a, corner_b),
            (corner_b, corner_c),
            (corner_c, corner_a),
        ):
            key = (start, end) if start < end else (end, start)
            edge_count[key] += 1
            edge_direction.setdefault(key, (start, end))

    boundary = [edge_direction[key] for key, count in edge_count.items() if count == 1]

    successors: dict[int, list[int]] = defaultdict(list)
    for start, end in boundary:
        successors[start].append(end)

    loops: list[list[int]] = []
    used: set[tuple[int, int]] = set()
    for start, end in boundary:
        if (start, end) in used:
            continue
        loop = [start]
        current = end
        used.add((start, end))
        guard = 0
        while current != start and guard < 1_000_000:
            loop.append(current)
            candidates = [w for w in successors[current] if (current, w) not in used]
            if not candidates:
                break
            following = candidates[0]
            used.add((current, following))
            current = following
            guard += 1
        loops.append(loop)
    return loops


def _project_to_plane(
    point: np.ndarray, origin: np.ndarray, normal: np.ndarray
) -> np.ndarray:
    """Project ``point`` orthogonally onto the exact plane (``origin``, ``normal``).

    Args:
        point (np.ndarray): the 3-D point to project.
        origin (np.ndarray): a point on the plane.
        normal (np.ndarray): the plane normal (need not be unit length).

    Returns:
        np.ndarray: the orthogonal projection of ``point`` onto the plane.
    """
    unit_normal = normal / (np.linalg.norm(normal) or 1.0)
    return point - np.dot(point - origin, unit_normal) * unit_normal


# ---------------------------------------------------------------------------
# shared-topology assembly keyed by result-mesh vertex index
# ---------------------------------------------------------------------------


class _SharedTopology:
    """Per-result-mesh-vertex-index shared ``TopoDS_Vertex`` / ``TopoDS_Edge``.

    The crux of valid mixed planar/faceted recovery: every face built through
    this object — exact planar wire segment *or* faceted triangle edge — that
    references the same unordered mesh vertex-index pair reuses the **same**
    ``TopoDS_Edge`` (shared reversed between its two incident faces), exactly as
    :meth:`build123d.Solid.from_mesh`'s ``get_edge`` does. Planar and faceted
    regions that abut at a seam therefore share edges, so the assembled shell
    has no free / non-manifold seam edges and is valid by construction.

    Vertex positions follow the seam rule (see :func:`_vertex_positions`):
    vertices incident to exactly one planar group are pre-projected onto that
    group's exact plane; every other vertex keeps its raw mesh position.

    Attributes:
        positions (np.ndarray): ``(V, 3)`` placed vertex coordinates.
    """

    def __init__(self, positions: np.ndarray):
        """Initialise the shared-topology caches.

        Args:
            positions (np.ndarray): ``(V, 3)`` placed vertex coordinates, one
                per result-mesh vertex index.
        """
        self.positions = positions
        self._vertices: list[TopoDS_Vertex | None] = [None] * len(positions)
        self._edges: dict[tuple[int, int], TopoDS_Edge] = {}

    def vertex(self, index: int) -> TopoDS_Vertex:
        """Return the shared ``TopoDS_Vertex`` for mesh vertex ``index``."""
        vertex = self._vertices[index]
        if vertex is None:
            x, y, z = self.positions[index]
            vertex = BRepBuilderAPI_MakeVertex(
                gp_Pnt(float(x), float(y), float(z))
            ).Vertex()
            self._vertices[index] = vertex
        return vertex

    def edge(self, start: int, end: int) -> TopoDS_Edge | None:
        """Return the shared ``TopoDS_Edge`` for the pair ``(start, end)``.

        The edge is stored forward (``key[0] < key[1]``); a caller traversing it
        in the opposite direction gets the SAME edge reversed, so its two
        incident faces share topology.

        Args:
            start (int): first mesh vertex index of the directed edge.
            end (int): second mesh vertex index of the directed edge.

        Returns:
            TopoDS_Edge | None: the shared edge, oriented ``start -> end``, or
            ``None`` if the edge could not be built (degenerate).
        """
        if start == end:
            return None
        key = (start, end) if start < end else (end, start)
        edge = self._edges.get(key)
        if edge is None:
            edge_builder = BRepBuilderAPI_MakeEdge(
                self.vertex(key[0]), self.vertex(key[1])
            )
            if not edge_builder.IsDone():
                return None
            edge = edge_builder.Edge()
            self._edges[key] = edge
        return edge if start == key[0] else TopoDS.Edge_s(edge.Reversed())

    def wire(self, loop: list[int]) -> TopoDS_Wire | None:
        """Build a closed wire over ``loop`` from shared per-segment edges.

        Each consecutive pair of loop vertices (and the closing pair) becomes the
        shared :meth:`edge` — so the wire is subdivided at every mesh vertex and
        its edges line up one-to-one with any adjacent faceted patch.

        Args:
            loop (list[int]): an ordered list of mesh vertex indices.

        Returns:
            TopoDS_Wire | None: the built wire, or ``None`` if any segment edge
            failed or the wire did not close.
        """
        count = len(loop)
        if count < 3:
            return None
        wire_builder = BRepBuilderAPI_MakeWire()
        for position in range(count):
            edge = self.edge(loop[position], loop[(position + 1) % count])
            if edge is None:
                return None
            wire_builder.Add(edge)
        if not wire_builder.IsDone():
            return None
        return wire_builder.Wire()


def _vertex_positions(result: ResultMesh, side_map: SideMap) -> np.ndarray:
    """Place every result-mesh vertex, applying the seam-projection rule.

    A vertex incident to **exactly one** planar seeded group is projected
    orthogonally onto that group's exact ``Geom_Plane`` — erasing tessellation
    jitter so the planar face stays bit-exact. A vertex incident to several
    planar groups, or to any curved / unseeded group as well, keeps its raw mesh
    position: it already lies on the true seam within tolerance, and projecting
    onto one plane would pull it off the others and tear the shared seam.

    Args:
        result (ResultMesh): the boolean result mesh.
        side_map (SideMap): the ``faceID -> provenance`` map.

    Returns:
        np.ndarray: ``(V, 3)`` placed vertex coordinates.
    """
    vertex_count = len(result.vertices)
    # For each vertex: the set of distinct planar groups touching it, and whether
    # any non-planar (curved or unseeded) group touches it.
    planar_groups: list[set[int]] = [set() for _ in range(vertex_count)]
    touches_nonplanar = np.zeros(vertex_count, dtype=bool)

    for face_id in result.distinct_ids:
        record = side_map[face_id] if face_id in side_map else None
        # Only a *seeded* planar record carries an exact input plane to project
        # onto. A synthetic record's plane (if any) is fitted from these very
        # vertices during recovery, so projecting them here would be circular and
        # could pull a shared seam vertex off a neighbouring region — keep raw.
        is_planar = record is not None and record.is_planar
        triangles = result.triangles[result.triangles_of(face_id)]
        incident = np.unique(triangles)
        if is_planar:
            for index in incident:
                planar_groups[int(index)].add(face_id)
        else:
            touches_nonplanar[incident] = True

    positions = result.vertices.copy()
    for index in range(vertex_count):
        groups = planar_groups[index]
        if len(groups) == 1 and not touches_nonplanar[index]:
            record = side_map[next(iter(groups))]
            assert record.plane_origin is not None and record.plane_normal is not None
            positions[index] = _project_to_plane(
                result.vertices[index], record.plane_origin, record.plane_normal
            )
    return positions


# ---------------------------------------------------------------------------
# recovered-face / recovered-solid records
# ---------------------------------------------------------------------------


@dataclass
class RecoveredFace:
    """The build123d faces recovered for one seeded id.

    Attributes:
        face_id (int): the seeded id this record covers.
        kind (str): ``"PLANE"`` for an exact planar recovery, or
            ``"<SURFACE>-faceted"`` for a curved fallback patch.
        faces (list[Face]): the recovered build123d faces (one per connected
            component for a planar id; one per triangle for a faceted patch).
        exact (bool): True iff the faces lie on a real analytic surface.
        note (str): a short human-readable description.
    """

    face_id: int
    kind: str
    faces: list[Face]
    exact: bool
    note: str = ""


@dataclass
class RecoveryResult:  # pylint: disable=too-many-instance-attributes
    """The outcome of a faceID-grouped B-rep recovery.

    Attributes:
        solid (Solid | Compound | Shell | None): the recovered body — a
            :class:`~build123d.Solid` for a single body, a
            :class:`~build123d.Compound` for several disjoint bodies, a
            :class:`~build123d.Shell` if no closed solid could be made.
        recovered_faces (list[RecoveredFace]): one record per face_id in the
            result mesh (seeded *or* unseeded).
        n_exact_planar (int): number of exact planar faces built (seeded ids on
            the *known input* ``Geom_Plane``).
        n_faceted_curved (int): number of curved seeded ids kept faceted.
        n_synthetic_planar (int): number of synthetic coplanar-region ids
            (``hull`` / ``minkowski`` / ``from_mesh``) recovered as a single
            **fitted**-plane analytic face per connected component — planar
            within tolerance, but on a plane fitted from the region's own
            triangles, NOT a seeded-from-input exact plane. Distinct from
            :attr:`n_exact_planar` for exactly that reason.
        n_synthetic_faceted (int): number of synthetic coplanar-region ids whose
            triangles were *not* coplanar within tolerance (a curved region of a
            constructive op) and so were kept as a single faceted patch.
        n_unseeded_faceted (int): number of result face_ids that had no
            side-map entry — recovered as anonymous faceted patches (one flat
            ``TopoDS_Face`` per triangle). Non-zero when the input MeshPart
            mixed seeded operands with hull / Minkowski / level_set /
            ``from_mesh`` / imported-STL geometry that synthesised new shell
            facets without analytic provenance. Such a mixed result is now
            valid by construction (shared seam topology), so a non-zero count
            no longer implies an invalid body.
        is_valid (bool): whether the recovered solid passes ``is_valid``.
            Because planar and faceted regions share their seam edges, a mixed
            planar/faceted body is valid here — the recovery no longer needs the
            fully-faceted fallback for mixed input.
        volume (float): the recovered body's volume.
    """

    solid: "Solid | Compound | Shell | None"
    recovered_faces: list[RecoveredFace] = field(default_factory=list)
    n_exact_planar: int = 0
    n_faceted_curved: int = 0
    n_synthetic_planar: int = 0
    n_synthetic_faceted: int = 0
    n_unseeded_faceted: int = 0
    is_valid: bool = False
    volume: float = 0.0


# ---------------------------------------------------------------------------
# exact planar face reconstruction on the known Geom_Plane
# ---------------------------------------------------------------------------


def _exact_plane(record: FaceRecord) -> Geom_Plane:
    """Build a ``Geom_Plane`` from a planar side-map record's exact parameters.

    Args:
        record (FaceRecord): a planar face record.

    Returns:
        Geom_Plane: the exact analytic plane of the input face.
    """
    origin = record.plane_origin
    normal = record.plane_normal
    assert origin is not None and normal is not None  # planar record invariant
    axis = gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal))
    return Geom_Plane(gp_Pln(axis))


def _recover_planar_face(
    result: ResultMesh,
    face_id: int,
    record: FaceRecord,
    topology: _SharedTopology,
    component_of_triangle: np.ndarray,
    faces_by_component: dict[int, list[Face]],
) -> RecoveredFace:
    """Rebuild exact planar ``TopoDS_Face``(s) for one seeded planar id.

    Each edge-connected component of the id's triangle group becomes one exact
    planar :class:`~build123d.Face` on the *known input* ``Geom_Plane``. Within a
    component the largest boundary loop is the outer wire and any nested loop is
    a hole. Each wire is built from the **shared** per-segment ``TopoDS_Edge``
    objects (see :class:`_SharedTopology`), subdividing the boundary at every
    mesh vertex so its edges line up one-to-one with any adjacent faceted patch.

    A single seeded id can span more than one disjoint body (face ids are not
    confined to one connected component once meshes are fused), so each built
    face is filed into ``faces_by_component`` under the body label of the
    triangles that produced it.

    Args:
        result (ResultMesh): the boolean result mesh.
        face_id (int): the seeded id to recover.
        record (FaceRecord): the planar provenance record for ``face_id``.
        topology (_SharedTopology): the shared vertex/edge cache.
        component_of_triangle (np.ndarray): per-triangle body component label.
        faces_by_component (dict[int, list[Face]]): built faces filed by body
            component (mutated in place).

    Returns:
        RecoveredFace: the recovered face(s); ``faces`` is empty if no face
        could be built.
    """
    group_indices = result.triangles_of(face_id)
    group = result.triangles[group_indices]
    geom_plane = _exact_plane(record)
    origin = record.plane_origin
    normal = record.plane_normal
    assert origin is not None and normal is not None

    # Local 2-D frame on the plane, used to score loop areas for outer/hole.
    unit_normal = normal / (np.linalg.norm(normal) or 1.0)
    in_plane_u = np.array([1.0, 0.0, 0.0])
    if abs(np.dot(in_plane_u, unit_normal)) > 0.9:
        in_plane_u = np.array([0.0, 1.0, 0.0])
    in_plane_u = in_plane_u - np.dot(in_plane_u, unit_normal) * unit_normal
    in_plane_u /= np.linalg.norm(in_plane_u)
    in_plane_v = np.cross(unit_normal, in_plane_u)

    def loop_area(loop: list[int]) -> float:
        points = topology.positions[loop]
        coords = np.column_stack([points @ in_plane_u, points @ in_plane_v])
        x_coords, y_coords = coords[:, 0], coords[:, 1]
        return 0.5 * abs(
            np.dot(x_coords, np.roll(y_coords, -1))
            - np.dot(y_coords, np.roll(x_coords, -1))
        )

    faces: list[Face] = []
    for component in _connected_components(group):
        component_triangles = group[component]
        loops = [
            loop for loop in _boundary_loops(component_triangles) if len(loop) >= 3
        ]
        if not loops:
            continue
        scored = sorted(loops, key=loop_area, reverse=True)
        outer_wire = topology.wire(scored[0])
        if outer_wire is None:
            continue
        face_builder = BRepBuilderAPI_MakeFace(geom_plane, outer_wire, True)
        for hole_loop in scored[1:]:
            hole_wire = topology.wire(hole_loop)
            if hole_wire is not None:
                face_builder.Add(TopoDS.Wire_s(hole_wire.Reversed()))
        face: Face | None = None
        if face_builder.IsDone():
            face = Face(face_builder.Face())
        else:
            fallback = BRepBuilderAPI_MakeFace(outer_wire, True)
            if fallback.IsDone():
                face = Face(fallback.Face())
        if face is not None:
            faces.append(face)
            # A connected planar sub-component lies entirely in one body.
            body = int(component_of_triangle[group_indices[component[0]]])
            faces_by_component[body].append(face)

    if not faces:
        return RecoveredFace(face_id, "PLANE", [], False, "no face built")
    note = "exact Geom_Plane"
    if len(faces) > 1:
        note = f"exact Geom_Plane, split into {len(faces)} pieces"
    return RecoveredFace(face_id, "PLANE", faces, True, note)


# ---------------------------------------------------------------------------
# synthetic coplanar-region recovery: fitted plane if planar, else faceted
# ---------------------------------------------------------------------------

# A synthetic region is treated as planar when every triangle normal aligns with
# the area-weighted region normal to within this cosine tolerance (~0.6 degrees).
# Constructive-op output (hull / from_mesh / Minkowski) that is genuinely flat
# clears this comfortably; a rounded Minkowski shell's facets do not.
_SYNTHETIC_PLANAR_COS_TOLERANCE = 1e-4


def _fit_plane(
    result: ResultMesh, group: np.ndarray
) -> tuple[np.ndarray, np.ndarray] | None:
    """Fit a plane to a synthetic region's triangles, or ``None`` if not planar.

    Computes the area-weighted mean normal of ``group`` and accepts the region
    as planar only if **every** triangle's unit normal aligns with that mean to
    within :data:`_SYNTHETIC_PLANAR_COS_TOLERANCE`. The returned origin is the
    region's centroid; the normal is the unit mean normal. This is a *fitted*
    plane (from the region's own tessellated vertices), deliberately distinct
    from a seeded-from-input exact ``Geom_Plane``.

    Args:
        result (ResultMesh): the boolean result mesh.
        group (np.ndarray): ``(M, 3)`` triangle vertex indices of one synthetic
            id.

    Returns:
        tuple[np.ndarray, np.ndarray] | None: ``(origin, unit_normal)`` if the
        region is coplanar within tolerance, else ``None``.
    """
    corner_a = result.vertices[group[:, 0]]
    corner_b = result.vertices[group[:, 1]]
    corner_c = result.vertices[group[:, 2]]
    cross = np.cross(corner_b - corner_a, corner_c - corner_a)  # 2*area * normal
    lengths = np.linalg.norm(cross, axis=1)
    keep = lengths > 0.0
    if not np.any(keep):
        return None
    mean_normal = cross[keep].sum(axis=0)
    mean_length = np.linalg.norm(mean_normal)
    if mean_length == 0.0:
        return None
    unit_normal = mean_normal / mean_length
    # Every (non-degenerate) facet normal must align with the mean normal.
    unit_facets = cross[keep] / lengths[keep][:, None]
    if np.min(unit_facets @ unit_normal) < 1.0 - _SYNTHETIC_PLANAR_COS_TOLERANCE:
        return None
    origin = np.concatenate([corner_a, corner_b, corner_c], axis=0).mean(axis=0)
    return origin, unit_normal


def _recover_synthetic_face(
    result: ResultMesh,
    face_id: int,
    topology: _SharedTopology,
    component_of_triangle: np.ndarray,
    faces_by_component: dict[int, list[Face]],
) -> tuple[RecoveredFace, bool]:
    """Recover one synthetic coplanar-region id.

    Synthetic ids come from constructive ops (``hull`` / ``minkowski`` /
    ``from_mesh``) that have no input provenance but whose ``manifold3d``-derived
    ``face_id`` channel groups coplanar triangles. If the region's triangles are
    coplanar within tolerance (:func:`_fit_plane`), it is rebuilt as **one
    fitted-plane analytic** :class:`~build123d.Face` per connected component —
    merging what would otherwise be one anonymous ``TopoDS_Face`` per triangle
    into a single planar face. Otherwise (a curved region of the op, e.g. a
    Minkowski rounded shell) it is kept as a single faceted patch.

    The fitted-plane face reuses :func:`_recover_planar_face` via a temporary
    planar :class:`FaceRecord`, so its wires are built from the same shared
    per-segment edges and share seams with neighbours exactly like a seeded face.

    Args:
        result (ResultMesh): the boolean result mesh.
        face_id (int): the synthetic id to recover.
        topology (_SharedTopology): the shared vertex/edge cache.
        component_of_triangle (np.ndarray): per-triangle body component label.
        faces_by_component (dict[int, list[Face]]): built faces filed by body
            component (mutated in place).

    Returns:
        tuple[RecoveredFace, bool]: the recovered record, and ``True`` if it was
        rebuilt as fitted-plane face(s) (``False`` if kept faceted).
    """
    group = result.triangles[result.triangles_of(face_id)]
    # A single-triangle synthetic region is not a *merge*: manifold simply did
    # not coplanar-group it (e.g. a Minkowski rounded shell emits one id per
    # triangle). A lone triangle trivially satisfies the plane fit, so guard
    # against counting it as a genuine merged planar face — keep it faceted so
    # n_synthetic_planar honestly means "coplanar triangles actually merged".
    if len(group) <= 1:
        patch = _faceted_patch(
            result, face_id, topology, component_of_triangle, faces_by_component
        )
        return (
            RecoveredFace(
                face_id,
                "SYNTHETIC-faceted",
                patch,
                False,
                f"faceted: {len(patch)} triangle, ungrouped synthetic region",
            ),
            False,
        )
    fitted = _fit_plane(result, group)
    if fitted is not None:
        origin, normal = fitted
        # A *fitted* plane, not a seeded input plane: a synthetic record carrying
        # the fit so _recover_planar_face can build wires on it.
        fitted_record = FaceRecord(
            face_id=face_id,
            b3d_face=None,
            surface_kind="SYNTHETIC",
            geom_surface=None,
            source="synthetic",
            plane_origin=origin,
            plane_normal=normal,
        )
        recovered = _recover_planar_face(
            result,
            face_id,
            fitted_record,
            topology,
            component_of_triangle,
            faces_by_component,
        )
        if recovered.faces:
            note = "fitted plane (synthetic coplanar region)"
            if len(recovered.faces) > 1:
                note = f"fitted plane (synthetic), split into {len(recovered.faces)}"
            return (
                RecoveredFace(face_id, "SYNTHETIC-plane", recovered.faces, False, note),
                True,
            )
    # Not coplanar (or the fitted face failed to build): keep it faceted.
    patch = _faceted_patch(
        result, face_id, topology, component_of_triangle, faces_by_component
    )
    return (
        RecoveredFace(
            face_id,
            "SYNTHETIC-faceted",
            patch,
            False,
            f"faceted: {len(patch)} triangles, synthetic non-planar region",
        ),
        False,
    )


def _faceted_patch(
    result: ResultMesh,
    face_id: int,
    topology: _SharedTopology,
    component_of_triangle: np.ndarray,
    faces_by_component: dict[int, list[Face]],
) -> list[Face]:
    """Return a curved / unseeded id as a list of flat triangle faces (faceted).

    faceID identifies *which* analytic surface a curved region lies on, but exact
    re-trimming of that surface with re-fitted boundary curves is out of scope —
    so a curved (or unseeded) group is recovered as one flat ``TopoDS_Face`` per
    triangle. Each triangle's wire is built from the **shared** per-segment
    ``TopoDS_Edge`` objects (see :class:`_SharedTopology`), so the patch shares
    its boundary edges with any neighbouring planar face or patch.

    Each triangle face is filed into ``faces_by_component`` under its own body
    component label, so a single faceted id spanning more than one disjoint body
    is split across the right shells.

    Args:
        result (ResultMesh): the boolean result mesh.
        face_id (int): the seeded curved / unseeded id to recover.
        topology (_SharedTopology): the shared vertex/edge cache.
        component_of_triangle (np.ndarray): per-triangle body component label.
        faces_by_component (dict[int, list[Face]]): built faces filed by body
            component (mutated in place).

    Returns:
        list[Face]: one flat triangle face per triangle of the group.
    """
    patch: list[Face] = []
    for triangle_index in result.triangles_of(face_id):
        triangle = result.triangles[triangle_index]
        corner_a, corner_b, corner_c = (int(x) for x in triangle)
        edge_ab = topology.edge(corner_a, corner_b)
        edge_bc = topology.edge(corner_b, corner_c)
        edge_ca = topology.edge(corner_c, corner_a)
        if edge_ab is None or edge_bc is None or edge_ca is None:
            continue
        wire_builder = BRepBuilderAPI_MakeWire(edge_ab, edge_bc, edge_ca)
        if not wire_builder.IsDone():
            continue
        face_builder = BRepBuilderAPI_MakeFace(wire_builder.Wire(), True)
        if face_builder.IsDone():
            face = Face(face_builder.Face())
            patch.append(face)
            faces_by_component[int(component_of_triangle[triangle_index])].append(face)
    return patch


# ---------------------------------------------------------------------------
# full faceID-grouped recovery
# ---------------------------------------------------------------------------


def recover_brep(result: ResultMesh, side_map: SideMap) -> RecoveryResult:
    """Rebuild a build123d body from a seeded boolean result and its side-map.

    Three handlings, one per result face_id:

    * **planar seeded ids** become exact analytic faces on the known
      ``Geom_Plane`` (the §16.8 payoff — bit-exact, filletable);
    * **curved seeded ids** are kept faceted (identity preserved, geometry
      approximate — Tier C re-trim is not yet implemented);
    * **unseeded ids** (face_ids present in the result but missing from
      ``side_map`` — produced when the input MeshPart mixes seeded operands
      with ``hull`` / ``minkowski`` / ``level_set`` / ``from_mesh`` /
      imported-STL geometry that synthesises new shell facets without
      analytic provenance) are rendered as **anonymous faceted patches** —
      one flat ``TopoDS_Face`` per triangle, no surface claim. Without this,
      these ids would be silently dropped and the result would be missing
      whole regions of the input mesh.

    All faces are assembled into shared-topology shells: every ``TopoDS_Vertex``
    is built once per mesh vertex index, every ``TopoDS_Edge`` once per unordered
    index pair and shared (reversed) between its two incident faces (see
    :class:`_SharedTopology`). A planar face and an adjacent faceted patch that
    meet at a seam therefore reference the **same** edge objects, so the result
    shell is valid by construction — no free / non-manifold seam edges. The
    shells become a :class:`~build123d.Solid`, or a
    :class:`~build123d.Compound` when the result is several disjoint bodies.

    For an all-planar CSG result this is exact: bit-exact volume, analytic
    planar faces, and a ``fillet()`` / ``chamfer()`` works on the result.
    For a mixed seeded+unseeded result the seeded planar portion stays exact and
    the curved / unseeded portion is faceted but present — and, unlike the old
    independently-sewn recovery, the whole shell is still valid (see
    :attr:`RecoveryResult.n_unseeded_faceted` to detect a mixed result).

    Args:
        result (ResultMesh): the boolean result mesh, with seeded ``face_id``.
        side_map (SideMap): the ``faceID → provenance`` map.

    Returns:
        RecoveryResult: the recovered body and a breakdown of the recovery.

    Raises:
        ValueError: if the side-map is empty (no seeded ids to group on).
    """
    if not side_map:
        raise ValueError(
            "recover_brep needs a non-empty SideMap; a MeshPart built from raw "
            "arrays carries no provenance — use to_solid(reconstruct=False)."
        )

    # One TopoDS_Vertex / TopoDS_Edge per result-mesh vertex index, placed by
    # the seam-projection rule, shared across all faces built below.
    topology = _SharedTopology(_vertex_positions(result, side_map))

    # Split the whole result into disjoint bodies up front (triangles sharing a
    # vertex index are one body), so each recovered face can be filed into the
    # shell of the body it belongs to — exactly as Solid.from_mesh does.
    component_of_triangle = connected_components_by_vertex(
        result.triangles, len(result.vertices)
    )

    recovered_faces: list[RecoveredFace] = []
    n_exact_planar = 0
    n_faceted_curved = 0
    n_synthetic_planar = 0
    n_synthetic_faceted = 0
    n_unseeded_faceted = 0

    # Faces grouped by body component label, ready for shell assembly. A single
    # face_id is NOT confined to one body once meshes are fused, so each built
    # face is filed by the body component of the triangles that produced it —
    # done inside the recovery helpers, not by an id-level representative.
    faces_by_component: dict[int, list[Face]] = defaultdict(list)

    for face_id in result.distinct_ids:
        if face_id not in side_map:
            # Unseeded id — from hull / Minkowski / level_set / from_mesh /
            # imported-STL operands mixed into the boolean result, or any
            # constructive op that synthesises new shell facets without
            # analytic provenance. No known surface to claim, so render as
            # an anonymous faceted patch (one flat ``TopoDS_Face`` per
            # triangle) — same shape as a curved-residue group, just without
            # a ``surface_kind``. Preserves the full geometry of the result
            # instead of silently dropping it.
            patch = _faceted_patch(
                result, face_id, topology, component_of_triangle, faces_by_component
            )
            n_unseeded_faceted += 1
            recovered_faces.append(
                RecoveredFace(
                    face_id,
                    "UNSEEDED-faceted",
                    patch,
                    False,
                    f"faceted: {len(patch)} triangles, no provenance "
                    "(hull / Minkowski / level_set / from_mesh)",
                )
            )
            continue
        record = side_map[face_id]
        if record.is_synthetic:
            # Synthetic coplanar-region id from a constructive op (hull /
            # minkowski / from_mesh): one fitted-plane analytic face per
            # connected component when coplanar-within-tolerance, else a single
            # faceted patch. Strictly better than the unseeded per-triangle path
            # and clearly distinct from a seeded exact plane.
            synthetic, is_fitted_plane = _recover_synthetic_face(
                result,
                face_id,
                topology,
                component_of_triangle,
                faces_by_component,
            )
            if is_fitted_plane:
                n_synthetic_planar += len(synthetic.faces)
            else:
                n_synthetic_faceted += 1
            recovered_faces.append(synthetic)
        elif record.is_planar:
            recovered = _recover_planar_face(
                result,
                face_id,
                record,
                topology,
                component_of_triangle,
                faces_by_component,
            )
            n_exact_planar += len(recovered.faces)
            recovered_faces.append(recovered)
        else:
            patch = _faceted_patch(
                result, face_id, topology, component_of_triangle, faces_by_component
            )
            n_faceted_curved += 1
            recovered_faces.append(
                RecoveredFace(
                    face_id,
                    f"{record.surface_kind}-faceted",
                    patch,
                    False,
                    f"faceted: {len(patch)} triangles on known "
                    f"{record.surface_kind}",
                )
            )

    # Assemble one TopoDS_Shell per body component directly (no sewing): the
    # faces already share vertices and edges through ``topology``, so adding
    # them to a shell yields a connected, manifold shell with no seam repair.
    # group_shells_into_solids then classifies by bounding-box nesting:
    #   * disjoint bodies      -> each shell is its own positive Solid;
    #   * a body with a cavity -> the cavity's inward-facing shell is an
    #                             internal VOID of the enclosing body.
    builder = BRep_Builder()
    shells: list[Shell] = []
    for component in sorted(faces_by_component):
        faces = faces_by_component[component]
        if not faces:
            continue
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        for face in faces:
            builder.Add(shell, face.wrapped)
        shell.Closed(True)
        shells.append(Shell(shell))

    solids: list[Solid] = []
    for outer_shell, void_shells in group_shells_into_solids(shells):
        solid_builder = BRepBuilderAPI_MakeSolid(outer_shell.wrapped)
        for void_shell in void_shells:
            solid_builder.Add(void_shell.wrapped)
        if not solid_builder.IsDone():
            continue
        solid_fix = ShapeFix_Solid(solid_builder.Solid())
        solid_fix.Perform()
        solids.append(Solid(TopoDS.Solid_s(solid_fix.Solid())))

    solid: Solid | Compound | Shell | None = None
    is_valid = False
    volume = 0.0
    if len(solids) == 1:
        solid = solids[0]
    elif len(solids) > 1:
        solid = Compound(children=solids)
    elif shells:
        solid = shells[0]

    if solids and solid is not None:
        try:
            is_valid = bool(solid.is_valid)
        except Exception:  # pylint: disable=broad-except
            is_valid = False
        try:
            # Each Solid already accounts for its internal voids, so this sum
            # is over disjoint top-level bodies only.
            volume = sum(s.volume for s in solids)
        except Exception:  # pylint: disable=broad-except
            volume = 0.0

    return RecoveryResult(
        solid=solid,
        recovered_faces=recovered_faces,
        n_exact_planar=n_exact_planar,
        n_faceted_curved=n_faceted_curved,
        n_synthetic_planar=n_synthetic_planar,
        n_synthetic_faceted=n_synthetic_faceted,
        n_unseeded_faceted=n_unseeded_faceted,
        is_valid=is_valid,
        volume=volume,
    )
