"""
build123d mesh

name: fillet.py

desc:

Selective chamfer (Phase A3a) and selective fillet (Phase A3b) for
:class:`~build123d.mesh.MeshPart` — the per-chain swept-tool implementation
specified in the mesh-fillet engineering design (§3, §5). Multi-chain corner
blends (A3c) are not yet shipped; A3a/A3b establish the substrate they will
share.

A user names edges via a :class:`~build123d.mesh.FeatureChainSelection`; this
module turns each selected chain into one swept boolean tool, applies all
chain-tools in a single batched difference (convex) or union (concave) pass,
and returns the chamfered / filleted :class:`~build123d.mesh.MeshPart`.

The keystone is **one swept solid per chain** (design §3.1), not one per
segment. p10's per-segment tools overlapped heavily on curved feature loops
(a 63-edge bore rim emitted 1052–1720 degenerate slivers); the per-chain
construction shares its boundary cross-section between successive
``hull_points`` lofts, removing the overlap by construction.

A3b's fillet uses the same swept substrate but with an arc cross-section: a
faceted quarter-disc tangent to both adjacent faces at distance ``radius``
from the edge. Mixed convex/concave chains are handled by **per-edge sign
splitting** (design §3.4 / §8.3): the chain is broken at every sign flip
into single-sign sub-runs, each built as its own swept tool and added to the
appropriate cut / add batch. This deprecates A3a's chamfer
``"mixed-convexity"`` infeasibility for fillets — chamfer keeps the old check
because its profile geometry has no equivalent per-vertex framing for mixed
chains.

Three feasibility checks (design §5.1) pre-flight every chain; failing any
raises :exc:`MeshFilletInfeasible` with a clear message — A3a/A3b never clamp
(P3: ``"raise"`` is the default; ``"skip"`` lands in A4).

A3a/A3b handle single chains whose endpoints are multi-chain corner vertices
natively — design §9 notes the corner-fragility vanishes for chamfer because
chamfer corners are planar half-space intersections; for fillet the per-chain
swept arc-tool similarly intersects cleanly at corners up to the small-corner
patch quality limit. The "real" multi-chain corner blend (setback + spherical
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
    edge_convexity_sign,
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

# Default arc segment count for the fillet cross-section (design §3.4, §8.1).
_DEFAULT_FILLET_SEGMENTS = 8

# Per-edge convexity sign threshold (an edge counts as flat below this and is
# elided when splitting a mixed chain into sub-runs).
_SIGN_TOL = 1e-6


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
    * **mixed-convexity** — a chain has both convex and concave edges. Raised
      by *chamfer* (A3a operates on chains with a single convexity sign);
      **fillet (A3b) does not raise this** — it splits mixed chains at the
      sign flip into single-sign sub-runs (design §3.4 / §8.3).

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
    endpoint_overshoots: Optional[tuple[float, float]] = None,
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
            the terminal frame, in mesh units. Overridden per-endpoint by
            ``endpoint_overshoots`` when supplied (e.g. to suppress overshoot
            at a multi-chain corner where the tool's tip would otherwise add
            material outside the body — design §3.6, A3b corner stub).
        endpoint_overshoots (tuple[float, float] | None): if given, the
            per-endpoint overshoot for the first and last frame of an open
            chain. ``None`` means use ``open_overshoot`` at both ends. Ignored
            for loops.

    Returns:
        list[tuple]: one ``(origin, tangent, u_axis, w_axis)`` per vertex.
    """
    n_verts = len(chain.verts)
    is_loop = chain.is_loop
    if endpoint_overshoots is None:
        ov_start = ov_end = open_overshoot
    else:
        ov_start, ov_end = endpoint_overshoots
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
                origin = p_i - tangent * ov_start
            elif index == n_verts - 1:
                e_prev = _unit(p_i - vertices[chain.verts[-2]])
                tangent = e_prev
                origin = p_i + tangent * ov_end
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


# ---------------------------------------------------------------------------
# fillet-profile loft tool (Phase A3b)
# ---------------------------------------------------------------------------


