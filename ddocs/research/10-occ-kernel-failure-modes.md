# 10 — The OCC Kernel and its Boolean Failure Modes (build123d view)

> Research doc 10. Motivates the manifold3d integration: build123d's booleans
> route through OpenCASCADE's Boolean Operations Algorithm (BOPAlgo), which is
> slow on many-operand CSG and fragile on near-degenerate input. This doc is the
> kernel deep-dive — *why* OCC is slow/fragile, *what* it nonetheless does that a
> mesh kernel cannot, and *which* operations should move to manifold3d.
>
> Companion docs (do not duplicate): `01-build123d-architecture.md` (the `Shape`
> hierarchy, `_bool_op` walkthrough, extension seams), `02-build123d-mesh-paths.md`
> (tessellate ↔ sew round-trip). A parallel CadQuery doc covers the same kernel —
> `cadquery/ddocs/explorations/02-occ-opencascade-kernel.md` — and is the source
> for the OCP packaging / hands-on failure-reproduction details. CadQuery and
> build123d link the **same** OCCT 7.9.x binaries (the `cadquery-ocp` /
> `cadquery-ocp-novtk` wheel), so the kernel behaviour is identical; only the
> Python wrapper differs.
>
> build123d file/line references are against `/Users/ochafik/github/build123d/src/build123d/`.
> Benchmark numbers are from `ddocs/prototypes/p2_boolean_benchmark/` (real run,
> `results.json` / `full_run.log`, 2026-05-22).

---

## 1. What OCCT is

**Open CASCADE Technology (OCCT)** is a large C++ geometric-modeling kernel — the
open-source descendant of the CAS.CADE kernel built by Matra Datavision in the
early 1990s, now maintained by Open Cascade SAS. It is a **boundary-representation
(B-Rep)** kernel: a solid is stored as its exact bounding surfaces and curves, not
as voxels or triangle soup.

build123d depends on it through `cadquery-ocp-novtk >= 7.9, < 8.0`
(`pyproject.toml:36`) — the VTK-free build of the CadQuery OpenCASCADE Python
bindings, OCCT **7.9.x**. The Python module is `OCP`; build123d imports OCCT
sub-modules directly, with **no abstraction layer** — `topology/shape_core.py:72-138`
pulls in ~40 `OCP.*` modules by name (`01-build123d-architecture.md` §3.2).

### 1.1 Topology vs. geometry

OCCT separates **topology** (`TopoDS` — the combinatorial structure: what connects
to what, with orientation and a `TopLoc_Location` transform) from **geometry**
(`Geom` / `gp` — the exact curves and surfaces). The topological hierarchy is
`Compound ⊃ CompSolid ⊃ Solid ⊃ Shell ⊃ Face ⊃ Wire ⊃ Edge ⊃ Vertex`. A `Face`
references a `Geom_Surface`; an `Edge` references a `Geom_Curve` plus a 2D
**p-curve** in each owning face's UV space. **Geometry is exact and analytic** — a
cylindrical face is a true infinite-precision cylinder, a fillet a true blend
surface. Every build123d `Shape` wraps exactly one `TopoDS_*` in `.wrapped`.

### 1.2 The toolkit (`libTK*`) structure

OCCT ships as ~80 `libTK*` shared libraries (versioned `7.9.3` in the wheel's
`.dylibs/`). The ones that matter here:

| Toolkit | Role |
|---|---|
| `libTKernel`, `libTKMath` | base types, `Precision`, linear algebra |
| `libTKG2d`, `libTKG3d`, `libTKGeomBase` | `Geom`/`gp` geometry |
| `libTKBRep` | `TopoDS` topology + `BRep` geometry-on-topology |
| `libTKGeomAlgo`, `libTKTopAlgo` | intersection / curve-surface algorithms |
| **`libTKBO`** | **the Boolean Operations Algorithm — `BOPAlgo`, `BOPDS`, `BOPTools`** |
| `libTKBool` | `BRepAlgoAPI` — the user-facing Boolean API over `TKBO` |
| `libTKShHealing` | `ShapeFix`, `ShapeUpgrade`, `ShapeAnalysis` — shape repair |
| `libTKPrim`, `libTKFillet`, `libTKOffset` | primitives, fillet/chamfer, offset/sweep |
| `libTKMesh` | `BRepMesh` tessellation (B-Rep → triangles) |
| `libTKDESTEP`, `libTKDEIGES`, `libTKDESTL`, `libTKDEGLTF` | data exchange |

