"""
build123d mesh

name: mesh_part.py

desc:

The :class:`MeshPart` value type and the free-function CSG API (:func:`mesh_fuse`,
:func:`mesh_cut`, :func:`mesh_intersect`) — the Phase 0 surface of the
``build123d[manifold]`` extra.

:class:`MeshPart` is a thin wrapper around a ``manifold3d.Manifold``. It is a
*standalone value type*, deliberately **not** a :class:`~build123d.Shape`
subclass: a ``Shape`` is by contract an exact-BREP entity (analytic faces,
fillets, STEP export), and a faceted mesh is none of those things. Crossing the
mesh↔BREP boundary is therefore always an explicit, named verb
(:meth:`MeshPart.from_part`, :meth:`MeshPart.to_solid`) — never an implicit
coercion.

The free functions are the primary surface: they accept any mix of build123d
``Shape`` and :class:`MeshPart` operands, tessellate the ``Shape`` operands
under the hood, and return a :class:`MeshPart` so the user stays in fast mesh
space across a CSG chain and pays the BREP bake at most once, at the end.

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

import struct
from typing import TYPE_CHECKING, Sequence, Union

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]
from OCP.Bnd import Bnd_Box

from build123d.geometry import BoundBox
from build123d.topology import Compound, Part, Shape, Solid

from .bridge import shape_to_manifold

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

if TYPE_CHECKING:  # pragma: no cover
    from os import PathLike

# A free-function / operator operand: either a build123d shape or a MeshPart.
MeshOperand = Union[Shape, "MeshPart"]


class MeshPart:
    """A ``manifold3d``-backed 3D mesh body.

    A sibling of build123d's :class:`~build123d.Part` — **not** a
    :class:`~build123d.Shape` subclass. Booleans on a :class:`MeshPart` are fast
    and robust (``manifold3d`` guarantees a watertight, 2-manifold result), but
    the geometry is *faceted*: curves and fillets are lost. Convert to an exact
    BREP :class:`~build123d.Solid` explicitly with :meth:`to_solid`.

    This is the Phase 0 minimal wrapper: construction, queries, the explicit
    BREP bake, and STL export. Operators, primitives, hull, and face-identity
    selectors are later phases.
    """

    __slots__ = ("_manifold",)

    # ---- Constructors ----

    def __init__(self, manifold: m3d.Manifold):
        """Wrap a ``manifold3d.Manifold`` directly.

        Usually one of the classmethod constructors (:meth:`from_part`,
        :meth:`from_mesh`) is more convenient.

        Args:
            manifold (manifold3d.Manifold): the underlying manifold.

        Raises:
            TypeError: if ``manifold`` is not a ``manifold3d.Manifold``.
        """
        if not isinstance(manifold, m3d.Manifold):
            raise TypeError(
                f"MeshPart expects a manifold3d.Manifold, got {type(manifold)}"
            )
        self._manifold = manifold

    @classmethod
    def from_part(
        cls,
        shape: Shape,
        *,
        linear_tolerance: float = 0.1,
        angular_tolerance: float = 0.2,
    ) -> MeshPart:
        """Build a MeshPart from a build123d Shape by tessellation.

        This is the BREP → mesh boundary (the OUT leg). It is reasonably cheap
        but *lossy*: exact curves become facets at the given tolerance.

        Args:
            shape (Shape): a build123d 3D shape.
            linear_tolerance (float, optional): absolute linear deflection of
                the tessellation. Defaults to 0.1.
            angular_tolerance (float, optional): angular deflection in radians.
                Defaults to 0.2.

        Returns:
            MeshPart: a guaranteed-manifold mesh body.
        """
        return cls(
            shape_to_manifold(
                shape,
                linear_tolerance=linear_tolerance,
                angular_tolerance=angular_tolerance,
            )
        )

    @classmethod
    def from_mesh(
        cls,
        vertices: Sequence[Sequence[float]] | np.ndarray,
        triangles: Sequence[Sequence[int]] | np.ndarray,
    ) -> MeshPart:
        """Build a MeshPart from raw vertex/triangle arrays.

        Wraps a raw triangle mesh — for example an imported STL — into a
        guaranteed-manifold body. The mesh must already be a closed, oriented,
        indexed 2-manifold; the manifold status is asserted after construction.

        Args:
            vertices: ``(N, 3)`` array of vertex coordinates.
            triangles: ``(M, 3)`` array of triangle vertex indices, wound
                counter-clockwise for outward normals.

        Returns:
            MeshPart: a guaranteed-manifold mesh body.

        Raises:
            ValueError: if the arrays are mis-shaped or the mesh does not import
                as a valid manifold.
        """
        vertex_array = np.ascontiguousarray(vertices, dtype=np.float64)
        triangle_array = np.ascontiguousarray(triangles, dtype=np.uint64)
        if vertex_array.ndim != 2 or vertex_array.shape[1] != 3:
            raise ValueError("vertices must be an (N, 3) array")
        if triangle_array.ndim != 2 or triangle_array.shape[1] != 3:
            raise ValueError("triangles must be an (M, 3) array")

        mesh = m3d.Mesh64(vert_properties=vertex_array, tri_verts=triangle_array)
        manifold = m3d.Manifold(mesh)
        if manifold.status() != m3d.Error.NoError:
            raise ValueError(
                f"Raw mesh is not a valid manifold: {manifold.status()}. It "
                "must be a closed, oriented, 2-manifold triangle mesh."
            )
        return cls(manifold)

    # ---- Properties ----

    @property
    def volume(self) -> float:
        """The signed volume of this mesh body (forces lazy evaluation)."""
        return self._manifold.volume()

    @property
    def area(self) -> float:
        """The surface area of this mesh body."""
        return self._manifold.surface_area()

    @property
    def is_valid(self) -> bool:
        """True if the manifold is a non-empty, error-free body."""
        return (
            not self._manifold.is_empty()
            and self._manifold.status() == m3d.Error.NoError
        )

    @property
    def manifold(self) -> m3d.Manifold:
        """The underlying ``manifold3d.Manifold`` (escape hatch).

        Use this to reach ``manifold3d`` features not yet surfaced by
        :class:`MeshPart`.
        """
        return self._manifold

    # ---- Queries ----

    def bounding_box(self) -> BoundBox:
        """The axis-aligned bounding box of this mesh body.

        Returns:
            BoundBox: a build123d :class:`~build123d.BoundBox`.
        """
        x_min, y_min, z_min, x_max, y_max, z_max = self._manifold.bounding_box()
        box = Bnd_Box()
        box.Update(x_min, y_min, z_min, x_max, y_max, z_max)
        return BoundBox(box)

    def to_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(vertices, triangles)`` as numpy arrays — cheap, no bake.

        Returns:
            tuple[np.ndarray, np.ndarray]: ``(N, 3)`` float vertices and
            ``(M, 3)`` int triangle indices.
        """
        mesh = self._manifold.to_mesh()
        vertices = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
        triangles = np.asarray(mesh.tri_verts, dtype=np.int64)
        return vertices, triangles

    # ---- The explicit BREP boundary ----

    def to_solid(self, *, unify_coplanar: bool = False) -> Solid | Compound:
        """Bake this mesh body into a build123d BREP ``Solid``.

        This is the mesh → BREP boundary (the IN leg). It is **expensive** and
        the result is *faceted*: one planar face per triangle, no analytic
        surfaces. Do all CSG in mesh space first and bake once, at the end.

        Args:
            unify_coplanar (bool, optional): when True, merge coplanar facets
                into single faces (``ShapeUpgrade_UnifySameDomain``), collapsing
                the facet explosion. Run it when the baked solid is user-facing;
                skip it for throwaway intermediates. Defaults to False.

        Returns:
            Solid | Compound: a faceted build123d solid (a :class:`Compound`
            when the mesh has several disjoint bodies).

        Raises:
            ValueError: if this MeshPart is empty.
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot bake an empty MeshPart to a Solid")
        vertices, triangles = self.to_arrays()
        solid = Solid.from_mesh(vertices, triangles, fix=False)
        if unify_coplanar:
            solid = solid.clean()
        return solid

    def to_part(self) -> Part:
        """Bake this mesh body into a build123d ``Part``.

        Same cost as :meth:`to_solid`; use this when downstream code expects the
        algebra API's :class:`~build123d.Part` type.

        Returns:
            Part: a faceted build123d Part.
        """
        baked = self.to_solid()
        solids = baked.solids() if isinstance(baked, Compound) else [baked]
        return Part(solids)

    # ---- Export ----

    def export_stl(self, path: str | PathLike, *, ascii_format: bool = False) -> bool:
        """Export this mesh body to an STL file — cheap, no BREP bake.

        Writes the manifold's triangles straight to disk. There is no BREP
        conversion and no third-party dependency: the STL is serialized directly
        from the vertex/triangle arrays, so this works with only the ``manifold``
        extra installed.

        Args:
            path: destination file path.
            ascii_format (bool, optional): write ASCII STL instead of binary.
                Defaults to False.

        Returns:
            bool: True on success.

        Raises:
            ValueError: if this MeshPart is empty.
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot export an empty MeshPart")
        vertices, triangles = self.to_arrays()
        if ascii_format:
            _write_ascii_stl(path, vertices, triangles)
        else:
            _write_binary_stl(path, vertices, triangles)
        return True

    # ---- Dunder ----

    def __repr__(self) -> str:
        manifold = self._manifold
        return (
            f"MeshPart(triangles={manifold.num_tri()}, "
            f"vertices={manifold.num_vert()}, "
            f"volume={manifold.volume():.3f})"
        )


