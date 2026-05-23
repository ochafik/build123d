"""
build123d mesh

name: fillet.py

desc:

Selective chamfer / fillet for :class:`~build123d.mesh.MeshPart` — the
per-chain swept-tool implementation specified in the mesh-fillet engineering
design (§3, §5). Phases A3a/A3b/A3c established the substrate, single-chain
arc tool, and the setback + vertex-patch corner blend; A4 (this revision)
adds variable radius along a chain and ``on_infeasible="skip"`` mode.

A user names edges via a :class:`~build123d.mesh.FeatureChainSelection`; this
module turns each selected chain into one swept boolean tool, applies all
chain-tools in a single batched difference (convex) or union (concave) pass,
and returns the chamfered / filleted :class:`~build123d.mesh.MeshPart`.

The keystone is **one swept solid per chain** (design §3.1), not one per
segment. p10's per-segment tools overlapped heavily on curved feature loops
(a 63-edge bore rim emitted 1052–1720 degenerate slivers); the per-chain
construction shares its boundary cross-section between successive
``hull_points`` lofts, removing the overlap by construction.

The fillet uses the same swept substrate as the chamfer but with an arc
cross-section: a faceted quarter-disc tangent to both adjacent faces at
distance ``radius`` from the edge. Mixed convex/concave chains are handled
by **per-edge sign splitting** (design §3.4 / §8.3): the chain is broken at
every sign flip into single-sign sub-runs, each built as its own swept tool
and added to the appropriate cut / add batch. This deprecates A3a's chamfer
``"mixed-convexity"`` infeasibility for fillets — chamfer keeps the old check
because its profile geometry has no equivalent per-vertex framing for mixed
chains.

**A4 — variable radius along a chain.** ``radius`` / ``size`` now accept
either a scalar ``float`` (uniform — the A3 behaviour, bit-identical) **or**
a callable ``(chain, vertex_index) -> float`` evaluated at every chain
vertex. The per-vertex profile is generated at that vertex's local radius;
the ribbon-mesh loft (:func:`_ribbon_mesh_from_rings`) naturally interpolates
between adjacent rings of different sizes. The feasibility pre-flight is
evaluated per vertex using each vertex's local radius (the worst case is
also the strictest case, since the half-thickness threshold scales with the
size). Sequences ``Sequence[float]`` indexed per chain are *not* a separate
shape — the simpler callable form covers every case the design's §2.4
mentions, and forces the caller to write a one-liner that names *what is
varying* explicitly.

**A4 — ``on_infeasible="skip"``.** Default stays ``"raise"`` (P3 — never
silently mis-answer). ``"skip"`` drops the offending chain (per
:class:`MeshFilletInfeasible`) or corner from the operation and continues;
the dropped items are recorded on a :class:`FilletReport` attached to the
returned :class:`~build123d.mesh.MeshPart` (``mp.last_fillet_report``,
design §8.2). A skip is also logged at WARNING level — never silent in P3's
sense. ``"clamp"`` is **not** shipped (design §5.4 forbids it in A3 / A4).

Feasibility checks (design §5.1) pre-flight every chain; failing any
raises :exc:`MeshFilletInfeasible` (default) or appends a
:class:`SkippedItem` to the :class:`FilletReport` (skip mode).

A3a/A3b handle single chains whose endpoints are multi-chain corner vertices
natively — design §9 notes the corner-fragility vanishes for chamfer because
chamfer corners are planar half-space intersections; for fillet the per-chain
swept arc-tool similarly intersects cleanly at corners up to the small-corner
patch quality limit. A3c's "real" multi-chain corner blend (setback +
spherical patch) is integrated through :func:`build_corner_fillet_patch` /
:func:`build_corner_chamfer_patch`.

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

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Callable,
    Iterable,
    Literal,
    Optional,
    Sequence,
    Union,
)

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from .bridge import SideMap
from .corners import (
    Corner,
    MAX_CORNER_CHAINS,
    build_corner_chamfer_patch,
    build_corner_fillet_patch,
    detect_corners,
    per_chain_setback,
)
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

# Skip-mode WARNING logger (design §8.2 — a skip is never completely silent).
_LOG = logging.getLogger("build123d.mesh.fillet")


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
# A4 — variable radius + skip-mode reporting
# ---------------------------------------------------------------------------


#: A per-vertex radius/size callable. ``chain`` is the parent
#: :class:`FeatureChain` and ``vertex_index`` is the position into
#: ``chain.verts`` (NOT the host-mesh vertex index — the caller usually wants a
#: parameter along the chain, which is what the positional index is). The
#: callable must return a strictly positive float at every vertex of every
#: selected chain. Returning ``0`` or a negative value raises
#: :class:`ValueError` from the dispatcher.
RadiusFunc = Callable[["FeatureChain", int], float]


@dataclass
class SkippedItem:
    """One chain or corner dropped by ``on_infeasible="skip"``.

    Records the reason every dropped item was infeasible so the user can react
    programmatically (e.g. log the drop, adjust the radius and re-run on the
    skipped subset, or raise to a caller). Carries the same structured payload
    as a :class:`MeshFilletInfeasible` would have.

    Attributes:
        chains (list[FeatureChain]): the chains that were dropped — either a
            single chain (chain-level failure: half-thickness, chain-length,
            mixed-convexity) or every chain incident to a dropped corner
            (corner-level failure: mixed-corner, k>6-corner).
        constraint (str): one of ``"half-thickness"``, ``"chain-length"``,
            ``"mixed-convexity"``, ``"mixed-corner"``, ``"k>6-corner"``, or
            ``"degenerate-tool"`` (the tool builder returned ``None``).
        requested (float): the requested radius / size (at the failing vertex
            for variable-radius inputs, the input scalar otherwise).
        measured (float): the measured feature size (0.0 when the constraint
            has no numeric measurement — mixed-corner, mixed-convexity,
            k>6-corner, degenerate-tool).
        vertex (int | None): for a chain-level half-thickness failure under
            variable radius, the offending host-mesh vertex index; ``None``
            for constraints that do not pin a single vertex.
        message (str): human-readable diagnostic.
    """

    chains: list[FeatureChain]
    constraint: str
    requested: float
    measured: float = 0.0
    vertex: Optional[int] = None
    message: str = ""


@dataclass
class FilletReport:
    """The skip-mode return shape — every chain / corner dropped from the op.

    Attached to the returned :class:`~build123d.mesh.MeshPart` as
    :attr:`~build123d.mesh.MeshPart.last_fillet_report` whenever
    ``on_infeasible="skip"`` drops at least one item (design §8.2 / A4). The
    method form is the only return surface — the free-function variants
    (:func:`mesh_fillet` / :func:`mesh_chamfer`) also return a single
    :class:`~build123d.mesh.MeshPart` and attach the report there, to keep
    the two API surfaces interchangeable and the type stable.

    Attributes:
        operation (str): ``"fillet"`` or ``"chamfer"`` — which op produced the
            report.
        requested (float | None): the input scalar (``None`` when ``radius``
            / ``size`` was a callable — each :class:`SkippedItem` then names
            its own per-vertex measurement).
        skipped_chains (list[SkippedItem]): every chain dropped at the
            chain-level pre-flight.
        skipped_corners (list[SkippedItem]): every corner dropped at the
            corner-level pre-flight (mixed-corner, k>6-corner).

    The report is *empty-equivalent* (boolean ``False``) iff nothing was
    dropped; the method/free function then attaches ``None`` (not an empty
    report) so callers can compare to ``None`` without inspecting the lists.
    """

    operation: str
    requested: Optional[float] = None
    skipped_chains: list[SkippedItem] = field(default_factory=list)
    skipped_corners: list[SkippedItem] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True if anything was dropped — empty report is falsy."""
        return bool(self.skipped_chains) or bool(self.skipped_corners)

    @property
    def total_skipped(self) -> int:
        """The total count of dropped chains + dropped corners."""
        return len(self.skipped_chains) + len(self.skipped_corners)