### 1.3 The tolerance model — the root of the pain

OCCT is **not** an exact-arithmetic kernel. Every vertex, edge and face carries a
**tolerance** — a radius within which the entity "is" at its nominal position.
build123d's own constant `TOLERANCE = 1e-6` (`geometry.py:91`) is its working
epsilon; OCCT's `Precision::Confusion()` default is `1e-7`. A "point" is really a
fuzzy ball. Two surfaces that *should* meet in a clean curve can, after tolerance
propagation, meet in a region; an edge can drift outside the tolerance of the face
that owns it. Booleans **grow** tolerances as they chain. This single design
choice is behind almost every failure mode in §3.

---

## 2. The Boolean Operations Algorithm (BOPAlgo)

build123d's `fuse`/`cut`/`intersect`/`split` all funnel into OCCT's Boolean
Operations Algorithm. Understanding *why it is expensive* is the whole point.

### 2.1 The API surface and how build123d drives it

`OCP.BRepAlgoAPI` exposes `BRepAlgoAPI_Fuse` (union), `_Cut` (difference),
`_Common` (intersection), `_Section` (intersection *curves only*),
`_Splitter` (split arguments by tools, keep all pieces), and the bases
`_BuilderAlgo` / `_BooleanOperation`. Under the hood every one of these is the
**Boolean Operations Algorithm** in toolkit `TKBO`, namespace `BOPAlgo`
(`BOPAlgo_PaveFiller`, `BOPAlgo_Builder`, `BOPAlgo_BOP`) operating over the data
structure `BOPDS`.

build123d's single chokepoint is `Shape._bool_op` (`shape_core.py:2459`):

1. Compute the highest-`order` class among all inputs so the result is cast
   correctly (Solid 3.0 > Face 2.0 > Edge 1.0 …) — `shape_core.py:2480-2485`.
2. Pack args/tools into `TopTools_ListOfShape` — `:2490-2498`.
3. Zero-shape shortcuts (empty tool list etc.) return early — `:2500-2521`.
4. `operation.SetArguments(arg)` / `SetTools(tool)`, `SetRunParallel(True)`,
   `operation.Build()`, then `downcast(operation.Shape())` — `:2524-2530`.
5. **Auto-clean**: `ShapeUpgrade_UnifySameDomain` (§3.5) — `:2533-2540`.
6. Unwrap redundant compounds, re-type, copy label/color — `:2543-2558`.

The public methods: `cut` (`shape_core.py:1219`) → `BRepAlgoAPI_Cut`; `fuse`
(`:1380`) → `BRepAlgoAPI_Fuse`; `intersect` (`:1455`) dispatches to per-subclass
`_intersect` which uses `BRepAlgoAPI_Common` (e.g. `three_d.py:511`); `split`
(`:2003`) → `BRepAlgoAPI_Splitter`. Notably, **`fuse` is the only method that
exposes the robustness knobs** — `glue` and `tol` (`shape_core.py:1397-1401`);
`cut`, `intersect` and `split` give the caller no `SetFuzzyValue` / `SetGlue`
access at all.

### 2.2 How a Boolean actually runs internally

The algorithm has two big phases over `BOPDS` (the master data structure
`BOPDS_DS`, plus `BOPDS_ShapeInfo`, `BOPDS_Interf*`, `BOPDS_PaveBlock`,
`BOPDS_FaceInfo`):

**Phase 1 — Intersection (`BOPAlgo_PaveFiller`).** Compute *every* geometric
interference between sub-shapes of all arguments, in strict ascending dimension:
Vertex/Vertex → Vertex/Edge → Edge/Edge → Vertex/Face → Edge/Face →
**Face/Face** → solid interferences. The Face/Face step is the expensive one: it
intersects exact analytic/NURBS surfaces and produces **section curves**
(`BOPDS_Curve`) and **section points** (`BOPDS_Point`). Intersection points on a
curve become **paves** (a `(vertex, parameter)` pair); the curve segments between
consecutive paves are **pave blocks** (`BOPDS_PaveBlock`). Each pave block carries
a *shrunk range* — the sub-segment that, after subtracting vertex/curve tolerance,
can genuinely interfere. This phase is `O(F²)` in face count in the worst case
(every face tested against every other) and dominated by exact surface/surface
intersection — which for two NURBS or two trimmed cylinders is itself an iterative
numerical solve.