# ---- Operand coercion ----


def _to_manifold(operand: MeshOperand) -> m3d.Manifold:
    """Coerce a free-function operand into a ``manifold3d.Manifold``.

    A :class:`MeshPart` yields its manifold directly; a build123d
    :class:`~build123d.Shape` is tessellated and welded (the OUT leg).

    Args:
        operand: a build123d Shape or a MeshPart.

    Returns:
        manifold3d.Manifold: the operand in mesh space.

    Raises:
        TypeError: if ``operand`` is neither a Shape nor a MeshPart.
    """
    if isinstance(operand, MeshPart):
        return operand.manifold
    if isinstance(operand, Shape):
        return shape_to_manifold(operand)
    raise TypeError(f"Expected a build123d Shape or a MeshPart, got {type(operand)}")


def _check_result(manifold: m3d.Manifold, operation: str) -> MeshPart:
    """Wrap a boolean result, raising on a non-``NoError`` status.

    Args:
        manifold: the manifold produced by a boolean.
        operation: a human-readable operation name for the error message.

    Returns:
        MeshPart: the wrapped result.

    Raises:
        ValueError: if the manifold carries an error status.
    """
    if manifold.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh {operation} produced an invalid manifold: " f"{manifold.status()}"
        )
    return MeshPart(manifold)