# A user-facing input shape: scalar float or per-vertex callable. ``int`` is
# accepted (and immediately float-coerced) so ``radius=2`` works as expected.
RadiusInput = Union[float, int, RadiusFunc]


def _normalise_radius(
    value: RadiusInput,
    operation: str,
) -> tuple[Optional[float], RadiusFunc]:
    """Resolve a ``radius``/``size`` input into ``(scalar_or_none, per_vertex_fn)``.

    For a scalar input the returned ``scalar`` carries the float and the
    per-vertex function is a constant. For a callable input ``scalar`` is
    ``None`` (no single representative value) and the callable is wrapped to
    coerce its return to ``float`` and to validate positivity per call site.

    This keeps the call sites simple: every per-vertex profile query goes
    through the ``per_vertex_fn``, so the scalar path is a no-op specialisation
    of the variable path.

    Args:
        value: the ``radius`` / ``size`` argument from the public API.
        operation: ``"fillet"`` or ``"chamfer"`` — used only in the error
            message.

    Returns:
        tuple[float | None, RadiusFunc]: ``(scalar, per_vertex_fn)``. ``scalar``
        is the float input itself, or ``None`` when the input is a callable.

    Raises:
        ValueError: scalar input is ``<= 0``.
        TypeError: input is neither a scalar number nor a callable.
    """
    if callable(value):
        fn = value

        def _per_vertex(chain: "FeatureChain", index: int) -> float:
            return float(fn(chain, index))

        return None, _per_vertex
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        scalar = float(value)
        if scalar <= 0.0:
            raise ValueError(
                f"{operation} {('radius' if operation == 'fillet' else 'size')} "
                f"must be > 0, got {scalar!r}"
            )
        return scalar, lambda _chain, _index: scalar
    raise TypeError(
        f"{operation} {('radius' if operation == 'fillet' else 'size')} must "
        f"be a positive number or a callable (chain, vertex_index) -> float; "
        f"got {type(value)!r}"
    )


def _chain_radius_samples(
    chain: "FeatureChain",
    per_vertex_fn: RadiusFunc,
) -> np.ndarray:
    """Sample ``per_vertex_fn`` at every vertex of ``chain``; validate positivity.

    Args:
        chain: the chain being processed.
        per_vertex_fn: the resolved per-vertex callable from
            :func:`_normalise_radius`.

    Returns:
        np.ndarray: ``(len(chain.verts),)`` array of positive floats.

    Raises:
        ValueError: any sample is ``<= 0`` (the variable-radius callable
            returned a non-positive size at some vertex — refuse loudly, P3).
    """
    n_verts = len(chain.verts)
    samples = np.empty(n_verts, dtype=np.float64)
    for index in range(n_verts):
        value = float(per_vertex_fn(chain, index))
        if value <= 0.0:
            raise ValueError(
                f"per-vertex radius/size returned non-positive value "
                f"{value!r} at chain pair {chain.pair} vertex index {index} "
                f"(host vertex {chain.verts[index]}). Every vertex must "
                f"receive a strictly positive size."
            )
        samples[index] = value
    return samples