**Phase 2 — Building (`BOPAlgo_Builder` / `BOPAlgo_BOP`).** Split edges and faces
at the paves/section curves, then reassemble: split edges → split faces → split
solids, classify each resulting cell's state (`IN` / `OUT` / `ON`) relative to the
other arguments, and keep the cells the operation asks for. **Fuse** keeps `OUT`
cells (and requires equal-dimension arguments); **Common** keeps `IN`+`ON`;
**Cut** removes tool cells from the object. A *history* is tracked (which input
sub-shape generated/modified/deleted which output).

**General Fuse (GFA).** `BOPAlgo_Builder` (exposed as `BRepAlgoAPI_BuilderAlgo`)
is the generalization: intersect N arbitrary-dimension arguments and return *every*
cell, with shared sub-shapes wherever they interfere. **Fuse/Cut/Common and the
Splitter are all just "GFA + select cells."** This is the natural N-ary entry
point — and the natural mirror for a manifold3d batch-Boolean.

### 2.3 Why it is inherently expensive

The cost is intrinsic to exact B-Rep, not an implementation defect:

- Every Face/Face intersection is an exact surface/surface solve producing a new
  analytic/NURBS section curve, plus a *new p-curve reconstruction* of that curve
  in each face's UV space. There is no cheap path.
- The result must remain a *valid B-Rep*: every new edge needs p-curves on every
  owning face, every tolerance must be reconciled, every face re-trimmed.
- The pairwise interference search is super-linear in sub-shape count.

A mesh kernel does none of this: a triangle/triangle test is a handful of dot
products, and the result is just more triangles — no p-curves, no analytic
re-fitting, no tolerance reconciliation. That asymmetry is the entire
performance argument (§4).

---

## 3. Failure modes

OCCT Booleans usually produce correct results on *clean* input. They fail —
sometimes loudly, often **silently** — on the near-degenerate input that real CAD
and (especially) transpiled-OpenSCAD code routinely produce. The documented
precondition (OCCT Boolean Operations spec) is strict and *frequently violated in
practice*: arguments must be **valid** (`BRepCheck_Analyzer`-clean),
**non-self-interfered** (coincident sub-shapes must *share* topology, not merely
overlap geometrically), and have **C1-or-better** continuity. build123d does
**not** enforce any of this before calling `Build()`.

### 3.1 Coincident / near-coincident faces

The most common case in CSG: two boxes flush against each other, an
OpenSCAD-style zero-gap stack. When faces are *exactly* coplanar BOPAlgo can
handle it, but the moment they are *near*-coincident (offset by less than the
working tolerance) the algorithm must decide whether they are "the same surface."
Below `~Precision::Confusion()` (`1e-7`) a real gap is **silently swallowed** and
two solids merge into one with no error and no warning; just above it, a real gap
survives. The "correct" answer flips on an invisible threshold. The benchmark's
`sub_tolerance_offset_cut` case (a tool offset `1e-7` from a box face,
`workloads.py:298`) is exactly this trap — it ran "ok" and produced a clean volume
*because the sliver was absorbed*, which is luck, not robustness.

### 3.2 Tangent surfaces — point/line contact

A sphere tangent to a box at a single point, or two cylinders tangent along a
line, is a **measure-zero** contact — not a real Face/Face intersection. OCCT
returns `IsDone()==True` but hands back a `Compound` of the two *unfused* inputs
side-by-side (a fuse that did not fuse), or, for `Common`, an *empty* shape.
Pedantically correct, almost always not what the user wanted. The benchmark's
`edge_touch_union` and `cylinder_tangent_union` (`workloads.py:269,303`) exercise
this; both completed but the topology (faces=12, faces=7) reflects un-merged
inputs rather than a clean weld.

### 3.3 Self-intersections and invalid input

BOPAlgo *assumes* non-self-interfered arguments. A self-intersecting
primitive — e.g. a torus with minor radius > major radius — is accepted by
`BRepCheck_Analyzer` as **valid** (OCCT's checker does not detect analytic-surface
self-intersection), and a Boolean will run on it and return a "done", "valid"
result that is almost certainly wrong topology. Worse: a B-Rep **reconstructed
from a mesh** (build123d's `Mesher._get_shape` per-triangle sew,
`02-build123d-mesh-paths.md` §2.2) is routinely `BRepCheck`-**invalid** because
sewing tolerances across thousands of near-coincident triangle edges do not
reconcile — yet OCCT will happily Boolean it. This is a *silent-corruption
pipeline*: invalid in, "valid" out, wrong everywhere downstream.

