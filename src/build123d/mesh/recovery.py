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
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.Geom import Geom_Plane
from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Shell, TopoDS_Vertex, TopoDS_Wire

from build123d.topology import Compound, Face, Shell, Solid
from build123d.topology.utils import (
    connected_components_by_vertex,
    group_shells_into_solids,
)

from build123d.geometry import TOLERANCE

from .bridge import DEFAULT_LINEAR_TOLERANCE, FaceRecord, ResultMesh, SideMap

# ---------------------------------------------------------------------------
# seam weld -- collapse boolean-result slivers before any per-id grouping
# ---------------------------------------------------------------------------

# A mesh EDGE shorter than this is treated as a collapsed duplicate, not a
# genuine (if small) feature. Matches build123d's own geometric TOLERANCE; the
# repro's own artefact edges top out two orders of magnitude below this (their
# max is ~3.6e-7) and the next-smallest *legitimate* edges in the same mesh
# start three orders of magnitude above it (~1e-3), so there is no observed
# case in this codebase's test geometry where a real feature is anywhere near
# this threshold.
_SLIVER_EDGE_TOLERANCE = TOLERANCE

# A triangle with area below this is treated as a degenerate sliver rather
# than a genuine (if small) facet: near machine-epsilon, not merely "small".
# A genuine collinear/T-vertex artefact is an exact (to float precision)
# degeneracy, so its area sits many orders of magnitude below this. This must
# stay well *below* the smallest legitimate-but-thin facet a coarse
# tessellation can produce — a genuine (non-degenerate) sliver from a
# low-resolution faceted sphere was observed at area 9.4e-10 on a Minkowski
# rounded box; dropping it split a merged planar face's connected component
# in two, since it was the only link between two otherwise-disconnected
# fragments.
_SLIVER_AREA_TOLERANCE = 1e-15


