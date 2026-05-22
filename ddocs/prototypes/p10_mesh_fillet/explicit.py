"""p10 -- approach (a): explicit geometry, inset + bridge via boolean tool solids.

The brief frames a mesh chamfer as "inset the two adjacent faces and bridge the
gap with a flat strip". Doing that as raw mesh surgery (delete triangles, punch
holes, stitch a strip) is fragile: re-triangulating an arbitrary hole and
keeping the result a 2-manifold is exactly the kind of bookkeeping manifold3d
exists to avoid. So this approach realises the *same geometry* through a
**boolean tool solid**:

* Find the feature edge/chain via faceID (selective -- only the chosen pair).
* For each feature edge, build a prism cross-section in the plane normal to the
  edge: the chamfer/fillet profile.
* Sweep that profile along the edge into a tool ``Manifold``.
* **Convex** edge  -> subtract the tool (carve the bevel/trough).
* **Concave** edge -> add the tool (fill the bevel/trough).

manifold3d's boolean guarantees the result is a watertight 2-manifold, so
"inset + bridge" is delivered without hand-rolled stitching. The bridge strip is
the tool's outer wall; a fillet just uses an arc cross-section instead of a
chamfer's straight one.

This is the OpenSCAD-style faceted round, made *selective* by faceID.
"""

from __future__ import annotations

import numpy as np
import manifold3d as m3d  # type: ignore