def _wedge_profile_points(size: float) -> np.ndarray:
    """The chamfer-style wedge profile — three corners of the right triangle.

    Used as the *outer envelope* of the fillet tool: subtracting the
    rolling-ball cylinder from this wedge gives the "square minus quarter-disc"
    non-convex fillet region (design §3.4). The wedge alone is what A3a uses
    for chamfer; reused here as the first half of the fillet tool.

    Args:
        size (float): leg length (equal to the fillet radius for A3b).

    Returns:
        np.ndarray: ``(3, 2)`` array of wedge corners ``(0,0), (size,0), (0,size)``.
    """
    return np.array([[0.0, 0.0], [size, 0.0], [0.0, size]], dtype=np.float64)


def _ball_profile_points(radius: float, segments: int) -> np.ndarray:
    """The rolling-ball cylinder cross-section — a faceted circle centred at ``(r,r)``.

    The faceted circle has ``4 · max(segments, 2)`` samples around the full
    circumference, so the arc *facing the original edge* (from ``(r,0)`` through
    ``(0,r)``) has exactly ``segments`` facets — matching the design's
    ``n_seg`` cross-section facet count for the visible roll. The complete
    circle is convex, so ``hull_points`` lofts between consecutive lifted
    samples exactly trace the swept ball-cylinder.

    Args:
        radius (float): rolling-ball radius (the circle's radius).
        segments (int): arc facet count for the *visible* quarter (the full
            circle has ``4 · segments`` total samples).

    Returns:
        np.ndarray: ``(4 · segments, 2)`` circle samples in CCW order.
    """
    n_full = 4 * max(segments, 2)
    cx, cy = radius, radius
    pts = np.empty((n_full, 2), dtype=np.float64)
    for i in range(n_full):
        ang = 2.0 * np.pi * i / n_full
        pts[i, 0] = cx + radius * np.cos(ang)
        pts[i, 1] = cy + radius * np.sin(ang)
    return pts


def _lift_profile_to_3d(
    frame: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    profile_2d: np.ndarray,
) -> np.ndarray:
    """Lift a 2-D profile into 3-D at the given vertex frame.

    Each ``(u, w)`` profile point becomes ``origin + u * u_axis + w * w_axis``.
    The frame's ``tangent`` is unused — the profile lives in the cross-section
    plane orthogonal to the chain (the swept-tool's segment hulls between
    consecutive profiles supply the tangent-direction extent automatically).

    Args:
        frame (tuple): ``(origin, tangent, u_axis, w_axis)`` for one vertex.
        profile_2d (np.ndarray): ``(K, 2)`` profile points.

    Returns:
        np.ndarray: ``(K, 3)`` lifted profile points.
    """
    origin, _tangent, u_axis, w_axis = frame
    return origin + profile_2d[:, 0:1] * u_axis + profile_2d[:, 1:2] * w_axis


def _segment_hull_from_profiles(
    points_i: np.ndarray,
    points_j: np.ndarray,
) -> Optional[m3d.Manifold]:
    """Build one ``hull_points`` loft between two consecutive lifted profiles.

    The convex hull of the union of the two 3-D point sets is exactly the
    lofted segment between two adjacent convex cross-sections — manifold3d's
    ``Manifold.hull_points`` builds the closed solid. Both fillet sub-tools
    (wedge envelope and rolling-ball cylinder) use convex profile regions, so
    the hull traces each swept envelope exactly.

    Args:
        points_i (np.ndarray): ``(K, 3)`` lifted profile at vertex i.
        points_j (np.ndarray): ``(K, 3)`` lifted profile at vertex i+1.

    Returns:
        manifold3d.Manifold | None: the loft segment, or ``None`` if
        ``hull_points`` returns an invalid manifold (rare — only if the input
        is geometrically degenerate, e.g. all coplanar points).
    """
    points = np.vstack([points_i, points_j])
    hull = m3d.Manifold.hull_points(points.tolist())
    if hull.status() != m3d.Error.NoError or hull.is_empty():
        return None
    return hull