def _weld_degenerate_triangles(result: ResultMesh) -> ResultMesh:
    """Weld collapsed-edge vertex pairs and drop the slivers they leave behind.

    A ``manifold3d`` boolean (a fillet/chamfer's swept-tool subtract or union,
    in particular) can leave a handful of triangles with near-zero area at its
    own seam curves — an intrinsic artefact of the boolean result, not the
    input tessellation. When such a sliver sits *inside* a planar seeded id's
    own triangle group, it pinches :func:`_boundary_loops` into a
    self-retracing loop, and the exact planar face built on that loop comes
    back ``BRepCheck_UnorientableShape`` (its boundary wire is
    ``BRepCheck_SelfIntersectingWire``) even though the *mesh* is perfectly
    manifold — ``MeshPart.is_valid`` never sees the problem.

    Two distinct sliver shapes are observed, and need different handling:

    * **Duplicate-vertex**: two of the triangle's three corners are
      (near-)coincident — a genuine near-zero-length mesh **edge**. Welding
      that edge's two endpoints together is what actually collapses the
      pinch: dropping the triangle without merging would leave its other two
      corners at two distinct-but-coincident indices, each with its own edge
      to the third corner — exactly the original pinch, just relabelled.
    * **Collinear / T-vertex**: all three corners are distinct and none of
      the three edges is short, but the corners are (near-)colinear — one
      genuinely lies on the segment between the other two. No pair here is
      "the same point", so there is nothing to weld; dropping the triangle
      outright is correct, because a T-vertex triangle's two short legs are
      always shared with the two real (non-degenerate) triangles that meet
      at the T, while its long closing edge is a boolean-seam artefact used
      by no other triangle — so dropping it only ever removes an edge with
      zero remaining users, never orphaning one with one.

    Unlike :func:`build123d.mesh.bridge._weld` (a spatial grid-snap of *every*
    vertex, safe on a fresh per-face tessellation where a shared seam vertex is
    always bit-identical between its two faces), the weld here only welds the
    endpoints of an actual mesh **edge** shorter than
    :data:`_SLIVER_EDGE_TOLERANCE` — i.e. two vertices already joined by a
    triangle in this result. Grid-snapping the whole boolean-result mesh
    instead merges any vertices that happen to land in the same quantization
    cell regardless of whether they are topologically related, which was
    observed to weld unrelated nearby ribbon-loft vertices into one index and
    turn previously-fine edges non-manifold (used by 4 triangles instead of
    2). Restricting the weld to genuine short edges only ever merges vertices
    the mesh itself already connects, so it cannot manufacture a new
    non-manifold edge.

    Merged vertices are grouped by union-find over every sub-tolerance edge,
    then averaged (like :func:`build123d.mesh.bridge._weld`) so the result
    isn't grid-biased. Any triangle still below :data:`_SLIVER_AREA_TOLERANCE`
    after the weld — the duplicate-vertex kind now has a repeated vertex
    index; the collinear kind never had one to begin with — is dropped.
    Because this all runs on the *whole* result mesh before triangles are
    split by ``face_id``, every surviving triangle in every group (planar or
    faceted) still shares its vertex indices with its neighbours, so
    :class:`_SharedTopology` keeps building one edge per seam regardless of
    which side of the seam collapsed.

    One more artefact only becomes visible *after* this weld: a closed-loop
    seam (a bore-rim fillet, running the weld over ~2 · chain-length
    coincidence points instead of an open chain's 2 endpoints) can uncover a
    **back-to-back fin** — two triangles that land on the exact same three
    canonical vertices, wound oppositely, because each was tessellated
    independently on its own side of the seam before welding. See
    :func:`_drop_back_to_back_fins`, called last, below.

    Args:
        result (ResultMesh): the raw boolean result mesh.

    Returns:
        ResultMesh: a mesh with the same seam-sharing invariant, sliver
        triangles removed, and ``face_id`` aligned with the surviving
        triangles.
    """
    vertices = result.vertices
    triangles = result.triangles
    corner_a = vertices[triangles[:, 0]]
    corner_b = vertices[triangles[:, 1]]
    corner_c = vertices[triangles[:, 2]]
    edge_lengths = (
        np.linalg.norm(corner_a - corner_b, axis=1),
        np.linalg.norm(corner_b - corner_c, axis=1),
        np.linalg.norm(corner_c - corner_a, axis=1),
    )
    edge_pairs = (
        (triangles[:, 0], triangles[:, 1]),
        (triangles[:, 1], triangles[:, 2]),
        (triangles[:, 2], triangles[:, 0]),
    )

    parent = np.arange(len(vertices))

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            parent[index], index = root, parent[index]
        return root

    any_short = False
    for (starts, ends), lengths in zip(edge_pairs, edge_lengths):
        short = lengths < _SLIVER_EDGE_TOLERANCE
        if not np.any(short):
            continue
        any_short = True
        for start, end in zip(starts[short].tolist(), ends[short].tolist()):
            root_start, root_end = find(start), find(end)
            if root_start != root_end:
                parent[max(root_start, root_end)] = min(root_start, root_end)

    if any_short:
        roots = np.fromiter(
            (find(i) for i in range(len(vertices))), dtype=np.int64, count=len(vertices)
        )
        unique_roots, inverse = np.unique(roots, return_inverse=True)
        inverse = inverse.reshape(-1)
        welded_vertices = np.zeros((len(unique_roots), 3), dtype=np.float64)
        counts = np.zeros(len(unique_roots), dtype=np.int64)
        np.add.at(welded_vertices, inverse, vertices)
        np.add.at(counts, inverse, 1)
        welded_vertices /= counts[:, None]
        welded_triangles = inverse[triangles]
        welded_face_id = result.face_id
    else:
        welded_vertices = vertices
        welded_triangles = triangles
        welded_face_id = result.face_id

    # Drop every triangle still degenerate after the weld: a repeated vertex
    # index (the duplicate-vertex kind, now collapsed to a point) or a
    # near-zero area with three still-distinct indices (the collinear /
    # T-vertex kind, which the edge-weld above never touches since none of
    # its three edges is short).
    corner_a = welded_triangles[:, 0]
    corner_b = welded_triangles[:, 1]
    corner_c = welded_triangles[:, 2]
    distinct = (corner_a != corner_b) & (corner_b != corner_c) & (corner_a != corner_c)
    points_a = welded_vertices[corner_a]
    points_b = welded_vertices[corner_b]
    points_c = welded_vertices[corner_c]
    areas = 0.5 * np.linalg.norm(np.cross(points_b - points_a, points_c - points_a), axis=1)
    keep = distinct & (areas >= _SLIVER_AREA_TOLERANCE)
    kept_triangles = welded_triangles[keep]
    kept_face_id = welded_face_id[keep]

    # A run of consecutive slivers around a *closed* seam (a bore-rim fillet)
    # welds cleanly per-edge, but the vertex weld can silently uncover a
    # back-to-back fin: two of the surviving triangles referencing the same
    # three canonical vertices with opposite winding (see _drop_back_to_back_fins).
    fin_keep = _drop_back_to_back_fins(kept_triangles)
    if fin_keep.all():
        return ResultMesh(
            vertices=welded_vertices,
            triangles=kept_triangles,
            face_id=kept_face_id,
        )
    return ResultMesh(
        vertices=welded_vertices,
        triangles=kept_triangles[fin_keep],
        face_id=kept_face_id[fin_keep],
    )


