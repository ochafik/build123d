"""
Build123dRenderer — a prototype scad2py CSG backend that renders a `csg.Node`
tree into build123d `Shape` objects (exact OpenCASCADE BREP).

This is a derisking prototype for "build123d as a scad2py geometry backend"
(Option A from ddocs/research/04-scad2py-architecture.md): a renderer parallel
to scad2py's `ManifoldRenderer`, but producing exact BREP `Shape`s instead of
mesh `manifold3d.Manifold` / `CrossSection` geometry.

It is a `csg.DefaultVisitor`: one `visitXxx` method per `csg.Node` subtype.
Each method returns a `RenderResult` — see below.

Design notes
------------
* OpenSCAD's 2D / 3D split is mirrored: a sub-tree is either 2D (a build123d
  `Sketch` / `Face` `Compound`) or 3D (a `Part` / `Solid` `Compound`). We track
  dimension via `csg.Node.dim` and the result type.
* scad2py lowers `cube`/`sphere`/`cylinder` to `csg.Polyhedron` leaves that
  carry a *mesh builder* lambda plus a `name_` discriminator and the original
  `args`/`kwargs`. We DO NOT call the mesh builder — we read `name_`/`args`/
  `kwargs` and emit an *exact* build123d primitive. `square`/`circle` become
  `csg.Polygon` leaves, handled the same way.
* Transforms (`Translate`/`Rotate`/`Scale`/`Mirror`/`Multmatrix`) all expose a
  4x4 numpy `.matrix`. Rigid transforms use a cheap `Location`; non-rigid ones
  (non-uniform scale, shear) use `Matrix` + `transform_geometry`, which rewrites
  the BREP. Both are honest — see NOTES.md.
* Things OCCT genuinely cannot do (`minkowski`, true 3D `hull` of curved
  solids, raw mesh `polyhedron` reconstruction edge cases) raise
  `UnsupportedOperation` rather than faking a result.

This file does NOT modify scad2py or build123d. It only imports from them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np

import scad2py.csg as csg

from build123d import (
    Align,
    Axis,
    Box,
    Circle,
    Color,
    Compound,
    Cone,
    Cylinder,
    Face,
    Location,
    Matrix,
    Part,
    Plane,
    Polygon as B3DPolygon,
    Pos,
    Rectangle,
    RegularPolygon,
    Shell,
    Sketch,
    Solid,
    Sphere,
    Text,
    Vector,
    Wire,
    extrude,
    make_face,
    make_hull,
    revolve,
    scale as b3d_scale,
)
from build123d.topology import Compound as TopoCompound


class UnsupportedOperation(Exception):
    """Raised for csg.Node types build123d's BREP kernel cannot faithfully render."""


# A render result is a build123d Shape (3D Part/Solid/Compound, or 2D
# Sketch/Face/Compound), possibly None for an empty sub-tree.
RenderResult = Optional[Union[Part, Sketch, Solid, Face, Compound]]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _kw(n: csg.Node, name: str, default=None):
    """Fetch a keyword argument value from a csg leaf node's kwargs list."""
    if n.kwargs is None:
        return default
    for k, v in n.kwargs:
        if k == name:
            return v
    return default


def _is_3d(shape) -> bool:
    return isinstance(shape, (Part, Solid)) or (
        isinstance(shape, Compound) and shape._dim == 3
    )


def _is_2d(shape) -> bool:
    return isinstance(shape, (Sketch, Face)) or (
        isinstance(shape, Compound) and shape._dim == 2
    )


def _as_compound(shapes: list) -> RenderResult:
    """Wrap a list of build123d shapes into one Compound (dimension-typed)."""
    shapes = [s for s in shapes if s is not None]
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]
    return Compound(shapes)