### 3.4 Tolerance mismatches and sliver faces

When two arguments carry different tolerances (very common after chained
Booleans — tolerance grows per op), the intersection phase can produce **sliver
faces**: faces thinner than the tolerance of their own edges. A genuine thin
overlap region can fall off the "tolerance cliff" and **silently disappear** — a
`Common` of two barely-overlapping solids returns an empty shape with
`IsDone()==True`. A downstream consumer sees a valid, empty `Shape` and no signal
anything went wrong. build123d's `is_valid` (`BRepCheck_Analyzer`,
`shape_core.py:472`) is the only correctness gate and **`_bool_op` never calls
it** — invalid and empty results propagate unchecked.

### 3.5 The "fuzzy boolean" tolerance — `SetFuzzyValue`

`SetFuzzyValue(tol)` raises the *minimum* tolerance used during the intersection
phase: "treat anything closer than `tol` as coincident." It is the standard knob
for forcing a Boolean through near-degenerate input — if two faces are *almost*
coplanar, a large fuzzy value makes the algorithm treat them as exactly
coincident instead of generating slivers. It genuinely fixes a real failure
class. But it is a **global, manual, magic number with no automatic value**: too
small and it does nothing; too large and it merges features the user wanted
distinct. OCCT's own guidance: the fuzzy value "should be significantly smaller
than the minimum length of any geometry," and most fuzzy-boolean *problems* come
from a fuzzy value chosen too large. build123d exposes it only on `fuse(tol=...)`
(`shape_core.py:1401`); `cut`/`intersect`/`split` cannot set it at all. **There is
no global "robust mode."**

### 3.6 The gluing options — `SetGlue`

`BOPAlgo_GlueEnum` (`GlueShift`, `GlueFull`) is a **fast path**: it tells the
algorithm the arguments only *touch* along coincident faces/edges and do not truly
interpenetrate, so the expensive general Face/Face intersection can be skipped
(`GlueShift` merges coincident vertices via local coordinate shifts; `GlueFull`
merges all coincident sub-shapes). It is fast and *safe only when the precondition
holds*. build123d's `fuse(glue=True)` sets `BOPAlgo_GlueShift`
(`shape_core.py:1399`); with the wrong glue mode on interpenetrating shapes the
result is silently wrong.

### 3.7 When OCC returns invalid vs. throws vs. hangs

Three distinct outcomes, in roughly decreasing order of how often they bite:

- **Silently invalid / silently wrong (most dangerous).** `IsDone()==True`,
  `BRepCheck` may even pass, but the shape is geometrically wrong (sub-tolerance
  merge, vanishing sliver, self-intersecting input, mesh-reconstruction input).
  No exception, no warning. This is the worst class because nothing signals it.
- **Throws.** A `Standard_Failure` (e.g. on a NULL result from a collapsed
  offset). Catchable, at least.
- **Hangs / does not terminate.** Documented on certain coincident-and-tangent
  configurations (the pave-filler entering pathological subdivision; the OCCT
  forums report cylinder intersections looping in `IntCyCyTrim` with ~0.003
  parameter increments). The build123d benchmark **reproduced this**:
  `occ_drill` at 1000 holes hit the 120 s hard timeout (`full_run.log:61`,
  status `timeout`) while manifold did the same job in 0.51 s. A timeout wrapper
  around every OCC Boolean is prudent — build123d has none.

BOPAlgo *does* emit diagnostics — the `BOPAlgo` namespace has dozens of alerts
(`BOPAlgo_AlertSelfInterferingShape`, `BOPAlgo_AlertIntersectionFailed`,
`BOPAlgo_AlertSolidBuilderFailed`, …) reachable via `HasErrors()`/`HasWarnings()`
on the operation object — but **build123d never inspects them**. `_bool_op`
checks neither `HasErrors()` nor `IsDone()`.

### 3.8 `ShapeFix` / `ShapeUpgrade` — and why build123d auto-runs `UnifySameDomain`