from feature_graph import FeatureChain, FeatureEdge


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def edge_convexity(
    vertices: np.ndarray, fe: FeatureEdge
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """Classify a feature edge as convex/concave and return the edge frame.

    Ordering-independent test: face A is *convex* relative to face B iff face
    B's far vertex sits **behind** face A's outward plane -- i.e.
    ``dot(na, far_b - edge) < 0``. (For a concave edge the far vertex pokes out
    in front of the neighbour's plane.) This does not depend on which incident
    triangle ``manifold3d`` happened to label ``tri_a``.

    The reported ``convexity`` magnitude is the signed dihedral
    ``dot(cross(na,nb), edge_dir)`` but its *sign* is forced to match the
    far-vertex test, so it is robust.

    Returns:
        (convexity, edge_dir, na, nb): convexity > 0 convex, < 0 concave;
        edge_dir is a unit vector along the edge.
    """
    p0 = vertices[fe.v0]
    p1 = vertices[fe.v1]
    edge_dir = _unit(p1 - p0)
    na = _unit(fe.normal_a)
    nb = _unit(fe.normal_b)
    magnitude = abs(float(np.dot(np.cross(na, nb), edge_dir)))
    # robust sign from the far-vertex projection
    if fe.far_a >= 0 and fe.far_b >= 0:
        signed = np.dot(na, vertices[fe.far_b] - p0)
        signed += np.dot(nb, vertices[fe.far_a] - p0)
        convex = signed < 0.0
    else:  # fallback to the dihedral sign
        convex = float(np.dot(np.cross(na, nb), edge_dir)) > 0.0
    convexity = magnitude if convex else -magnitude
    if magnitude < 1e-9:
        convexity = 0.0
    return convexity, edge_dir, na, nb


def _chamfer_profile(distance: float) -> np.ndarray:
    """2-D right-triangle chamfer cross-section in the (u, w) edge-normal plane.

    u runs along face A's surface direction, w along face B's. The hypotenuse is
    the bevel; the right-angle corner sits at the original edge.
    Returns (3,2) polygon, CCW.
    """
    return np.array([[0.0, 0.0], [distance, 0.0], [0.0, distance]])


def _fillet_profile(radius: float, segments: int) -> np.ndarray:
    """2-D quarter-disc fillet cross-section (rolling-ball arc).

    The corner at the original edge plus an n-segment arc of radius ``radius``
    centred at (radius, radius). Returns (segments+2, 2) polygon, CCW.
    """
    cx, cy = radius, radius
    pts = [[0.0, 0.0]]
    for i in range(segments + 1):
        ang = np.pi + i * (np.pi / 2) / segments  # from pi to 3pi/2
        pts.append([cx + radius * np.cos(ang), cy + radius * np.sin(ang)])
    return np.array(pts)


def _sweep_tool(
    p0: np.ndarray,
    p1: np.ndarray,
    u_axis: np.ndarray,
    w_axis: np.ndarray,
    profile_2d: np.ndarray,
    overshoot: float,
) -> m3d.Manifold:
    """Sweep a 2-D profile along the segment p0->p1 into a prism Manifold.

    The profile lives in the (u_axis, w_axis) plane. The prism is extended by
    ``overshoot`` past each endpoint along the edge so that, when several edges'
    tools are unioned at a corner, the cut surfaces overlap cleanly.

    Built directly as a triangle mesh (two end caps + side walls) and imported
    as a Manifold -- manifold3d then validates / repairs at the boolean.
    """
    edge_dir = _unit(p1 - p0)
    a = p0 - edge_dir * overshoot
    b = p1 + edge_dir * overshoot
    n = len(profile_2d)
    # 3-D ring of profile points at each cap
    ring_a = np.array([a + uu * u_axis + ww * w_axis for uu, ww in profile_2d])
    ring_b = np.array([b + uu * u_axis + ww * w_axis for uu, ww in profile_2d])
    verts = np.vstack([ring_a, ring_b])
    tris: list[tuple[int, int, int]] = []
    # side walls
    for i in range(n):
        j = (i + 1) % n
        # quad (ring_a[i], ring_a[j], ring_b[j], ring_b[i])
        tris.append((i, j, n + j))
        tris.append((i, n + j, n + i))
    # end caps -- fan triangulation (profile is convex enough for our shapes)
    for i in range(1, n - 1):
        tris.append((0, i + 1, i))          # cap A (reverse winding)
        tris.append((n, n + i, n + i + 1))  # cap B
    tri_arr = np.array(tris, dtype=np.uint64)

    def _make(tris_in: np.ndarray) -> m3d.Manifold:
        mesh = m3d.Mesh64(
            vert_properties=np.ascontiguousarray(verts, dtype=np.float64),
            tri_verts=np.ascontiguousarray(tris_in, dtype=np.uint64),
        )
        return m3d.Manifold(mesh)

    man = _make(tri_arr)
    # A closed mesh with reversed winding still imports as a valid 2-manifold but
    # has *negative* volume -- subtracting it would ADD material. Flip so the
    # tool is a genuine positive solid.
    if man.status() != m3d.Error.NoError or man.volume() < 0.0:
        man = _make(tri_arr[:, ::-1].copy())
    return man


# ---------------------------------------------------------------------------
# the selective chamfer / fillet
# ---------------------------------------------------------------------------


def _fillet_convex_profile(radius: float, segments: int) -> np.ndarray:
    """2-D 'wedge minus quarter-disc' region carved from a convex edge.

    Polygon: the original sharp edge corner (0,0), then the n-segment arc of
    radius ``radius`` centred at (radius, radius) running from (radius,0) to
    (0,radius). Removing this region rounds a convex edge.
    """
    cx, cy = radius, radius
    pts = [[0.0, 0.0]]
    for i in range(segments + 1):
        ang = 1.5 * np.pi - i * (np.pi / 2) / segments  # 3pi/2 -> pi
        pts.append([cx + radius * np.cos(ang), cy + radius * np.sin(ang)])
    return np.array(pts)


def _edge_frame(
    vertices: np.ndarray, fe: FeatureEdge
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool, float]:
    """Return (p0, p1, u_axis, w_axis, is_convex, convexity) for one edge.

    u_axis / w_axis are the in-solid tangents of the two adjacent faces; the
    tool profile's right-angle corner sits on the edge and its legs run along
    these axes into the material.
    """
    convexity, edge_dir, na, nb = edge_convexity(vertices, fe)
    p0, p1 = vertices[fe.v0], vertices[fe.v1]
    u_axis = _unit(np.cross(na, edge_dir))
    w_axis = _unit(np.cross(nb, edge_dir))
    if np.dot(u_axis, -nb) < 0:
        u_axis = -u_axis
    if np.dot(w_axis, -na) < 0:
        w_axis = -w_axis
    return p0, p1, u_axis, w_axis, convexity > 0.0, convexity


def _build_tools(
    vertices: np.ndarray,
    chains: list[FeatureChain],
    profile_fn,
    overshoot: float,
) -> tuple[list[m3d.Manifold], list[m3d.Manifold]]:
    """Build (cut_tools, add_tools) for every edge of every chain.

    ``profile_fn(is_convex) -> (M,2)`` returns the cross-section polygon. Convex
    edges -> cut tools (carve), concave edges -> add tools (fill).
    """
    cut_tools: list[m3d.Manifold] = []
    add_tools: list[m3d.Manifold] = []
    for chain in chains:
        for fe in chain.edges:
            p0, p1, u_axis, w_axis, is_convex, convexity = _edge_frame(vertices, fe)
            if abs(convexity) < 1e-6:
                continue
            profile = profile_fn(is_convex)
            tool = _sweep_tool(p0, p1, u_axis, w_axis, profile, overshoot)
            if tool.status() != m3d.Error.NoError or tool.is_empty():
                continue
            (cut_tools if is_convex else add_tools).append(tool)
    return cut_tools, add_tools


def _apply_tools(
    base: m3d.Manifold,
    cut_tools: list[m3d.Manifold],
    add_tools: list[m3d.Manifold],
) -> m3d.Manifold:
    """Apply cut/add tools to ``base`` in ONE batch boolean per direction.

    Tools are unioned into a single combined tool first, then a single
    difference / union is taken against the base. Doing the whole chain in two
    booleans (rather than one per edge) avoids the corner-fragmentation seen
    when intermediate results are re-cut edge-by-edge.

    Order matters: **cut first, then add.** An overshooting convex-cut tool can
    otherwise slice through just-added concave-fill material and isolate a
    fragment (seen on the L-shape, where a convex edge's cut tool crossed the
    reentrant edge's fill). Cutting before adding means the fill is applied to
    the final carved body and cannot be re-sliced.
    """
    result = base
    if cut_tools:
        combined = m3d.Manifold.batch_boolean(cut_tools, m3d.OpType.Add)
        result = result - combined
    if add_tools:
        combined = m3d.Manifold.batch_boolean(add_tools, m3d.OpType.Add)
        result = result + combined
    return result


def selective_chamfer(
    base: m3d.Manifold,
    vertices: np.ndarray,
    chains: FeatureChain | list[FeatureChain],
    distance: float,
    overshoot: float | None = None,
) -> m3d.Manifold:
    """Apply a faceted chamfer to one or more selected feature chains.

    Selective: only the edges of the given chain(s) -- identified by faceID
    pair -- are chamfered. Convex edges have the bevel carved, concave edges
    filled.

    Args:
        base: the body to chamfer.
        vertices: the body's vertex array (chain verts index into this).
        chains: one FeatureChain or a list of them.
        distance: chamfer leg length.
        overshoot: tool extension past each edge endpoint. Defaults to
            ``distance``.

    Returns:
        manifold3d.Manifold: the chamfered body (valid by manifold3d guarantee).
    """
    if overshoot is None:
        overshoot = distance
    chain_list = [chains] if isinstance(chains, FeatureChain) else list(chains)
    cut_tools, add_tools = _build_tools(
        vertices, chain_list, lambda _conv: _chamfer_profile(distance), overshoot
    )
    return _apply_tools(base, cut_tools, add_tools)


def selective_fillet(
    base: m3d.Manifold,
    vertices: np.ndarray,
    chains: FeatureChain | list[FeatureChain],
    radius: float,
    segments: int = 8,
    overshoot: float | None = None,
) -> m3d.Manifold:
    """Apply a faceted (n-segment rolling-ball) fillet to selected chain(s).

    Same machinery as :func:`selective_chamfer` but with an arc profile: a
    convex edge has a 'wedge minus quarter-disc' region carved (the rolling-ball
    round); a concave edge has a quarter-disc of material added.

    Args:
        base: the body to fillet.
        vertices: the body's vertex array.
        chains: one FeatureChain or a list of them.
        radius: rolling-ball radius.
        segments: arc facet count.
        overshoot: tool extension past each edge endpoint. Defaults to
            ``radius``.

    Returns:
        manifold3d.Manifold: the filleted body.
    """
    if overshoot is None:
        overshoot = radius
    chain_list = [chains] if isinstance(chains, FeatureChain) else list(chains)

    def profile_fn(is_convex: bool) -> np.ndarray:
        if is_convex:
            return _fillet_convex_profile(radius, segments)
        return _fillet_profile(radius, segments)

    cut_tools, add_tools = _build_tools(
        vertices, chain_list, profile_fn, overshoot
    )
    return _apply_tools(base, cut_tools, add_tools)