# ---- Free-function CSG API ----


def mesh_fuse(*shapes: MeshOperand) -> MeshPart:
    """Fast union of any mix of build123d Shapes and MeshParts.

    ``Shape`` operands are tessellated under the hood. The N-way union is
    resolved in a single ``manifold3d`` ``batch_boolean`` pass — materially
    faster than folding ``+`` in Python.

    Args:
        *shapes: two or more build123d Shapes / MeshParts.

    Returns:
        MeshPart: the union, in mesh space.

    Raises:
        ValueError: if no operand is given.
    """
    if not shapes:
        raise ValueError("mesh_fuse needs at least one operand")
    manifolds = [_to_manifold(shape) for shape in shapes]
    result = m3d.Manifold.batch_boolean(manifolds, m3d.OpType.Add)
    return _check_result(result, "fuse")


def mesh_cut(base: MeshOperand, *tools: MeshOperand) -> MeshPart:
    """Fast difference: subtract every tool from ``base``.

    ``base`` and ``tools`` may be any mix of build123d Shapes and MeshParts;
    ``Shape`` operands are tessellated under the hood. The subtraction is
    resolved in a single ``manifold3d`` ``batch_boolean`` pass.

    Args:
        base: the shape / mesh to subtract from.
        *tools: shapes / meshes to subtract.

    Returns:
        MeshPart: the difference, in mesh space.
    """
    base_manifold = _to_manifold(base)
    if not tools:
        return MeshPart(base_manifold)
    manifolds = [base_manifold] + [_to_manifold(tool) for tool in tools]
    result = m3d.Manifold.batch_boolean(manifolds, m3d.OpType.Subtract)
    return _check_result(result, "cut")


