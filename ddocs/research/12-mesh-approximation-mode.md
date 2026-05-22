# 12 — A mesh approximation mode for build123d's manifold backend

> **Research / exploration note.** Not a committed design. It asks one question:
> *the manifold backend (`ddocs/design/design-manifold-in-build123d.md`) does
> fast booleans + faceID-seeded exact B-rep recovery — could it be extended so
> that **most BREP-only operations** (fillet, chamfer, loft, sweep, offset/shell,
> draft, primitives) are also available, done **approximately, in mesh space**,
> under an honest "approximation mode"?*
>
> **Status:** exploration, for owner review. **Author:** architecture agent,
> 2026-05-22. **Scope:** extends the manifold backend; changes nothing already
> decided in `design-manifold-in-build123d.md`. **Branch context:**
> `feat/manifold-mesh-backend`, sub-package `build123d.mesh`.
>
> Cited corpus: `ddocs/design/design-manifold-in-build123d.md` (Principle P7,
> §6), `ddocs/research/03-manifold3d-deep-dive.md`,
> `ddocs/prototypes/p8_face_identity/NOTES.md`,
> `ddocs/prototypes/p9_faceid_brep_recovery/NOTES.md`,
> `/Users/ochafik/github/OCCT/ddocs/design/02-attributes-and-feature-recovery.md`,
> and scad2py's reference implementation
> (`/Users/ochafik/github/scad2py/scad2py/rendering/manifold_renderer.py`,
> `runtime/modules.py`, `minkowski_impl.py`).

---

## 0. Executive summary

The committed manifold design (`design-manifold-in-build123d.md`) draws a hard
line: manifold3d does **booleans + hull + Minkowski + SDF**, and OCC does
**everything else** — fillet, chamfer, loft, sweep, offset/shell, draft, exact
primitives, STEP. Non-goal **NG5** says explicitly that those finishing
operations are "undefined on a faceted mesh — `MeshPart` must not pretend
otherwise."

That line is correct *as a statement about exact geometry*. But it conflates two
different claims:

1. *"A mesh cannot represent an exact analytic blend surface."* — **True.** A
   round-tripped sphere is a polyhedron forever (`p1` §3, NG4).
2. *"Therefore these operations cannot be offered on a mesh at all."* — **False.**
   Every one of fillet, chamfer, loft, sweep, offset, draft has a perfectly good
   *faceted approximation*. OpenSCAD ships all of them and 3D-printing users are
   entirely happy. The proof is in this very tree: scad2py's `ManifoldRenderer`
   already runs OpenSCAD's whole operation set — `linear_extrude`,
   `rotate_extrude`, `offset`, `hull`, `minkowski` — on manifold meshes today
   (`scad2py/rendering/manifold_renderer.py`).

This note proposes resolving the conflation with an **explicit, honest
"approximation mode"**: a coherent all-mesh layer where the user *knows* results
are faceted and non-exact, and in exchange gets the full operation surface, fast,
robust, and WASM-friendly. It is not a competitor to the exact-BREP path — it is
a *second, clearly-labelled* path, the natural build123d backend for scad2py
(Goal 2), and the right default for 3D-printing / preview / OpenSCAD-style work.

The headline finding: **most of the catalogue is cheap.** Extrude, revolve, 2D
offset, hull, SDF-shell, SDF-fillet, primitives are *native* in manifold3d 3.x or
a *port* of code that already exists in scad2py. Only **selective mesh
fillet/chamfer** and **mesh loft/sweep** are genuine new builds — and faceID
(already built into the bridge, §3) is precisely what makes the *selective* ones
tractable. The genuinely lost set is small and narrow: exact NURBS interop and
metrology-exact queries.

---

## 1. Premise — "BREP-only" is mostly "BREP-exact", not "BREP-only"

build123d's finishing operations are documented as BREP-only because OCC is the
only kernel build123d has, and OCC implements them with exact analytic geometry
(`BRepFilletAPI_MakeFillet` produces a true `Geom_Cylinder`/`Geom_BSplineSurface`
blend; `BRepOffsetAPI_ThruSections` lofts with exact section curves). The mesh
backend design (`design-manifold-in-build123d.md` §3.6, P7) inherited that
framing and made it a kernel-boundary rule.

But "BREP-only" is two claims welded together. Strip them apart:

- **The exactness claim is real and permanent.** A mesh has no NURBS, no analytic
  surface, no exact circle (`research/03` §6.1). Anything that *needs* an exact
  surface — a STEP body whose cylinder must re-import as a `Geom_Cylinder`, a
  metrology query asking for the exact radius of a fillet — cannot come from a
  mesh. `p8` POINT 1 measured it: `filter_by(GeomType.CYLINDER)` on a meshed bore
  drops 1 → 0.

