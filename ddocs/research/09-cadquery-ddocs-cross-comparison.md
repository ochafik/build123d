# 09 — CadQuery ddocs ↔ build123d ddocs Cross-Comparison

> Cross-comparison doc. A **parallel research effort** analyzed the same two goals
> for **CadQuery** that this corpus analyzed for **build123d**. This document
> ingests the CadQuery corpus (`/Users/ochafik/github/cadquery/ddocs/`), maps it
> onto the build123d corpus (`/Users/ochafik/github/build123d/ddocs/`), and
> records convergences, divergences, and gaps in each.
>
> Author: research agent · Date: 2026-05-22 · Knowledge cutoff: Jan 2026.

---

## 1. Overview — two parallel efforts, why they are comparable

Both efforts evaluate the **same two goals**:

- **GOAL 1** — bring `manifold3d`'s fast/robust mesh-CSG into a Python BREP CAD
  library (CadQuery / build123d), so the library gains cheap booleans, 3D hull,
  Minkowski, level-set, etc.
- **GOAL 2** — make ochafik's `scad2py` OpenSCAD transpiler target that library
  as a geometry backend.

**Why the two analyses transfer.** CadQuery and build123d are *sibling* libraries:
both are Apache-2.0 Python CAD frameworks, both wrap **the identical OpenCASCADE
kernel via the identical OCP pybind11 bindings** (`cadquery-ocp` / `cadquery-ocp-novtk`,
both OCCT 7.9.3). build123d is explicitly "the spiritual successor to CadQuery"
(b3d `07-prior-art-ecosystem` §1; cq `07` has no analogue but cq `01` confirms the
shared kernel). The CadQuery `02-occ-opencascade-kernel` doc states the kernel
relationship directly: *"OCCT is the kernel under FreeCAD … and — via the OCP
bindings — CadQuery and build123d."*

**What is therefore transferable vs library-specific:**

| Layer | Transferable between cq ↔ b3d? |
|---|---|
| OCCT kernel behaviour (boolean fragility, fillet failures, no minkowski/hull, tolerance model) | **Fully** — same kernel, same version |
| manifold3d capabilities/limits (API, robustness, minkowski, properties, run-IDs) | **Fully** — same library, both pinned at 3.4.1 |
| BREP↔mesh interconversion physics (tessellate weld, per-triangle sewing super-linearity) | **Fully** — same OCCT mesher, same sewing API |
| License analysis (OCCT LGPL+exception, manifold3d Apache, scad2py GPL files) | **Fully** — identical components |
| scad2py architecture (parser, AST, csg.Node IR, ManifoldRenderer seam) | **Fully** — same upstream `scad2py` repo analyzed |
| The *insertion seam* in the host library (`Shape` hierarchy, `_bool_op`, `downcast`) | **Mostly** — both have a single `Shape.wrapped: TopoDS_Shape` boundary; class names and method names differ |
| Selector API surface | **Partly** — CadQuery's string-DSL selectors vs build123d's `ShapeList`/`filter_by`; the *concept* (topological/geometric selectors) transfers, the *details* do not |

The two corpora are remarkably congruent in conclusions — which, given they were
produced independently, is strong corroboration (§3).

---

## 2. Topic-by-topic comparison table

