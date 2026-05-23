# Mesh fillet / chamfer — engineering design

> **Status:** decision document. The `p10` prototype derisked the keystone; this
> doc picks the engineering shape that ships. Owner has committed to the build.
> **Branch:** `feat/manifold-mesh-backend` · **Sub-package:** `build123d.mesh` ·
> **Date:** 2026-05-23 (after p10, 2026-05-22). **Author:** design agent.
>
> **Cited corpus:**
> `ddocs/prototypes/p10_mesh_fillet/NOTES.md` (results table, the three failure
> modes), `ddocs/research/12-mesh-approximation-mode.md` (mode framing, A3/A4
> phasing), `ddocs/research/03-manifold3d-deep-dive.md` (manifold3d API surface),
> `ddocs/design/design-manifold-in-build123d.md` §3.4 (faceID seeding), §6
> (faceID feature edges), **P3** (never silently mis-answer), **P7** (where
> finishing sits), `src/build123d/mesh/` (the shipped backend).
> Prior art: BOSL2 `rounding.scad` (corner / rounded_prism), ACIS / Spatial
> "vertex blend with setbacks" (the canonical 3-edge-corner construction),
> Fusion 360 "Rolling Ball" vs "Setback" corner option, the Pratt rolling-ball
> trim paper.

---

## 0. TL;DR (the decisions)

1. **Selection** is by **faceID-pair feature chain** (`p10` keystone), not by
   triangle edge. `MeshPart.fillet(edges, radius)` / `.chamfer(edges, size)`
   accept a `FeatureChainSelection` (a typed query) — the user *names* an
   edge by its two adjacent face provenances, never by triangle index.

2. **The tool is one swept solid per chain.** Per-vertex frames along the
   chain, each carrying the cross-section profile (chamfer triangle or fillet
   arc) lifted to 3D. The solid is built as the **union of `Manifold.batch_hull`
   between consecutive frames** — `batch_hull` is the closest thing manifold3d
   has to a sweep, and it gives a continuous, manifold tube that does not need
   any sliver bookkeeping. This is **p10's "per-chain swept tool" recommendation,
   committed**, and it directly kills the 1000+-sliver failure on curved loops.

3. **The corner blend is the "setback + ball patch" construction.** At a
   convex 3-chain corner: pull each chain's last frame back by a setback
   distance `s = r` (clamped to half the shortest incident chain), drop a
   spherical patch (faceted geodesic, the `Manifold.sphere` recipe) at the
   corner vertex, and let the three swept tubes hull-bridge into it. Concave
   corners use the same setback with a hemispherical *fill*. Mixed corners
   (convex + concave at the same vertex) **raise** — they need analytic
   trimming we do not have. This is the ball-corner that p10 lacked.

4. **Oversize radius is a clear, loud error — never a clamp, never a silent
   degrade** (P3). Pre-flight: a local feature-size estimate per chain edge,
   plus a corner-feasibility check. If `r > 0.5 · feature_size` at any
   sample, raise `MeshFilletInfeasible` with the offending chain + measurement.
   The validity guarantee of manifold3d is preserved; *correctness* is what we
   add on top.

5. **Phasing.** A3a chamfer (flat profile, no corner blend needed — chamfer
   tools self-bridge by planar intersection). A3b fillet on a single chain.
   A3c the setback-ball corner construction. A4 variable radius / inner-loop
   handling. **Chamfer of flat CSG ships first** — it is the cheap exact case,
   and validates the chain-sweep substrate before any of the corner work.

---

## 1. Inputs and substrate

### 1.1 What p10 derisked

`p10/NOTES.md` (verified, 5 cases, 17 tests):

- **The faceID feature-edge graph is sound and selective.** A box: 12 feature
  edges, 12 distinct faceID pairs. A bored box: two 63-edge closed loops.
  "Edge between face A and face B" = dict lookup `chains_by_pair[(A,B)]`.
- **Explicit boolean tooling beats SDF by ~1000×** (0.2 ms vs 223 ms for one
  edge; 60 vs 9572 tris). SDF also destroys faceID provenance everywhere it
  remeshes — disqualified for selective work. **Do not base the mode on SDF**
  (§6 of this doc).
- The **per-segment** explicit tool works but breaks on three shapes:
  1. **Curved/faceted feature loops** — overlapping per-segment tools emit
     1052–1720 degenerate slivers on the bored-box rim. Fix: **one swept tool
     per chain** (§3).
  2. **3-edge corners** — valid `q_min=0.001` patches, no ball corner. Fix:
     **setback + ball construction** (§4).
  3. **Oversize radius** — `r=8` on 6 mm plate stays valid but is no longer a
     fillet. Fix: **feature-size pre-flight that raises, never clamps** (§5).

### 1.2 What is already shipped under `src/build123d/mesh/`

`MeshPart` value type with batch booleans, hull, `minkowski_sum` (native in
pinned 3.x — verified `manifold3d.Manifold` exposes `hull`, `batch_hull`,
`hull_points`, `minkowski_sum`, `minkowski_difference`); `bridge.py` with
systematic per-triangle `face_id` seeding (every triangle of every input
`TopoDS_Face` gets a globally-unique id, preserved through every boolean);
`SideMap` provenance (`faceID → FaceRecord`); `recovery.py` faceID-grouped
exact B-rep reconstruction; `MeshPart.faces()` / `.analytic_faces()` /
`.faces_from(...)` selectors. Fillet/chamfer sits on top — no core changes.

### 1.3 The feature-edge graph as it stands in p10

`p10/feature_graph.py` exposes (extracted verbatim — promoted into the package
in A3a):

```python
@dataclass class FeatureEdge:
    v0, v1: int                  # vertex indices (v0 < v1)
    tri_a, tri_b: int            # adjacent triangles
    fid_a, fid_b: int            # their faceIDs (fa != fb by definition)
    normal_a, normal_b: vec3     # outward unit normals
    far_a, far_b: int            # the off-edge vertex of each triangle
    @property pair: (lo, hi)     # the chain key — unordered (fid_a, fid_b)

@dataclass class FeatureChain:
    pair: (int, int)             # the (lo, hi) faceID pair every edge separates
    verts: list[int]             # ordered vertex indices
    is_loop: bool
    edges: list[FeatureEdge]
```

Two pieces this design adds to the dataclass:

- **`convexity_class: Literal["convex", "concave", "flat", "mixed"]`** at the
  *chain* level — a chain whose edges all agree gets the strong tag; one whose
  edges disagree is `"mixed"` and the sweep splits at the sign flip (§3.4).