OCCT ships an entire toolkit, `TKShHealing`, devoted to **repairing shapes** —
including shapes its own Booleans produce. `ShapeFix_Shape` is the catch-all
repair pass (fix small edges, fix face boundaries, fix wire order, fix solid
orientation, re-establish tolerances); build123d wraps it as the free function
`fix` (`shape_core.py:3700`) and the method `Shape.fix()` (`:1368`, runs only when
`not is_valid`). **The mere prominence of this toolkit is itself the evidence**:
OCCT routinely emits shapes that need healing, and healing is heuristic, slow, and
not guaranteed to succeed.

`ShapeUpgrade_UnifySameDomain` is a *different* tool — not a repair, a
*simplification*. After a Boolean, the result is littered with **artificial seam
edges and split faces**: two originally-coplanar faces that the Boolean cut along
some intersection edge come back as two faces plus a redundant shared edge.
`UnifySameDomain` merges faces/edges that lie on *coincident* geometry back into
one. **build123d runs it unconditionally after every Boolean** —
`_bool_op` step 5 (`shape_core.py:2533-2540`):

```python
if SkipClean.clean:
    upgrader = ShapeUpgrade_UnifySameDomain(topo_result, True, True, True)
    upgrader.AllowInternalEdges(False)
    try:
        upgrader.Build()
        topo_result = downcast(upgrader.Shape())
    except Exception:
        warnings.warn("Boolean operation unable to clean", stacklevel=2)
```

The same call is the body of `Shape.clean()` (`shape_core.py:1166`). Why
unconditionally? Without it, a chain of N Booleans accumulates N layers of
junk seam edges, every later op gets slower (more sub-shapes to intersect), and
the face count balloons. The benchmark shows it *works*: `occ_union_grid` at 2000
boxes ends with just **10 faces** (`results.json`) — the cleaner collapsed
thousands of coplanar fragments. But it has three costs:

1. **It is itself a full traversal+rebuild of the accumulated solid**, run *every*
   op — for chained CSG it can rival the Boolean itself.
2. It only merges geometry that is **100% coincident** — near-coincident faces
   left by a fuzzy Boolean are *not* merged, so it does not rescue robustness.
3. The `try/except Exception` **silently degrades to a warning** — a "successful"
   Boolean can still leave an un-simplified, messy solid, and the caller is not
   told in any structured way.

`SkipClean` (`shape_core.py:3594`) is a *process-global* flag (a class attribute,
**not thread-local** — a concurrency hazard) that suppresses this pass; the
algebra-API operators use it to avoid cleaning every intermediate.

---

## 4. Performance

### 4.1 The pairwise-accumulation O(n²) problem

OCCT Booleans *can* take many tools in one call (`SetTools` accepts a list), but
build123d's accumulation patterns are **pairwise**. The `BuildPart` builder does
`self._obj.fuse(*shapes)` per `with`-block step (`build_common.py:465`); the
algebra API's `acc = acc + p` loop does one Boolean per operand. Each step:

1. re-intersects the **entire accumulated B-Rep** against the new operand —
   cost grows with the running sub-shape count;
2. then runs a full `UnifySameDomain` clean over the whole accumulated solid.

So N incremental unions cost roughly **O(N²)** — each of N Booleans pays a price
proportional to the size of the result so far. This is the classic
build123d/CadQuery performance cliff, and it is exactly the OpenSCAD-transpilation
workload (a deep CSG tree → hundreds of sequential Booleans).

### 4.2 Benchmark results — `ddocs/prototypes/p2_boolean_benchmark`

Real run, `results.json` / `full_run.log`, 2026-05-22. OCC = build123d `+`/`-`/`&`
operators; manifold = `manifold3d 3.4.1` native primitives + `batch_boolean`.
`wall` is the boolean time only. Per-run hard timeout 120 s.

**Workload 1 — union of N overlapping boxes (grid):**

| N | OCC `wall` | manifold `wall` | speedup | OCC result faces |
|---:|---:|---:|---:|---:|
| 10 | 0.071 s | 0.0026 s | 27× | 9 |
| 50 | 0.663 s | 0.0065 s | 102× | 9 |
| 200 | 1.950 s | 0.0195 s | 100× | 10 |
| 1000 | 21.32 s | 0.161 s | 132× | 6 |
| 2000 | 34.46 s | 0.257 s | 134× | 10 |

The OCC curve is super-linear (10→2000 boxes = 200× the work, 488× the time);
manifold stays near-linear. Note the OCC result is *correct and clean* (≈10
faces — `UnifySameDomain` did its job) — OCC is **slow, not wrong, here**.

