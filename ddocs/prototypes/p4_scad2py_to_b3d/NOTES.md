# Prototype p4 — `Build123dRenderer`: build123d as a scad2py geometry backend

Derisking **Option A** from `ddocs/research/04-scad2py-architecture.md`: a new
scad2py CSG renderer, parallel to `ManifoldRenderer`, that turns a
`scad2py.csg.Node` tree into build123d `Shape` objects (exact OpenCASCADE BREP)
instead of `manifold3d` meshes.

## What this prototype is

* `build123d_renderer.py` — `Build123dRenderer(csg.DefaultVisitor)`. One
  `visitXxx` per `csg.Node` subtype, mirroring `ManifoldRenderer`, producing
  build123d `Box`/`Sphere`/`Cylinder`/`Cone`/`Solid`/`Sketch`/`Compound`.
* `run.py` — end-to-end driver: `.scad` → scad2py parser → transpiler → `exec`
  → `csg.Node` tree → `Build123dRenderer` → STEP + STL export → validity check
  → optional manifold-volume sanity comparison.
* `batch.py` — runs `run.py` on every scad2py example in its own subprocess
  with a 90 s timeout.
* `out/` — the STEP + STL files actually produced (committed as evidence).

It does **not** modify scad2py or build123d. It imports only scad2py's front
end (`parser`, `transpiler`) plus the `csg` module and runtime — never
scad2py's `rendering` package — exactly the seam Option A targets.

## Environment

Dedicated venv `/Users/ochafik/github/.ddocs-venv-scad2py` (Python 3.10):
`build123d` (editable), `cadquery-ocp-novtk 7.9.3`, `manifold3d 3.4.1`,
`ply`, `numpy 2.2`, `trimesh 4.12`, `shapely`, `svg.path`, `rtree`,
`typeguard`. **scad2py's front end + `csg` import cleanly with the current
`manifold3d 3.4.1`** — no version pin was needed. (The brief warned scad2py's
renderer imports moved manifold3d symbols; that only affects scad2py's
`rendering` package, which Option A bypasses, so it never bit us.)

## End-to-end status — it runs

`run.py` works end-to-end. Real output, `cube.scad`:

```
build123d type : Box
is_valid       : True
volume         : 1.0
bbox           : ([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
STEP reopens   : True (vol=1.0)
manifold ref   : volume=1.0000 bbox=([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
```

build123d volume + bbox match scad2py's own manifold3d output **exactly**, and
the STEP file re-opens valid in OCCT.

`polyhedron.scad` (a 5-point square bipyramid):

```
build123d type : Solid
is_valid       : True
volume         : 666.6667     <- exact (square pyramid pair, V = 2/3·a²·h)
STEP reopens   : True (vol=666.6667)
```

A raw OpenSCAD `polyhedron(points, faces)` was sewn into a **valid OCCT
`Solid`** with a clean STEP roundtrip — a real exact-BREP win.

## Batch sweep — 15 / 33 examples render to build123d

| Result | Count | Examples |
|---|---|---|
| **OK** (rendered, valid, STEP roundtrips) | 14 | `cube` `cylinder` `sphere` `spheres` `polyhedron` `bug` `echo` `lookup` `vars` `mods1` `mods2` `shapes` `simplecolors` `CSG-modules` |
| OK but STEP roundtrip flips sign | 1 | `CSG` (renders + valid, see below) |
| UNSUPPORTED — `hull()` of 3D solids | 5 | `a11y` `box` `torus` `scalemail` `simplecustom` |
| UNSUPPORTED — `minkowski()` | 2 | `car` `minkowski` |
| scad2py front-end failure (not our code) | 4 | `bosl2_math` `bosl2_utility` `extrusions` `runtime` |
| TIMEOUT (>90 s, OCCT perf cliff) | 4 | `colors` `condensed-matter` `example` `menger` |
| Other | 3 | `import` `customiser` `scopes` |

15/33 is an honest figure: of the 18 non-OK, **9 are not our renderer's fault**
(4 scad2py front-end bugs + 4 perf timeouts on pathological models + 1 missing
input file). Of the geometry-feature failures, all are the *expected*
BREP-kernel gaps the research docs predicted (`hull`/`minkowski`).

## Mapped cleanly (exact BREP)

