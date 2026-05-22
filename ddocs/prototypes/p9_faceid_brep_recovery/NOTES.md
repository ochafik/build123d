# p9 -- faceID-seeded exact B-rep recovery from a manifold3d boolean

**Date:** 2026-05-22 · **Status:** runnable prototype, all claims measured

Prototypes, at the **Python level**, the design in
`OCCT/ddocs/design/02-attributes-and-feature-recovery.md`: seed manifold3d's
per-triangle `face_id` with one unique id per input `TopoDS_Face`; the id
survives the boolean; for flat-faced CSG, regroup the output triangles by id,
rebuild **exact** planar faces on the *known* `Geom_Plane`, derive feature
edges from `face_id` boundaries, and run **real `BRepFilletAPI`** chamfers and
fillets on the result.

**Headline result: it works.** faceID-seeded exact B-rep recovery is achievable
in pure Python, and a real build123d `fillet()` / `chamfer()` runs on a
mesh-CSG result and yields exact, analytic, STEP-valid geometry. This directly
contradicts build123d design Principle **P7**'s "fragile, not supported"
wording for the planar case -- see "Design-doc changes" below.

All output pasted below is **real**, produced by the venv
(`/Users/ochafik/github/.ddocs-venv/bin/python`): build123d
`0.1.dev2750`, manifold3d `3.4.1`, trimesh `4.12.2`, numpy `2.2.6`,
Python 3.10.12, macOS arm64.

## How to run

```bash
VENV=/Users/ochafik/github/.ddocs-venv/bin/python
cd /Users/ochafik/github/build123d/ddocs/prototypes/p9_faceid_brep_recovery

$VENV faceid_bridge.py     # step 1: seed face_id per TopoDS_Face (self-test)
$VENV survive.py           # step 2: identity survives the boolean
$VENV recover.py           # step 3: faceID-grouped exact planar recovery
$VENV payoff.py            # step 4: THE PAYOFF -- real fillet/chamfer
$VENV curved.py            # step 5: the curved (cylindrical-bore) case
$VENV reconcile_p8.py      # step 6: reconcile with p8
$VENV -m pytest test_p9.py -v   # 17 tests, all pass
```

## Files

- `faceid_bridge.py` -- build123d <-> manifold3d bridge that **seeds**
  `Mesh64.face_id` per `TopoDS_Face`; keeps the `SideMap`
  (`id -> {build123d Face, Geom_Surface, source solid}`).
- `recover.py` -- faceID-grouped reconstruction: planar groups -> **exact**
  `TopoDS_Face` on the known `Geom_Plane`; feature edges from `face_id`
  boundaries; connected-component split for cut-apart faces; sew to a `Solid`.
- `refacer_curved.py` -- the honest curved fallback: faceted patch, no re-trim.
- `survive.py`, `payoff.py`, `curved.py`, `reconcile_p8.py` -- the 4 probes.
- `test_p9.py` -- pytest, 17 tests, all passing.

---

## Debugging the `Mesh(face_id=...)` constructor (the brief's open question)

The brief warned that `Mesh(face_id=...)` "gets rejected as 'incompatible
function arguments' even after casting -- debug this properly." Done. **The
`face_id` channel works.** The rejection is **not** a dtype problem:

| what we tried | result |
|---|---|
| `face_id` as int64 (what `to_mesh()` returns) into `Mesh` | **OK** -- nanobind auto-casts to uint32 |
| `tri_verts` as int32 (what `to_mesh()` returns) | **OK** -- auto-cast |
| `vert_properties` as float64 into `Mesh` (not `Mesh64`) | **OK** -- auto-cast |
| non-contiguous (strided-view) `face_id` | **OK** -- nanobind copies |
| `face_id` as a Python **list** | **FAIL** -- "incompatible function arguments" |
| `face_id` shape **(N,1)** instead of **(N,)** | **FAIL** -- "incompatible function arguments" |

**The real failure is a rank/shape bug.** `Mesh.__init__`'s `face_id` parameter
is typed `ndarray[uint32, shape=(*), order='C']` -- it must be a flat **1-D**
array. A column vector `(N,1)` or a Python list is rejected. nanobind silently
auto-casts dtype and contiguity, so dtype-casting alone never fixes it; the
prior effort almost certainly passed a 2-D / column `face_id`. The fix is one
line: `np.ascontiguousarray(face_id.ravel(), dtype=np.uint64)`.