def _fragments(r: float, fn, fa, fs) -> int:
    """OpenSCAD $fn/$fa/$fs -> fragment count. Mirrors scad2py.calc.get_fragments_from_r."""
    fn = 0.0 if fn is None else float(fn)
    fa = 12.0 if fa is None else float(fa)
    fs = 2.0 if fs is None else float(fs)
    if fn > 0.0:
        return int(fn) if fn >= 3 else 3
    return int(math.ceil(max(min(360.0 / fa, r * 2 * math.pi / fs), 5)))


# How small an explicit $fn must be before we treat the facets as INTENTIONAL
# design (emit a faceted RegularPolygon / prism) rather than render tuning
# (emit an exact Circle / Cylinder). See NOTES.md "resolution policy".
_FACET_INTENT_THRESHOLD = 24


def _matrix_is_rigid(m: np.ndarray) -> bool:
    """True if the upper-left 3x3 is orthonormal (rotation/translation only)."""
    rot = np.asarray(m, dtype=float)[:3, :3]
    return bool(np.allclose(rot @ rot.T, np.eye(3), atol=1e-9))


def _full4(m: np.ndarray) -> np.ndarray:
    """Pad any affine matrix to a 4x4."""
    m = np.asarray(m, dtype=float)
    full = np.eye(4)
    full[: m.shape[0], : m.shape[1]] = m
    return full


def _b3d_matrix(m: np.ndarray) -> Matrix:
    """Build a build123d Matrix (gp_GTrsf wrapper) from an affine matrix."""
    return Matrix(_full4(m).tolist())


def _rigid_location(m: np.ndarray) -> Location:
    """Build a build123d Location from a rigid 4x4 affine via OCCT gp_Trsf."""
    from OCP.gp import gp_Trsf
    full = _full4(m)
    trsf = gp_Trsf()
    trsf.SetValues(*[float(full[r][c]) for r in range(3) for c in range(4)])
    return Location(trsf)


def _apply_matrix(shape, m: np.ndarray):
    """Apply a 4x4 affine to a build123d shape, exact-BREP where possible."""
    m = np.asarray(m, dtype=float)
    if _matrix_is_rigid(m):
        # Rigid: a cheap Location move, no BREP rewrite.
        return _rigid_location(m) * shape
    # Non-rigid (scale / shear): rewrites the BREP via OCCT gp_GTrsf.
    return shape.transform_geometry(_b3d_matrix(m))


# --------------------------------------------------------------------------
# the renderer
# --------------------------------------------------------------------------