**Workload 4 — one plate minus N drilled holes:**

| N | OCC `wall` | manifold `wall` | OCC status |
|---:|---:|---:|---:|
| 10 | 0.063 s | 0.0069 s | ok |
| 50 | 0.433 s | 0.030 s | ok |
| 200 | 4.078 s | 0.090 s | ok |
| 500 | 27.99 s | 0.260 s | ok |
| **1000** | **— (TIMEOUT >120 s)** | **0.513 s** | **timeout** |

This is the headline failure: a 1000-hole drill — a trivially simple, entirely
realistic part — **does not finish in 120 s** on OCC, while manifold completes in
half a second. Sequential `cut` accumulation collapses.

**Workload 2 — deep CSG tree (alternating cut/union):** OCC stays competitive at
shallow depth (depth 20: 1.01 s OCC vs. 0.036 s manifold) — depth, not breadth, so
the accumulated solid stays small; the O(n²) term hasn't bitten yet. Still ~28×.

**Workload 3 — coincident/degenerate cases:** every case completed "ok" and
`valid=True` on OCC (0.003–0.19 s). build123d's auto-`UnifySameDomain` plus
OCCT's exact-coplanar handling cope with *exactly* degenerate input;
sub-tolerance cases got lucky (§3.1). manifold did each in sub-millisecond.

### 4.3 Why deep CSG trees blow up

Three compounding factors: (a) the O(n²) pairwise accumulation of §4.1; (b) the
per-op `UnifySameDomain` clean, also over the growing solid; (c) every Boolean
re-does exact Face/Face intersection + p-curve reconstruction (§2.3) on inputs
that themselves have grown more numerous and more fragmented. A naively-transpiled
non-trivial OpenSCAD file *is* a deep CSG tree — transpiled to sequential
build123d Booleans it is slow and, on coincident faces, may also be invalid.

---

## 5. What OCCT does WELL — the things a mesh kernel cannot

This is the other half of the picture, and it is what makes the design a **hybrid**
rather than a replacement. manifold3d only ever has triangles; everything below is
out of reach for *any* mesh kernel.

- **Exact analytic / NURBS geometry.** A cylinder is a cylinder, a hole is a true
  cylindrical surface, a sphere is exact. `Shape.geom_type` (`shape_core.py:369`)
  is *meaningful* — downstream code can select "the cylindrical faces." A mesh
  has only planar facets; that distinction is gone forever once tessellated.
- **Fillet and chamfer.** `BRepFilletAPI_MakeFillet` produces a true rolling-ball
  blend surface (toroidal/NURBS), exact and analytic. build123d's
  `Mixin3D.fillet`/`chamfer` (`three_d.py`) are pure OCC. A mesh "fillet" is a
  faceted approximation with no analytic identity.
- **Lofts, sweeps, pipes.** `BRepOffsetAPI_ThruSections`, `MakePipe`,
  `MakePipeShell` — sweeping a profile along a spine with law-based scaling/twist,
  producing exact swept surfaces. build123d's `loft`/`sweep` (`operations_part.py`,
  `three_d.py`) are OCC-only. No mesh equivalent.
- **Offset / shell / draft.** `BRepOffsetAPI_MakeThickSolid` (hollowing to a wall
  thickness), `MakeOffsetShape`, `DraftAngle` — exact surface offsets.
- **STEP / IGES import-export.** `STEPCAFControl` round-trips exact B-Rep with
  assembly structure, names, colours, per-component placement
  (`importers.py` / `exporters3d.py`). STEP is *the* CAD interchange format and it
  is a B-Rep format — a mesh exported as STEP is a useless faceted blob.
- **Exact metrology.** `BRepGProp` gives exact volume, surface area, moments of
  inertia, centre of mass. On a mesh these are approximations of the facets.
- **Analytic primitives and exact transforms** — `BRepPrimAPI_Make*`, exact
  rigid/affine transforms via `gp_Trsf`/`gp_GTrsf`.

So the design rule is: **OCC is not the problem to be removed — it is the kernel
to be kept for everything except bulk CSG.** manifold3d's job is narrow.

---

## 6. Design implications

### 6.1 What routes to manifold3d vs. stays on OCC

