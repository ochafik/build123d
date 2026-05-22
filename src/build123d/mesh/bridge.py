"""
build123d mesh

name: bridge.py

desc:

The OUT leg of the mesh backend: convert a build123d :class:`~build123d.Shape`
into a ``manifold3d.Manifold``.

The pipeline is ``Shape.tessellate(weld=True)`` → ``manifold3d.Mesh64`` →
``manifold3d.Manifold``. Two facts make the weld mandatory:

* OCCT triangulates each face independently, so a raw tessellation is a vertex
  *soup* — seam vertices are duplicated and the mesh is topologically open
  everywhere (a box yields 24 vertices, not 8).
* ``manifold3d`` merges vertices only per explicit merge vectors; it does not
  weld by distance. An un-welded soup is rejected as ``Error.NotManifold`` and
  silently produces an *empty* boolean result.

``tessellate(weld=True)`` returns the indexed, cross-face-deduplicated mesh this
leg needs. ``Mesh64`` (double precision) is used so OCC's ``double`` vertex
coordinates survive without float32 truncation. After construction the manifold
status is asserted; a non-``NoError`` status raises rather than silently
yielding an empty body.

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

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]

from build123d.topology import Shape

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

# Default tessellation tolerances for mesh CSG. build123d's exporters default to
# a *relative* deflection, which is wrong for CSG where parts of different sizes
# must align on a common grid; an absolute deflection is used instead.
DEFAULT_LINEAR_TOLERANCE = 0.1  # absolute, model units (mm)
DEFAULT_ANGULAR_TOLERANCE = 0.2  # radians (~11 degrees)


def shape_to_manifold(
    shape: Shape,
    *,
    linear_tolerance: float = DEFAULT_LINEAR_TOLERANCE,
    angular_tolerance: float = DEFAULT_ANGULAR_TOLERANCE,
) -> m3d.Manifold:
    """Convert a build123d Shape into a ``manifold3d.Manifold`` (the OUT leg).

    Tessellates the shape with vertex welding enabled, builds a double-precision
    ``Mesh64``, and constructs a ``Manifold``. The manifold's status is checked;
    a non-``NoError`` status raises :class:`ValueError` so an un-watertight
    tessellation cannot silently produce an empty boolean.

    Args:
        shape (Shape): any build123d 3D shape (``Solid``, ``Part``,
            ``Compound``).
        linear_tolerance (float, optional): absolute linear deflection of the
            tessellation, in model units. Defaults to 0.1.
        angular_tolerance (float, optional): angular deflection in radians.
            Defaults to 0.2.

    Returns:
        manifold3d.Manifold: a guaranteed watertight, 2-manifold body.

    Raises:
        ValueError: if the shape tessellates to an empty mesh, or the welded
            mesh does not import as a valid manifold.
    """
    vertices, triangles = shape.tessellate(
        linear_tolerance, angular_tolerance, weld=True, relative=False
    )
    if not vertices or not triangles:
        raise ValueError("Shape tessellated to an empty mesh; cannot build a Manifold")

    vertex_array = np.ascontiguousarray(
        [(v.X, v.Y, v.Z) for v in vertices], dtype=np.float64
    )
    triangle_array = np.ascontiguousarray(triangles, dtype=np.uint64)

    mesh = m3d.Mesh64(vert_properties=vertex_array, tri_verts=triangle_array)
    manifold = m3d.Manifold(mesh)

    status = manifold.status()
    if status != m3d.Error.NoError:
        raise ValueError(
            f"Tessellated shape did not import as a valid manifold: {status}. "
            "The welded mesh is not a closed, oriented 2-manifold; try a finer "
            "linear_tolerance."
        )
    if manifold.is_empty():
        raise ValueError("Tessellated shape produced an empty manifold (zero volume).")
    return manifold