| Topic | CadQuery doc/prototype | build123d doc/prototype | Notes |
|---|---|---|---|
| Host-library architecture / kernel seam | `explorations/01-cadquery-architecture` | `research/01-build123d-architecture` | Direct counterparts. Both find one `Shape.wrapped: TopoDS_Shape` boundary; both enumerate `_bool_op` insertion options. |
| OCCT kernel deep-dive (what's missing/slow/fragile) | `explorations/02-occ-opencascade-kernel` | **NO dedicated counterpart** | b3d folds OCCT pain into `01` §6 and `05` §3 — much thinner. **Gap in b3d corpus — see §5.** |
| manifold3d deep-dive | `explorations/03-manifold3d-deep-dive` | `research/03-manifold3d-deep-dive` | Direct counterparts. cq introspected 3.4.1 directly; b3d introspected **2.3.1** and *predicted* 3.x — see §4 divergence. |
| scad2py architecture | `explorations/04-scad2py-architecture` | `research/04-scad2py-architecture` | Direct counterparts; near-identical findings. |
| OpenSCAD↔host semantics / CSG-BREP gap | `explorations/05-openscad-semantics-csg-gap` | `research/05-openscad-vs-build123d-model` | Direct counterparts; both build a construct-by-construct mapping table. |
| BREP↔mesh interconversion | `explorations/06-brep-mesh-interconversion` | `research/02-build123d-mesh-paths` | Direct counterparts. cq `06` is empirical (measured numbers); b3d `02` is source-survey + the `p1` prototype carries the numbers. |
| Prior-art / ecosystem survey | **NO counterpart** | `research/07-prior-art-ecosystem` | b3d surveyed issue #1228, FreeCAD `importCSG.py`, PythonOpenScad, etc. **Gap in cq corpus — see §6.** |
| Caching infrastructure | **NO counterpart** | `research/08-caching-infrastructure` | b3d-only. **Gap in cq corpus — see §6.** |
| License analysis | `licenses/10-license-analysis` | `research/06-license-analysis` | Direct counterparts; near-identical verdicts. |
| GPL remediation (clean-room rewrite plan) | `design/22-scad2py-gpl-remediation` + `prototypes/p6-clean-room-math` | **NO dedicated doc** (b3d `06` §6.4 scopes it but does not execute) | cq went further: actually clean-room-rewrote `calc.py`/`colors.py` and proved 3308-case equivalence. **Gap in b3d corpus — see §5.** |
| BREP↔mesh probe prototype | `prototypes/p0-brep-mesh-probe` | `prototypes/p1_brep_mesh_roundtrip` | Direct counterparts. |
| MeshShape value type prototype | `prototypes/p1-meshshape` | `prototypes/p3_meshsolid` | Direct counterparts (`MeshShape` ≈ `MeshSolid`). |
| Boolean benchmark prototype | folded into `p1-meshshape` demo (a) | `prototypes/p2_boolean_benchmark` | b3d has a *dedicated* benchmark prototype; cq's numbers are in `p1`'s demo + doc `02` §6.0. |
| Fast mesh→BREP prototype | `prototypes/p2-fast-mesh2brep` | **NO counterpart** | cq prototyped a direct-shell-assembly (no-sewing) reconstructor. **Gap in b3d corpus — see §5.** |
| scad2py→host renderer prototype | `prototypes/p3-scad2py-cq-renderer` | `prototypes/p4_scad2py_to_b3d` | Direct counterparts (`CadQueryRenderer` ≈ `build123d_renderer`). Neither has a writeup `.md`; both are runnable code. |
| scad2py→host *source codegen* prototype | **NO counterpart** | `prototypes/p5_openscad_to_b3d_source` | b3d prototyped emitting build123d *source* (`scad2b3d.py`), the "Option B" of b3d `04`. **Gap in cq corpus — see §6.** |
| Face/selector identity through mesh booleans | `prototypes/p4-face-identity` | **NO counterpart** | cq-only, and high-value for build123d's selector-heavy API. **Gap in b3d corpus — see §5.** |
| WASM / pyodide feasibility | `prototypes/p5-wasm-pyodide` | **NO counterpart** | cq-only. **Gap in b3d corpus — see §5.** |
| Clean-room math rewrite | `prototypes/p6-clean-room-math` | **NO counterpart** | cq-only (see GPL-remediation row). **Gap in b3d corpus — see §5.** |

**Numbering note.** The CadQuery corpus uses a sparse numbering (`01–06`, `10`,
`20–22` design docs, `p0–p6`) implying design docs `20`/`21` exist but were not in
scope here. The build123d corpus is `01–08` + `p1–p5`. Neither has a doc the
other's numbering implies is missing beyond what is listed above.

---

## 3. Convergences — findings both efforts reached independently

These are the load-bearing conclusions. Both teams reached them *separately*, on
two different libraries; the agreement is independent corroboration and should be
treated as high-confidence.

### 3.1 GPL contamination is *exactly* `calc.py` + `colors.py` — nothing else

Both license analyses, scanning the *same* `scad2py` repo independently, found the
contamination is contained in precisely two files and that the rest is clean:

- cq `licenses/10` §1.5 + cq `design/22` §2: *"contamination is exactly two files
  … a repo-wide scan of all 52 `.py` files found no other GPL headers."*
- b3d `research/06` §6.4: *"Two files carry explicit third-party copyright + GPL
  headers: `calc.py`, `colors.py`."*

Both independently agree that (a) translating GPL C++ to Python is a derivative
work, so the files are GPL-2.0+; (b) the parser is a *clean* PLY reimplementation
and reimplementing the OpenSCAD *language/grammar* is not infringement (both cite
*Google v. Oracle* 2021); (c) the files are on the geometry hot path (imported by
`csg.py`, `io.py`, `runtime/modules.py`, the renderers); (d) the fix is a
clean-room rewrite from public specs (OpenSCAD User Manual formula + W3C CSS
colors). This is a textbook independent-corroboration result.

### 3.2 OCCT booleans are slow *and* fragile; manifold3d is ~2 orders of magnitude faster

Both measured large speedups on hard CSG workloads:

| Source | Workload | OCCT | manifold3d | Speedup |
|---|---|---|---|---|
| cq `02` §6.0 | 60 chained cylinder unions | 83,533 ms | 4 ms | ~20,000× |
| cq `03` §9 | sphere − 49 cylinders | 5.53 s | 0.041 s | ~130× |
| cq `p1-meshshape` (a) | sphere − 60 cylinders | 15.8 s | 0.058 s | ~274× |
| b3d `p3_meshsolid` demo §2 | 72 spheres fused+subtracted | 2,499 ms | 35.1 ms | ~71× |
| b3d `01` §6.4 / `03` §5 | qualitative ("~1000× faster than CGAL") | — | — | — |

The *magnitude* varies with workload (both note this honestly — cq `p1` and `02`
§Risks both caveat "workload-specific; benign one-shot booleans are sub-10 ms").
But both efforts independently land the same verdict: **the win is real, largest
on chained/dense CSG, and is the core GOAL-1 motivation.** Both also independently
found OCCT booleans *silently produce invalid solids* — cq `p1` demo (d) and
`occt_invalid_case.py` (fails `BRepCheck` at cut #36, no exception raised); b3d
`05` §3.3 and `01` §6.4 ("invalid results propagate silently"). Both cite the
*same* CadQuery discussion #1686 where CadQuery's own maintainer says "use a CSG
kernel" (b3d `07` §2; cq corpus references it implicitly via the shared kernel
analysis).

### 3.3 BREP→mesh is easy; mesh→BREP is the wall

Both efforts independently characterized the asymmetry identically:

- **BREP→mesh:** robust, fast, deflection-controlled — but `tessellate()` emits a
  *per-face triangle soup* with duplicate seam vertices that manifold3d rejects as
  `NotManifold`; **you must weld first.** cq `06` §1.3 (numpy `unique` on rounded
  coords); b3d `02` §1.2/§8.1 (grid-snap to `1e-6`, same scheme as
  `Mesher._create_3mf_mesh`); b3d `p3` §5 (uses manifold3d 3.x `Mesh.merge()`).
- **mesh→BREP:** the only faithful path is **per-triangle planar-face sewing**,
  which is **super-linear** and **destroys exact geometry**. cq `06` §2.2 measured
  143k triangles → **148 s**; b3d `02` §6.7 reaches the same "minutes for tens of
  thousands of triangles" conclusion. Both found the sewn result is *valid*
  (`BRepCheck` True) but has *one planar face per triangle* — fillet/chamfer and
  type selectors do not survive.

Both conclude with the *same strategy*: **stay in mesh as long as possible,
convert back at most once at an explicit export/feature boundary.** cq `06` §3.1
("convert BRep→mesh once, do all CSG in manifold, convert back at most once");
b3d `02` §9.2-9.3 and `p3` §4 ("mesh→BREP is always explicit and never implicit").

### 3.4 The same insertion-seam analysis and the same recommendation

Both found OCP is a thin, unabstracted binding with **no kernel-abstraction layer**
— any manifold path must live in the library's own `occ_impl`/`topology` code
(cq `01` §1 / `02` §2.2; b3d `01` §7.1). Both found the structural blocker is the
same: `Shape.__init__` unconditionally calls `downcast(obj)` and `downcast`/
`shapetype` are hardwired to `TopoDS`, so a non-`TopoDS` `.wrapped` cannot be a
`Shape` subclass (cq `01` §9.2 Option 1 "Cons"; b3d `01` §7.4 + `p3` §3.1 "the
single biggest integration friction").

Both independently recommend the *same layered design*: a **standalone mesh value
type** (`MeshShape` / `MeshSolid`) that is **not** a `Shape` subclass, exposed
through **free functions** (`mesh_fuse/cut/intersect` or a `cadquery.manifold`
namespace) with **explicit, expensive, opt-in** mesh→BREP conversion, shipped as
an **opt-in extra** — never core, never an implicit kernel swap. cq `p1-meshshape`
§"What it means for design doc 20"; b3d `p3_meshsolid` §6 ("Ship `build123d[manifold]`
as an opt-in extra … do NOT make `MeshSolid` a `Shape` subclass"). b3d additionally
anchors this to a real upstream signal — issue **#1228** explicitly asks for a
`build123d[manifold]` optional extra (b3d `07` §1.1).

### 3.5 hull (3D) and minkowski are impossible in pure BREP — GOAL 2 is gated on GOAL 1

Both OpenSCAD-semantics docs independently conclude that `hull()` (3D) and
`minkowski()` have **no BREP formulation** — OCCT has neither operator — so a
faithful `scad2py → CadQuery/build123d` backend is *only* possible if the library
first gains the manifold3d mesh path. cq `05` is blunt: *"Goal 2's success is
gated on Goal 1."* b3d `05` §5 reaches the identical conclusion: *"a faithful
transpiler must also have a manifold3d mesh backend as a sibling."* Both also note
manifold3d's *native* `minkowski_sum`/`minkowski_difference` (cheap only for convex
operands) and that scad2py's own `minkowski_impl.py` convex-decomposition path may
still be needed for non-convex inputs.

### 3.6 The scad2py seam is the `csg.Node` tree; a sibling renderer is the integration

Both `04` docs independently identify the *same* clean insertion point: scad2py's
parser/AST/transpiler/`csg.Node` IR are 100% geometry-agnostic and reusable
verbatim; only the **Stage-2 `ManifoldRenderer` visitor** is backend-specific. A
host-library backend is a *sibling visitor* over the same `csg.Node` tree. cq `04`
§5 + `05` B.10 ("`CadQueryRenderer(csg.DefaultVisitor)`"); b3d `04` §"Option A"
("`Build123dRenderer(csg.DefaultVisitor)` parallel to `ManifoldRenderer`"). Both
prototypes (`p3-scad2py-cq-renderer`, `p4_scad2py_to_b3d`) implement exactly that.

### 3.7 The `$fn` faceting mismatch needs a heuristic-with-override

Both `05` docs independently arrive at the same policy: `$fn`/`$fa`/`$fs` are part
of OpenSCAD model *identity* (a low-`$fn` cylinder *is* a prism); a transpiler must
decide per-primitive exact-vs-faceted; the answer is a **heuristic threshold with
an override**, defaulting to "high `$fn` → exact, low `$fn` → faceted polygon."
cq `05` §B.6 / recommendation 4; b3d `05` §6 policy (B), `--resolution` flag. The
cq `p3` renderer hard-codes `FACET_FN_THRESHOLD = 32`; b3d `05` suggests ≥24 — a
minor, non-contradictory tuning difference.

### 3.8 License verdict: manifold3d is clean, OCCT LGPL+exception is fine, the combo is Apache-shippable

Both license docs independently conclude: manifold3d is Apache-2.0 with only
permissive bundled deps (Clipper2 BSL-1.0, TBB Apache, nanobind BSD, glm MIT) —
**no copyleft anywhere in the manifold stack**; OCCT is LGPL-2.1 + Open CASCADE
Exception 1.0, satisfied by dynamic linking + notice; the combined Apache-2.0
product is shippable once the two scad2py GPL files are cleaned. Both flag the
*same* secondary item: the OCP wheel ships OCCT binaries **without** the LGPL/
exception license texts (cq `10` §1.3; b3d `06` §2 note + §3.3) — a curable
inherited compliance gap.

---

## 4. Divergences & contradictions

The two corpora largely agree; the differences are mostly *coverage depth*, not
factual contradiction. The genuine disagreements:

### 4.1 manifold3d version introspected — a real factual divergence (resolved)

- **cq `03`** introspected **manifold3d 3.4.1** directly and reports the 3.x API
  as fact: native `minkowski_sum`/`minkowski_difference`, `batch_boolean`+`OpType`,
  `level_set`, double-precision kernel.
- **b3d `03`** introspected **manifold3d 2.3.1** and marks all 3.x features as
  `[upstream]` *predictions* — including "possibly `minkowski_sum`/`minkowski_difference`"
  and a flagged **intermittent segfault** (b3d `03` §3.5) in the 2.3.1 build.

**Assessment: not a contradiction — a corpus-staleness artifact.** cq `03` is the
more current and authoritative read. The build123d *prototypes* resolve it: both
b3d `p2` README and `p3` NOTES explicitly state the installed wheel is **3.4.1**
("the upstream 3.x API the doc *predicted* is what is installed"). So b3d's own
later work confirms cq `03`. **Action for b3d:** treat b3d `03`'s `[upstream]`
caveats as resolved-confirmed, and re-check whether the 2.3.1 segfault (§3.5)
reproduces on 3.4.1 — cq `03` introspected 3.4.1 and reported *no* such instability.

### 4.2 Native minkowski on non-convex inputs — cq is more definite

cq `03` §2.8 *verified* `minkowski_sum`/`minkowski_difference` work in 3.4.1 and
quotes the docstring warning (convex-only for good performance; O(faces²) for
non-convex). b3d `03` §6.3 only says 3.x "reportedly adds" them and "verify before
relying." **cq's verified finding supersedes** — but both agree the non-convex
case may still need scad2py's `minkowski_impl.py` convex decomposition, and both
flag it as an open question.

### 4.3 Boolean-benchmark speedup magnitude — not a contradiction, workload variance

The headline speedups differ widely (cq: 130×–20,000×; b3d `p3`: 71×). This is
**not a contradiction** — they are different workloads (chained-cylinder fuse vs
72 sphere ops) on different machines, and *both corpora explicitly caveat that the
speedup is workload-specific* and smallest for benign one-shot booleans (cq `02`
§Risks; cq `p1` final bullet; b3d `01` §6.4). The agreement on the *qualitative*
claim — "1–2+ orders of magnitude on hard/chained CSG" — is solid. No resolution
needed; just do not quote a single multiplier as if universal.

### 4.4 Mesh→BREP — cq prototyped a *faster* path; b3d only measured the slow one

This is a divergence in **how far each effort got**, not a factual disagreement.
cq `06` §"Open questions" floated a faster direct-shell-assembly reconstructor and
**cq `p2-fast-mesh2brep` actually built it** (direct `BRep_Builder` shell assembly
reusing the already-known welded mesh connectivity — no `BRepBuilderAPI_Sewing`
spatial search). b3d `02` §6 only inventories the slow per-triangle-sewing path
and `p3` measures it (167 ms for 1262 tris, "~10 s+ for 100k"). **Not a
contradiction** — b3d simply has not prototyped the optimization cq did. See §5.

### 4.5 Selector identity — cq has a verdict, b3d has only an open question

b3d `01` open-question #7 and `03` §8.2 *raise* the question "can `face_id` +
coplanar grouping reconstruct face selectors after a boolean?" and leave it
unresolved. cq `p4-face-identity` **answers it empirically**: provenance
(`run_original_id`) and planar-face region-grouping survive booleans and even add
a new `from:<solid>` selector; analytic curved-face selectors (`%CYLINDER`,
`geomType()=="PLANE"`) and fillet-after-boolean do **not**. **Not a contradiction
— cq closed a question b3d left open.** See §5.

### 4.6 No genuine factual contradictions found

On every shared technical claim — OCCT fragility, the weld requirement, sewing
super-linearity, the GPL-file scoping, the license verdict, the `csg.Node` seam,
the `downcast`/`TopoDS` blocker, the opt-in-extra recommendation — the two corpora
**agree**. The only divergences are (a) the manifold3d version artifact (§4.1,
resolved in b3d's favour by its own prototypes) and (b) coverage depth (§4.4,
§4.5, and §5/§6 generally).

---

## 5. Gaps in the build123d corpus that CadQuery's covers

The CadQuery corpus contains four substantial pieces of work with **no build123d
counterpart**. All four transfer well because the underlying kernel/library/repo
is shared.

### 5.1 A dedicated OCCT-kernel deep-dive — cq `02-occ-opencascade-kernel`

**What cq has:** a full standalone doc on *what OCCT is, what is missing, slow,
and fragile*, with reproduced failure cases: sub-tolerance gaps silently merge two
boxes into one solid (≤~1e-7); sliver intersections vanish at the tolerance cliff;
fillet at r = half-edge-length hard-fails and r > feature-size **silently returns
an invalid shape**; self-intersecting `MakeTorus(R=2,r=5)` is accepted as valid by
`BRepCheck`; offsets silently return NULL on collapse. Plus the OCP packaging
facts (148 MB `.so`, 77 bundled OCCT dylibs ~78 MB, 320 submodules, `OCP.Voxel`
empty), the tolerance model (`Precision::Confusion()` = 1e-7), and the general-fuse
/ fuzzy-value / glue knobs.

**What b3d has:** this is *scattered and much thinner* — b3d `01` §6.4 ("Where
booleans are slow / fragile" — 6 bullets), b3d `05` §3.3 (one paragraph on the
robustness gap). There is no enumeration of *failure modes*, no reproduced
tolerance-cliff cases, no OCP-packaging inventory.

**How much transfers:** **~100%.** It is the *identical* OCCT 7.9.3 via the
*identical* OCP. Every failure case cq reproduced will reproduce in build123d.
**Recommendation:** b3d should adopt cq `02` largely as-is — it is the missing
"why GOAL 1 is needed" foundation doc. The only build123d-specific edit: b3d uses
`cadquery-ocp-novtk` (VTK stripped), so the OCP-inventory section drops the `IVtk*`
/ `vtk` rows.

### 5.2 A fast mesh→BREP prototype — cq `p2-fast-mesh2brep`

**What cq has:** a prototype testing two mesh→BREP reconstruction approaches —
(A) the baseline per-triangle `BRepBuilderAPI_Sewing` (the slow path b3d measured),
and (B) a **direct shell assembly** that exploits the fact that the welded mesh
*already encodes connectivity*: build each `TopoDS_Vertex` once, each `TopoDS_Edge`
once and share it between the two triangles using it (keyed by sorted vertex-index
pair), build one `TopoDS_Face` per triangle, `BRep_Builder.Add` to one `TopoDS_Shell`,
close to a `TopoDS_Solid` — **no spatial search at all**, targeting near-linear
scaling. The prototype includes phased timing, a `--no-fix` variant, degenerate-
triangle filtering, and a dedicated 10⁶-triangle scaling probe.

**What b3d has:** nothing. b3d `02` §6 and `p3` only ever use the slow sewing path;
b3d `02` §10 open-question #1 *asks* whether sewing scales but never tests an
alternative. b3d `p3` §6 recommendation #6 says "reuse `Mesher._get_shape`'s sew
logic" — i.e. it bakes in the slow path.

**How much transfers:** **~100%.** Direct `BRep_Builder` shell assembly is pure
OCP/OCCT, identical in both libraries. This is the single most actionable cq gap:
mesh→BREP super-linearity is *the* named blocker in **both** corpora (cq `06`
§Risks "HIGH"; b3d `02` §8.3), and cq has a prototyped mitigation b3d does not.
**Recommendation:** b3d should port cq `p2`'s direct-assembly reconstructor and
benchmark it; if it scales near-linearly, the b3d `p3` `MeshSolid.to_solid()` /
the proposed `Solid.from_mesh()` should be built on it, not on `Mesher._get_shape`.

### 5.3 A face/selector-identity prototype — cq `p4-face-identity`

**What cq has:** an empirical study of whether topological selectors survive a
manifold boolean — *the* named top integration risk, since both CadQuery and
build123d are selector-driven. Findings: `run_original_id` provenance is **solid**
and survives chained booleans; boolean-*created* cut faces inherit the tool
solid's id (nothing orphaned); `run_index` is in **halfedge units** (÷3 for
triangle offsets); `face_id` is unique only *within a run* so the real key is
`(run_original_id, connectivity-cluster)`; region-growing by connectivity + crease
angle rebuilds CadQuery-style planar faces with correct count (a hole-punched
plate → exactly 10 face-groups); planar directional selectors (`>Z`, `|X`, `#XY`)
and a new `from:<solid>` provenance selector work; **analytic curved-face
selectors (`%CYLINDER`, `geomType`) and fillet-after-boolean are infeasible** and
the prototype rejects them rather than mis-answer. Also: never call `as_original()`
on an intermediate (it flattens the run table); use `reserve_ids` for run-stable
tags.

**What b3d has:** only open questions — b3d `01` #7, `03` §8.2, §9. build123d's API
is **even more selector-centric** than CadQuery's (`ShapeList`, `filter_by`,
`>`/`<`/`>>`/`|` operators, `GeomType` filters — b3d `01` §2.4), so this gap is
*more* consequential for build123d than for CadQuery.

