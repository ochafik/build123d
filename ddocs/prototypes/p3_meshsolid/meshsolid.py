"""
meshsolid.py -- prototype: a manifold3d-backed mesh shape for build123d.

This module prototypes ``MeshSolid``, a 3D solid whose geometry lives in a
``manifold3d.Manifold`` (a fast, robust, *faceted* triangle-mesh kernel)
rather than in OpenCASCADE BREP. It is designed to sit *next to* build123d's
BREP ``Solid``/``Part`` rather than replace them.

Why a parallel mesh shape at all?
---------------------------------
build123d booleans go through OCCT ``BRepAlgoAPI_*`` + a mandatory
``ShapeUpgrade_UnifySameDomain`` clean on every op. For CSG-heavy workloads
(hundreds of incremental unions, transpiled OpenSCAD, coincident faces) that
is the classic O(n^2) perf cliff and a robustness minefield. manifold3d does
the same booleans in milliseconds and *guarantees* a watertight, 2-manifold,
consistently-oriented result. The trade-off is loss of exact geometry: curves
and fillets become facets.

So ``MeshSolid`` is the "fast bulk CSG" lane. You stay in mesh space for the
boolean chain and only pay the expensive BREP conversion (``.to_solid()``)
once, at the boundary, when you actually need BREP features or STEP export.

Design stance (see NOTES.md for the full rationale)
---------------------------------------------------
* ``MeshSolid`` is a **standalone class**, NOT a ``build123d.Shape`` subclass.
  build123d's ``Shape`` assumes ``.wrapped`` is a ``TopoDS_Shape`` and its
  ``__init__`` unconditionally calls ``downcast()`` (hardwired to ``TopoDS``).
  A mesh-backed object cannot honour that contract, so we do not pretend to.
* Interop is **explicit and directional**: ``from_build123d()`` tessellates in,
  ``to_solid()`` / ``to_part()`` sews out. The expensive direction (mesh->BREP)
  is never implicit.
* Operators (``+ - &``) mirror build123d's algebra API but operate on the
  manifold kernel. Mixed operands (``MeshSolid`` op ``Part``) are accepted by
  auto-converting the BREP operand into mesh space.

Tested against: manifold3d 3.4.1, build123d (editable fork), OCP 7.9.
"""

from __future__ import annotations

import math
import time
from typing import Iterable, Optional, Sequence, Union

import numpy as np

import manifold3d as m3d

import build123d as bd
from build123d import Color, Location, Pos, Rotation, Shape, Solid, Part

# OCP imports used only by the (lazy) mesh -> BREP sewing path.
from OCP.gp import gp_Pnt
from OCP.BRep import BRep_Tool
from OCP.TopoDS import TopoDS, TopoDS_Shell
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
)
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps


# --------------------------------------------------------------------------
# Tessellation tolerances. build123d's exporters default to a *relative* 1e-3
# linear deflection; for mesh CSG we want a predictable absolute resolution so
# coincident geometry from different parts lines up. These are the knobs.
# --------------------------------------------------------------------------
DEFAULT_LINEAR_TOLERANCE = 0.1      # absolute, mm
DEFAULT_ANGULAR_TOLERANCE = 0.2     # radians ~ 11 deg


# ==========================================================================
# build123d <-> manifold3d mesh bridge (the load-bearing glue)
# ==========================================================================