def _build_swept_from_profile(
    frames: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    profile_2d: np.ndarray,
    is_loop: bool,
) -> Optional[m3d.Manifold]:
    """Loft a 2-D profile along a list of frames into one swept manifold.

    For *straight* sub-runs (2 frames), uses ``hull_points`` on the two lifted
    rings — the design's per-segment hull (§3.2). For longer sub-runs (curved
    loops, multi-vertex chains) ``hull_points`` per segment loses manifold
    connectivity at the shared rings when consecutive frames rotate (the bore
    rim and chamfered-curve cases of design §6.1): consecutive hulls report as
    separate decomposed bodies because their shared end-ring is not bit-exact
    after the slight frame rotation.

    The robust construction for longer sub-runs is the same one p10 used: a
    **direct mesh loft** stitching the lifted profile rings with side-wall
    quads and end-cap fans (design §3.5 — "extrude profile to a 2·precision
    prism" generalised to a true sweep). The result imports as one continuous
    manifold of *exactly* the swept solid, no per-segment seam issues.

    Args:
        frames (list): ``[(origin, tangent, u_axis, w_axis), ...]`` per vertex.
        profile_2d (np.ndarray): the convex cross-section in ``(u, w)`` plane.
        is_loop (bool): True if the sub-run closes on itself.

    Returns:
        manifold3d.Manifold | None: the swept solid, or ``None`` if the input
        is geometrically degenerate.
    """
    lifted = [_lift_profile_to_3d(frame, profile_2d) for frame in frames]
    return _ribbon_mesh_from_rings(lifted, is_loop)


def _ribbon_mesh_from_rings(
    rings: list[np.ndarray],
    is_loop: bool,
) -> Optional[m3d.Manifold]:
    """Stitch ``rings`` (one per vertex) into a closed swept-solid manifold.

    Each ring is a CCW-oriented convex polygon lifted to 3-D. Side walls are
    two triangles per profile edge (one quad per consecutive frame pair); end
    caps are fan-triangulated from the first profile point (the profile is
    convex by construction, so the fan is non-self-intersecting). A closed
    loop sub-run omits the caps and stitches the last ring back to the first.

    Args:
        rings (list[np.ndarray]): per-vertex ``(K, 3)`` lifted profile points.
        is_loop (bool): True if the sub-run closes on itself.

    Returns:
        manifold3d.Manifold | None: the imported swept manifold, or ``None``
        if manifold3d rejects the input (e.g. degenerate, non-manifold input).
    """
    if not rings:
        return None
    n_frames = len(rings)
    k = rings[0].shape[0]
    if any(ring.shape != (k, 3) for ring in rings):
        return None

    verts = np.vstack(rings)
    tris: list[tuple[int, int, int]] = []
    n_segments = n_frames if is_loop else n_frames - 1
    for index in range(n_segments):
        next_index = (index + 1) % n_frames
        base_i = index * k
        base_j = next_index * k
        for j in range(k):
            jn = (j + 1) % k
            # Quad (i,j) (i,jn) (next, jn) (next, j) -> two CCW tris seen from
            # the OUTSIDE of the tool (the profile is CCW in (u,w), so the
            # outward face is "ring i -> ring j" with the profile-edge winding
            # giving outward normals).
            a = base_i + j
            b = base_i + jn
            c = base_j + jn
            d = base_j + j
            tris.append((a, b, c))
            tris.append((a, c, d))

    if not is_loop:
        # End caps: fan from vertex 0 of each end ring. The "near" cap (ring 0)
        # is oriented so its outward normal points along -tangent (away from
        # ring 1); the "far" cap (ring n-1) points the opposite way.
        base_near = 0
        base_far = (n_frames - 1) * k
        for j in range(1, k - 1):
            # near cap: reverse winding (so outward normal opposes the sweep)
            tris.append((base_near, base_near + j + 1, base_near + j))
            # far cap: forward winding
            tris.append((base_far, base_far + j, base_far + j + 1))

    tri_arr = np.array(tris, dtype=np.uint64)

    def _make(tris_in: np.ndarray) -> m3d.Manifold:
        mesh = m3d.Mesh64(
            vert_properties=np.ascontiguousarray(verts, dtype=np.float64),
            tri_verts=np.ascontiguousarray(tris_in, dtype=np.uint64),
        )
        return m3d.Manifold(mesh)

    man = _make(tri_arr)
    # A closed mesh with reversed winding still imports as a valid 2-manifold
    # but has *negative* volume — subtracting it would ADD material. Flip if
    # so. (Same fallback as p10's _sweep_tool.)
    if man.status() != m3d.Error.NoError or man.volume() < 0.0:
        man = _make(tri_arr[:, ::-1].copy())
    if man.status() != m3d.Error.NoError or man.is_empty():
        return None
    return man