**How much transfers:** **~95%.** The manifold3d identity mechanisms
(`as_original`, `run_index`, `run_original_id`, `face_id`, `set_properties`,
`reserve_ids`) are library-agnostic — pure manifold3d 3.4.1. The only b3d-specific
translation: mapping reconstructed `FaceGroup`s onto build123d's `ShapeList`/
`filter_by` rather than CadQuery's `">Z"` string DSL — a thin adapter, the
underlying region-grow result is the same. **Recommendation:** b3d should
replicate cq `p4` against build123d's selector API; it directly answers b3d `01`
open-question #7 and tells the `p3 MeshSolid` design how much selector fidelity it
can promise (answer: planar + provenance yes, analytic-curved no).

### 5.4 A WASM/pyodide packaging analysis — cq `p5-wasm-pyodide`

**What cq has:** a deployment/distribution analysis. Key findings: "CadQuery in
pyodide" ≠ "OCP in pyodide" (CadQuery/build123d are pure Python; OCP is the 148 MB
native blocker); **`OCP.wasm` (yeicor) already exists** — a working Emscripten port
of OCCT 7.9.3 + OCP, **built specifically so build123d runs in the browser**, ~22 MB
compressed (`cadquery-ocp-novtk` WASM wheel on PyPI); manifold3d→WASM is trivial
(~0.5–1 MB, already powers web OpenSCAD); the recommendation is a **pure-manifold
WASM build** as the light browser story and full-OCP-WASM as an optional heavy
add-on; and the GPL `calc.py`/`colors.py` files are a **hard blocker for any
distributed WASM bundle** because WASM static linking removes every
"separate-process" argument.