- **`vertex_kind: list[Literal["interior", "corner", "endpoint"]]`** — interior
  (2 chain-edges at this vertex), corner (≥3 *distinct* chain pairs at this
  vertex), or endpoint (only on open chains).

A vertex's `corner` flag is computed *globally* across chains: vertex `v` is a
corner iff it appears in **two or more chains with different `pair` values**.
This is the load-bearing definition for §4: the corner blend operates on the
set of chains that share a vertex.

---

## 2. Selection API — name feature edges without leaking mesh indices

### 2.1 What the user types

Native build123d is `part.fillet(edges, radius)` with selectors like
`.edges().filter_by(Axis.Z)`. The mesh analogue reads the same:

```python
mp = mp.fillet(mp.feature_edges().filter_by_axis(Axis.Z), radius=2.0)
mp = mp.chamfer(mp.feature_edges().between("box", "cylinder"), size=1.0)
mp = mp.fillet(mp.feature_edges().convex(), radius=1.5)
```

`mp.feature_edges()` returns a `FeatureChainSelection` — a `ShapeList`-like
collection of `FeatureChain`s tagged with their two faceIDs. It answers
queries by `FaceRecord.source` (shape name), planarity, faceID, edge axis,
convexity. **Every selector is faceID-based; no caller ever sees a triangle
index.**

### 2.2 `FeatureChainSelection` operations

| Method | Returns | Semantics |
|---|---|---|
| `.convex()` / `.concave()` / `.flat()` | `FeatureChainSelection` | filter by chain convexity_class |
| `.between(source_a, source_b)` | `FeatureChainSelection` | chains whose faceID pair straddles these two source shapes |
| `.of_face(face_id)` / `.of_source(name)` | `FeatureChainSelection` | chains touching the given face |
| `.filter_by_axis(axis)` | `FeatureChainSelection` | chains whose edges align with the given axis (within `cos_tol=0.95`) |
| `.filter_by_plane(plane)` | `FeatureChainSelection` | chains all of whose vertices lie on the plane |
| `.closed()` / `.open()` | `FeatureChainSelection` | loops vs open chains |
| `.sort_by(...)` / `.group_by(...)` | parallels `ShapeList` | for ergonomic parity |
| `len(...)`, iteration | `int`, `Iterator[FeatureChain]` | a plain collection |

### 2.3 What "edge handle" means after a boolean

A boolean can *create* feature edges (`MeshPart.box - MeshPart.cylinder`
creates a circular feature edge between the box top and the cylinder lateral).
Because faceIDs are *globally unique and are preserved through booleans*
(design §3.4, `p9` finding), the chain key (lo, hi) is stable across the
chain's lifetime. **A chain handle persists through subsequent booleans iff
both of its endpoint faceIDs survive** — this is the same persistence story as
`MeshPart.faces()` already documents.

### 2.4 Variable radius — *deferred to A4*

Native build123d supports per-edge variable radii. For mesh, deferring this is
the right call: a single radius along a chain is hard enough; a varying
profile parameter forces interpolation of the swept frame's cross-section,
which is the *next* hard layer up. A4 picks it up; A3 ships single radius
only, and the signature is `fillet(edges, radius: float)` not `radius:
float | list[float]` so adding it later is non-breaking.

---

## 3. The per-chain swept tool (the substrate)

### 3.1 Why per-chain (and not per-edge)

p10 measured: **per-segment tools on a 63-edge bore rim produce 1052–1720
degenerate slivers** (`q_min=0.000`). The tools overlap heavily on every
turn; manifold3d's boolean *stays valid* (the manifold guarantee), but emits
genuine slivers from the overlapping cuts. That is the symptom; the cause is
that two adjacent edges' prism tools share a cut volume, and the boolean's
ε-tolerance collapses the shared region into degenerate triangles.

The cure is to **never present overlapping tools to manifold3d in the first
place.** One swept solid per chain ⇒ no overlap inside the chain, by
construction. Different chains may still overlap at corners, and that is what
§4 handles separately.

### 3.2 The construction — per-vertex frames + `batch_hull` lofts

manifold3d **has no native sweep** (`research/03` §2 — the closest are
`CrossSection.extrude` with a *straight* path, and the warp callbacks).
`batch_hull(manifolds: list[Manifold]) -> Manifold` is the building block we
*do* have, and it directly fits a generalised sweep:

```
For chain C = (v0, v1, v2, ..., vn):
  for each vertex vi:
      compute a 3D frame F_i = (origin=p_i, u_axis, w_axis)
      construct a "cross-section solid" S_i:
          a degenerate-thin triangular prism whose base is the cross-section
          profile lifted into F_i's plane
  sweep_tool = union over i of batch_hull([S_i, S_{i+1}])
```

The `batch_hull` of two thin profile prisms is the **convex hull of two
adjacent cross-sections** — i.e. a lofted segment. The union of these
per-segment lofts is one continuous swept solid that smoothly tracks the
chain's curvature. Because each successive `batch_hull` *shares its boundary
prism* with the next one, the union has no overlapping interiors and produces
no slivers.

This is the recipe BOSL2's `path_sweep()` uses in OpenSCAD-land
(`BOSL2/rounding.scad`), and the recipe scad2py's Minkowski path uses
(`minkowski_impl.py` — `batch_hull` of cross-product vertex pairs). It is
proven on the same kernel we are on.

### 3.3 The per-vertex frame `F_i`

At interior vertex `v_i` (the common case, two chain-edges meet):

```
e_prev = unit(p_i - p_{i-1})       # incoming edge direction
e_next = unit(p_{i+1} - p_i)       # outgoing edge direction
tangent = unit(e_prev + e_next)    # bisector — locally smoothest
n_a = average of the chain-vertex-A-side triangle normals at v_i
n_b = average of the chain-vertex-B-side triangle normals at v_i
u_axis = unit(cross(n_a, tangent))     # in-solid tangent of face A
w_axis = unit(cross(n_b, tangent))     # in-solid tangent of face B
```

`u_axis` and `w_axis` are forced to point **into the solid** with the same
far-vertex sign check `p10/explicit.py:edge_convexity` uses (so the chamfer's
right-angle leg lies inside, not outside). At endpoints of an open chain, use
the unique edge direction as `tangent`. At chain *corners* (handled by §4) the
frame degenerates and we hand off to the corner construction.

### 3.4 The cross-section profile

Two profiles, three convexity regimes (so two-times-three combinations):