def _drop_back_to_back_fins(triangles: np.ndarray) -> np.ndarray:
    """Cancel triangle pairs that share all three vertices with opposite winding.

    **Symptom this fixes.** A closed-loop (bore-rim) fillet/chamfer's own
    ``manifold3d`` boolean can leave, at a handful of spots around the seam, a
    *pair* of triangles that are geometrically the same patch seen from both
    sides — one wound forward, one backward — because the boolean's own seam
    curve touches an existing surface (design K.42's "near-duplicate vertex"
    artefact) from two independently-tessellated sides at once. Before
    :func:`_weld_degenerate_triangles` collapses each side's near-duplicate
    vertices onto one canonical index, the pair look like two distinct
    (nearly-coincident) triangles; *after* the weld they land on the exact
    same three vertex indices, exposing the fin. Left in place, a fin's shared
    edges are used by **four** triangles instead of two (the fin's own two,
    plus the two genuine faces it happens to sit between) —
    ``BRepCheck_Analyzer`` reports these as non-manifold, even though
    ``MeshPart.is_valid`` never sees a problem (a mod-2 mesh library is blind
    to a locally-cancelling double cover).

    **Why cancelling is safe.** A back-to-back pair contributes zero enclosed
    volume and no new boundary — removing both together restores exactly the
    edge-valence the surrounding mesh already relies on (the two genuine
    faces on either side of the fin keep their own two-triangle-per-edge
    count). This is *only* found after the weld — a fin hiding behind two
    different near-duplicate index groups on each side would not be visible
    to a search over the raw (pre-weld) triangle array.

    **Why cancelling only when an edge is over-valenced.** A forward/backward
    pair sharing all three vertices is not automatically a defect: a tiny,
    already-degenerate input can legitimately weld down to two triangles that
    happen to share a triple with opposite winding while every one of their
    edges is still used by exactly the two of them (no non-manifold edge, no
    third or fourth face involved) — cancelling *those* would silently delete
    real, if unfortunately-shaped, geometry that the weld never actually broke
    anything for. A genuine fin, by contrast, always leaves at least one of
    its three edges shared by more than the fin's own two triangles (the
    faces on either side of the fin still reference it too), which is what
    actually manifests as ``BRepCheck``'s four-triangle non-manifold edge.
    Gating cancellation on that condition acts *only* where it demonstrably
    fixes an existing valence problem and never touches an isolated
    (if degenerate) pair that was not causing one.

    **How.** Group triangles by their *unordered* vertex-index triple. Within
    a group of size ≥ 2, rotate each triangle to start at its lowest vertex
    index — this collapses the two cyclic rotations sharing one winding onto
    one key, leaving *at most* two distinct rotated keys per group (a
    triangle on three fixed vertices has exactly two possible windings).
    Exactly two keys present means the group is a genuine forward/backward
    pair (or several); if at least one of the triple's three edges has a
    mesh-wide valence above 2, cancel ``min(count_forward, count_backward)``
    of each, dropping both members of every cancelled pair. A group with only
    one winding present (an ordinary, non-duplicated triangle), with more
    than two triangles sharing a triple but only one winding (impossible for
    a 3-cycle, so unreachable in practice), or whose edges are all already at
    valence 2 is left untouched.

    Args:
        triangles (np.ndarray): ``(M, 3)`` int64 triangle vertex indices,
            already vertex-welded and sliver-dropped (canonical indices).

    Returns:
        np.ndarray: ``(M,)`` boolean keep-mask, ``True`` for every triangle
        that survives fin cancellation.
    """
    if len(triangles) == 0:
        return np.ones(0, dtype=bool)

    edge_valence: dict[tuple[int, int], int] = defaultdict(int)
    for corner_a, corner_b, corner_c in triangles.tolist():
        for u, v in ((corner_a, corner_b), (corner_b, corner_c), (corner_c, corner_a)):
            edge_valence[(u, v) if u < v else (v, u)] += 1

    groups: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for index, (corner_a, corner_b, corner_c) in enumerate(triangles.tolist()):
        groups[tuple(sorted((corner_a, corner_b, corner_c)))].append(index)

    keep = np.ones(len(triangles), dtype=bool)
    for triple, indices in groups.items():
        if len(indices) < 2:
            continue
        over_valenced = any(
            edge_valence[(u, v) if u < v else (v, u)] > 2
            for u, v in ((triple[0], triple[1]), (triple[1], triple[2]), (triple[0], triple[2]))
        )
        if not over_valenced:
            continue
        by_winding: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index in indices:
            corner_a, corner_b, corner_c = (int(x) for x in triangles[index])
            verts = (corner_a, corner_b, corner_c)
            start = min(range(3), key=lambda i: verts[i])
            by_winding[verts[start:] + verts[:start]].append(index)
        if len(by_winding) != 2:
            # Either every triangle in the group shares one winding (not a
            # fin — e.g. a coincidentally-repeated but consistently-oriented
            # facet from an upstream duplicate), or more than two windings
            # are present (impossible for a 3-cycle, so unreachable in
            # practice) — leave the group alone either way.
            continue
        forward, backward = by_winding.values()
        cancelled = min(len(forward), len(backward))
        for index in forward[:cancelled] + backward[:cancelled]:
            keep[index] = False
    return keep


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
    # Split into per-component index arrays in one O(n log n) argsort pass
    # rather than one O(n) np.where scan per component (which would be
    # O(n · components), quadratic when a group fragments into many pieces).
    # Labels are contiguous 0..next_component-1, so bincount gives the run
    # lengths and a stable argsort gives ascending indices within each run.
    if next_component == 0:
        return []
    order = np.argsort(component, kind="stable")
    counts = np.bincount(component, minlength=next_component)
    ends = np.cumsum(counts)
    starts = ends - counts
    return [order[starts[k] : ends[k]] for k in range(next_component)]


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


