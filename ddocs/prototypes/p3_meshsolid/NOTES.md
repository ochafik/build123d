# MeshSolid prototype — design notes

> Prototype P3: a `manifold3d`-backed mesh solid that coexists with build123d's
> BREP shapes. Goal: derisk the integration design for build123d issue **#1228**
> ("optional manifold extra"). Files: `meshsolid.py` (the module), `demo.py`
> (runnable end-to-end demo), this file.
>
> Environment as used: **manifold3d 3.4.1** (not the 2.3.1 the wave-3 research
> doc introspected — the upstream 3.x API the doc *predicted* is what is
> installed: `batch_boolean`, `OpType`, `Mesh.merge()`, double-precision kernel,
> `minkowski_*`). build123d editable fork, OCP 7.9.

---

## 1. What the prototype is

`MeshSolid` wraps a `manifold3d.Manifold` and exposes a build123d-flavoured
surface:

| Concern | API |
|---|---|
| Construct from BREP | `MeshSolid.from_build123d(part)` — tessellates in |
| Construct from raw mesh | `MeshSolid.from_mesh(verts, tris)` — welds an unwelded soup |
| Construct from `Manifold` | `MeshSolid(manifold)` |
| Primitives | `MeshSolid.box/sphere/cylinder(...)` — mirror build123d defaults (origin-centred) |
| Booleans | `a + b`, `a - b`, `a & b`; `MeshSolid.fuse_all([...])` (batch) |
| Transforms | `.translate()`, `.rotate()` (deg), `.scale()`, `.move(Location)` |
| Interop out | `.to_solid()` / `.to_part()` — explicit, expensive BREP bake |
| Color | `.color` getter/setter, carried through booleans as per-vertex RGB |
| Export | `.export_stl()`, `.export_3mf()` (cheap-ish), `.export_step()` (needs bake) |
| Free functions | `mesh_fuse()`, `mesh_cut()`, `mesh_intersect()` |

## 2. Real demo output

Run: `/Users/ochafik/github/.ddocs-venv/bin/python demo.py` — verbatim:

```
======================================================================
2. A chain of fast booleans -- the perf point of the exercise
======================================================================
  MeshSolid: 72 spheres fused + subtracted in 35.1 ms
  result: MeshSolid(tris=21708, verts=10760, volume=98567.803, ...)
  native OCC: same 72 subtractions in 2499.1 ms
  speedup: 71.3x

======================================================================
3. Color carried through a boolean chain
======================================================================
  red box + blue sphere -> dominant color: Color: (0.0, 0.0, 1.0, 1.0) is 'BLUE'
  combined - native Cylinder (auto-tessellated): MeshSolid(tris=1262, ...)
  color survives the mixed-operand cut: Color: (0.0, 0.0, 1.0, 1.0) is 'BLUE'

======================================================================
5. Convert a MeshSolid result back to a build123d BREP Solid
======================================================================
  to_solid() bake took 166.7 ms
  -> build123d Solid: is_valid=True, volume=10283.99 (mesh said 10283.99),
     faces=1262, color=Color: (0.0, 0.0, 1.0, 1.0) is 'BLUE'
  STEP-exported the baked solid: demo_baked.step (3053479 bytes)
  baked solid further booleaned in native OCC: volume=8054.76
```

**Headline numbers:** the mesh boolean chain is **~71× faster** than the native
OCC equivalent (35 ms vs 2.5 s for 72 subtractions), the mesh→BREP bake is
**valid** (`is_valid=True`, volume matches the mesh exactly), and the baked
solid round-trips back into native OCC booleans. Color survives the whole
chain including a mixed `MeshSolid - native Part` cut.

---

## 3. Does a parallel mesh-backed shape feel natural next to BREP shapes?

**Mostly yes — with two visible seams.**

What feels native:
- `+ - &` operators read identically to build123d's algebra API. A user who
  knows `Box(10,10,10) - Cylinder(3,10)` immediately reads `MeshSolid` code.
- Origin-centred primitives, degree-based rotation, and `Location`/`Pos`/`Rot`
  via `.move()` all map 1:1 — manifold3d and build123d share conventions
  (right-handed, Z-up, degrees, global X-Y-Z Euler order). No surprises.
- `.volume`, `.area`, `.bounding_box()`, `.is_valid` mirror `Shape` and are
  *cheaper* than build123d's (manifold caches; build123d re-runs `BRepGProp`
  every call).

What does **not** feel native (the awkward bits):

