# build123d Core Architecture

> Research deep-dive for: (1) bringing manifold3d mesh operations into build123d,
> (2) making `scad2py` use build123d as a backend.
>
> Codebase: `/Users/ochafik/github/build123d/src/build123d/` — fork at `github.com/ochafik/build123d`.
> All `file.py:line` references are clickable and were read directly (not guessed).

---

## 1. Package layout & module responsibilities

build123d is a single Python package, `build123d`, split into a flat set of top-level
modules plus one sub-package, `build123d.topology`. The public surface is re-exported
wholesale from `__init__.py` (`src/build123d/__init__.py:3-26`), so `from build123d import *`
pulls everything.

### 1.1 Top-level modules (`src/build123d/`)

| Module | LOC | Responsibility |
|---|---|---|
| `geometry.py` | 3456 | Math layer: `Vector`, `Axis`, `Plane`, `Location`, `Color`, `Matrix`, `BoundBox`, `OrientedBoundBox`, `Rotation`, `Pos`. Wraps OCP `gp_*` types but **no `TopoDS_*` topology**. See §4. |
| `build_enums.py` | 447 | All enums: `Mode`, `Align`, `Keep`, `Select`, `SortBy`, `GeomType`, `Transition`, `Until`, `Kind`, `Side`, etc. Pure Python, no OCP. |
| `build_common.py` | 1395 | The `Builder` ABC (context-manager API core), `LocationList`/`WorkplaneList`/`HexLocations`/`PolarLocations`/`GridLocations`/`Locations`, `validate_inputs`, context-aware selectors `solids/faces/wires/edges/vertices`. See §5. |
| `build_part.py` | 133 | `BuildPart` — 3D builder context. |
| `build_sketch.py` | 126 | `BuildSketch` — 2D builder context. |
| `build_line.py` | 143 | `BuildLine` — 1D builder context. |
| `objects_part.py` | 637 | Algebra-API 3D primitives: `Box`, `Cylinder`, `Sphere`, `Cone`, `Torus`, `Wedge`, `Hole`, `CounterBoreHole`, `CounterSinkHole`, `ConvexPolyhedron`. All subclass `BasePartObject(Part)`. |
| `objects_sketch.py` | 829 | Algebra-API 2D primitives: `Rectangle`, `Circle`, `Ellipse`, `Polygon`, `RegularPolygon`, `Triangle`, `Trapezoid`, `Slot*`, `Text`. Subclass `BaseSketchObject(Sketch)`. |
| `objects_curve.py` | 2951 | Algebra-API 1D primitives: `Line`, `Polyline`, `Spline`, `Bezier`, `*Arc`, `Helix`, `Airfoil`, tangent constructions. Subclass `BaseLineObject(Wire)`. |
| `operations_generic.py` | 1085 | Dimension-agnostic ops: `add`, `bounding_box`, `chamfer`, `fillet`, `mirror`, `offset`, `scale`, `split`, `sweep`, `project`. |
| `operations_part.py` | 720 | 3D ops: `extrude`, `revolve`, `loft`, `thicken`, `section`, `draft`, `make_brake_formed`, `project_workplane`. |
| `operations_sketch.py` | 321 | 2D ops: `make_face`, `make_hull`, `trace`, `full_round`. |
| `joints.py` | 819 | `Joint`, `RigidJoint`, `RevoluteJoint`, `LinearJoint`, `CylindricalJoint`, `BallJoint` — assembly kinematics. |
| `importers.py` | 466 | `import_step`, `import_stl`, `import_brep`, `import_svg`, `import_svg_as_buildline_code`. Uses OCP `STEPCAFControl`, `RWStl`, `BRepTools`. |
| `import_dxf.py` | 492 | DXF import via `ezdxf`. |
| `exporters.py` | 1569 | 2D vector export: `ExportDXF`, `ExportSVG`, `Export2D`. |
| `exporters3d.py` | 454 | 3D export: `export_step`, `export_stl`, `export_gltf`, `export_brep`. Uses OCP `StlAPI_Writer`, `RWGltf_CafWriter`. |
| `mesher.py` | 574 | `Mesher` — 3MF/STL read+write via `lib3mf`. Critically also **reconstructs BREP solids from triangle meshes** (see §6.4, §7). |
| `brep_from_stl.py` | 1970 | `detect_primitives` — heuristic mesh→BREP primitive (plane/cylinder/sphere) fitter. Uses `numpy`/`scipy`. Large, mesh-centric. |
| `drafting.py` | 829 | `Draft`, `DimensionLine`, `ExtensionLine`, `TechnicalDrawing`, arrows — annotation/dimensioning sketch objects. |
| `text.py` | 269 | `FontManager`, `available_fonts` — font discovery. |
| `pack.py` | 167 | `pack` — 2D bin-packing of shapes for layout. |
| `persistence.py` | 159 | `modify_copyreg` — makes OCP objects picklable (called at import time, `__init__.py:30`). |
| `jupyter_tools.py` | 80 | `_repr_javascript_` rendering hooks for notebooks. |
| `vtk_tools.py` | 197 | VTK polydata conversion (note: dependency is `cadquery-ocp-novtk` — VTK is *not* bundled; this module is best-effort). |
| `version.py` / `_version.py` / `_dev/` | — | Version plumbing (setuptools-scm). |

### 1.2 The `topology` sub-package (`src/build123d/topology/`)

