"""
build123d mesh

name: mesh_part.py

desc:

The :class:`MeshPart` value type, its CSG operators, and the free-function CSG
API (:func:`mesh_fuse`, :func:`mesh_cut`, :func:`mesh_intersect`) — the surface
of the ``build123d[manifold]`` extra.

:class:`MeshPart` is a wrapper around a ``manifold3d.Manifold`` *plus* the
:class:`~build123d.mesh.bridge.SideMap` provenance carried alongside it. It is a
*standalone value type*, deliberately **not** a :class:`~build123d.Shape`
subclass: a ``Shape`` is by contract an exact-BREP entity (analytic faces,
fillets, STEP export), and a faceted mesh is none of those things. Crossing the
mesh↔BREP boundary is therefore always an explicit, named verb
(:meth:`MeshPart.from_part`, :meth:`MeshPart.to_solid`) — never an implicit
coercion.

CSG happens two ways, both returning a :class:`MeshPart` so the user stays in
fast mesh space across a chain and pays the BREP bake at most once:

* **Operators** — ``+`` / ``-`` / ``&`` (and their in-place forms). A
  ``Shape`` operand is coerced (tessellated + seeded) so
  ``mesh_part - native_part`` and ``native_part + mesh_part`` both work. The
  *mesh operand must be on the left* for ``-`` and ``&`` (see §4.5 of the design
  doc); use the free functions when operand direction needs to be explicit.
* **Free functions** — :func:`mesh_fuse` / :func:`mesh_cut` /
  :func:`mesh_intersect` resolve an N-way boolean in a single ``batch_boolean``
  pass.

Because face ids are globally unique, every boolean simply *merges* the operand
side-maps. A :class:`MeshPart` that carries a non-empty side-map can be baked
back to an **exact, analytic, filletable** B-rep for an all-planar result; one
built from raw arrays (:meth:`from_mesh`) has no provenance and bakes to a
faceted solid.

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
from typing import TYPE_CHECKING, Optional, Sequence, Union

import numpy as np

import manifold3d as m3d  # type: ignore[import-not-found]
from OCP.Bnd import Bnd_Box

from build123d.geometry import BoundBox, Location
from build123d.topology import Compound, Face, Part, Shape, ShapeList, Solid

from ._utils import triangle_normals
from .bridge import SideMap, read_result, shape_to_manifold, synthetic_side_map
from .recovery import recover_brep

# manifold3d is a C extension; pylint cannot introspect its members statically.
# pylint: disable=c-extension-no-member

if TYPE_CHECKING:  # pragma: no cover
    from os import PathLike

    from .feature_edges import FeatureChainSelection
    from .fillet import FilletReport

# A free-function / operator operand: either a build123d shape or a MeshPart.
MeshOperand = Union[Shape, "MeshPart"]


class MeshPart:
    """A ``manifold3d``-backed 3D mesh body with face-identity provenance.

    A sibling of build123d's :class:`~build123d.Part` — **not** a
    :class:`~build123d.Shape` subclass. Booleans on a :class:`MeshPart` are fast
    and robust (``manifold3d`` guarantees a watertight, 2-manifold result), and
    every triangle carries a seeded ``face_id`` tracing it to an input
    :class:`~build123d.Face`. For an all-planar CSG result :meth:`to_solid`
    therefore rebuilds an *exact, analytic, filletable* B-rep; curved geometry is
    recovered faceted.

    A :class:`MeshPart` holds a ``manifold3d.Manifold`` *and* a
    :class:`~build123d.mesh.bridge.SideMap`. Booleans merge the operand
    side-maps. A :class:`MeshPart` built from raw arrays (:meth:`from_mesh`)
    carries an empty side-map and bakes to a faceted solid.
    """

    __slots__ = (
        "_manifold",
        "_side_map",
        "_feature_edges_cache",
        "_last_fillet_report",
    )

    # ---- Constructors ----

    def __init__(self, manifold: m3d.Manifold, side_map: Optional[SideMap] = None):
        """Wrap a ``manifold3d.Manifold`` and its provenance side-map.

        Usually one of the classmethod constructors (:meth:`from_part`,
        :meth:`from_mesh`) or a CSG operator is more convenient.

        Args:
            manifold (manifold3d.Manifold): the underlying manifold.
            side_map (SideMap | None): the ``faceID → provenance`` map. Defaults
                to an empty :class:`~build123d.mesh.bridge.SideMap` (no
                provenance — :meth:`to_solid` then falls back to a faceted bake).

        Raises:
            TypeError: if ``manifold`` is not a ``manifold3d.Manifold``.
        """
        if not isinstance(manifold, m3d.Manifold):
            raise TypeError(
                f"MeshPart expects a manifold3d.Manifold, got {type(manifold)}"
            )
        self._manifold = manifold
        self._side_map = side_map if side_map is not None else SideMap()
        self._feature_edges_cache: Optional["FeatureChainSelection"] = None
        # Populated by :meth:`fillet` / :meth:`chamfer` when ``on_infeasible="skip"``
        # drops one or more chains. ``None`` otherwise (design §8.2 / A4).
        self._last_fillet_report: Optional["FilletReport"] = None

    @classmethod
    def from_part(
        cls,
        shape: Shape,
        *,
        source: str = "shape",
        linear_tolerance: float = 0.1,
        angular_tolerance: float = 0.2,
    ) -> MeshPart:
        """Build a MeshPart from a build123d Shape by per-face tessellation.

        This is the BREP → mesh boundary (the OUT leg). It tessellates the shape
        per :class:`~build123d.Face`, seeds every triangle with a unique
        ``face_id``, and records the originating face's exact surface in a
        :class:`~build123d.mesh.bridge.SideMap`. It is reasonably cheap but
        *lossy*: exact curves become facets at the given tolerance — though the
        seeded provenance lets :meth:`to_solid` recover exact *planar* faces.

        Args:
            shape (Shape): a build123d 3D shape.
            source (str): a name for the shape, recorded on every face record
                for provenance reporting. Defaults to ``"shape"``.
            linear_tolerance (float): absolute linear deflection of the
                tessellation. Defaults to 0.1.
            angular_tolerance (float): angular deflection in radians. Defaults
                to 0.2.

        Returns:
            MeshPart: a guaranteed-manifold mesh body carrying seeded face ids.
        """
        manifold, side_map = shape_to_manifold(
            shape,
            source=source,
            linear_tolerance=linear_tolerance,
            angular_tolerance=angular_tolerance,
        )
        return cls(manifold, side_map)

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

        The resulting :class:`MeshPart` carries a **synthetic** side-map: a raw
        mesh has no analytic provenance, but ``manifold3d`` groups its triangles
        into coplanar regions. Those regions are seeded as synthetic ids (no
        claimed analytic surface), so :meth:`to_solid` recovers **one merged
        planar face per coplanar region** — an imported STL of a cube becomes 6
        faces, not 12 triangles — fitting a plane where the region is planar and
        keeping curved regions faceted. (Use ``to_solid(reconstruct=False)`` for
        the pure one-face-per-triangle bake.)

        Args:
            vertices: ``(N, 3)`` array of vertex coordinates.
            triangles: ``(M, 3)`` array of triangle vertex indices, wound
                counter-clockwise for outward normals.

        Returns:
            MeshPart: a guaranteed-manifold mesh body carrying synthetic
            coplanar-region provenance.

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
        # No input provenance, but manifold3d groups the raw triangles into
        # coplanar regions; seed those as synthetic ids so to_solid merges each
        # planar region into a single face (a cube STL -> 6 faces, not 12).
        seeded, side_map = synthetic_side_map(manifold)
        return cls(seeded, side_map)

    # ---- Faceted primitive constructors ----

    @classmethod
    def box(cls, length: float, width: float, height: float) -> MeshPart:
        """A faceted box — the mesh analogue of :class:`~build123d.Box`.

        Built by tessellating a build123d :class:`~build123d.Box` through the
        seeding bridge, so it matches build123d's centering convention exactly
        (origin-centred) **and** carries a full six-record side-map — a
        :meth:`to_solid` on a box-derived CSG result therefore still recovers
        an exact analytic B-rep.

        Args:
            length (float): box length (X extent).
            width (float): box width (Y extent).
            height (float): box height (Z extent).

        Returns:
            MeshPart: an origin-centred faceted box with seeded provenance.
        """
        # pylint: disable=import-outside-toplevel
        from build123d.objects_part import Box

        return cls.from_part(Box(length, width, height), source="box")

    @classmethod
    def sphere(cls, radius: float) -> MeshPart:
        """A faceted sphere — the mesh analogue of :class:`~build123d.Sphere`.

        Built by tessellating a build123d :class:`~build123d.Sphere` through
        the seeding bridge (origin-centred, like build123d's default). The
        sphere is a single curved face: its side-map holds one record, and
        :meth:`to_solid` recovers it faceted (a curved surface is not analytic
        on a mesh — see :meth:`faces`).

        Args:
            radius (float): sphere radius.

        Returns:
            MeshPart: an origin-centred faceted sphere with seeded provenance.
        """
        # pylint: disable=import-outside-toplevel
        from build123d.objects_part import Sphere

        return cls.from_part(Sphere(radius), source="sphere")

    @classmethod
    def cylinder(cls, radius: float, height: float) -> MeshPart:
        """A faceted cylinder — the mesh analogue of :class:`~build123d.Cylinder`.

        Built by tessellating a build123d :class:`~build123d.Cylinder` through
        the seeding bridge (origin-centred, like build123d's default). The two
        flat caps are planar faces; the lateral surface is one curved face.

        Args:
            radius (float): cylinder radius.
            height (float): cylinder height (Z extent).

        Returns:
            MeshPart: an origin-centred faceted cylinder with seeded
            provenance.
        """
        # pylint: disable=import-outside-toplevel
        from build123d.objects_part import Cylinder

        return cls.from_part(Cylinder(radius, height), source="cylinder")

    @classmethod
    def cone(cls, bottom_radius: float, top_radius: float, height: float) -> MeshPart:
        """A faceted cone — the mesh analogue of :class:`~build123d.Cone`.

        Built by tessellating a build123d :class:`~build123d.Cone` through the
        seeding bridge (origin-centred, like build123d's default). Set
        ``top_radius=0`` for a pointed cone.

        Args:
            bottom_radius (float): radius of the bottom circle.
            top_radius (float): radius of the top circle; may be zero.
            height (float): cone height (Z extent).

        Returns:
            MeshPart: an origin-centred faceted cone with seeded provenance.
        """
        # pylint: disable=import-outside-toplevel
        from build123d.objects_part import Cone

        return cls.from_part(Cone(bottom_radius, top_radius, height), source="cone")

    @classmethod
    def torus(cls, major_radius: float, minor_radius: float) -> MeshPart:
        """A faceted torus — the mesh analogue of :class:`~build123d.Torus`.

        Built by tessellating a build123d :class:`~build123d.Torus` through the
        seeding bridge (origin-centred, like build123d's default).

        Args:
            major_radius (float): major (centre-line) radius.
            minor_radius (float): minor (tube) radius.

        Returns:
            MeshPart: an origin-centred faceted torus with seeded provenance.
        """
        # pylint: disable=import-outside-toplevel
        from build123d.objects_part import Torus

        return cls.from_part(Torus(major_radius, minor_radius), source="torus")

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

    @property
    def side_map(self) -> SideMap:
        """The ``faceID → provenance`` map carried with this mesh body.

        Empty for a :class:`MeshPart` built from raw arrays. Non-empty for one
        built from a build123d shape or by a CSG operator/free function — the
        provenance :meth:`to_solid` uses to rebuild an exact B-rep.
        """
        return self._side_map

    @property
    def last_fillet_report(self) -> Optional["FilletReport"]:
        """The :class:`~build123d.mesh.FilletReport` from the last fillet/chamfer.

        Populated by :meth:`fillet` / :meth:`chamfer` when ``on_infeasible="skip"``
        dropped any chain or corner; ``None`` otherwise (and ``None`` after a
        successful all-feasible call). Carries the dropped chain/corner records,
        the failing constraint, and the request/measurement that triggered the
        skip (design §8.2 / A4). Logged at WARNING level so a skip is never
        completely silent (P3 — never silently mis-answer).
        """
        return self._last_fillet_report

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

    # ---- CSG operators ----

    def __add__(self, other: MeshOperand) -> MeshPart:
        """Union: ``self + other``. Returns a :class:`MeshPart`."""
        return mesh_fuse(self, other)

    def __radd__(self, other: MeshOperand) -> MeshPart:
        """Union with ``self`` on the right: coerces a ``Shape`` left operand."""
        return mesh_fuse(other, self)

    def __iadd__(self, other: MeshOperand) -> MeshPart:
        """In-place union: ``self += other``."""
        return mesh_fuse(self, other)

    def __sub__(self, other: MeshOperand) -> MeshPart:
        """Difference: ``self - other``. Returns a :class:`MeshPart`."""
        return mesh_cut(self, other)

    def __rsub__(self, other: MeshOperand) -> MeshPart:
        """Difference with ``self`` on the right: ``shape - self``.

        Coerces a ``Shape`` left operand so ``native_part - mesh_part`` works.
        """
        return mesh_cut(other, self)

    def __isub__(self, other: MeshOperand) -> MeshPart:
        """In-place difference: ``self -= other``."""
        return mesh_cut(self, other)

    def __and__(self, other: MeshOperand) -> MeshPart:
        """Intersection: ``self & other``. Returns a :class:`MeshPart`."""
        return mesh_intersect(self, other)

    def __rand__(self, other: MeshOperand) -> MeshPart:
        """Intersection with ``self`` on the right: coerces a ``Shape`` left."""
        return mesh_intersect(other, self)

    def __iand__(self, other: MeshOperand) -> MeshPart:
        """In-place intersection: ``self &= other``."""
        return mesh_intersect(self, other)

    # ---- Geometry-generating operations ----

    def hull(self, *others: MeshOperand) -> MeshPart:
        """Convex hull of this body — optionally enveloping further operands.

        ``self.hull()`` is the convex hull of this mesh body alone;
        ``self.hull(a, b)`` is the single hull that envelops ``self`` and every
        extra operand (a ``Shape`` operand is tessellated under the hood).

        A hull synthesises new envelope facets, so **no input ``face_id``
        survives**: the returned :class:`MeshPart` carries an empty side-map and
        :meth:`to_solid` bakes it faceted. See :func:`build123d.mesh.mesh_hull`.

        Args:
            *others: optional extra build123d Shapes / MeshParts to envelop
                together with ``self``.

        Returns:
            MeshPart: the convex hull, with no provenance.
        """
        # pylint: disable=import-outside-toplevel
        from .ops import mesh_hull

        return mesh_hull(self, *others)

    def minkowski(self, other: MeshOperand, *, method: str = "native") -> MeshPart:
        """Minkowski sum of this body with ``other`` — the dilation ``self ⊕ other``.

        Sweeps ``other`` over every point of ``self`` (a sphere over a box
        rounds the box's edges). ``method`` selects the backend — ``"native"``
        (default, handles non-convex) or ``"decompose"`` (the scad2py
        convex-pairs port). See :func:`build123d.mesh.mesh_minkowski`.

        A Minkowski sum synthesises a new shell, so the result carries an empty
        side-map and bakes faceted.

        Args:
            other: the other operand (build123d Shape or MeshPart).
            method (str): ``"native"`` (default) or ``"decompose"``.

        Returns:
            MeshPart: the Minkowski sum, with no provenance.
        """
        # pylint: disable=import-outside-toplevel
        from .ops import mesh_minkowski

        return mesh_minkowski(self, other, method=method)

    def minkowski_difference(self, other: MeshOperand) -> MeshPart:
        """Minkowski difference with ``other`` — the erosion ``self ⊖ other``.

        Erodes ``self`` by sweeping ``other`` across its surface (the inverse
        of :meth:`minkowski`). See
        :func:`build123d.mesh.mesh_minkowski_difference`.

        Args:
            other: the eroding body (build123d Shape or MeshPart).

        Returns:
            MeshPart: the Minkowski difference, with no provenance (may be an
            empty body if ``other`` is larger than ``self``).
        """
        # pylint: disable=import-outside-toplevel
        from .ops import mesh_minkowski_difference

        return mesh_minkowski_difference(self, other)

    def offset(self, amount: float, *, sphere_segments: int = 32) -> MeshPart:
        """3-D offset by ``amount`` — Minkowski with a faceted sphere.

        Outward (``amount > 0``) inflates this body by ``amount``, rounding
        every sharp edge with a sphere of that radius. Inward
        (``amount < 0``) erodes the body by ``|amount|``. Outward is robust;
        inward is **fragile** on a faceted sphere tool — see
        :func:`build123d.mesh.mesh_offset` for the honest-limits caveat.

        The result is synthesised geometry: the returned :class:`MeshPart`
        carries an empty side-map.

        Args:
            amount (float): the offset distance. Positive grows outward,
                negative erodes inward, zero returns a copy of this body.
            sphere_segments (int): segment count for the sphere tool. Higher
                is smoother but slower. Defaults to 32.

        Returns:
            MeshPart: the offset body, with no provenance.
        """
        # pylint: disable=import-outside-toplevel
        from .ops import mesh_offset

        return mesh_offset(self, amount, sphere_segments=sphere_segments)

    def shell(self, thickness: float, *, sphere_segments: int = 32) -> MeshPart:
        """Hollow this body to a wall of ``thickness`` — ``self − offset(−thickness)``.

        Inward-offsets this body by ``thickness`` and subtracts the eroded
        body, leaving a hollow shell. Inherits :meth:`offset`'s honest limit
        on inward erosion: if ``thickness`` exceeds the body's thinnest
        half-feature the inward offset can collapse and this raises. See
        :func:`build123d.mesh.mesh_shell`.

        Args:
            thickness (float): wall thickness. Must be strictly positive.
            sphere_segments (int): segment count for the sphere tool used by
                the inward offset. Defaults to 32.

        Returns:
            MeshPart: the hollow shell, with no provenance.
        """
        # pylint: disable=import-outside-toplevel
        from .ops import mesh_shell

        return mesh_shell(self, thickness, sphere_segments=sphere_segments)

    # ---- Transforms ----

    def translate(self, offset: Sequence[float]) -> MeshPart:
        """Return this mesh body translated by ``offset``.

        faceID *identity* is transform-invariant; the side-map's exact plane
        parameters are moved alongside the mesh so :meth:`to_solid` still
        reconstructs in the new frame.

        Args:
            offset (Sequence[float]): an ``(x, y, z)`` translation vector.

        Returns:
            MeshPart: the translated mesh body.
        """
        vector = np.array([float(c) for c in offset], dtype=np.float64)
        moved = self._manifold.translate(list(vector))
        matrix = np.hstack([np.identity(3), vector.reshape(3, 1)])
        return MeshPart(moved, self._side_map.transformed(matrix))

    def rotate(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> MeshPart:
        """Return this mesh body rotated by Euler angles (degrees).

        The Euler order matches ``manifold3d``: a global-frame X, then Y, then Z
        rotation. faceID identity is transform-invariant; the side-map's exact
        plane parameters are rotated alongside the mesh.

        Args:
            x (float): rotation about the global X axis, in degrees.
            y (float): rotation about the global Y axis, in degrees.
            z (float): rotation about the global Z axis, in degrees.

        Returns:
            MeshPart: the rotated mesh body.
        """
        rotated = self._manifold.rotate([float(x), float(y), float(z)])
        rotation = _euler_rotation_matrix(x, y, z)
        matrix = np.hstack([rotation, np.zeros((3, 1))])
        return MeshPart(rotated, self._side_map.transformed(matrix))

    def scale(self, factor: float | Sequence[float]) -> MeshPart:
        """Return this mesh body scaled by ``factor``.

        faceID identity is transform-invariant; the side-map's exact plane
        parameters are scaled alongside the mesh.

        Args:
            factor (float | Sequence[float]): a uniform scalar, or a per-axis
                ``(sx, sy, sz)`` vector.

        Returns:
            MeshPart: the scaled mesh body.
        """
        if isinstance(factor, (int, float)):
            vector = [float(factor)] * 3
        else:
            vector = [float(c) for c in factor]
        matrix = np.hstack([np.diag(vector), np.zeros((3, 1))])
        return MeshPart(
            self._manifold.scale(vector), self._side_map.transformed(matrix)
        )

    def move(self, location: Location) -> MeshPart:
        """Return this mesh body moved by a build123d :class:`~build123d.Location`.

        Applies the location's full affine transform (rotation + translation).
        faceID identity is transform-invariant; the side-map's exact plane
        parameters are moved alongside the mesh.

        Args:
            location (Location): a build123d Location.

        Returns:
            MeshPart: the moved mesh body.
        """
        transformation = location.wrapped.Transformation()
        matrix = np.array(
            [
                [transformation.Value(row + 1, col + 1) for col in range(4)]
                for row in range(3)
            ],
            dtype=np.float64,
        )
        return MeshPart(
            self._manifold.transform(matrix), self._side_map.transformed(matrix)
        )

    # ---- The explicit BREP boundary ----

    def to_solid(
        self, *, reconstruct: bool = True, unify_coplanar: bool = False
    ) -> Solid | Compound:
        """Bake this mesh body into a build123d BREP ``Solid``.

        This is the mesh → BREP boundary (the IN leg). Two paths:

        * **faceID-grouped reconstruction** (the default, when a non-empty
          side-map is present): output triangles are grouped by their seeded
          ``face_id``; planar seeded groups rebuild **exact analytic faces**
          on the known ``Geom_Plane``; curved seeded groups are recovered
          faceted; **unseeded** ids (from ``hull`` / ``minkowski`` /
          ``level_set`` / ``from_mesh`` operands mixed in) are recovered as
          anonymous faceted patches. For an all-planar CSG result this
          yields a bit-exact, *filletable* B-rep; for a mixed-provenance
          result the seeded portion stays exact and the unseeded portion is
          faceted but present (see
          :attr:`~build123d.mesh.recovery.RecoveryResult.n_unseeded_faceted`).
        * **faceted bake** (the fallback, or when ``reconstruct=False``): direct
          shell assembly — one planar face per triangle, no analytic surfaces.

        Args:
            reconstruct (bool): when ``True`` (default) use faceID-grouped
                reconstruction if a side-map is present. Set ``False`` to force
                the pure faceted path.
            unify_coplanar (bool): when ``True``, merge coplanar facets of the
                faceted path into single faces (``ShapeUpgrade_UnifySameDomain``).
                Only affects the faceted bake. Defaults to False.

        Returns:
            Solid | Compound: a build123d solid (a :class:`Compound` when the
            mesh has several disjoint bodies).

        Raises:
            ValueError: if this MeshPart is empty.
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot bake an empty MeshPart to a Solid")

        if reconstruct and self._side_map:
            result_mesh = read_result(self._manifold)
            recovered = recover_brep(result_mesh, self._side_map)
            if (
                recovered.solid is not None
                and isinstance(recovered.solid, (Solid, Compound))
                and recovered.is_valid
            ):
                return recovered.solid
            # Recovery did not yield a valid closed solid. With shared-topology
            # recovery (one TopoDS_Vertex/Edge per mesh vertex index, shared
            # across planar faces and faceted patches) a mixed-provenance body
            # — exact-planar seeded faces abutting faceted hull / Minkowski /
            # unseeded regions — is now valid by construction, so this fallback
            # is *not* expected to trigger for bp10-style mixed input. It
            # remains a safety net for genuinely-unrecoverable cases: a bare
            # Shell (no closed solid built), or a degenerate mesh OCCT cannot
            # close. Falling through to the faceted bake then still gives the
            # caller a guaranteed-valid Solid; callers that want the
            # partially-exact recovered body can invoke ``recover_brep()``
            # directly and inspect the ``RecoveryResult``.

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
            Part: a build123d Part.
        """
        baked = self.to_solid()
        solids = baked.solids() if isinstance(baked, Compound) else [baked]
        return Part(solids)

    # ---- Face-identity selectors ----

    def faces(self) -> ShapeList[Face]:
        """Return the recovered build123d :class:`~build123d.Face`s of this body.

        Runs the faceID-grouped B-rep recovery (the same path
        :meth:`to_solid` uses) and returns its faces as a real build123d
        :class:`~build123d.topology.ShapeList` — so ``sort_by``, ``filter_by``
        and ``group_by`` all work on the result. Planar seeded ids become
        **exact analytic** :class:`~build123d.Face`s on the known
        ``Geom_Plane``; curved ids are returned as faceted triangle patches.

        For an all-planar :class:`MeshPart` (a box, or any CSG of boxes) this
        returns exactly the faces a native BREP boolean would — every one an
        analytic ``GeomType.PLANE``.

        **Curved geometry is provenance-only (design §6.5, P3).** A curved
        region's facet patches are *not* analytic; a curved-analytic selector
        run on them — ``filter_by(GeomType.CYLINDER)``, a ``fillet`` — would
        mis-answer. :meth:`faces` returns the patches so directional and planar
        selectors keep working, but does not pretend they are analytic. The one
        forbidden outcome is a silent wrong answer; see :meth:`analytic_faces`.

        Returns:
            ShapeList[Face]: every recovered face. Planar faces are analytic;
            curved regions are faceted triangle patches.

        Raises:
            ValueError: if this MeshPart is empty, or carries no side-map (a
                raw mesh has no face provenance to group on — use
                :meth:`to_solid` with ``reconstruct=False`` for a faceted
                bake).
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot recover faces of an empty MeshPart")
        if not self._side_map:
            raise ValueError(
                "MeshPart.faces() needs a non-empty SideMap; a MeshPart built "
                "from raw arrays (or by hull/minkowski) carries no face "
                "provenance. Use to_solid(reconstruct=False) for a faceted "
                "bake instead."
            )
        recovery = recover_brep(read_result(self._manifold), self._side_map)
        faces: list[Face] = []
        for recovered in recovery.recovered_faces:
            faces.extend(recovered.faces)
        return ShapeList(faces)

    def analytic_faces(self) -> ShapeList[Face]:
        """Return the recovered faces, but only if **every** one is analytic.

        The strict counterpart of :meth:`faces` and the enforcement point of
        the design's P3 contract (§6.5): a curved region of a mesh body
        recovers *faceted*, not analytic, so a curved-analytic selector run on
        it (``filter_by(GeomType.CYLINDER)``, a ``fillet``) would silently
        mis-answer. Rather than hand back faceted patches that *look* like faces
        to such a selector, this method **raises** when any curved region is
        present — a clear error instead of a silent wrong answer.

        Use this when downstream code will apply analytic selectors or fillets
        and an all-planar result is required; use :meth:`faces` when faceted
        patches for curved regions are acceptable.

        Returns:
            ShapeList[Face]: every recovered face, all guaranteed analytic
            (``GeomType.PLANE``).

        Raises:
            ValueError: if this MeshPart is empty, carries no side-map, or
                contains any curved (non-planar) region — a curved mesh region
                cannot be recovered as an analytic face.
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot recover faces of an empty MeshPart")
        if not self._side_map:
            raise ValueError(
                "MeshPart.analytic_faces() needs a non-empty SideMap; a "
                "MeshPart built from raw arrays (or by hull/minkowski) carries "
                "no face provenance."
            )
        recovery = recover_brep(read_result(self._manifold), self._side_map)
        if recovery.n_faceted_curved:
            raise ValueError(
                "MeshPart.analytic_faces() refuses to return faces: this body "
                f"has {recovery.n_faceted_curved} curved region(s) that "
                "recover only as faceted patches, not analytic faces. A "
                "curved-analytic selector (filter_by(GeomType.CYLINDER), "
                "fillet) on a mesh-origin curved region would mis-answer. Use "
                "MeshPart.faces() to accept faceted patches for curved regions."
            )
        faces: list[Face] = []
        for recovered in recovery.recovered_faces:
            faces.extend(recovered.faces)
        return ShapeList(faces)

    # ---- Feature-edge chain selection (Phase A3a) ----

    def feature_edges(self) -> "FeatureChainSelection":
        """Return this mesh body's full faceID feature-chain graph.

        Builds the feature-edge graph from this mesh's seeded ``face_id``s and
        groups feature edges into ordered chains/loops keyed by their
        ``(lo, hi)`` faceID pair (design §1.3). The returned
        :class:`~build123d.mesh.FeatureChainSelection` is the selection unit
        consumed by :meth:`chamfer` (and, in later phases, ``.fillet``); its
        chainable filters (``.convex()``, ``.between(source_a, source_b)``,
        ``.closed()``, …) let users name edges without seeing a triangle index.

        Every chain carries its ``convexity_class`` and per-vertex
        ``vertex_kinds`` tags so the chamfer pre-flight can refuse infeasible
        configurations loudly (P3 — see
        :class:`~build123d.mesh.MeshFilletInfeasible`).

        Returns:
            FeatureChainSelection: a fluent collection of every feature chain
            on this mesh.

        Raises:
            ValueError: if this MeshPart is empty.
        """
        # pylint: disable=import-outside-toplevel
        from .feature_edges import FeatureChainSelection, build_feature_graph

        if self._manifold.is_empty():
            raise ValueError("Cannot extract feature edges from an empty MeshPart")
        if self._feature_edges_cache is not None:
            return self._feature_edges_cache
        result_mesh = read_result(self._manifold)
        chains = build_feature_graph(
            result_mesh.vertices, result_mesh.triangles, result_mesh.face_id
        )
        selection = FeatureChainSelection(
            chains,
            result_mesh.vertices,
            result_mesh.triangles,
            result_mesh.face_id,
            self._side_map,
        )
        self._feature_edges_cache = selection
        return selection

    def chamfer(
        self,
        edges: object,
        size: object,
        *,
        on_infeasible: str = "raise",
    ) -> "MeshPart":
        """Apply a faceted chamfer to ``edges``.

        Method form of :func:`build123d.mesh.mesh_chamfer`. The selection
        argument follows design §8.1: a :class:`FeatureChainSelection` (from
        :meth:`feature_edges`), a single :class:`FeatureChain`, or any iterable
        of :class:`FeatureChain`.

        ``size`` may be a scalar ``float`` (uniform chamfer leg) or, per A4's
        variable-radius extension, a callable ``(chain, vertex_index) -> float``
        evaluated at every chain vertex (design §2.4 deferred this to A4).
        The construction is one **swept tool per chain** (design §3) — convex
        chains have their wedge subtracted, concave chains have it added; both
        directions are batched into a single ``manifold3d`` boolean.

        Over-size requests, mixed corners, and ``k > 6`` corners **raise**
        :class:`~build123d.mesh.MeshFilletInfeasible` by default (P3 — never
        silently clamp). Pass ``on_infeasible="skip"`` (design §8.2 / A4) to
        drop the offending chain or corner from the operation; the dropped
        items are reported on :attr:`last_fillet_report`.

        Args:
            edges: chains to chamfer — a
                :class:`~build123d.mesh.FeatureChainSelection`, a single
                :class:`~build123d.mesh.FeatureChain`, or any iterable of
                :class:`~build123d.mesh.FeatureChain`. Chains must originate
                from this mesh's own chain graph.
            size: chamfer leg length — a scalar ``float`` (> 0) or a callable
                ``(chain, vertex_index) -> float`` returning a positive size at
                every chain vertex.
            on_infeasible (str): ``"raise"`` (default — P3 contract) or
                ``"skip"`` (A4). ``"clamp"`` is not shipped — design §5.4.

        Returns:
            MeshPart: the chamfered mesh body. When ``on_infeasible="skip"``
            dropped any chain/corner, the returned mesh's
            :attr:`last_fillet_report` carries the structured drop list.

        Raises:
            ValueError: if ``size <= 0`` (scalar), this MeshPart is empty, or
                ``on_infeasible`` is not one of ``"raise"`` / ``"skip"``.
            MeshFilletInfeasible: if any feasibility constraint fails and
                ``on_infeasible="raise"``.
            TypeError: if ``edges`` is none of the accepted shapes.
        """
        # pylint: disable=import-outside-toplevel
        from .fillet import mesh_chamfer

        return mesh_chamfer(
            self,
            edges,  # type: ignore[arg-type]
            size,  # type: ignore[arg-type]
            on_infeasible=on_infeasible,  # type: ignore[arg-type]
        )

    def fillet(
        self,
        edges: object,
        radius: object,
        *,
        segments: int = 8,
        on_infeasible: str = "raise",
    ) -> "MeshPart":
        """Apply a faceted fillet to ``edges``.

        Method form of :func:`build123d.mesh.mesh_fillet`. The selection
        argument follows design §8.1: a :class:`FeatureChainSelection` (from
        :meth:`feature_edges`), a single :class:`FeatureChain`, or any iterable
        of :class:`FeatureChain`.

        ``radius`` may be a scalar ``float`` (uniform rolling-ball radius along
        every selected chain) or, per A4's variable-radius extension, a
        callable ``(chain, vertex_index) -> float`` evaluated at every chain
        vertex — the per-vertex frame's profile then carries that vertex's
        radius and the ribbon loft interpolates between adjacent rings (design
        §2.4 deferred this to A4). For a scalar input the result is bit-identical
        to A3b's scalar path (regression-tested).

        The cross-section is a ``segments``-faceted quarter-disc tangent to
        both adjacent faces; the construction is one **swept tool per chain**
        (design §3) using the same per-vertex frame + ribbon-loft substrate as
        A3a/A3b. Convex chains have their tool subtracted (round the edge);
        concave chains have it unioned (fill the channel). Mixed convex/concave
        chains are split at the sign flip into single-sign sub-runs (design
        §3.4 / §8.3); A3c's setback + spherical-patch corner blend rounds
        every multi-chain corner.

        Over-size requests, mixed corners, and ``k > 6`` corners **raise**
        :class:`~build123d.mesh.MeshFilletInfeasible` by default (P3 — never
        silently clamp). Pass ``on_infeasible="skip"`` (design §8.2 / A4) to
        drop the offending chain or corner from the operation; the dropped
        items are reported on :attr:`last_fillet_report`.

        Args:
            edges: chains to fillet — a
                :class:`~build123d.mesh.FeatureChainSelection`, a single
                :class:`~build123d.mesh.FeatureChain`, or any iterable of
                :class:`~build123d.mesh.FeatureChain`. Chains must originate
                from this mesh's own chain graph.
            radius: rolling-ball radius — a scalar ``float`` (> 0) or a
                callable ``(chain, vertex_index) -> float`` returning a
                positive radius at every chain vertex.
            segments (int): arc facet count for the cross-section (default 8).
            on_infeasible (str): ``"raise"`` (default — P3 contract) or
                ``"skip"`` (A4). ``"clamp"`` is not shipped — design §5.4.

        Returns:
            MeshPart: the filleted mesh body. When ``on_infeasible="skip"``
            dropped any chain/corner, the returned mesh's
            :attr:`last_fillet_report` carries the structured drop list.

        Raises:
            ValueError: if ``radius <= 0`` (scalar), ``segments < 1``, this
                MeshPart is empty, or ``on_infeasible`` is not one of
                ``"raise"`` / ``"skip"``.
            MeshFilletInfeasible: if any feasibility constraint fails and
                ``on_infeasible="raise"``.
            TypeError: if ``edges`` is none of the accepted shapes.
        """
        # pylint: disable=import-outside-toplevel
        from .fillet import mesh_fillet

        return mesh_fillet(
            self,
            edges,  # type: ignore[arg-type]
            radius,  # type: ignore[arg-type]
            segments=segments,
            on_infeasible=on_infeasible,  # type: ignore[arg-type]
        )

    # ---- Source-filtered selection ----

    def faces_from(self, source: str) -> ShapeList[Face]:
        """Return the recovered faces that originate from input shape ``source``.

        Filters :meth:`faces` by :class:`~build123d.mesh.bridge.SideMap`
        provenance: every recovered face whose seeded ``face_id`` traces back to
        an input shape named ``source`` (the ``source`` argument of
        :meth:`from_part` or a primitive constructor). **This includes
        boolean-created cut faces** — a cut face inherits the id of the input
        face it was cut from — which has no BREP equivalent (design §6.4): a
        genuinely new selector dimension.

        Args:
            source (str): the source-shape name to filter on — matched against
                :attr:`~build123d.mesh.bridge.FaceRecord.source`.

        Returns:
            ShapeList[Face]: the recovered faces originating from ``source``
            (empty if no input shape carried that name).

        Raises:
            ValueError: if this MeshPart is empty or carries no side-map.
        """
        if self._manifold.is_empty():
            raise ValueError("Cannot recover faces of an empty MeshPart")
        if not self._side_map:
            raise ValueError(
                "MeshPart.faces_from() needs a non-empty SideMap; a MeshPart "
                "built from raw arrays (or by hull/minkowski) carries no face "
                "provenance."
            )
        recovery = recover_brep(read_result(self._manifold), self._side_map)
        faces: list[Face] = []
        for recovered in recovery.recovered_faces:
            record = self._side_map.records.get(recovered.face_id)
            if record is not None and record.source == source:
                faces.extend(recovered.faces)
        return ShapeList(faces)

    # ---- Export ----

    def export_stl(self, path: str | PathLike, *, ascii_format: bool = False) -> bool:
        """Export this mesh body to an STL file — cheap, no BREP bake.

        Writes the manifold's triangles straight to disk. There is no BREP
        conversion and no third-party dependency: the STL is serialized directly
        from the vertex/triangle arrays, so this works with only the ``manifold``
        extra installed.

        Args:
            path: destination file path.
            ascii_format (bool): write ASCII STL instead of binary. Defaults to
                False.

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
            f"volume={manifold.volume():.3f}, "
            f"face_records={len(self._side_map)})"
        )


# ---- Transform helpers ----


def _euler_rotation_matrix(x: float, y: float, z: float) -> np.ndarray:
    """Return the 3x3 rotation matrix for ``manifold3d``'s Euler convention.

    ``manifold3d.Manifold.rotate`` rotates about the global X axis, then the
    global Y axis, then the global Z axis — so the composite matrix is
    ``Rz @ Ry @ Rx``.

    Args:
        x (float): rotation about the global X axis, in degrees.
        y (float): rotation about the global Y axis, in degrees.
        z (float): rotation about the global Z axis, in degrees.

    Returns:
        np.ndarray: the ``(3, 3)`` rotation matrix.
    """
    angle_x, angle_y, angle_z = np.radians([x, y, z])
    cos_x, sin_x = np.cos(angle_x), np.sin(angle_x)
    cos_y, sin_y = np.cos(angle_y), np.sin(angle_y)
    cos_z, sin_z = np.cos(angle_z), np.sin(angle_z)
    rotate_x = np.array(
        [[1, 0, 0], [0, cos_x, -sin_x], [0, sin_x, cos_x]], dtype=np.float64
    )
    rotate_y = np.array(
        [[cos_y, 0, sin_y], [0, 1, 0], [-sin_y, 0, cos_y]], dtype=np.float64
    )
    rotate_z = np.array(
        [[cos_z, -sin_z, 0], [sin_z, cos_z, 0], [0, 0, 1]], dtype=np.float64
    )
    return rotate_z @ rotate_y @ rotate_x


# ---- Operand coercion ----


def _coerce(operand: MeshOperand) -> MeshPart:
    """Coerce a free-function / operator operand into a :class:`MeshPart`.

    A :class:`MeshPart` is returned unchanged; a build123d
    :class:`~build123d.Shape` is tessellated and seeded (the OUT leg) so it
    arrives in mesh space *with* provenance.

    Args:
        operand: a build123d Shape or a MeshPart.

    Returns:
        MeshPart: the operand in mesh space, carrying a side-map.

    Raises:
        TypeError: if ``operand`` is neither a Shape nor a MeshPart.
    """
    if isinstance(operand, MeshPart):
        return operand
    if isinstance(operand, Shape):
        return MeshPart.from_part(operand)
    raise TypeError(f"Expected a build123d Shape or a MeshPart, got {type(operand)}")


def _merge_side_maps(parts: Sequence[MeshPart]) -> SideMap:
    """Merge the side-maps of several :class:`MeshPart` operands.

    Face ids are globally unique, so the merge is a collision-free dictionary
    update.

    Args:
        parts: the operand mesh parts.

    Returns:
        SideMap: the union of every operand's provenance records.
    """
    merged = SideMap()
    for part in parts:
        merged = merged.merged(part.side_map)
    return merged


def _check_result(
    manifold: m3d.Manifold, side_map: SideMap, operation: str
) -> MeshPart:
    """Wrap a boolean result with its merged side-map, raising on a bad status.

    Args:
        manifold: the manifold produced by a boolean.
        side_map: the merged provenance side-map.
        operation: a human-readable operation name for the error message.

    Returns:
        MeshPart: the wrapped result.

    Raises:
        ValueError: if the manifold carries an error status.
    """
    if manifold.status() != m3d.Error.NoError:
        raise ValueError(
            f"mesh {operation} produced an invalid manifold: {manifold.status()}"
        )
    return MeshPart(manifold, side_map)


# ---- Free-function CSG API ----


def mesh_fuse(*shapes: MeshOperand) -> MeshPart:
    """Fast union of any mix of build123d Shapes and MeshParts.

    ``Shape`` operands are tessellated and seeded under the hood. The N-way
    union is resolved in a single ``manifold3d`` ``batch_boolean`` pass —
    materially faster than folding ``+`` in Python — and the operand side-maps
    are merged so the result carries full provenance.

    Args:
        *shapes: one or more build123d Shapes / MeshParts.

    Returns:
        MeshPart: the union, in mesh space.

    Raises:
        ValueError: if no operand is given.
    """
    if not shapes:
        raise ValueError("mesh_fuse needs at least one operand")
    parts = [_coerce(shape) for shape in shapes]
    side_map = _merge_side_maps(parts)
    if len(parts) == 1:
        return MeshPart(parts[0].manifold, side_map)
    result = m3d.Manifold.batch_boolean(
        [part.manifold for part in parts], m3d.OpType.Add
    )
    return _check_result(result, side_map, "fuse")


def mesh_cut(base: MeshOperand, *tools: MeshOperand) -> MeshPart:
    """Fast difference: subtract every tool from ``base``.

    ``base`` and ``tools`` may be any mix of build123d Shapes and MeshParts;
    ``Shape`` operands are tessellated and seeded under the hood. The
    subtraction is resolved in a single ``manifold3d`` ``batch_boolean`` pass and
    the operand side-maps are merged.

    Args:
        base: the shape / mesh to subtract from.
        *tools: shapes / meshes to subtract.

    Returns:
        MeshPart: the difference, in mesh space.
    """
    base_part = _coerce(base)
    if not tools:
        return MeshPart(base_part.manifold, base_part.side_map)
    parts = [base_part] + [_coerce(tool) for tool in tools]
    side_map = _merge_side_maps(parts)
    result = m3d.Manifold.batch_boolean(
        [part.manifold for part in parts], m3d.OpType.Subtract
    )
    return _check_result(result, side_map, "cut")


def mesh_intersect(*shapes: MeshOperand) -> MeshPart:
    """Fast intersection of any mix of build123d Shapes and MeshParts.

    ``Shape`` operands are tessellated and seeded under the hood. The N-way
    intersection is resolved in a single ``manifold3d`` ``batch_boolean`` pass
    and the operand side-maps are merged.

    Args:
        *shapes: one or more build123d Shapes / MeshParts.

    Returns:
        MeshPart: the intersection, in mesh space.

    Raises:
        ValueError: if no operand is given.
    """
    if not shapes:
        raise ValueError("mesh_intersect needs at least one operand")
    parts = [_coerce(shape) for shape in shapes]
    side_map = _merge_side_maps(parts)
    if len(parts) == 1:
        return MeshPart(parts[0].manifold, side_map)
    result = m3d.Manifold.batch_boolean(
        [part.manifold for part in parts], m3d.OpType.Intersect
    )
    return _check_result(result, side_map, "intersect")


# ---- STL serialization (dependency-free) ----


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
    normals = triangle_normals(vertices, triangles).astype(np.float32)
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
    normals = triangle_normals(vertices, triangles)
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