`Mesh` wants uint32 ids + float32 verts; `Mesh64` wants uint64 ids + float64
verts. **p9 uses `Mesh64`** so OCC's double-precision vertices survive without
float32 truncation. `to_mesh()` returns `tri_verts` as int32 and `face_id` as
int64 and `.face_id` is read-only -- all fine, because we never re-seed an
existing mesh; each forward conversion builds a fresh `Mesh64`.

**Channel used: `face_id` (not `reserve_ids`/`run_original_id`).** Both
channels carry an integer per triangle and both survive booleans, so the B-rep
reconstruction is identical regardless. `face_id` is the better Python fit: it
is natively per-**face** granularity, whereas `run_original_id` is
per-**run**/per-input-**solid** and forcing per-face out of it means splitting
each solid into one run per face. Seeding `face_id` is one flat array on one
`Mesh64` per solid. It is also the same channel the OCCT C++ design uses
(`MeshGL64::faceID`).

---

## Step 1-2 -- seeding & survival (`survive.py`)

Seed one unique global id per input `TopoDS_Face`, run booleans, count distinct
ids in the result. If manifold discarded the seed, the count would explode
toward the triangle count; staying near the **input face count** proves
identity is retained -- the OCCT doc sec 4 result.

```
  WA box - notch box (all planar)              12 input faces ->     32 tris in   10 face-groups
  WB box - cylindrical bore                     9 input faces ->    268 tris in    7 face-groups
  WC union 8 overlapping boxes                 48 input faces ->    156 tris in   48 face-groups
  WD fuse 2 boxes, coplanar abutting face (doc W4)   12 input faces ->     20 tris in   10 face-groups
  WE sphere - box, fine deflection (doc W5)     7 input faces ->   2036 tris in    4 face-groups
  WF sphere + 6 radial boxes                   37 input faces ->   2300 tris in   31 face-groups
```

The pattern is decisive and reproduces the OCCT doc's W4/W5:

- **WE (doc W5 style)** -- 2036 triangles -> exactly **4** face-groups: the
  sphere's face + the 3 box faces the cut exposes (the other 3 box faces lie
  outside the sphere and contribute nothing). 7 seeded, 4 survive -- and the
  mesh says precisely which.
- **WD (doc W4 style)** -- 12 input faces -> **10** groups: the two coplanar
  abutting faces are entirely consumed by the union.
- **WC** -- 156 triangles -> **48** groups, exactly the 8x6 box faces; nothing
  shattered.

A count *below* the input total is not loss -- it is the result correctly
reporting which input faces survived. Group count tracks input face count
(tens), never the triangle count (hundreds-thousands).

---

## Step 3-4 -- THE PAYOFF: exact recovery + real fillet/chamfer (`payoff.py`)

Workload: a 40x30x12 plate with a 14x10 blind rectangular pocket, computed via
the **manifold mesh path**, recovered by faceID grouping, then a **real
build123d `fillet()` / `chamfer()`** on the recovered pocket feature edges.

```
==============================================================================
STEP 4 -- THE PAYOFF: real fillet/chamfer on a mesh-CSG result
==============================================================================

Reference (native build123d boolean):
    native Box - pocket                valid=True  volume=13140.000
    faces=11  edges=24

Manifold mesh-CSG path with seeded face_id:
    mesh result: 28 triangles, 11 face-groups (input faces=12)

faceID-grouped exact B-rep recovery:
    11 exact planar faces, 0 curved patches
    24 feature edges (faceID boundaries)
    recovered Solid                    valid=True  volume=13140.000
    geom types present: ['GeomType.PLANE']
    volume error vs native B-rep: 0.000000  (EXACT)

REAL build123d fillet() on a recovered pocket feature edge:
    found 4 concave pocket feature edges (expected 4)
    target edge: vertical, length=9.00, center=(-7.0, -5.0, 1.5)
    filleted Solid (r=2.0)             valid=True  volume=13147.726
    geom types after fillet: ['GeomType.CYLINDER', 'GeomType.PLANE']
    fillet produced an analytic CYLINDER blend face: True

REAL build123d chamfer() on a recovered pocket feature edge:
    target edge: vertical, length=9.00, center=(7.0, -5.0, 1.5)
    chamfered Solid (1.5)              valid=True  volume=13150.125

Combined: fillet all 4 pocket feature edges at once:
    4-edge filleted Solid              valid=True  volume=13170.903
    analytic CYLINDER blend faces created: 4 (expected 4)

STEP export of the recovered + filleted solid:
    wrote .../recovered_filleted.step (35298 bytes)
    re-imported STEP                   valid=True  volume=13147.726

------------------------------------------------------------------------------
PAYOFF VERDICT
------------------------------------------------------------------------------
  [PASS] recovered solid valid
  [PASS] volume EXACT (err<1e-6)
  [PASS] fillet succeeded & valid
  [PASS] fillet made analytic CYLINDER blend
  [PASS] chamfer succeeded & valid
  [PASS] fillet+chamfer combined valid
  [PASS] STEP exported & re-imports valid
```

