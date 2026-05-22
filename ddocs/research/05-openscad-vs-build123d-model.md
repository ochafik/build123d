# OpenSCAD Semantics vs build123d Semantics

*Research note for the `scad2py` → `build123d` transpiler effort.*
*Maps the OpenSCAD computational/geometric model onto build123d's, and identifies the semantic gaps.*

---

## 0. TL;DR

OpenSCAD and build123d both produce solid geometry, but they sit on opposite sides of a deep
representational divide:

- **OpenSCAD** is a *functional, declarative* language whose programs build a **CSG render tree**;
  geometry is evaluated lazily as a **polygon mesh**, and every curved surface is *faceted* at a
  resolution chosen by the `$fn/$fa/$fs` special variables. Since 2023 the **Manifold** backend
  makes those mesh booleans fast and watertight.
- **build123d** is an *imperative Python* library on top of the **OpenCASCADE (OCCT)** BREP kernel.
  Geometry is **exact**: a circle is a real `Geom_Circle`, a cylinder has an exact conical/cylindrical
  surface, NURBS are first-class. Resolution does not exist until you *tessellate for export*.

The transpiler's central problem: **OpenSCAD's geometric truth is a mesh; build123d's is a BREP.**
Some OpenSCAD operations (`hull`, `minkowski`, `polyhedron` on near-degenerate input, `$fn`-faceted
primitives, robust booleans on self-intersecting meshes) are *natural in a mesh world and either
impossible, fragile, or slow in a BREP kernel*. That asymmetry is precisely the motivation for
**Goal 1: a manifold3d mesh backend inside build123d.**

`scad2py` already proves the point — its runtime (`scad2py/runtime/modules.py`) is built *entirely*
on `manifold3d` and `trimesh`, not on a BREP kernel. Targeting build123d means re-deriving as much
exact BREP as is faithful, and falling back to mesh for the rest.

---

## 1. OpenSCAD's computational model

### 1.1 A functional language

OpenSCAD's scripting language is **functional / declarative**, not imperative. Variables are
single-assignment bindings to expressions; there is no `x = x + 1`. Within a scope the *last*
assignment wins regardless of textual order (assignments are "pulled up"). There are no loops that
mutate state — `for` is a *generator* of geometry instances, not an iteration with side effects.

Two kinds of user-defined callable:

| Construct | Returns | Purpose |
|-----------|---------|---------|
| **function** | a *value* (number, string, list, …) | pure computation; `function f(x) = x*x;` |
| **module**   | *geometry* (added to the CSG tree) | `module ring() { difference() {...} }` |

Modules split further into **object modules** (leaf geometry: `cube`, `sphere`, …) and
**operator modules** (consume `children()`: `translate`, `difference`, `hull`, …).

### 1.2 Two-stage evaluation

OpenSCAD evaluation has two clearly separated phases:

1. **Script execution / instantiation.** The interpreter walks the program, evaluates expressions,
   expands modules, unrolls `for`/`if`/list-comprehensions, and **emits a CSG abstract node tree**.
   No geometry exists yet — only nodes like `union { translate { cube } sphere }`. This is what
   OpenSCAD writes when you "Export as CSG", and it is exactly what `scad2py` reproduces with its
   `csg.Node` hierarchy (`scad2py/csg.py:152` `class Node`, and the concrete `Union`, `Difference`,
   `Translate`, `Polyhedron`, … subclasses).
2. **Geometry evaluation / rendering.** The CSG tree is rendered into actual geometry. In *preview*
   (F5) OpenSCAD uses OpenCSG (z-buffer CSG, no real mesh). In *render* (F6) it computes a real
   watertight mesh via the geometry backend (CGAL historically, **Manifold** since 2023).

