"""
build123d mesh

name: __init__.py

desc:

This sub-package is the optional ``build123d[manifold]`` extra: a fast, robust
mesh-CSG backend built on `manifold3d <https://github.com/elalish/manifold>`_.

build123d's native booleans route through OpenCASCADE's BOPAlgo, which is slow
on many-operand CSG and scales super-linearly. `manifold3d` resolves the same
booleans one to two orders of magnitude faster and *guarantees* a watertight,
2-manifold result. The trade-off is exact geometry: curves and fillets become
facets. This package therefore offers a *separate* mesh value type,
:class:`MeshPart`, and the free functions :func:`mesh_fuse`, :func:`mesh_cut`,
:func:`mesh_intersect` — never an implicit replacement of the OCC kernel.

``manifold3d`` is an *optional* dependency: ``import build123d`` does not import
it, and importing this sub-package without it installed raises a clear,
actionable :class:`ImportError`. Install with::

    pip install 'build123d[manifold]'

Use :func:`is_available` to feature-detect without catching the error.

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

_IMPORT_ERROR_MESSAGE = (
    "build123d's manifold mesh features require the optional 'manifold' extra.\n"
    "Install it with:\n"
    "    pip install 'build123d[manifold]'"
)


def is_available() -> bool:
    """Return True if the optional ``manifold3d`` dependency is importable.

    Lets callers feature-detect the mesh backend without catching an
    :class:`ImportError`.

    Returns:
        bool: True if ``build123d.mesh`` can be used.
    """
    try:
        # pylint: disable=import-outside-toplevel,unused-import
        import manifold3d  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


try:
    import manifold3d as _manifold3d  # type: ignore[import-not-found] # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_IMPORT_ERROR_MESSAGE) from exc

# Imports are deliberately placed after the guarded manifold3d import above so
# that a missing extra raises the actionable ImportError before these run.
# pylint: disable=wrong-import-position
from .bridge import FaceRecord, ResultMesh, SideMap, read_result, shape_to_manifold
from .feature_edges import (
    FeatureChain,
    FeatureChainSelection,
    FeatureEdge,
)
from .fillet import MeshFilletInfeasible, mesh_chamfer, mesh_fillet
from .mesh_part import MeshPart, mesh_cut, mesh_fuse, mesh_intersect
from .ops import (
    mesh_extrude,
    mesh_hull,
    mesh_minkowski,
    mesh_minkowski_difference,
    mesh_offset,
    mesh_revolve,
    mesh_shell,
)
from .recovery import RecoveredFace, RecoveryResult, recover_brep
from .sketch2d import to_cross_section

__all__ = [
    "FaceRecord",
    "FeatureChain",
    "FeatureChainSelection",
    "FeatureEdge",
    "MeshFilletInfeasible",
    "MeshPart",
    "RecoveredFace",
    "RecoveryResult",
    "ResultMesh",
    "SideMap",
    "is_available",
    "mesh_chamfer",
    "mesh_cut",
    "mesh_extrude",
    "mesh_fillet",
    "mesh_fuse",
    "mesh_hull",
    "mesh_intersect",
    "mesh_minkowski",
    "mesh_minkowski_difference",
    "mesh_offset",
    "mesh_revolve",
    "mesh_shell",
    "read_result",
    "recover_brep",
    "shape_to_manifold",
    "to_cross_section",
]