**What b3d has:** only a passing mention — b3d `07` §7.2 notes `ochafik/manifold@
pyodide-build` and §1 mentions `OCP.wasm` exists, but there is no analysis.

**How much transfers:** **~100%, and it is arguably *more* relevant to build123d
than to CadQuery** — cq `p5` itself states `OCP.wasm` was built *for build123d*.
The whole analysis (OCP.wasm wheel, payload sizes, the LGPL static-link question,
the GPL-blocks-WASM finding) applies to build123d unchanged. **Recommendation:**
b3d should adopt cq `p5` essentially verbatim — it is the browser-deployment story
for the very library `OCP.wasm` targets.

### 5.5 An *executed* GPL remediation — cq `design/22` + `p6-clean-room-math`

**What cq has:** cq did not just *identify* the GPL problem (both corpora did that)
— it **executed the fix**. cq `design/22` is a full audit confirming the
contamination is exactly two files, and cq `p6-clean-room-math` is a **clean-room
rewrite** of `calc.py`/`colors.py` from public specs (OpenSCAD User Manual + W3C
CSS Color 4), with a `test_equivalence.py` proving **3308 comparison cases**
bit-equivalent to the originals (FP noise ≤ 1.4e-17). It includes the call-site
rename checklist and the git-history note.

**What b3d has:** b3d `06` §6.4 *scopes* the fix ("re-implement both files from
the specification") and §9.2 item 2 makes it a gating action item — but does not
execute it.

**How much transfers:** **~100%** — `scad2py` is the *same upstream repo*; the
clean-room modules are drop-in for either library's GOAL-2 integration.
**Recommendation:** b3d's GOAL-2 effort should *reuse cq's `p6-clean-room-math`
modules directly* rather than redo the rewrite. The clean-room hygiene (originals
never opened, only run) is already established; redoing it risks contaminating a
second agent. This is the cleanest "reuse as-is" item in the whole comparison.

---

## 6. Gaps in the CadQuery corpus that build123d's covers

The build123d corpus has three pieces with no CadQuery counterpart.

### 6.1 A caching-infrastructure deep-dive — b3d `08-caching-infrastructure`

**What b3d has:** the CadQuery corpus does **not address caching at all** — no
exploration doc, no prototype, no section. b3d `08` is a full deep-dive: build123d
has *essentially no geometry cache* (only a tiny `_color_cache`, plus OCCT's
implicit per-face triangulation); OpenSCAD has `GeometryCache`+`CGALCache` (LRU,
byte-bounded) and a stalled GSoC-2020 persistent cache; scad2py has a *working*
content-addressed cache (`FastKey` = interned hash of the CSG-subtree text dump,
`CachingVisitor` tree→DAG dedup, `@renderer` decorator). The doc's key insight:
OpenSCAD and scad2py independently converged on "hash the CSG-subtree serialization
as the cache key," and a manifold backend should adopt scad2py's `FastKey` directly
because *meshes are cheap to hash/copy/serialize while OCCT `Shape`s are not*
(`Shape.__hash__` is identity-based, useless as a content key).

**Does it transfer to CadQuery? Yes, almost entirely.** CadQuery has the *same*
gap — cq `01` and `02` never mention caching; `Shape.__hash__` in CadQuery is
likewise identity-ish. CadQuery *also* has no geometry cache, and a CadQuery+
manifold integration driven by scad2py would hit the *same* repetitive-CSG
workload. The scad2py side of the analysis (`FastKey`, `CachingVisitor`, `@renderer`)
is 100% transferable (same repo). The build123d-specific parts (`persistence.py`
`BinTools` serialization, `export_gltf`'s `BRepTools.Clean_s`) have direct CadQuery
analogues. **Note for the CadQuery effort:** caching is a real omission in the cq
corpus; b3d `08` should be read across — especially the recommendation to reuse
scad2py's `FastKey`/`CachingVisitor` wholesale and to make the *cached artifact a
mesh, not a `Shape`*.

### 6.2 A prior-art / ecosystem survey — b3d `07-prior-art-ecosystem`

**What b3d has:** the CadQuery corpus has no ecosystem survey. b3d `07` surveys
build123d issue **#1228** (a maintainer explicitly requesting the
`build123d[manifold]` optional extra — the single most important "this is wanted"
signal, and the prescribed integration *shape*), FreeCAD's `importCSG.py`
(OpenSCAD→OCC-BREP via the `.csg` IR, same PLY toolchain), PythonOpenScad/AnchorScad
(OpenSCAD-style Python on a manifold3d backend — confirms `minkowski` is hard for
everyone), PythonSCAD, the mesh-boolean library landscape (pymeshlab/CGAL are
GPL — avoid), and ochafik's own OpenSCAD PR #4533 (the owner's directly-transferable
prior experience integrating Manifold).

