"""
build123d mesh

name: corners.py

desc:

Multi-chain corner blending (Phase A3c) for :class:`~build123d.mesh.MeshPart`
fillet and chamfer — the **setback + vertex patch** construction specified in
the mesh-fillet engineering design (§4, the design's centrepiece).

At a vertex where three or more feature chains of different ``(lo, hi)``
faceID pairs meet, the per-chain swept tools of A3a/A3b would each terminate
at the vertex and leave a tiny pyramidal point of the original solid sticking
out into the middle of where the ball/flat corner should be (p10's
``q_min=0.001`` thin-patch failure). The A3c construction follows the
classical ACIS / Spatial "setback vertex blending" recipe (design §4.2):

* **Setback** each incident chain's tool back from the corner by a distance
  ``s`` so it terminates on a "setback ring" some way down the chain;
* **Drop a vertex patch** that fills the corner gap — a faceted sphere of
  radius ``r`` for a *fillet* (design §4.3 step 5), or a flat polyhedron
  (tetrahedron / generalised n-gon pyramid) for a *chamfer*;
* **Union** the setback tubes with the vertex patch into the combined cut
  (convex corner) or add (concave corner) tool.

**Mixed convex/concave corners and k > 6 corners raise** —
``MeshFilletInfeasible(constraint="mixed-corner" | "k>6-corner")`` (design
§4.5, §4.6, the canonical NG_F4/NG_F5 non-goals).

This module is used by :mod:`build123d.mesh.fillet` — it does not depend on
:class:`MeshPart` directly; it works on the per-chain primitives the swept
tool machinery already builds and returns ``manifold3d.Manifold`` add/cut
contributions the dispatcher merges with the per-chain batched booleans.

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
from typing import Literal, Optional, Sequence

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from .feature_edges import FeatureChain, edge_convexity_sign

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member


# Per-chain endpoint "side" tag at a corner: which end of the chain meets the
# corner vertex. (Loops never have corner endpoints — every loop vertex is
# either interior or shared with another loop at a degenerate junction we
# explicitly do not handle here; the corner machinery only sees *open* chain
# endpoints.)
CornerSide = Literal["start", "end"]

# The corner classification — convex / concave / mixed / degenerate (k>6 or k<3).
CornerKind = Literal["convex", "concave", "mixed", "degenerate"]

# Maximum chain count at one corner before we raise k>6-corner (design §4.6).
MAX_CORNER_CHAINS = 6

# Sign threshold for "is this corner-chain convex or concave?" — matches
# fillet.py's _SIGN_TOL.
_SIGN_TOL = 1e-6

# Fraction of the shortest incident-chain length we allow the setback to consume
# (design §4.3 step 2 — "never consume more than 45% of a chain length to the
# corner").
_SETBACK_MAX_FRACTION = 0.45

# A sphere-corner patch needs at least this many segments so the patch joins the
# swept-tube facets cleanly. Design §4.3 step 5 suggests ``n = chain n_seg + 4``.
_CORNER_SPHERE_SEG_BIAS = 4


@dataclass
class ChainEndpointAtCorner:
    """One chain's contribution to a corner vertex.

    A chain that *ends* at a corner contributes an :class:`ChainEndpointAtCorner`
    pointing to that chain plus which side (``"start"`` or ``"end"``) of the
    chain sits on the corner vertex. The per-chain swept tool builder uses this
    record to apply the setback at the right endpoint when it constructs the
    chain's swept arc / wedge.

    Attributes:
        chain (FeatureChain): the incident chain.
        side (CornerSide): which end of ``chain.verts`` meets the corner.
        convex (bool): True for a convex chain endpoint at this corner. (A
            mixed-sign chain might have its convex sub-run end at the corner
            while its concave sub-run ends elsewhere; this field is the *local*
            sign of the edge incident to the corner.)
        tangent_into_chain (np.ndarray): unit 3-vector along the chain pointing
            *away* from the corner (toward the next interior vertex). The
            chain tool's swept tube starts at distance ``s`` along this tangent.
    """

    chain: FeatureChain
    side: CornerSide
    convex: bool
    tangent_into_chain: np.ndarray


@dataclass
class Corner:
    """A multi-chain corner vertex — the unit the §4 setback recipe operates on.

    Attributes:
        vertex (int): the host-mesh vertex index sitting at the corner.
        position (np.ndarray): ``(3,)`` corner coordinate.
        chain_endpoints (list[ChainEndpointAtCorner]): every chain touching
            the corner, with its side and local sign.
        face_normals (list[np.ndarray]): per-incident-face unit outward normals
            (one per distinct ``face_id`` met at this corner — usually three
            for a cube corner, more at higher-k corners).
        kind (CornerKind): ``"convex"`` (every endpoint convex),
            ``"concave"`` (every endpoint concave), ``"mixed"`` (both signs at
            this corner — RAISES), or ``"degenerate"`` (k > 6 — RAISES).
    """

    vertex: int
    position: np.ndarray
    chain_endpoints: list[ChainEndpointAtCorner]
    face_normals: list[np.ndarray] = field(default_factory=list)
    kind: CornerKind = "convex"

    @property
    def k(self) -> int:
        """Number of incident chains (i.e. ``k`` in design §4.6)."""
        return len(self.chain_endpoints)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _unit(vector: np.ndarray) -> np.ndarray:
    """Normalize a 3-vector; return the input unchanged if it is near-zero."""
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


def _chain_total_length(chain: FeatureChain, vertices: np.ndarray) -> float:
    """Sum every edge length of ``chain``."""
    total = 0.0
    for edge in chain.edges:
        total += float(np.linalg.norm(vertices[edge.v1] - vertices[edge.v0]))
    return total


# ---------------------------------------------------------------------------
# corner detection
# ---------------------------------------------------------------------------


def detect_corners(
    chains: Sequence[FeatureChain],
    vertices: np.ndarray,
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
) -> dict[int, Corner]:
    """Scan ``chains`` for multi-chain corners and classify each one.

    A corner is a host-mesh vertex where *two or more* :class:`FeatureChain` s
    of **different** ``(lo, hi)`` faceID pairs terminate (design §1.3,
    promoted to ≥3 by §4 — the standard corner is k≥3, but a vertex shared
    by exactly two chains-of-different-pairs is also classified so the
    chains' shared endpoint can be properly setback). The classification per
    corner:

    * if any endpoint is convex *and* any endpoint is concave →
      :attr:`Corner.kind` = ``"mixed"`` (design §4.5: caller raises);
    * if ``k > MAX_CORNER_CHAINS`` → ``"degenerate"`` (design §4.6);
    * if every endpoint is convex → ``"convex"``;
    * if every endpoint is concave → ``"concave"``.

    Only open-chain endpoints participate; a loop has no terminating vertex
    and is never a corner-chain (its frame walks the loop continuously).

    Args:
        chains: the chains in the selection.
        vertices: host mesh ``(N, 3)`` vertex array.
        triangles: host mesh ``(M, 3)`` triangle vertex indices.
        face_id: host mesh ``(M,)`` seeded face id per triangle.
        tri_normals: host mesh ``(M, 3)`` unit outward normals.

    Returns:
        dict[int, Corner]: ``vertex_index → Corner`` for every corner the
        selection's chains form. Vertices with only one chain-endpoint are
        not included (no corner blend is needed there).
    """
    # Walk every chain's open endpoints and collect them per host vertex.
    endpoints_at: dict[int, list[ChainEndpointAtCorner]] = defaultdict(list)
    pairs_at: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for chain in chains:
        if chain.convexity_class == "flat":
            continue
        if chain.is_loop:
            # Loops never have endpoints; their frames walk continuously.
            continue
        if len(chain.verts) < 2:
            continue
        # Endpoint-local sign — the sign of the *edge incident to the corner*
        # rather than the whole chain. A mixed chain might still have a single
        # consistent sign at its corner endpoint; we read that sign.
        first_edge_sign = (
            edge_convexity_sign(vertices, chain.edges[0]) if chain.edges else 0.0
        )
        last_edge_sign = (
            edge_convexity_sign(vertices, chain.edges[-1]) if chain.edges else 0.0
        )
        start_v = chain.verts[0]
        end_v = chain.verts[-1]
        start_tangent = _unit(vertices[chain.verts[1]] - vertices[start_v])
        end_tangent = _unit(vertices[chain.verts[-2]] - vertices[end_v])
        if abs(first_edge_sign) >= _SIGN_TOL:
            endpoints_at[int(start_v)].append(
                ChainEndpointAtCorner(
                    chain=chain,
                    side="start",
                    convex=first_edge_sign > 0.0,
                    tangent_into_chain=start_tangent,
                )
            )
            pairs_at[int(start_v)].add(chain.pair)
        if abs(last_edge_sign) >= _SIGN_TOL:
            endpoints_at[int(end_v)].append(
                ChainEndpointAtCorner(
                    chain=chain,
                    side="end",
                    convex=last_edge_sign > 0.0,
                    tangent_into_chain=end_tangent,
                )
            )
            pairs_at[int(end_v)].add(chain.pair)

    corners: dict[int, Corner] = {}
    for v_index, eps in endpoints_at.items():
        # The setback + vertex-patch construction is *defined* for k ≥ 3
        # chains meeting at one vertex (design §4 — the three-edge corner is
        # the canonical case). A k = 2 vertex is a chain bend, not a junction:
        # the two chains' swept tubes meet there naturally, and adding a
        # sphere / polyhedron patch would over-cut the body. We therefore
        # require ≥ 3 *distinct chain pairs* at the vertex; vertices with
        # only one or two chains in the *current selection* are not corners
        # for this run (the design specifies a corner is the meeting of ≥ 3
        # chains; the user can include the third chain in the selection to
        # opt into the patch construction).
        if len(pairs_at[v_index]) < 3:
            continue
        face_normals = _face_normals_at_vertex(
            v_index, eps, triangles, face_id, tri_normals
        )
        kind = _classify_corner(eps)
        corners[v_index] = Corner(
            vertex=v_index,
            position=vertices[v_index].copy(),
            chain_endpoints=eps,
            face_normals=face_normals,
            kind=kind,
        )
    return corners


def _classify_corner(endpoints: list[ChainEndpointAtCorner]) -> CornerKind:
    """Classify a corner from its chain-endpoint convexities."""
    if len(endpoints) > MAX_CORNER_CHAINS:
        return "degenerate"
    has_convex = any(ep.convex for ep in endpoints)
    has_concave = any(not ep.convex for ep in endpoints)
    if has_convex and has_concave:
        return "mixed"
    if has_convex:
        return "convex"
    return "concave"


def _face_normals_at_vertex(
    vertex_index: int,
    endpoints: list[ChainEndpointAtCorner],
    triangles: np.ndarray,
    face_id: np.ndarray,
    tri_normals: np.ndarray,
) -> list[np.ndarray]:
    """Per-incident-face unit outward normals at ``vertex_index``.

    The set of distinct ``face_id`` s touched by the corner's chains is
    collected (the union of every chain's ``pair`` (lo, hi)). For each
    ``face_id``, every triangle of that face touching ``vertex_index`` is
    located and the outward normals are averaged into a single unit vector —
    the "face normal at the corner".

    Args:
        vertex_index: host vertex sitting at the corner.
        endpoints: incident chain endpoints (their ``chain.pair`` s are the
            faceIDs that bound the corner).
        triangles: host mesh ``(M, 3)`` triangle vertex indices.
        face_id: host mesh ``(M,)`` seeded face id per triangle.
        tri_normals: host mesh ``(M, 3)`` unit outward normals.

    Returns:
        list[np.ndarray]: one unit normal per distinct face id at the corner.
    """
    face_ids: set[int] = set()
    for ep in endpoints:
        face_ids.update(int(f) for f in ep.chain.pair)
    samples: dict[int, list[np.ndarray]] = defaultdict(list)
    for tri_index, tri in enumerate(triangles):
        fid = int(face_id[tri_index])
        if fid not in face_ids:
            continue
        for v_local in tri:
            if int(v_local) == vertex_index:
                samples[fid].append(tri_normals[tri_index])
                break
    normals: list[np.ndarray] = []
    for fid in sorted(face_ids):
        if samples[fid]:
            normals.append(_unit(np.mean(samples[fid], axis=0)))
    return normals


# ---------------------------------------------------------------------------
# setback distance + per-corner offsets
# ---------------------------------------------------------------------------


def compute_setback(
    corner: Corner,
    size: float,
    vertices: np.ndarray,
) -> float:
    """Pick a per-corner setback distance ``s`` (design §4.3 step 2).

    The design's default is ``s = r`` (fillet) or ``s = d`` (chamfer), clamped
    to ``_SETBACK_MAX_FRACTION · L`` for every incident chain length ``L``.
    Returns 0.0 if the clamped setback would collapse to zero (i.e. every
    incident chain is shorter than the required ε); the caller treats that
    as a degenerate corner and skips the patch.

    Args:
        corner: the corner record to size.
        size: the requested fillet radius / chamfer size.
        vertices: host mesh ``(N, 3)`` vertex array (for chain-length sums).

    Returns:
        float: the setback distance ``s`` for every chain at this corner
        (uniform across the corner — design §4.3 picks a single ``s`` per
        corner, not per chain).
    """
    s = float(size)
    for ep in corner.chain_endpoints:
        chain_len = _chain_total_length(ep.chain, vertices)
        if chain_len <= 1e-9:
            continue
        s = min(s, _SETBACK_MAX_FRACTION * chain_len)
    return max(s, 0.0)


def per_chain_setback(
    corners: dict[int, Corner],
    chain: FeatureChain,
    size: float,
    vertices: np.ndarray,
) -> tuple[float, float]:
    """Return the ``(start_setback, end_setback)`` to apply to ``chain``.

    For an open chain whose start vertex is a corner the start setback is the
    corner's :func:`compute_setback` value; for an interior endpoint the
    setback is ``0`` (the chain tool still overshoots by its open-overshoot
    amount — handled by the caller). Loops always return ``(0, 0)``.

    Args:
        corners: corner records by vertex index.
        chain: the chain to inspect.
        size: requested fillet radius / chamfer size.
        vertices: host mesh ``(N, 3)`` vertex array.

    Returns:
        tuple[float, float]: ``(start_setback, end_setback)``.
    """
    if chain.is_loop or len(chain.verts) < 2:
        return 0.0, 0.0
    start_v = chain.verts[0]
    end_v = chain.verts[-1]
    start_s = 0.0
    end_s = 0.0
    start_corner = corners.get(int(start_v))
    end_corner = corners.get(int(end_v))
    if start_corner is not None and start_corner.kind in ("convex", "concave"):
        start_s = compute_setback(start_corner, size, vertices)
    if end_corner is not None and end_corner.kind in ("convex", "concave"):
        end_s = compute_setback(end_corner, size, vertices)
    return start_s, end_s


# ---------------------------------------------------------------------------
# the vertex patches (sphere for fillet, polyhedron for chamfer)
# ---------------------------------------------------------------------------


def _inward_unit(corner: Corner, convex: bool) -> Optional[np.ndarray]:
    """Unit 3-vector pointing into the solid at a convex/concave corner.

    For a convex corner the outward face normals all point *out* of the body,
    so ``-sum(normals)`` points inward — into the material the cut tool needs
    to remove. For a concave corner the construction mirrors: the add tool's
    sphere sits *outside* the corner (in the missing-material void), so we
    flip the sign.

    Returns ``None`` if the normal sum is too short to normalise (the corner
    is geometrically degenerate — all face normals cancel).
    """
    if not corner.face_normals:
        return None
    total = np.sum(corner.face_normals, axis=0)
    if convex:
        inward = -total
    else:
        inward = total
    norm = float(np.linalg.norm(inward))
    if norm < 1e-9:
        return None
    return inward / norm


def build_corner_fillet_patch(
    corner: Corner,
    radius: float,
    segments: int,
) -> Optional[m3d.Manifold]:
    """Build the faceted-sphere vertex patch for a fillet corner (design §4.3 step 5).

    The construction:

    1. Compute ``c = v + r · inward_unit`` (convex) or ``c = v − r · inward_unit``
       (concave) — the inset position of the patch sphere centre.
    2. Build :func:`manifold3d.Manifold.sphere` with ``segments + 4`` circular
       segments (a faceted geodesic icosahedron-style ball — design §4.3 step 5
       picks up the chain's facetting and adds a small bias).
    3. Translate to ``c``. The resulting solid is the cut/add patch the caller
       unions with the setback chain tubes.

    Args:
        corner: the corner record.
        radius: the fillet rolling-ball radius.
        segments: the chain's cross-section facet count — the sphere uses
            ``segments + _CORNER_SPHERE_SEG_BIAS`` so the sphere is at least as
            smooth as the adjacent rolled-arc strips.

    Returns:
        manifold3d.Manifold | None: the translated sphere, or ``None`` if the
        corner's inward direction degenerates (caller falls back to the
        no-patch behaviour).
    """
    convex = corner.kind == "convex"
    inward = _inward_unit(corner, convex=convex)
    if inward is None:
        return None
    if convex:
        centre = corner.position + radius * inward
    else:
        centre = corner.position - radius * inward
    n_seg = max(int(segments) + _CORNER_SPHERE_SEG_BIAS, 8)
    ball = m3d.Manifold.sphere(float(radius), circular_segments=n_seg)
    ball = ball.translate(tuple(float(x) for x in centre))
    if ball.status() != m3d.Error.NoError or ball.is_empty():
        return None
    return ball


def build_corner_chamfer_patch(
    corner: Corner,
    size: float,
    vertices: np.ndarray,
) -> Optional[m3d.Manifold]:
    """Build the flat-polyhedron vertex patch for a chamfer corner (design §4.3 step 5).

    For a k-edge convex chamfer corner the patch is a generalised pyramid: an
    apex at the corner vertex ``v``, base vertices at the setback positions on
    each incident chain ``v + s · tangent_into_chain``. ``Manifold.hull_points``
    on the apex + base points gives the planar-faced tetrahedron / pyramid
    that the cut boolean removes — leaving a flat triangle / n-gon between the
    setback ring endpoints, exactly the classical chamfer-corner facet.

    For a concave corner the same hull is built but the apex is *reflected*
    across the corner vertex (``v + (v - apex)``); the add boolean fills the
    inside of the dihedral.

    Args:
        corner: the corner record.
        size: the chamfer leg length (equal to the setback distance ``s``).
        vertices: host mesh ``(N, 3)`` vertex array.

    Returns:
        manifold3d.Manifold | None: the patch polyhedron, or ``None`` if the
        hull degenerates (e.g. coplanar base points).
    """
    s = compute_setback(corner, size, vertices)
    if s <= 0.0:
        return None
    apex = corner.position.copy()
    base = [apex + s * ep.tangent_into_chain for ep in corner.chain_endpoints]
    if corner.kind == "concave":
        # Reflect the apex to the opposite side of the base so the add-tool
        # pyramid points *into* the void rather than into the body.
        centroid = np.mean(np.array(base), axis=0)
        apex = 2.0 * centroid - apex
    points = np.vstack([apex.reshape(1, 3), np.array(base)])
    hull = m3d.Manifold.hull_points(points.tolist())
    if hull.status() != m3d.Error.NoError or hull.is_empty():
        return None
    return hull


__all__ = [
    "ChainEndpointAtCorner",
    "Corner",
    "CornerKind",
    "CornerSide",
    "MAX_CORNER_CHAINS",
    "build_corner_chamfer_patch",
    "build_corner_fillet_patch",
    "compute_setback",
    "detect_corners",
    "per_chain_setback",
]
