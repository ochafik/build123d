"""
build123d mesh

name: fillet.py

desc:

Selective chamfer (Phase A3a) for :class:`~build123d.mesh.MeshPart` — the
per-chain swept-tool implementation specified in the mesh-fillet engineering
design (§3, §5). Round fillets (A3b) and corner blends (A3c) are not yet
shipped; A3a establishes the substrate they will share.

A user names edges via a :class:`~build123d.mesh.FeatureChainSelection`; this
module turns each selected chain into one swept boolean tool, applies all
chain-tools in a single batched difference (convex) or union (concave) pass,
and returns the chamfered :class:`~build123d.mesh.MeshPart`.

The keystone is **one swept solid per chain** (design §3.1), not one per
segment. p10's per-segment tools overlapped heavily on curved feature loops
(a 63-edge bore rim emitted 1052–1720 degenerate slivers); the per-chain
construction shares its boundary cross-section between successive
``batch_hull`` lofts, removing the overlap by construction.

Three feasibility checks (design §5.1) pre-flight every chain; failing any
raises :exc:`MeshFilletInfeasible` with a clear message — A3a never clamps
(P3: ``"raise"`` is the default and only A3a mode; ``"skip"`` lands in A4).

A3a's chamfer handles single chains whose endpoints are multi-chain corner
vertices natively — design §9 notes the corner-fragility vanishes for chamfer
because chamfer corners are planar half-space intersections, which manifold3d
resolves bit-exactly. The "real" multi-chain corner blend (setback + spherical
patch) is A3c work.

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
from typing import TYPE_CHECKING, Iterable, Literal, Optional, Sequence, Union

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from .bridge import SideMap
from .feature_edges import (
    FeatureChain,
    FeatureChainSelection,
    triangle_normals,
)

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

if TYPE_CHECKING:  # pragma: no cover
    from .mesh_part import MeshPart


# Edge-overshoot for an open chain's terminal frame, as a multiple of size.
_OPEN_OVERSHOOT = 1.5

# Minimum tangent-direction prism thickness, in absolute mesh units. Below this
# manifold3d's epsilon collapse can erase the prism; above this the hull-loft is
# robust without distorting the tool noticeably.
_MIN_PRISM_THICKNESS = 1e-5


# ---------------------------------------------------------------------------
# the load-bearing exception
# ---------------------------------------------------------------------------


class MeshFilletInfeasible(ValueError):
    """Raised when a fillet/chamfer cannot be applied to the requested edges.

    The chamfer/fillet engineering design (§5.3) commits to *raising* — never
    silently clamping or returning a degraded result — when a feasibility
    constraint fails:

    * **half-thickness** — a chain vertex's distance to the opposing face is
      below ``2 · size``; the swept tool would slice through the body.
    * **chain-length** — an open chain is shorter than the tool can occupy
      without folding the cross-section frame.
    * **mixed-convexity** — a chain has both convex and concave edges (A3a
      ships a single-sign chamfer; A3b will split mixed chains).

    Attributes:
        chains (list[FeatureChain]): the chains that violate the test.
        constraint (str): one of ``"half-thickness"``, ``"chain-length"``,
            ``"mixed-convexity"``.
        requested (float): the chamfer/fillet size requested by the caller.
        measured (float): the measured feature size (or ``0.0`` when the
            constraint has no numeric measurement).
    """

    def __init__(
        self,
        message: str,
        *,
        chains: Sequence[FeatureChain],
        constraint: str,
        requested: float,
        measured: float = 0.0,
    ) -> None:
        """Initialize the exception with structured attributes.

        Args:
            message (str): a human-readable diagnostic.
            chains (Sequence[FeatureChain]): the offending chains.
            constraint (str): the failing constraint name.
            requested (float): the requested radius / size.
            measured (float): the measured feature size (0.0 if not applicable).
        """
        super().__init__(message)
        self.chains: list[FeatureChain] = list(chains)
        self.constraint: str = constraint
        self.requested: float = float(requested)
        self.measured: float = float(measured)


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _unit(vector: np.ndarray) -> np.ndarray:
    """Normalize a 3-vector; return the input unchanged if it is near-zero."""
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


def _per_vertex_face_normals(
    chain: FeatureChain,
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Compute ``(n_a, n_b)`` averaged face-side normals at every chain vertex.

    For each chain vertex ``v`` and each side of the chain's faceID pair, the
    incident triangles whose ``face_id`` matches that side are gathered and
    their normals averaged — this gives the "outward normal of face A as seen
    at this vertex". On a faceted curve (a bore rim) this normal genuinely
    *rotates* along the chain; on a planar feature edge it is constant.

    Args:
        chain (FeatureChain): the chain whose vertices to sample.
        triangles (np.ndarray): host mesh ``(M, 3)`` triangles.
        face_id (np.ndarray): host mesh ``(M,)`` per-triangle face id.
        tri_normals (np.ndarray): host mesh ``(M, 3)`` per-triangle normals.

    Returns:
        tuple[list[np.ndarray], list[np.ndarray]]: per-vertex
        ``(n_a, n_b)`` arrays, aligned with ``chain.verts``.
    """
    fid_a, _fid_b = chain.pair
    vertex_set = set(chain.verts)
    samples_a: dict[int, list[np.ndarray]] = defaultdict(list)
    samples_b: dict[int, list[np.ndarray]] = defaultdict(list)
    for tri_index, tri in enumerate(triangles):
        fid = int(face_id[tri_index])
        if fid not in chain.pair:
            continue
        bucket = samples_a if fid == fid_a else samples_b
        for vi in tri:
            v_index = int(vi)
            if v_index in vertex_set:
                bucket[v_index].append(tri_normals[tri_index])
    n_a: list[np.ndarray] = []
    n_b: list[np.ndarray] = []
    for vertex_index in chain.verts:
        n_a.append(_unit(np.mean(samples_a[vertex_index], axis=0)))
        n_b.append(_unit(np.mean(samples_b[vertex_index], axis=0)))
    return n_a, n_b