This is the heart of the system — the `Shape` hierarchy. Modules are layered by
topological dimension so that imports flow strictly upward (0D → 1D → 2D → 3D → composite).

| Module | LOC | Responsibility |
|---|---|---|
| `shape_core.py` | 3838 | `Shape` base class, `ShapeList`, `GroupBy`, `Comparable`, `BoundBox` re-export, `SkipClean`, and the free functions `downcast`, `fix`, `shapetype`, `unwrap_topods_compound`, `get_top_level_topods_shapes`, `_topods_bool_op`. **All boolean operations live here.** |
| `zero_d.py` | 359 | `Vertex` (0D), `topo_explore_common_vertex`. |
| `one_d.py` | 4771 | `Mixin1D`, `Edge`, `Wire`; curve constructors (`make_circle`, `make_spline`, `make_bezier`, `make_helix`, …); `edges_to_wires`, `topo_explore_connected_edges/faces`, `offset_topods_face`. |
| `two_d.py` | 2918 | `Mixin2D`, `Face`, `Shell`; `sort_wires_by_build_order`. |
| `three_d.py` | 1845 | `Mixin3D`, `Solid`, `DraftAngleError`; primitive solid constructors (`make_box`, `make_cylinder`, …), `extrude`, `revolve`, `sweep`, `loft`, `thicken`. |
| `composite.py` | 962 | `Compound` and its three dimension-typed subclasses `Curve` (1D), `Sketch` (2D), `Part` (3D). Registers composite factories (see §2.5). |
| `utils.py` | 376 | `tuplify`, `isclose_b`, `polar`, `delta`, `new_edges`, `find_max_dimension` — small helpers. |
| `constrained_lines.py` | 826 | 2D geometric-constraint line/arc solver (`ConstrainedLines`, `ConstrainedArcs`). |
| `__init__.py` | 102 | Re-exports the public topology API. |

The package docstring (`topology/__init__.py:8-13`) states the intent: foundational
classes for vertices→solids→composites, working "seamlessly with the OpenCascade
Python bindings."

---

## 2. The topology system

### 2.1 Class hierarchy

```
NodeMixin (anytree)            Generic[TOPODS]
        │
      Shape ─────────────────────────────────────────────  (shape_core.py:175)
        │  wraps a single TopoDS_Shape via .wrapped
        │
   ┌────┼───────────────┬─────────────────┬───────────────┐
   │    │               │                 │               │
 Vertex Mixin1D       Mixin2D(ABC)      Mixin3D       (Mixin3D ⊃)
 (0D)   │               │                 │
        ├─ Edge (1.0)   ├─ Face (2.0)    └─ Solid (3.0)
        └─ Wire (1.5)   └─ Shell (2.5)
                                          Compound (4.0)  (composite.py:122, subclasses Mixin3D)
                                            ├─ Curve   (1D Compound)
                                            ├─ Sketch  (2D Compound)
                                            └─ Part    (3D Compound)
```

- `Shape` is `Generic[TOPODS]` where `TOPODS` is bound to `TopoDS_Shape`
  (`shape_core.py:170`, `class Shape(NodeMixin, Generic[TOPODS])` at `:175`). The type
  parameter records which `TopoDS_*` subtype `.wrapped` holds, e.g. `Edge(Mixin1D[TopoDS_Edge])`.
- `Shape` extends `anytree.NodeMixin` — every Shape is an assembly tree node, with
  `parent`/`children`/`topo_parent` (`shape_core.py:308-311`, attribute docs `:186-192`).
- Each leaf class carries a class-level **`order`** float used to pick the "highest
  order" result class in boolean ops: Vertex 0.0, Edge 1.0, Wire 1.5, Face 2.0,
  Shell 2.5, Solid 3.0, Compound 4.0 (`zero_d.py:86`, `one_d.py:1552,3452`,
  `two_d.py:680,2690`, `three_d.py:731`, `composite.py:132`).
- `Curve`/`Sketch`/`Part` are *thin* subclasses of `Compound` that only override
  `_dim` to a fixed value (1/2/3) and `__iadd__` (`composite.py:904-957`). They are
  the algebra-API return types.

### 2.2 The `Shape` base class

Constructor (`shape_core.py:293-311`):

```python
def __init__(self, obj=None, label="", color=None, parent=None):
    self._wrapped = (downcast(obj) if obj is not None else None)
    self.for_construction = False
    self.label = label
    self.color = color
    self.parent = parent
    self.topo_parent = None
```

Key points:

- **`.wrapped`** is the single OCP `TopoDS_Shape` this object owns. The setter/getter
  are at `shape_core.py:317-325`; `__bool__` is "has a non-None wrapped" (`:327`).
- The constructor **always calls `downcast(obj)`** so `.wrapped` holds the *specialised*
  `TopoDS_*` type, not a bare `TopoDS_Shape`.
- `_dim` is an `@abstractmethod` property (`shape_core.py:330-333`); Mixin1D returns 1,
  Mixin2D returns 2 implicitly, Mixin3D returns 3, Vertex 0, Compound returns
  `topods_dim()` (variable).
- `cast` is an `@classmethod @abstractmethod` (`shape_core.py:654-657`) — "returns the
  right type of wrapper given an OCCT object." Each subtree implements its own LUT
  (e.g. `Mixin3D.cast` at `three_d.py:156-172`, `Compound.cast` at `composite.py:190-210`).

