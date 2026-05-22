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
of a Minkowski sum). ``manifold3d`` therefore re-derives ``face_id`` from its own
coplanar-region calculation, which does **not** trace back to any input
build123d :class:`~build123d.Face`. The resulting :class:`MeshPart` carries an
**empty** :class:`~build123d.mesh.bridge.SideMap`; :meth:`MeshPart.to_solid`
then falls back to the faceted bake (one planar face per triangle). This is an
honest consequence of the operation, not a limitation of the bridge.

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

    A hull synthesises new envelope facets, so no input ``face_id`` survives:
    the returned :class:`~build123d.mesh.MeshPart` carries an **empty**
    side-map and :meth:`MeshPart.to_solid` bakes it faceted (see this module's
    docstring).

    Args:
        *operands: one or more build123d Shapes / MeshParts to envelop.

    Returns:
        MeshPart: the convex hull, in mesh space, with no provenance.

    Raises:
        ValueError: if no operand is given, or the hull is degenerate (all
            operands collapse to fewer than four non-coplanar points).
    """
    # Imported here, not at module scope, to avoid a circular import:
    # mesh_part imports this module for the MeshPart.hull/.minkowski methods.
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
    # A hull is new geometry: no input face_id traces through it -> empty map.
    return MeshPart(hull)


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
    survives: the result carries an **empty** side-map and bakes faceted.

    Args:
        a: the first operand (build123d Shape or MeshPart).
        b: the second operand (build123d Shape or MeshPart).
        method (str): ``"native"`` (default) or ``"decompose"``.

    Returns:
        MeshPart: the Minkowski sum, in mesh space, with no provenance.

    Raises:
        ValueError: for an unknown ``method``, an invalid manifold result, or
            a non-convex operand under ``method="decompose"``.
    """
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
    return MeshPart(result)


def mesh_minkowski_difference(a: "MeshOperand", b: "MeshOperand") -> "MeshPart":
    """Minkowski difference of two bodies — the morphological erosion ``a ⊖ b``.

    Erodes ``a`` by sweeping ``b`` across its surface and removing the swept
    region (the inverse of :func:`mesh_minkowski`; e.g. eroding a box by a
    sphere shrinks every face inward by the sphere's radius). Uses
    ``manifold3d.Manifold.minkowski_difference``.

    Like the sum, erosion synthesises new geometry: the result carries an empty
    side-map and bakes faceted.

    Args:
        a: the body to erode (build123d Shape or MeshPart).
        b: the eroding body (build123d Shape or MeshPart).

    Returns:
        MeshPart: the Minkowski difference, in mesh space, with no provenance.

    Raises:
        ValueError: if the result is an invalid manifold (erosion of ``a`` by a
            ``b`` larger than ``a`` legitimately yields an *empty* body).
    """
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
    return MeshPart(result)