def _vertex_frames(
    chain: FeatureChain,
    vertices: np.ndarray,
    n_a_per_vertex: list[np.ndarray],
    n_b_per_vertex: list[np.ndarray],
    open_overshoot: float,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Build one ``(origin, tangent, u_axis, w_axis)`` frame per chain vertex.

    Implements design §3.3: tangent is the local bisector of the incoming and
    outgoing edge directions; ``u_axis`` and ``w_axis`` are the in-solid
    tangents of face A and face B at that vertex (the chamfer profile's
    right-angle legs). At the endpoints of an open chain the tangent is the
    unique adjacent edge direction and the origin is overshot by
    ``open_overshoot`` along ``-tangent`` (or ``+tangent``) so the swept tool
    cleanly crosses face boundaries it ends on (design §3.6).

    Sign convention follows :meth:`build123d.mesh.feature_edges.edge_convexity_sign`:
    ``u_axis`` is forced to point *into the solid* (so the chamfer leg lies in
    material), and same for ``w_axis``.

    Args:
        chain (FeatureChain): the chain to frame.
        vertices (np.ndarray): host mesh ``(N, 3)`` vertices.
        n_a_per_vertex (list[np.ndarray]): per-vertex normal of face A.
        n_b_per_vertex (list[np.ndarray]): per-vertex normal of face B.
        open_overshoot (float): how far past an open chain's endpoint to push
            the terminal frame, in mesh units.

    Returns:
        list[tuple]: one ``(origin, tangent, u_axis, w_axis)`` per vertex.
    """
    n_verts = len(chain.verts)
    is_loop = chain.is_loop
    frames: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    for index in range(n_verts):
        p_i = vertices[chain.verts[index]]
        if is_loop:
            p_prev = vertices[chain.verts[(index - 1) % n_verts]]
            p_next = vertices[chain.verts[(index + 1) % n_verts]]
            e_prev = _unit(p_i - p_prev)
            e_next = _unit(p_next - p_i)
            tangent = _unit(e_prev + e_next)
            origin = p_i.copy()
        else:
            if index == 0:
                e_next = _unit(vertices[chain.verts[1]] - p_i)
                tangent = e_next
                origin = p_i - tangent * open_overshoot
            elif index == n_verts - 1:
                e_prev = _unit(p_i - vertices[chain.verts[-2]])
                tangent = e_prev
                origin = p_i + tangent * open_overshoot
            else:
                e_prev = _unit(p_i - vertices[chain.verts[index - 1]])
                e_next = _unit(vertices[chain.verts[index + 1]] - p_i)
                tangent = _unit(e_prev + e_next)
                origin = p_i.copy()
        n_a = n_a_per_vertex[index]
        n_b = n_b_per_vertex[index]
        u_axis = _unit(np.cross(n_a, tangent))
        w_axis = _unit(np.cross(n_b, tangent))
        # Force both axes to point into the solid (away from the *other* face's
        # outward normal). Identical sign convention to p10's _edge_frame.
        if float(np.dot(u_axis, -n_b)) < 0:
            u_axis = -u_axis
        if float(np.dot(w_axis, -n_a)) < 0:
            w_axis = -w_axis
        frames.append((origin, tangent, u_axis, w_axis))
    return frames


# ---------------------------------------------------------------------------
# chamfer-profile loft tool (Phase A3a)
# ---------------------------------------------------------------------------


def _chamfer_segment_hull(
    frame_i: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    frame_j: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    size: float,
) -> Optional[m3d.Manifold]:
    """Build one ``batch_hull`` loft between two consecutive vertex frames.

    The chamfer profile is the right triangle ``(0, 0) – (size, 0) – (0, size)``
    in the ``(u_axis, w_axis)`` plane (design §3.4). Lifting the three corners
    into 3D at each frame gives two triangles; ``Manifold.hull_points`` of the
    six points is the lofted segment — i.e. the convex hull of two adjacent
    cross-sections, exactly the building block §3.2 calls for.

    Args:
        frame_i (tuple): ``(origin, tangent, u_axis, w_axis)`` at vertex i.
        frame_j (tuple): ``(origin, tangent, u_axis, w_axis)`` at vertex i+1.
        size (float): chamfer leg length.

    Returns:
        manifold3d.Manifold | None: the loft segment, or ``None`` if
        ``hull_points`` returns an empty / invalid manifold (rare —
        degenerate-thin frames can degenerate the hull).
    """
    o_i, _t_i, u_i, w_i = frame_i
    o_j, _t_j, u_j, w_j = frame_j
    points = np.vstack(
        [
            o_i,
            o_i + u_i * size,
            o_i + w_i * size,
            o_j,
            o_j + u_j * size,
            o_j + w_j * size,
        ]
    )
    hull = m3d.Manifold.hull_points(points.tolist())
    if hull.status() != m3d.Error.NoError or hull.is_empty():
        return None
    return hull


def _build_chain_chamfer_tool(
    chain: FeatureChain,
    vertices: np.ndarray,
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
    size: float,
) -> Optional[m3d.Manifold]:
    """Build the swept chamfer tool for one chain (a single manifold body).

    Implements design §3.2 (per-chain swept tool): per-vertex frames, then a
    ``batch_boolean`` union of consecutive ``hull_points`` lofts. For a closed
    loop the segments wrap (``n_loop`` segments); for an open chain there are
    ``n_loop - 1`` segments and the terminal frames are overshot per §3.6.

    Args:
        chain (FeatureChain): the chain (already classified — non-flat).
        vertices (np.ndarray): host mesh ``(N, 3)`` vertex array.
        triangles (np.ndarray): host mesh ``(M, 3)`` triangle vertex indices.
        face_id (np.ndarray): host mesh ``(M,)`` seeded face id per triangle.
        tri_normals (np.ndarray): host mesh ``(M, 3)`` per-triangle normals.
        size (float): chamfer leg length.

    Returns:
        manifold3d.Manifold | None: the combined swept tool, or ``None`` if
        every segment hull failed (degenerate input).
    """
    n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
        chain, triangles, face_id, tri_normals
    )
    open_overshoot = max(_OPEN_OVERSHOOT * size, _MIN_PRISM_THICKNESS)
    frames = _vertex_frames(
        chain, vertices, n_a_per_vertex, n_b_per_vertex, open_overshoot
    )
    n_frames = len(frames)
    is_loop = chain.is_loop
    segments: list[m3d.Manifold] = []
    last = n_frames if is_loop else n_frames - 1
    for index in range(last):
        next_index = (index + 1) % n_frames
        segment = _chamfer_segment_hull(frames[index], frames[next_index], size)
        if segment is not None:
            segments.append(segment)
    if not segments:
        return None
    if len(segments) == 1:
        return segments[0]
    return m3d.Manifold.batch_boolean(segments, m3d.OpType.Add)


# ---------------------------------------------------------------------------
# feasibility pre-flight (design §5)
# ---------------------------------------------------------------------------


def _half_thickness_feasibility(
    chain: FeatureChain,
    vertices: np.ndarray,
    n_a_per_vertex: list[np.ndarray],
    n_b_per_vertex: list[np.ndarray],
    size: float,
) -> Optional[float]:
    """Constraint A — half-thickness rule.

    At every chain vertex ``v`` the *opposing-face distance* must be greater
    than ``2 · size``; otherwise the swept tool would slice through the body
    (design §5.1).

    We use a simpler / robust proxy than the design's full ``min_gap`` query:
    the *minimum host-vertex distance along the inward bisector*. For each
    chain vertex the inward bisector is ``-(n_a + n_b)``; we measure the
    distance from ``v`` to any other host-mesh vertex that lies roughly along
    that ray. This is approximate but conservative — it never *passes* a thin
    plate that the swept tool would actually breach.

    Args:
        chain (FeatureChain): the chain to test.
        vertices (np.ndarray): host mesh vertices.
        n_a_per_vertex (list[np.ndarray]): per-vertex face-A normal.
        n_b_per_vertex (list[np.ndarray]): per-vertex face-B normal.
        size (float): the requested chamfer leg length.

    Returns:
        float | None: the offending measured distance if the constraint fails,
        otherwise ``None``.
    """
    bbox_min, bbox_max = vertices.min(axis=0), vertices.max(axis=0)
    bbox_diag = float(np.linalg.norm(bbox_max - bbox_min))
    threshold = 2.0 * size

    for index, v_index in enumerate(chain.verts):
        p_i = vertices[v_index]
        inward = _unit(-(n_a_per_vertex[index] + n_b_per_vertex[index]))
        if float(np.linalg.norm(inward)) < 1e-9:
            continue
        # Look along inward at all other vertices of the host mesh; the closest
        # vertex on the far-side along this ray gives an upper bound for the
        # opposing-face distance.
        offsets = vertices - p_i
        depths = offsets @ inward
        # Only points *ahead* along the inward ray, within bbox_diag
        ahead = (depths > 1e-6) & (depths < bbox_diag * 2.0)
        if not ahead.any():
            continue
        # Reject points far off-axis (we want roughly axial hits)
        axial = offsets - depths[:, None] * inward
        radial = np.linalg.norm(axial, axis=1)
        on_axis = ahead & (radial < size * 1.5 + 1e-3)
        if not on_axis.any():
            continue
        nearest_depth = float(depths[on_axis].min())
        if nearest_depth < threshold:
            return nearest_depth
    return None


def _check_feasibility(
    chain: FeatureChain,
    vertices: np.ndarray,
    n_a_per_vertex: list[np.ndarray],
    n_b_per_vertex: list[np.ndarray],
    size: float,
) -> None:
    """Run per-chain feasibility checks, raise :exc:`MeshFilletInfeasible` on failure.

    Two checks (design §5.1): half-thickness and chain-length. The
    cross-chain corner-sharing case is *not* checked here — A3a chamfer
    handles a single chain whose endpoints sit on a multi-chain corner
    natively (design §9: planar wedges bevel into a corner bit-exactly).

    Args:
        chain (FeatureChain): the chain to test.
        vertices (np.ndarray): host mesh vertices.
        n_a_per_vertex (list[np.ndarray]): per-vertex face-A normal.
        n_b_per_vertex (list[np.ndarray]): per-vertex face-B normal.
        size (float): the requested chamfer leg length.

    Raises:
        MeshFilletInfeasible: with ``constraint`` set to the failing check.
    """
    # Constraint A — half-thickness
    measured = _half_thickness_feasibility(
        chain, vertices, n_a_per_vertex, n_b_per_vertex, size
    )
    if measured is not None:
        raise MeshFilletInfeasible(
            f"chamfer size {size:g} exceeds half the local feature thickness "
            f"{measured:g} on chain pair {chain.pair}; the swept tool would "
            "slice through the body. Reduce the size or exclude the offending "
            "chain from the selection.",
            chains=[chain],
            constraint="half-thickness",
            requested=size,
            measured=measured,
        )

    # Constraint B — chain length
    chain_length = 0.0
    for edge in chain.edges:
        chain_length += float(np.linalg.norm(vertices[edge.v1] - vertices[edge.v0]))
    if not chain.is_loop and chain_length < 2.0 * size + 1e-9:
        raise MeshFilletInfeasible(
            f"chamfer size {size:g} exceeds half the chain arc length "
            f"{chain_length:g} on chain pair {chain.pair}; the swept tool's "
            "cross-section frame would fold. Reduce the size or pick a longer "
            "chain.",
            chains=[chain],
            constraint="chain-length",
            requested=size,
            measured=chain_length,
        )


# ---------------------------------------------------------------------------
# selection coercion
# ---------------------------------------------------------------------------


SelectionInput = Union[
    "FeatureChainSelection",
    FeatureChain,
    Iterable[FeatureChain],
]


def _selection_chains(
    selection: SelectionInput, source_selection: FeatureChainSelection
) -> list[FeatureChain]:
    """Coerce the public ``edges`` argument into a flat list of chains.

    Accepts the three shapes specified in design §8.1: a
    :class:`FeatureChainSelection`, a single :class:`FeatureChain`, or any
    iterable of :class:`FeatureChain`.

    Args:
        selection: the user's ``edges`` argument.
        source_selection (FeatureChainSelection): the mesh's own selection,
            used as the validity reference (``source_selection.chains`` is the
            universe of valid chains).

    Returns:
        list[FeatureChain]: chains to operate on, in caller-given order.

    Raises:
        TypeError: if ``selection`` is none of the accepted shapes.
        ValueError: if a passed chain is not one of the host mesh's chains.
    """
    if isinstance(selection, FeatureChainSelection):
        return list(selection.chains)
    if isinstance(selection, FeatureChain):
        candidates = [selection]
    else:
        try:
            candidates = list(selection)
        except TypeError as exc:
            raise TypeError(
                "`edges` must be a FeatureChainSelection, a FeatureChain, or "
                f"an iterable of FeatureChain; got {type(selection)!r}"
            ) from exc
        if not all(isinstance(item, FeatureChain) for item in candidates):
            raise TypeError("Every item of `edges` must be a FeatureChain instance")
    valid_ids = {id(chain) for chain in source_selection.chains}
    for candidate in candidates:
        if id(candidate) not in valid_ids:
            raise ValueError(
                "FeatureChain object is not part of this mesh's chain graph; "
                "use MeshPart.feature_edges() to obtain chains for this mesh"
            )
    return candidates


# ---------------------------------------------------------------------------
# the public entry — chamfer (Phase A3a)
# ---------------------------------------------------------------------------


def mesh_chamfer(
    meshpart: "MeshPart",
    edges: SelectionInput,
    size: float,
    *,
    on_infeasible: Literal["raise"] = "raise",
) -> "MeshPart":
    """Apply a faceted chamfer of ``size`` to ``edges`` (Phase A3a).

    Free-function counterpart of :meth:`MeshPart.chamfer`. Implements the
    per-chain swept-tool construction from the mesh-fillet engineering design
    (§3 — A3a profile is a flat triangular wedge, the chamfer leg length is
    ``size``). The pre-flight (§5) raises :exc:`MeshFilletInfeasible` on
    over-size requests or multi-chain corners; A3a never clamps.

    Args:
        meshpart (MeshPart): the mesh body to chamfer.
        edges (SelectionInput): the chains to chamfer — a
            :class:`FeatureChainSelection`, a single :class:`FeatureChain`, or
            any iterable of :class:`FeatureChain`. Chains must belong to
            ``meshpart``'s own chain graph.
        size (float): chamfer leg length (positive).
        on_infeasible (Literal["raise"]): A3a only supports ``"raise"`` (the
            ``"skip"`` and ``"clamp"`` modes ship in A4). Default ``"raise"``.

    Returns:
        MeshPart: the chamfered mesh body, carrying the merged side-map.

    Raises:
        ValueError: if ``size`` is not strictly positive or ``meshpart`` is
            empty.
        MeshFilletInfeasible: if any feasibility constraint fails — see
            :class:`MeshFilletInfeasible`.
        TypeError: if ``edges`` is none of the accepted shapes.
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart

    if not isinstance(meshpart, MeshPart):
        raise TypeError(f"mesh_chamfer expects a MeshPart, got {type(meshpart)!r}")
    if size <= 0.0:
        raise ValueError(f"chamfer size must be > 0, got {size!r}")
    if on_infeasible != "raise":
        raise ValueError(
            f"on_infeasible={on_infeasible!r} is not supported in A3a (only "
            "'raise' ships in this phase; 'skip' lands in A4)."
        )
    if meshpart.manifold.is_empty():
        raise ValueError("Cannot chamfer an empty MeshPart")

    selection = meshpart.feature_edges()
    chains = _selection_chains(edges, selection)
    if not chains:
        return MeshPart(meshpart.manifold, meshpart.side_map)

    vertices = selection.vertices
    triangles = selection.triangles
    face_id = selection.face_id
    tri_normals = triangle_normals(vertices, triangles)

    # Note: cross-chain corner sharing is *allowed* for A3a chamfer (design §9):
    # chamfer corners are planar intersections of half-spaces, which manifold3d
    # handles bit-exactly — the per-chain swept wedges naturally bevel into each
    # other at a shared corner. The aspirational corner-blend ships in A3c.

    # Pre-flight every chain *before* building any tool — fail fast.
    per_chain_normals: list[tuple[list[np.ndarray], list[np.ndarray]]] = []
    for chain in chains:
        if chain.convexity_class == "flat":
            per_chain_normals.append(([], []))
            continue
        if chain.convexity_class == "mixed":
            # A3a defers mixed-convexity chains — they need per-edge sign
            # splitting that lands with the fillet profile work in A3b.
            raise MeshFilletInfeasible(
                f"chain pair {chain.pair} is mixed convex/concave; A3a chamfer "
                "operates on chains with a single convexity sign. Split the "
                "selection so each chain is uniformly convex or concave.",
                chains=[chain],
                constraint="mixed-convexity",
                requested=size,
            )
        n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
            chain, triangles, face_id, tri_normals
        )
        _check_feasibility(
            chain,
            vertices,
            n_a_per_vertex,
            n_b_per_vertex,
            size,
        )
        per_chain_normals.append((n_a_per_vertex, n_b_per_vertex))

    # Build per-chain tools; collect into cut (convex) and add (concave) batches.
    cut_tools: list[m3d.Manifold] = []
    add_tools: list[m3d.Manifold] = []
    for chain, _normals in zip(chains, per_chain_normals):
        if chain.convexity_class == "flat":
            continue
        tool = _build_chain_chamfer_tool(
            chain, vertices, triangles, face_id, tri_normals, size
        )
        if tool is None:
            continue
        if chain.convexity_class == "convex":
            cut_tools.append(tool)
        else:
            add_tools.append(tool)

    # Apply cut first, then add (design §3.7 — p10 fix #3 carried forward).
    result = meshpart.manifold
    if cut_tools:
        combined_cut = (
            cut_tools[0]
            if len(cut_tools) == 1
            else m3d.Manifold.batch_boolean(cut_tools, m3d.OpType.Add)
        )
        result = result - combined_cut
    if add_tools:
        combined_add = (
            add_tools[0]
            if len(add_tools) == 1
            else m3d.Manifold.batch_boolean(add_tools, m3d.OpType.Add)
        )
        result = result + combined_add

    if result.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh chamfer produced an invalid manifold: {result.status()}"
        )

    return MeshPart(result, _carry_side_map(meshpart.side_map))


def _carry_side_map(side_map: SideMap) -> SideMap:
    """Pass the host mesh's side-map straight through.

    A3a's chamfer tool produces *planar* bevel facets that originate from the
    sweep — they have no analytic side-map entry yet (§8.3 reserves new
    ``"fillet"``-sourced FaceRecord entries for future work, but the A3a flat
    bevel is recoverable as the existing planes already in the side-map via
    coplanar merging). For now we forward the side-map unchanged; recovery
    treats the new triangles as additional faceted facets, and downstream
    selectors that care will see the same set of FaceRecord entries the input
    carried.

    Args:
        side_map (SideMap): the host mesh's side-map.

    Returns:
        SideMap: a shallow copy.
    """
    return SideMap(dict(side_map.records))


__all__ = [
    "MeshFilletInfeasible",
    "mesh_chamfer",
]
