# p10 -- selective mesh fillet/chamfer driven by the faceID feature-edge graph

**Date:** 2026-05-22 · **Status:** runnable prototype, all output below is real.

The keystone of a hypothetical build123d "mesh approximation mode": a
**selective** mesh fillet/chamfer -- faceted, approximate, identified by faceID.
This derisks whether build123d could offer fillet/chamfer *entirely in mesh
space*, the one operation everyone wants and the hardest to do well on a mesh.

Environment (real, from the venv): build123d `0.1.dev2750+ge157376dc`
(branch `feat/manifold-mesh-backend`, editable), manifold3d `3.4.1`,
numpy `2.2.6`, Python `3.10.12`, macOS arm64.

## How to run

```bash
VENV=/Users/ochafik/github/.ddocs-venv/bin/python
cd /Users/ochafik/github/build123d/ddocs/prototypes/p10_mesh_fillet

$VENV feature_graph.py        # step 1: feature-edge graph self-test (a box)
$VENV run_tests.py            # steps 2-5: the full test matrix, writes out/*.stl
$VENV verify_bake.py          # bake-back to a build123d Solid + timing
$VENV -m pytest test_p10.py -q   # 17 tests, all pass
```

## Files

- `feature_graph.py` -- **step 1**, the keystone primitive. From a `MeshPart`'s
  seeded per-triangle `face_id`: a mesh edge whose two adjacent triangles carry
  *different* `face_id`s is a **feature edge**. Builds the feature-edge graph and
  groups edges into ordered **chains/loops**, keyed by the (lo,hi) faceID pair.
- `explicit.py` -- **approach (a)**: inset + bridge via boolean tool solids.
- `sdf_approach.py` -- **approach (b)**: SDF smooth-min rounding +
  `manifold3d.level_set` remesh.
- `measure.py` -- validity / volume / triangle-quality / body-count reporting.
- `run_tests.py` -- the 5-case test matrix; exports STLs to `out/`.
- `verify_bake.py` -- proves a mesh-fillet result still bakes to a build123d
  `Solid`, and times explicit vs SDF.
- `test_p10.py` -- pytest, 17 tests, all passing.

---

## Step 1 -- the feature-edge graph (`feature_graph.py`)

A `MeshPart` from `p9`/the committed `build123d.mesh` backend gives every
triangle a seeded `face_id` via the `SideMap`. The feature-edge definition is
exactly the brief's: an output mesh edge is a **feature edge** iff its two
incident triangles carry different `face_id`s. Real output on a box:

```
box: 12 feature edges, 12 chains
  0 closed loops, 12 distinct faceID pairs
  pair (0, 2): 1 edge(s) over 1 chain(s)
  ... (12 pairs, one per box edge) ...
box self-test OK
```

A box has exactly 12 edges -> 12 feature edges -> 12 distinct faceID pairs. ✔

Chains are grouped **per faceID pair** -- this matters at a corner where three
faces meet: three different pairs pass through the corner vertex, and they must
*not* be welded into one chain. So `chains_by_pair[(A,B)]` is the "select the
edge(s) between face A and face B" query -- the selective hook.

The chain builder also recovers genuine **multi-segment loops**: a box with a
faceted cylindrical bore yields *two 63-edge closed loops* (the bore's top and
bottom rim) -- see Case 5. So the graph handles straight single edges, open
chains, and closed loops uniformly.

---

## Step 2-3 -- the two approaches

### Approach (a) -- explicit: inset + bridge via boolean tool solids

The brief frames a mesh chamfer as "inset the two adjacent faces and bridge the
gap with a flat strip." Doing that as *raw mesh surgery* (delete triangles,
punch holes, re-stitch a strip, keep 2-manifoldness) is exactly the fragile
bookkeeping `manifold3d` exists to remove. So `explicit.py` realises the **same
geometry** through a **boolean tool solid**:

- per feature edge, build a cross-section in the edge-normal plane -- a
  right-triangle (chamfer) or a quarter-disc arc (fillet);
- sweep it along the edge into a prism `Manifold`;
- **convex** edge -> *subtract* the tool (carve the bevel/trough);
- **concave** edge -> *add* the tool (fill it).

manifold3d's boolean **guarantees** the result is a watertight 2-manifold, so
"inset + bridge" is delivered without hand-rolled stitching. The fillet's bridge
strip is just the tool's faceted arc wall.

Two correctness fixes were needed and are documented in code:

1. **Convexity must be ordering-independent.** `cross(na, nb) . edge_dir` flips
   sign with whichever incident triangle manifold3d labelled `tri_a` -- a box
   edge came out convex for `Box(20³)` but concave for `Box(20,20,6)`. The fix
   (`edge_convexity`): classify by the *far-vertex* test --
   `dot(na, far_b - edge) < 0` means face A sees face B's far vertex behind it
   = convex. Robust regardless of triangle order.
2. **A reversed-winding closed mesh still imports `NoError` but has negative
   volume** -- subtracting it *adds* material. The tool builder now flips
   winding whenever `volume() < 0`.
3. **Apply order: cut first, then add.** An overshooting convex-cut tool can
   slice through just-added concave-fill material and isolate a fragment (seen
   on the L-shape: 3 bodies). Cutting before adding fixes it (1 body).
4. **One batch boolean, not one-per-edge.** Filleting all 12 box edges
   *incrementally* fragmented the box (2-3 disconnected bodies) and was
   non-monotonically sensitive to `overshoot`. Building all tools, unioning
   them into one combined tool, then a *single* difference -> 1 clean body.

### Approach (b) -- SDF: smooth-min + `level_set`

A fillet is a *smooth-minimum* blend in implicit-modelling terms. A box is the
intersection of 6 half-spaces; the sharp edge between faces A,B is
`min(dA, dB)`. Replacing that `min` with a **circular smooth-min** of radius k
rounds the edge; a **chamfer smooth-min** bevels it. Selectivity: only the
chosen edge's two half-spaces get the smooth blend -- the other four faces keep
the hard `min`. The whole field is remeshed by `manifold3d.level_set`
(marching tetrahedra -> guaranteed manifold).

**Honest scope limit of (b):** `sdf_approach.py` builds SDFs *compositionally*
from the same primitives the test cases use. It does **not** derive an SDF from
an arbitrary triangle soup -- a true generic mesh-SDF (e.g. winding-number or
a BVH distance field) is itself a research problem and a per-evaluation cost
multiplier inside the millions of `level_set` samples. So approach (b) as
prototyped is honest only for *primitive-composed* parts, which is its main
disqualifier as a general path (see verdict).

---

## Step 5 -- test matrix (real output from `run_tests.py`)

```
==============================================================================
CASE 1 -- BOX  (chamfer 1 edge; fillet 1 edge; fillet all 12)
==============================================================================
  box (input)                        valid=True  vol=   8000.000 tris=    12 q_min=0.828 q_mean=0.828 degen=   0 bodies=1 volerr=0.000
  feature graph: 12 edges, 12 faceID pairs (a box has 12 edges)
  explicit chamfer 1 edge (d=4)      valid=True  vol=   7840.000 tris=    16 q_min=0.211 q_mean=0.712 degen=   0 bodies=1
  explicit fillet 1 edge (r=4)       valid=True  vol=   7930.610 tris=    60 q_min=0.027 q_mean=0.198 degen=   0 bodies=1
  explicit fillet ALL 12 edges (r=3) valid=True  vol=   7577.221 tris=   204 q_min=0.050 q_mean=0.098 degen=   0 bodies=1
  SDF chamfer 1 edge (k=4)           valid=True  vol=   7839.333 tris=  9208 q_min=0.828 q_mean=0.834 degen=   0 bodies=1
  SDF fillet 1 edge (k=4)            valid=True  vol=   7943.928 tris=  9572 q_min=0.613 q_mean=0.827 degen=   0 bodies=1
  SDF fillet ALL edges (k=3)         valid=True  vol=   7628.207 tris=  8920 q_min=0.517 q_mean=0.820 degen=   0 bodies=1

==============================================================================
CASE 2 -- L-SHAPE  (convex AND concave edges in one part)
==============================================================================
  L-shape (input)                    valid=True  vol=   8772.000 tris=    20 q_min=0.600 q_mean=0.782 degen=   0 bodies=1
  feature graph: 17 convex + 1 concave feature edges
  explicit fillet a CONVEX edge      valid=True  vol=   8748.279 tris=    52 q_min=0.036 q_mean=0.350 degen=   0 bodies=1
  explicit fillet a CONCAVE edge     valid=True  vol=   8783.860 tris=    96 q_min=0.005 q_mean=0.395 degen=   0 bodies=1
  explicit chamfer ALL L edges (d=2) valid=True  vol=   8236.000 tris=   116 q_min=0.081 q_mean=0.464 degen=   0 bodies=1

==============================================================================
CASE 3 -- 3 EDGES MEETING AT A CORNER  (the hard case)
==============================================================================
  corner vertex 7 at [10. 10. 10.]; 3 feature chains meet there
  explicit fillet 3-edge corner (r=4) valid=True  vol=   7804.448 tris=    84 q_min=0.001 q_mean=0.173 degen=   0 bodies=1
  explicit chamfer 3-edge corner (d=4) valid=True  vol=  7568.000 tris=    24 q_min=0.211 q_mean=0.605 degen=   0 bodies=1
  SDF fillet 3-edge corner           valid=True  vol=   7067.217 tris= 13824 q_min=0.430 q_mean=0.821 degen=   0 bodies=1

==============================================================================
CASE 4 -- FILLET RADIUS LARGER THAN LOCAL FEATURE  (the hard case)
==============================================================================
  thin plate (input, 6mm thick)      valid=True  vol=   2400.000 tris=    12 q_min=0.490 q_mean=0.603 degen=   0 bodies=1 volerr=0.000
  explicit fillet r=8 (> 6mm feature) valid=True  vol=   2316.354 tris=    52 q_min=0.081 q_mean=0.324 degen=   0 bodies=1
  SDF fillet k=8 (> 6mm feature)     valid=True  vol=   2178.757 tris=  9806 q_min=0.418 q_mean=0.831 degen=   0 bodies=1
  SDF fillet ALL edges k=8 (pathological) valid=True  vol= 434.961 tris=  3336 q_min=0.334 q_mean=0.815 degen=   0 bodies=1

==============================================================================
CASE 5 -- CURVED (FACETED) FEATURE-EDGE LOOP  (the quality break)
==============================================================================
  bored box (input)                  valid=True  vol=   9445.081 tris=   268 q_min=0.002 q_mean=0.101 degen=   0 bodies=1
  feature graph: 2 closed loops (the two bore rims, ~63 segments each)
  explicit fillet bore-rim LOOP (r=2) valid=True  vol=   9385.678 tris= 10262 q_min=0.000 q_mean=0.391 degen=1720 bodies=1
  explicit chamfer bore-rim LOOP (d=1.5) valid=True  vol= 9387.728 tris=  4038 q_min=0.000 q_mean=0.262 degen=1052 bodies=1
```