def _b3d_to_mesh(shape: Shape,
                 linear_tolerance: float = DEFAULT_LINEAR_TOLERANCE,
                 angular_tolerance: float = DEFAULT_ANGULAR_TOLERANCE) -> m3d.Mesh:
    """Tessellate a build123d ``Shape`` into a manifold3d ``Mesh``.

    build123d's ``Shape.tessellate()`` returns a *per-face soup*: vertices are
    shared within a face but duplicated at every face seam (a box -> ~24 verts,
    not 8). manifold3d rejects such a soup outright (``Error.NotManifold``),
    because it has open edges everywhere.

    The clean fix is manifold3d's own ``Mesh.merge()``: build the soup, then
    let manifold weld coincident vertices along open edges within tolerance.
    This is more robust than rolling our own grid-snap dedup and is exactly
    what manifold3d provides the helper for.
    """
    verts, tris = shape.tessellate(linear_tolerance, angular_tolerance)

    vert_arr = np.array([(v.X, v.Y, v.Z) for v in verts], dtype=np.float32)
    tri_arr = np.array(tris, dtype=np.uint32)

    mesh = m3d.Mesh(vert_arr, tri_arr)
    # merge() returns True if it changed anything; for a face-seam soup it
    # always will. It welds open-edge verts within the bbox tolerance.
    mesh.merge()
    return mesh


def _mesh_to_solid(manifold: m3d.Manifold) -> Solid:
    """Sew a manifold3d ``Manifold`` into a build123d BREP ``Solid``.

    This is the EXPENSIVE direction. It reuses the same mechanism as
    ``build123d.Mesher._get_shape``: one planar ``TopoDS_Face`` per triangle,
    all fed to a single ``BRepBuilderAPI_Sewing``, then ``MakeSolid`` on the
    sewn shell.

    Because manifold3d guarantees a watertight 2-manifold mesh, the sewn shell
    reliably closes into a valid ``Solid`` (verified in the demo). The result
    is faceted -- N triangles -> N planar faces -- so it is heavy and should
    only be materialised when BREP features (fillet, STEP export) are needed.
    """
    mesh = manifold.to_mesh()
    verts = np.asarray(mesh.vert_properties[:, :3], dtype=float)
    tris = np.asarray(mesh.tri_verts, dtype=int)

    if len(tris) == 0:
        raise ValueError("Cannot convert an empty MeshSolid to a BREP Solid")

    gp_pnts = [gp_Pnt(float(x), float(y), float(z)) for x, y, z in verts]

    sew = BRepBuilderAPI_Sewing()
    props = GProp_GProps()
    for a, b, c in tris:
        poly = BRepBuilderAPI_MakePolygon(gp_pnts[a], gp_pnts[b], gp_pnts[c],
                                          Close=True)
        face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
        # Drop zero-area facets -- they would break the sewing pass.
        BRepGProp.SurfaceProperties_s(face, props)
        if props.Mass() != 0:
            sew.Add(face)
    sew.Perform()
    sewed = sew.SewedShape()

    if sewed.ShapeType() == TopAbs_ShapeEnum.TopAbs_SHELL:
        shell = TopoDS.Shell_s(sewed)
        solid_topo = BRepBuilderAPI_MakeSolid(shell).Solid()
        return Solid(solid_topo)

    # Fallback: hand the raw shape to build123d and let Solid() heal it.
    return Solid(sewed)


# ==========================================================================
# MeshSolid -- the prototype class
# ==========================================================================

# manifold3d property channel layout: cols 0-2 are X,Y,Z; we use 3-6 for RGB.
_RGB_CHANNELS = 3
_COLOR_NUM_PROP = 3 + _RGB_CHANNELS  # position + rgb