Geometry/metrology lives directly on `Shape` and is computed lazily from OCP each call:
`area` (`:336`, `BRepGProp.SurfaceProperties_s`), `volume`, `geom_type`
(`:369`, via `BRepAdaptor_Curve/Surface`), `is_manifold` (`:396`, edge→face ancestry
check), `is_valid` (`:472`, `BRepCheck_Analyzer`), `matrix_of_inertia`, `static_moments`,
`principal_properties`, `bounding_box`. **There is no caching** — every `.volume` call
re-runs `BRepGProp` over the kernel.

### 2.3 The Mixins

The Mixins are not mix-ins in the multiple-inheritance sense — each is
`class MixinND(Shape[TOPODS])`, i.e. an *intermediate base class* that adds
dimension-specific methods, then the concrete classes subclass it.

- **`Mixin1D`** (`one_d.py:485`): `_dim → 1`; `is_closed`, `is_forward`, `is_interior`,
  `length`, `radius`, `volume → 0`; geometric ops `position_at`/`tangent_at`/
  `location_at`/`normal`/`project`/`offset_2d`; operators `@` (`__matmul__`, position),
  `%` (`__mod__`, tangent), `^` (`__xor__`, location). `cast` resolves Vertex/Edge/Wire.
- **`Mixin2D`** (`two_d.py:179`, `class Mixin2D(ABC, Shape[TOPODS])`): adds
  `find_intersection_points`, offset/extrude helpers for Face/Shell.
- **`Mixin3D`** (`three_d.py:142`): `_dim → 3`; `chamfer`, `fillet`, `hollow`/`shell`,
  `is_inside`, plus `center(CenterOf.MASS)` default. `Mixin3D.find_intersection_points`
  is *aliased* to `Mixin2D.find_intersection_points` (`three_d.py:145`) — a deliberate
  cross-mixin method reuse pattern. `_make_3d_result` (`three_d.py:181-195`) wraps a raw
  `TopoDS_Shape` op-result back into a `Solid` or `Part`.

`Compound` subclasses `Mixin3D` even when it holds 1D content — its `_dim` override
(`composite.py:177-180`) calls `topods_dim(self.wrapped)` which inspects children.

### 2.4 `ShapeList` and selectors

`ShapeList` (`shape_core.py:2921`) is `class ShapeList(list[T])` — a plain `list`
subclass with CAD-aware query operators. It is the universal return type for "all the
X in this shape." Operators (`shape_core.py:2940-3016`):

| Operator | Method | Meaning |
|---|---|---|
| `>` / `<` | `sort_by` | sort by `Axis` or `SortBy`, ascending / descending |
| `>>` / `<<` | `group_by`[-1] / [0] | group, take largest / smallest group |
| `\|` | `filter_by` | filter by `Axis`, `Plane`, `GeomType`, callable, or `property` |
| `&` | set intersection | `ShapeList & ShapeList` |
| `-` | set difference | `ShapeList - ShapeList` |
| `+` / `+=` | append/extend | |

Core methods: `filter_by` (`:3085`, accepts callable / `Axis` / `Plane` / `GeomType` /
`property`), `filter_by_position` (`:3223`), `group_by` (`:3274`, returns a `GroupBy`),
`sort_by` (`:3393`), `sort_by_distance` (`:3492`), and the singular/plural accessors
`vertices()/edges()/faces()/solids()/compounds()` plus `.edge()/.face()/...` (which
raise if count ≠ 1, e.g. `:3073-3079`). `.expand()` (`:3018`) dissolves compounds→
children, wires→edges, shells→faces.

`GroupBy` (`shape_core.py:2680`) is a generic grouping container; `Comparable`
(`:2656`) is the ABC for sort keys.

**Context-aware selectors** are separate free functions in `build_common.py`
(`solids`, `faces`, `wires`, `edges`, `vertices`, plus singular `solid`, …). They are
generated by `__gen_context_component_getter` (`build_common.py:1320`) — they read the
*active builder context* and return a `ShapeList` of sub-shapes, with a `Select` enum
for ALL vs LAST.

### 2.5 Composite factory registration (decoupling)

`Shape` cannot import `Compound`/`Curve`/`Sketch`/`Part` (circular). Instead it keeps a
class-level registry:

```python
composite_factories: ClassVar[dict[int | None, CompositeFactory]] = {}   # shape_core.py:196
@classmethod
def register_composite_factory(cls, dimension, factory): ...             # :936-942
@classmethod
def make_composite(cls, shapes, dimension=None) -> Shape: ...            # :944-959
```

`composite.py:959-962` registers them at import time:

```python
Shape.register_composite_factory(None, Compound)
Shape.register_composite_factory(1, Curve)
Shape.register_composite_factory(2, Sketch)
Shape.register_composite_factory(3, Part)
```

So `_bool_op` can produce the correctly-typed composite (`Part`/`Sketch`/`Curve`)
without a hard import. **This registry is the cleanest existing extension seam in the
code** (see §7).

---

## 3. OCP / OpenCASCADE wrapping

### 3.1 The dependency

`pyproject.toml:36` pins `cadquery-ocp-novtk >= 7.9, < 8.0` — the `novtk` build of the
CadQuery OpenCASCADE Python bindings (OCCT 7.9, **without** the VTK render module). Plus
`cadquery-ocp-stubs` for typing (`:84`), `ocpsvg` (`:43`) for SVG↔Edge conversion, and
`ocp_gordon` (`:44`) for advanced surfacing. Math/mesh helpers: `numpy>=2` (`:38`),
`scipy` (`:47`), `svgpathtools`, `ezdxf`, `trianglesolver`.