def _segments_cross(
    start_a: np.ndarray, end_a: np.ndarray, start_b: np.ndarray, end_b: np.ndarray
) -> bool:
    """True if open 2-D segments ``(start_a, end_a)`` and ``(start_b, end_b)`` cross.

    Endpoint touches (shared vertices between consecutive loop segments) are
    deliberately excluded via the open interval, so adjacent loop edges never
    register as crossing at their shared vertex.

    Args:
        start_a (np.ndarray): first segment's start point, ``(2,)``.
        end_a (np.ndarray): first segment's end point, ``(2,)``.
        start_b (np.ndarray): second segment's start point, ``(2,)``.
        end_b (np.ndarray): second segment's end point, ``(2,)``.

    Returns:
        bool: True if the segments cross at an interior point of both.
    """
    direction_a = end_a - start_a
    direction_b = end_b - start_b
    denominator = direction_a[0] * direction_b[1] - direction_a[1] * direction_b[0]
    if abs(denominator) < 1e-12:
        return False  # parallel (or one is degenerate) -- not a transversal crossing
    offset = start_b - start_a
    t = (offset[0] * direction_b[1] - offset[1] * direction_b[0]) / denominator
    u = (offset[0] * direction_a[1] - offset[1] * direction_a[0]) / denominator
    return 1e-9 < t < 1.0 - 1e-9 and 1e-9 < u < 1.0 - 1e-9