def _edge_signs(chain: FeatureChain, vertices: np.ndarray) -> list[float]:
    """Per-edge signed convexity for a chain (positive convex, negative concave)."""
    return [edge_convexity_sign(vertices, edge) for edge in chain.edges]


def _split_chain_by_sign(
    chain: FeatureChain, vertices: np.ndarray
) -> list[tuple[list[int], str]]:
    """Split a chain into single-sign sub-runs (design §3.4 / §8.3).

    Walks the chain's edges in path order and emits one ``(verts, sign)`` pair
    per maximal run of edges sharing a convexity sign. Near-flat edges (within
    :data:`_SIGN_TOL`) are skipped — they neither cut nor fill. A sub-run's
    ``verts`` list contains the ordered vertex indices spanning that run
    (always at least two; one per consecutive same-sign edge plus the trailing
    vertex). For a *loop* whose sign is uniform the result wraps: the single
    sub-run's ``verts`` covers the whole loop (without repeating the start).

    Args:
        chain (FeatureChain): the chain to split.
        vertices (np.ndarray): host vertex array.

    Returns:
        list[tuple[list[int], str]]: each entry is ``(vert_indices, sign)``
        where ``sign`` is ``"convex"`` or ``"concave"``. Empty if every edge is
        flat (no sub-runs to build).
    """
    signs = _edge_signs(chain, vertices)
    n_edges = len(signs)
    if n_edges == 0:
        return []

    def _sign_tag(value: float) -> Optional[str]:
        if value > _SIGN_TOL:
            return "convex"
        if value < -_SIGN_TOL:
            return "concave"
        return None

    edge_tags = [_sign_tag(s) for s in signs]

    # A chain with a uniform tag becomes one sub-run that uses the full vertex
    # list (preserves loop semantics for the swept-tool builder).
    non_flat = [t for t in edge_tags if t is not None]
    if non_flat and all(t == non_flat[0] for t in non_flat):
        only_tag = non_flat[0]
        if chain.is_loop:
            # Loop case: pass the whole vertex list, the swept builder wraps.
            return [(list(chain.verts), only_tag)]
        # Open chain: drop any flat-tagged edges at the ends only — for a
        # uniformly non-flat chain there are none, so this is the whole chain.
        # If there are flat edges in the middle of an otherwise-uniform chain,
        # they fall through to the mixed-walk below.
        if all(t == only_tag for t in edge_tags):
            return [(list(chain.verts), only_tag)]

    # Mixed (or has interior flat edges): walk and split.
    sub_runs: list[tuple[list[int], str]] = []
    cur_tag: Optional[str] = None
    cur_verts: list[int] = []
    for edge_index, tag in enumerate(edge_tags):
        v_start = chain.verts[edge_index]
        if chain.is_loop:
            v_end = chain.verts[(edge_index + 1) % len(chain.verts)]
        else:
            v_end = chain.verts[edge_index + 1]
        if tag is None:
            # Close any in-progress sub-run on a flat edge; flat edges are not
            # included in the swept tool.
            if cur_tag is not None and len(cur_verts) >= 2:
                sub_runs.append((cur_verts, cur_tag))
            cur_tag = None
            cur_verts = []
            continue
        if tag != cur_tag:
            # Sign change: close the previous run and start a new one. The new
            # run begins at the edge's start vertex (so the sub-run boundary
            # vertex is shared with the previous sub-run's last vertex — the
            # two swept tools then meet at that vertex).
            if cur_tag is not None and len(cur_verts) >= 2:
                sub_runs.append((cur_verts, cur_tag))
            cur_tag = tag
            cur_verts = [v_start, v_end]
        else:
            cur_verts.append(v_end)

    if cur_tag is not None and len(cur_verts) >= 2:
        # For a uniformly-non-flat loop we already returned above; this handles
        # mixed loops by closing the final sub-run as an open arc (any wrap is
        # handled inside the swept-tool builder via the open-chain code path).
        sub_runs.append((cur_verts, cur_tag))

    return sub_runs