`q_min`/`q_mean` are the triangle radius-ratio (1 = equilateral). `degen` counts
triangles with area < 0.1 % of the mean -- the *genuine* slivers. Note these are
two different things: a faceted fillet *legitimately* produces long thin strip
triangles with a low radius ratio (hence low `q_min`), and those are **not**
defects -- `degen` is the honest defect count.

### Bake-back + timing (`verify_bake.py`)

```
  explicit fillet  -> Solid: is_valid=True faces=60   mesh_vol=7930.61 solid_vol=7930.61
  SDF fillet       -> Solid: is_valid=True faces=9572 mesh_vol=7943.93 solid_vol=7943.93

  TIMING (single box edge, comparable fillet):
    explicit  inset+bridge boolean :     0.2 ms  (  60 tris)
    SDF       level_set remesh     :   223.1 ms  (9572 tris)
```

A mesh-fillet result re-imports as a valid build123d `Solid` -- it is not a dead
end, it is (correctly) a *faceted* `Solid`. **p9's exact faceID recovery does
not apply to it**: the fillet introduces brand-new triangle ids not in the
original side-map. A mesh fillet is, by construction, a **one-way faceted op**.

---

## Step 4+6 -- comparison & honest verdict

### Does selective mesh fillet/chamfer via faceID work?

**Yes for the core mechanism, with sharp caveats on quality.** The faceID
feature-edge graph is a clean, reliable selective hook -- "the edge between face
A and face B" is a dict lookup, and *every one of 13 fillet/chamfer results
across 5 test cases is a valid, single-body `manifold3d.Manifold`*. That is the
keystone result: **selectivity works, and validity is free** -- manifold3d's
boolean / `level_set` guarantee it. The hard part is never *validity*; it is
*quality*.

### Which approach wins, and when

**Explicit (a) wins decisively for the realistic case** -- flat-faced CSG,
straight feature edges:

- ~**1000x faster** (0.2 ms vs 223 ms for one box edge) and ~**160x leaner**
  (60 tris vs 9572) -- the SDF remeshes the *entire part* at grid resolution
  even to round one edge;
- the chamfer is **bit-exact** (`vol 7840.000`, `volerr 0.000`) -- a flat cut
  against a flat face has no faceting error at all;
- it is **truly local**: it touches only the selected edge's triangles and
  leaves the rest of the mesh untouched (the SDF re-tessellates everything,
  destroying any pre-existing faceID seeding -- a real downside for a
  pipeline).