- **The availability claim is false.** *Every* finishing operation has a faceted
  analogue that is perfectly serviceable for a large class of work:
  - **fillet / chamfer** → a feature-edge-aware mesh bevel/round (OCCT doc 02
    §3.1: *"a mesh-domain chamfer/bevel is just more geometry … a reasonable
    feature to add on top of the mesh-CSG path"*).
  - **loft** → skinning triangle strips between section polylines.
  - **sweep** → extruding a profile polygon along a path polyline.
  - **extrude / revolve** → `CrossSection.extrude` / `.revolve` — *native*.
  - **3D offset / shell** → `level_set` SDF arithmetic, or per-vertex normal
    offset — *native or near-native*.
  - **draft** → a per-vertex shear keyed on Z — a `warp` callback.
  - **minkowski** → hull-of-pairs, decompose-then-Minkowski — **already
    implemented** in `scad2py/minkowski_impl.py`.

OpenSCAD is the existence proof: it offers *all* of these on a manifold mesh
kernel (manifold3d *is* OpenSCAD's modern backend — `research/03` §0), and an
enormous community of 3D-printing users finds them entirely adequate.

**The premise of this note:** the honest move is not to forbid the operations,
it is to *label the mode*. A user who has explicitly entered "approximation mode"
knows the output is faceted and non-exact — exactly as an OpenSCAD user knows
`$fn` controls facet count. Forbidding the operation (NG5) protects the user from
a *silent* wrong answer; an *explicit* mode protects them just as well while
delivering a coherent, useful capability. The principle that must hold (P3 —
"never silently degrade exact geometry") is satisfied by *visibility*, not by
*absence*: `design-manifold-in-build123d.md` §6.5 already states "the one outcome
to forbid is a silent wrong answer" — an explicit approximation mode is precisely
not silent.

This does not weaken the committed design. The exact path is untouched. P7 still
holds for the *exact* recovery story. This note adds a *parallel, opt-in,
clearly-faceted* path beside it.

---

## 2. Op-by-op catalogue

Cost classes:

- **native** — manifold3d 3.x already does it; a thin binding is all that is
  needed.
- **port** — the algorithm exists in scad2py (Apache-2.0, citable) and needs
  porting/adapting into `build123d.mesh`.
- **build** — a genuine new implementation in `build123d.mesh`; non-trivial but
  bounded.
- **lost** — genuinely unavailable in mesh space; no honest approximation exists.

| Operation | Mesh-approximation approach | Cost class | Notes |
|---|---|---|---|
| **box / sphere / cylinder / cone / wedge / torus** | `Manifold.cube/sphere/cylinder` + tessellated revolve for torus; facet count from a `$fn`-like tolerance | **native** | `MeshPart.box/sphere/cylinder` already in `design` §4.1. scad2py builds sphere/cylinder via `trimesh.creation.revolve` for OpenSCAD-faithful facetting (`runtime/modules.py`) — port that recipe. |
| **extrude (linear)** | `CrossSection.extrude(height, twist, n_divisions, scale_top)` | **native** | `research/03` §2.5. scad2py's `visitLinearExtrude` is a direct one-liner over it (`manifold_renderer.py`). Twist + top-scale come free. |
| **revolve** | `CrossSection.revolve(circular_segments, revolve_degrees)` | **native** | `research/03` §2.5. Partial-angle revolve supported. The 2D profile must first be a `CrossSection` (needs the 2D story — §5). |
| **3D convex hull** | `Manifold.hull` / `batch_hull` / `hull_points` | **native** | Already in the committed design (`mesh_hull`, G6). OCC has no equivalent. |
| **minkowski (sum)** | `manifold3d.Manifold.minkowski_sum` (3.x) **or** hull-of-vertex-pairs over a convex decomposition | **port** | scad2py's `minkowski_impl.py` is the reference: `_is_convex_3d`, `get_convex_part_vertices` (decompose → convex parts, fall back to trimesh/coacd convex decomposition), `_cross_product_hulls`. 3.x may have native `minkowski_sum` (`research/03` §6, open Q) — verify and prefer it; keep the scad2py decomposition path as the non-convex fallback. |
| **offset (2D)** | `CrossSection.offset(delta, JoinType, miter_limit)` | **native** | `research/03` §2.6. scad2py's `visitOffset` maps `r`→`JoinType.Round`, `chamfer`→`Miter`, else `Square` (`manifold_renderer.py`). This is the 2D fillet/chamfer primitive. |
| **3D offset / inflate** | `level_set` of `sdf(p) − r`; **or** per-vertex offset along welded normals + re-weld | **native / build** | SDF route is exact-ish and topology-correct (handles self-merging); normal-offset route is cheap but breaks at concavities. §4. |
| **shell / hollow / thicken** | `level_set` of `|sdf(p)| − t`; **or** `solid − solid.offset(−t)` (inward 3D offset then subtract) | **native / build** | The `\|sdf\|−t` form is the elegant one (§4). The boolean form reuses 3D offset and is simpler to land first. |
| **draft (taper faces by angle)** | `warp` / `warp_batch` callback applying a Z-dependent shear/scale to vertices | **build** | `research/03` §2.3 — `warp` moves vertices without adding topology; refine first if the drafted face must stay flat-ish. Per-face draft needs faceID (§3) to know which vertices belong to the face being drafted. |
| **fillet / chamfer (selective, on feature edges)** | faceID feature-edge graph (§3) → per-edge: round = subtract a swept-profile / smooth-min of an edge-tube SDF; chamfer = cut a swept bevel | **build** | The real new work. faceID gives the edge set; corner blends where ≥3 filleted edges meet are the hard part (§7). OCCT doc 02 §3.1 confirms it is *buildable*. SDF smooth-min gives constructive rounding cheaply (§4). |
| **fillet / chamfer (whole-model "soften everything")** | `manifold3d.smooth` + `refine`, or a global SDF smooth-min | **native** | `research/03` §2.4: `smooth(mesh, sharpened_edges, edge_smoothness)` + `refine` is "the standard make-this-faceted-thing-curved recipe". 3.x adds `smooth_out(min_sharp_angle, min_smoothness)` — automatic, no edge list. Not *selective*, but a real, cheap, honest "round all the things". |
| **loft / skin between sections** | triangulate strips between consecutive section polylines (consistent winding); cap ends | **build** | No native manifold loft. Sections come from `CrossSection.to_polygons()`. The hard part is correspondence between sections with different vertex counts — standard, solvable. |
| **sweep / pipe along a path** | place a profile `CrossSection` at samples along a path polyline (Frenet or fixed-up frames), skin consecutive copies — i.e. a loft with auto-generated sections | **build** | Reduces to loft once frames are chosen. Twist and scale along the path fall out of the frame transforms. Self-intersection on tight turns is the failure mode — manifold's boolean cleans it if the sweep is built as a union of segment solids. |
| **holes / counterbores / countersinks** | `mesh_cut` with a cylinder / stepped-cylinder tool | **native** | Plain boolean — the manifold backend's core competency (`design` §3.6). Threads: a helical-swept profile (mesh sweep) subtracted; cosmetic threads are just a cylinder. |
| **threads (real helical)** | sweep a thread-profile polygon along a helix path → boolean | **build** | A specialisation of mesh sweep; the helix path is analytic and easy to sample. Faceted but printable — exactly what 3D-printing users want. |
| **section / split by plane** | `Manifold.split_by_plane` / `trim_by_plane`; `Manifold.slice(z)` → `CrossSection` | **native** | `research/03` §2.1, §2.5. `split` returns both halves in one pass. `slice`/`project` give 2D sections. |
| **decompose / connected components** | `Manifold.decompose()` | **native** | `research/03` §2.7. Used by Minkowski; also a useful query. |
| **free-form deformation (twist, bend, taper)** | `warp` / `warp_batch` | **native** | `research/03` §2.3. scad2py uses it implicitly via twisted extrude; a general `MeshPart.warp(fn)` is one binding. |
| **exact NURBS / analytic-surface STEP export** | — | **lost** | A faceted body STEP-exports as a faceted shell, not analytic surfaces (`p7` §7, NG4). The faceID-seeded *exact* recovery path (`p9`) is the answer for **planar** CSG only; curved is faceted. This is the genuine, narrow loss. |
| **metrology-exact queries** (exact radius/area of a curved feature, exact tangency, GD&T) | — | **lost** | A facet patch has no exact radius. `volume`/`area`/`bbox` are fine and cheap (`research/03` §1.1); *exact analytic* measurement is not. |
| **IGES / STEP analytic interop, exact assembly mates on curved faces** | — | **lost** | Needs analytic faces a mesh does not carry. Joints can still be *placed* on rebuilt planar faces (`p8` POINT 3); curved-face mates cannot. |

**Reading the table.** Of ~20 catalogue rows, the majority are **native** or
**port** — i.e. cheap, and several already running in scad2py. Only **selective
fillet/chamfer**, **loft**, **sweep/threads**, and **per-face draft** are
**build**. The **lost** set is three rows, all about *exact analytic interop and
metrology* — not about modelling capability. That asymmetry is the whole argument
for the mode: the approximation layer is mostly assembly of existing parts.

---

## 3. The faceID enabler — why *selective* mesh fillet/chamfer is now tractable

A naive mesh fillet is "round every edge" — `smooth_out` already does that
(native, table above). The hard, *useful* version is **selective**: "fillet *this*
edge", "chamfer the four edges of this pocket". That needs an edge set, and a
triangle mesh has no edges — only triangle sides. This is exactly the problem
`p8`/`p9`/OCCT-doc-02 solved for the *exact recovery* path, and the same machinery
serves the *approximate finishing* path.

**The bridge already seeds faceID systematically.** `design-manifold-in-build123d.md`
§3.4 makes faceID seeding the *default* behaviour of the OUT leg: every triangle
of each input `TopoDS_Face` is stamped with a globally-unique id, and manifold3d
*maintains* that id through every boolean (`p9` §1–2, OCCT doc 02 §4 — group
count tracks input face count, not triangle count). This is *built*, not
hypothetical.

**A faceID partition implies a feature-edge graph for free.** OCCT doc 02 §1 is
explicit: *"an edge of the result mesh where the two adjacent triangles carry
different faceIDs is a feature edge. A faceID partition implies the feature-edge
graph for free."* manifold's `Simplify`/`smooth` even *"maintains all edges
between triangles with different faceIDs"* — it will not collapse a feature edge
(OCCT doc 02 §1). So after any boolean chain, `build123d.mesh` can walk the welded
mesh, find every welded edge whose two triangles disagree on faceID, and chain
those into feature-edge polylines.

**That polyline set is the input to a selective mesh fillet/chamfer.** Given a
feature-edge polyline and the two adjacent faceID regions:

- **chamfer** — inset each adjacent face along the edge, bridge the gap with a
  flat strip (OCCT doc 02 §3.1: *"inset the two faces along the edge and bridge
  them, or subtract a swept profile"*).
- **fillet** — replace the edge with a rolled tube: sweep a quarter-arc profile,
  or subtract/union an edge-tube and smooth (SDF smooth-min, §4).

**faceID is the unifying substrate.** This is the load-bearing observation: the
*same* faceID channel that the committed design uses for **exact B-rep recovery**
(`p9` — group by id, rebuild on the known `Geom_Plane`) is what makes
**approximate selective finishing** possible (group by id-boundary, get the edge
set). One mechanism, two payoffs:

```
              ┌─ faceID-grouped EXACT recovery  → planar → analytic B-rep (p9)
seeded faceID ┤
              └─ faceID-boundary feature-edge graph → selective mesh fillet/
                 chamfer/draft  (this note)
```

build123d does not need to pick. The bridge seeds faceID once; the exact path and
the approximate path both consume it. A `MeshPart` can offer
`feature_edges()` → a list of edge polylines, each tagged with the two faceIDs it
separates and (via the `SideMap`, `design` §3.4) whether those faces are planar
or curved — letting `fillet(edges, radius)` operate on a selected subset exactly
the way build123d's BREP `fillet` selects edges today.

**Honest limit.** faceID gives the *topology* of where edges are; it does not
give analytic surface equations for the *blend*. The mesh fillet produces a
faceted approximation of a roll, not a `Geom_Cylinder`. That is the point of
approximation mode. For curved adjacent faces the blend quality degrades further
(§7).

---

## 4. SDF as a substrate — `level_set`, and where it is elegant

manifold3d ships `Mesh.level_set(sdf, bounds, edgeLength, level)` — Marching
Tetrahedra over a body-centred-cubic grid, producing a *guaranteed-manifold*
solid from a Python `def sdf(x,y,z) -> float` (`research/03` §2.7; verified there
— a sphere SDF → 7958-vertex manifold in 87 ms). OCC has nothing comparable. The
signed-distance representation makes several finishing operations fall out as
*arithmetic on the field*:

| Operation | SDF expression | Why it is elegant |
|---|---|---|
| **3D offset / inflate by r** | `sdf(p) − r` | one scalar subtraction; topology (merging lobes, closing gaps) is handled automatically by the iso-surface extraction |
| **shell / hollow to thickness t** | `\|sdf(p)\| − t` | the absolute value turns the surface into a thin shell of half-width t — no inward-offset-then-subtract boolean needed |
| **fillet (constructive round of radius r)** | `smin(sdf_a, sdf_b, r)` — polynomial/exponential smooth-minimum | a union that *automatically* rounds the seam; `smax` rounds an intersection; `smin` of `−sdf` rounds a subtraction. This is the cleanest possible "round as you combine". |
| **chamfer (constructive bevel)** | a "chamfer-min" variant of `smin` (linear blend region) | same idea, linear blend instead of circular |
| **rounded primitives, lattices, gyroids** | analytic SDF directly | TPMS / infill / field-driven geometry that build123d cannot express at all today |

**Where SDF is genuinely the right tool.** *Constructive* rounding — when you
*build* the shape and want the joins rounded as a property of the combination
(the `smin` union). Lattices, gyroids, organic blends, variable-radius rounding
keyed on a field. Offset and shell, where the field arithmetic is a one-liner and
the iso-surface extraction handles topology changes (lobes merging, thin walls
self-intersecting) that a boolean-based offset handles only awkwardly.

**Where SDF is the wrong tool — its real limits.**

- **Resolution.** `edgeLength` sets a uniform grid; runtime and triangle count
  scale with `1/edgeLength³`. Fine features force a fine grid everywhere
  (`research/03` §2.7). There is no native adaptive octree in `level_set`.
- **Sharp-feature softening.** Marching Tetrahedra rounds every sharp edge to the
  grid scale. An SDF box re-extracted has *softened corners*. This is the exact
  opposite of what selective fillet wants — you cannot SDF-round one edge while
  keeping the rest crisp without a *per-edge* field, and a per-edge field is
  itself the selective-fillet problem in disguise.
- **mesh → SDF is non-trivial.** SDF arithmetic is elegant when you *have* the
  field. But a `MeshPart` is a triangle mesh, and turning an arbitrary mesh into
  a signed-distance field means a (winding-number or closest-triangle) distance
  query per grid point — buildable, not free, and itself a source of error near
  thin features. The clean SDF story works best when the *whole model* is built
  in SDF from the start (the lattice/gyroid case), not retrofitted onto a mesh
  produced by booleans.
- **Bounds clipping.** If the iso-surface exits `bounds` you get a grid-aligned
  cap (`research/03` §2.7) — a correctness footgun.

**Verdict for the mode.** Offer SDF as a *first-class but distinct* sub-capability:
`MeshPart.from_sdf(fn, bounds, resolution)`, `MeshPart.offset(r)` /
`.shell(t)` implemented via `level_set` where the field is available, and an
explicit `smin`-union constructor for constructive rounding. Do **not** make SDF
the implementation of *selective* fillet — there, the faceID feature-edge +
swept-profile route (§3) keeps un-filleted edges crisp, which SDF cannot.

---

## 5. Form options — how to expose the mode

Three shapes, composable.

### Option A — a rich `MeshPart` with every op as a method

Extend the `MeshPart` from `design-manifold-in-build123d.md` §4.1 with the whole
catalogue as plain methods:

```python
mp = MeshPart.box(40, 30, 12)
mp = mp.fillet(mp.feature_edges().filter_by(...), radius=2)   # selective
mp = mp.shell(thickness=1.5)
mp = MeshPart.loft([section_a, section_b, section_c])
mp = profile.sweep(path)
```

"Approximation mode" here is **not a mode at all** — it is simply *the set of
things a `MeshPart` can do*. Holding a `MeshPart` *is* being in approximation
mode; the type is the honesty signal (it is not a `Shape`, has no `geom_type`,
cannot STEP-export analytically — `design` §3.2). No magic, no global state,
statically visible capability boundary. `to_solid()` still bakes (faceted, or
exact-planar via `p9`).

- **Pro:** zero magic; the type *is* the contract; trivially testable; matches
  the committed design's "explicit verb" principle (P2); composes cleanly.
- **Con:** users who want OpenSCAD-like ergonomics (`fillet` as a builder verb)
  do not get them; verbose for scad2py-style scripts (mitigated — scad2py
  generates code, it does not care about verbosity).

### Option B — a `with approximate():` context that routes native ops

A context manager that, while active, makes build123d's *existing* verbs
(`fillet`, `chamfer`, `loft`, `sweep`, `extrude`, builder `BuildPart`) route
through the mesh backend instead of OCC:

```python
with approximate(tolerance=0.1):
    part = extrude(profile, 10)
    part = fillet(part.edges().group_by(Axis.Z)[-1], radius=2)
```

- **Pro:** familiar build123d code "just works" faceted; one-line opt-in; great
  demo.
- **Con:** **dangerous.** It silently changes the *type* and *exactness* of every
  result inside the block — exactly the "silent degradation" P3 forbids. It needs
  thread-local state, it interacts badly with nested exact/approximate regions,
  and `fillet(...)` returning something faceted while *looking* identical to the
  exact call is the precise footgun `design` §6.5 calls "the one outcome to
  forbid". A context that *changes the kernel under the same verb* is implicit
  coercion (NG3) wearing a `with` block.

### Option C — composition with the exact-recovery path

Whatever the surface, it must compose with what is already built:
`MeshPart.to_solid()` still runs the faceID-seeded reconstruction (`p9`) —
**planar regions recover exact and analytic, curved regions stay faceted**. So a
realistic pipeline is *approximate while modelling, exact-where-possible at the
bake*:

```python
mp = MeshPart.box(...) - MeshPart.cylinder(...)   # fast mesh CSG
mp = mp.chamfer(mp.feature_edges()..., 1.0)       # approximate finishing
solid = mp.to_solid()    # planar faces → exact analytic; chamfer faces → faceted
```

The approximate chamfer and the exact recovery are not in conflict: recovery
rebuilds whatever is planar, the mesh chamfer's flat bevel facets included; the
chamfer's geometry is *approximate* but its *recovered faces are exact planes*.
A mesh *fillet* (curved roll) stays faceted through `to_solid()` — honest.

### Recommendation

**Ship Option A. Reject Option B. Document Option C as the bake contract.**

`MeshPart`-with-methods is the only form consistent with the committed design's
load-bearing principles: P2 (every kernel crossing is an explicit verb), P3
(never silently degrade — the *type* tells the user), NG3 (no implicit
coercion). "Approximation mode" should mean **"you are holding a `MeshPart`"** —
a value, not a global. It is honest by construction: a `MeshPart` cannot pretend
to be exact because it is not a `Shape`.

Option B is a tempting demo and a real trap; if it is ever wanted, it must (a) be
explicit at *every* result (`approximate_fillet(...)` as a distinct verb, not a
re-bound `fillet`) and (b) never reuse an exact verb's name for a faceted result.
The cheap, safe version of B's ergonomics is just well-named free functions
mirroring §4.2 of the committed design (`mesh_fillet`, `mesh_loft`, `mesh_sweep`)
— explicit, no global state, no kernel-swap-under-a-verb.

---

## 6. scad2py convergence — approximation mode *is* OpenSCAD's model

OpenSCAD's entire computational model **is** approximation mode. OpenSCAD has no
BREP, no analytic surfaces, no exact fillet. `$fn`/`$fa`/`$fs` are the user's
explicit acknowledgement that everything is faceted. Its modern backend *is
manifold3d* (`research/03` §0). Every OpenSCAD operation — `union`, `difference`,
`hull`, `minkowski`, `linear_extrude`, `rotate_extrude`, `offset` — is a mesh
operation. There is no "exact mode" to fall out of.

scad2py's `ManifoldRenderer` (`scad2py/rendering/manifold_renderer.py`) is the
**existing reference implementation** of exactly the approximation-mode operation
set this note proposes:

- `visitUnion` / `visitDifference` / `visitIntersection` — `Manifold` `+`/`-`/`^`.
- `visitHull` — `Manifold.batch_hull` / `CrossSection.batch_hull`.
- `visitMinkowski` — `minkowski_3d`/`minkowski_2d` (`minkowski_impl.py`):
  decompose → convex parts (with a trimesh/coacd convex-decomposition fallback for
  non-convex) → `_cross_product_hulls` → union. **This is the `minkowski` "port"
  row of §2's table, already written and Apache-2.0.**
- `visitLinearExtrude` — `CrossSection.extrude(height, twist, n_divisions,
  scale_top)`.
- `visitOffset` — `CrossSection.offset` with `JoinType` chosen from `r`/`delta`/
  `chamfer` — **this is the 2D fillet/chamfer primitive, already mapped.**
- `visitColor` — `run_original_id` / `set_properties` colour tagging — the same
  channel `design` §4.6 specifies.
- A `@renderer` caching decorator keyed on `csg.FastKey` with an `is_reused`
  gate — the content-addressed cache `design` §7.2 / `research/08` prescribe,
  *already implemented*.

In other words: **scad2py has already built most of `build123d.mesh`'s
approximation layer.** The committed manifold design names scad2py the natural
consumer (G8, Goal 2); this note sharpens the claim: scad2py is not just a
*consumer*, it is the **prior-art reference** for the approximation-mode op set.
The build123d work is substantially *porting and re-homing* scad2py's renderer
behind a `MeshPart` API — `runtime/modules.py` is the primitive catalogue,
`manifold_renderer.py` is the operation dispatch, `minkowski_impl.py` is the one
non-trivial algorithm.

This is the convergence: **build123d's approximation mode and scad2py's only mode
are the same thing.** Building `build123d.mesh`'s approximation layer *is*
building the scad2py backend. The OpenSCAD `.scad` → scad2py CSG tree →
`build123d.mesh` `MeshPart` pipeline becomes near-mechanical, and scad2py's
caching visitor transfers wholesale (`design` §7.4, Phase 3).

Note the licence is clean: scad2py is Apache-2.0, manifold3d is Apache-2.0,
build123d is Apache-2.0 (`research/06`). The scad2py code can be ported directly.

---

## 7. Honest scope — what this is, and what is hard

**What this is.** A *mesh-CAD layer*. Not a CAD kernel, not a competitor to OCC's
exact path. It is the faceted-modelling capability that OpenSCAD users already
have, brought into build123d as an explicit, opt-in `MeshPart` surface, sharing
the manifold backend's bridge and faceID machinery.

**It is incremental.** Nothing here blocks or changes the committed
`design-manifold-in-build123d.md` phases. The catalogue (§2) sorts itself: ship
the **native** and **port** rows first (they are cheap and several already exist
in scad2py), then the **build** rows. Each row is independently shippable.

**Cheap/native first.** extrude, revolve, 2D offset, hull, decompose,
split/section, `smooth_out` whole-model rounding, SDF offset/shell/`from_sdf`,
minkowski (port) — these are bindings or ports. They deliver most of the OpenSCAD
op set for little effort.

**The real builds, in honest difficulty order.**

1. **loft / sweep** — medium. Section correspondence (different vertex counts per
   section) and path-frame selection are standard, solvable problems; building
   each sweep segment as a solid and unioning lets manifold's robust boolean
   clean self-intersections on tight turns.
2. **selective mesh fillet / chamfer** — hard, and *fillet quality is the crux*.
   Edge identification is *solved* (faceID feature-edge graph, §3). A single
   straight edge between two planar faces is easy — sweep a profile, boolean. The
   hard parts: **corner blends** where ≥3 filleted edges meet at a vertex (a
   naive per-edge tube gaps or self-intersects — a proper blended corner patch
   must be built; this is also the hard part of OCC's own `BRepFilletAPI`);
   **radius vs feature size** (a radius larger than the adjacent face
   self-intersects — the mesh version must fail loudly per P3 or clamp, never
   silently produce garbage); **curved adjacent faces** (compounds faceting
   error — the SideMap should flag it).
3. **per-face draft** — medium; the `warp` is easy, identifying which vertices
   belong to the drafted face needs faceID and care at face boundaries.

**The honest framing for users.** Approximation mode trades *exactness* for
*capability, speed, robustness, and a tiny WASM footprint*. A `MeshPart` fillet
is a faceted approximation of a roll — good for printing, preview, and
OpenSCAD-style work; **not** for a STEP body whose blend must re-import as a
`Geom_BSplineSurface`. The mode never hides this: the user is holding a
`MeshPart`, not a `Solid`, and the bake (`to_solid()`) is explicit and documents
the faceting. The single rule from the committed design still governs: **never a
silent wrong answer** (`design` §6.5) — and an explicitly-entered mesh mode is,
by construction, not silent.

---

## 8. Recommended phasing

Layered onto `design-manifold-in-build123d.md`'s Phase 0–4. Phases here are
**after** that document's Phase 1 (`MeshPart` exists) and Phase 2 (faceID /
ReFacer landed) — the approximation layer *depends* on `MeshPart` and on the
faceID feature-edge graph.

### Phase A0 — native ops on `MeshPart` (cheap, high value)

- `MeshPart` primitives beyond box/sphere/cylinder: cone, wedge, torus (port
  scad2py's `trimesh.creation.revolve` recipe from `runtime/modules.py` for
  `$fn`-faithful facetting).
- `MeshPart.extrude` / `.revolve` over `CrossSection` (native).
- `MeshPart.split` / `.section` / `.slice` / `.decompose` (native).
- `MeshPart.warp(fn)` free-form deformation (native).
- **Decision needed:** the 2D story. `extrude`/`revolve`/2D-`offset` need a 2D
  region. `design-manifold-in-build123d.md` open question 4 defers `MeshSketch`;
  the approximation mode *forces* the question — recommend a thin `MeshSketch`
  over `manifold3d.CrossSection` lands here (it is also exactly what scad2py's 2D
  path needs).

**Value: high. Risk: low.** Mostly bindings; scad2py has the recipes.

### Phase A1 — SDF sub-capability + whole-model rounding

- `MeshPart.from_sdf(fn, bounds, resolution)` over `level_set` (native).
- `MeshPart.offset(r)` / `.shell(t)` — SDF form where a field is available,
  boolean form (`solid − inward-offset`) as the fallback.
- `MeshPart.smooth(min_sharp_angle=...)` whole-model rounding via
  `smooth_out` + `refine` (native).
- An explicit `smin`-union constructor for constructive rounding.

**Value: medium–high** (SDF / lattices are a genuine new capability OCC lacks —
G6). **Risk: low.**

### Phase A2 — minkowski + 2D offset (port from scad2py)

- Port `minkowski_impl.py` (`minkowski_2d`/`minkowski_3d`, convex decomposition,
  `_cross_product_hulls`) into `build123d.mesh`; prefer native
  `Manifold.minkowski_sum` if the pinned 3.x wheel has it, keep the
  decomposition path as the non-convex fallback.
- `MeshSketch.offset` over `CrossSection.offset` (the 2D fillet/chamfer).

**Value: medium** (Minkowski is OCC-impossible; high for scad2py). **Risk: low**
— the code exists and is Apache-2.0.

### Phase A3 — feature-edge graph + selective mesh chamfer

- `MeshPart.feature_edges()` — walk welded edges, group by faceID disagreement,
  chain into polylines tagged with the two faceIDs + planar/curved from the
  `SideMap`.
- `MeshPart.chamfer(edges, length)` — inset + bridge (the *easier* of the two;
  flat bevel, no corner-blend curvature).

**Value: high** (selective finishing is the headline new capability). **Risk:
medium** — corner handling.

### Phase A4 — selective mesh fillet, loft, sweep, draft

- `MeshPart.fillet(edges, radius)` — swept-profile / edge-tube; the corner-blend
  problem is the bulk of the work (§7).
- `MeshPart.loft(sections)` and `profile.sweep(path)`.
- `MeshPart.draft(faces, angle, neutral_plane)`.

**Value: high. Risk: medium–high** — fillet corner quality is the hard part of
the whole note; loft/sweep are bounded.

### Phase A5 (forward-looking) — scad2py backend wiring

With A0–A4 landed, `build123d.mesh` exposes the full OpenSCAD operation set on
`MeshPart`. Wire scad2py's CSG-tree → `MeshPart` (the `ManifoldRenderer` becomes
a thin re-target); reuse scad2py's `FastKey` caching visitor (`design` §7,
Phase 3). This is Goal 2 substantially delivered as a *consequence* of the
approximation layer rather than as separate work.

---

## 9. Open questions for the owner

1. **Mode framing.** Endorse "approximation mode = holding a `MeshPart`"
   (Option A) and reject the `with approximate():` context (Option B) as silent
   degradation — or keep B as a documented power-user escape hatch with distinct
   verb names?
2. **2D / `MeshSketch`.** The mode forces `design-manifold-in-build123d.md`
   open-question 4 — land a thin `MeshSketch` over `CrossSection` in Phase A0?
   (It is also the scad2py 2D path.)
3. **Scope ceiling.** Is selective mesh *fillet* (A4, the hard corner-blend work)
   in scope, or does the mode stop at chamfer (A3) + whole-model `smooth_out`,
   leaving true rounding to the exact OCC path?
4. **scad2py code intake.** Port `minkowski_impl.py` / `manifold_renderer.py`
   logic into `build123d.mesh` directly (Apache-2.0 → Apache-2.0, clean), or keep
   scad2py as an external consumer that *calls* `build123d.mesh`?
5. **Naming.** `MeshPart.fillet` vs `mesh_fillet` vs `approximate_fillet` — how
   loud must the "this is faceted" signal be in the *name*, given the *type*
   already signals it?
6. **NG5 amendment.** `design-manifold-in-build123d.md` NG5 currently rules
   fillet/chamfer/loft/sweep on mesh shapes out of scope. If adopted, NG5 should
   be **rewritten**: "exact … out of scope; *approximate, faceted* versions on
   `MeshPart` are the subject of `research/12`." Owner to ratify.

---

## 10. Bottom line

The committed manifold design treats fillet/chamfer/loft/sweep/offset as a hard
OCC-only boundary. That is correct for *exactness* and wrong for *availability*:
every one of those operations has an honest faceted approximation, OpenSCAD ships
them all on this very kernel, and scad2py already implements most of them
(`manifold_renderer.py`, `minkowski_impl.py`). An explicit, opt-in
**approximation mode** — best expressed as a rich `MeshPart` whose *type* is the
honesty signal — turns build123d's mesh backend into a coherent all-mesh
modelling layer: ideal for 3D-printing, preview, OpenSCAD-style workflows, the
WASM target, and the scad2py backend (Goal 2).

The catalogue is mostly cheap: native bindings and scad2py ports cover extrude,
revolve, offset, hull, minkowski, SDF offset/shell, split, and whole-model
rounding. faceID — already seeded by the bridge for *exact* recovery — is the
unifying substrate that *also* makes *selective* mesh fillet/chamfer tractable by
yielding a feature-edge graph for free. The genuinely lost set is small and
narrow: exact NURBS/STEP analytic interop and metrology-exact queries. The hard
build is fillet *corner-blend quality* — the same problem OCC's own
`BRepFilletAPI` struggles with. Recommended path: ship the cheap native/port
rows first (Phases A0–A2), then the feature-edge graph and selective chamfer
(A3), then fillet/loft/sweep (A4), then wire scad2py (A5).