| | convex chain | concave chain |
|---|---|---|
| **chamfer (size = d)** | right triangle `(0,0)-(d,0)-(0,d)` lifted into `(u_axis, w_axis)` from the chain vertex; the prism is **subtracted** (carves a bevel). | the *same* triangle but on the *outside* — i.e. translated by `(-d, -d)` along `(-u, -w)` so the hypotenuse touches the concave edge; the prism is **unioned** (fills the bevel). |
| **fillet (size = r)** | "square minus quarter-disc" of side `r`: vertices `(0,0)` plus an arc of `n_seg` points sweeping from `(r,0)` to `(0,r)` centred at `(r,r)`. **Subtracted** to round the convex edge. | a `(r, r)`-centred quarter-disc plus its two straight legs, **unioned** to fill the concave channel. |

These are exactly the profiles p10 verified (`explicit._chamfer_profile`,
`_fillet_convex_profile`, `_fillet_profile`). What we change is **where** the
profile is sampled (per-*vertex*, not per-*edge*) and **how** the profile is
extruded into 3D (a thin prism, not an open polygon).

For a **mixed chain** (some convex edges and some concave) the chain splits at
the sign flip into one convex sub-chain and one concave sub-chain; each builds
its own swept tool. A near-flat edge (`|convexity| < tol_flat = 1e-6`) is
elided — `p10` already handles this with a continue.

### 3.5 The thin-prism degeneracy and how `batch_hull` survives it

A degenerate-thin prism (height ≈ ε) imported as `manifold3d.Manifold` carries
`precision()` ε-tolerance; manifold3d silently collapses triangles below ε.
`p10/explicit._sweep_tool` already deals with this by overshooting along the
edge — we keep the overshoot trick but apply it to the *thin* direction
(perpendicular to the profile plane): give each cross-section prism a fixed
thickness `2·precision` along `tangent`. The hull of two such prisms is then a
proper 3D wedge.

Concretely: each `S_i` is built as `extrude(profile, height=2·precision,
center=True)` and then placed at `p_i` rotated to align with `F_i`. The hull
of two consecutive `S_i`, `S_{i+1}` is the lofted segment between their
midplanes, which is exactly what we want.

### 3.6 Closed loops vs open chains vs end-caps

- **Closed loop:** indices wrap; the union of segment hulls is naturally
  closed, no end-cap needed.
- **Open chain (interior segment of a face):** at each endpoint, extend the
  last frame by a half-edge worth of overshoot along the chain tangent, so
  the tool cleanly crosses any face boundaries it ends on. The overshoot
  amount is `max(1.5 · d, 1.5 · r, 0.1 · bbox_diag)`.
- **End-cap geometry:** the terminal `S_i` itself *is* the cap (it is a 3D
  prism). No special cap construction needed.

### 3.7 Applying the tool — cut first, then add (p10 finding, kept)

A chain whose convex sub-chain produces a cut tool *and* whose concave
sub-chain produces an add tool **must apply cut first, then add** (p10 fix
#3 — otherwise an overshooting cut can slice through just-added fill and
isolate a fragment, seen on the L-shape going from 3 bodies → 1). At the
selection level:

```python
all_cut_tools = []
all_add_tools = []
for chain in selected_chains:
    tool = build_swept_tool(chain)
    if chain.convexity_class == "convex":
        all_cut_tools.append(tool)
    elif chain.convexity_class == "concave":
        all_add_tools.append(tool)
    else:  # mixed — sub-chains separate
        cut, add = build_swept_subtools(chain)
        all_cut_tools.extend(cut)
        all_add_tools.extend(add)

# one batch_boolean per direction
combined_cut = m3d.Manifold.batch_boolean(all_cut_tools, m3d.OpType.Add)
result = base.manifold - combined_cut
combined_add = m3d.Manifold.batch_boolean(all_add_tools, m3d.OpType.Add)
result = result + combined_add
```

(p10 fix #4: one batch boolean, not one-per-edge — kept; one-per-chain *still*
emits per-chain tools, the batch is then over those chain-tools.)

### 3.8 The triangle-quality story for the swept tool

p10 single-edge box fillet: 60 tris, `q_min=0.027`, `q_mean=0.198`. A swept
tool's faceting *legitimately* produces long thin strip triangles along the
fillet roll (this is what an OpenSCAD-faceted round looks like). These are
**not** defects — `degen` (area < 0.1% of mean) is the honest count, and on
the curved-loop case the swept construction should drop `degen` from
1052/1720 → 0 by removing the per-segment overlap. **The acceptance
criterion** for A3 is `degen == 0` on all five p10 cases including the bored
box. p10 promised; this section commits to delivering.

---

## 4. The corner blend — the design's centrepiece

### 4.1 Why the corner is hard

At a vertex where ≥3 different chain-pairs meet, three (or more) per-chain
sweeps converge. Three failure modes if you do nothing:

- **Gap.** Each chain ends at the corner vertex; its swept tool stops there.
  The three tools cleanly cut three separate cylinders, leaving the sharp
  vertex of the original solid *untouched* — a tiny pyramidal point sticking
  out into the middle of where the ball-corner should be. (p10 Case 3:
  `q_min=0.001` thin patch.)
- **Self-intersection.** Extend each chain past the vertex and the tools
  overlap; the boolean stays valid but emits the same kind of slivers as the
  curved-loop case.
- **Wrong topology.** A chain ending exactly at the vertex with no
  cross-tool blending leaves the round abruptly meeting itself at a knife-edge
  ridge — the antithesis of a "fillet".

### 4.2 The classical solution — Setback + Vertex Blend

**ACIS / Spatial** call this "setback vertex blending"; **Fusion 360** calls
it "Setback corner" (as opposed to "Rolling Ball"). The construction
(Sederberg/Pratt et al., see `cad-journal` and the Spatial Blending Component
PDF):

1. **Setback.** Each chain's tool stops short of the corner vertex by a
   setback distance `s` (instead of going all the way to the vertex). The
   three tools terminate on three "setback rings" around the vertex.
2. **Vertex patch.** A new surface fills the n-sided hole bounded by the
   setback rings — for a 3-edge convex corner with equal radii, **this is
   exactly an octant of a sphere of radius `r` centred at the corner vertex's
   *inset* position** (offset along the inward normal sum by `r`).
3. **Cut as one tool.** The setback tubes + the sphere patch union into one
   "corner tool" with a smooth `C¹` boundary against the three setback rings.

For a chamfer the "vertex patch" is a flat triangle (or n-gon) connecting the
setback rings — also classical.

This is the standard CAD corner. We adopt it because:

- It is implementable with `Manifold.sphere` + `batch_hull` (so no new
  primitives needed).
- It generalises to n ≥ 3 chains at one vertex (the corner of a hexagonal
  pocket has 4 chains meeting, etc.) — see §4.6.