`OCP` is the Python module name; everything is imported as `from OCP.<Pkg> import ...`.

### 3.2 How OCP is imported and used

There is **no abstraction layer** — OCP is imported directly, module-by-module, into
every file that needs it. `shape_core.py:72-138` is the canonical example: ~40 OCP
sub-modules imported, including the BREP kernel (`BRep`, `BRepBuilderAPI`, `BRepAlgoAPI`,
`BRepGProp`, `BRepMesh`, `BRepPrimAPI`, `BRepTools`), the topology types (`TopoDS`,
`TopAbs`, `TopExp`, `TopLoc`, `TopTools`), the geometry types (`gp`, `Geom`, `GeomAPI`),
and the fix/upgrade modules (`ShapeFix`, `ShapeUpgrade`, `ShapeCustom`).

Every build123d Shape **owns exactly one** `TopoDS_*` object in `.wrapped`:

| build123d | `.wrapped` type |
|---|---|
| `Vertex` | `TopoDS_Vertex` |
| `Edge` | `TopoDS_Edge` |
| `Wire` | `TopoDS_Wire` |
| `Face` | `TopoDS_Face` |
| `Shell` | `TopoDS_Shell` |
| `Solid` | `TopoDS_Solid` |
| `Compound`/`Curve`/`Sketch`/`Part` | `TopoDS_Compound` |

The mapping is encoded in several LUTs on `Shape`: `shape_LUT` /
`inverse_shape_LUT` (`shape_core.py:198-220`), `downcast_LUT` (`:222-231`),
`geom_LUT_EDGE` / `geom_LUT_FACE` (`:233-256`).

### 3.3 `downcast` and `fix`

```python
def downcast(obj: TopoDS_Shape) -> TopoDS_Shape:        # shape_core.py:3684
    f_downcast = Shape.downcast_LUT[shapetype(obj)]     # e.g. TopoDS.Solid
    return f_downcast(obj)
```

`downcast` converts a *generically-typed* `TopoDS_Shape` (which OCP often hands back
from kernel operations) into its concrete static type (`TopoDS.Solid(obj)` etc.). This
matters because Python `isinstance` checks against `TopoDS_Solid` only work post-downcast.
`shapetype` (`:3762`) is `obj.ShapeType()` returning a `TopAbs_ShapeEnum`.

```python
def fix(obj: TopoDS_Shape) -> TopoDS_Shape:             # shape_core.py:3700
    shape_fix = ShapeFix_Shape(obj)
    shape_fix.Perform()
    return downcast(shape_fix.Shape())
```

`fix` runs OCCT's `ShapeFix_Shape` healing pass (repairs tolerances, gaps, orientation,
small edges) and re-downcasts. `Shape.fix()` (the *method*, `:1368-1378`) only invokes
it if `not self.is_valid`.

### 3.4 Reaching the BREP kernel

There is no kernel handle to "reach" — OCCT is a set of free-function/builder classes.
build123d touches it in four idioms:

1. **`BRepPrimAPI_Make*`** for primitives (`three_d.py:75-82`): `MakeBox`, `MakeCone`,
   `MakeCylinder`, `MakeSphere`, `MakeTorus`, `MakeWedge`, `MakeRevol`.
2. **`BRepBuilderAPI_Make*`** for incremental construction: `MakeVertex`, `MakeEdge`,
   `MakePolygon`, `MakeFace`, `MakeWire`, `MakeSolid`, `Sewing`, `Copy`, `Transform`,
   `GTransform`.
3. **`BRepAlgoAPI_*`** for booleans (§6).
4. **`BRepGProp` / `BRepMesh` / `BRepTools`** for metrology and tessellation.

`gp_Trsf` (rigid transform) and `gp_GTrsf` (general/affine) are applied via
`BRepBuilderAPI_Transform` / `BRepBuilderAPI_GTransform` in `Shape._apply_transform`
(`shape_core.py:2437`).

---

## 4. `geometry.py` — the math layer

`geometry.py` is the **OCP-`gp_*`-only** layer: it imports `gp_*`, `Geom_*`, `Bnd_*`,
`Quantity_*` but deliberately **no `TopoDS_*` topology** (it `TYPE_CHECKING`-imports
`Edge`/`Face`/`Shape`/`Vertex` only, `geometry.py:84`). Classes (`geometry.py`):

| Class | Line | Wraps | Notes |
|---|---|---|---|
| `Vector` | 151 | `gp_Vec` | `.wrapped` is `gp_Vec`; `to_pnt()→gp_Pnt`, `to_dir()→gp_Dir` (`:506-512`). |
| `Axis` | 626 | `gp_Ax1` | `AxisMeta` metaclass (`:607`) provides class attrs `Axis.X/Y/Z`. |
| `Plane` | 2666 | `gp_Pln` | `PlaneMeta` (`:2593`) gives `Plane.XY/XZ/...`; `to_local_coords`/`to_gp_ax2`. |
| `Location` | 1607 | `TopLoc_Location` | The preferred way to place Shapes; built from `gp_Trsf`. |
| `Rotation` | 2328 | (subclass of `Location`) | Euler-angle Location. |
| `Pos` | 2401 | (subclass of `Location`) | Pure-translation Location. |
| `Matrix` | 2443 | `gp_GTrsf` | General affine 4×4; used by `transform_geometry`. |
| `Color` | 1248 | `Quantity_ColorRGBA` | |
| `BoundBox` | 1023 | `Bnd_Box` | Axis-aligned. |
| `OrientedBoundBox` | 2118 | `Bnd_OBB` | Minimal oriented box. |