**This is the significant result.** The recovered solid has the **same face
and edge count as the native B-rep boolean** (11 faces, 24 edges), its volume
is **bit-exact** (error `0.000000`, not "within tessellation tolerance"), every
face is an analytic `GeomType.PLANE`, every edge a `GeomType.LINE`, and
`BRepFilletAPI` runs on it for real -- producing genuine analytic
`GeomType.CYLINDER` blend faces. The STEP export re-imports valid.

Why it is exact, not approximate: the recovered planar faces are not *fitted*
to the triangle soup. Each face is built on the **exact `Geom_Plane`** carried
in the side map, and every boundary vertex is orthogonally **projected onto
that exact plane**, erasing tessellation jitter. For an all-planar CSG result
the only place facets entered -- the flat faces -- is exactly where the known
analytic surface lets us undo them. Straight edges are plane-plane
intersections; they are exact too.

---

## Step 5 -- the curved case (`curved.py`)

A 30x30x12 plate minus a cylindrical bore (`Cylinder(r=5)`):

```
input faces seeded: 9
  cylindrical input faces: ids [6]
  id 6: CYLINDER  r=5.000  axis_dir=(0.0, 0.0, 1.0)  solid=bore

mesh result: 268 triangles, 7 face-groups

Per-group analysis:
  id  6: CYLINDER   126 tris (solid=bore)  <-- THE BORE: faceID identifies 126
         triangles as lying on Geom_Cylinder r=5.00

Recovery: 6 exact planar faces, 1 curved groups (kept faceted)
  solid valid=True  volume=9859.08
  analytic CYLINDER faces in recovered solid: 0
```

**Honest result.** faceID *does* identify the bore: all 126 wall triangles are
one group, and the side map says "this group lies on `Geom_Cylinder r=5.0`" --
real, retained information. But the recovered bore stays **faceted** -- 0
analytic cylinder faces. Exact re-trimming of the known cylinder with
re-fitted boundary curves + p-curves is the **Tier C** research problem and is
*not* done here. faceID makes the curved case *tractable* (you know the
surface, you do not have to guess it); it does not make it *done*.

So the honest split, confirmed in Python: **planar -> fast mesh path + exact
filletable B-rep, achievable today; curved -> fast mesh path + identity
preserved, exact re-fit still research.**

---

## Step 6 -- reconciliation with p8 (`reconcile_p8.py`)

p8 concluded raw `face_id` is "unusable" and routed identity through
`run_original_id` + `reserve_ids`. The OCCT doc says *seeded* `faceID` is
exactly right. **These are not in conflict -- p8 was solving a different
problem, and p8 was correct about the problem it actually had.**

Measured:

```
CLAIM 1 -- p8 is right about UNSEEDED face_id
  a tessellated cylinder, NO seed -> manifold computes face_id from
  coplanarity: 248 triangles -> 65 distinct face_ids
  => raw/unseeded face_id is UNUSABLE for face identity. p8 was CORRECT.

CLAIM 2 -- the OCCT doc is right about SEEDED face_id
  the SAME cylinder, SEEDED one id per TopoDS_Face: 248 triangles -> 3 ids
  => seeded face_id IS the right hook. The OCCT doc is CORRECT.

CLAIM 3 -- p8's split-face concern is REAL (and p9 handles it)
  bar sliced clean in two: 4 seeded ids each carry a face the cut split
  into 2 disjoint pieces: [(2,2),(3,2),(4,2),(5,2)]
```