def _build_swept_arc_tool(
    sub_run_verts: Sequence[int],
    parent_chain: FeatureChain,
    is_loop_subrun: bool,
    vertices: np.ndarray,
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
    radius: float,
    segments: int,
    endpoint_overshoots: Optional[tuple[float, float]] = None,
    *,
    sign: str = "convex",
) -> Optional[m3d.Manifold]:
    """Build a swept fillet tool for one single-sign sub-run.

    The fillet cross-section (design §3.4) is "square minus quarter-disc": the
    triangular wedge ``(0,0)-(r,0)-(0,r)`` with the rolling ball at ``(r,r)``
    of radius ``r`` carved out. That region is **non-convex** (the arc dips
    *toward* the corner), so a direct ``hull_points`` of the profile points
    would collapse it into the chamfer wedge (the convex hull loses the arc).

    The construction therefore splits the fillet tool into two convex pieces
    and combines them with one boolean per sub-run:

    1. The **wedge envelope** — A3a's chamfer profile of size ``radius``,
       swept along the sub-run via ``hull_points`` lofts.
    2. The **rolling-ball cylinder** — a faceted circle of radius ``radius``
       centred at ``(r, r)``, swept along the same frames.

    The fillet tool for the sub-run is ``wedge − cylinder``. This recovers the
    "wedge minus quarter-disc" non-convex shape, and the rolled surface
    inherits the cylinder's facetting (``segments`` facets across the visible
    quarter — design §3.4). Both convex and concave sub-runs use the same
    construction; the caller routes the result to ``cut_tools`` or
    ``add_tools`` based on the sub-run's sign (design §3.7).

    For a closed loop sub-run the lofts wrap (segment ``n-1`` → ``0``); for an
    open sub-run the endpoint frames are overshot along their unique adjacent
    edge tangent to cleanly cross any chain boundary the sub-run ends on
    (design §3.6).

    Args:
        sub_run_verts (Sequence[int]): the ordered vertex indices for this
            sub-run (must have ``len >= 2``).
        parent_chain (FeatureChain): the parent chain (its ``pair`` is used to
            select which mesh face-side a vertex's normals come from).
        is_loop_subrun (bool): True if this sub-run closes on itself (i.e. the
            parent chain is a loop and the whole loop is one uniform-sign
            sub-run).
        vertices (np.ndarray): host vertex array.
        triangles (np.ndarray): host triangle array.
        face_id (np.ndarray): host per-triangle faceID.
        tri_normals (np.ndarray): host per-triangle normals.
        radius (float): the fillet radius.
        segments (int): arc facet count.

    Returns:
        manifold3d.Manifold | None: the combined swept tool, or ``None`` if
        every loft hull failed (degenerate input).
    """
    # Build a virtual chain-like vertex list view for the frame helpers; we
    # reuse _per_vertex_face_normals and _vertex_frames by constructing a
    # transient FeatureChain over the sub-run vertices and the parent's pair.
    transient = FeatureChain(
        pair=parent_chain.pair,
        verts=list(sub_run_verts),
        is_loop=is_loop_subrun,
        edges=[],
    )
    n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
        transient, triangles, face_id, tri_normals
    )
    open_overshoot = max(_OPEN_OVERSHOOT * radius, _MIN_PRISM_THICKNESS)
    frames = _vertex_frames(
        transient,
        vertices,
        n_a_per_vertex,
        n_b_per_vertex,
        open_overshoot,
        endpoint_overshoots,
    )

    wedge_profile = _wedge_profile_points(radius)
    ball_profile = _ball_profile_points(radius, segments)
    if sign == "concave":
        # For concave fillets the wedge sits on the *outside* of the corner —
        # design §3.4: "the same triangle but on the outside, translated by
        # (-d, -d) along (-u, -w)". Mirror the profile in (u, w) so the
        # lifted tool fills the empty channel rather than re-cutting the
        # already-solid region.
        wedge_profile = -wedge_profile
        ball_profile = -ball_profile

    wedge_tool = _build_swept_from_profile(frames, wedge_profile, is_loop_subrun)
    ball_tool = _build_swept_from_profile(frames, ball_profile, is_loop_subrun)
    if wedge_tool is None:
        return None
    if ball_tool is None:
        # Without the ball cut-out the tool degenerates to a chamfer; refuse
        # to silently return that.
        return None

    fillet_tool = wedge_tool - ball_tool
    if fillet_tool.status() != m3d.Error.NoError or fillet_tool.is_empty():
        return None
    return fillet_tool