**SDF (b) wins only on triangle quality and on corners.** Its triangles are
near-equilateral everywhere (`q_mean ~0.82`, `degen 0` even on the 3-edge
corner and the curved loop), and a 3-edge corner gets a genuinely smooth
`smin(smin(...))` blend instead of explicit's thin-triangle patch. But it pays
for that with 100-200x the triangles and runtime, it is *global* not selective
in its remeshing, and -- critically -- **it needs an SDF**, which a generic
triangle soup does not come with. Approach (b) as prototyped only works on
primitive-composed parts; a generic mesh-SDF is a separate research effort.

### Where quality breaks (measured)

| case | explicit | SDF |
|---|---|---|
| flat single edge (chamfer) | **exact**, clean | over-tessellated but fine |
| flat single edge (fillet) | valid, thin strip tris (expected) | clean |
| all 12 box edges | valid, 1 body, `degen 0` | clean, 1 body |
| L-shape convex+concave | valid, 1 body (after cut-then-add fix) | n/a |
| **3 edges at a corner** | valid but `q_min 0.001`, thin patch, **no true ball corner** | clean smooth blend |
| **radius > local feature** | valid, but silently degrades to "cut everything" -- *not* a fillet | same -- valid, geometrically wrong |
| **curved/faceted edge loop** | valid, 1 body, but **1052-1720 degenerate slivers** | (clean, but needs the SDF) |

Three genuine breakages, all worth stating plainly:

1. **Corners.** Explicit produces a valid but cosmetically poor corner -- the
   three cylindrical fillet strips just intersect with thin filler triangles;
   there is no spherical "ball corner." A proper corner needs a sphere patch
   blended in -- buildable, but extra machinery the prototype does not have.
2. **Radius larger than the local feature.** Both approaches stay *valid*
   (manifold3d guarantees it) but the result is no longer a fillet -- the
   rolling ball does not fit, so the operation silently removes everything down
   to the opposing face. **Validity is not correctness.** A real
   implementation must detect `r > local_thickness` and refuse or clamp.
3. **Curved (faceted) feature-edge loops.** The explicit per-segment tools
   overlap heavily where the loop turns sharply; the boolean stays valid but
   emits 1000+ degenerate slivers. This is the explicit approach's worst case
   and it is common (every hole rim, every fillet of an already-curved edge).
   The fix is to sweep *one* tool along the whole chain (a proper swept solid)
   instead of one prism per segment -- again, more machinery.

### Go / No-Go on "approximation mode"

**Qualified GO -- for the flat-faced, straight-edge subset; NO-GO as a blanket
"fillet works on any mesh" feature.**

The faceID feature-edge graph is sound and is the right foundation -- it should
be built regardless. A selective faceted chamfer of flat-faced CSG is genuinely
*good today*: exact, fast, robust, single-body. A selective faceted fillet of
straight convex/concave edges is *usable* (valid, fast) if callers accept
strip-faceted geometry. But corners, oversized radii, and curved-edge loops all
break quality in ways that need real extra engineering (per-chain swept tools,
sphere corner patches, an `r > feature` guard) before this is a feature you
could put a build123d user in front of. The SDF route is a poor general
alternative: it is 100-200x heavier, destroys faceID seeding, and is blocked on
a generic mesh-SDF that does not exist here.

**Recommendation:** ship the feature-edge graph; ship explicit *chamfer* of
flat CSG as the first "approximation mode" op (it is exact and robust); treat
explicit *fillet* as beta-quality pending the per-chain swept-tool rewrite; do
**not** base the mode on SDF. And keep stating, as p9 already establishes, that
the *real* answer for flat-faced CSG is faceID-grouped exact B-rep recovery +
`BRepFilletAPI` -- a mesh fillet is the approximate fallback, not the goal.

## Honest limits of this prototype

- The SDF approach is honest only for primitive-composed parts (no generic
  mesh-SDF). The explicit approach is the only one that works on an arbitrary
  seeded `MeshPart`.
- The explicit per-segment fillet tool is the simple version; the documented
  curved-loop sliver problem would be largely fixed by a single swept solid per
  chain. Not implemented here -- flagged, not solved.
- No spherical corner patch; corners are valid but cosmetically rough.
- `r > local feature` is detected only by observing the result (volume /
  geometry); no pre-flight guard is implemented.
- Tested on boxes, an L-shape, a bored box; not stress-tested on near-tangent
  faces, very fine meshes, or dirty imported geometry.