**Was p8 wrong?** No. p8 never *seeded* `face_id`. Unseeded, manifold fills
`face_id` from its own coplanar-face calculation, which shatters every curved
face (65 ids for one cylinder, above). p8 observed exactly that and called it
"unusable" -- a correct observation of the *unseeded* channel. p8 then
(reasonably) used `run_original_id` + `reserve_ids`, a channel p8 *did*
control. p8's design-doc sec 6.2 even says `face_id` is "unique only within a
run" -- that *is* the unseeded behaviour; once seeded with a global counter the
caveat disappears.

The OCCT doc's key move -- which p8 missed -- is the conditional in manifold's
`mesh.h`: *"Input faceIDs will be maintained ... but if none are given, they
will be filled in with Manifold's coplanar face calculation."* p8 hit the
"if none are given" branch. p9 takes the "input faceIDs ... maintained" branch.

**p8's split-face worry is real and important.** A face cut clean into two
disjoint pieces *does* keep one seeded id. p9 handles it by splitting each id's
triangles into edge-**connected components** before building faces -- one
`TopoDS_Face` per piece (a bar cut in two recovers as a `Compound` of two
solids, volume exact). This is the *same fix* p8's connectivity region-grow
applies; p9 just applies it as a per-id post-step rather than ignoring `face_id`
entirely. So: **seed the id channel for identity, then still use connectivity
to split disjoint pieces.** Both are needed; neither alone is enough.

**Which channel is better in Python?** `face_id`. It is natively per-face;
`run_original_id` is per-solid and needs per-face run orchestration to match.
Seeding `face_id` is one flat array per solid. The "incompatible arguments"
scare is just the rank bug above. (`reserve_ids`/`run_original_id` remains a
valid fallback -- the reconstruction is channel-agnostic -- but it is more
plumbing for no gain here.)

---

## Does it work in pure Python? -- the verdict

**Yes.** faceID-seeded exact B-rep recovery works in pure Python, with measured
evidence:

- Seeding `manifold3d.Mesh64.face_id` per `TopoDS_Face` works (the constructor
  is fine; the prior blocker was a 1-D-vs-2-D shape bug).
- The seed survives union / difference / intersection / chained booleans;
  distinct-id count tracks input face count (reproduces OCCT doc sec 4).
- For all-planar CSG, faceID grouping rebuilds an **exact** B-rep -- analytic
  planar faces on the known `Geom_Plane`, exact straight edges, bit-exact
  volume, same face/edge count as the native boolean.
- A **real `BRepFilletAPI` fillet and chamfer** runs on the recovered solid
  and produces exact analytic blend faces; the result STEP-exports and
  re-imports valid. **17/17 pytest tests pass.**

**The planar payoff -- is it significant?** Yes. It means flat-faced CSG
(transpiled OpenSCAD, plate/bracket/mechanical work -- the bulk of build123d's
CSG-heavy use) can take the fast, robust manifold mesh path **and still come
back as an exact, filletable B-rep**. The mesh path is no longer "faceted only"
for that whole class of part.

---

## How this should change `design-manifold-in-build123d.md`

### Principle P7 -- the "fragile, not supported" wording is now WRONG for planar

P7 currently says fillet/chamfer is "BREP-only ... a `fillet()` *after* a mesh
stage has nothing to grab" and calls the planar exception "fragile and not a
supported path." **The measured evidence contradicts this for the planar
case.** Recommend rewriting P7 to:

- Keep the rule that fillet/chamfer are not *mesh* operations -- true.
- **Replace** "the one partial exception ... is fragile and not a supported
  path" with: *"For an **all-planar** CSG result, faceID-seeded reconstruction
  (`p9`) rebuilds an **exact, analytic, filletable** B-rep -- real
  `BRepFilletAPI` fillet/chamfer succeed and produce exact geometry (`p9`
  payoff, 17/17 tests). This is a **supported planar path**, not a fragile
  exception. Fillet-after-mesh is one-way only for **curved**-dominated
  results."*
- The one-way ordering diagram should branch: planar-CSG -> recover exact
  B-rep -> fillet/chamfer OK; curved-CSG -> faceted, fillet not available.