`Location` (`geometry.py:1607-1616`) explicitly documents that it wraps
`TopLoc_Location` and is "the preferred type to locate objects in build123d." Note the
**important distinction**:

- **`Location`** = a `TopLoc_Location` stored on a `TopoDS_Shape` — *cheap*, shared,
  does not deep-modify geometry. `Shape.location`, `.move()`, `.moved()`, `.locate()`,
  `.located()` (`shape_core.py:501-512`, `1660`, `1590-1602`).
- **`Matrix` / `gp_GTrsf`** = a real geometric transform that *rewrites* the BREP —
  `Shape.transform_geometry()` (`:2327`) and `transform_shape()` (`:2354`). Required for
  non-rigid transforms (scaling, shear).

`TOLERANCE = 1e-6` and `DEG2RAD` are defined here (`geometry.py:91-94`) and imported by
`shape_core.py:141-157`.

---

## 5. The two APIs

build123d famously offers two equivalent front-ends over the *same* topology objects.

### 5.1 Builder / context-manager API

`Builder` (ABC) is in `build_common.py:185`. It is `Generic[ShapeT]` and is subclassed by:

- `BuildPart(Builder[Part])` — `build_part.py:40`
- `BuildSketch(Builder[Sketch])` — `build_sketch.py`
- `BuildLine(Builder[Curve])` — `build_line.py`

Each builder is a **context manager** (`__enter__`/`__exit__` at
`build_common.py:259,299`). Builders maintain:

- a thread-local **context stack** so nested `with` blocks and operations can find the
  active builder (`Builder._get_context`);
- a current object `_obj` (e.g. `BuildPart._part`, `build_part.py:84-91`);
- **pending lists** — `pending_faces`, `pending_face_planes`, `pending_edges`,
  `pending_planes` (`build_part.py:67-70`). Pending faces are 2D content waiting to be
  consumed by a 3D op like `extrude`.

The central method is `Builder._add_to_context` (`build_common.py:351-490`). It:

1. Buckets incoming objects by class into `typed[Edge|Wire|Face|Solid|Compound]`
   (`:387-392`).
2. Dissolves Compounds, converts Wires→Edges, aligns sketch faces to `Plane.XY`.
3. For objects matching the builder's `_shape` (e.g. `Solid` for `BuildPart`), combines
   them with `_obj` according to `Mode`:
   - `Mode.ADD` → `self._obj.fuse(*shapes)` (`:465`)
   - `Mode.SUBTRACT` → `self._obj.cut(*shapes)` (`:470`)
   - `Mode.INTERSECT` → `self._obj.intersect(Compound(shapes))` (`:475`)
   - `Mode.REPLACE` → wrap as new `_sub_class` (`:478`)

So **the builder API delegates every combine to `Shape.fuse/cut/intersect`** — i.e. to
the same OCC booleans as the algebra API.

`LocationList` / `WorkplaneList` (`build_common.py:838,1205`) are context managers that
multiply placement: `PolarLocations`, `GridLocations`, `HexLocations`, `Locations`
(`:900-1204`). Inside them, every object created is replicated at each location.

Operations (`operations_*.py`) are *dual-mode* free functions: each one calls
`Builder._get_context(...)`; if a context is active it mutates it via `_add_to_context`,
otherwise it returns a fresh shape. E.g. `extrude` (`operations_part.py:112-241`):
`context = BuildPart._get_context("extrude")` at `:146`, then either consumes
`context.pending_faces` or the explicit `to_extrude` arg, builds solids with
`Solid.extrude`, and finally `context._add_to_context(*new_solids, ...)` at `:240`.

### 5.2 Algebra / direct API

The algebra API treats shapes as values and uses Python operators. The primitive
*objects* are classes that subclass the dimension-typed Compounds:

- `BasePartObject(Part)` (`objects_part.py:58`) → `Box`, `Cylinder`, `Sphere`, …
- `BaseSketchObject(Sketch)` (`objects_sketch.py`) → `Rectangle`, `Circle`, …
- `BaseLineObject(Wire)` (`objects_curve.py`) → `Line`, `Spline`, …

`BasePartObject.__init__` (`objects_part.py:73-122`) is illuminating: it builds the
`Solid`, applies `align` and `rotation`, **and if a builder context is active it also
calls `context._add_to_context`** (`:104-105`). So the *same object classes* serve both
APIs — `Box(1,1,1)` inside a `with BuildPart()` registers itself; outside one it's just
a `Part` value.

Operators on `Shape` (`shape_core.py`):

- `__add__` (`:961-994`) → fuse (union); dimension-checked.
- `__sub__` (`:1094`) → cut (difference).
- `__and__` (`:996-1012`) → intersect, then `clean()`.
- `Mixin1D` overrides `__add__` (`one_d.py:612`) to keep results 1D.

So `result = Box(10,10,10) - Cylinder(3,10)` and the `with BuildPart(): Box(...);
Cylinder(..., mode=Mode.SUBTRACT)` block produce the same `Part`. Both ultimately hit
`Shape.fuse`/`cut`/`intersect`.

---

## 6. Boolean operations (the critical section)

### 6.1 The public methods

All on `Shape` (`shape_core.py`):