`scad2py` mirrors this split: `runtime/modules.py` builds the tree at "script" time;
`rendering/rendering.py:27` `render_geom()` walks it to produce geometry. The transpiler can hook in
at *either* phase — translate the OpenSCAD program into a Python program that *builds* the same tree
(scad2py's approach), or translate it into a Python program that builds geometry directly. For a
build123d target, the tree-building intermediate is valuable because it lets a backend decide,
per-subtree, whether to stay exact (BREP) or drop to mesh.

### 1.3 The CSG render tree

Every OpenSCAD program is, after stage 1, a tree of:

- **Leaves** — primitives that carry concrete geometry parameters: `cube`, `sphere`, `cylinder`,
  `polyhedron`, `square`, `circle`, `polygon`, `text`, `import`, `surface`.
- **CSG operators** — `union`, `difference`, `intersection`, `group`.
- **Transforms** — `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `resize`, `color`.
- **Special operators** — `hull`, `minkowski`, `linear_extrude`, `rotate_extrude`, `offset`,
  `projection`, `render`, `fill`.
- **Modifiers** — `#` (debug/highlight), `%` (background/transparent), `!` (root/show only this),
  `*` (disable). These annotate a subtree; `scad2py` models them as
  `csg.Modified` with a `csg.Modifier` enum (`scad2py/csg.py:146`).

Crucially the tree is **untyped at the dimension level until rendered**: a subtree is "2D" or "3D"
depending on its leaves. `scad2py` computes this lazily (`csg.py:359` `AbstractGroupNode.dim`).
Mixing 2D and 3D in one boolean is an error in OpenSCAD.

### 1.4 Resolution variables `$fn / $fa / $fs`

OpenSCAD has no exact curves. Every circle, arc, sphere, cylinder, and rotational extrude is a
polygon/polyhedron whose *fragment count* is resolved at instantiation time from three special
variables (dynamically scoped, so they can be overridden per-subtree):

- `$fn` — explicit fragment count. Default `0` (disabled).
- `$fa` — minimum fragment angle in degrees. Default `12`.
- `$fs` — minimum fragment size in model units. Default `2`.

The exact formula (ported verbatim into `scad2py/calc.py:38` `get_fragments_from_r`):

```
if $fn > 0:   fragments = max(int($fn), 3)
else:         fragments = ceil( max( min(360/$fa, 2*pi*r/$fs), 5 ) )
```

So **`circle(r=10, $fn=6)` is literally a regular hexagon**, and `sphere(r=10)` with defaults is a
~`get_fragments_from_r`-faceted polyhedron, not a sphere. This is not an approximation OpenSCAD
makes for *display* — it is the geometric *truth* of the model. Exporting an STL gives you that
hexagon.

Other special variables: `$t` (animation time 0–1), `$preview` (bool: F5 vs F6), `$children`
(child count inside an operator module), `$vpr/$vpt/$vpd/$vpf` (viewport). `$children` and the
`children([idx])` module are how operator modules re-emit / index their children.

### 1.5 Everything ends up as a polygon mesh

This is the single most important fact. In OpenSCAD's `render` phase, **all geometry is a polygon
mesh** (2D: polygons / cross-sections; 3D: triangle/polygon soups assembled into manifolds). There
is no notion of an exact analytic surface anywhere in the pipeline. CSG booleans are *mesh* booleans.

### 1.6 The Manifold backend (default-capable since 2023.x)

Historically OpenSCAD's F6 backend was **CGAL** (Nef polyhedra) — exact rational arithmetic,
correct but slow, single-threaded, and prone to choking on degenerate input.

The **Manifold** library ([github.com/elalish/manifold](https://github.com/elalish/manifold)) was
integrated as an experimental backend in the 2023.x development snapshots and promoted to a
first-class, user-selectable backend (Preferences → Advanced → 3D Rendering → Backend) in the
2024.09.28+ snapshots. What changed:

- **Speed**: mesh booleans are dramatically faster and **multithreaded**.
- **Guaranteed manifold output**: Manifold's data structure is *always* a watertight 2-manifold;
  it does not produce the non-manifold garbage CGAL sometimes did.
- **Floating-point, not exact-rational**: Manifold uses `float`/`double` with robust predicates
  rather than CGAL's exact arithmetic — practically robust, occasionally differs on truly
  degenerate coincidences.
- It also provides `CrossSection` for 2D, `hull`, `minkowski`-enabling primitives, etc.

`scad2py` **already targets Manifold directly**: `from manifold3d import CrossSection, Manifold, Mesh`
at the top of `runtime/modules.py`, and primitives are `Manifold.cube(...)`, `CrossSection.circle(...)`,
`Manifold(Mesh(...))` for `polyhedron`. So scad2py's "native" geometry kernel *is* the OpenSCAD
Manifold backend. A build123d target is therefore strictly a *change of kernel*.

---

## 2. build123d's computational model

### 2.1 Imperative Python on a BREP kernel

build123d is a regular Python library. You write imperative code; objects are created and combined
with operators (`+`, `-`, `&`) or builder context managers. There are two coexisting API styles:

- **Builder API** — context managers `BuildPart`, `BuildSketch`, `BuildLine`
  (`build_part.py`, `build_sketch.py`, `build_line.py`) with implicit "pending" geometry and
  combination `Mode` (ADD/SUBTRACT/INTERSECT/REPLACE).
- **Algebra API** — pure objects combined with Python operators; `Box(...) + Cylinder(...)`,
  `sketch - Circle(...)`. No implicit context.

Both produce the same topology objects.

### 2.2 Exact BREP geometry

build123d wraps **OpenCASCADE (OCCT)**. Geometry is **Boundary Representation**: a solid is a
shell of faces, each face is bounded by wires of edges, each edge carries an *exact* analytic or
NURBS curve, each face an *exact* analytic or NURBS surface. The topology hierarchy lives in
`src/build123d/topology/` (`zero_d.py` Vertex, `one_d.py` Edge/Wire, `two_d.py` Face/Shell,
`three_d.py` Solid, `composite.py` Compound).

Consequences:

- A `Circle` (`objects_sketch.py:115`) is an *exact circle* — infinite precision, not N segments.
- A `Cylinder` (`objects_part.py:377`) has an *exact* cylindrical surface; a `Sphere` an exact
  sphere; a `Torus` an exact torus.
- Filleting, chamfering, offsetting, lofting all produce exact (often NURBS) surfaces.
- The model is **resolution-independent**. Tessellation happens *only* at export
  (`exporters3d.py`, `mesher.py`) or for display, and the chosen mesh deviation tolerance has *no
  effect* on the stored model.

### 2.3 Locations, Planes, and the coordinate model

build123d makes coordinate frames explicit and first-class (`geometry.py`):

- `Vector`, `Axis`, `Plane`, `Location` (a rigid transform = rotation + translation),
  `Rotation`, `Color`.
- A `Plane` is a full local coordinate system; sketches are drawn *on* a plane.
- `Location` objects compose and can be multiplied onto shapes; `Locations`/`GridLocations`/
  `PolarLocations` contexts replicate children at many placements.

OpenSCAD instead threads a single implicit `multmatrix` down the tree. The mapping is direct but
build123d's affine transforms are **rigid-body + uniform/non-uniform scale** handled per-shape;
a general `multmatrix` (shear, non-uniform scale) is *not* a `Location` — see §4.

### 2.4 No CSG tree, no two-stage split

build123d has no separate "tree then render" phase. Each operation *immediately* invokes the OCCT
kernel and yields a concrete `Shape`. There is no lazy CSG node graph (unlike scad2py's `csg.Node`).
A transpiler that goes via scad2py's tree must therefore **collapse the tree into eager build123d
calls** (or keep scad2py's tree as its own IR and only emit build123d at the leaves).

---

## 3. The core semantic gap

> **OpenSCAD: mesh + resolution-dependent + CSG-tree.
> build123d: exact BREP + resolution-independent + immediate.**

### 3.1 The hexagon problem (fidelity, OpenSCAD → build123d)

`circle(r=10, $fn=6)` in OpenSCAD **is a hexagon**. If the transpiler emits build123d
`Circle(radius=10)` it produces an *exact circle* — a geometrically *different object*. Downstream
this matters:

- A part designed to mate with that "circle" expects 6 flat faces after extrusion.
- `difference()` of two `$fn=6` cylinders expects faceted intersection curves.
- Re-exporting must reproduce OpenSCAD's STL *vertex-for-vertex* if "round-trip fidelity" is a goal.

So there are **two fidelity targets**, and they conflict:

- **Intent fidelity** — "the designer wanted a circle; `$fn` was just render tuning." → emit exact
  `Circle`. Cleaner BREP, exact fillets, smaller STEP. *But not what OpenSCAD renders.*
- **Output fidelity** — "reproduce OpenSCAD's mesh exactly." → emit a faceted polygon
  (`RegularPolygon` / explicit `Polygon` with N vertices). Matches OpenSCAD STL, but loses all the
  benefits of a BREP target.

### 3.2 Fidelity gap, build123d → OpenSCAD

The reverse direction loses too: an exact NURBS-filleted edge or a lofted organic surface has **no
exact OpenSCAD representation** — it can only be exported as a faceted `polyhedron`. build123d is a
strict *superset* of representable geometry; OpenSCAD a strict subset (polygon meshes).

### 3.3 Robustness gap

- OpenSCAD/Manifold booleans on **degenerate, self-intersecting, or coincident-face** meshes are
  *defined* (Manifold guarantees manifold output). OCCT booleans on such input frequently **fail or
  hang** — OCCT wants clean BREP with well-defined surfaces.
- `polyhedron()` with non-planar faces, reversed winding, or T-junctions is *common* in real
  OpenSCAD code and *renders fine* under Manifold. Building an OCCT `Solid` from the same
  `Shell` (`objects_part.py` `ConvexPolyhedron` does `Solid(Shell(faces))`) is fragile and may
  yield an invalid solid.

### 3.4 Performance gap

- `hull()` / `minkowski()` of many faceted children: Manifold does this with mesh hulls quickly.
  The OCCT equivalent (if attempted via exact surfaces) is slow and numerically delicate.
- `$fn`-heavy designs (thousands of faceted cylinders, scalemail/menger examples in
  `scad2py/examples/`) become thousands of OCCT BREP solids — OCCT boolean trees of that size are
  *much* slower than Manifold mesh booleans.

---

## 4. Comprehensive mapping table

Legend for **Difficulty**: E = easy/direct, M = moderate, H = hard, X = no faithful BREP route.
**Fidelity loss** describes what is lost translating *OpenSCAD → build123d*.
**Mesh backend?** = whether honest reproduction needs a manifold3d/mesh backend rather than OCCT BREP.

### 4.1 3D primitives

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `cube(size,center)` | `Box(l,w,h, align=...)` (`objects_part.py:125`) | E | none | yes | no |
| `sphere(r/d,$fn..)` | `Sphere(radius)` (`objects_part.py:472`) | E (intent) / H (output) | **exact sphere vs faceted polyhedron** — scad2py builds it via `trimesh.creation.revolve` (`modules.py:89`) | yes (exact) | only for `$fn` output-fidelity |
| `cylinder(h,r1,r2,…,$fn)` | `Cylinder` (`objects_part.py:377`) / `Cone` (`objects_part.py:169`) for `r1≠r2` | E (intent) / M (output) | exact vs N-gon prism; cone for tapered | yes (exact) | only for `$fn` output-fidelity |
| `polyhedron(points,faces)` | `Solid(Shell([Face(Wire.make_polygon(...)) ...]))`; convex case → `ConvexPolyhedron` (`objects_part.py:222`) | M (clean convex) / **H–X** (non-planar/degenerate) | none if valid; **OCCT may reject** non-planar/non-manifold input | partial | **yes** when faces non-planar or mesh degenerate |

### 4.2 2D primitives

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `circle(r/d,$fn..)` | `Circle(radius)` (`objects_sketch.py:115`); faceted → `RegularPolygon` (`objects_sketch.py:302`) | E (intent) / M (output) | **exact circle vs polygon** | yes (exact) | only for `$fn` output-fidelity |
| `square(size,center)` | `Rectangle(w,h,align)` (`objects_sketch.py:226`) | E | none | yes | no |
| `polygon(points,paths)` | `Polygon(*pts)` (`objects_sketch.py:187`); holes via `paths` → subtract inner faces / `make_face` | M | none; multi-path holes need explicit boolean | yes | no |
| `text(...)` | `Text` (`objects_sketch.py:554`, uses `text.py`) | M | font metrics / `halign`/`valign`/`spacing`/`direction` differ; both rasterize fonts to outlines | yes | no |
| `import("file.dxf")` | `import_dxf` (`import_dxf.py`) → faces | M | DXF arc/spline handling differs | yes | no |
| `import("file.svg")` | `import_svg` / `import_svg_as_buildline_code` (`importers.py:325/401`) | M | SVG path fidelity, units | yes | no |

### 4.3 Booleans

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `union()` | `+` / `Mode.ADD` / `fuse` | E (clean) / H (degenerate) | none if input clean | yes | **yes** if children are dirty meshes / many faceted parts |
| `difference()` | `-` / `Mode.SUBTRACT` / `cut` | E / H | none if clean; **coincident faces** can break OCCT | yes | **yes** for coincident/degenerate cases |
| `intersection()` | `&` / `Mode.INTERSECT` / `intersect` | E / H | as above | yes | **yes** for degenerate cases |

OCCT booleans are exact and excellent on clean BREP, but OpenSCAD code routinely relies on
Manifold's tolerance to coincident faces and self-intersection — a frequent source of OCCT failures.

### 4.4 Transforms

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `translate(v)` | `Location` / `Pos` / `.translate` | E | none | yes | no |
| `rotate(a)` / `rotate(a,v)` | `Rotation` / `.rotate(Axis,…)` | E | none (scad2py reuses OpenSCAD's matrix, `calc.py:62`) | yes | no |
| `scale(v)` | uniform → `.scale`; non-uniform → `scale()` op (`operations_generic.py:844`) | E / M | non-uniform scale on exact circle → **exact ellipse in OCCT** (faithful); on faceted shape → scaled polygon | yes | no |
| `mirror(v)` | `mirror()` (`operations_generic.py:498`) about a `Plane` | E | none | yes | no |
| `multmatrix(m)` | rigid part → `Location`; **shear / projective** → no direct equivalent | M–H | OCCT `gp_GTrsf` handles affine incl. shear on BREP but **not via `Location`**; arbitrary 4×4 needs `GTrsf` and may degrade surfaces | partial | sometimes |
| `resize(newsize)` | non-uniform `scale()` to a bounding box; `bounding_box` (`operations_generic.py:207`) to measure | M | `resize` autoscale (0 = keep, axis-proportional) needs explicit logic | yes | no |
| `color(c)` | `Color` (`geometry.py`); set `Shape.color` / `.label` | E | OpenSCAD color is preview-only & non-geometric; build123d carries it on the shape | yes | no |

### 4.5 Extrusion & 2D↔3D

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `linear_extrude(h)` plain | `extrude(sketch, amount=h)` (`operations_part.py:112`) | E | none | yes | no |
| `linear_extrude(h, twist)` | `extrude` has **no twist**; approximate with `loft` of rotated cross-sections, or `sweep` along a helix-twisted path | H | **twist becomes a finite stack of lofted slices** — OpenSCAD itself uses `slices`/`$fn`; both faceted, but slicing differs | partial (loft = exact NURBS through slices) | maybe — exact twist surface is non-trivial |
| `linear_extrude(h, scale=v)` | `taper` (`extrude(..., taper=)`) for uniform shrink; `loft` start→scaled-end face for general/vector scale | M–H | vector scale → non-planar walls; loft approximates | partial | no |
| `linear_extrude(..., slices, convexity)` | `slices` → number of loft sections; `convexity` is a *preview hint*, **drop it** | M | `convexity` has no geometric meaning in BREP | yes | no |
| `rotate_extrude(angle,$fn)` | `revolve(profile, axis=Axis.Z, revolution_arc=angle)` (`operations_part.py:514`) | E (intent) / M (output) | **exact surface of revolution vs `$fn`-faceted**; profile must be x≥0 (same constraint) | yes (exact) | only for `$fn` output-fidelity |
| `offset(r=…)` (rounded) | `offset(sketch, amount, kind=Kind.ARC)` (`operations_generic.py:551`) | E | none — ARC matches OpenSCAD `r` | yes | no |
| `offset(delta=…, chamfer=)` | `offset(..., kind=Kind.INTERSECTION)` (sharp) / `Kind.TANGENT`; `chamfer` → `Kind` choice | M | corner-handling rules differ slightly | yes | no |
| `projection(cut=false)` | `project()` (`operations_generic.py:699`) — shadow projection to a plane | M | OpenSCAD projection flattens *outline*; build123d projects faces — post-process to 2D face | partial | no |
| `projection(cut=true)` | `section()` (`operations_part.py:569`) — planar cross-section | M | none significant | yes | no |
| `surface(file)` (heightmap) | no primitive; build a grid of points → `Face`/loft, or mesh directly | H | heightmap → must be tessellated | no | **yes** (naturally a mesh) |

### 4.6 The hard operators

| OpenSCAD | build123d equivalent | Difficulty | Fidelity loss | BREP native? | Needs mesh backend? |
|----------|---------------------|-----------|---------------|---------------|---------------------|
| `hull()` 2D | `make_hull(edges)` (`operations_sketch.py:232`) — `Wire.make_convex_hull` | E–M | none (convex hull is well-defined); curved inputs → hull of sampled points | partial | no for 2D |
| `hull()` 3D | **`ConvexPolyhedron(points)`** (`objects_part.py:222`, uses `scipy.spatial.ConvexHull`) — but only of *vertices* | M–H | **3D hull of curved solids must sample surface points**; hull of exact spheres is *not* a polyhedron — it has spherical caps + ruled patches OCCT won't synthesize | **no** for curved inputs | **YES** — true 3D hull of arbitrary solids needs a mesh hull (Manifold `hull`) |
| `minkowski()` 2D | no primitive; = offset for convex, else convex-decompose + sum | H | approximation | no | **YES** |
| `minkowski()` 3D | **none** — scad2py implements it in `minkowski_impl.py` via convex decomposition + per-pair `Manifold.hull_points` of cartesian point sums + mesh union | **X** for BREP | structurally a mesh algorithm; non-convex case is *approximate* even in scad2py (`convex_decomposition`) | **no** | **YES — definitionally** |
| `render(convexity)` | no-op / `clean()` | E | `render` forces F6 meshing & is a preview hint; semantically transparent | yes | no |
| `children()` / `children([i])` / `$children` | Python: pass child objects as function args / lists; index normally | E–M | OpenSCAD's lazy child re-instantiation → eager Python values | n/a | no |
| `#` highlight modifier | set `Color`/`label`, keep shape | E | preview-only annotation | yes | no |
| `%` background modifier | mark shape non-participating (don't add to result) or separate Compound | E | preview-only; geometrically *excluded from result* | yes | no |
| `!` root modifier | render only that subtree → emit only that object | E | tree-pruning, do at transpile time | yes | no |
| `*` disable modifier | drop the subtree entirely | E | none — delete at transpile time | yes | no |
| `fill()` | fill holes of a 2D shape → `make_face` of outer wire only | M | none | yes | no |

### 4.7 Control flow & functional constructs

These are **stage-1 (script execution)** features — they vanish before geometry exists. They map to
ordinary Python and never reach build123d:

| OpenSCAD | Python / build123d | Difficulty | Notes |
|----------|--------------------|-----------|-------|
| `for (i=[a:s:b])` (geometry) | Python `for` emitting objects into a list / builder | E | OpenSCAD `for` *unions* its instances implicitly |
| `for` (list-comp form) | Python list comprehension | E | |
| `intersection_for(...)` | Python loop accumulating with `&` | E–M | `for` that intersects instead of unions — no implicit equivalent, write the fold |
| `if / else` (geometry) | Python `if` | E | |
| `let(x=…) ...` | Python local variables / nested function | E | pure expression-level binding |
| list comprehensions `[f(x) for x=…]` | Python list comprehensions | E | incl. `each`, `if`, nested generators |
| `function` definitions | Python `def` returning values | E | recursion supported both sides |
| `module` definitions | Python `def` taking/returning geometry, or context-manager helper | M | operator modules + `children()` need a children-passing convention |
| `assert`, `echo` | Python `assert`, `print` | E | `echo` rounds to 5 sig-figs (scad2py replicates in `modules.py:52`) |
| special vars `$fn/$fa/$fs/$t/$preview/$children` | thread as keyword args / context | M | scad2py threads `_fn/_fa/_fs` kwargs + `runtime/variables.py` dynamic scope |

---

## 5. Why this motivates Goal 1 (a manifold3d mesh backend in build123d)

The mapping table makes the case concretely. The following OpenSCAD features are **exactly** where
an OCCT-only build123d target breaks down, and where a manifold3d mesh backend is required:

1. **`minkowski()` (especially 3D).** There is *no* BREP algorithm for this. It is *definitionally*
   a swept-sum best computed on meshes via convex decomposition + per-part hulls + union — which is
   exactly what `scad2py/minkowski_impl.py` already does on `manifold3d`. Without a mesh backend,
   `minkowski` is simply unsupported.

2. **`hull()` of 3D curved solids.** A 2D hull is fine (`make_hull`). A 3D hull of vertices is fine
   (`ConvexPolyhedron`). But the hull of, say, two spheres has spherical caps joined by a ruled
   surface — OCCT will not synthesize that exact surface. The honest, general implementation is a
   *mesh* hull (`Manifold.hull` / `hull_points`).

3. **Robust booleans on degenerate meshes.** OpenSCAD code constantly produces coincident faces,
   self-intersections, and near-degenerate geometry that Manifold handles by contract and OCCT
   booleans fail/hang on. To translate real-world `.scad` files reliably you need a backend with
   Manifold's robustness guarantees for at least the troublesome subtrees.

4. **`polyhedron()` with non-planar / non-manifold faces.** Extremely common in generated OpenSCAD.
   `Solid(Shell(faces))` in OCCT requires well-behaved faces; a mesh backend ingests the triangle
   soup directly (`Manifold(Mesh(tri_verts, vert_properties))`, as scad2py does in `modules.py:120`).

5. **`$fn`-faceted output fidelity.** If the goal is to reproduce OpenSCAD's *exact STL*, every
   circle/sphere/cylinder is a specific polygon mesh. Carrying thousands of faceted primitives
   through OCCT booleans is both slow and pointless — the geometry is already a mesh; keep it one.

6. **`surface()` heightmaps** and **imported STL/3MF meshes** — naturally meshes; `import_stl`
   (`importers.py:270`) even returns a *single `Face`* reference, not an editable solid, precisely
   because OCCT does not want to digest large meshes.

7. **Scale at large `$fn` / huge models** (scad2py's `scalemail.scad`, `menger.scad`,
   `condensed-matter.scad` examples) — Manifold's multithreaded mesh booleans are orders of
   magnitude faster than OCCT BREP boolean trees of equivalent size.

**Conclusion:** build123d's OCCT kernel is the right target for the *clean, exact, parametric*
subset of OpenSCAD (cubes, exact cylinders/spheres, extrudes, revolves, offsets, filletable BREP).
But a faithful transpiler must also have a **manifold3d mesh backend** as a sibling — for
`hull`/`minkowski`, degenerate booleans, raw `polyhedron`/`surface`/mesh imports, and `$fn`-faithful
output. The transpiler should be able to **choose the backend per CSG subtree** and convert between
exact BREP and mesh at the boundary (BREP → tessellate → Manifold; Manifold → `brep_from_stl.py`
already exists in build123d for the mesh → BREP direction).

---

## 6. Resolution strategy: how to honor `$fn / $fa / $fs`

Three viable policies; the transpiler should support choosing per-run, and probably default to (B):

**(A) Exact BREP, ignore `$fn` until export ("intent fidelity").**
Emit exact `Circle`/`Sphere`/`Cylinder`/`revolve`. Honor `$fn/$fa/$fs` *only* by recording them as
a tessellation hint used at STL/3MF export time (via `exporters3d.py` / `mesher.py` mesh tolerance,
or by re-deriving a fragment count from `calc.py:get_fragments_from_r`).
*Pros:* cleanest BREP, exact fillets/booleans, tiny STEP files, the model stays parametric.
*Cons:* does **not** reproduce OpenSCAD's mesh; `circle($fn=6)` silently becomes a true circle —
wrong when the facets are *intentional* (hex bolts, faceted vases).

**(B) Hybrid: exact when `$fn` looks like render-tuning, faceted when it looks intentional.**
Heuristic: if `$fn` is unset, or large (≥ some threshold, e.g. ≥ 24, or resolved via `$fa/$fs`),
treat it as resolution tuning → emit **exact** primitives. If `$fn` is small and explicit
(e.g. `$fn=3..12`), the facets are part of the design → emit a **faceted** primitive
(`RegularPolygon`, an explicit `Polygon`, or a mesh-backed prism/polyhedron).
*Pros:* matches designer intent in the common cases; keeps BREP benefits where they apply.
*Cons:* heuristic, occasionally surprising; needs a clear override.

**(C) Facet eagerly, everywhere ("output fidelity").**
Reproduce `get_fragments_from_r` exactly and emit faceted geometry for *every* curved primitive, so
the build123d STL matches OpenSCAD's STL vertex-for-vertex. Best paired with the **mesh backend** —
faceting everything and then feeding OCCT is the worst of both worlds.
*Pros:* exact round-trip parity with OpenSCAD.
*Cons:* throws away every advantage of targeting build123d; no exact fillets, bloated geometry.

**Recommendation:** default to **(B)**, with explicit transpiler flags
`--resolution=exact|hybrid|faithful`. Always *carry* the resolved `$fn/$fa/$fs` values as metadata
on the generated objects (build123d shapes can hold labels/attributes) so export can reproduce
OpenSCAD's mesh density on demand even when the in-memory model is exact BREP. Reuse the *already
ported* `scad2py/calc.py:get_fragments_from_r` as the single source of truth for fragment counts —
do not re-derive the formula.

A subtlety: `$fn/$fa/$fs` are **dynamically scoped** in OpenSCAD (a `$fn` set on a parent applies to
descendants unless overridden). scad2py already threads them as `_fn/_fa/_fs` keyword args plus a
dynamic-scope shim (`runtime/variables.py`, `_resolve_specials` in `modules.py:60`). The build123d
emitter must preserve that scoping when deciding facet counts.

---

## 7. Recommended mapping strategy

1. **Keep a CSG IR.** Do not translate `.scad` straight to build123d calls. Keep scad2py's
   `csg.Node` tree (`csg.py`) as the intermediate representation. It already models the two-stage
   OpenSCAD evaluation and lets a backend reason per-subtree.

2. **Two backends behind one interface.**
   - *Exact/BREP backend* → build123d (OCCT): cubes, exact cylinders/spheres/cones/tori,
     `extrude`, `revolve`, `offset`, clean booleans, transforms, text, DXF/SVG import.
   - *Mesh backend* → `manifold3d` (reuse scad2py's existing runtime essentially unchanged):
     `hull` (3D), `minkowski`, `polyhedron`/`surface`, mesh imports, degenerate booleans,
     `$fn`-faithful output.

3. **Per-subtree backend selection.** Walk the CSG tree; mark each subtree "BREP-capable" or
   "mesh-required". A subtree is mesh-required if it contains `minkowski`, a 3D `hull` of curved
   solids, a `polyhedron`/`surface`/mesh-`import`, or (in faithful mode) faceted primitives.
   Booleans mixing the two convert the BREP operand to mesh at the boundary (tessellate with the
   resolved `$fn`) — *or*, conversely, try the OCCT boolean first and fall back to mesh on failure.

4. **Conversions at the boundary.** BREP → mesh: tessellate via OCCT triangulation (the deviation
   from resolved `$fn`). Mesh → BREP: build123d already ships `brep_from_stl.py` — usable when a
   mesh-backend result must re-enter exact-BREP operations (rare; prefer to keep mesh results as
   mesh through to export).

5. **Default to "hybrid" resolution** (§6 policy B), flag-overridable; always attach resolved
   `$fn/$fa/$fs` as export metadata.

6. **Stage-1 constructs are pure Python.** `for`, `if`, `let`, list comprehensions, functions,
   `assert`, `echo`, `$children`, modifiers `* ! % #` — all resolved at transpile/script time, never
   reaching either geometry backend. `*` and `!` are tree edits; `%` excludes from the result set;
   `#` is a label/color.

7. **Transforms:** rigid + uniform/non-uniform scale → `Location`/`scale()`; a *general*
   `multmatrix` (shear/projective) → OCCT `gp_GTrsf` on the BREP, or apply to mesh — never assume a
   `Location` suffices.

---

## 8. Open questions

1. **Round-trip vs. clean target — which is the product?** Is the goal STL-identical reproduction
   of OpenSCAD output, or a clean parametric build123d model a human will keep editing? This single
   decision sets the resolution policy and whether the mesh backend is the *default* or the
   *fallback*.

2. **`$fn` intent heuristic threshold.** What `$fn` value is the cutoff between "render tuning"
   (→ exact) and "intentional facets" (→ polygon)? Is there a better signal than a magic number —
   e.g. whether the same variable feeds a `$fa/$fs` style expression, or whether the shape is later
   filleted?

3. **3D `hull` of curved solids.** Even with a mesh backend, the *result* is a faceted hull. Is
   that acceptable, or should the transpiler attempt to recover exact spherical caps / ruled
   patches for simple cases (hull of N spheres)? Probably not worth it — note as out of scope.

4. **Non-convex `minkowski` is approximate even in scad2py** (`minkowski_impl.py` falls back to
   `trimesh`/`coacd` convex *approximation*). Accept the approximation, surface a warning, or
   refuse? OpenSCAD itself is exact here (CGAL) — so the mesh backend is *less* faithful than
   OpenSCAD for non-convex minkowski.

5. **OCCT boolean failure handling.** When an OCCT boolean fails/hangs on degenerate input, do we
   (a) auto-fall-back to the mesh backend for that whole subtree, (b) fail loudly, or (c) attempt a
   `clean()`/healing pass first? Auto-fallback is most robust but makes output non-deterministic
   w.r.t. backend.

6. **`color()` and modifiers in the output.** build123d shapes carry `Color`/`label`. Should `#`
   highlight and `color()` both map onto `Shape.color`? Should `%` background geometry be emitted
   at all (as a separate non-participating `Compound`) or dropped?

7. **`text()` fidelity.** OpenSCAD `text` and build123d `Text` both rasterize fonts to outlines but
   via different stacks (FreeType vs. build123d's `text.py`). Glyph outlines, `halign`/`valign`,
   `spacing`, `direction`, and per-platform font resolution will differ. How exact must text be?

8. **`surface()` heightmaps and `import` of meshes** — confirmed mesh-backend territory; but should
   imported STL stay a mesh end-to-end, or be (slowly) converted to BREP via `brep_from_stl.py`
   when a user wants to fillet it?

9. **Performance crossover.** At what model size does the mesh backend become preferable even for
   nominally BREP-capable subtrees (the `scalemail`/`menger` regime)? Worth a benchmark before
   committing to "BREP-first, mesh-fallback".

---

## Appendix: key source references

**scad2py** (`/Users/ochafik/github/scad2py/scad2py/`)
- `csg.py:152` — `Node`; `csg.py:146` — `Modifier`; `csg.py:359` — lazy `dim`; concrete CSG node
  classes `Union`/`Difference`/`Translate`/`LinearExtrude`/`Polyhedron`/`Polygon`/… throughout.
- `runtime/modules.py` — every OpenSCAD primitive/operator as a `@module`; built on `manifold3d`
  (`Manifold`, `CrossSection`, `Mesh`) and `trimesh`. `_resolve_specials` at `:60`.
- `calc.py:38` — `get_fragments_from_r`, the exact `$fn/$fa/$fs` → fragment-count formula ported
  from OpenSCAD's `calc.cc`.
- `minkowski_impl.py` — mesh-based 3D minkowski via convex decomposition + hull of cartesian sums.
- `rendering/rendering.py:27` — `render_geom`, the stage-2 tree walker.

**build123d** (`/Users/ochafik/github/build123d/src/build123d/`)
- `objects_part.py` — `Box:125`, `Cone:169`, `ConvexPolyhedron:222`, `Cylinder:377`, `Sphere:472`,
  `Torus:524`.
- `objects_sketch.py` — `Circle:115`, `Ellipse:152`, `Polygon:187`, `Rectangle:226`,
  `RegularPolygon:302`, `Text:554`.
- `operations_part.py` — `extrude:112`, `revolve:514`, `section:569`, `loft:245`, `thicken:643`.
- `operations_generic.py` — `offset:551`, `mirror:498`, `scale:844`, `project:699`,
  `bounding_box:207`, `sweep:965`.
- `operations_sketch.py` — `make_hull:232`, `make_face:197`.
- `topology/` — BREP topology (`one_d.py`, `two_d.py`, `three_d.py`, `composite.py`).
- `importers.py` — `import_step:135`, `import_stl:270` (returns a single `Face`),
  `import_svg:401`; `import_dxf.py`.
- `mesher.py:124` — `Mesher` (3MF/mesh I/O); `brep_from_stl.py` — mesh → BREP conversion;
  `exporters3d.py` — STL/STEP export & tessellation.

**OpenSCAD documentation**
- Language model: <https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/The_OpenSCAD_Language>
- Transformations (`hull`, `minkowski`, modifiers): <https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Transformations>
- 2D→3D extrusion: <https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/2D_to_3D_Extrusion>
- Special variables / `$fn$fa$fs`, `let`, `assert`: <https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Other_Language_Features>
- Manifold backend: <https://github.com/elalish/manifold> ; integration issue
  <https://github.com/openscad/openscad/issues/4825> ; promotion announcement
  <https://fosstodon.org/@OpenSCAD/113256867413539398>