| Operation | Route | Why |
|---|---|---|
| Many-operand union / `batch_boolean` | **manifold3d** | the O(n²) cliff (§4.1); manifold is 100–130× faster and near-linear |
| Deep CSG trees / transpiled OpenSCAD | **manifold3d** | same; plus manifold's *guaranteed-manifold* invariant kills the silent-corruption risk (§3.7) |
| Booleans on dense / imported meshes | **manifold3d** | OCC must first reconstruct an (invalid) B-Rep by per-triangle sewing (`02-build123d-mesh-paths.md` §2.2); manifold operates on triangles directly |
| Booleans near coincident/tangent faces | **manifold3d** | manifold is robust by construction; OCC needs hand-tuned fuzzy values (§3.5) |
| Minkowski sum, 3D convex hull, SDF/level-set | **manifold3d** | OCCT has *no implementation at all* of these |
| Fillet / chamfer | **OCC** | exact blend surfaces; mesh cannot represent them |
| Loft / sweep / pipe / offset / shell / draft | **OCC** | exact swept/offset surfaces |
| STEP / IGES export, exact metrology | **OCC** | B-Rep-native; mesh degrades these |
| One-shot Boolean of a few exact primitives | **OCC** (either works) | already sub-10 ms on OCC (§4.2); conversion overhead would not pay off |

The boundary is clean: **manifold for bulk/chained/mesh-origin CSG and the
ops OCC simply lacks; OCC for exact-surface modelling, healing-sensitive ops, and
data exchange.** A mesh-origin solid can be re-fitted to analytic surfaces
afterward via `brep_from_stl.detect_primitives` (`02-build123d-mesh-paths.md` §5)
when an exact result is needed downstream.

### 6.2 The tolerance / units bridge between the two kernels

Both kernels are double-precision *numeric* (not exact-arithmetic), but their
epsilon conventions differ and must be reconciled at the conversion boundary:

- **Units.** build123d/OCC work in **millimetres**; manifold3d is purely numeric
  (unitless). The bridge is a pass-through — feed mm coordinates straight into
  `Manifold`, read mm back. OpenSCAD is also unitless, so a scad2py path is
  consistent. No scaling, but the policy must be *documented* so nobody inserts a
  spurious factor.
- **Vertex weld tolerance.** `Shape.tessellate()` (`shape_core.py:2241`) emits a
  **non-indexed soup** — duplicate vertices at every face seam (no cross-face
  dedup). manifold3d *requires* a properly indexed, watertight mesh and rejects
  soup as `NotManifold`. The bridge must weld vertices first. Use a single weld
  tolerance and apply it consistently: grid-snap at build123d's `TOLERANCE = 1e-6`
  (`geometry.py:91`) — exactly the scheme `Mesher._create_3mf_mesh` already uses
  (`02-build123d-mesh-paths.md` §2.1). The `p2_boolean_benchmark` bridge does
  precisely this (`bridge.py`, README "Vertex weld" note).
- **Tessellation deflection.** `BRepMesh_IncrementalMesh` runs with
  `isRelative=True` in build123d's defaults — deflection scales per-edge, not
  absolute. A mesh-CSG path that wants uniform absolute resolution must pass
  `isRelative=False` itself (`02-build123d-mesh-paths.md` §1.1). This deflection
  is the *only* lossy step in the round trip — it sets how faithfully a curved OCC
  surface becomes triangles, and is the manifold-backend analogue of OpenSCAD's
  `$fn`.
- **Round-trip asymmetry.** OCC → mesh (`tessellate`) is robust and fast; mesh →
  OCC `Solid` (per-triangle sew) is the genuine wall — minutes-slow and
  topology-bloated (`02-build123d-mesh-paths.md` §8). So the bridge should keep
  `Shape` and `Manifold` as **parallel representations** and convert only at the
  boundary — never round-trip mid-pipeline. Treat mesh → B-Rep as a deliberate,
  explicit, costly "bake" step, not an implicit conversion.

### 6.3 Where it plugs in

There is **no kernel abstraction** in build123d — OCP is assumed in every module,
`_bool_op` literally constructs `BRepAlgoAPI_Cut()`
(`01-build123d-architecture.md` §7.1). A manifold backend therefore cannot be a
drop-in `.wrapped` replacement; it must be additive. The lowest-risk insertion is
standalone `mesh_fuse`/`mesh_cut`/`mesh_intersect` helpers
(`01-build123d-architecture.md` §7.4 option 1); the one clean seam for producing
correctly-typed `Part`/`Sketch`/`Curve` results without import cycles is the
`composite_factories` registry (`shape_core.py:196,936`). Any new mesh-mode flag
should be **thread-local**, unlike the existing process-global `SkipClean` hazard.