- `cut(*to_cut)` (`:1219-1231`) → `BRepAlgoAPI_Cut`
- `fuse(*to_fuse, glue=False, tol=None)` (`:1380-1405`) → `BRepAlgoAPI_Fuse`
- `intersect(*to_intersect, tolerance=1e-6, include_touched=False)` (`:1455-…`) →
  dispatches per-subclass `_intersect`, which uses `BRepAlgoAPI_Common`
- `split(tool, keep=Keep.TOP)` (`:1977-2057`) → `BRepAlgoAPI_Splitter`
- `_ocp_section(other)` (`:2589-…`) → `BRepAlgoAPI_Section` (returns vertices+edges)

`BRepAlgoAPI_*` are imported at `shape_core.py:80-87`.

### 6.2 `_bool_op` — the engine

`Shape._bool_op(args, tools, operation)` (`shape_core.py:2459-2560`) is where every
fuse/cut runs. Walkthrough:

1. Compute the **highest `order`** among all inputs (`:2480-2485`) — the result is cast
   to that class (Solid beats Face beats Edge…).
2. Pack `args` and `tools` into `TopTools_ListOfShape` (`:2490-2498`).
3. **Zero-shape shortcuts** (`:2500-2521`): cutting/fusing with empty tool lists, or
   intersecting with an empty operand, return early without invoking the kernel.
4. Otherwise: `operation.SetArguments/SetTools`, `SetRunParallel(True)`,
   `operation.Build()`, then `downcast(operation.Shape())` (`:2524-2530`).
5. **Auto-clean** (`:2533-2540`): unless `SkipClean` is active, run
   `ShapeUpgrade_UnifySameDomain(result, True, True, True)` with
   `AllowInternalEdges(False)` to merge coplanar faces / collinear edges left behind by
   the boolean. Wrapped in `try/except` that downgrades failures to a `warnings.warn`.
6. `unwrap_topods_compound` strips redundant compound wrappers (`:2543-2544`).
7. If still a compound and the top order isn't 4 (Compound), split into top-level
   shapes and rebuild a typed composite via `Shape.make_composite` (`:2546-2555`).
8. Cast and `copy_attributes_to` (label/color/joints) onto the result (`:2557-2558`).

`_bool_op_list` (`:2562-2587`) is a thin wrapper guaranteeing a `ShapeList` return.
There is also a free function `_topods_bool_op` (`:3616-3652`) doing the same at the
raw `TopoDS_Shape` level (no clean, no casting) — used internally where wrapping is
unwanted.

### 6.3 `SkipClean`

```python
class SkipClean:                       # shape_core.py:3594
    clean = True
    def __enter__(self):  SkipClean.clean = False
    def __exit__(self, *a): SkipClean.clean = True
```

A process-global flag (class attribute, **not** thread-local — a concurrency hazard)
that suppresses the `ShapeUpgrade_UnifySameDomain` pass. Used by operator-driven code
where repeated cleaning would be wasteful.

### 6.4 Where booleans are slow / fragile — concrete observations

- **`ShapeUpgrade_UnifySameDomain` on every boolean.** Step 5 above runs *unconditionally*
  after each fuse/cut (unless `SkipClean`). For a scad2py-style workload that does
  hundreds of incremental unions, this is the dominant cost — each clean re-traverses
  and re-sews the whole accumulated solid. The `try/except Exception` around it
  (`:2536-2540`, also `clean()` at `:1169-1173`) silently degrades to a warning when the
  upgrader chokes, meaning a "successful" boolean can still leave a messy solid.
- **No fuzzy tolerance by default.** `fuse` only sets `SetFuzzyValue` if the caller
  passes `tol` (`:1400-1401`); `cut` never does (`:1229`). Booleans between
  near-coincident faces (extremely common in transpiled OpenSCAD, which loves
  zero-gap stacked boxes) are exactly where OCCT BOPAlgo produces self-intersections or
  missing faces. There is no global "robust mode."
- **Pairwise accumulation in the builder.** `BuildPart` `Mode.ADD` does
  `self._obj.fuse(*shapes)` (`build_common.py:465`) — OCCT *can* take multiple tools in
  one `BRepAlgoAPI_Fuse`, which is good, but successive `with`-block operations each
  trigger a fresh full-solid boolean + clean. A 200-cube CSG tree → 200 sequential
  kernel booleans, each on a growing solid. This is O(n²)-ish and is the classic
  build123d/CadQuery performance cliff.
- **`is_valid` / `BRepCheck_Analyzer`** (`:472-482`) is the only correctness gate, and
  it is *not* called inside `_bool_op` — invalid results propagate silently.
- **No CSG tree / lazy evaluation.** Every operator eagerly evaluates the kernel.
  There's no deferred evaluation that could be re-targeted, batched, or simplified.

These are precisely the pain points a manifold3d mesh backend would address: manifold3d
does mesh booleans in milliseconds and is numerically robust to coincident geometry,
at the cost of losing exact BREP (curves become tessellated).

---

## 7. Extension points — where a new geometry backend plugs in

### 7.1 Is the kernel abstracted? — No.

OCP is assumed *everywhere*. There is no `Backend` interface, no `Kernel` protocol, no
indirection. `Shape.wrapped` is typed as `TopoDS_Shape`, `_bool_op` literally constructs
`BRepAlgoAPI_Cut()`, and `geometry.py` is built on `gp_*`. A manifold3d backend cannot
be a drop-in replacement of `.wrapped`.

