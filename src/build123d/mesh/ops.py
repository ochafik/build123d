"""
build123d mesh

name: ops.py

desc:

Geometry-*generating* mesh operations that sit alongside the CSG booleans:
the convex :func:`mesh_hull` and the Minkowski sum / difference
(:func:`mesh_minkowski`). Both are exposed as free functions here and as
:class:`~build123d.mesh.MeshPart` methods (:meth:`MeshPart.hull`,
:meth:`MeshPart.minkowski`).

Unlike a boolean, hull and Minkowski do **not** preserve face provenance: they
synthesise entirely new surfaces (the hull's envelope facets, the rounded shell
of a Minkowski sum) that trace back to no input build123d
:class:`~build123d.Face`. ``manifold3d`` does, however, re-derive ``face_id``
from its own coplanar-region calculation, and that grouping is good: a hull of a
box yields one id per planar facet, a Minkowski box ⊕ box yields six, and a
Minkowski box ⊕ sphere keeps the six flat faces merged while the rounded shell
stays per-facet. So the resulting :class:`MeshPart` carries a **synthetic**
:class:`~build123d.mesh.bridge.SideMap` (see
:func:`~build123d.mesh.bridge.synthetic_side_map`): coplanar-region ids with no
claimed analytic surface. :meth:`MeshPart.to_solid` groups by those ids and
recovers **one merged face per coplanar region** — a *fitted* plane where the
region is planar, a single faceted patch where it is curved — instead of one
anonymous ``TopoDS_Face`` per triangle. This is strictly better than the old
fully-faceted bake, while honestly *not* claiming an exact input surface.

MINKOWSKI BACKENDS
------------------
manifold3d 3.4.x ships a *native* ``Manifold.minkowski_sum`` /
``minkowski_difference`` (morphological dilation / erosion). It is correct for
both convex and non-convex operands; its only caveat is performance — for a
non-convex pair the cost scales with the product of the two face counts. It is
the default backend (``method="native"``).

For the convex case there is a second, fully self-contained backend
(``method="decompose"``) ported from scad2py's ``minkowski_impl.py`` (Apache-2.0,
owned by ochafik): split each operand into convex parts, hull every pair of
parts translated against one another, and union the hulls. With only
``manifold3d`` installed this backend handles **convex** operands (and unions of
convex parts that :meth:`Manifold.decompose` can separate) exactly. It does
**not** perform a general non-convex convex decomposition — that needs an
external library (``coacd`` / ``trimesh``) and is intentionally out of scope; a
non-convex operand under ``method="decompose"`` raises a clear error pointing at
``method="native"``.

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

from typing import TYPE_CHECKING

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

if TYPE_CHECKING:  # pragma: no cover
    from .mesh_part import MeshOperand, MeshPart
    from .sketch2d import Profile2D


# A body is treated as convex when its volume matches its convex hull's volume
# within this *relative* tolerance. The hull of a convex body is the body
# itself, so an exactly-convex body scores 0. The tolerance is deliberately
# loose (0.1%) because a *faceted* convex primitive (a tessellated sphere) is
# geometrically convex but the bridge's grid-snap weld leaves sub-micron vertex
# jitter that registers a ~0.01% hull gap. A genuinely non-convex body (an
# L-shape) has a hull gap orders of magnitude larger (~17%), so 0.1% separates
# the two cleanly without ever mis-classifying real concavity as convex.
_CONVEXITY_REL_TOLERANCE = 1e-3


# ---------------------------------------------------------------------------
# convex hull
# ---------------------------------------------------------------------------


def mesh_hull(*operands: "MeshOperand") -> "MeshPart":
    """Convex hull enveloping any mix of build123d Shapes and MeshParts.

    The hull is computed natively by ``manifold3d`` (``Manifold.hull`` for a
    single operand, ``Manifold.batch_hull`` for several) — the same
    ``quickhull`` engine OpenSCAD's ``hull()`` uses, but watertight by
    construction.

    A hull synthesises new envelope facets, so no input ``face_id`` survives —
    but ``manifold3d`` groups those facets into coplanar regions. The returned
    :class:`~build123d.mesh.MeshPart` carries a **synthetic** side-map (no
    claimed analytic surface) keyed by those regions, so
    :meth:`MeshPart.to_solid` recovers **one merged planar face per hull facet**
    (a box-shaped hull face becomes a single planar face, not N triangles)
    rather than one anonymous face per triangle. See this module's docstring.

    Args:
        *operands: one or more build123d Shapes / MeshParts to envelop.

    Returns:
        MeshPart: the convex hull, in mesh space, with synthetic
        coplanar-region provenance.

    Raises:
        ValueError: if no operand is given, or the hull is degenerate (all
            operands collapse to fewer than four non-coplanar points).
    """
    # Imported here, not at module scope, to avoid a circular import:
    # mesh_part imports this module for the MeshPart.hull/.minkowski methods.
    from .bridge import synthetic_side_map  # pylint: disable=import-outside-toplevel
    from .mesh_part import _coerce  # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart  # pylint: disable=import-outside-toplevel

    if not operands:
        raise ValueError("mesh_hull needs at least one operand")
    parts = [_coerce(operand) for operand in operands]
    manifolds = [part.manifold for part in parts]
    hull = (
        manifolds[0].hull()
        if len(manifolds) == 1
        else m3d.Manifold.batch_hull(manifolds)
    )
    if hull.is_empty() or hull.status() != m3d.Error.NoError:
        raise ValueError(
            "mesh_hull produced an empty manifold; the operands are degenerate "
            "(fewer than four non-coplanar points)."
        )
    # A hull is new geometry: no input face_id traces through it. But manifold3d
    # groups the envelope facets into coplanar regions -> seed those as synthetic
    # ids so to_solid recovers one merged planar face per hull facet.
    seeded, side_map = synthetic_side_map(hull)
    return MeshPart(seeded, side_map)


# ---------------------------------------------------------------------------
# Minkowski sum / difference
# ---------------------------------------------------------------------------


def _is_convex(manifold: m3d.Manifold) -> bool:
    """Return True if ``manifold`` is (numerically) convex.

    A convex body equals its own convex hull, so its volume equals the hull's
    volume; a non-convex body always has a strictly larger hull. This is the
    convexity test scad2py's ``minkowski_impl`` uses.

    Args:
        manifold (manifold3d.Manifold): the body to test.

    Returns:
        bool: True if the body is convex within ``_CONVEXITY_REL_TOLERANCE``.
    """
    volume = manifold.volume()
    if volume <= 0.0:
        return True
    hull_volume = manifold.hull().volume()
    return abs(hull_volume - volume) <= _CONVEXITY_REL_TOLERANCE * volume


def _convex_parts(manifold: m3d.Manifold) -> list[m3d.Manifold]:
    """Split ``manifold`` into a list of convex parts, or raise if it cannot.

    ``Manifold.decompose`` separates *topologically* disconnected bodies; if
    every disconnected body is already convex the union of those parts equals
    the original and the Minkowski sum decomposes exactly. A connected
    non-convex body cannot be split without a true convex-decomposition library
    (``coacd`` / ``trimesh``), which is out of scope for the ``manifold``
    extra — so this raises rather than approximating.

    Args:
        manifold (manifold3d.Manifold): the operand to decompose.

    Returns:
        list[manifold3d.Manifold]: the convex parts.

    Raises:
        ValueError: if a connected component is non-convex.
    """
    parts = manifold.decompose() or [manifold]
    for part in parts:
        if not _is_convex(part):
            raise ValueError(
                "mesh_minkowski(method='decompose') needs convex operands (or a "
                "union of convex parts). A connected non-convex operand needs a "
                "general convex decomposition, which the 'manifold' extra does "
                "not bundle. Use method='native' for non-convex Minkowski."
            )
    return parts


def _part_vertices(manifold: m3d.Manifold) -> np.ndarray:
    """Return the ``(N, 3)`` vertex coordinates of a manifold.

    Args:
        manifold (manifold3d.Manifold): the body to read.

    Returns:
        np.ndarray: its vertex coordinates.
    """
    mesh = manifold.to_mesh()
    return np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]


def _minkowski_via_decomposition(
    left: m3d.Manifold, right: m3d.Manifold
) -> m3d.Manifold:
    """Minkowski sum by the scad2py convex-pairs algorithm.

    Ported from scad2py ``minkowski_impl.minkowski_3d`` (Apache-2.0, ochafik):
    split each operand into convex parts, and for every (left-part,
    right-part) pair take the convex hull of the Cartesian sum of their vertex
    sets (``A ⊕ B`` of two convex bodies is the hull of all vertex sums). Union
    the per-pair hulls for the full sum. ``manifold3d.Manifold.hull_points`` is
    the native primitive that makes each pairwise hull cheap.

    Args:
        left (manifold3d.Manifold): the first operand.
        right (manifold3d.Manifold): the second operand.

    Returns:
        manifold3d.Manifold: the Minkowski sum.

    Raises:
        ValueError: if either operand is non-convex and cannot be split into
            convex parts.
    """
    left_parts = _convex_parts(left)
    right_parts = _convex_parts(right)
    hulls: list[m3d.Manifold] = []
    for left_part in left_parts:
        left_vertices = _part_vertices(left_part)
        for right_part in right_parts:
            right_vertices = _part_vertices(right_part)
            # Cartesian sum: every left vertex offset by every right vertex.
            summed = (left_vertices[:, None, :] + right_vertices[None, :, :]).reshape(
                -1, 3
            )
            hulls.append(m3d.Manifold.hull_points(summed))
    if len(hulls) == 1:
        return hulls[0]
    return m3d.Manifold.batch_boolean(hulls, m3d.OpType.Add)


def mesh_minkowski(
    a: "MeshOperand", b: "MeshOperand", *, method: str = "native"
) -> "MeshPart":
    """Minkowski sum of two bodies — the morphological dilation ``a ⊕ b``.

    Sweeping ``b`` over every point of ``a`` (e.g. a sphere over a box rounds
    the box's edges by the sphere's radius). The two backends:

    * ``method="native"`` (default) — ``manifold3d.Manifold.minkowski_sum``.
      Correct for **convex and non-convex** operands. For a non-convex pair the
      cost scales with the product of the two face counts, so keep operand
      tessellation coarse.
    * ``method="decompose"`` — the scad2py convex-pairs algorithm (this
      module's docstring). Exact for **convex** operands and for unions of
      convex parts; a connected non-convex operand raises (a general convex
      decomposition needs an external library, intentionally not bundled).

    A Minkowski sum synthesises a new rounded shell, so no input ``face_id``
    survives — but ``manifold3d``'s coplanar grouping merges its flat faces. The
    result carries a **synthetic** side-map: :meth:`MeshPart.to_solid` recovers
    the flat faces as merged fitted-planes and the rounded shell as faceted.

    Args:
        a: the first operand (build123d Shape or MeshPart).
        b: the second operand (build123d Shape or MeshPart).
        method (str): ``"native"`` (default) or ``"decompose"``.

    Returns:
        MeshPart: the Minkowski sum, in mesh space, with synthetic
        coplanar-region provenance.

    Raises:
        ValueError: for an unknown ``method``, an invalid manifold result, or
            a non-convex operand under ``method="decompose"``.
    """
    from .bridge import synthetic_side_map  # pylint: disable=import-outside-toplevel
    from .mesh_part import _coerce  # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart  # pylint: disable=import-outside-toplevel

    left = _coerce(a).manifold
    right = _coerce(b).manifold
    if method == "native":
        result = left.minkowski_sum(right)
    elif method == "decompose":
        result = _minkowski_via_decomposition(left, right)
    else:
        raise ValueError(
            f"mesh_minkowski: unknown method {method!r}; "
            "expected 'native' or 'decompose'."
        )
    if result.is_empty() or result.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh_minkowski produced an invalid manifold: {result.status()}"
        )
    # New rounded shell: no input provenance, but manifold3d's coplanar grouping
    # merges the flat faces (the rounded part stays per-facet). Seed synthetic.
    seeded, side_map = synthetic_side_map(result)
    return MeshPart(seeded, side_map)


def mesh_minkowski_difference(a: "MeshOperand", b: "MeshOperand") -> "MeshPart":
    """Minkowski difference of two bodies — the morphological erosion ``a ⊖ b``.

    Erodes ``a`` by sweeping ``b`` across its surface and removing the swept
    region (the inverse of :func:`mesh_minkowski`; e.g. eroding a box by a
    sphere shrinks every face inward by the sphere's radius). Uses
    ``manifold3d.Manifold.minkowski_difference``.

    Like the sum, erosion synthesises new geometry: the result carries a
    **synthetic** coplanar-region side-map (an *empty* result — erosion by a
    tool larger than ``a`` — carries none).

    Args:
        a: the body to erode (build123d Shape or MeshPart).
        b: the eroding body (build123d Shape or MeshPart).

    Returns:
        MeshPart: the Minkowski difference, in mesh space, with synthetic
        coplanar-region provenance (empty side-map if the result is empty).

    Raises:
        ValueError: if the result is an invalid manifold (erosion of ``a`` by a
            ``b`` larger than ``a`` legitimately yields an *empty* body).
    """
    from .bridge import synthetic_side_map  # pylint: disable=import-outside-toplevel
    from .mesh_part import _coerce  # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart  # pylint: disable=import-outside-toplevel

    left = _coerce(a).manifold
    right = _coerce(b).manifold
    result = left.minkowski_difference(right)
    if result.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh_minkowski_difference produced an invalid manifold: "
            f"{result.status()}"
        )
    if result.is_empty():
        # Erosion by a tool larger than the body legitimately empties it; an
        # empty manifold carries no coplanar regions to seed.
        return MeshPart(result)
    seeded, side_map = synthetic_side_map(result)
    return MeshPart(seeded, side_map)


# ---------------------------------------------------------------------------
# 2-D → 3-D — native extrude / revolve via CrossSection
# ---------------------------------------------------------------------------


def mesh_extrude(
    profile: "Profile2D",
    height: float,
    *,
    twist: float = 0.0,
    scale: float | tuple[float, float] = 1.0,
    n_divisions: int = 0,
    tolerance: float = 0.1,
) -> "MeshPart":
    """Linear-extrude a 2-D profile to a 3-D :class:`MeshPart`.

    Routes a build123d 2-D profile through
    :func:`~build123d.mesh.sketch2d.to_cross_section` and then drives
    ``manifold3d.CrossSection.extrude`` — the native primitive that also
    supports a *twist* and a *top-scale* (a non-1 ``scale`` tapers the top
    relative to the bottom; ``scale=0`` collapses the top to a point).

    The result is a synthesised faceted body — no input ``face_id`` traces
    through ``extrude`` — so the returned :class:`MeshPart` carries an empty
    side-map and :meth:`MeshPart.to_solid` falls back to the faceted bake.

    Args:
        profile: a build123d :class:`~build123d.Sketch` /
            :class:`~build123d.Face` / :class:`~build123d.Compound`, or a plain
            ``[(x, y), ...]`` point list.
        height (float): the extrusion distance along +Z. Must be positive.
        twist (float): rotation of the top face about Z, in degrees. Defaults
            to 0 (no twist). A non-zero twist is interpolated linearly across
            ``n_divisions`` slices.
        scale (float | tuple[float, float]): top-face scale relative to the
            bottom — a scalar applies isotropically, a pair scales X and Y
            independently. Defaults to 1.0 (parallel-sided prism). Set to 0
            for a pointed cone-like top.
        n_divisions (int): number of intermediate cross-section slices. The
            native primitive uses 0 for a flat top, raise it when twist or
            scale needs smoother facets. Defaults to 0.
        tolerance (float): linear deflection used to polygonise curved edges
            of the profile. Defaults to 0.1.

    Returns:
        MeshPart: the extruded mesh body, with no face provenance.

    Raises:
        ValueError: if the profile yields no closed contours, ``height`` is
            not positive, or the native extrude produces an invalid manifold.
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart
    from .sketch2d import to_cross_section

    if height <= 0.0:
        raise ValueError(f"mesh_extrude needs a positive height, got {height!r}")
    scale_top = (
        (float(scale), float(scale))
        if isinstance(scale, (int, float))
        else (float(scale[0]), float(scale[1]))
    )
    cross_section = to_cross_section(profile, tolerance=tolerance)
    manifold = cross_section.extrude(
        height=float(height),
        n_divisions=int(n_divisions),
        twist_degrees=float(twist),
        scale_top=scale_top,
    )
    if manifold.is_empty() or manifold.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh_extrude produced an invalid manifold: {manifold.status()}"
        )
    return MeshPart(manifold)


def mesh_revolve(
    profile: "Profile2D",
    *,
    angle: float = 360.0,
    circular_segments: int = 0,
    tolerance: float = 0.1,
) -> "MeshPart":
    """Revolve a 2-D profile about the Y axis to a 3-D :class:`MeshPart`.

    Routes a build123d 2-D profile through
    :func:`~build123d.mesh.sketch2d.to_cross_section` and then drives
    ``manifold3d.CrossSection.revolve``. **The profile is revolved about the
    Y axis** — only its ``X >= 0`` half is swept; any region with ``X < 0`` is
    clipped before the sweep (the same convention OpenSCAD's
    ``rotate_extrude`` follows).

    The result is a synthesised faceted body — no input ``face_id`` traces
    through ``revolve`` — so the returned :class:`MeshPart` carries an empty
    side-map and :meth:`MeshPart.to_solid` falls back to the faceted bake.

    Args:
        profile: a build123d :class:`~build123d.Sketch` /
            :class:`~build123d.Face` / :class:`~build123d.Compound`, or a plain
            ``[(x, y), ...]`` point list.
        angle (float): revolution angle in degrees. Defaults to 360 (full).
            Set to < 360 for a partial revolve.
        circular_segments (int): number of facets around the full revolution
            (the native primitive picks a sensible default for 0). Defaults
            to 0.
        tolerance (float): linear deflection used to polygonise curved edges
            of the profile. Defaults to 0.1.

    Returns:
        MeshPart: the revolved mesh body, with no face provenance.

    Raises:
        ValueError: if the profile yields no closed contours, ``angle`` is
            non-positive, or the native revolve produces an invalid manifold.
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart
    from .sketch2d import to_cross_section

    if angle <= 0.0:
        raise ValueError(f"mesh_revolve needs a positive angle, got {angle!r}")
    cross_section = to_cross_section(profile, tolerance=tolerance)
    manifold = cross_section.revolve(
        circular_segments=int(circular_segments),
        revolve_degrees=float(angle),
    )
    if manifold.is_empty() or manifold.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh_revolve produced an invalid manifold: {manifold.status()}"
        )
    return MeshPart(manifold)


# ---------------------------------------------------------------------------
# 3-D offset / shell — Minkowski with a faceted sphere
# ---------------------------------------------------------------------------


# Default tessellation segment count for the sphere "tool" used to round (or
# erode) edges in the 3-D offset / shell ops. 32 is a balance: faceted enough
# to resolve the sphere shape, coarse enough that the Minkowski cost stays in
# the seconds range for a small body. Tune up for smoother rounds, down for
# speed.
_OFFSET_SPHERE_SEGMENTS = 32


def _offset_sphere(radius: float, *, segments: int) -> m3d.Manifold:
    """Return a tessellated sphere used as the Minkowski tool in 3-D offset.

    Built directly via ``manifold3d.Manifold.sphere`` so the offset op does
    not invoke the BREP bridge for what is purely a mesh-space tool body.

    Args:
        radius (float): the sphere radius (absolute value of the offset).
        segments (int): the sphere's facet segment count.

    Returns:
        manifold3d.Manifold: the sphere body.
    """
    return m3d.Manifold.sphere(radius, segments)


def mesh_offset(
    body: "MeshOperand",
    amount: float,
    *,
    sphere_segments: int = _OFFSET_SPHERE_SEGMENTS,
) -> "MeshPart":
    """3-D offset (inflate / deflate) by ``amount``, via Minkowski with a sphere.

    Outward offset (``amount > 0``) is the Minkowski **sum** of ``body`` with a
    sphere of radius ``amount`` — the body grows outward by ``amount`` with
    every sharp edge rounded by a sphere of that radius. This case is robust:
    it reuses :func:`mesh_minkowski` (``method="native"``).

    Inward offset (``amount < 0``) is the Minkowski **difference** with a
    sphere of radius ``|amount|`` — :func:`mesh_minkowski_difference`. Inward
    erosion is **fragile** on a faceted sphere tool: the native
    ``minkowski_difference`` can collapse to an empty body, hit numerical
    instability, or produce a degenerate manifold when the eroding sphere is
    comparable in size to the body's thinnest feature. See the module
    docstring's "honest limits" note.

    The result is a synthesised faceted body; the returned :class:`MeshPart`
    carries an empty side-map.

    Args:
        body: the body to offset (build123d Shape or MeshPart).
        amount (float): the offset distance. Positive grows outward, negative
            erodes inward, zero is a no-op (the input body, in mesh space).
        sphere_segments (int): segment count for the sphere tool. Higher is
            smoother but materially slower. Defaults to 32.

    Returns:
        MeshPart: the offset body, with no provenance.

    Raises:
        ValueError: if the offset produces an invalid (or, for inward offset
            larger than the body, an empty) manifold. The error message
            points at the honest-limits caveat for inward offsets.
    """
    # pylint: disable=import-outside-toplevel
    from .bridge import synthetic_side_map
    from .mesh_part import MeshPart, _coerce

    part = _coerce(body)
    if amount == 0.0:
        return MeshPart(part.manifold, part.side_map)
    sphere = _offset_sphere(abs(amount), segments=sphere_segments)
    if amount > 0.0:
        result = part.manifold.minkowski_sum(sphere)
    else:
        result = part.manifold.minkowski_difference(sphere)
    if result.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh_offset produced an invalid manifold: {result.status()}. "
            "Inward offsets are fragile on a faceted sphere tool — "
            "raise sphere_segments or reduce |amount|."
        )
    if result.is_empty():
        raise ValueError(
            f"mesh_offset({amount}) produced an empty manifold — the body's "
            "thinnest feature is smaller than the offset radius (inward "
            "erosion of a body by a tool larger than its thinnest section "
            "legitimately empties it)."
        )
    seeded, side_map = synthetic_side_map(result)
    return MeshPart(seeded, side_map)


def mesh_shell(
    body: "MeshOperand",
    thickness: float,
    *,
    sphere_segments: int = _OFFSET_SPHERE_SEGMENTS,
) -> "MeshPart":
    """Hollow ``body`` to a wall of ``thickness`` via ``body − offset(−thickness)``.

    Inward-offsets the body by ``thickness`` and subtracts the eroded body
    from the original, leaving a hollow shell of the given wall thickness.
    Inherits :func:`mesh_offset`'s honest limit on inward erosion — if the
    thickness exceeds the body's thinnest half-feature the inward offset can
    collapse and the shell raises a clear error.

    Args:
        body: the body to hollow (build123d Shape or MeshPart).
        thickness (float): wall thickness. Must be strictly positive.
        sphere_segments (int): segment count for the sphere tool used by the
            inward offset. Defaults to 32.

    Returns:
        MeshPart: the hollow shell, with no provenance.

    Raises:
        ValueError: if ``thickness`` is not positive, or the inward offset
            collapses (the body is too thin to accommodate the wall).
    """
    # pylint: disable=import-outside-toplevel
    from .mesh_part import MeshPart, _coerce, mesh_cut

    if thickness <= 0.0:
        raise ValueError(f"mesh_shell needs a positive thickness, got {thickness!r}")
    part = _coerce(body)
    eroded = mesh_offset(part, -float(thickness), sphere_segments=sphere_segments)
    return mesh_cut(part, eroded)
