"""
build123d mesh

name: sketch2d.py

desc:

The 2-D profile bridge for the ``build123d[manifold]`` extra: converts a
build123d 2-D :class:`~build123d.Sketch` / :class:`~build123d.Face` (or a plain
list of ``(x, y)`` points) into a ``manifold3d.CrossSection`` so the native
2-D→3-D ops (:func:`mesh_extrude`, :func:`mesh_revolve`) can run on a profile
built with build123d's exact-BREP 2-D verbs.

A :class:`~build123d.Face` carries one outer :class:`~build123d.Wire` and zero
or more inner (hole) wires. Each wire is polygonised by tessellating its edges
to a configurable linear tolerance — straight edges become two-point segments,
curved edges (arcs, splines) become point sequences within the deflection. The
result is a list of closed 2-D polygons in the (x, y) plane, fed into
``manifold3d.CrossSection``'s contour constructor. The CrossSection constructor
runs a Clipper2 boolean union on construction, so even slight overlap or vertex
duplication at wire seams is cleaned up automatically.

A :class:`~build123d.Sketch` is a :class:`~build123d.Compound` of one or more
faces; every face is polygonised and the resulting per-face CrossSections are
unioned. A plain list of points is taken as a single closed polygon.

Convention: build123d sketches live in 3-D space on a :class:`~build123d.Plane`;
the bridge takes the (X, Y) coordinates of every vertex *as-is*. Profiles
intended for ``mesh_extrude`` / ``mesh_revolve`` should already lie in the XY
plane (the default for a builder ``Sketch``). Out-of-plane profiles are
silently flattened to XY — the caller is responsible for placing the profile.

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

from typing import Iterable, Sequence, Union

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from build123d.topology import Compound, Face, Shape, Wire

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

# Default linear deflection used when polygonising a wire's edges. Matches the
# default linear tolerance the 3-D bridge uses for shape_to_manifold, so a
# profile extruded here lands at the same fidelity a tessellated Shape would.
_DEFAULT_TOLERANCE = 0.1

# Acceptable input types for the 2-D profile bridge: a build123d 2-D Shape (a
# Face or a Sketch / Compound holding faces), or a plain list of (x, y) points.
Profile2D = Union[Shape, Sequence[Sequence[float]]]  # pylint: disable=invalid-name


def to_cross_section(
    profile: Profile2D, *, tolerance: float = _DEFAULT_TOLERANCE
) -> m3d.CrossSection:
    """Convert a build123d 2-D profile into a ``manifold3d.CrossSection``.

    Accepts a :class:`~build123d.Face`, a :class:`~build123d.Sketch` /
    :class:`~build123d.Compound` of faces, or a plain ``[(x, y), ...]`` list of
    points (taken as a single closed polygon). Wires are polygonised to the
    given linear deflection — straight edges yield two-point segments, curved
    edges yield point sequences within ``tolerance``.

    Args:
        profile: a build123d Face / Sketch / Compound, or a 2-D point list.
        tolerance (float): the linear deflection used to polygonise curved
            edges. Defaults to 0.1 (matching the 3-D bridge's default).

    Returns:
        manifold3d.CrossSection: the 2-D region. May be a multi-contour set
        (outer + holes, multiple faces).

    Raises:
        TypeError: if ``profile`` is none of the supported input types.
        ValueError: if ``profile`` produces no closed contours.
    """
    polygons = _polygons_from_profile(profile, tolerance=tolerance)
    if not polygons:
        raise ValueError("to_cross_section: profile produced no closed contours")
    cross_section = m3d.CrossSection(polygons, m3d.FillRule.Positive)
    if cross_section.is_empty():
        raise ValueError(
            "to_cross_section: resulting CrossSection is empty (degenerate "
            "input — zero-area or self-cancelling contours?)"
        )
    return cross_section


# ---------------------------------------------------------------------------
# Internals — polygonisation
# ---------------------------------------------------------------------------


def _polygons_from_profile(profile: Profile2D, *, tolerance: float) -> list[np.ndarray]:
    """Polygonise a profile into a list of closed (x, y) contours.

    Dispatches on the profile type and returns the list of contours that the
    CrossSection contour constructor consumes. Hole contours are included
    alongside outer contours — Clipper2 (via ``FillRule.Positive``) figures out
    the orientation in the constructor.

    Args:
        profile: a build123d Face / Sketch / Compound, or a 2-D point list.
        tolerance (float): linear deflection for curved edges.

    Returns:
        list[np.ndarray]: every closed contour as an ``(N, 2)`` array.
    """
    # Plain (x, y) point list — wrap as a single contour.
    if isinstance(profile, Face):
        return _polygons_from_face(profile, tolerance=tolerance)
    if isinstance(profile, Compound):
        polygons: list[np.ndarray] = []
        for face in profile.faces():
            polygons.extend(_polygons_from_face(face, tolerance=tolerance))
        return polygons
    if isinstance(profile, Shape):
        # A bare 2-D Shape that is neither Face nor Compound — try to read its
        # faces (covers Sketch-shaped things that bypass the Compound type).
        faces = profile.faces() if hasattr(profile, "faces") else []
        if faces:
            polygons = []
            for face in faces:
                polygons.extend(_polygons_from_face(face, tolerance=tolerance))
            return polygons
        raise TypeError(
            f"to_cross_section: build123d Shape {type(profile).__name__} carries "
            "no Faces — pass a Sketch, a Face, or a list of (x, y) points."
        )
    if isinstance(profile, Iterable):
        polygon = _polygon_from_points(profile)
        return [polygon] if polygon is not None else []
    raise TypeError(
        f"to_cross_section: unsupported profile type {type(profile).__name__}; "
        "expected a build123d Sketch, Face, Compound, or a list of (x, y) points."
    )


def _polygons_from_face(face: Face, *, tolerance: float) -> list[np.ndarray]:
    """Polygonise a build123d Face into outer + hole contours.

    Args:
        face (Face): a build123d 2-D face.
        tolerance (float): linear deflection for curved edges.

    Returns:
        list[np.ndarray]: outer-wire contour first, then every inner-wire
        (hole) contour, each as an ``(N, 2)`` array.
    """
    polygons: list[np.ndarray] = []
    outer = _polygon_from_wire(face.outer_wire(), tolerance=tolerance)
    if outer is not None:
        polygons.append(outer)
    for hole in face.inner_wires():
        inner = _polygon_from_wire(hole, tolerance=tolerance)
        if inner is not None:
            polygons.append(inner)
    return polygons


def _polygon_from_wire(wire: Wire, *, tolerance: float) -> np.ndarray | None:
    """Polygonise a single wire into an ``(N, 2)`` closed contour.

    Walks the wire end-to-end via :meth:`~build123d.Wire.positions` with a
    deflection budget — straight edges yield two-point segments, curved edges
    (arcs, splines) yield point sequences within ``tolerance``. The wire's
    own walker handles edge ordering and orientation, so seam-point duplicates
    do not arise.

    Args:
        wire (Wire): a build123d wire (closed or open — open wires are still
            taken as polygons, the caller is responsible for closure).
        tolerance (float): linear deflection for curved edges.

    Returns:
        np.ndarray | None: the polygon as an ``(N, 2)`` array, or None when
        the wire has fewer than three distinct vertices.
    """
    try:
        sampled = wire.positions(deflection=tolerance)
    except (RuntimeError, ValueError):
        # Fall back to vertex-level sampling for a wire whose deflection
        # walker fails (e.g. a degenerate single-vertex loop).
        sampled = [vertex.center() for vertex in wire.vertices()]
    points = [(float(vector.X), float(vector.Y)) for vector in sampled]
    # Drop a duplicate closing vertex (the polygon constructor closes
    # implicitly). Clipper2 tolerates duplicates but a clean polygon is nicer.
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return None
    return np.asarray(points, dtype=np.float64)


def _polygon_from_points(
    points: Iterable[Sequence[float]],
) -> np.ndarray | None:
    """Wrap a raw ``[(x, y), ...]`` iterable as an ``(N, 2)`` contour.

    Args:
        points: an iterable of ``(x, y)`` (or longer-tuple) coordinates;
            extra coordinates beyond the first two are ignored.

    Returns:
        np.ndarray | None: the polygon as an ``(N, 2)`` array, or None when
        fewer than three distinct vertices were given.

    Raises:
        ValueError: if a point has fewer than two coordinates.
    """
    array_points: list[tuple[float, float]] = []
    for point in points:
        coords = list(point)
        if len(coords) < 2:
            raise ValueError(
                "to_cross_section: every point needs at least (x, y) coordinates"
            )
        array_points.append((float(coords[0]), float(coords[1])))
    if len(array_points) >= 2 and array_points[0] == array_points[-1]:
        array_points = array_points[:-1]
    if len(array_points) < 3:
        return None
    return np.asarray(array_points, dtype=np.float64)