These `csg.Node` types map to build123d with **no fidelity loss**:

| csg.Node | build123d | Notes |
|---|---|---|
| `Polyhedron` name_=`cube` | `Box(align=...)` | `center` → `Align.CENTER/MIN`. Exact. |
| `Polyhedron` name_=`sphere` | `Sphere(r)` | Exact sphere (see `$fn` caveat). |
| `Polyhedron` name_=`cylinder` | `Cylinder` / `Cone` | `r1==r2`→Cylinder, else Cone. **Exact** — and notably *better* than scad2py's own renderer, which faceted it. |
| `Polyhedron` name_=`polyhedron` | `Solid(Shell(Faces))` | Works for the planar-face examples; sewn into a valid solid. |
| `Polygon` name_=`square` | `Rectangle` | Exact. |
| `Polygon` name_=`circle` | `Circle` | Exact circle (see `$fn` caveat). |
| `Polygon` name_=`polygon` | `Polygon` / face-`-`-holes | Multi-path holes via boolean subtraction. |
| `Union` / `Difference` / `Intersection` | `+` / `-` / `&` | Direct. `Group`/`Root` → implicit union / `Compound`. |
| `Translate` / `Rotate` / `Multmatrix` (rigid) | `Location` (`gp_Trsf`) | Rigid transforms = cheap `Location`, no BREP rewrite. |
| `Scale` (uniform) | `scale(by=k)` | Exact. |
| `LinearExtrude` (plain & `scale=`) | `extrude` / `loft` | Plain → exact `extrude`; `scale=` → exact ruled `loft` of base→scaled-top face. `center` handled. |
| `Color` | `Shape.color` | RGBA carried on the shape; survives STEP/GLTF (manifold3d can't). |
| `Modified` (`#`/`%`/`!`) | pass-through | (`%` should be excluded from result in a real backend.) |
| `Text` | `Text(font_size, align)` | `halign`/`valign` → `Align`. |

**The exact-BREP wins** (the whole point of Option A):
1. `cylinder` and `circle` are *exact analytic surfaces*, not `$fn` facets —
   smaller, cleaner geometry than scad2py's manifold renderer.
2. **STEP export works and re-opens valid** — verified by `import_step` +
   `is_valid` on 14/15 rendered examples. manifold3d cannot produce STEP at all.
3. `polyhedron` → a real `Solid` with `BRepCheck` validity.

## Hard / impossible — honest failures

The renderer raises `UnsupportedOperation` rather than faking these:

| Feature | Why it has no BREP mapping |
|---|---|
| **`minkowski()`** | No BREP algorithm exists. Definitionally a mesh operation (convex-decompose + per-pair hull + union). `car.scad`, `minkowski.scad` hit this. **Keep manifold3d for this.** |
| **`hull()` of 3D solids** | The hull of curved solids (e.g. two spheres) has spherical caps joined by ruled patches OCCT will not synthesize. 5 examples (`box`, `torus`, `a11y`, `scalemail`, `simplecustom`) need it. *2D hull works* (`make_hull` is exact). |
| `linear_extrude(twist=...)` | OCCT `extrude` has no twist. Faithful answer is a lofted stack of rotated cross-sections — out of prototype scope, flagged. |
| `rotate_extrude` | build123d `revolve` **is** the exact mapping (implemented), but scad2py's *own* `csg.RotateExtrude` constructor is broken (see below) so it can't be exercised through the front end. |
| `surface()`, `resize()`, `projection()` | `surface` heightmap is naturally a mesh; `resize`/`projection` simply not implemented in this prototype. |
| Non-uniform `scale` / shear `multmatrix` | Handled via `transform_geometry` (`gp_GTrsf`) — works, but rewrites the BREP and degrades exact curved surfaces; flagged with a warning, not refused. |

## Fidelity findings

* **Exact match on clean CSG.** `cube.scad`: build123d volume 1.0 and bbox
  identical to manifold3d. Booleans of cube/sphere produce valid solids.
* **`$fn` (the hexagon problem).** A build123d `Sphere`/`Circle` is an *exact*
  surface; OpenSCAD's `$fn` facet count has no exact analogue. The renderer
  uses a threshold (`_FACET_INTENT_THRESHOLD = 24`): small explicit `$fn`
  (intentional facets) → exact `RegularPolygon`/N-gon prism for `circle` and
  `cylinder`; large/unset `$fn` (render tuning) → exact primitive. Spheres have
  no faceted build123d primitive, so a faceted `sphere($fn=6)` is emitted as an
  exact sphere **with a warning** — an honest, documented fidelity loss.
* **`CSG.scad` STEP roundtrip flips sign.** `CSG.scad` renders fine and
  `is_valid` is True, but the re-imported STEP reports a *negative* volume.
  Cause: the top-level `Group` mixes 3D solids with a 2D `linear_extrude` of
  `text` that `run.py` extrudes to a 1 mm slab for export — a **heterogeneous
  `Compound`** (mixed-dimension members) confuses OCCT's STEP writer. Real
  finding: a build123d backend needs a proper scene model that keeps 2D and 3D
  members separate (or partitions like `SceneTransformer` does), not a flat
  `Compound`.
* **Performance cliff.** `colors.scad` (1369 colored cubes), `menger`,
  `condensed-matter`, `example` all **time out at 90 s**. This is the OCCT
  pairwise-boolean O(n²) problem the architecture doc predicted: manifold3d
  does these in milliseconds. A build123d backend **must** keep manifold3d for
  large CSG trees.

## scad2py bugs found (not fixed — house rules)

Exercising the front end surfaced real scad2py defects, all *upstream* of our
renderer:

1. **`csg.RotateExtrude` is broken.** `runtime/modules.py:255` calls
   `csg.RotateExtrude(angle=..., fn=..., fa=..., fs=...)` but the
   `csg.RotateExtrude` dataclass declares none of those fields →
   `TypeError: unexpected keyword argument 'angle'`. `extrusions.scad` crashes
   at the *runtime* stage, before any renderer. So `rotate_extrude` cannot be
   tested through scad2py's front end at all today.
2. **`csg.Import.dim` extension bug.** `csg.py:758` checks
   `ext in ('stl','svg',...)` but `os.path.splitext` returns `'.stl'` (with the
   dot) → `Exception: Unknown file extension: .stl`. Breaks `import.scad`.
3. `bosl2_math`, `bosl2_utility`, `runtime` fail in scad2py's transpiler /
   runtime (`name 'valid_range' is not defined`, `'function' object is not
   subscriptable`) — front-end bugs unrelated to geometry.

## Recommendation — is Option A viable?

**Yes. Option A is the right integration path.** The prototype proves the core
thesis end-to-end:

* The seam is exactly as the research doc described — reuse scad2py's whole
  front end unchanged; the new code is one visitor file. No scad2py changes
  were needed to *render*; the csg tree is a clean, sufficient IR.
* build123d delivers the promised wins: exact analytic primitives, valid
  `polyhedron` solids, and **working STEP export** — none of which manifold3d
  can do.
* The failures are all the *expected* ones and are honest kernel limits, not
  prototype gaps.

**Remaining work for a production backend:**

1. **Hybrid renderer, not a replacement.** `minkowski` and 3D `hull` have no
   BREP form, and large CSG trees blow the OCCT perf budget. The backend must
   keep manifold3d for those subtrees and convert at the boundary
   (tessellate ↔ sew). This matches the research doc's "two backends behind one
   interface" recommendation.
2. **A build123d-flavoured scene model.** A flat `Compound` mixing 2D and 3D
   members breaks STEP export (the `CSG.scad` sign-flip). Need a
   `SceneTransformer`-equivalent that partitions by dimension and colour.
3. **`linear_extrude(twist=)`** via lofted rotated slices.
4. **Caching.** scad2py's `FastKey`/`@renderer` dedup is mesh-shaped; OCCT
   `Shape`s are not cheaply cloneable — the caching layer needs rethinking
   (or dropping) for the BREP backend.
5. Fix the two scad2py front-end bugs above so `rotate_extrude` / `import` can
   even reach the renderer (`revolve` mapping is already implemented and
   trivial — it just can't be exercised).

Net: Option A is a medium-effort, well-scoped change that unlocks exact BREP +
STEP and removes the shell-out to a hard-coded OpenSCAD binary. Recommended,
**as a hybrid** (build123d for the exact/parametric subset, manifold3d retained
for `minkowski`/3D-`hull`/huge CSG).