- It maps cleanly to `BRepFilletAPI`'s own corner construction, so a future
  mesh→BREP recovery of a corner can target the same topology.

The alternative — "rolling ball" corner (no setback, the cylinders just bend
into the sphere tangentially) — produces visibly better G2 corners but
requires real tangent-continuous blending we cannot do on a faceted mesh. The
ACIS literature explicitly says rolling-ball is *G1* on mesh meta-geometry
anyway. **Setback is the right faceted choice.**

### 4.3 The construction, step by step

Given a vertex `v` that hosts chains `C1, C2, ..., Ck` (each `Ci` has its own
`pair` and convexity), with radius `r` (or chamfer size `d`):

```
1. Classify the corner.
   - all chains convex     → "convex corner"     → cut tool
   - all chains concave    → "concave corner"    → add tool
   - mixed convex/concave  → RAISE  (§4.5)

2. Compute the setback distance s.
   s = r  (for a fillet)
   s = d  (for a chamfer)
   clamp:   s = min(s, 0.45 · length(Ci))  for every Ci
            (never consume more than 45% of a chain length to the corner)
   if  s < r·0.5  AND fillet:
       — chain too short to fit the corner ball;
       RAISE  MeshFilletInfeasible (§5).

3. Cut each chain Ci's swept tool short by s at vertex v.
   When building the chain tool (§3), skip the last few frames whose
   cumulative chain-distance from v is < s, then insert a "setback ring"
   frame at exactly the cumulative-distance-s point. The chain tool now
   ends on that ring instead of at v.

4. Compute the inset corner centre.
   inward_normal_sum = unit( -(sum of incident face normals at v) )
   c = v + r · inward_normal_sum    (for a convex corner)
   c = v - r · inward_normal_sum    (for a concave corner)

5. Build the spherical (or flat) vertex patch.
   - convex fillet: a faceted sphere  Manifold.sphere(r, circular_segments=n)
     centred at c.
     [n = chain n_seg + 4, picks up the chain facetting]
   - convex chamfer: a flat polyhedron whose top face is the n-gon connecting
     the setback ring midpoints, and whose sides drop back to the original
     solid (a generalised tetrahedron for k=3).
   - concave fillet: same sphere but unioned (fill the inside of the corner).
   - concave chamfer: a flat fill polyhedron.

6. Union the per-chain swept tools (already setback by s) WITH the vertex
   patch into ONE combined corner tool per corner. Then add this combined
   corner tool to the per-direction tool list (cut or add).
```

The setback ring is two cross-section frames stacked normal to the chain
tangent at distance `s` from `v` — built by the same per-vertex frame
construction in §3.3. **The sphere patch must overlap the setback ring
prisms slightly**, by the `2·precision` thickness we already use for prism
thickness — this is the manifold-tolerance-friendly equivalent of
G1-tangency.

### 4.4 Why a sphere (and not a Bezier-corner)

ACIS provides a `BLN_CRNR_SMOOTH_BIQUAD` continuous-curvature corner
(BSpline biquadratic patch). On a mesh we cannot represent biquadratic
surfaces; the only "ball" we have is `Manifold.sphere(r, segments)`, a
geodesic-refined octahedron with `4·segments²` triangles. At chain
`n_seg=8` (p10 default), `4·64 = 256` triangles is plenty for a corner
patch indistinguishable from a real ball. **It is `G0`, not `G1`** —
honest, but indistinguishable at print scale, which is the mode's contract.

### 4.5 Mixed convex/concave corners — raise, do not blend

A vertex where one incident chain is convex and another is concave is a
*saddle* of the surface. Classical CAD treatment for these is non-trivial
(ACIS calls it a "compound vertex blend" and requires both rolling-ball
radii to agree). On a mesh:

- The setback + sphere construction has no consistent "centre c" (one
  chain wants the centre inset, the other wants it outset).
- A naive "do nothing" leaves the corner exactly as p10 leaves it — valid
  but a thin patch.

**Decision:** detect the mixed case and **raise `MeshFilletInfeasible`**
with a message that names the offending vertex and chains. The user can
work around by filleting the convex and concave subsets in *separate calls*
— each call has internally consistent corners. This honours P3 and matches
what the BOSL2 library does (it requires the user to flag each edge's
"rounding" direction; mixed verges raise).

### 4.6 k ≥ 4 chains at one vertex (T-junctions, hex corners)

The construction in §4.3 already generalises: `k=4` is a square corner where
4 setback rings bound the sphere octant (well, hemisphere quadrant). The
inward-normal-sum offset still places the centre correctly. We **test up to
k=6** (a hexagonal pocket corner) in A3c. Beyond k=6 we raise with the
"degenerate corner" message — n-sided fillet patches above hex are out of
scope.

### 4.7 What happens in the all-edges-of-a-box fillet (p10 Case 1)

The p10 box has 8 corners, each with `k=3` chains. Without the setback +
ball blend, the result is `q_min=0.050, q_mean=0.098, degen=0` — valid but
the corners are 3-way intersecting thin patches.