1. **`MeshSolid` is not a `Shape`.** It cannot be — build123d's `Shape.__init__`
   unconditionally calls `downcast(obj)`, and `downcast`/`shapetype`
   (`shape_core.py:3684`/`3762`) are free functions hardwired to `TopoDS`.
   A non-`TopoDS` `.wrapped` blows up at construction. So `MeshSolid` cannot be
   passed to anything typed `Shape`: builder contexts (`BuildPart`), `ShapeList`,
   `pack()`, `export_gltf` assembly walks, joints. It is a *sibling*, not a
   subclass. **This is the single biggest integration friction.**

2. **The `.wrapped`-is-`TopoDS` assumption is pervasive.** Any build123d code
   that does `shape.wrapped` expects an OCP object. `MeshSolid` deliberately
   has no `.wrapped`; it exposes `.manifold` instead. Mixing the two worlds
   therefore *must* go through the explicit `from_build123d()` / `.to_solid()`
   bridge — there is no implicit interop, by design.

3. **Operator interop between `MeshSolid` and native `Part` is asymmetric.**
   `mesh_solid - native_part` works (the prototype auto-tessellates the BREP
   operand — see `_coerce`). But `native_part - mesh_solid` would hit
   `Shape.__sub__`, which calls `_bool_op` and expects a `TopoDS` operand — it
   cannot see a `MeshSolid`. `__radd__` papers over `+` only. **Full symmetry
   is impossible without patching `Shape`'s operators**, which is out of scope
   for an opt-in extra. Recommendation: document that the *mesh* operand must
   be on the left, or steer users to the free-function API where direction is
   explicit.

---

## 4. Lazy vs eager BREP conversion

The prototype draws a hard line: **mesh→BREP is always explicit and never
implicit.**

- Construction (`from_build123d`) and all booleans/transforms stay in mesh
  space. manifold3d ops are themselves lazy (transforms accumulate, evaluation
  is forced by touching geometry) — so a 500-deep CSG chain costs almost
  nothing until you ask for `.volume` or `.to_solid()`.
- `.to_solid()` / `.to_part()` are the *only* place the expensive sew runs.
  In the demo a 1262-triangle result bakes in **167 ms**; that scales roughly
  linearly with triangle count, so a 100k-triangle result is ~10 s+ — genuinely
  expensive, and the API names + docstrings shout about it.
- `.export_step()` necessarily bakes (STEP is BREP). `.export_stl()` /
  `.export_3mf()` are advertised as "cheap" but **currently are not fully** —
  build123d's `Mesher` only consumes `Shape`s, so the prototype still bakes a
  `Solid` first. See recommendation 5 below.

**Verdict:** lazy/explicit is the right call. An implicit `MeshSolid → Solid`
coercion (e.g. making `MeshSolid` quack like `Shape`) would hide minutes-long
sews behind innocent-looking attribute access. Keep the bake a verb the user
types.

---

## 5. Sharp edges found while prototyping

- **Tessellation produces an unwelded soup.** `Shape.tessellate()` returns
  per-face vertex blocks (a box → 24 verts, not 8); manifold3d rejects that as
  `Error.NotManifold` because every face seam is an open edge. **Fix used:**
  manifold3d 3.x's `Mesh.merge()` — build the soup, let manifold weld open
  edges within tolerance. Cleaner and more robust than a hand-rolled grid-snap
  dedup (which the research doc suggested for 2.3.1, before `merge()` existed).
- **Color "dominant" heuristic is crude.** Color is carried as per-vertex RGB
  (channels 3–5) via `set_properties`; it genuinely survives booleans (verified).
  But reading *one* color back out is lossy: the demo's red box + blue sphere
  reports **blue**, because the sphere simply has more vertices, so the modal
  color is blue. Per-vertex color is correct *as data*; collapsing it to a
  single `Color` is the lossy step. For a real integration, prefer
  `run_original_id` integer tags + a Python `id→Color` map (exact, per-source-
  solid) over modal per-vertex RGB — see recommendation 4.
- **manifold3d uses `^` for intersection**, build123d uses `&`. The prototype
  maps `MeshSolid.__and__` → `manifold ^ manifold` so the *build123d*
  convention wins at the public surface. Easy to get wrong.
- **No exact geometry, ever.** A `MeshSolid` sphere baked to a `Solid` is 1152
  planar faces, not one spherical face. `geom_type`, fillet/chamfer, clean STEP
  are all gone. The demo's STEP file is 3 MB for a trivial shape. This is
  inherent and must be communicated, not "fixed".