def _chain_max_size(samples: np.ndarray) -> float:
    """Return the maximum per-vertex size on a chain.

    Used wherever the existing scalar-radius code reasoned about overshoot /
    feasibility distance — the conservative choice is the chain's *largest*
    radius (the strictest feasibility threshold and the longest overshoot the
    swept tool needs).
    """
    return float(samples.max()) if samples.size else 0.0


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
    size_i: float,
    size_j: Optional[float] = None,
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
        size_i (float): chamfer leg length at vertex i.
        size_j (float | None): chamfer leg length at vertex i+1. ``None``
            means "same as ``size_i``" — the scalar / A3 path. Passing a
            distinct ``size_j`` lofts between two different cross-section
            sizes (A4 variable size).

    Returns:
        manifold3d.Manifold | None: the loft segment, or ``None`` if
        ``hull_points`` returns an empty / invalid manifold (rare —
        degenerate-thin frames can degenerate the hull).
    """
    o_i, _t_i, u_i, w_i = frame_i
    o_j, _t_j, u_j, w_j = frame_j
    s_j = size_i if size_j is None else size_j
    points = np.vstack(
        [
            o_i,
            o_i + u_i * size_i,
            o_i + w_i * size_i,
            o_j,
            o_j + u_j * s_j,
            o_j + w_j * s_j,
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
    sizes: np.ndarray,
    endpoint_overshoots: Optional[tuple[float, float]] = None,
) -> Optional[m3d.Manifold]:
    """Build the swept chamfer tool for one chain (a single manifold body).

    Implements design §3.2 (per-chain swept tool): per-vertex frames, then a
    ``batch_boolean`` union of consecutive ``hull_points`` lofts. For a closed
    loop the segments wrap (``n_loop`` segments); for an open chain there are
    ``n_loop - 1`` segments and the terminal frames are overshot per §3.6.

    A negative endpoint overshoot **sets the chain tool back** from the
    endpoint by that magnitude (design §4 — corner setback). The corner
    blend dispatcher uses this to terminate the chain tool on a "setback
    ring" some distance from the corner vertex, where the vertex patch
    (sphere / polyhedron) picks up.

    A4: ``sizes`` is a per-vertex ``(len(chain.verts),)`` array of chamfer
    leg lengths. A scalar input is broadcast to a constant array by the
    dispatcher; a variable input passes its per-vertex samples through. Each
    consecutive-pair hull is lofted between two different cross-section
    sizes ``(sizes[i], sizes[i+1])``.

    Args:
        chain (FeatureChain): the chain (already classified — non-flat).
        vertices (np.ndarray): host mesh ``(N, 3)`` vertex array.
        triangles (np.ndarray): host mesh ``(M, 3)`` triangle vertex indices.
        face_id (np.ndarray): host mesh ``(M,)`` seeded face id per triangle.
        tri_normals (np.ndarray): host mesh ``(M, 3)`` per-triangle normals.
        sizes (np.ndarray): per-vertex chamfer leg length, length
            ``len(chain.verts)``, every entry > 0.
        endpoint_overshoots (tuple[float, float] | None): per-endpoint
            overshoot for an open chain. A *negative* value setbacks the
            frame *into* the chain by that magnitude (the A3c corner setback
            recipe). ``None`` (default) keeps the standard open-chain
            overshoot.

    Returns:
        manifold3d.Manifold | None: the combined swept tool, or ``None`` if
        every segment hull failed (degenerate input).
    """
    n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
        chain, triangles, face_id, tri_normals
    )
    open_overshoot = max(_OPEN_OVERSHOOT * _chain_max_size(sizes), _MIN_PRISM_THICKNESS)
    frames = _vertex_frames(
        chain,
        vertices,
        n_a_per_vertex,
        n_b_per_vertex,
        open_overshoot,
        endpoint_overshoots,
    )
    n_frames = len(frames)
    is_loop = chain.is_loop
    segments: list[m3d.Manifold] = []
    last = n_frames if is_loop else n_frames - 1
    for index in range(last):
        next_index = (index + 1) % n_frames
        segment = _chamfer_segment_hull(
            frames[index],
            frames[next_index],
            float(sizes[index]),
            float(sizes[next_index]),
        )
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
    sizes: np.ndarray,
) -> Optional[tuple[int, float, float]]:
    """Constraint A — half-thickness rule (per-vertex, A4-compatible).

    At every chain vertex ``v`` the *opposing-face distance* must be greater
    than ``2 · sizes[i]``; otherwise the swept tool would slice through the
    body (design §5.1). For A4 every vertex is checked against *its own*
    requested size — the strictest threshold wins.

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
        sizes (np.ndarray): per-vertex requested chamfer leg / fillet radius.

    Returns:
        tuple[int, float, float] | None: ``(host_vertex_index, requested_size,
        measured_thickness)`` for the first failing vertex, or ``None`` if
        every vertex passes.
    """
    bbox_min, bbox_max = vertices.min(axis=0), vertices.max(axis=0)
    bbox_diag = float(np.linalg.norm(bbox_max - bbox_min))

    for index, v_index in enumerate(chain.verts):
        size_i = float(sizes[index])
        threshold = 2.0 * size_i
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
        # Reject points far off-axis (we want roughly axial hits). The radial
        # tolerance scales with the per-vertex size (the swept tool's lateral
        # footprint is ``size`` at this vertex).
        axial = offsets - depths[:, None] * inward
        radial = np.linalg.norm(axial, axis=1)
        on_axis = ahead & (radial < size_i * 1.5 + 1e-3)
        if not on_axis.any():
            continue
        nearest_depth = float(depths[on_axis].min())
        if nearest_depth < threshold:
            return int(v_index), size_i, nearest_depth
    return None


def _check_feasibility(
    chain: FeatureChain,
    vertices: np.ndarray,
    n_a_per_vertex: list[np.ndarray],
    n_b_per_vertex: list[np.ndarray],
    sizes: np.ndarray,
) -> None:
    """Run per-chain feasibility checks, raise :exc:`MeshFilletInfeasible` on failure.

    Two checks (design §5.1): half-thickness (per-vertex under A4) and
    chain-length (against the maximum per-vertex size — the strictest
    threshold). The cross-chain corner-sharing case is *not* checked here —
    chamfer handles a single chain whose endpoints sit on a multi-chain
    corner natively (design §9: planar wedges bevel into a corner bit-exactly).

    Args:
        chain (FeatureChain): the chain to test.
        vertices (np.ndarray): host mesh vertices.
        n_a_per_vertex (list[np.ndarray]): per-vertex face-A normal.
        n_b_per_vertex (list[np.ndarray]): per-vertex face-B normal.
        sizes (np.ndarray): per-vertex requested chamfer leg / fillet radius.

    Raises:
        MeshFilletInfeasible: with ``constraint`` set to the failing check.
    """
    # Constraint A — half-thickness, per vertex (A4)
    failure = _half_thickness_feasibility(
        chain, vertices, n_a_per_vertex, n_b_per_vertex, sizes
    )
    if failure is not None:
        v_idx, requested, measured = failure
        raise MeshFilletInfeasible(
            f"size {requested:g} exceeds half the local feature thickness "
            f"{measured:g} on chain pair {chain.pair} at host vertex {v_idx}; "
            "the swept tool would slice through the body. Reduce the size or "
            "exclude the offending chain from the selection.",
            chains=[chain],
            constraint="half-thickness",
            requested=requested,
            measured=measured,
        )

    # Constraint B — chain length (against the strictest per-vertex size).
    chain_length = 0.0
    for edge in chain.edges:
        chain_length += float(np.linalg.norm(vertices[edge.v1] - vertices[edge.v0]))
    max_size = _chain_max_size(sizes)
    if not chain.is_loop and chain_length < 2.0 * max_size + 1e-9:
        raise MeshFilletInfeasible(
            f"size {max_size:g} exceeds half the chain arc length "
            f"{chain_length:g} on chain pair {chain.pair}; the swept tool's "
            "cross-section frame would fold. Reduce the size or pick a longer "
            "chain.",
            chains=[chain],
            constraint="chain-length",
            requested=max_size,
            measured=chain_length,
        )


# ---------------------------------------------------------------------------
# corner pre-flight (Phase A3c — design §4.5 / §4.6)
# ---------------------------------------------------------------------------


def _classify_corner_problem(
    corner: Corner, size: float
) -> Optional[tuple[str, str, list[FeatureChain]]]:
    """Return ``(constraint, message, chains)`` if ``corner`` cannot be blended.

    Pure classification — no raise. Used by both the raise- and skip-mode
    dispatchers; the raise-mode wrapper :func:`_check_corner_feasibility`
    promotes any returned tuple into a :class:`MeshFilletInfeasible`.

    Args:
        corner: the corner to classify.
        size: the requested fillet radius / chamfer size (only embedded in
            the diagnostic message — the failure mode itself is geometric).

    Returns:
        tuple[str, str, list[FeatureChain]] | None: ``(constraint_tag,
        human_message, incident_chains)`` or ``None`` if the corner is fine.
    """
    if corner.kind == "mixed":
        chains = [ep.chain for ep in corner.chain_endpoints]
        return (
            "mixed-corner",
            f"corner at vertex {corner.vertex} has both convex and concave "
            "chains incident; mesh-fillet's setback + vertex-patch construction "
            "(design §4.5) requires a uniform-sign corner. Fillet the convex "
            "and concave chains in separate calls so each call's corners are "
            "consistent.",
            chains,
        )
    if corner.kind == "degenerate":
        chains = [ep.chain for ep in corner.chain_endpoints]
        return (
            "k>6-corner",
            f"corner at vertex {corner.vertex} has {corner.k} incident chains "
            f"(> {MAX_CORNER_CHAINS}); mesh-fillet supports up to "
            f"{MAX_CORNER_CHAINS}-edge corners (design §4.6). Reduce the "
            "corner's edge count or split the selection.",
            chains,
        )
    _ = size  # size carried by callers — only used for the report payload
    return None


def _check_corner_feasibility(
    corners: dict[int, Corner],
    size: float,
) -> None:
    """Pre-flight every multi-chain corner — raise on mixed / k > 6.

    Implements the design's NG_F4 (mixed convex/concave corners) and NG_F5
    (k > 6 corners) non-goals. Both cases require analytic tooling we do not
    have on a faceted mesh; the design specifies a clean raise with a
    one-line workaround in the message ("fillet convex and concave subsets
    in separate calls" for mixed; "split the corner" for k>6).

    Args:
        corners: ``vertex_index → Corner`` from
            :func:`build123d.mesh.corners.detect_corners`.
        size: the requested fillet radius / chamfer size (carried on the
            raised exception).

    Raises:
        MeshFilletInfeasible: with ``constraint`` ``"mixed-corner"`` or
            ``"k>6-corner"`` and ``chains`` listing every chain incident to
            the offending corner.
    """
    for corner in corners.values():
        problem = _classify_corner_problem(corner, size)
        if problem is None:
            continue
        constraint, message, chains = problem
        raise MeshFilletInfeasible(
            message,
            chains=chains,
            constraint=constraint,
            requested=size,
        )


def _filter_corners_for_skip(
    corners: dict[int, Corner],
    size: float,
    report: "FilletReport",
) -> dict[int, Corner]:
    """Skip-mode counterpart of :func:`_check_corner_feasibility`.

    Walks every corner; for the bad ones, appends a :class:`SkippedItem` to
    ``report.skipped_corners`` and drops the corner from the returned dict.
    The remaining corners are safe to feed into the patch builders.

    Args:
        corners: input corner records by vertex index.
        size: requested fillet radius / chamfer size (used as the
            ``SkippedItem.requested`` payload).
        report: the report to append to (mutated in place).

    Returns:
        dict[int, Corner]: only the corners that passed classification.
    """
    keep: dict[int, Corner] = {}
    for vertex_index, corner in corners.items():
        problem = _classify_corner_problem(corner, size)
        if problem is None:
            keep[vertex_index] = corner
            continue
        constraint, message, chains = problem
        report.skipped_corners.append(
            SkippedItem(
                chains=list(chains),
                constraint=constraint,
                requested=size,
                measured=0.0,
                vertex=int(vertex_index),
                message=message,
            )
        )
        _LOG.warning(
            "mesh-fillet skip: corner at vertex %d (constraint=%s)",
            int(vertex_index),
            constraint,
        )
    return keep


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
    size: RadiusInput,
    *,
    on_infeasible: Literal["raise", "skip"] = "raise",
) -> "MeshPart":
    """Apply a faceted chamfer to ``edges``.

    Free-function counterpart of :meth:`MeshPart.chamfer`. Implements the
    per-chain swept-tool construction from the mesh-fillet engineering design
    (§3 — the profile is a flat triangular wedge, the chamfer leg length is
    ``size``). The pre-flight (§5) raises :exc:`MeshFilletInfeasible` on
    over-size requests, mixed corners, or k>6 corners by default;
    ``on_infeasible="skip"`` drops the offending item and continues.

    A4: ``size`` may be a scalar ``float`` (uniform — bit-identical to the
    A3a path) **or** a callable ``(chain, vertex_index) -> float`` evaluated
    at every chain vertex. Per-vertex sizes are validated positive; the
    feasibility pre-flight evaluates the half-thickness rule with each
    vertex's local size and refuses the chain if any vertex fails. The
    ribbon-mesh loft naturally interpolates between adjacent rings of
    different sizes.

    Args:
        meshpart (MeshPart): the mesh body to chamfer.
        edges (SelectionInput): the chains to chamfer — a
            :class:`FeatureChainSelection`, a single :class:`FeatureChain`, or
            any iterable of :class:`FeatureChain`. Chains must belong to
            ``meshpart``'s own chain graph.
        size: chamfer leg length — a positive ``float`` or a callable
            ``(chain, vertex_index) -> float`` returning a positive size at
            every chain vertex.
        on_infeasible (Literal["raise", "skip"]): default ``"raise"`` —
            never silently mis-answer (P3). ``"skip"`` drops the offending
            chain/corner and continues; dropped items are attached to the
            returned :class:`MeshPart`'s
            :attr:`~build123d.mesh.MeshPart.last_fillet_report`.

    Returns:
        MeshPart: the chamfered mesh body, carrying the merged side-map. The
        ``last_fillet_report`` attribute is set when ``on_infeasible="skip"``
        dropped any chain/corner; ``None`` otherwise.

    Raises:
        ValueError: if ``size`` is not strictly positive (scalar) or returns
            a non-positive value (callable), ``meshpart`` is empty, or
            ``on_infeasible`` is not one of ``"raise"`` / ``"skip"``.
        MeshFilletInfeasible: if any feasibility constraint fails and
            ``on_infeasible="raise"``.
        TypeError: if ``edges`` is none of the accepted shapes or ``size`` is
            neither a number nor a callable.
    """
    return _mesh_chamfer_or_fillet(
        meshpart, edges, size, operation="chamfer", on_infeasible=on_infeasible
    )


def _mesh_chamfer_impl(
    meshpart: "MeshPart",
    edges: SelectionInput,
    size: RadiusInput,
    *,
    on_infeasible: Literal["raise", "skip"],
) -> "MeshPart":
    """Internal chamfer driver (called by :func:`_mesh_chamfer_or_fillet`).

    This is the body of :func:`mesh_chamfer` with the dispatch wrapper
    stripped, so :func:`mesh_fillet` can share the same outer-shell
    validation without recursing through itself.

    Args:
        meshpart: the mesh body to chamfer.
        edges: the selection input.
        size: scalar float or per-vertex callable.
        on_infeasible: ``"raise"`` or ``"skip"``.

    Returns:
        MeshPart: the chamfered mesh body.
    """
    # pylint: disable=import-outside-toplevel,too-many-branches,too-many-locals
    # pylint: disable=too-many-statements
    from .mesh_part import MeshPart

    scalar, per_vertex_fn = _normalise_radius(size, "chamfer")
    selection = meshpart.feature_edges()
    chains = _selection_chains(edges, selection)
    if not chains:
        return MeshPart(meshpart.manifold, meshpart.side_map)

    vertices = selection.vertices
    triangles = selection.triangles
    face_id = selection.face_id
    tri_normals = triangle_normals(vertices, triangles)

    report = FilletReport(operation="chamfer", requested=scalar)
    skipped_chain_ids: set[int] = set()

    # Detect multi-chain corners (design §4) and pre-flight mixed / k>6 cases
    # before building any tool. The chain swept tools get a per-endpoint
    # setback at each corner and the corner gets a flat-polyhedron vertex
    # patch (cut for convex, add for concave).
    corners = detect_corners(chains, vertices, triangles, face_id, tri_normals)
    # A representative scalar for the corner-feasibility payload — the
    # callable's max sample is the strictest constraint and the most useful
    # number on the report.
    representative_size = (
        scalar if scalar is not None else _representative_size(chains, per_vertex_fn)
    )
    if on_infeasible == "raise":
        _check_corner_feasibility(corners, representative_size)
        active_corners = corners
    else:
        active_corners = _filter_corners_for_skip(corners, representative_size, report)

    # Pre-flight every chain *before* building any tool — fail fast on raise,
    # collect on skip.
    per_chain_normals: dict[int, tuple[list[np.ndarray], list[np.ndarray]]] = {}
    per_chain_sizes: dict[int, np.ndarray] = {}
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        if chain.convexity_class == "mixed":
            msg = (
                f"chain pair {chain.pair} is mixed convex/concave; chamfer "
                "operates on chains with a single convexity sign. Split the "
                "selection so each chain is uniformly convex or concave."
            )
            if on_infeasible == "raise":
                raise MeshFilletInfeasible(
                    msg,
                    chains=[chain],
                    constraint="mixed-convexity",
                    requested=representative_size,
                )
            report.skipped_chains.append(
                SkippedItem(
                    chains=[chain],
                    constraint="mixed-convexity",
                    requested=representative_size,
                    measured=0.0,
                    vertex=None,
                    message=msg,
                )
            )
            _LOG.warning(
                "mesh-chamfer skip: chain pair %s (constraint=mixed-convexity)",
                chain.pair,
            )
            skipped_chain_ids.add(id(chain))
            continue
        # Per-vertex callable returning non-positive at any vertex always
        # raises (it's a caller bug, never a geometry constraint — skip mode
        # does NOT swallow these).
        sizes = _chain_radius_samples(chain, per_vertex_fn)
        n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
            chain, triangles, face_id, tri_normals
        )
        try:
            _check_feasibility(
                chain,
                vertices,
                n_a_per_vertex,
                n_b_per_vertex,
                sizes,
            )
        except MeshFilletInfeasible as exc:
            if on_infeasible == "raise":
                raise
            report.skipped_chains.append(
                SkippedItem(
                    chains=list(exc.chains),
                    constraint=exc.constraint,
                    requested=exc.requested,
                    measured=exc.measured,
                    vertex=None,
                    message=str(exc),
                )
            )
            _LOG.warning(
                "mesh-chamfer skip: chain pair %s (constraint=%s requested=%g "
                "measured=%g)",
                chain.pair,
                exc.constraint,
                exc.requested,
                exc.measured,
            )
            skipped_chain_ids.add(id(chain))
            continue
        per_chain_normals[id(chain)] = (n_a_per_vertex, n_b_per_vertex)
        per_chain_sizes[id(chain)] = sizes

    # Build per-chain tools; collect into cut (convex) and add (concave) batches.
    cut_tools: list[m3d.Manifold] = []
    add_tools: list[m3d.Manifold] = []
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        if id(chain) in skipped_chain_ids:
            continue
        sizes = per_chain_sizes[id(chain)]
        chain_max = _chain_max_size(sizes)
        # Corner setback: shorten the chain tool at any corner endpoint by
        # the corner's setback distance (a negative overshoot in the frame
        # helper's convention). Non-corner endpoints keep the standard
        # open-chain overshoot.
        start_s, end_s = per_chain_setback(active_corners, chain, chain_max, vertices)
        overshoots: Optional[tuple[float, float]] = None
        if (start_s > 0.0 or end_s > 0.0) and not chain.is_loop:
            open_ov = max(_OPEN_OVERSHOOT * chain_max, _MIN_PRISM_THICKNESS)
            ov_start = -start_s if start_s > 0.0 else open_ov
            ov_end = -end_s if end_s > 0.0 else open_ov
            overshoots = (ov_start, ov_end)
        tool = _build_chain_chamfer_tool(
            chain,
            vertices,
            triangles,
            face_id,
            tri_normals,
            sizes,
            endpoint_overshoots=overshoots,
        )
        if tool is None:
            if on_infeasible == "skip":
                report.skipped_chains.append(
                    SkippedItem(
                        chains=[chain],
                        constraint="degenerate-tool",
                        requested=chain_max,
                        measured=0.0,
                        vertex=None,
                        message=(
                            f"swept chamfer tool returned no manifold for "
                            f"chain pair {chain.pair} — likely geometrically "
                            "degenerate input."
                        ),
                    )
                )
                _LOG.warning(
                    "mesh-chamfer skip: chain pair %s (constraint=degenerate-tool)",
                    chain.pair,
                )
            continue
        if chain.convexity_class == "convex":
            cut_tools.append(tool)
        else:
            add_tools.append(tool)

    # Build per-corner chamfer patches (design §4.3 step 5) and merge into the
    # appropriate cut / add batches. A convex corner contributes a flat
    # polyhedron to the cut tool; a concave corner contributes one to the add
    # tool. Mixed / degenerate corners were rejected by the pre-flight above
    # (raise) or filtered out (skip).
    #
    # Per-corner size: pick the strictest size across each corner's surviving
    # incident chains. If every incident chain was skipped, drop the corner
    # patch entirely — otherwise a freestanding pyramid would sit at the
    # corner with no chain tubes terminating into it.
    for corner in active_corners.values():
        if corner.kind not in ("convex", "concave"):
            continue
        size_for_corner = _corner_size(
            corner, per_chain_sizes, fallback=representative_size
        )
        if size_for_corner is None:
            continue
        patch = build_corner_chamfer_patch(corner, size_for_corner, vertices)
        if patch is None:
            continue
        if corner.kind == "convex":
            cut_tools.append(patch)
        else:
            add_tools.append(patch)

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

    result = _drop_zero_volume_artifacts(result)
    out = MeshPart(result, _carry_side_map(meshpart.side_map))
    # pylint: disable=protected-access
    out._last_fillet_report = report if report else None
    # pylint: enable=protected-access
    return out


def _representative_size(
    chains: Sequence[FeatureChain], per_vertex_fn: RadiusFunc
) -> float:
    """Pick a single representative size for callable-radius inputs.

    Used in diagnostic messages where the original scalar-radius code
    expected a single number. We take the maximum sample over every
    selected non-flat chain vertex — the strictest feasibility threshold
    and the most informative single number to surface on the report.

    Note this is NOT used to size corner patches under variable radius —
    those use a per-corner radius via :func:`_corner_size`, otherwise an
    oversize-but-skipped chain would still inflate every corner patch.

    Args:
        chains: the selection (the chains we'll evaluate).
        per_vertex_fn: the resolved per-vertex callable.

    Returns:
        float: the representative size (always > 0 because every per-vertex
        call has already been validated to return > 0 — but the empty case
        falls back to ``1.0`` as a defensive sentinel; the caller never
        actually uses it when there are no chains).
    """
    max_size = 0.0
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        for index in range(len(chain.verts)):
            try:
                value = float(per_vertex_fn(chain, index))
            except (TypeError, ValueError):
                # The proper validation happens in _chain_radius_samples;
                # here we just want a representative number.
                continue
            max_size = max(max_size, value)
    return max_size if max_size > 0.0 else 1.0


def _corner_size(
    corner: Corner,
    per_chain_sizes: dict[int, np.ndarray],
    fallback: float,
) -> Optional[float]:
    """Pick the patch radius for one corner from its incident chains' per-vertex sizes.

    For each chain endpoint at the corner, look up that endpoint's local
    per-vertex size from the chain's resampled radii. The patch radius is
    the **maximum** across those endpoint samples (the strictest setback the
    incident chains need, design §4.3 step 2's ``s = r`` generalisation).

    Args:
        corner: the corner to size.
        per_chain_sizes: ``id(chain) -> per-vertex sizes`` for every chain
            that passed the pre-flight. Chains that were skipped or filtered
            do not appear in this map.
        fallback: the scalar input (or ``_representative_size`` value for a
            callable) used when *no* incident chain survived. Returning the
            fallback in this case is harmless because the caller will see
            no surviving chain tubes touching this corner and the patch by
            itself just adds/cuts a faceted ball — but in practice we
            **return None** to drop the corner entirely (the patch with no
            corresponding chain tubes leaves a freestanding sphere bump).

    Returns:
        float | None: the per-corner radius, or ``None`` if every incident
        chain was skipped — in which case the caller drops the corner.
    """
    samples: list[float] = []
    for endpoint in corner.chain_endpoints:
        chain = endpoint.chain
        chain_sizes = per_chain_sizes.get(id(chain))
        if chain_sizes is None:
            # This chain was skipped — its tube doesn't reach the corner.
            continue
        v_index = 0 if endpoint.side == "start" else len(chain.verts) - 1
        samples.append(float(chain_sizes[v_index]))
    if not samples:
        return None
    _ = fallback  # signature parity for documentation
    return max(samples)


def _mesh_chamfer_or_fillet(
    meshpart: "MeshPart",
    edges: SelectionInput,
    size: RadiusInput,
    *,
    operation: Literal["chamfer", "fillet"],
    on_infeasible: Literal["raise", "skip"],
    segments: int = _DEFAULT_FILLET_SEGMENTS,
) -> "MeshPart":
    """Shared outer-shell validation for :func:`mesh_chamfer` / :func:`mesh_fillet`.

    Performs argument-type validation, empty-mesh rejection, and
    ``on_infeasible`` validation; then dispatches to the operation-specific
    impl. The split keeps each public entry point's argument handling
    declarative while the geometry pipeline lives in one place per operation.
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart

    if not isinstance(meshpart, MeshPart):
        raise TypeError(f"mesh_{operation} expects a MeshPart, got {type(meshpart)!r}")
    if on_infeasible not in ("raise", "skip"):
        raise ValueError(
            f"on_infeasible={on_infeasible!r} is not supported; expected "
            f"'raise' (default — P3 contract) or 'skip' (A4)."
        )
    if meshpart.manifold.is_empty():
        raise ValueError(f"Cannot {operation} an empty MeshPart")

    if operation == "chamfer":
        return _mesh_chamfer_impl(meshpart, edges, size, on_infeasible=on_infeasible)
    return _mesh_fillet_impl(
        meshpart,
        edges,
        size,
        segments=segments,
        on_infeasible=on_infeasible,
    )


def _drop_zero_volume_artifacts(
    manifold: m3d.Manifold, threshold: float = 1e-6
) -> m3d.Manifold:
    """Drop near-zero-volume components from a manifold.

    A3c's corner-blend tools occasionally leave tiny zero-volume surface
    fragments after the batched boolean (sphere-vs-tube precision pinch
    points). These are not geometrically meaningful — every component with
    ``|volume| > threshold`` is the real body — but they make
    ``manifold.decompose()`` over-count. We recompose just the substantial
    components so callers see one body when there is one body.

    Args:
        manifold: the manifold to clean.
        threshold: components with ``|volume| ≤ threshold`` are dropped.

    Returns:
        manifold3d.Manifold: the cleaned manifold, or the input unchanged if
        every component is below the threshold (caller decides what to do
        with an empty result).
    """
    if manifold.is_empty():
        return manifold
    components = manifold.decompose()
    if len(components) <= 1:
        return manifold
    real_components = [
        component for component in components if abs(component.volume()) > threshold
    ]
    if not real_components:
        return manifold
    if len(real_components) == len(components):
        return manifold
    if len(real_components) == 1:
        return real_components[0]
    return m3d.Manifold.batch_boolean(real_components, m3d.OpType.Add)


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


def _build_swept_from_per_vertex_profile(
    frames: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]],
    profiles_2d: list[np.ndarray],
    is_loop: bool,
) -> Optional[m3d.Manifold]:
    """A4 variant of :func:`_build_swept_from_profile` with per-vertex profiles.

    Every frame gets its own 2-D profile (lifted into 3-D at that frame),
    rather than one shared profile lifted at every frame. Every profile must
    have the same number of points ``k`` so the ribbon mesh's quad stitching
    lines up frame-to-frame. The scalar path is a thin wrapper that broadcasts
    its single profile to every frame.

    Args:
        frames: the per-vertex frames.
        profiles_2d: a list, one ``(K, 2)`` profile per frame. ``len(profiles_2d)``
            must equal ``len(frames)`` and every profile must have the same
            ``K``.
        is_loop: True if the sub-run closes on itself.

    Returns:
        manifold3d.Manifold | None: the swept solid, or ``None`` if the input
        is geometrically degenerate.
    """
    if len(profiles_2d) != len(frames):
        return None
    if not profiles_2d:
        return None
    k = profiles_2d[0].shape[0]
    if any(prof.shape[0] != k for prof in profiles_2d):
        return None
    lifted = [
        _lift_profile_to_3d(frame, profile)
        for frame, profile in zip(frames, profiles_2d)
    ]
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
    radii: np.ndarray,
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

    A4: ``radii`` is a per-sub-run-vertex array of rolling-ball radii. Each
    frame gets its own profile sized at its local radius; the ribbon-mesh
    loft interpolates between adjacent rings. A constant ``radii`` array is
    the scalar / A3b path, bit-identical to the pre-A4 code.

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
        radii (np.ndarray): per-sub-run-vertex rolling-ball radii, length
            ``len(sub_run_verts)``, every entry > 0.
        segments (int): arc facet count.

    Returns:
        manifold3d.Manifold | None: the combined swept tool, or ``None`` if
        every loft hull failed (degenerate input).
    """
    if len(radii) != len(sub_run_verts):
        return None
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
    open_overshoot = max(_OPEN_OVERSHOOT * _chain_max_size(radii), _MIN_PRISM_THICKNESS)
    frames = _vertex_frames(
        transient,
        vertices,
        n_a_per_vertex,
        n_b_per_vertex,
        open_overshoot,
        endpoint_overshoots,
    )

    wedge_profiles: list[np.ndarray] = []
    ball_profiles: list[np.ndarray] = []
    for index in range(len(frames)):
        r = float(radii[index])
        wedge = _wedge_profile_points(r)
        ball = _ball_profile_points(r, segments)
        if sign == "concave":
            # For concave fillets the wedge sits on the *outside* of the corner
            # — design §3.4: "the same triangle but on the outside, translated
            # by (-d, -d) along (-u, -w)". Mirror the profile in (u, w) so the
            # lifted tool fills the empty channel rather than re-cutting the
            # already-solid region.
            wedge = -wedge
            ball = -ball
        wedge_profiles.append(wedge)
        ball_profiles.append(ball)

    wedge_tool = _build_swept_from_per_vertex_profile(
        frames, wedge_profiles, is_loop_subrun
    )
    ball_tool = _build_swept_from_per_vertex_profile(
        frames, ball_profiles, is_loop_subrun
    )
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
    radii: np.ndarray,
) -> None:
    """Run the fillet feasibility checks for one chain (design §5.1).

    Mirrors A3a's chamfer pre-flight (half-thickness, chain-length) but with
    the fillet per-vertex radii substituted for the chamfer size.
    Mixed-convexity is **not** a constraint for fillet — A3b splits mixed
    chains at the sign flip and builds per-sub-run tools (design §3.4 / §8.3).

    Args:
        chain (FeatureChain): the chain to test.
        vertices (np.ndarray): host vertex array.
        triangles (np.ndarray): host triangle array.
        face_id (np.ndarray): host per-triangle faceID.
        tri_normals (np.ndarray): host per-triangle normals.
        radii (np.ndarray): per-vertex requested fillet radius.

    Raises:
        MeshFilletInfeasible: with ``constraint`` set to the failing check.
    """
    n_a_per_vertex, n_b_per_vertex = _per_vertex_face_normals(
        chain, triangles, face_id, tri_normals
    )
    _check_feasibility(chain, vertices, n_a_per_vertex, n_b_per_vertex, radii)


# ---------------------------------------------------------------------------
# the public entry — fillet (Phase A3b)
# ---------------------------------------------------------------------------


def mesh_fillet(
    meshpart: "MeshPart",
    edges: SelectionInput,
    radius: RadiusInput,
    *,
    segments: int = _DEFAULT_FILLET_SEGMENTS,
    on_infeasible: Literal["raise", "skip"] = "raise",
) -> "MeshPart":
    """Apply a faceted fillet to ``edges``.

    Free-function counterpart of :meth:`MeshPart.fillet`. Implements the
    per-chain swept-arc-tool construction from the mesh-fillet engineering
    design (§3, §8.1 — the profile is a ``segments``-faceted quarter-disc
    cross-section, the rolling-ball radius is ``radius``). The pre-flight (§5)
    raises :exc:`MeshFilletInfeasible` on over-size requests, mixed corners,
    and k>6 corners by default; ``on_infeasible="skip"`` drops the offending
    item and continues.

    A4: ``radius`` may be a scalar ``float`` (uniform — bit-identical to the
    A3b path) **or** a callable ``(chain, vertex_index) -> float`` evaluated
    at every chain vertex. Each frame's profile is sized at its local radius;
    the ribbon-mesh loft naturally interpolates between adjacent rings. The
    feasibility pre-flight is per-vertex: any vertex that fails the
    half-thickness rule against *its own* radius triggers the chain-level
    raise (or skip).

    Mixed convex/concave chains are handled by **per-edge sign splitting**
    (design §3.4 / §8.3): the chain is broken at every sign flip into
    single-sign sub-runs, each built as its own swept tool. The cuts (convex
    sub-runs) are batched together, then the adds (concave sub-runs) — cut
    first, then add (design §3.7).

    Multi-chain corner blends are A3c's setback + faceted-sphere recipe;
    every chain that runs into a corner is setback by the corner's setback
    distance, and the corner is filled with a sphere patch.

    Args:
        meshpart (MeshPart): the mesh body to fillet.
        edges (SelectionInput): the chains to fillet — a
            :class:`FeatureChainSelection`, a single :class:`FeatureChain`, or
            any iterable of :class:`FeatureChain`. Chains must belong to
            ``meshpart``'s own chain graph.
        radius: rolling-ball radius — a positive ``float`` or a callable
            ``(chain, vertex_index) -> float`` returning a positive radius at
            every chain vertex.
        segments (int): arc facet count for the cross-section. Default
            ``8`` matches design §8.1's recommended ``n_seg``.
        on_infeasible (Literal["raise", "skip"]): default ``"raise"`` —
            never silently mis-answer (P3). ``"skip"`` drops the offending
            chain/corner and continues; dropped items are attached to the
            returned :class:`MeshPart`'s
            :attr:`~build123d.mesh.MeshPart.last_fillet_report`.

    Returns:
        MeshPart: the filleted mesh body, carrying the merged side-map. The
        ``last_fillet_report`` attribute is set when ``on_infeasible="skip"``
        dropped any chain/corner; ``None`` otherwise.

    Raises:
        ValueError: if ``radius`` is not strictly positive (scalar) or
            returns a non-positive value (callable), ``segments < 1``,
            ``meshpart`` is empty, or ``on_infeasible`` is not one of
            ``"raise"`` / ``"skip"``.
        MeshFilletInfeasible: if any feasibility constraint fails and
            ``on_infeasible="raise"``.
        TypeError: if ``edges`` is none of the accepted shapes or ``radius``
            is neither a number nor a callable.
    """
    if segments < 1:
        raise ValueError(f"fillet segments must be >= 1, got {segments!r}")
    return _mesh_chamfer_or_fillet(
        meshpart,
        edges,
        radius,
        operation="fillet",
        on_infeasible=on_infeasible,
        segments=segments,
    )


def _mesh_fillet_impl(
    meshpart: "MeshPart",
    edges: SelectionInput,
    radius: RadiusInput,
    *,
    segments: int,
    on_infeasible: Literal["raise", "skip"],
) -> "MeshPart":
    """Internal fillet driver (called by :func:`_mesh_chamfer_or_fillet`).

    The fillet pipeline mirrors :func:`_mesh_chamfer_impl` but with sign
    splitting per chain (a mixed chain becomes one tool per convex / concave
    sub-run) and the rolling-ball cross-section.

    Args:
        meshpart: the mesh body to fillet.
        edges: the selection input.
        radius: scalar float or per-vertex callable.
        segments: arc facet count.
        on_infeasible: ``"raise"`` or ``"skip"``.

    Returns:
        MeshPart: the filleted mesh body.
    """
    # pylint: disable=import-outside-toplevel,too-many-branches,too-many-locals
    # pylint: disable=too-many-statements,too-many-nested-blocks
    from .mesh_part import MeshPart

    scalar, per_vertex_fn = _normalise_radius(radius, "fillet")
    selection = meshpart.feature_edges()
    chains = _selection_chains(edges, selection)
    if not chains:
        return MeshPart(meshpart.manifold, meshpart.side_map)

    vertices = selection.vertices
    triangles = selection.triangles
    face_id = selection.face_id
    tri_normals = triangle_normals(vertices, triangles)

    report = FilletReport(operation="fillet", requested=scalar)
    skipped_chain_ids: set[int] = set()

    # Detect multi-chain corners (design §4) and pre-flight mixed / k>6 cases
    # before building any tool. A3c: chains terminating at a corner get a
    # setback at that endpoint, and a faceted sphere fills the corner.
    corners = detect_corners(chains, vertices, triangles, face_id, tri_normals)
    representative_size = (
        scalar if scalar is not None else _representative_size(chains, per_vertex_fn)
    )
    if on_infeasible == "raise":
        _check_corner_feasibility(corners, representative_size)
        active_corners = corners
    else:
        active_corners = _filter_corners_for_skip(corners, representative_size, report)

    # Pre-flight every chain — sample radii, then run the half-thickness /
    # chain-length checks. Per-vertex sample arrays are cached for tool build.
    per_chain_radii: dict[int, np.ndarray] = {}
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        # Non-positive per-vertex callable return — always raises (caller bug,
        # not a geometry constraint; skip mode does NOT swallow these).
        radii = _chain_radius_samples(chain, per_vertex_fn)
        try:
            _check_fillet_feasibility(
                chain, vertices, triangles, face_id, tri_normals, radii
            )
        except MeshFilletInfeasible as exc:
            if on_infeasible == "raise":
                raise
            report.skipped_chains.append(
                SkippedItem(
                    chains=list(exc.chains),
                    constraint=exc.constraint,
                    requested=exc.requested,
                    measured=exc.measured,
                    vertex=None,
                    message=str(exc),
                )
            )
            _LOG.warning(
                "mesh-fillet skip: chain pair %s (constraint=%s requested=%g "
                "measured=%g)",
                chain.pair,
                exc.constraint,
                exc.requested,
                exc.measured,
            )
            skipped_chain_ids.add(id(chain))
            continue
        per_chain_radii[id(chain)] = radii

    cut_tools: list[m3d.Manifold] = []
    add_tools: list[m3d.Manifold] = []
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        if id(chain) in skipped_chain_ids:
            continue
        radii = per_chain_radii[id(chain)]
        chain_max = _chain_max_size(radii)
        open_overshoot = max(_OPEN_OVERSHOOT * chain_max, _MIN_PRISM_THICKNESS)
        kind_for: dict[int, str] = dict(zip(chain.verts, chain.vertex_kinds))
        sub_runs = _split_chain_by_sign(chain, vertices)
        chain_start_v = chain.verts[0]
        chain_end_v = chain.verts[-1]
        start_setback, end_setback = per_chain_setback(
            active_corners, chain, chain_max, vertices
        )
        # Per-vertex radii indexed by host-mesh vertex index for the sub-run
        # lookups below — each sub-run uses a subset of the parent chain's
        # vertices, and we resample at exactly those positions.
        radii_by_host_vertex: dict[int, float] = {
            int(chain.verts[i]): float(radii[i]) for i in range(len(chain.verts))
        }
        for sub_verts, sign in sub_runs:
            is_loop_sub = chain.is_loop and len(sub_verts) == len(chain.verts)
            endpoint_overshoots: Optional[tuple[float, float]] = None
            if not is_loop_sub:
                # See A3b commentary: corner setback at chain endpoints; zero
                # overshoot at mid-chain sign-flip boundaries.
                if sub_verts[0] == chain_start_v and start_setback > 0.0:
                    ov_start = -start_setback
                elif sub_verts[0] == chain_start_v:
                    ov_start = open_overshoot if sign == "convex" else 0.0
                    kind = kind_for.get(sub_verts[0])
                    if sign == "concave" and kind == "corner":
                        ov_start = 0.0
                else:
                    ov_start = 0.0
                if sub_verts[-1] == chain_end_v and end_setback > 0.0:
                    ov_end = -end_setback
                elif sub_verts[-1] == chain_end_v:
                    ov_end = open_overshoot if sign == "convex" else 0.0
                    kind = kind_for.get(sub_verts[-1])
                    if sign == "concave" and kind == "corner":
                        ov_end = 0.0
                else:
                    ov_end = 0.0
                endpoint_overshoots = (ov_start, ov_end)
            sub_radii = np.array(
                [radii_by_host_vertex[int(v)] for v in sub_verts],
                dtype=np.float64,
            )
            tool = _build_swept_arc_tool(
                sub_verts,
                chain,
                is_loop_sub,
                vertices,
                triangles,
                face_id,
                tri_normals,
                sub_radii,
                segments,
                endpoint_overshoots,
                sign=sign,
            )
            if tool is None:
                if on_infeasible == "skip":
                    report.skipped_chains.append(
                        SkippedItem(
                            chains=[chain],
                            constraint="degenerate-tool",
                            requested=chain_max,
                            measured=0.0,
                            vertex=None,
                            message=(
                                f"swept fillet tool returned no manifold for "
                                f"chain pair {chain.pair} sub-run ({sign}) — "
                                "likely geometrically degenerate input."
                            ),
                        )
                    )
                    _LOG.warning(
                        "mesh-fillet skip: chain pair %s sub-run %s "
                        "(constraint=degenerate-tool)",
                        chain.pair,
                        sign,
                    )
                continue
            if sign == "convex":
                cut_tools.append(tool)
            else:
                add_tools.append(tool)

    # Build per-corner faceted-sphere patches (design §4.3 step 5) and merge
    # into the appropriate cut / add batches. A convex corner contributes a
    # sphere to the cut tool (subtracted, rounding the corner); a concave
    # corner contributes one to the add tool. Mixed / degenerate corners were
    # rejected (raise) or filtered (skip).
    #
    # Per-corner radius: the maximum sample across the corner's surviving
    # incident chains. If every incident chain was skipped, drop the corner
    # patch — a freestanding sphere with no chain tubes converging into it
    # is geometrically meaningless and would punch a spurious hole / bump.
    for corner in active_corners.values():
        if corner.kind not in ("convex", "concave"):
            continue
        radius_for_corner = _corner_size(
            corner, per_chain_radii, fallback=representative_size
        )
        if radius_for_corner is None:
            continue
        patch = build_corner_fillet_patch(corner, radius_for_corner, segments)
        if patch is None:
            continue
        if corner.kind == "convex":
            cut_tools.append(patch)
        else:
            add_tools.append(patch)

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

    result = _drop_zero_volume_artifacts(result)
    out = MeshPart(result, _carry_side_map(meshpart.side_map))
    # pylint: disable=protected-access
    out._last_fillet_report = report if report else None
    # pylint: enable=protected-access
    return out


__all__ = [
    "FilletReport",
    "MeshFilletInfeasible",
    "SkippedItem",
    "mesh_chamfer",
    "mesh_fillet",
]