After the blend: predicted `tris ≈ 12 chains · n_seg(8) + 8 corners · 256
+ tube hull strips ≈ 96 + 2048 + 200 ≈ 2300`. (vs p10's `204`). The triangle
count goes up by ~10×, but the corners are *actually* round.

This is the acceptance-criterion shift the design takes on: the *quality*
went from "valid" to "valid + looks like a real fillet." Triangle counts
multiply by ~10 from p10's prototype; that is the cost of correctness.

---

## 5. Oversize radius — P3 in action

### 5.1 What "oversize" means

Three independent feasibility constraints; failing *any* triggers the same
`MeshFilletInfeasible`:

| Constraint | Test | When |
|---|---|---|
| **A — Half-thickness rule.** | At every chain vertex `v`, sample the *opposing-face distance*: shoot a ray from `v` along `-1 · n_a` (the inward normal of face A) and along `-1 · n_b`. The minimum hit distance must be `> 2·r`. | Per chain, per vertex. |
| **B — Chain-length rule.** | A chain of arc length `L` cannot host a corner setback at *both* endpoints if `L < 2·s + ε`. Equivalent: each interior segment of the chain must satisfy `length(segment) ≥ r/2` somewhere (else the cross-section frame folds). | Per chain. |
| **C — Adjacent-chain rule.** | Two chains touching the same vertex must individually have an unblended length `≥ s` after each chain's other-endpoint setback is also accounted for. | Per corner. |

A and B are p10's observed failure mode: a 6 mm plate with `r=8` violates A
(opposing face distance = 6 mm, needed > 16 mm). p10 noted: *"both
approaches stay valid (manifold3d guarantees it) but the result is no
longer a fillet."* This design promotes that observation to **the loud
error**.

### 5.2 The local feature-size estimate

For each chain edge `e` with vertices `v0, v1, p_mid = (v0+v1)/2`:

```
opposing_face_dist(v) = min over inward rays of
    distance to first surface hit  (via Manifold.ray_cast if available;
                                    else via a bbox-bounded BVH)
feature_size(chain) = min over vertices and midpoints of
                     opposing_face_dist(v)
```

**Implementation choice:** `manifold3d` 3.x exposes `min_gap(other, search_length)`
between two manifolds (`research/03` §9 — verified). For self-distance we
build the chain's "inward swept cone" (a few rays of length `2·r`) as a thin
solid, call `base.min_gap(cone, search=3·r)`, and take that as
`opposing_face_dist`. Simple, fast, exact-mesh.

### 5.3 Clamp vs raise — the decision

`p10` flagged this directly: *"validity is not correctness. A real
implementation must detect `r > local_thickness` and refuse or clamp."*

**We raise, we never clamp.** Reasons:

1. **P3.** A clamp is a silent degradation: the user asked for `r=8`, the
   API returned a `r=2.9` result with no signal. That is the *exact* outcome
   §6.5 of the design forbids ("the one outcome to forbid is a silent wrong
   answer").
2. **A clamp is locally consistent but globally wrong.** Clamping per
   chain to `r=2.9` while the user expected `r=8` means a fillet that looks
   different on different edges, breaking any downstream geometric reasoning
   (parametric drives, downstream fillets, dimensional tolerances).
3. **The error message is actionable.** The exception names the offending
   chain, the measured `feature_size`, and the requested `r`. The user can
   either pick a smaller `r`, exclude the offending chain from the
   selection, or split the part. None of these is possible from a silent
   clamp.

The exception type:

```python
class MeshFilletInfeasible(ValueError):
    """Raised when a fillet/chamfer cannot be applied to the requested edges.

    Attributes:
        chains: list[FeatureChain]      — the chains that violate the test
        constraint: str                  — "half-thickness" | "chain-length"
                                          | "adjacent-chain" | "mixed-corner"
        requested: float                 — the requested radius / size
        measured: float                  — the measured feature size
    """
```

### 5.4 Opt-in clamp (well-named) — *not* shipped in A3

A power-user `MeshPart.fillet(edges, r, *, on_infeasible="raise" | "clamp"
| "skip")` option is a *non-default* future addition. It would default to
`"raise"`; setting `"clamp"` clamps the radius to `0.45 · feature_size` and
records the clamping in a returned `FilletReport`. We document it as future
work and **do not ship it in A3 / A4** — the clamp option is exactly the
"approximate mode hides exactness loss" failure we want to keep out of the
default path.

---

## 6. Curved / faceted feature loops

### 6.1 What "curved" means in mesh space

p10 Case 5: a box with a cylindrical bore. The bore lateral has been
tessellated to ~32 facets; the rim where it meets the box top is a
**63-edge closed loop** between two faceIDs (bore lateral, box top). Each of
the 63 edges is a *straight* segment; the loop *as a whole* is a faceted
circle.

This is the common case — every hole rim, every fillet of an already-curved
edge, every chamfer of a sphere's equator. **The construction in §3.2
handles it natively**:

- The per-vertex frame's `tangent = unit(e_prev + e_next)` averages the two
  incoming edge directions, so the frame turns continuously along the loop.
- Consecutive `batch_hull` of profile prisms produces lofted segments that
  *share their boundary cross-section* — no overlap, no sliver.
- For a closed loop, the wrap is automatic: the last segment hulls
  `S_{n-1}` with `S_0`.

### 6.2 The faceting story along the loop

The user's `n_seg` for the *cross-section* and the mesh's `~32` segments
*along* the loop are different parameters:

- The **profile facet count** `n_seg` controls the arc resolution of the
  cross-section (how many facets approximate the quarter-disc).
- The **path facet count** is inherited from the mesh: 63 segments = 63
  cross-section frames. We do not subdivide the path.

p10's bored box: 63-edge loop, `n_seg=6`, prediction: `63 · 6 = 378` tube
strip triangles + 63 cap pairs + ε. Versus p10's measured `10262` tris with
sliver pollution. Even with the new geometry being heavier per-vertex, the
total should still drop by ~10× because we eliminate the sliver tail.

### 6.3 Two pathological sub-cases

- **Very tight turn on the loop.** If a single segment of the loop turns
  more than ~75° (e.g. a chamfered cube whose vertices we then mesh-fillet),
  the bisector frame collapses (`e_prev ≈ -e_next`). Detection: if
  `dot(e_prev, e_next) < cos(75°)`, **the vertex is treated as a corner**
  even if it has only k=2 chains — the §4 setback construction applies.
  This is what gives mesh fillets graceful behaviour on chamfered cubes:
  the per-vertex frame switches to the setback + sphere recipe at sharp
  turns.

- **Non-planar feature loop.** A space-curve feature edge (e.g. the loop
  where a cylinder pierces a sphere) is *not* planar. The per-vertex frame
  construction handles this — the frame rotates in 3D as it walks the loop
  (Frenet-like, but built from face normals not curve curvature). Test
  case in A3c: a sphere pierced by a cylinder, fillet the intersection
  loop.

---

## 7. Mixed / non-90° dihedral angles

### 7.1 The problem

p10's profiles assume a 90° dihedral: the right triangle / quarter-disc has
its legs at right angles. A real CSG model has dihedrals from ~10° (a thin
wedge) through 90° (boxes, the common case) to ~170° (a near-flat edge,
where the round looks like a hairline).

### 7.2 The fix — angle-aware profile

The cross-section profile generator takes the local dihedral as a third
argument and scales:

```python
def chamfer_profile(d: float, dihedral_radians: float) -> ndarray[(3, 2)]:
    # leg lengths are unchanged at d; the inside angle is dihedral
    half = dihedral_radians / 2
    return [
        (0.0, 0.0),
        ( d * sin(half),  d * cos(half)),    # along face A
        (-d * sin(half),  d * cos(half)),    # along face B (mirrored)
    ]   # generally non-right-triangle

def fillet_profile(r: float, dihedral_radians: float, n_seg: int):
    # the rolled-ball arc: tangent to both faces, radius r
    # the centre is at perpendicular distance r/sin(half) from the edge,
    # bisecting the angle.
    half = dihedral_radians / 2
    offset = r / sin(half)
    # arc from face A tangent point to face B tangent point, swept through
    # the OUTSIDE of the dihedral (convex) or INSIDE (concave).
    ...
```

For a 90° edge the formulas reduce to p10's `_chamfer_profile` /
`_fillet_profile` exactly — we do not regress on the verified case.

### 7.3 Sampling the dihedral per-vertex

We sample `dihedral_radians` at each chain vertex by:

```
cos_dihedral = dot(n_a, n_b)
dihedral = arccos(clamp(cos_dihedral, -1, 1))
# but: convex chains use π − dihedral_in_solid, concave use π + dihedral.
# the signed-far-vertex test from p10 already gives the convexity sign;
# reuse it.
```

Within a chain the dihedral may vary (a chamfered cube's chamfer edges have
a 135° dihedral on one side, 135° on the other; *but* a faceted-revolve
edge can have 175° at the equator and 91° at a pole). The profile is built
per-vertex, so this variation is automatic.

### 7.4 Acute dihedrals — the small-angle warning

A dihedral below `30°` is *both* a feature-size concern (the fillet is
likely larger than the local feature) and a profile concern (the
right-triangle chamfer becomes a needle). Treatment:

- Run the §5 feature-size test first; if a chain has any vertex with
  dihedral < 30°, *and* `r > 0.1 · edge_length`, raise.
- Otherwise build the angle-aware profile and proceed.

This integrates the "acute dihedral" case naturally into the existing
oversize-radius pre-flight.

---

## 8. The shipped API

### 8.1 Method form (Option A from `research/12`)

We follow `research/12` §5 and ship `fillet` / `chamfer` as **methods on
`MeshPart`**. No global context manager, no implicit verb-rebinding. The
*type* is the honesty signal — a `MeshPart.fillet` cannot be confused with
a `Part.fillet` because the receiver tells you it is mesh.

```python
class MeshPart:
    def feature_edges(self) -> FeatureChainSelection: ...

    def fillet(
        self,
        edges: FeatureChainSelection | FeatureChain | Iterable[FeatureChain],
        radius: float,
        *,
        n_seg: int = 8,
        on_infeasible: Literal["raise", "skip"] = "raise",
    ) -> MeshPart: ...

    def chamfer(
        self,
        edges: FeatureChainSelection | FeatureChain | Iterable[FeatureChain],
        size: float,
        *,
        on_infeasible: Literal["raise", "skip"] = "raise",
    ) -> MeshPart: ...
```

Free-function equivalents `mesh_fillet(mp, edges, r)` / `mesh_chamfer(mp,
edges, d)` are also added, mirroring the `mesh_fuse` / `mesh_cut` free
functions already in the module — for users who prefer that style and for
the scad2py backend.

### 8.2 What `on_infeasible="skip"` does

`"skip"`: chains that fail the feasibility pre-flight are silently dropped
from the operation; the result is still a valid `MeshPart` but only the
feasible chains are filleted. **A `FilletReport` is attached to the
returned MeshPart** (`mp._last_fillet_report`) and is logged at WARNING
level — so it is *not* silent in P3's sense. Default is `"raise"` for
exactly the "never silently mis-answer" reason.

### 8.3 What `.fillet()` *returns*

A `MeshPart` whose side-map merges the input's, with one **important new
entry per fillet chain**: the swept-tool surface gets its own freshly
reserved `face_id` so that `to_solid()` recovers it as a *faceted patch*
identifiable in downstream selectors. Specifically:

- The swept tool's outer rolled surface registers as a *single* curved
  region with one new faceID and a `FaceRecord(source="fillet",
  surface_kind="rolled_arc", parent_chain=(fa, fb), radius=r)`.
- The setback + sphere corner patch registers similarly with
  `surface_kind="spherical_corner"`.
- The chamfer tool's flat bevel registers as a **planar** face with a real
  `Geom_Plane` in its FaceRecord — meaning a *chamfered* mesh body still
  recovers exact analytic planar bevels via the existing `recovery.py`.
  **This is why chamfer ships first** — its result is a *planar* CSG, and
  recovers all-analytic.

### 8.4 Interaction with `MeshPart.faces()` / `.analytic_faces()`

- `MeshPart.faces()` after a chamfer: returns analytic planar faces
  (including the new bevel planes), exactly as a native chamfer would.
- `MeshPart.faces()` after a fillet: returns analytic planar faces for
  every untouched region + **faceted patches with `surface_kind="rolled_arc"`**
  for the rolled fillet. The faceted patch is honest about not being analytic.
- `MeshPart.analytic_faces()` after a fillet: **raises** (the curved roll
  cannot be returned as analytic). After a chamfer: returns successfully.

This is consistent with what `mesh_part.py:686:analytic_faces` already
documents: "the strict counterpart of `faces()` and the enforcement point
of the design's P3 contract."

---

## 9. Implementation plan & phasing

`research/12` placed mesh chamfer at A3 and mesh fillet at A4. We
sub-divide and re-order:

### A3a — Chamfer of flat CSG (single chain, no corners) — *ships first*

- Promote `p10/feature_graph.py` into `src/build123d/mesh/feature_edges.py`.
- Add `FeatureChainSelection`, `MeshPart.feature_edges()`, the chain-based
  selectors.
- Implement the §3 swept tool for **chamfer profiles only**.
- `MeshPart.chamfer(edges, size)` works for single chains and multi-chain
  selections that share *no corners*.
- Tests: p10 Cases 1 (chamfer 1 edge), 2 (L-shape chamfer all), 5 (chamfer
  bore-rim loop). Acceptance: every case returns 1 body, `degen == 0`,
  `volume` within 0.01% of analytic expectation (chamfer of flat is bit-exact).
- **Risk: low.** This is essentially p10's working chamfer + the swept-tool
  substrate that p10 recommended but did not build. The corner-fragility
  vanishes for chamfer because chamfer corners are *planar intersections*
  of three half-spaces — manifold3d handles those bit-exactly.
- **Estimate: ~2 weeks.**

### A3b — Fillet of a single chain (no corners) — ships after A3a

- Add the fillet profile + `MeshPart.fillet(edges, radius)` for selections
  whose chains do not share a corner vertex.
- The feature-size pre-flight from §5 lands here.
- Tests: p10 Cases 1 (fillet 1 edge), 2 (fillet single L convex edge,
  single L concave edge), 5 (fillet bore-rim loop). Acceptance: `degen ==
  0` everywhere (the curved-loop fix is the headline measurement);
  oversize raises cleanly.
- **Risk: low–medium.** The angle-aware profile (§7) is new; the swept-tool
  substrate is reused from A3a.
- **Estimate: ~2 weeks.**

### A3c — The corner blend (setback + ball) — ships third

- Build the §4 corner construction. Sphere patch (convex/concave),
  flat-polyhedron chamfer patch, setback ring.
- `MeshPart.fillet` and `.chamfer` now accept selections whose chains share
  vertices.
- Tests: p10 Case 1 (fillet ALL 12 edges of a box — the 8 corner case),
  p10 Case 3 (3-edge-corner fillet, chamfer), plus the new hexagonal pocket
  corner (`k=4` test).
- Acceptance: `degen == 0` *plus* `q_min ≥ 0.1` (no thin patches anywhere).
- **Risk: medium.** Mixed convex/concave corners are explicitly out of
  scope and raise (§4.5). The k > 6 corners also raise.
- **Estimate: ~3–4 weeks.**

### A4 — Polish

- `on_infeasible="skip"` mode + `FilletReport`.
- Variable radius (per-chain `radius` list).
- Helper selectors (`.between(...)`, `.of_face(...)`).
- scad2py wiring (a `mesh_fillet` free function that scad2py's `polyround`
  port can call).
- Edge-case tests: non-planar feature loops, tight-turn dihedrals,
  acute-dihedral pre-flight.

### Non-goals (out of scope for the entire A3+A4 work)

- **NG_F1:** Tangent-continuous (G1) corner blends. Setback + ball is G0.
- **NG_F2:** Fillet of curved-face regions. A mesh fillet between two
  faceted curved faces (e.g. fillet at the seam between a sphere and a
  cylinder mesh-imported from STL) is buildable but the result is
  cosmetically poor *and* the faceID story degrades. Raise with "curved
  source face" for now.
- **NG_F3:** Asymmetric chamfer (different distance per face). The native
  build123d API supports this; the mesh API does not. Future.
- **NG_F4:** Mixed convex/concave corners (§4.5). Raise.
- **NG_F5:** k > 6 corners. Raise.
- **NG_F6:** "Soften everything" via `Manifold.smooth_out` — this is a
  whole-model operation and is the subject of Phase A1 in `research/12`,
  not this design.

---

## 10. Risk register

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **`batch_hull` slow on many small operands.** A box-with-all-edges-filleted has 12 chains, each with ~8 frames → 84 hull calls per chain · 12 = 1000+ small hulls. | Medium | Medium (perf) | manifold3d's `batch_hull(list)` resolves N hulls in one pass; verify on the box-all-edges case it stays < 100 ms per chain. If not, fall back to a hand-rolled vertex-list hull (`hull_points(all_vertices_of_S_i+S_{i+1})`) — also native. |
| R2 | **Per-vertex frame instability at sharp turns.** Bisector frame is ill-defined when `e_prev ≈ -e_next`. | Medium | High (geometric failure) | §6.3 promotes sharp turns to corners and routes them through §4. Detection threshold `dot(e_prev, e_next) < cos(75°)`. |
| R3 | **Spherical corner patch overlap with neighbours.** A k=3 corner's sphere may extend past the setback ring and into the adjacent face. | Medium | Medium | Setback distance `s = r` is sized exactly to prevent this for convex corners; for k > 3 we expand `s = r · k/3` (more setback for more chains). |
| R4 | **Recovery interaction.** `recover_brep` may not handle the new "rolled_arc" / "spherical_corner" `FaceRecord` kinds gracefully — they are curved-without-analytic-equation. | High | Low | Document them as curved → faceted patches in `recovery.py`. `analytic_faces()` already raises on any curved region; the new kinds inherit that contract. Add explicit unit tests. |
| R5 | **Feature-size estimate too expensive.** `min_gap` between the chain's inward cone and the body costs `O(n_tri)` per chain. On a 100 k-triangle body with 50 chains this is 5 s. | Medium | Medium (perf) | Build the inward-cone solid once per chain, batch the `min_gap` calls. If still too slow, add an opt-out `feasibility="strict" | "fast" | "skip"` knob — but **default stays `"strict"`** per P3. |
| R6 | **Triangle-count blowup at corners.** 8-corner box-all-edges fillet went from p10's 204 tris to ~2300 tris (10× as predicted). On a part with hundreds of corners this scales badly. | Medium | Low (acceptable for the use case) | The mode is honest about faceted geometry. Document `n_seg` as the tunable; user reduces it for preview, raises it for export. |
| R7 | **Non-planar feature loop tangent twisting.** Walking a 3D space curve, the per-vertex frame may twist 360° (the classic Frenet-frame problem). | Low | Medium | We never use a Frenet frame — we use *face normals*, which are physically meaningful and do not twist on a closed loop. Sanity check: at the end of a closed loop, the first frame and the "would-be" continuation frame must agree to within ε. |
| R8 | **`Manifold.sphere` corner patch coplanarity drift.** A faceted sphere centred at `c` does not exactly tangent the swept tube on the setback ring; small gaps possible. | High | Low (manifold guarantee absorbs it) | Build the corner tool as `sphere ∪ tube_segments` and rely on manifold3d's boolean to weld the seam. The 2·precision prism thickness from §3.5 already provides the overlap. |
| R9 | **Mixed convex/concave corners common in real models.** Raising on them may be a frequent user-facing failure. | Medium | Medium (UX) | The error message explicitly suggests "fillet the convex edges and concave edges in separate calls" — the workaround is one-line, and the result is geometrically correct. |
| R10 | **Operation ordering with downstream recovery.** A filleted `MeshPart` baked to a Solid then filleted again *with `BRepFilletAPI`* — does the second fillet pick up the rolled-arc face? | Low | Low | Out of scope for A3+A4. `recovery.py` returns the rolled arc as a faceted patch; native `BRepFilletAPI` requires analytic adjacencies and will refuse it — which is correct per P3. |

---

## 11. The test matrix

A3 acceptance lives in `tests/mesh/test_fillet.py`. The matrix
deliberately mirrors p10's 5 cases plus four new ones for the corner /
oversize work.

| # | Case | Acceptance |
|---|---|---|
| T1 | Box, chamfer 1 edge (`d=4`). | 1 body, `degen=0`, `volume = 7840.000` bit-exact, all faces analytic via `to_solid()`. |
| T2 | Box, fillet 1 edge (`r=4`, `n_seg=12`). | 1 body, `degen=0`, `volume` within 0.05% of analytic `8000 - r² (1-π/4) · L`. |
| T3 | Box, fillet ALL 12 edges (`r=3`). | 1 body, `degen=0`, **`q_min ≥ 0.1`** (corner blend present), 8 sphere-corner faceIDs in side_map. |
| T4 | L-shape, chamfer convex + concave edges. | 1 body, `degen=0`, volume matches signed-tool-volume sum, `to_solid().faces()` returns analytic planes for the bevels. |
| T5 | L-shape, fillet convex + concave. | 1 body, `degen=0`, fillet faces tagged `"rolled_arc"` in side_map. |
| T6 | 3-edge corner, fillet (`r=4`). | 1 body, `degen=0`, **`q_min ≥ 0.15`** (the corner-blend headline metric — up from p10's 0.001), one `"spherical_corner"` face in side_map. |
| T7 | 3-edge corner, chamfer (`d=4`). | 1 body, `degen=0`, the corner triangle is a single analytic plane in `to_solid()`. |
| T8 | Thin plate (6 mm), fillet (`r=8`) on a top edge. | **`MeshFilletInfeasible` raised**, with constraint="half-thickness", measured=6.0, requested=8.0. |
| T9 | Bored box, fillet bore-rim loop (`r=2`, `n_seg=6`). | 1 body, **`degen=0`** (the curved-loop headline — down from p10's 1720), volume within 0.1% of analytic. |
| T10 | Bored box, chamfer bore-rim loop (`d=1.5`). | 1 body, `degen=0`, bevel face is a single faceted ring of `~63 · 2 = 126` analytic-plane facets. |
| T11 | Hexagonal pocket, fillet all 12 edges of the hex pocket (`k=4` corners). | 1 body, `degen=0`, 6 spherical-corner faces. |
| T12 | Cylinder pierces sphere, fillet the intersection loop. | RAISES `MeshFilletInfeasible` constraint="curved-source-face" (NG_F2). |
| T13 | Convex+concave edges meeting at one vertex (an L with a chamfered top corner), fillet both. | RAISES `MeshFilletInfeasible` constraint="mixed-corner", `chains=[the two offenders]`. |
| T14 | `on_infeasible="skip"` on T8's input. | Returns the unmodified MeshPart, logs WARNING, `FilletReport.skipped_chains` non-empty. |

T9 is the most load-bearing test for the engineering: a 10× reduction in
triangle count and a `1720 → 0` reduction in degenerate slivers is the
single numerical proof that the per-chain swept tool fixes the curved-loop
failure p10 identified.

T3 and T6 are the load-bearing tests for the corner work: `q_min` from
`0.001` to `≥ 0.15` quantifies the "real ball corner" promise.

T8 and T14 are the load-bearing tests for P3 compliance: raise by default,
opt-in skip with a report.

---

## 12. Honest scope — limits we keep

1. **G0 corners.** Setback + faceted sphere — visually a ball at print scale,
   tangent-discontinuous at the setback ring. Real G1 needs analytic surfaces
   we do not have.
2. **Mixed corners raise** with a one-line workaround in the error message
   ("fillet convex and concave subsets separately").
3. **Curved-face fillets raise.** Defer (faceID provenance leaks into the
   rolled face when both source faces are already faceted curves).
4. **No analytic recovery of the rolled face.** Chamfer's flat bevel
   recovers analytic via existing `recovery.py`; fillet's roll stays
   faceted. `analytic_faces()` raises on the rolled face — same contract
   as any other curved region.
5. **The exact path stays gold.** For all-planar flat CSG, `MeshPart →
   to_solid() → BRepFilletAPI.fillet()` remains the exact answer (P7).
   Mesh fillet is for the *faceted* path.

Framing: a `MeshPart.fillet` result is what OpenSCAD would give you, but
selectively, with faceID provenance maintained, corners actually round, and
oversize radii raising loudly instead of silently producing garbage.

---

## 13. Open questions for the implementation agent

1. **`min_gap` performance.** §5.2 needs ~50 `min_gap` calls on a
   100k-triangle model. Measure before A3b ships; fallback is a hand-rolled
   BVH (numpy + `to_arrays`).
2. **`n_seg` for the corner sphere.** Visually calibrate on T3 / T6.
3. **`Manifold.smooth_out(min_sharp_angle=120°)` as a polish pass.** Would
   smooth the intra-roll faceting without touching chamfer/setback edges
   (which are sharper than 120°). Experiment in A4; probably defer.
4. **`FilletReport` return shape.** Method form attaches privately; free
   function `mesh_fillet(...) -> (MeshPart, FilletReport)` returns the tuple.
   Decided in A4.

---

## 14. Bottom line

p10 derisked the keystone. Three failure modes remained; each gets one
decision: per-chain swept tool (kills slivers), setback + faceted-sphere
vertex patch (real ball corners), pre-flight feature-size check that raises
(P3, never silently degrade). Ship chamfer-of-flat-CSG first (A3a, bit-exact
substrate validation), single-chain fillet next (A3b, curved-loop fix), then
the corner blend (A3c, the headline quality jump); polish in A4. Three
non-goals stay explicit: G1 corners, curved-face fillets, k > 6 corners.
What the user gets: a faceted fillet/chamfer that looks like a real fillet,
never silently produces garbage, and remains a `MeshPart` that composes with
every other operation already shipped.

---

### Sources

- p10 prototype + measurements:
  `/Users/ochafik/github/build123d/ddocs/prototypes/p10_mesh_fillet/NOTES.md`
- manifold3d 3.x API surface:
  `/Users/ochafik/github/build123d/ddocs/research/03-manifold3d-deep-dive.md`
- Approximation-mode framing:
  `/Users/ochafik/github/build123d/ddocs/research/12-mesh-approximation-mode.md`
- P3 / P7 / faceID feature-edge graph:
  `/Users/ochafik/github/build123d/ddocs/design/design-manifold-in-build123d.md`
- BOSL2 rounding library (path_sweep, rounded_prism, corner_mask):
  <https://github.com/BelfrySCAD/BOSL2/wiki/rounding.scad>
- BOSL2 tutorial — Rounding the Cube:
  <https://github.com/BelfrySCAD/BOSL2/wiki/Tutorial-Rounding_the_Cube>
- Spatial ACIS Blending Component (setback vertex blending):
  <http://www-isl.ece.arizona.edu/ACIS-docs/PDF/BLND/01CMP.PDF>
- Sederberg et al., "Geometric construction for setback vertex blending":
  <https://www.sciencedirect.com/science/article/abs/pii/S001044859600070X>
- Fusion 360 rolling-ball vs setback corner option:
  <https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/FilletFeature_isRollingBallCorner.htm>
- Pratt, "Fillet and surface intersections defined by rolling balls":
  <https://www.sciencedirect.com/science/article/abs/pii/016783969290016I>
- Shapr3D corner blending (rolling ball / setback options):
  <https://www.linkedin.com/pulse/shapr3ds-advanced-fillet-options-behind-scenes-istv%C3%A1n-kov%C3%A1cs>