def _loop_self_intersects(coords: np.ndarray) -> bool:
    """True if a closed 2-D polygon (ordered vertices) crosses itself.

    Guards :func:`_recover_planar_face` against forcing a wire onto its
    seeded plane when the projection isn't a simple polygon: a component can
    pass the coarse coplanarity check (bounded seam noise) yet still fold
    onto itself once flattened, e.g. a long boundary chord elsewhere in the
    same loop crossing a mildly off-plane seam detail — a configuration
    ``BRepBuilderAPI_MakeFace`` accepts (``IsDone()`` is true) but which comes
    back ``BRepCheck_UnorientableShape``. An O(n²) segment-pair scan is cheap
    here: recovered boundary loops are tens of vertices, not thousands.

    Args:
        coords (np.ndarray): ``(N, 2)`` ordered polygon vertices in the
            plane's local frame.

    Returns:
        bool: True if any two non-adjacent edges of the polygon cross.
    """
    count = len(coords)
    if count < 4:
        return False
    for i in range(count):
        start_a, end_a = coords[i], coords[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or (j + 1) % count == i or (i + 1) % count == j:
                continue  # shares an endpoint with segment i -- not a crossing
            if _segments_cross(start_a, end_a, coords[j], coords[(j + 1) % count]):
                return True
    return False


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
        n_planar_off_plane_faceted (int): number of faces recovered faceted
            because they belong to a seeded *planar* id's connected component
            that failed the coplanarity check (see
            :data:`_PLANAR_COMPONENT_COPLANARITY_TOLERANCE`). Non-zero when a
            fillet/chamfer boolean's own id-inheritance assigned a base
            planar face's id to a handful of triangles that are actually part
            of the swept tool's curved transition — forcing those onto the
            exact plane would corrupt the face, so they are kept faceted
            instead, same as a genuinely curved id.
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
    n_planar_off_plane_faceted: int = 0
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


# A connected component of a *seeded planar* id is trusted to lie on that
# id's exact plane only if every one of its (pre-weld-projection) vertices is
# within this distance of it. manifold3d's fillet/chamfer boolean occasionally
# inherits a base face's id onto a handful of triangles that are actually part
# of the swept tool's own curved transition -- an id-inheritance artifact, not
# a tessellation error. Forcing such an off-plane component onto the analytic
# plane anyway builds a wire whose vertices don't actually lie in the plane's
# 2-D parameter space, which BRepBuilderAPI_MakeFace turns into a
# BRepCheck_UnorientableShape face. A genuine seam component (shared with an
# adjacent faceted patch) deviates from the plane by at most the fillet
# profile's own tessellation sagitta -- observed up to ~0.03 for a half-mm
# fillet at the default 8 segments -- while a misassigned component deviates
# by the fillet radius or more (observed >= 0.5). Matching
# DEFAULT_LINEAR_TOLERANCE (the tessellation deflection already in play
# everywhere else in this bridge) sits comfortably between the two.
_PLANAR_COMPONENT_COPLANARITY_TOLERANCE = DEFAULT_LINEAR_TOLERANCE


def _recover_planar_face(
    result: ResultMesh,
    face_id: int,
    record: FaceRecord,
    topology: _SharedTopology,
    component_of_triangle: np.ndarray,
    faces_by_component: dict[int, list[Face]],
) -> tuple[RecoveredFace, int]:
    """Rebuild exact planar ``TopoDS_Face``(s) for one seeded planar id.

    Each edge-connected component of the id's triangle group becomes one exact
    planar :class:`~build123d.Face` on the *known input* ``Geom_Plane`` --
    unless the component fails the coplanarity check (see
    :data:`_PLANAR_COMPONENT_COPLANARITY_TOLERANCE`), in which case it is
    recovered as a faceted patch instead (:func:`_triangle_faces`), same as a
    genuinely curved id. Within an exact component the largest boundary loop is
    the outer wire and any nested loop is a hole. Each wire is built from the
    **shared** per-segment ``TopoDS_Edge`` objects (see :class:`_SharedTopology`),
    subdividing the boundary at every mesh vertex so its edges line up
    one-to-one with any adjacent faceted patch.

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
        tuple[RecoveredFace, int]: the recovered face(s) (``faces`` is empty if
        none could be built), and how many of those faces are faceted-fallback
        (off-plane component) rather than exact.
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

    def loop_coords(loop: list[int]) -> np.ndarray:
        points = topology.positions[loop]
        return np.column_stack([points @ in_plane_u, points @ in_plane_v])

    def signed_loop_area(coords: np.ndarray) -> float:
        x_coords, y_coords = coords[:, 0], coords[:, 1]
        return 0.5 * (
            np.dot(x_coords, np.roll(y_coords, -1))
            - np.dot(y_coords, np.roll(x_coords, -1))
        )

    def loop_area(coords: np.ndarray) -> float:
        return abs(signed_loop_area(coords))

    faces: list[Face] = []
    n_faceted_fallback = 0
    for component in _connected_components(group):
        component_indices = group_indices[component]
        component_triangles = group[component]

        def fall_back_to_faceted() -> None:
            nonlocal n_faceted_fallback
            patch = _triangle_faces(
                result,
                component_indices,
                topology,
                component_of_triangle,
                faces_by_component,
            )
            faces.extend(patch)
            n_faceted_fallback += len(patch)

        # Coplanarity guard (see _PLANAR_COMPONENT_COPLANARITY_TOLERANCE): a
        # component whose *raw* mesh vertices stray far from the seeded plane
        # is an id-inheritance artifact, not real seam noise -- recover it
        # faceted instead of building a corrupt exact face on top of it.
        raw_points = result.vertices[np.unique(component_triangles)]
        deviation = np.abs((raw_points - origin) @ unit_normal)
        if deviation.max() > _PLANAR_COMPONENT_COPLANARITY_TOLERANCE:
            fall_back_to_faceted()
            continue

        loops = [
            loop for loop in _boundary_loops(component_triangles) if len(loop) >= 3
        ]
        if not loops:
            continue
        loop_coordinates = [loop_coords(loop) for loop in loops]
        scored = sorted(
            zip(loops, loop_coordinates), key=lambda pair: loop_area(pair[1]), reverse=True
        )

        # Self-intersection guard, checked on the loops' *2-D* projection
        # (their eventual pcurve trace) *before* any topology.wire() call: a
        # component can pass the coarse coplanarity check above (bounded seam
        # noise) yet still fold onto itself once flattened -- e.g. a long
        # boundary chord elsewhere in the same loop crossing a mildly
        # off-plane seam detail. Checking (and discarding) only *after*
        # building the wire is unsafe: topology.wire() already shares its
        # edges through _SharedTopology, so a discarded wire still leaves its
        # edge directions cached, silently flipping the orientation a
        # neighbouring face sees on those same edges. Detecting this from the
        # read-only projected coordinates avoids ever touching the shared
        # cache for a component we are about to reject.
        if any(_loop_self_intersects(coords) for _, coords in scored):
            fall_back_to_faceted()
            continue

        outer_wire = topology.wire(scored[0][0])
        if outer_wire is None:
            continue
        # _boundary_loops's natural directed-edge winding already traces a
        # hole in the *opposite* rotational sense from the outer boundary in
        # this same 2-D projection (a hole is bounded by the group's edges
        # the same way the outer loop is, just with the material on the
        # other side) — that's a property of any consistently-wound mesh, not
        # something that depends on which way the plane's own normal happens
        # to point. Unconditionally reversing every hole wire assumed the
        # opposite (that the natural winding always needs flipping), which is
        # only true when the outer loop's own signed area is positive; a
        # negative-signed outer (observed on e.g. a box's bottom face, whose
        # seeded plane_normal points -Z) needs its holes left un-reversed —
        # reversing them anyway silently flips a face-with-hole to
        # BRepCheck_BadOrientationOfSubshape even though every wire and edge
        # checks out individually. Comparing each hole's sign against the
        # outer's is robust to either case.
        outer_sign = signed_loop_area(scored[0][1])
        face_builder = BRepBuilderAPI_MakeFace(geom_plane, outer_wire, True)
        for hole_loop, hole_coords in scored[1:]:
            hole_wire = topology.wire(hole_loop)
            if hole_wire is not None:
                if (signed_loop_area(hole_coords) > 0) == (outer_sign > 0):
                    hole_wire = TopoDS.Wire_s(hole_wire.Reversed())
                face_builder.Add(hole_wire)
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
        return RecoveredFace(face_id, "PLANE", [], False, "no face built"), 0
    note = "exact Geom_Plane"
    if n_faceted_fallback:
        note = (
            f"exact Geom_Plane ({len(faces) - n_faceted_fallback} piece(s)); "
            f"{n_faceted_fallback} triangle(s) in off-plane component(s) "
            "recovered faceted instead (id-inheritance artifact)"
        )
    elif len(faces) > 1:
        note = f"exact Geom_Plane, split into {len(faces)} pieces"
    return (
        RecoveredFace(face_id, "PLANE", faces, n_faceted_fallback == 0, note),
        n_faceted_fallback,
    )


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
        recovered, _n_fallback = _recover_planar_face(
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


def _triangle_faces(
    result: ResultMesh,
    triangle_indices: np.ndarray,
    topology: _SharedTopology,
    component_of_triangle: np.ndarray,
    faces_by_component: dict[int, list[Face]],
) -> list[Face]:
    """Build one flat ``TopoDS_Face`` per triangle of ``triangle_indices``.

    The shared per-segment worker behind :func:`_faceted_patch` (a full seeded
    id's triangles) and :func:`_recover_planar_face`'s per-component
    coplanarity fallback (one connected component's triangles that turned out
    not to lie on the seeded plane) — both need the identical "one flat face
    per triangle, edges from :class:`_SharedTopology`" construction, just over
    a different triangle-index subset of the same result mesh.

    Args:
        result (ResultMesh): the boolean result mesh.
        triangle_indices (np.ndarray): result-mesh triangle indices to build.
        topology (_SharedTopology): the shared vertex/edge cache.
        component_of_triangle (np.ndarray): per-triangle body component label.
        faces_by_component (dict[int, list[Face]]): built faces filed by body
            component (mutated in place).

    Returns:
        list[Face]: one flat triangle face per input triangle index.
    """
    patch: list[Face] = []
    for triangle_index in triangle_indices:
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
    return _triangle_faces(
        result,
        result.triangles_of(face_id),
        topology,
        component_of_triangle,
        faces_by_component,
    )


# ---------------------------------------------------------------------------
# full faceID-grouped recovery
# ---------------------------------------------------------------------------

# ShapeFix_Solid's repair heuristics scale badly with face count -- above
# this bound a repair attempt is skipped and the raw (still likely
# imperfect) solid is returned instead. Calibrated between two observed
# cases: a faceted-curved + planar mixed body (sphere minus a box bore,
# ~2000 faces from the faceted sphere) genuinely NEEDS and is FIXED by
# ShapeFix_Solid in well under a second, while a multi-bore filleted panel
# (~11000+ faces once the hole count climbs past a handful) was observed to
# cost tens of seconds for NO change in the resulting validity either way.
_SHAPE_FIX_SOLID_FACE_LIMIT = 3000


def _count_faces(shape) -> int:
    """Count ``TopAbs_FACE`` subshapes of ``shape`` (a cheap O(n) walk)."""
    count = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        count += 1
        explorer.Next()
    return count


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

    # Collapse any near-duplicate-vertex sliver left by the boolean itself
    # (see _weld_degenerate_triangles) before triangles are split by face_id —
    # a sliver inside a planar group's own component would otherwise pinch its
    # boundary loop into a self-intersecting wire (BRepCheck_UnorientableShape).
    result = _weld_degenerate_triangles(result)

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
    n_planar_off_plane_faceted = 0

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
            recovered, n_fallback = _recover_planar_face(
                result,
                face_id,
                record,
                topology,
                component_of_triangle,
                faces_by_component,
            )
            n_exact_planar += len(recovered.faces) - n_fallback
            n_planar_off_plane_faceted += n_fallback
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
        raw_solid = solid_builder.Solid()
        # ShapeFix_Solid is only attempted up to a face-count bound, checked
        # *first* and cheaply (a plain TopExp walk) — both BRepCheck_Analyzer
        # itself and ShapeFix_Solid's repair heuristics scale badly with face
        # count on a large shell (a fillet's own facet count easily reaches
        # the tens of thousands): each has been observed to cost upwards of
        # twenty seconds on such a shape, for no change in the resulting
        # validity either way. Past the bound, a faceted body this large is
        # going to stay imperfect regardless (see recovery.py's design-note
        # on curved/closed-loop chains), so the raw solid is returned
        # as-is — spending a bounded, size-appropriate check+repair effort
        # matters more than chasing full validity on a huge one.
        if _count_faces(raw_solid) <= _SHAPE_FIX_SOLID_FACE_LIMIT and not BRepCheck_Analyzer(
            raw_solid
        ).IsValid():
            solid_fix = ShapeFix_Solid(raw_solid)
            solid_fix.Perform()
            raw_solid = TopoDS.Solid_s(solid_fix.Solid())
        solids.append(Solid(TopoDS.Solid_s(raw_solid)))

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
        n_planar_off_plane_faceted=n_planar_off_plane_faceted,
        is_valid=is_valid,
        volume=volume,
    )