def mesh_intersect(*shapes: MeshOperand) -> MeshPart:
    """Fast intersection of any mix of build123d Shapes and MeshParts.

    ``Shape`` operands are tessellated under the hood. The N-way intersection is
    resolved in a single ``manifold3d`` ``batch_boolean`` pass.

    Args:
        *shapes: two or more build123d Shapes / MeshParts.

    Returns:
        MeshPart: the intersection, in mesh space.

    Raises:
        ValueError: if no operand is given.
    """
    if not shapes:
        raise ValueError("mesh_intersect needs at least one operand")
    manifolds = [_to_manifold(shape) for shape in shapes]
    result = m3d.Manifold.batch_boolean(manifolds, m3d.OpType.Intersect)
    return _check_result(result, "intersect")


# ---- STL serialization (dependency-free) ----


def _triangle_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Return the unit normal of every triangle.

    Args:
        vertices: ``(N, 3)`` vertex coordinates.
        triangles: ``(M, 3)`` triangle vertex indices.

    Returns:
        np.ndarray: ``(M, 3)`` unit normals; a degenerate (zero-area) triangle
        yields a zero normal.
    """
    corners = vertices[triangles]  # (M, 3, 3)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


def _write_binary_stl(
    path: str | PathLike, vertices: np.ndarray, triangles: np.ndarray
) -> None:
    """Write a binary STL file straight from vertex/triangle arrays.

    Args:
        path: destination file path.
        vertices: ``(N, 3)`` vertex coordinates.
        triangles: ``(M, 3)`` triangle vertex indices.
    """
    corners = vertices[triangles].astype(np.float32)  # (M, 3, 3)
    normals = _triangle_normals(vertices, triangles).astype(np.float32)
    # The binary-STL 50-byte facet record: normal, 3 corners, 2-byte attribute.
    record = np.dtype([("n", "<f4", 3), ("c", "<f4", (3, 3)), ("attr", "<u2")])
    facets = np.zeros(len(triangles), dtype=record)
    facets["n"] = normals
    facets["c"] = corners
    with open(path, "wb") as stl_file:
        stl_file.write(b"build123d MeshPart (binary STL)".ljust(80, b"\0"))
        stl_file.write(struct.pack("<I", len(triangles)))
        stl_file.write(facets.tobytes())


def _write_ascii_stl(
    path: str | PathLike, vertices: np.ndarray, triangles: np.ndarray
) -> None:
    """Write an ASCII STL file straight from vertex/triangle arrays.

    Args:
        path: destination file path.
        vertices: ``(N, 3)`` vertex coordinates.
        triangles: ``(M, 3)`` triangle vertex indices.
    """
    corners = vertices[triangles]  # (M, 3, 3)
    normals = _triangle_normals(vertices, triangles)
    lines = ["solid build123d_MeshPart"]
    for normal, triangle in zip(normals, corners):
        lines.append(f"  facet normal {normal[0]:.6e} {normal[1]:.6e} {normal[2]:.6e}")
        lines.append("    outer loop")
        for corner in triangle:
            lines.append(
                f"      vertex {corner[0]:.6e} {corner[1]:.6e} {corner[2]:.6e}"
            )
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid build123d_MeshPart")
    with open(path, "w", encoding="ascii") as stl_file:
        stl_file.write("\n".join(lines) + "\n")