### 7.2 How Shapes are constructed from raw geometry

Three construction families exist, all OCP-bound:

1. **Primitive builders** — `Solid.make_box` etc. (`three_d.py:1315-1340`):
   `BRepPrimAPI_MakeBox(plane.to_gp_ax2(), l, w, h).Shape()` wrapped in `TopoDS.Solid`.
2. **Sewing meshes into a BREP** — the most relevant path. `Mesher._get_shape`
   (`mesher.py:460-510`) takes a list of vertices + triangle indices and:
   - builds one `BRepBuilderAPI_MakePolygon` + `BRepBuilderAPI_MakeFace` per triangle,
   - feeds them all to `BRepBuilderAPI_Sewing`,
   - if the resulting `Shell` `is_manifold`, wraps it with `BRepBuilderAPI_MakeSolid`.
   This is **exactly the bridge a manifold3d backend needs in reverse**: manifold3d
   produces `(vertices, triangles)`; this code already converts that into a `Solid`.
   `import_stl` (`importers.py:270-322`) does the lighter-weight version: `RWStl` →
   `BRep_Builder().MakeFace(face, reader)` → a single triangulated `Face` reference.
3. **`Shape.cast(topods)`** — wrap an existing `TopoDS_*`.

### 7.3 Tessellation — the path *out* to a mesh

`Shape.tessellate(tolerance, angular_tolerance)` (`shape_core.py:2241-2288`) returns
`(list[Vector], list[tuple[int,int,int]])` — exactly manifold3d's input format. It calls
`Shape.mesh()` (`:1604-1620`, `BRepMesh_IncrementalMesh`) then reads
`BRep_Tool.Triangulation_s` per face. `mesher.py:_create_3mf_mesh` (`:313-357`) does the
same for 3MF export.

So the **round trip already exists**: `Shape → tessellate() → (verts, tris)` out, and
`(verts, tris) → Mesher._get_shape() → Solid` back in.

### 7.4 Realistic insertion points for a manifold3d backend

Ranked from least to most invasive:

1. **Mesh-boolean helper functions (lowest risk).** Add free functions
   `mesh_fuse/mesh_cut/mesh_intersect(*shapes) -> Shape` that: `tessellate()` each
   operand → build `manifold3d.Manifold` from `MeshGL` → run the manifold boolean →
   convert result back via the `Mesher._get_shape` sewing logic (or a new direct
   `Solid`-from-triangles constructor). Zero changes to the `Shape` hierarchy. Lossy
   (BREP→mesh) but fast and robust. Good first deliverable.
2. **An opt-in "mesh mode" on `_bool_op`.** A thread-local flag (mirroring `SkipClean`)
   that reroutes `cut`/`fuse`/`intersect` through the manifold path. The
   `composite_factories` registry (§2.5) already lets results be re-typed without
   import cycles.
3. **A parallel `wrapped` representation.** Give `Shape` an optional `._mesh`
   (`manifold3d.Manifold`) cache alongside `._wrapped`, lazily synced. Booleans operate
   on `._mesh` when both operands have one; `._wrapped` is regenerated on demand for
   BREP-only consumers (STEP export, fillets). Most powerful, most invasive — touches
   `copy`, `deepcopy`, `persistence.py`, equality, `location`.
4. **A first-class `MeshSolid` subtype.** A new `Shape` subclass whose `.wrapped` is a
   manifold mesh and which implements `cast`/`_dim`/`tessellate`/booleans natively,
   registered as an additional `composite_factory`. Cleanest conceptually but requires
   `downcast`/`shapetype` (`:3684`,`:3762`) — both hardcoded to `TopoDS` — to become
   polymorphic.

The single biggest structural obstacle is that `downcast` and `shapetype` are
**free functions hardwired to `TopoDS`**, and `Shape.__init__` calls `downcast(obj)`
unconditionally (`:300-302`). Any non-`TopoDS` backend must either route around the
base `__init__` or generalise these two functions.

### 7.5 `brep_from_stl.detect_primitives` — an existing mesh→BREP precedent

`brep_from_stl.py` (1970 LOC) already does heavy mesh analysis: it fits planes,
cylinders and spheres to triangle clusters (`PlanePatch`/`CylinderPatch`/`SpherePatch`,
`build_plane_face`/`build_cylinder_face`/`build_sphere_face` at `:472-527`) and
reconstructs an analytic BREP from a mesh. It uses `numpy`/`scipy`. This proves the
project already accepts mesh-centric code and a mesh↔BREP boundary — a manifold3d
backend is philosophically in-bounds, and `detect_primitives` is the natural "upgrade
a manifold result back to clean BREP" companion.

---

## Design implications

**For inserting a manifold3d mesh backend:**

- There is **no kernel abstraction** to implement against — OCP is assumed in every
  module. Don't try to make `.wrapped` polymorphic first; start with standalone
  mesh-boolean helper functions (§7.4 option 1).
- The **tessellate ↔ sew round trip already exists** (`Shape.tessellate`
  `shape_core.py:2241`; `Mesher._get_shape` `mesher.py:460`). A manifold3d boolean is:
  tessellate operands → `Manifold` → boolean → triangles → sew to `Solid`. The two
  hardest sub-problems (BREP→triangles, triangles→`Solid`) are solved code you can lift.
- The win is real: build123d booleans run `ShapeUpgrade_UnifySameDomain` on *every*
  op (`:2533`), accumulate **pairwise** in builders, and have **no fuzzy tolerance by
  default** — the exact failure mode (coincident faces) and perf cliff (O(n²) CSG) that
  manifold3d eliminates.