- **No 2D / no wires / no sketches.** manifold3d's `CrossSection` exists but
  `MeshSolid` is 3D-only. build123d's `Sketch`/`Curve` have no mesh analogue
  here; a full integration would need a parallel `MeshSketch` over
  `CrossSection`, or to keep 2D on OCC.

---

## 6. Concrete recommendations for the real integration

**Ship it as an opt-in `build123d[manifold]` extra (issue #1228) — do NOT make
it core, and do NOT try to make `.wrapped` polymorphic.** Ranked concretely:

1. **Primary surface: a free-function API.** `mesh_fuse()`, `mesh_cut()`,
   `mesh_intersect()` taking and returning *build123d `Shape`s* (tessellate in,
   sew out under the hood) is the lowest-friction, lowest-risk deliverable. It
   needs **zero changes to the `Shape` hierarchy**, slots into existing code
   (`result = mesh_cut(block, *holes)`), and directly fixes the documented OCC
   pain (mandatory `ShapeUpgrade` clean per op, pairwise O(n²) accumulation, no
   fuzzy tolerance). This should be phase 1.

2. **Secondary surface: a `MeshSolid` value type** (this prototype) for users
   who want to *stay in mesh space* across a long CSG chain and pay one sew at
   the end. It is a standalone class, not a `Shape` subclass — accept that.
   Provide `from_build123d()` / `.to_solid()` as the explicit bridge. This is
   the right home for scad2py's CSG evaluation: each OpenSCAD primitive → a
   `MeshSolid`, each `union/difference/intersection` → an operator, only
   `import`/export → `.to_solid()`.

3. **Do not attempt the invasive options.** A polymorphic `.wrapped`, a
   `MeshSolid(Shape)` subclass, or a mesh-mode flag inside `_bool_op` all
   require generalising `downcast`/`shapetype`/`Shape.__init__` — a deep, risky
   change to core for an *optional* feature. The `composite_factories` registry
   helps for *result typing* but does not solve the `TopoDS`-everywhere
   assumption. Out of scope for an extra.

4. **Color: use `run_original_id`, not modal per-vertex RGB.** manifold3d 3.x's
   `reserve_ids` + per-run `original_id` is an *exact* integer provenance
   channel that survives booleans without float interpolation. Keep a Python
   `{id: Color}` map. The prototype's `set_properties` RGB approach proves color
   *can* propagate, but the read-back is lossy (demo picks the wrong dominant
   color). Reserve per-vertex properties for genuine gradients only.

5. **Add a `Mesher` path that takes `(verts, tris)` arrays directly.** Today
   `Mesher.add_shape` only accepts `Shape`s, forcing `.export_stl()` to bake a
   `Solid` first — defeating "cheap mesh export". A `Mesher.add_mesh(verts,
   tris, color)` (or letting `MeshSolid` implement a small protocol the mesher
   recognises) makes STL/3MF export of mesh-origin geometry genuinely free.
   This is a small, self-contained build123d core change worth doing.

6. **Reuse `Mesher._get_shape`'s sew logic for the bake**, but expose it as a
   first-class `Solid.from_mesh(verts, tris)` rather than copy it. The
   prototype's `_mesh_to_solid` is essentially that function; it should live in
   build123d core (the research doc flags the same need). Because manifold3d
   *guarantees* watertight 2-manifold output, the sew reliably closes to a valid
   `Solid` (demo: `is_valid=True`) — unlike sewing arbitrary STL.

7. **Pin `manifold3d >= 3.x`.** The 3.x line has the double-precision kernel
   (no size-dependent ε loss), `batch_boolean` (one-pass N-way union — the
   prototype uses it in `fuse_all`), and `Mesh.merge()` (the welding helper the
   tessellation bridge depends on). The 2.3.1 wheel the research doc introspected
   lacks all three.

8. **Tessellation tolerance is a real API knob, expose it.** `from_build123d()`
   takes `linear_tolerance` / `angular_tolerance`. The bridge passes *absolute*
   linear deflection (build123d's exporters default to *relative*, which is
   wrong for CSG where parts of different sizes must align). Default chosen here
   is 0.1 mm absolute — a real integration should make this a documented,
   tunable contract since it sets the fidelity floor for everything downstream.

### One-line summary

Ship `build123d[manifold]` as an opt-in extra exposing **(a)** a free-function
`mesh_fuse/cut/intersect` over plain `Shape`s for the 90% case and **(b)** a
standalone `MeshSolid` value type for staying in mesh space; keep mesh↔BREP
conversion explicit; do **not** make `MeshSolid` a `Shape` subclass or touch
`downcast`/`shapetype`.
