"""
build123d mesh

name: recovery.py

desc:

faceID-grouped **exact** B-rep recovery: turn a seeded ``manifold3d`` boolean
result back into an analytic build123d :class:`~build123d.Solid`.

Given a manifold boolean result whose triangles carry seeded ``face_id`` values
(from :func:`build123d.mesh.bridge.shape_to_manifold`) and the matching
:class:`~build123d.mesh.bridge.SideMap`:

* Group output triangles by ``face_id``.
* Split each group into edge-connected components — a single seeded id can carry
  a face the boolean cut into two disjoint pieces, and each piece must become
  its own :class:`~build123d.Face`.
* For each **planar** component: extract the boundary edge loops (mesh edges used
  by exactly one triangle of the component), project their vertices orthogonally
  onto the *known input* ``Geom_Plane`` carried by the side-map — erasing
  tessellation jitter — and build one **exact** planar ``TopoDS_Face`` on that
  analytic plane. The largest loop is the outer boundary; any nested loop is a
  hole.
* For each **curved** component: keep it faceted (one flat face per triangle).
  faceID *identifies* the surface but exact re-trimming of a known cylinder /
  sphere with re-fitted boundary curves is out of scope.
* Sew the faces into a :class:`~build123d.Solid` — or a
  :class:`~build123d.Compound` when the result is several disjoint bodies.

For an all-planar CSG result this yields an exact, analytic, *filletable* B-rep:
the recovered faces lie on the input analytic planes, so the volume is bit-exact
and ``BRepFilletAPI`` fillet/chamfer succeed and produce real analytic blend
surfaces.

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

from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Sewing,
)
from OCP.Geom import Geom_Plane
from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
from OCP.ShapeFix import ShapeFix_Shell, ShapeFix_Solid
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from build123d.topology import Compound, Face, Shell, Solid
from build123d.topology.utils import group_shells_into_solids

from .bridge import FaceRecord, ResultMesh, SideMap

# Sewing tolerance: the welded mesh vertices are well within this of each other.
_SEW_TOLERANCE = 1e-6


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
class RecoveryResult:
    """The outcome of a faceID-grouped B-rep recovery.

    Attributes:
        solid (Solid | Compound | Shell | None): the recovered body — a
            :class:`~build123d.Solid` for a single body, a
            :class:`~build123d.Compound` for several disjoint bodies, a
            :class:`~build123d.Shell` if no closed solid could be made.
        recovered_faces (list[RecoveredFace]): one record per face_id in the
            result mesh (seeded *or* unseeded).
        n_exact_planar (int): number of exact planar faces built.
        n_faceted_curved (int): number of curved seeded ids kept faceted.
        n_unseeded_faceted (int): number of result face_ids that had no
            side-map entry — recovered as anonymous faceted patches (one flat
            ``TopoDS_Face`` per triangle). Non-zero when the input MeshPart
            mixed seeded operands with hull / Minkowski / level_set /
            ``from_mesh`` / imported-STL geometry that synthesised new shell
            facets without analytic provenance.
        is_valid (bool): whether the recovered solid passes ``is_valid``.
        volume (float): the recovered body's volume.
    """

    solid: "Solid | Compound | Shell | None"
    recovered_faces: list[RecoveredFace] = field(default_factory=list)
    n_exact_planar: int = 0
    n_faceted_curved: int = 0
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
    result: ResultMesh, face_id: int, record: FaceRecord
) -> RecoveredFace:
    """Rebuild exact planar ``TopoDS_Face``(s) for one seeded planar id.

    Each edge-connected component of the id's triangle group becomes one exact
    planar :class:`~build123d.Face` on the *known input* ``Geom_Plane``. Within a
    component the largest boundary loop is the outer wire and any nested loop is
    a hole. Boundary vertices are projected onto the exact plane, erasing
    tessellation jitter, so the recovered face is exact.

    Args:
        result (ResultMesh): the boolean result mesh.
        face_id (int): the seeded id to recover.
        record (FaceRecord): the planar provenance record for ``face_id``.

    Returns:
        RecoveredFace: the recovered face(s); ``faces`` is empty if no face
        could be built.
    """
    group = result.triangles[result.triangles_of(face_id)]
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

    def loop_points(loop: list[int]) -> np.ndarray:
        return np.array(
            [_project_to_plane(result.vertices[i], origin, normal) for i in loop]
        )

    def loop_area(points: np.ndarray) -> float:
        coords = np.column_stack([points @ in_plane_u, points @ in_plane_v])
        x_coords, y_coords = coords[:, 0], coords[:, 1]
        return 0.5 * abs(
            np.dot(x_coords, np.roll(y_coords, -1))
            - np.dot(y_coords, np.roll(x_coords, -1))
        )

    def make_wire(loop: list[int]):
        points = loop_points(loop)
        vertices = [
            BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex() for point in points
        ]
        wire_builder = BRepBuilderAPI_MakeWire()
        count = len(vertices)
        all_edges_ok = True
        for index in range(count):
            edge_builder = BRepBuilderAPI_MakeEdge(
                vertices[index], vertices[(index + 1) % count]
            )
            if not edge_builder.IsDone():
                all_edges_ok = False
                continue
            wire_builder.Add(edge_builder.Edge())
        if not all_edges_ok or not wire_builder.IsDone():
            return None
        return wire_builder.Wire()

    faces: list[Face] = []
    for component in _connected_components(group):
        component_triangles = group[component]
        loops = [
            loop for loop in _boundary_loops(component_triangles) if len(loop) >= 3
        ]
        if not loops:
            continue
        scored = sorted(
            ((loop_area(loop_points(loop)), loop) for loop in loops),
            key=lambda pair: -pair[0],
        )
        outer_wire = make_wire(scored[0][1])
        if outer_wire is None:
            continue
        face_builder = BRepBuilderAPI_MakeFace(geom_plane, outer_wire, True)
        for _, hole_loop in scored[1:]:
            hole_wire = make_wire(hole_loop)
            if hole_wire is not None:
                face_builder.Add(TopoDS.Wire_s(hole_wire.Reversed()))
        if face_builder.IsDone():
            faces.append(Face(face_builder.Face()))
        else:
            fallback = BRepBuilderAPI_MakeFace(outer_wire, True)
            if fallback.IsDone():
                faces.append(Face(fallback.Face()))

    if not faces:
        return RecoveredFace(face_id, "PLANE", [], False, "no face built")
    note = "exact Geom_Plane"
    if len(faces) > 1:
        note = f"exact Geom_Plane, split into {len(faces)} pieces"
    return RecoveredFace(face_id, "PLANE", faces, True, note)


def _faceted_patch(result: ResultMesh, face_id: int) -> list[Face]:
    """Return a curved seeded id as a list of flat triangle faces (faceted).

    faceID identifies *which* analytic surface the region lies on, but exact
    re-trimming of that surface with re-fitted boundary curves is out of scope —
    so a curved group is recovered as one flat ``TopoDS_Face`` per triangle.

    Args:
        result (ResultMesh): the boolean result mesh.
        face_id (int): the seeded curved id to recover.

    Returns:
        list[Face]: one flat triangle face per triangle of the group.
    """
    patch: list[Face] = []
    for triangle in result.triangles[result.triangles_of(face_id)]:
        polygon = BRepBuilderAPI_MakePolygon()
        for vertex_index in triangle:
            polygon.Add(gp_Pnt(*(float(x) for x in result.vertices[vertex_index])))
        polygon.Close()
        if not polygon.IsDone():
            continue
        face_builder = BRepBuilderAPI_MakeFace(polygon.Wire())
        if face_builder.IsDone():
            patch.append(Face(face_builder.Face()))
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

    The faces are sewn into a :class:`~build123d.Solid`, or a
    :class:`~build123d.Compound` when the result is several disjoint bodies.

    For an all-planar CSG result this is exact: bit-exact volume, analytic
    planar faces, and a ``fillet()`` / ``chamfer()`` works on the result.
    For a mixed seeded+unseeded result the seeded portion is still exact;
    the unseeded portion is faceted but present (see
    :attr:`RecoveryResult.n_unseeded_faceted` to detect this).

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

    recovered_faces: list[RecoveredFace] = []
    sewing = BRepBuilderAPI_Sewing(_SEW_TOLERANCE)
    n_exact_planar = 0
    n_faceted_curved = 0
    n_unseeded_faceted = 0

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
            patch = _faceted_patch(result, face_id)
            for face in patch:
                sewing.Add(face.wrapped)
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
        if record.is_planar:
            recovered = _recover_planar_face(result, face_id, record)
            for face in recovered.faces:
                sewing.Add(face.wrapped)
            n_exact_planar += len(recovered.faces)
            recovered_faces.append(recovered)
        else:
            patch = _faceted_patch(result, face_id)
            for face in patch:
                sewing.Add(face.wrapped)
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

    sewing.Perform()
    sewed = sewing.SewedShape()

    # Sewing yields one shell per connected component. A CSG result splits into
    # several shells for two distinct reasons that must NOT be conflated:
    #   * disjoint bodies         -> each shell is its own positive Solid;
    #   * a body with a cavity    -> the cavity's inward-facing shell is an
    #                                internal VOID of the enclosing body.
    # Summing a positive Solid per shell would ADD a cavity instead of carving
    # it out. group_shells_into_solids classifies by bounding-box nesting: a
    # shell nested inside another is that body's void (one level of nesting,
    # matching Solid.from_mesh); non-nested shells are separate bodies.
    shells: list[Shell] = []
    explorer = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shell_fix = ShapeFix_Shell()
        shell_fix.Init(TopoDS.Shell_s(explorer.Current()))
        shell_fix.Perform()
        shells.append(Shell(shell_fix.Shell()))
        explorer.Next()

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
        n_unseeded_faceted=n_unseeded_faceted,
        is_valid=is_valid,
        volume=volume,
    )