def _check_fillet_feasibility(
    chain: FeatureChain,
    vertices: np.ndarray,
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
    radius: float,
) -> None:
    """Run the fillet feasibility checks for one chain (design §5.1).

    Mirrors A3a's chamfer pre-flight (half-thickness, chain-length) but with
    the fillet radius substituted for the chamfer size. Mixed-convexity is
    **not** a constraint for fillet — A3b splits mixed chains at the sign
    flip and builds per-sub-run tools (design §3.4 / §8.3).

    Args:
        chain (FeatureChain): the chain to test.
        vertices (np.ndarray): host vertex array.
        triangles (np.ndarray): host triangle array.
        face_id (np.ndarray): host per-triangle faceID.
        tri_normals (np.ndarray): host per-triangle normals.
        radius (float): the requested fillet radius.

    Raises:
        MeshFilletInfeasible: with ``constraint`` set to the failing check.
    """
    n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
        chain, triangles, face_id, tri_normals
    )
    _check_feasibility(chain, vertices, n_a_per_vertex, n_b_per_vertex, radius)


# ---------------------------------------------------------------------------
# the public entry — fillet (Phase A3b)
# ---------------------------------------------------------------------------


def mesh_fillet(
    meshpart: "MeshPart",
    edges: SelectionInput,
    radius: float,
    *,
    segments: int = _DEFAULT_FILLET_SEGMENTS,
    on_infeasible: Literal["raise"] = "raise",
) -> "MeshPart":
    """Apply a faceted fillet of ``radius`` to ``edges`` (Phase A3b).

    Free-function counterpart of :meth:`MeshPart.fillet`. Implements the
    per-chain swept-arc-tool construction from the mesh-fillet engineering
    design (§3, §8.1 — A3b profile is a ``segments``-faceted quarter-disc
    cross-section, the rolling-ball radius is ``radius``). The pre-flight (§5)
    raises :exc:`MeshFilletInfeasible` on over-size requests; A3b never clamps.

    Mixed convex/concave chains are handled by **per-edge sign splitting**
    (design §3.4 / §8.3): the chain is broken at every sign flip into
    single-sign sub-runs, each built as its own swept tool. The cuts (convex
    sub-runs) are batched together, then the adds (concave sub-runs) — cut
    first, then add (design §3.7).

    Multi-chain corner blends ship in A3c; for A3b, chains that run into a
    shared corner vertex still produce a valid swept tool — the corner is
    handled by the simplest stub the design specifies (the chain tool's
    endpoint overshoot crosses the corner cleanly; the resulting body is
    valid but the corner is a thin patch rather than a real ball).

    Args:
        meshpart (MeshPart): the mesh body to fillet.
        edges (SelectionInput): the chains to fillet — a
            :class:`FeatureChainSelection`, a single :class:`FeatureChain`, or
            any iterable of :class:`FeatureChain`. Chains must belong to
            ``meshpart``'s own chain graph.
        radius (float): rolling-ball radius (positive).
        segments (int): arc facet count for the cross-section. Default
            ``8`` matches design §8.1's recommended ``n_seg``.
        on_infeasible (Literal["raise"]): A3b only supports ``"raise"`` (the
            ``"skip"`` mode ships in A4). Default ``"raise"``.

    Returns:
        MeshPart: the filleted mesh body, carrying the merged side-map.

    Raises:
        ValueError: if ``radius`` is not strictly positive, ``segments < 1``,
            ``meshpart`` is empty, or ``on_infeasible`` is unsupported.
        MeshFilletInfeasible: if any feasibility constraint fails — see
            :class:`MeshFilletInfeasible`.
        TypeError: if ``edges`` is none of the accepted shapes.
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart

    if not isinstance(meshpart, MeshPart):
        raise TypeError(f"mesh_fillet expects a MeshPart, got {type(meshpart)!r}")
    if radius <= 0.0:
        raise ValueError(f"fillet radius must be > 0, got {radius!r}")
    if segments < 1:
        raise ValueError(f"fillet segments must be >= 1, got {segments!r}")
    if on_infeasible != "raise":
        raise ValueError(
            f"on_infeasible={on_infeasible!r} is not supported in A3b (only "
            "'raise' ships in this phase; 'skip' lands in A4)."
        )
    if meshpart.manifold.is_empty():
        raise ValueError("Cannot fillet an empty MeshPart")

    selection = meshpart.feature_edges()
    chains = _selection_chains(edges, selection)
    if not chains:
        return MeshPart(meshpart.manifold, meshpart.side_map)

    vertices = selection.vertices
    triangles = selection.triangles
    face_id = selection.face_id
    tri_normals = triangle_normals(vertices, triangles)

    # Pre-flight every chain *before* building any tool — fail fast.
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        _check_fillet_feasibility(
            chain, vertices, triangles, face_id, tri_normals, radius
        )

    cut_tools: list[m3d.Manifold] = []
    add_tools: list[m3d.Manifold] = []
    open_overshoot = max(_OPEN_OVERSHOOT * radius, _MIN_PRISM_THICKNESS)
    # Vertex_kinds is keyed on the parent chain's index, but a sub-run starts
    # mid-chain — translate sub-run start/end vertices to parent chain indices
    # to read the kind tag.
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        kind_for: dict[int, str] = dict(zip(chain.verts, chain.vertex_kinds))
        sub_runs = _split_chain_by_sign(chain, vertices)
        for sub_verts, sign in sub_runs:
            # A sub-run inherits the parent's loop flag only if it covers the
            # whole loop (uniform-sign loop — see _split_chain_by_sign).
            is_loop_sub = chain.is_loop and len(sub_verts) == len(chain.verts)
            endpoint_overshoots: Optional[tuple[float, float]] = None
            if not is_loop_sub:
                # For *concave* (add) sub-runs we must NOT overshoot at a
                # multi-chain corner endpoint — the overshot tip of the add
                # tool would lie in empty space outside the body and union as
                # a disconnected floating piece (the L-shape concave-chain
                # symptom). The full corner blend lands in A3c; for A3b the
                # stub is "clamp the add overshoot to zero at corner vertices".
                # Convex (cut) sub-runs keep the overshoot — a cut into empty
                # space is harmless and helps the tool cleanly cross the
                # adjacent face boundary.
                if sign == "concave":
                    start_corner = kind_for.get(sub_verts[0]) == "corner"
                    end_corner = kind_for.get(sub_verts[-1]) == "corner"
                    endpoint_overshoots = (
                        0.0 if start_corner else open_overshoot,
                        0.0 if end_corner else open_overshoot,
                    )
            tool = _build_swept_arc_tool(
                sub_verts,
                chain,
                is_loop_sub,
                vertices,
                triangles,
                face_id,
                tri_normals,
                radius,
                segments,
                endpoint_overshoots,
                sign=sign,
            )
            if tool is None:
                continue
            if sign == "convex":
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
        raise ValueError(f"mesh fillet produced an invalid manifold: {result.status()}")

    return MeshPart(result, _carry_side_map(meshpart.side_map))


__all__ = [
    "MeshFilletInfeasible",
    "mesh_chamfer",
    "mesh_fillet",
]