class Build123dRenderer(csg.DefaultVisitor):
    """Visitor turning a `csg.Node` tree into build123d `Shape` objects.

    Usage:
        root = <csg.Node tree from scad2py front end>
        result = root.accept(Build123dRenderer())
    """

    def __init__(self, facet_intent_threshold: int = _FACET_INTENT_THRESHOLD):
        super().__init__()
        self.facet_intent_threshold = facet_intent_threshold
        # Diagnostics collected during a render pass:
        self.warnings: list[str] = []
        self.unsupported: list[str] = []

    # -- diagnostics --------------------------------------------------------
    def _warn(self, msg: str):
        self.warnings.append(msg)

    # -- children helpers ---------------------------------------------------
    def _render_children(self, n: csg.Node) -> list:
        out = []
        for c in n.children or []:
            r = c.accept(self)
            if r is not None:
                out.append(r)
        return out

    # ======================================================================
    # group / boolean nodes
    # ======================================================================
    def visitGroup(self, n: csg.Group) -> RenderResult:
        # OpenSCAD's implicit group: an *implicit union* of its children.
        return self._union_of(self._render_children(n))

    def visitRoot(self, n: csg.Root) -> RenderResult:
        return self._union_of(self._render_children(n))

    def visitUnion(self, n: csg.Union) -> RenderResult:
        return self._union_of(self._render_children(n))

    def visitDifference(self, n: csg.Difference) -> RenderResult:
        children = self._render_children(n)
        if not children:
            return None
        result = children[0]
        for c in children[1:]:
            result = result - c
        return result

    def visitIntersection(self, n: csg.Intersection) -> RenderResult:
        children = self._render_children(n)
        if not children:
            return None
        result = children[0]
        for c in children[1:]:
            result = result & c
        return result

    def _union_of(self, children: list) -> RenderResult:
        children = [c for c in children if c is not None]
        if not children:
            return None
        result = children[0]
        for c in children[1:]:
            result = result + c
        return result

    # ======================================================================
    # transforms
    # ======================================================================
    def visitTranslate(self, n: csg.Translate) -> RenderResult:
        return self._transform(n)

    def visitRotate(self, n: csg.Rotate) -> RenderResult:
        return self._transform(n)

    def visitScale(self, n: csg.Scale) -> RenderResult:
        return self._transform(n)

    def visitMultmatrix(self, n: csg.Multmatrix) -> RenderResult:
        return self._transform(n)

    def visitAbstractTransform(self, n) -> RenderResult:
        # csg.Mirror routes here via accept->visitTranslate in scad2py; covered.
        return self._transform(n)

    def _transform(self, n) -> RenderResult:
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        m = np.asarray(n.matrix, dtype=float)

        if n.dim == 2:
            # 2D content: drop the matrix to its 2D affine (x,y) and apply.
            m2 = self._matrix_2d(m)
            return self._apply_2d_matrix(child, m2, n)
        return _apply_matrix(child, m)

    @staticmethod
    def _matrix_2d(m: np.ndarray) -> np.ndarray:
        """Reduce a 4x4 (or 2x3) affine to a 2x3 (x,y) affine."""
        m = np.asarray(m, dtype=float)
        if m.shape == (2, 3):
            return m
        return np.array([[m[0][0], m[0][1], m[0][3]],
                         [m[1][0], m[1][1], m[1][3]]])

    def _apply_2d_matrix(self, shape, m2: np.ndarray, n):
        """Apply a 2x3 affine to a 2D build123d shape."""
        full = np.eye(4)
        full[0, 0], full[0, 1], full[0, 3] = m2[0]
        full[1, 0], full[1, 1], full[1, 3] = m2[1]
        if _matrix_is_rigid(full):
            return _rigid_location(full) * shape
        return shape.transform_geometry(_b3d_matrix(full))

    # ======================================================================
    # color  (preview-only in OpenSCAD; carried on the build123d Shape)
    # ======================================================================
    def visitColor(self, n: csg.Color) -> RenderResult:
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        rgba = np.asarray(n.color, dtype=float).tolist()
        # scad2py colors come in 0..1 or 0..255; normalize to 0..1.
        if any(v > 1.0 for v in rgba):
            rgba = [v / 255.0 for v in rgba]
        while len(rgba) < 4:
            rgba.append(1.0)
        try:
            child.color = Color(*rgba[:4])
        except Exception as e:  # color is non-load-bearing; never fail render
            self._warn(f"could not set color {rgba}: {e}")
        return child

    def visitModified(self, n: csg.Modified) -> RenderResult:
        # %, #, ! modifiers: render the child; ! (ROOT) would prune in a real
        # backend. For the prototype just pass through.
        return self._union_of(self._render_children(n))

    # ======================================================================
    # 3D ops
    # ======================================================================
    def visitLinearExtrude(self, n: csg.LinearExtrude) -> RenderResult:
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        if not _is_2d(child):
            raise UnsupportedOperation("linear_extrude of a non-2D shape")

        sketch = self._to_sketch(child)
        height = float(n.height)
        twist = float(n.twist or 0)
        scale_top = n.scale

        if twist != 0.0:
            # OCCT `extrude` has no twist. A faithful twist would loft a stack
            # of rotated cross-sections. Out of scope for the prototype.
            raise UnsupportedOperation(
                "linear_extrude(twist=...) — OCCT extrude has no twist; "
                "needs a lofted-slice approximation")

        # Resolve scale to an (sx, sy) pair.
        if scale_top is None:
            sx = sy = 1.0
        elif isinstance(scale_top, (int, float)):
            sx = sy = float(scale_top)
        else:
            arr = np.asarray(scale_top, dtype=float).ravel()
            sx, sy = float(arr[0]), float(arr[1])

        if abs(sx - 1.0) < 1e-12 and abs(sy - 1.0) < 1e-12:
            # plain extrude — exact.
            solid = extrude(sketch, amount=height)
        else:
            # tapered / scaled extrude -> loft base face to scaled top face.
            # This is exact (a ruled BREP through the two faces), unlike a
            # `taper` angle which only works for uniform shrink.
            from build123d import loft
            base = sketch
            top = Pos(0, 0, height) * sketch.scale((sx, sy, 1.0))
            solid = loft([base, top])
            self._warn("linear_extrude(scale=...) rendered as a loft of "
                       "base->scaled-top faces (exact ruled surface)")

        if n.center:
            solid = Pos(0, 0, -height / 2.0) * solid
        return solid

    def visitRotateExtrude(self, n: csg.RotateExtrude) -> RenderResult:
        # build123d `revolve` IS the exact equivalent. NOTE: scad2py's
        # RotateExtrude csg node currently has a constructor bug (it does not
        # accept the `angle` kwarg the runtime passes), so this branch is only
        # reachable if that upstream bug is fixed. We implement it anyway to
        # show the mapping is trivial.
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        angle = float(getattr(n, "angle", 360) or 360)
        sketch = self._to_sketch(child)
        # OpenSCAD's rotate_extrude profile lives in the XY plane (x>=0) and is
        # revolved about Z with x becoming the radius. build123d's `revolve`
        # needs the profile on a plane CONTAINING the axis, so relocate the
        # XY profile onto the XZ plane: (x,y) -> (x,z).
        profile = Plane.XZ * sketch
        return revolve(profile, axis=Axis.Z, revolution_arc=angle)

    def visitOffset(self, n: csg.Offset) -> RenderResult:
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        from build123d import offset as b3d_offset, Kind
        amount = n.r if n.r is not None else n.delta
        kind = Kind.ARC if n.r is not None else (
            Kind.INTERSECTION if n.chamfer else Kind.TANGENT)
        return b3d_offset(self._to_sketch(child), amount=float(amount), kind=kind)

    def visitHull(self, n: csg.Hull) -> RenderResult:
        children = self._render_children(n)
        if not children:
            return None
        if n.dim == 2:
            # 2D convex hull is well-defined in build123d.
            edges = []
            for c in children:
                edges.extend(self._to_sketch(c).edges())
            hull_wire = make_hull(edges)
            return make_face(hull_wire)
        # 3D hull of arbitrary (curved) solids has no exact BREP form.
        raise UnsupportedOperation(
            "hull() of 3D solids — OCCT cannot synthesize the ruled/spherical "
            "patches; needs a mesh hull (manifold3d)")

    def visitMinkowski(self, n: csg.Minkowski) -> RenderResult:
        raise UnsupportedOperation(
            "minkowski() — no BREP algorithm exists; "
            "definitionally a mesh operation (keep manifold3d for this)")

    def visitFill(self, n: csg.Fill) -> RenderResult:
        child = self._union_of(self._render_children(n))
        if child is None:
            return None
        # fill = keep only the outer boundary of a 2D shape.
        sk = self._to_sketch(child)
        outer = sk.faces().sort_by(Axis.X)  # any face; rebuild from outer wire
        faces = [make_face(f.outer_wire()) for f in sk.faces()]
        return _as_compound(faces)

    def visitResize(self, n: csg.Resize) -> RenderResult:
        raise UnsupportedOperation(
            "resize() — not implemented in this prototype "
            "(maps to non-uniform scale-to-bounding-box)")

    def visitProjection(self, n: csg.Projection) -> RenderResult:
        raise UnsupportedOperation(
            "projection() — not implemented in this prototype "
            "(maps to build123d project()/section())")

    def visitSurface(self, n: csg.Surface) -> RenderResult:
        raise UnsupportedOperation("surface() heightmap — mesh-backend territory")

    # ======================================================================
    # leaves
    # ======================================================================
    def visitPolyhedron(self, n: csg.Polyhedron) -> RenderResult:
        # scad2py lowers cube / sphere / cylinder / polyhedron all to a
        # csg.Polyhedron leaf, discriminated by `name_`.
        name = getattr(n, "name_", None)
        if name == "cube":
            return self._cube(n)
        if name == "sphere":
            return self._sphere(n)
        if name == "cylinder":
            return self._cylinder(n)
        if name == "polyhedron":
            return self._polyhedron(n)
        raise UnsupportedOperation(f"unknown Polyhedron leaf '{name}'")

    def visitPolygon(self, n: csg.Polygon) -> RenderResult:
        name = getattr(n, "name_", None)
        if name == "square":
            return self._square(n)
        if name == "circle":
            return self._circle(n)
        if name == "polygon":
            return self._polygon2d(n)
        raise UnsupportedOperation(f"unknown Polygon leaf '{name}'")

    def visitText(self, n: csg.Text) -> RenderResult:
        size = float(n.size) if n.size else 10.0
        # OpenSCAD halign/valign -> build123d Align.
        h = {"left": Align.MIN, "center": Align.CENTER,
             "right": Align.MAX}.get(n.halign, Align.MIN)
        v = {"baseline": Align.MIN, "bottom": Align.MIN,
             "center": Align.CENTER, "top": Align.MAX}.get(n.valign, Align.MIN)
        kwargs = {}
        if n.font:
            kwargs["font"] = n.font
        return Text(str(n.text), font_size=size, align=(h, v), **kwargs)

    def visitImport(self, n: csg.Import) -> RenderResult:
        import os
        ext = os.path.splitext(n.file)[1].lower()
        if ext == ".step" or ext == ".stp":
            from build123d import import_step
            return import_step(n.file)
        if ext == ".stl":
            from build123d import import_stl
            self._warn("import(.stl) returns a single Face reference in "
                       "build123d, not an editable solid")
            return import_stl(n.file)
        if ext == ".svg":
            from build123d import import_svg
            return import_svg(n.file)
        raise UnsupportedOperation(f"import of '{ext}' files")

    def visitLeaf(self, n: csg.Leaf) -> RenderResult:
        raise UnsupportedOperation(f"unhandled leaf {type(n).__name__}")

    # -- primitive constructors --------------------------------------------
    def _cube(self, n: csg.Polyhedron):
        size = np.asarray(n.args[0], dtype=float)
        center = _kw(n, "center", False)
        align = Align.CENTER if center else Align.MIN
        return Box(float(size[0]), float(size[1]), float(size[2]),
                   align=(align, align, align))

    def _sphere(self, n: csg.Polyhedron):
        r = float(n.args[0])
        fn = _kw(n, "$fn")
        if fn and float(fn) < self.facet_intent_threshold:
            # Small explicit $fn -> facets are intentional. OCCT has no exact
            # faceted sphere; honest fallback is the EXACT sphere + a warning.
            self._warn(f"sphere($fn={fn}) faceting ignored — emitting exact "
                       f"sphere (build123d has no faceted-sphere primitive)")
        return Sphere(r)

    def _cylinder(self, n: csg.Polyhedron):
        h = float(_kw(n, "h"))
        r1 = _kw(n, "r1")
        r2 = _kw(n, "r2")
        r1 = 1.0 if r1 is None else float(r1)
        r2 = 1.0 if r2 is None else float(r2)
        center = _kw(n, "center", False)
        fn = _kw(n, "$fn")
        align = Align.CENTER if center else Align.MIN

        if fn and float(fn) < self.facet_intent_threshold and r1 == r2:
            # intentional N-gon prism -> extrude a RegularPolygon (exact).
            self._warn(f"cylinder($fn={fn}) -> exact N-gon prism via "
                       f"RegularPolygon extrude")
            ngon = RegularPolygon(radius=r1, side_count=int(fn))
            prism = extrude(ngon, amount=h)
            if center:
                prism = Pos(0, 0, -h / 2.0) * prism
            return prism

        if abs(r1 - r2) < 1e-12:
            return Cylinder(r1, h, align=(Align.CENTER, Align.CENTER, align))
        # tapered -> Cone (exact). build123d Cone(bottom, top, height).
        return Cone(r1, r2, h, align=(Align.CENTER, Align.CENTER, align))

    def _polyhedron(self, n: csg.Polyhedron):
        # OpenSCAD polyhedron(points, faces). faces may be n-gons.
        pts_raw = _kw(n, "points")
        faces_raw = _kw(n, "$faces")
        if faces_raw is None:
            faces_raw = _kw(n, "faces")
        points = [Vector(*[float(x) for x in p]) for p in np.asarray(pts_raw)]
        faces = []
        for face_idx in list(faces_raw):
            idx = [int(i) for i in face_idx]
            verts = [points[i] for i in idx]
            try:
                wire = Wire.make_polygon(verts, close=True)
                faces.append(Face(wire))
            except Exception as e:
                raise UnsupportedOperation(
                    f"polyhedron face {idx} could not be made into a planar "
                    f"BREP Face (non-planar/degenerate?): {e}")
        try:
            shell = Shell(faces)
            return Solid(shell)
        except Exception as e:
            raise UnsupportedOperation(
                f"polyhedron could not be sewn into a valid Solid: {e}")

    def _square(self, n: csg.Polygon):
        size = np.asarray(_kw(n, "size"), dtype=float)
        center = _kw(n, "center", False)
        align = Align.CENTER if center else Align.MIN
        return Rectangle(float(size[0]), float(size[1]), align=(align, align))

    def _circle(self, n: csg.Polygon):
        r = float(n.args[0])
        fn = _kw(n, "$fn")
        if fn and float(fn) < self.facet_intent_threshold:
            self._warn(f"circle($fn={fn}) -> exact RegularPolygon "
                       f"(intentional facets)")
            return RegularPolygon(radius=r, side_count=int(fn))
        return Circle(r)

    def _polygon2d(self, n: csg.Polygon):
        pts = np.asarray(_kw(n, "points"), dtype=float)
        paths = _kw(n, "paths")
        if paths is None or len(paths) == 0:
            return B3DPolygon(*[tuple(p) for p in pts])
        # multi-path: first path = outer, rest = holes.
        path_list = list(paths)
        if not hasattr(path_list[0], "__len__"):
            path_list = [path_list]
        faces = [B3DPolygon(*[tuple(pts[int(i)]) for i in path])
                 for path in path_list]
        result = faces[0]
        for hole in faces[1:]:
            result = result - hole
        return result

    # -- sketch coercion ----------------------------------------------------
    @staticmethod
    def _to_sketch(shape):
        """Coerce a 2D build123d shape into a Sketch suitable for extrude/revolve."""
        if isinstance(shape, Sketch):
            return shape
        if isinstance(shape, Face):
            return Sketch([shape])
        if isinstance(shape, Compound):
            return Sketch(list(shape.faces()))
        return shape


# --------------------------------------------------------------------------
# convenience entry point
# --------------------------------------------------------------------------

def render(root: csg.Node, facet_intent_threshold: int = _FACET_INTENT_THRESHOLD):
    """Render a csg.Node tree to a build123d Shape.

    Returns (shape, renderer) so the caller can inspect renderer.warnings.
    """
    renderer = Build123dRenderer(facet_intent_threshold=facet_intent_threshold)
    shape = root.accept(renderer)
    return shape, renderer
