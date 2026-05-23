"""
build123d mesh

name: _utils.py

desc:

Tiny dependency-free numeric helpers shared by several mesh-backend modules.
Kept private (leading underscore) — these are *internal-shared* utilities, not
part of the mesh sub-package's public surface (cf. :mod:`build123d.mesh`).

The only resident today is :func:`triangle_normals`, which previously lived in
two places (the STL writer in :mod:`build123d.mesh.mesh_part` and the feature-
graph geometry helpers in :mod:`build123d.mesh.feature_edges`); the duplication
tripped pylint's R0801 cross-module similarity check. Both call sites now import
it from here.

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


def triangle_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Return ``(M, 3)`` unit outward normals (one per triangle, CCW winding).

    Args:
        vertices (np.ndarray): ``(N, 3)`` vertex coordinates.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices.

    Returns:
        np.ndarray: ``(M, 3)`` unit normals; a degenerate (zero-area) triangle
        yields a zero normal.
    """
    corners = vertices[triangles]  # (M, 3, 3)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


__all__ = ["triangle_normals"]
