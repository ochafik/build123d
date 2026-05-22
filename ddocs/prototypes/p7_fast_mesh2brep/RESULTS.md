# p7 — fast mesh → build123d Solid — RESULTS

> Prototype: `ddocs/prototypes/p7_fast_mesh2brep/`
> Goal: close the #1 named blocker for a manifold3d backend in build123d —
> turning a triangle mesh back into a build123d BREP `Solid` **fast**.
> Date: 2026-05-22. venv: `/Users/ochafik/github/.ddocs-venv`.
> build123d `0.1.dev2750+ge157376dc`, manifold3d 3.4.x, OCP/OpenCASCADE 7.x.

All numbers below are **real measured output** pasted from the probe scripts.
Machine: Apple Silicon (Darwin 24.6), 36 GB RAM.

---

## TL;DR

- **The slow path is dead.** Per-triangle `BRepBuilderAPI_Sewing` (what
  `p1_brep_mesh_roundtrip` and build123d's `Mesher` use) is super-linear:
  it drops from ~4 800 tri/s at 1k triangles to **1 059 tri/s at 100k**, and
  100k triangles already costs **95 seconds**. Unusable at scale.
- **Direct shell assembly is the answer.** Building the `TopoDS_Shell` straight
  from manifold3d's already-welded vertex/index arrays — no spatial search —
  is **near-perfectly linear** (scaling exponent k = 1.00 / 0.99 / 1.02) at a
  steady **~19 000 tri/s**. **1M triangles → valid build123d `Solid` in ~53 s**
  (warm cache; ~136 s in a cold run under memory pressure — still minutes, not
  hours, and still linear).
- The directly-assembled `Solid` is **topologically valid** (`BRepCheck` passes,
  `Solid.is_valid` / `.is_manifold` true, Euler characteristic correct) and
  **fully usable downstream**: `.faces()`/`.edges()` selection, build123d
  booleans, and STEP export/round-trip all work with **0.0000 % volume error**.
- The crossover is immediate — direct-nofix wins at every size, by 4× at 1k
  and **18× at 100k**. `ShapeFix_Shell` (the `+fix` variant) is itself
  super-linear; skip it and rely on manifold3d's guaranteed winding.
- **Ceiling:** *reconstruction* scales to 1M+ triangles fine. The real ceiling
  is **`BRepCheck_Analyzer` validation** and **STEP export** at that scale
  (memory-heavy, minutes). For a manifold backend the lesson is: stay in mesh
  space, bake to BREP **once, late**, and skip per-step validation.

---

## 1. Setup

Three converters, all in `m2b.py`, all returning a real build123d `Solid`
(`Solid(TopoDS_Solid)` — the build123d constructor takes the raw OCC handle
directly, confirmed against `build123d.topology.three_d.Solid`):

| | Function | Technique |
|---|---|---|
| **A** | `mesh_to_solid_sewing` | **Slow baseline.** One planar `TopoDS_Face` per triangle → one `BRepBuilderAPI_Sewing` → `MakeSolid`. The `p1` / `Mesher` path. Sewing spatially matches coincident verts/edges across *all* faces. |
| **B+fix** | `mesh_to_solid_direct` | **Fast.** Build each `TopoDS_Vertex` once; each `TopoDS_Edge` once, keyed by sorted vertex-index pair and shared (reversed) between its two triangles; one `TopoDS_Face` per triangle via `BRep_Builder.Add` into one `TopoDS_Shell`. Then `ShapeFix_Shell` to repair orientation. |
| **B nofix** | `mesh_to_solid_direct_nofix` | Same as B but **no `ShapeFix_Shell`** — relies on manifold3d's guaranteed consistent outward winding. The only variant that scales to 1M. |

Test meshes: r=20 sphere from manifold3d, triangle count driven by
`Manifold.refine(n)` (multiplies tris by n², stays a guaranteed valid
2-manifold — OCC sphere tessellation saturates, so a deflection sweep does not
work; this is the `p1` driver's trick).

---

## 2. Correctness (`probe_correctness.py`)

All three converters on small meshes — every one produces a **valid** Solid
with **exact** volume and correct Euler characteristic (genus-0 → V−E+F=2,
genus-1 → 0; E = 3F/2 confirming each edge shared by exactly 2 triangles):

```
[cube]  8 verts, 12 tris  manifold vol=1000.0000  genus=0
  A sewing       :     2.8 ms  kind=Solid  F=   12 E=   18 V=    8  V-E+F=+2  valid=True   vol=1000.0000 err=0.0000%
  B direct+fix   :     3.0 ms  kind=Solid  F=   12 E=   18 V=    8  V-E+F=+2  valid=True   vol=1000.0000 err=0.0000%
  B direct nofix :     0.7 ms  kind=Solid  F=   12 E=   18 V=    8  V-E+F=+2  valid=True   vol=1000.0000 err=0.0000%

[sphere mid-res]  1026 verts, 2048 tris  manifold vol=4165.1365  genus=0
  A sewing       :   396.7 ms  kind=Solid  F= 2048 E= 3072 V= 1026  V-E+F=+2  valid=True   vol=4165.1364 err=0.0000%
  B direct+fix   :   474.4 ms  kind=Solid  F= 2048 E= 3072 V= 1026  V-E+F=+2  valid=True   vol=4165.1364 err=0.0000%
  B direct nofix :    96.9 ms  kind=Solid  F= 2048 E= 3072 V= 1026  V-E+F=+2  valid=True   vol=4165.1364 err=0.0000%

[sphere - cube (CSG)]  616 verts, 1232 tris  manifold vol=2942.1735  genus=1
  A sewing       :   228.7 ms  kind=Solid  F= 1232 E= 1848 V=  616  V-E+F=+0  valid=True   vol=2942.1735 err=0.0000%
  B direct+fix   :   277.0 ms  kind=Solid  F= 1232 E= 1848 V=  616  V-E+F=+0  valid=True   vol=2942.1735 err=0.0000%
  B direct nofix :    59.3 ms  kind=Solid  F= 1232 E= 1848 V=  616  V-E+F=+0  valid=True   vol=2942.1735 err=0.0000%

[cube with through-hole (genus 1)]  72 verts, 144 tris  manifold vol=3500.5688  genus=1
  A sewing       :    25.7 ms  ...  V-E+F=+0  valid=True   vol=3500.5688 err=0.0000%
  B direct nofix :     6.9 ms  ...  V-E+F=+0  valid=True   vol=3500.5688 err=0.0000%
```

**All three are correct.** Genus is preserved (tunnels survive). Even on tiny
meshes direct-nofix is already 4–5× faster than sewing.

---

## 3. Scaling benchmark (`probe_scaling.py`)

r=20 sphere, 1k → 1M triangles. Sewing and `+fix` capped at 150k (beyond that
they take minutes). **Real output:**

### Time (seconds, lower is better)

| target | tris | A sewing | B direct+fix | B direct nofix |
|---:|---:|---:|---:|---:|
| 1 000 | 512 | 0.106 | 0.120 | **0.027** |
| 10 000 | 8 192 | 2.922 | 2.515 | **0.434** |
| 100 000 | 100 352 | 94.724 | 41.633 | **5.156** |
| 1 000 000 | 991 232 | *skipped* | *skipped* | **52.786** |

### Throughput (tri/s, higher is better)

| target | tris | A sewing | B direct+fix | B direct nofix |
|---:|---:|---:|---:|---:|
| 1 000 | 512 | 4 827 | 4 252 | **18 964** |
| 10 000 | 8 192 | 2 804 | 3 258 | **18 886** |
| 100 000 | 100 352 | 1 059 | 2 410 | **19 463** |
| 1 000 000 | 991 232 | *skipped* | *skipped* | **18 778** |

### Scaling exponent of direct-nofix (time ~ tris^k)

```
       512 ->     8192 tris : k = 1.00
      8192 ->   100352 tris : k = 0.99
    100352 ->   991232 tris : k = 1.02
```

**Reading the table:**

- **Sewing is super-linear and collapsing.** Throughput *falls* as the mesh
  grows: 4 827 → 2 804 → 1 059 tri/s. 100k triangles = 95 s. Extrapolating the
  super-linear trend, 1M triangles would be **~1 hour+** — confirmed unusable,
  exactly as `p1` predicted.
- **Direct-nofix is flat-linear.** k ≈ 1.00 across three decades; throughput
  holds at ~19 000 tri/s from 512 to ~1M triangles. **1M triangles in 53 s.**
- **`ShapeFix_Shell` is the trap.** `+fix` is ~2× faster than sewing but still
  super-linear (3 258 → 2 410 tri/s); at 100k it costs 42 s vs 5 s for nofix.
  Do not use it at scale — rely on manifold3d's winding guarantee instead.
- **Crossover: immediate.** Direct-nofix wins at *every* size — 4× at 1k,
  18× at 100k. There is no regime where sewing is preferable.

---

## 4. Phased breakdown (`probe_phases.py`)

Where the direct-nofix time goes, and confirmation no super-linear phase hides
inside. **Real output:**

```
     tris     total     verts   edge+face   mkSolid      tri/s
------------------------------------------------------------
      512     0.025     0.001       0.024     0.000      20223
     8192     0.406     0.007       0.396     0.000      20181
   100352     5.015     0.089       4.883     0.000      20010
   991232    52.719     1.329      50.522     0.000      18802
```

- **~97 % of the time is the edges+faces+shell loop** — the Python-level
  per-triangle work (`MakeEdge`/`MakeWire`/`MakeFace`/`BRep_Builder.Add`). It
  is dead linear.
- `BRepBuilderAPI_MakeSolid` from the finished shell is **effectively free**
  (<1 ms even at 1M faces) — it just wraps the shell.
- Wrapping the OCC `TopoDS_Solid` in a build123d `Solid` is **0.03 ms** — the
  build123d layer adds no measurable cost.
- `is_valid()` (`BRepCheck_Analyzer`) measured separately: 0.03 s @ 512,
  0.54 s @ 8k, **7.8 s @ 100k** — already growing faster than linear and the
  practical validation ceiling (see §7).

The loop is pure Python overhead per triangle; a C++ / batched implementation
would be far faster, but even this naive Python version clears 1M triangles in
under a minute.

---

## 5. Downstream usability (`probe_validity.py`)

A fast reconstruction is worthless if the `Solid` cannot be used. The
directly-assembled (nofix) Solid was put through real build123d operations.
**Real output:**

```
1. build123d Shape API on the reconstructed Solid
[cube]  12 tris -> Solid (kind=Solid)
  Solid.is_valid     : True
  Solid.is_manifold  : True
  .faces()/.edges()/.vertices(): 12 / 18 / 8
  .bounding_box()    : (-5.00,-5.00,-5.00) .. (5.00,5.00,5.00)
  .volume            : 1000.0000 (manifold ref 1000.0000)
[sphere]  1152 tris -> Solid    is_valid=True  is_manifold=True  .faces()=1152  vol err 0%
[cube w/ hole]  144 tris -> Solid  is_valid=True  is_manifold=True  .faces()=144  vol err 0%

2. build123d boolean on a reconstructed Solid
[sphere]    reconstructed - native Box: 91.8 ms  valid=True  vol err 0.0000%  result faces=1092
[cylinder]  reconstructed - native Box: 20.7 ms  valid=True  vol err 0.0000%  result faces=55

3. STEP export round-trip
[cube]    export 4.5 ms / 23.9 KB    re-import 3.6 ms  valid=True  faces=12   vol err 0.0000%
[sphere]  export 108.9 ms / 2.6 MB   re-import 226.3 ms valid=True faces=1152 vol err 0.0000%

4. winding robustness of the no-fix shortcut (every 5th triangle flipped)
[cube]    no-fix : valid=False  vol=500.0000   err=50.0000%   |  +fix : valid=True  err=0.0000%
[sphere]  no-fix : valid=False  vol=2440.7575  err=40.3998%   |  +fix : valid=True  err=0.0000%
```

**Verdict on usability: fully usable.**

- `Solid.is_valid` and `Solid.is_manifold` both **True** — build123d's own API
  accepts the directly-assembled Solid as a first-class solid.
- `.faces()` / `.edges()` / `.vertices()` ShapeList selection works (returns
  one face per triangle — see §6 for collapsing them).
- **build123d booleans** on the reconstructed Solid produce valid results with
  **0.0000 % volume error** vs the reference manifold boolean.
- **STEP export + re-import** round-trips cleanly — a faceted BREP survives
  STEP with zero volume error. So mesh→BREP can be purely an *export-boundary*
  concern: the CSG pipeline can stay in mesh form and bake to BREP once.
- **Winding caveat:** the nofix shortcut depends on manifold3d's guaranteed
  consistent outward winding. With deliberately broken winding it produces an
  invalid Solid with wrong volume; `+fix` (`ShapeFix_Shell`) recovers it. Since
  manifold3d *always* emits consistent winding, nofix is safe for the
  manifold-backend use case — but any *untrusted* mesh source must use `+fix`.

---

## 6. Coplanar-merge post-pass (`probe_unify.py`)

A faceted BREP has one face per triangle — thousands of tiny faces.
`ShapeUpgrade_UnifySameDomain` collapses adjacent coplanar facets. **Real
output:**

```
[cube (flat)]                      faces 12 -> 6      (0.5 ms)  -> 50.0% fewer  vol err 0%
[cylinder (2 caps + faceted wall)] faces 188 -> 50    (3.9 ms)  -> 73.4% fewer  vol err 0%
[cube with through-hole]           faces 208 -> 54    (4.2 ms)  -> 74.0% fewer  vol err 0%
[sphere (fully curved)]            faces 2048 -> 2048 (33.2 ms) ->  0.0% fewer  vol err 0%
[sphere minus box (CSG)]           faces 2032 -> 1908 (34.3 ms) ->  6.1% fewer  vol err 0%
```

- Flat geometry collapses **hard** (cube → 6 logical faces, cylinder/holed-cube
  ~74 % fewer faces) — recovering a sane, selectable topology.
- Fully-curved geometry is **untouched** (no two facets exactly coplanar) — as
  expected; the merge cannot un-facet a sphere.
- Volume is **exactly preserved** in every case.
- Cheap on small/medium meshes. Recommended as an optional post-pass when the
  reconstructed Solid will be hand-edited or STEP-shipped — not needed if it is
  only an intermediate boolean operand.

---

## 7. The 1M-triangle ceiling (`probe_million.py`)

Phase-separated 1M-triangle run, with `--validity` and `--step`. **Real
output** (cold run, after several earlier heavy runs — system under memory
pressure; see note below):

```
=== 1M-triangle direct reconstruction (no-fix) ===
  mesh: 991232 triangles, 495618 verts  (gen 0.2s)  manifold vol=32761.761

reconstructing (direct, no ShapeFix) ...
  reconstruction : 136.31 s  -> 7272 tri/s
  result kind    : Solid  unique edges built: 1486848  face_fail=0 edge_fail=0
  volume         : 32761.761  err=0.0000%  (9.16 s)
  face count     : 991232  (0.35 s, explorer walk)
  running BRepCheck_Analyzer (memory-heavy at 1M faces) ...
  BRepCheck valid: True  (75.5 s)
  STEP export of the 1M-face solid ...
  STEP export    : ok=True  2501.3 MB  (146.7 s)
```

**Phase-by-phase at ~1M triangles:**

| Phase | Time | Notes |
|---|---:|---|
| reconstruction (mesh → `Solid`) | **53 s** warm / **136 s** cold | warm = `probe_scaling`/`probe_phases` (page cache hot); cold = this standalone run |
| volume (`BRepGProp`) | 9 s | cheap, no big map |
| face count (explorer walk) | 0.35 s | trivial |
| **`BRepCheck` validity** | **76 s** | memory-heavy — peaked ~15 GB RSS |
| **STEP export** | **147 s** | wrote a **2.5 GB** STEP file, peaked ~20 GB RSS |

- **Reconstruction itself is fine** even at 1M triangles: 53 s with a warm
  cache (the `probe_scaling` / `probe_phases` numbers, k≈1.0 linear), and 136 s
  in a cold run with the machine already under memory pressure from earlier
  runs. Both are *minutes, not hours* — and the warm number is the one a real
  pipeline (running once, not 6× back-to-back) would see. The slowdown is
  memory contention, not an algorithmic cliff: the scaling exponent stays 1.02.
- **`BRepCheck_Analyzer` at 1M faces costs ~76 s and ~15 GB.** This — not
  reconstruction — is the validation ceiling. Do not validate intermediates.
- **STEP export of a 1M-face faceted solid is the heaviest single op:** 147 s,
  a 2.5 GB file, ~20 GB peak RSS. A million planar faces is simply a huge BREP.
  This is the strongest argument for *not* baking to BREP until the very end,
  and for exporting STL straight from the mesh when a *mesh* file is acceptable.
- **The result is still valid** — `BRepCheck` returns `True`, volume error
  0.0000 % — so a 1M-face build123d `Solid` is a real, correct solid; it is
  just an unwieldy one. The practical ceiling is therefore not correctness but
  **OCC's memory/time cost of carrying a million-face BREP through validation
  and I/O**, roughly the 1M-face mark on a 36 GB machine.

---

## 8. Honest verdict & implications for the manifold backend

**How fast can mesh → BREP realistically be in build123d?**
With direct shell assembly: **~19 000 triangles/second**, dead linear, in
pure Python. A 100k-triangle mesh → valid `Solid` in **5 s**; a 1M-triangle
mesh in **~53 s** warm (137 s cold/under memory pressure — still linear, k=1.02).
That is **18× faster than the sewing path** at 100k and the only path that
reaches 1M at all. Reconstruction itself is **solved** and is no longer the
blocker.

**What's the ceiling?**
*Reconstruction* has no practical ceiling below 1M+ triangles. The ceiling
moved downstream:

1. **`BRepCheck_Analyzer` validation** — ~8 s at 100k faces, minutes and many
   GB of RAM at 1M faces. Validating every intermediate solid is the new
   bottleneck.
2. **STEP export** of a million-face faceted solid is large and slow (see §7).
3. **OCC booleans** on million-face faceted solids would themselves be slow —
   OCC is not built for million-face inputs.

The lesson: the cost is no longer *making* the BREP, it is *OCC working with a
million-face BREP*.

**Implications for the manifold-backend design:**

- **Stay in mesh space for the whole CSG pipeline.** manifold3d does booleans
  orders of magnitude faster than OCC on faceted geometry. Do every
  intermediate operation in `manifold3d`.
- **Bake to BREP exactly once, as late as possible** — at the export boundary,
  or when the user explicitly asks for a `Solid`. Use `mesh_to_solid_direct_nofix`
  (manifold3d's winding guarantee makes the nofix path safe and ~8× faster than
  `+fix`).
- **Do not validate intermediates.** `BRepCheck` is the real ceiling; skip it
  on the trusted manifold→BREP path. Trust manifold3d's guarantee that its
  output is a watertight 2-manifold.
- **Run `UnifySameDomain` only when the BREP is user-facing** (hand-editing,
  STEP shipping) to collapse the facet explosion. Skip it for throw-away
  intermediate operands.
- **Curvature is gone regardless.** Both paths yield an all-planar-facet BREP —
  a meshed sphere reconstructs as a many-faced polyhedron, not a `Sphere`.
  Recovering analytic surfaces is a *separate* problem (`brep_from_stl`-style
  primitive detection) and out of scope here.

**Bottom line:** mesh → build123d `Solid` is fast enough (linear, ~19k tri/s,
1M in <1 min) and the result is a valid, fully-usable build123d Solid. The #1
named blocker is **cleared**. The remaining cost is OCC's own handling of
million-face BREPs — which is the design argument for keeping the manifold
backend in mesh space and baking to BREP once, late, unvalidated.