**Does it transfer to CadQuery? Partly.** The manifold3d ecosystem, FreeCAD
`importCSG.py`, PythonOpenScad, PR #4533, the mesh-library landscape, and the
"avoid GPL deps" finding all transfer 1:1. What does **not** transfer is the
build123d-issue-#1228 anchor — that is a build123d-project signal with no CadQuery
equivalent (the cq corpus references CadQuery discussion #1686 instead, the "use a
CSG kernel" thread). **Net:** the cq corpus would benefit from the *generic* prior-
art content; the #1228 "this is wanted upstream, ship it as an extra" finding is a
genuine build123d advantage with no cq mirror.

### 6.3 A scad2py→host *source-codegen* prototype — b3d `p5_openscad_to_b3d_source`

**What b3d has:** b3d prototyped *two* GOAL-2 directions: `p4_scad2py_to_b3d` (the
runtime `csg.Node`→build123d renderer — the cq-`p3` counterpart) **and**
`p5_openscad_to_b3d_source` (`scad2b3d.py` — emitting build123d *source code*, the
"Option B" of b3d `04`). The cq corpus only prototyped the renderer direction
(`p3-scad2py-cq-renderer`).

**Does it transfer to CadQuery? Conceptually yes, mechanically no.** b3d `04`
"Option B" itself notes source-codegen is *medium-high effort, lower payoff than
Option A* and still needs a scad2py runtime shim — so the cq corpus's choice to
skip it is defensible. The *concept* (emit idiomatic host-library Python so users
can "graduate" off OpenSCAD) transfers; the emitted code (build123d `BuildPart`/
`Box` vs CadQuery `Workplane`) is entirely library-specific. **Minor gap** — worth
noting the cq effort could add an equivalent if "export to idiomatic CadQuery
script" is a product goal, but it is correctly lower priority than the renderer.

---

## 7. Net recommendations

### 7.1 What build123d's effort should additionally do (given the CadQuery findings)

1. **Adopt a dedicated OCCT-kernel deep-dive** modeled on cq `02`. The b3d corpus
   currently under-documents *why* GOAL 1 is needed — the failure-mode catalogue
   (silent sub-tolerance merges, silently-invalid fillets, accepted self-intersecting
   torus) is the strongest motivation and transfers ~100%. (§5.1)
2. **Port cq's `p2-fast-mesh2brep` direct-shell-assembly reconstructor** and
   benchmark it. mesh→BREP super-linearity is the #1 named blocker in *both*
   corpora; cq has a prototyped mitigation b3d lacks. If it scales near-linearly,
   build `Solid.from_mesh()` / `MeshSolid.to_solid()` on it rather than on the slow
   `Mesher._get_shape` sew. (§5.2)
3. **Replicate cq's `p4-face-identity` study against build123d's selector API.**
   build123d is *more* selector-centric than CadQuery, so this matters more here.
   It directly closes b3d `01` open-question #7 and tells the `MeshSolid` design
   exactly what selector fidelity to promise (planar + provenance: yes; analytic-
   curved + fillet-after-boolean: no). (§5.3)
4. **Adopt cq's `p5-wasm-pyodide` analysis** — `OCP.wasm` was built *for build123d*;
   the browser-deployment story applies to build123d unchanged, and the
   "GPL files block any distributed WASM bundle" finding is a hard prerequisite.
   (§5.4)
5. **Treat b3d `03`'s manifold3d `[upstream]` caveats as resolved-confirmed.** The
   b3d prototypes already run 3.4.1; cq `03` introspected 3.4.1 directly. Re-verify
   only the b3d `03` §3.5 intermittent-segfault on 3.4.1 (cq saw no such issue).
   (§4.1)

### 7.2 What build123d's effort can reuse from CadQuery as-is

- **The clean-room math modules** (`cq p6-clean-room-math`: `fragments.py`,
  `transforms.py`, `colors.py`). `scad2py` is the same upstream repo; these are
  drop-in. Reusing them avoids a second agent re-doing the clean-room rewrite (and
  re-incurring the contamination risk). This is the single best "reuse verbatim"
  item. (§5.5)
- **The GPL-remediation audit + checklist** (`cq design/22`). The audit (exactly
  two contaminated files, the call-site rename map, the git-history note) is repo-
  level, not library-level — it transfers wholesale to the build123d GOAL-2 merge.
- **The BREP↔mesh empirical numbers** (cq `06`): deflection→volume-error curves,
  the 148 s/143k sewing measurement, the weld-or-fail finding. Same kernel — these
  are facts build123d can cite without re-measuring (though b3d `p1` already
  re-measured and agrees).

### 7.3 The consolidated picture

Two independent research efforts on sibling libraries reached **the same
architecture**: a manifold3d mesh backend is *feasible, high-value, and the
enabling bridge for the scad2py goal* — but must be a **non-`Shape` mesh value
type behind an opt-in extra**, with **explicit mesh→BREP conversion**, never an
implicit kernel swap (the `downcast`/`TopoDS` hardwiring and the irreversibility of
faceting both forbid it). The independent corroboration is strong on every shared
claim: the GPL contamination is exactly `calc.py`+`colors.py`; OCCT booleans are
slow-and-fragile while manifold3d is 1–2+ orders of magnitude faster and
guaranteed-manifold; mesh→BREP is the wall; hull/minkowski gate GOAL 2 on GOAL 1.

The work is **complementary, not redundant**: CadQuery's corpus is stronger on
*kernel derisking* (the `02` deep-dive, the `p2` fast-reconstruction prototype, the
`p4` selector-identity study, the `p5` WASM analysis, and an *executed* GPL
clean-room rewrite); build123d's corpus is stronger on *project integration*
(the #1228 upstream anchor and prior-art survey in `07`, and the caching design in
`08`). Each effort should read the other's gaps across. The consolidated
recommendation for build123d: ship `build123d[manifold]` as an opt-in extra
exposing free-function `mesh_fuse/cut/intersect` (phase 1) + a standalone
`MeshSolid` (phase 2), built on a *fast* mesh→BREP reconstructor (port cq `p2`),
with selector fidelity scoped per cq `p4`, caching per b3d `08`, and the scad2py
backend reusing cq's clean-room math — and stand up the missing OCCT-kernel
deep-dive so the *why* is as well-documented as the *how*.

---

*End of doc 09. Cross-refs — CadQuery corpus: `cadquery/ddocs/explorations/01–06`,
`licenses/10`, `design/22`, `prototypes/p0–p6`. build123d corpus:
`build123d/ddocs/research/01–08`, `prototypes/p1–p5`.*