### Section 6.2 -- the `face_id` row is wrong; it is the recommended channel

6.2 currently says `face_id` is "partial -- unique only within a run, collides
across runs ... a *hint* ... never a global key," and 6.2's prose says "`p8`
found it unusable ... the `ReFacer` therefore ignores raw `face_id`." That
describes **unseeded** `face_id`. Recommend:

- Change the `face_id` table row to distinguish **unseeded** (manifold's
  coplanar calc -- shatters curved faces, the p8 finding) from **seeded** (one
  id per input `TopoDS_Face` via `Mesh(face_id=...)` -- *maintained* through
  the boolean, the recommended identity channel; `p9`).
- Note that the `Mesh(face_id=...)` constructor works; the "incompatible
  arguments" issue is a 1-D-array requirement, not a real blocker.
- State `face_id` is the **better** per-face channel than
  `reserve_ids`/`run_original_id` (per-face vs per-solid granularity), and it
  matches the OCCT C++ design (`MeshGL64::faceID`).

### Section 6.3 -- add faceID-grouped *exact* reconstruction

6.3's `ReFacer` (region-grow + `UnifySameDomain`) produces faces but not
provably-exact ones. Recommend adding a second strategy: **seed `face_id` per
face, group by it, rebuild planar faces on the known `Geom_Plane` from the
side map.** Connectivity is still used -- but to split a seeded id into disjoint
pieces, not as the primary grouping key. This is strictly more exact (faces lie
on the *input* analytic plane, not a fitted one) and is what enables the P7
revision.

### Section 6.5 -- the planar/curved split now has a stronger planar half

6.5 says planar CSG gets "exact `Face` counts, all directional and plane
selectors." Strengthen it: **planar CSG also gets exact filletable geometry and
clean STEP** (`p9`). The curved half is unchanged and correct -- provenance
only, no analytic re-trim (Tier C). The R3 risk-register line stays valid; the
"never mis-answer" contract is unchanged.

---

## Honest limits

- **Curved is faceted.** A cylindrical/spherical/NURBS region is *identified*
  by faceID (one group on a known surface) but recovered as a faceted patch.
  Exact re-trim (re-fit boundary curves, build p-curves) is Tier C, not done.
  A fillet on a *curved* recovered region is still unavailable.
- **Speed.** Pure-Python recovery is fine at this scale -- `base - 32 holes`
  (652 tris): seed+boolean 0.068 s, recovery 0.095 s, linear. Not benchmarked
  into the 1e5-1e6-triangle range; the boundary-loop chaining and per-face
  `MakeFace` would need profiling there (the OCCT C++ path avoids per-binding
  overhead, which is the doc's argument for doing this in C++).
- **Robustness.** Verified on axis-aligned and 30deg-rotated cuts, coplanar
  abutting unions, through-holes (face-with-hole loops), chained 2-pocket
  differences, and split-in-two solids -- all exact and valid. Not stress-
  tested on near-degenerate slivers, self-touching geometry, or dirty imported
  B-rep; the coordinate weld (grid-snap at 1e-7) inherits the OCCT doc sec 5
  weakness (a topology-driven exact weld would be more robust).
- **Edge cases handled:** a seeded id split into N disjoint pieces -> N faces /
  a `Compound`; a planar face with interior hole loops -> a face with holes.
- **Assumes the seed is faithful.** If two genuinely distinct input faces are
  coplanar and adjacent, they get distinct ids (good) but
  `ShapeUpgrade`-style merging is not applied -- the recovered solid keeps the
  input face partition, which is the intended behaviour for identity but means
  face count follows the *input*, not a minimal B-rep.

## Bottom line

The OCCT C++ design's central claim -- *seed manifold's `faceID` per
`TopoDS_Face`, and flat-faced CSG becomes an exact, filletable B-rep* --
**replicates in pure Python**. The `Mesh(face_id=...)` constructor works once
you pass a 1-D array. p8 was not wrong; it never seeded the channel and so
correctly described the unseeded behaviour. build123d Principle P7's blanket
"fillet-after-mesh is fragile / not supported" is **incorrect for the planar
case** and should be revised: planar fillet-after-mesh is solidly achievable.