---

## 7. Open questions

1. **Should `_bool_op` gate on `IsDone()` / `HasErrors()` before trusting a
   result?** Today it inspects neither (§3.7) — invalid and empty results
   propagate silently. Adding the check is cheap and would surface a whole class
   of silent corruption; it is orthogonal to the manifold work.
2. **Auto-fallback or explicit mode?** Should a manifold path trigger
   automatically when an OCC Boolean times out / `IsDone()==False`, or only on an
   explicit user-selected mesh mode? Auto-fallback changes result *type*
   (analytic → faceted) silently — risky.
3. **Timeout wrapper.** The 1000-hole drill hang (§3.7, §4.2) argues for a
   timeout around every OCC Boolean regardless of the manifold work. Where should
   it live, and what is a safe default?
4. **Fuzzy value for `cut`/`intersect`/`split`.** Only `fuse` exposes `tol`
   today. Should the others gain it, or is that a dead end given fuzzy values are
   un-automatable magic numbers (§3.5)?
5. **`UnifySameDomain` cost share.** No measurement isolates the per-op
   `UnifySameDomain` clean from the Boolean itself. If the clean is a large
   fraction of the O(n²) chained-CSG cost, batching Booleans (one `SetTools` call
   instead of N) plus a single final clean could help even *without* manifold.
6. **`BRepCheck_Analyzer` blind spots.** It passed a self-intersecting torus
   (§3.3) — any validity gate built on it inherits that blind spot. Is manifold's
   guaranteed-manifold invariant a strong enough substitute for the mesh path?

---

## Sources

OCCT documentation:
- [Boolean Operations — OCCT 7.9.0 specification](https://dev.opencascade.org/doc/occt-7.9.0/overview/html/specification__boolean_operations.html)
- [BOPAlgo_PaveFiller class reference](https://dev.opencascade.org/doc/occt-7.5.0/refman/html/class_b_o_p_algo___pave_filler.html)
- [BOPAlgo_BOP class reference](https://dev.opencascade.org/doc/occt-7.4.0/refman/html/class_b_o_p_algo___b_o_p.html)
- [BOPAlgo_Builder class reference](https://dev.opencascade.org/doc/occt-7.6.0/refman/html/class_b_o_p_algo___builder.html)
- [BOPAlgo_Options (fuzzy / glue / safe-input options)](https://dev.opencascade.org/doc/refman/html/class_b_o_p_algo___options.html)
- [BRepAlgoAPI_BooleanOperation class reference](https://dev.opencascade.org/doc/refman/html/class_b_rep_algo_a_p_i___boolean_operation.html)
- [Shape Healing — OCCT user guide](https://dev.opencascade.org/doc/overview/html/occt_user_guides__shape_healing.html)
- [ShapeUpgrade_UnifySameDomain class reference](https://dev.opencascade.org/doc/refman/html/class_shape_upgrade___unify_same_domain.html)
- [Fuzzy Boolean Operations — OCCT forum](https://dev.opencascade.org/content/fuzzy-boolean-operations)
- [Boolean operations: in search for a robust process — OCCT forum](https://dev.opencascade.org/content/boolean-operations-search-robust-process)
- [Why Boolean Operations are so slow — OCCT notes](https://opencascade.blogspot.com/2008/12/why-are-boolean-operations-so-sloooooow.html)
- [Performance of boolean operations — OCCT forum](https://dev.opencascade.org/content/performance-boolean-operations)

build123d source (`/Users/ochafik/github/build123d/src/build123d/`):
`pyproject.toml:36`; `geometry.py:91`; `topology/shape_core.py:72-138, 369, 472,
1156-1174, 1219-1231, 1368, 1380-1405, 1455, 2003-2057, 2459-2560, 3594, 3700`;
`topology/three_d.py:445-523`; `build_common.py:465`.

build123d benchmark: `ddocs/prototypes/p2_boolean_benchmark/`
(`README.md`, `workloads.py`, `results.json`, `full_run.log`).

Related research: `cadquery/ddocs/explorations/02-occ-opencascade-kernel.md`;
`build123d/ddocs/research/01-build123d-architecture.md`,
`02-build123d-mesh-paths.md`.