- The cost is **loss of exact BREP**: curves/fillets become facets, STEP export
  degrades, `geom_type` becomes meaningless. A hybrid is needed — mesh for bulk CSG,
  BREP for fillet/chamfer/export. `brep_from_stl.detect_primitives` is the existing
  tool for re-fitting analytic surfaces afterwards.
- The `composite_factories` registry (`shape_core.py:196`,`936`) is the one clean seam:
  it lets new code produce correctly-typed `Part`/`Sketch`/`Curve` results without
  import cycles. Reuse it.
- Watch `downcast`/`shapetype` (`:3684`,`:3762`) and `Shape.__init__`'s unconditional
  `downcast(obj)` (`:300`) — these hardcode `TopoDS` and block a non-OCP `.wrapped`.
- `SkipClean` is a **process-global** flag, not thread-local — any new mesh-mode flag
  should be thread-local to avoid the same hazard.

**For being a scad2py target:**

- OpenSCAD is pure CSG: `union`/`difference`/`intersection` over `cube`/`sphere`/
  `cylinder`/`polyhedron` plus affine transforms. build123d maps cleanly:
  `Box`/`Sphere`/`Cylinder` (`objects_part.py`), `+`/`-`/`&` operators (`shape_core.py:961,
  996,1094`), `Location`/`Rotation`/`Matrix` (`geometry.py`). The **algebra API is the
  right transpilation target** — it's expression-oriented like OpenSCAD, no implicit
  context, no pending lists.
- OpenSCAD `polyhedron(points, faces)` maps directly onto the `Mesher._get_shape`
  sewing pattern (`mesher.py:460-510`) or `ConvexPolyhedron` (`objects_part.py`).
- OpenSCAD `scale()`/`multmatrix()` are **non-rigid** — must use `Shape.transform_geometry`
  / `Matrix` (`shape_core.py:2327`), *not* `Location` (which is rigid only).
- `minkowski`/`hull` → `make_hull` (`operations_sketch.py`) exists for 2D; 3D hull and
  Minkowski are not first-class — gaps to flag for scad2py.
- The performance concern is decisive: a non-trivial OpenSCAD file is a deep CSG tree;
  transpiled naively to sequential build123d booleans it will be slow and may produce
  invalid solids on coincident faces. **scad2py should target the manifold3d backend
  for the CSG bulk** and fall back to OCP BREP only where exact geometry is needed.
- `$fn`/`$fa`/`$fs` (OpenSCAD facet resolution) have no build123d analogue for exact
  BREP primitives (a build123d `Cylinder` is an exact surface); they *do* map onto
  tessellation tolerance if a mesh backend is used — another reason a mesh backend
  aligns scad2py semantics with OpenSCAD's inherently-faceted output.

---

## Open questions / unknowns

1. **Manifold result re-typing.** After a manifold boolean produces triangles and they
   are sewn into a `Solid`, will `is_manifold`/`is_valid` reliably pass? `Mesher._get_shape`
   already falls back to returning a bare `Shell` when not watertight
   (`mesher.py:502-503`) — how often does manifold3d output survive OCCT sewing cleanly?
2. **Thread-safety.** `SkipClean.clean` is a class attribute; builder contexts use a
   stack. Is build123d ever used multi-threaded today, and would a manifold backend need
   its own (thread-local) mode flag? (Recommended: yes.)
3. **Performance baseline.** No benchmarks were found in the repo. Need to measure the
   actual cost of `ShapeUpgrade_UnifySameDomain` per boolean vs. the boolean itself to
   quantify the manifold win — is `tessellate()` (also OCC, also not cheap) cheap enough
   that the round trip still beats native OCC booleans?
4. **`ocp_gordon` / `ocpsvg` roles.** These extra deps (`pyproject.toml:43-44`) are
   imported where? (Not covered here — likely `one_d.py`/`two_d.py` surfacing and SVG
   import.) Could matter if they assume OCP-only shapes.
5. **`vtk_tools.py` viability.** The dependency is `cadquery-ocp-novtk` (no VTK), yet
   `vtk_tools.py` exists. Is it dead code, or does it import VTK separately? Affects
   whether mesh visualisation could reuse it.
6. **`copy`/`deepcopy`/pickle of a mesh-backed Shape.** `persistence.modify_copyreg`
   (`persistence.py`) patches OCP pickling. A `manifold3d.Manifold` in `.wrapped` or
   `._mesh` would need its own copyreg handling — `manifold3d` picklability unknown.
7. **Fillet/chamfer on mesh shapes.** `Mixin3D.chamfer`/`fillet` (`three_d.py:226+`) are
   pure OCC `BRepFilletAPI`. After a mesh boolean, edges are no longer exact — can a
   manifold result be filleted at all without `detect_primitives` re-fitting first?
8. **Coordinate/unit conventions.** build123d is mm-based; OpenSCAD is unitless. scad2py
   must decide a unit policy — does it matter to the manifold backend (purely numeric)?
9. **`Curve`/`Sketch`/`Part` `_dim` invariants.** A manifold backend is inherently 3D.
   How would 2D mesh booleans (OpenSCAD's 2D subsystem: `square`/`circle`/`polygon` +
   `linear_extrude`/`rotate_extrude`) be represented — stay on OCC `Face`, or use a 2D
   manifold/polygon library?