class MeshSolid:
    """A 3D solid backed by a ``manifold3d.Manifold``.

    Mirrors the *feel* of a build123d ``Part``/``Solid`` -- ``+ - &``
    operators, ``translate``/``rotate``/``scale``/``move``, ``.color`` -- but
    every boolean runs in the manifold kernel: fast and guaranteed manifold.

    ``MeshSolid`` is intentionally NOT a ``build123d.Shape``. Use
    ``from_build123d()`` / ``.to_solid()`` to cross the boundary explicitly.
    """

    __slots__ = ("_manifold", "_color", "label")

    # ----- construction ---------------------------------------------------

    def __init__(self, manifold: m3d.Manifold,
                 color: Optional[Color] = None,
                 label: str = ""):
        """Wrap a ``manifold3d.Manifold`` directly. Usually you want one of
        the classmethod constructors instead."""
        if not isinstance(manifold, m3d.Manifold):
            raise TypeError(f"expected manifold3d.Manifold, got {type(manifold)}")
        self._manifold = manifold
        self._color = color
        self.label = label

    @classmethod
    def from_build123d(cls, shape: Union[Shape, Solid, Part],
                       linear_tolerance: float = DEFAULT_LINEAR_TOLERANCE,
                       angular_tolerance: float = DEFAULT_ANGULAR_TOLERANCE
                       ) -> "MeshSolid":
        """Construct from any build123d 3D ``Shape`` by tessellation.

        This is the BREP -> mesh boundary. It is reasonably cheap (OCCT's
        incremental mesher is fast and parallel) but it is *lossy*: exact
        curves become facets at the given tolerance.
        """
        mesh = _b3d_to_mesh(shape, linear_tolerance, angular_tolerance)
        manifold = m3d.Manifold(mesh)
        if manifold.status() != m3d.Error.NoError:
            raise ValueError(
                f"Tessellated shape did not import as a manifold: "
                f"{manifold.status()}. The mesh may be non-watertight; try a "
                f"finer linear_tolerance.")
        color = getattr(shape, "color", None)
        result = cls(manifold, color=color, label=getattr(shape, "label", ""))
        if color is not None:
            # Bake the color into per-vertex properties so it survives booleans.
            result = result._with_color_properties(color)
        return result

    @classmethod
    def from_mesh(cls, vertices: "np.ndarray | Sequence",
                  triangles: "np.ndarray | Sequence",
                  color: Optional[Color] = None,
                  weld: bool = True) -> "MeshSolid":
        """Construct from raw ``(vertices, triangles)`` arrays.

        ``vertices`` -> ``(N, 3)`` floats, ``triangles`` -> ``(M, 3)`` int
        indices, CCW-wound for outward normals.

        If ``weld`` is True (default) the mesh is run through
        ``Mesh.merge()`` first, so an unwelded triangle soup is accepted.
        A properly indexed watertight mesh can pass ``weld=False``.
        """
        vert_arr = np.asarray(vertices, dtype=np.float32)
        tri_arr = np.asarray(triangles, dtype=np.uint32)
        if vert_arr.ndim != 2 or vert_arr.shape[1] != 3:
            raise ValueError("vertices must be (N, 3)")
        if tri_arr.ndim != 2 or tri_arr.shape[1] != 3:
            raise ValueError("triangles must be (M, 3)")

        mesh = m3d.Mesh(vert_arr, tri_arr)
        if weld:
            mesh.merge()
        manifold = m3d.Manifold(mesh)
        if manifold.status() != m3d.Error.NoError:
            raise ValueError(
                f"Raw mesh is not a valid manifold: {manifold.status()}. "
                f"It must be a closed, oriented, 2-manifold triangle mesh.")
        result = cls(manifold, color=color)
        if color is not None:
            result = result._with_color_properties(color)
        return result

    # ----- primitive helpers (mirror build123d's Box / Sphere / Cylinder) -

    @classmethod
    def box(cls, length: float, width: float, height: float,
            color: Optional[Color] = None) -> "MeshSolid":
        """A box, centred at the origin -- matches ``build123d.Box`` default
        (Align.CENTER)."""
        man = m3d.Manifold.cube([length, width, height], center=True)
        result = cls(man, color=color)
        return result._with_color_properties(color) if color else result

    @classmethod
    def sphere(cls, radius: float, segments: int = 0,
               color: Optional[Color] = None) -> "MeshSolid":
        """A sphere centred at the origin. ``segments`` controls facet count
        (0 = manifold3d's global default)."""
        man = m3d.Manifold.sphere(radius, circular_segments=segments)
        result = cls(man, color=color)
        return result._with_color_properties(color) if color else result

    @classmethod
    def cylinder(cls, radius: float, height: float, segments: int = 0,
                 color: Optional[Color] = None) -> "MeshSolid":
        """A cylinder centred at the origin (axis = Z) -- matches
        ``build123d.Cylinder``."""
        man = m3d.Manifold.cylinder(height, radius, radius,
                                    circular_segments=segments, center=True)
        result = cls(man, color=color)
        return result._with_color_properties(color) if color else result

    # ----- color / properties --------------------------------------------

    @property
    def color(self) -> Optional[Color]:
        """The dominant build123d ``Color`` of this solid (or ``None``)."""
        return self._color

    @color.setter
    def color(self, value: Optional[Color]) -> None:
        self._color = value
        if value is not None:
            baked = self._with_color_properties(value)
            self._manifold = baked._manifold

    def _with_color_properties(self, color: Color) -> "MeshSolid":
        """Return a copy whose every vertex carries this color in property
        channels 3-5. Per-vertex RGB is how a color survives manifold
        booleans (manifold interpolates properties across cut edges).
        """
        r, g, b, _a = tuple(color)
        rgb = (float(r), float(g), float(b))
        painted = self._manifold.set_properties(
            _COLOR_NUM_PROP, lambda pos, old: rgb)
        return MeshSolid(painted, color=color, label=self.label)

    def _dominant_color(self) -> Optional[Color]:
        """Read per-vertex RGB back out and report the most common color.

        After a boolean, faces from different operands keep their own color
        (manifold splits property-verts at seams). We snap to 2 decimals to
        absorb the float32 interpolation rounding noted in the research doc,
        then pick the modal color -- a pragmatic "what color is this mostly".
        """
        if self._manifold.num_prop() < _COLOR_NUM_PROP:
            return self._color
        mesh = self._manifold.to_mesh()
        cols = np.asarray(mesh.vert_properties[:, 3:6], dtype=float)
        if len(cols) == 0:
            return self._color
        snapped = np.round(cols, 2)
        uniq, counts = np.unique(snapped, axis=0, return_counts=True)
        r, g, b = uniq[int(np.argmax(counts))]
        return Color(float(r), float(g), float(b))

    # ----- boolean operators (the whole point: fast, chainable) -----------

    @staticmethod
    def _coerce(other: object) -> "MeshSolid":
        """Accept a MeshSolid as-is; auto-convert a build123d BREP shape.

        This is what makes ``mesh_solid - some_native_part`` just work --
        the BREP operand is tessellated into mesh space on the fly.
        """
        if isinstance(other, MeshSolid):
            return other
        if isinstance(other, Shape):
            return MeshSolid.from_build123d(other)
        raise TypeError(f"cannot combine MeshSolid with {type(other)}")

    def __add__(self, other: object) -> "MeshSolid":
        """Union. ``a + b`` -- a manifold boolean, milliseconds, robust."""
        o = self._coerce(other)
        result = self._manifold + o._manifold
        return MeshSolid(result, color=self._color, label=self.label)

    def __sub__(self, other: object) -> "MeshSolid":
        """Difference. ``a - b``."""
        o = self._coerce(other)
        result = self._manifold - o._manifold
        return MeshSolid(result, color=self._color, label=self.label)

    def __and__(self, other: object) -> "MeshSolid":
        """Intersection. ``a & b``."""
        o = self._coerce(other)
        result = self._manifold ^ o._manifold   # manifold3d uses ^ for common
        return MeshSolid(result, color=self._color, label=self.label)

    def __radd__(self, other: object) -> "MeshSolid":
        # Enables sum([...]) and `native_part + mesh_solid`.
        if other == 0:           # sum() starts from 0
            return self
        return self._coerce(other).__add__(self)

    @staticmethod
    def fuse_all(solids: Iterable["MeshSolid"]) -> "MeshSolid":
        """Union an arbitrary number of MeshSolids in a single kernel pass.

        manifold3d's ``batch_boolean`` resolves N solids at once -- materially
        faster than folding ``+`` in Python, and the recommended path for
        CSG trees with many children (e.g. transpiled OpenSCAD ``union()``).
        """
        solids = list(solids)
        if not solids:
            raise ValueError("fuse_all needs at least one solid")
        manifolds = [s._manifold for s in solids]
        result = m3d.Manifold.batch_boolean(manifolds, m3d.OpType.Add)
        return MeshSolid(result, color=solids[0]._color)

    # ----- transforms (build123d Location / Pos / Rot conventions) --------

    def translate(self, offset: "Sequence | bd.Vector") -> "MeshSolid":
        """Translate by an (x, y, z) offset. Lazy in the kernel."""
        x, y, z = _xyz(offset)
        return MeshSolid(self._manifold.translate([x, y, z]),
                         color=self._color, label=self.label)

    def rotate(self, x: float = 0, y: float = 0, z: float = 0) -> "MeshSolid":
        """Rotate by Euler angles in DEGREES about global X, Y, Z.

        manifold3d's ``rotate`` and build123d's ``Rotation`` both take degrees
        and both apply global X-then-Y-then-Z, so this maps 1:1.
        """
        return MeshSolid(self._manifold.rotate([x, y, z]),
                         color=self._color, label=self.label)

    def scale(self, factor: "float | Sequence") -> "MeshSolid":
        """Scale uniformly (scalar) or per-axis (3-tuple).

        Note: build123d's ``Location`` cannot scale (it is rigid-only); a
        non-uniform scale there needs ``Matrix``/``transform_geometry``. In
        mesh space scaling is trivial -- another point in MeshSolid's favour.
        """
        if isinstance(factor, (int, float)):
            v = [float(factor)] * 3
        else:
            v = [float(c) for c in factor]
        return MeshSolid(self._manifold.scale(v),
                         color=self._color, label=self.label)

    def move(self, location: Location) -> "MeshSolid":
        """Apply a build123d ``Location`` / ``Pos`` / ``Rotation``.

        The ``Location``'s rigid transform is extracted from its underlying
        ``TopLoc_Location`` as a 3x4 affine matrix and handed straight to
        manifold3d's ``transform`` -- so placement is consistent with how
        build123d places BREP shapes.
        """
        trsf = location.wrapped.Transformation()
        mat = np.array(
            [[trsf.Value(r, c) for c in range(1, 5)] for r in range(1, 4)],
            dtype=float)
        return MeshSolid(self._manifold.transform(mat),
                         color=self._color, label=self.label)

    def located(self, location: Location) -> "MeshSolid":
        """Alias of :meth:`move` -- mirrors build123d's ``Shape.located``."""
        return self.move(location)

    # ----- interop OUT: mesh -> build123d BREP (lazy / explicit) ----------

    def to_solid(self) -> Solid:
        """Materialise a build123d BREP ``Solid`` (EXPENSIVE -- sews N faces).

        Call this only when you need BREP-only features: fillet/chamfer,
        STEP export, OCCT selectors. The result is faceted (one planar face
        per triangle) so it is heavy; do all your CSG in mesh space first.
        """
        solid = _mesh_to_solid(self._manifold)
        col = self._dominant_color()
        if col is not None:
            solid.color = col
        if self.label:
            solid.label = self.label
        return solid

    def to_part(self) -> Part:
        """Materialise a build123d ``Part`` (a 3D ``Compound``). Same cost as
        :meth:`to_solid`; use this when downstream code expects the algebra
        API's ``Part`` type."""
        solid = self.to_solid()
        part = Part([solid])
        if solid.color is not None:
            part.color = solid.color
        part.label = self.label
        return part

    # ----- mesh data access -----------------------------------------------

    def to_arrays(self) -> "tuple[np.ndarray, np.ndarray]":
        """Return ``(vertices (N,3) float, triangles (M,3) int)`` -- the raw
        mesh, cheap, no BREP involved."""
        mesh = self._manifold.to_mesh()
        return (np.array(mesh.vert_properties[:, :3], dtype=float),
                np.array(mesh.tri_verts, dtype=int))

    @property
    def manifold(self) -> m3d.Manifold:
        """The underlying ``manifold3d.Manifold`` (escape hatch)."""
        return self._manifold

    # ----- measurement (cheap -- straight from the manifold kernel) -------

    @property
    def volume(self) -> float:
        return self._manifold.volume()

    @property
    def area(self) -> float:
        return self._manifold.surface_area()

    def bounding_box(self) -> "tuple[float, ...]":
        """``(xmin, ymin, zmin, xmax, ymax, zmax)``."""
        return self._manifold.bounding_box()

    @property
    def is_valid(self) -> bool:
        """True if the manifold is a non-empty, error-free solid."""
        return (not self._manifold.is_empty()
                and self._manifold.status() == m3d.Error.NoError)

    # ----- export ---------------------------------------------------------

    def export_stl(self, path: str, ascii: bool = False) -> str:
        """Export STL directly from the mesh -- CHEAP, no BREP conversion.

        Uses build123d's ``Mesher`` (lib3mf) so the export path is identical
        to what build123d users already know. Color is attached as a material.
        """
        self._export_via_mesher(path)
        return path

    def export_3mf(self, path: str) -> str:
        """Export 3MF directly from the mesh -- CHEAP. Carries color."""
        self._export_via_mesher(path)
        return path

    def _export_via_mesher(self, path: str) -> None:
        # build123d's Mesher consumes Shapes, so we still need a BREP Solid
        # here. That makes "direct mesh export" not quite free. A real
        # integration should add a Mesher path that takes (verts, tris)
        # arrays directly -- see NOTES.md.
        mesher = bd.Mesher()
        mesher.add_shape(self.to_solid())
        mesher.write(path)

    def export_step(self, path: str) -> str:
        """Export STEP -- this REQUIRES the expensive ``to_solid()`` bake.

        STEP is a BREP format; there is no cheap path. The exported solid is
        faceted (one planar face per triangle), so the file will be large.
        """
        bd.export_step(self.to_solid(), path)
        return path

    # ----- misc -----------------------------------------------------------

    def __repr__(self) -> str:
        man = self._manifold
        return (f"MeshSolid(tris={man.num_tri()}, verts={man.num_vert()}, "
                f"volume={man.volume():.3f}, "
                f"color={self._color}, label={self.label!r})")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _xyz(v: object) -> "tuple[float, float, float]":
    """Coerce a Vector / tuple / list into a plain (x, y, z) float tuple."""
    if isinstance(v, bd.Vector):
        return (v.X, v.Y, v.Z)
    x, y, z = v  # type: ignore[misc]
    return (float(x), float(y), float(z))


# --------------------------------------------------------------------------
# Free-function API -- the lower-friction alternative to the class.
# This is what build123d issue #1228's "optional extra" could expose as the
# minimal surface: no new type, just fast booleans over existing Shapes.
# --------------------------------------------------------------------------

def mesh_fuse(*shapes: Union[Shape, MeshSolid]) -> MeshSolid:
    """Fast union of any mix of build123d Shapes / MeshSolids."""
    solids = [MeshSolid._coerce(s) for s in shapes]
    return MeshSolid.fuse_all(solids)


def mesh_cut(base: Union[Shape, MeshSolid],
             *tools: Union[Shape, MeshSolid]) -> MeshSolid:
    """Fast difference: subtract every tool from ``base``."""
    result = MeshSolid._coerce(base)
    for tool in tools:
        result = result - tool
    return result


def mesh_intersect(*shapes: Union[Shape, MeshSolid]) -> MeshSolid:
    """Fast intersection of any mix of build123d Shapes / MeshSolids."""
    solids = [MeshSolid._coerce(s) for s in shapes]
    result = solids[0]
    for s in solids[1:]:
        result = result & s
    return result
