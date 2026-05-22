# Executive Summary

**Subject:** Feasibility of (1) bringing manifold3d mesh operations into build123d, and
(2) making scad2py use build123d as a backend and transpilation target.
**Date:** 2026-05-22 · **Status:** research + prototyping complete; no production code shipped.
**Verdict:** **Both goals are viable and worth doing.** Every hard blocker identified has
a prototyped solution.

---

## 1. Goal 1 — manifold3d in build123d: **GO**

### The problem
build123d's kernel is OpenCASCADE (OCC), a BREP kernel. It is excellent at *exact*
geometry (NURBS, fillets, chamfers, lofts, STEP I/O) but its boolean operations are
**slow and scale badly**:

- Measured (`p2`): manifold3d booleans are **20–134× faster** than OCC, and the gap
  *grows* with N — OCC scales ≈O(N¹·⁷–N²), manifold ≈linear.
- At N=1000 iterated holes, **OCC times out (>120 s); manifold finishes in 0.5 s.**
  Past a threshold this is finish-vs-never-finish, not just speed.
- Honest caveat: modern OCC 7.9 got all 10 degenerate/coplanar cases *correct* — it is
  slow there, not wrong. The fragility reputation has partly aged out; the speed
  problem has not.

manifold3d also offers operations OCC effectively *cannot* do — robust mesh CSG, hull,
Minkowski, SDF→solid — which is independently why Goal 2 needs it.

### What was de-risked
- **Solid → mesh** (`p1`): easy, <100 ms, robust. One subtlety: OCC tessellation
  duplicates vertices at face seams, so a vertex-**weld** pass is mandatory before
  manifold3d will accept the mesh (else it silently returns an empty result).
- **mesh → BREP** was *the* blocker. Per-triangle sewing (build123d's current
  `Mesher` path) is **quadratic** — 3.4 min at 130k triangles, unusable. **`p7` solved
  it**: direct `TopoDS_Shell` assembly is **~18× faster, near-perfectly linear**, turns
  1M triangles into a valid, downstream-usable `Solid` in ~53 s with **0 % volume
  error**.
- **The API** (`p3`): a `MeshPart` value type wrapping a `manifold3d.Manifold`, with
  `+ - &` operators and explicit `to_solid()`. A 72-boolean chain ran **71× faster**
  than native OCC.
- **Face identity** (`p8`): manifold booleans destroy BREP face naming, but
  *provenance survives* via `run_original_id`. Planar faces can be rebuilt into real
  build123d `Face`s (a plate+hole round-trips to the exact BREP face count, and
  selectors work). Curved faces and fillet-after-boolean are **permanently lost**.

### Recommendation
Ship manifold3d as an **opt-in hybrid** — build123d issue **#1228 already requests
exactly this** as a `build123d[manifold]` extra.

- **`MeshPart` is a standalone value type, *not* a `Shape` subclass.** `Shape.__init__`
  hardwires `downcast()` to `TopoDS`; subclassing would also lie about `geom_type`,
  fillet, and STEP capability. Keep the two kernels parallel.
- **The mesh↔BREP boundary is always explicit.** Stay in mesh space across a CSG
  chain; `to_solid()` is a deliberate, visible "bake once, late." An implicit per-op
  bake erases the entire speedup (`p2` measured this).
- **Never silently degrade.** Asking for a fillet or a curved-surface selector on a
  meshed result must *raise*, not mis-answer.

Full design: [`design/design-manifold-in-build123d.md`](design/design-manifold-in-build123d.md).

---

## 2. Goal 2 — scad2py on build123d: **GO, after a one-file-class GPL fix**

### Two products, both prototyped, both worth shipping
- **Option A — runtime backend** (`p4`): a `Build123dRenderer` visitor alongside
  scad2py's existing `ManifoldRenderer`, turning the `csg.Node` tree into build123d
  `Shape`s. Verified: 15/33 examples render to valid build123d with exact BREP and
  clean STEP export. *Use case: keep authoring in OpenSCAD, get BREP/STEP output.*
- **Option B — source transpiler** (`p5`): `.scad` → readable, editable build123d
  Python source. Verified: 8/8 example files transpile, run, and export. *Use case: a
  one-time migration off OpenSCAD.*

### The central design fact: it must be a *hybrid*
A pure-build123d backend is not possible. `minkowski`, 3D `hull` of curved solids,
non-planar `polyhedron`, and large/dense CSG have **no BREP form** — build123d's kernel
either cannot do them or times out. These route to the manifold backend from Goal 1;
the clean exact/parametric subset routes to build123d BREP. **Goal 2 depends on Goal 1.**

### The one real blocker — and it is solved
`scad2py/calc.py` and `scad2py/colors.py` carry **GPL-2.0+ headers** and are
self-described line-by-line ports of OpenSCAD C++ (© Clifford Wolf / Marius Kintel).
This is the exception to "all scad2py code is ochafik's" — those two files are *not*
his copyright, and while present they make scad2py a GPL work that **cannot** merge
into Apache-2.0 build123d.

`p6` **solved this**: a clean-room rewrite of both files (circle-fragment math,
rotation/mirror matrices, color parsing) from public specs only — the OpenSCAD User
Manual, the W3C CSS Color 4 standard, textbook linear algebra. Proven **bit-exact
against the originals across 3,308 test cases**. The contamination is exactly those two
formula-only files; a full 52-file scan found nothing else. Everything else in scad2py
is ochafik's and freely relicensable.

Full design: [`design/design-scad2py-build123d.md`](design/design-scad2py-build123d.md)
and [`design/scad2py-gpl-remediation.md`](design/scad2py-gpl-remediation.md).

---

## 3. License verdict (not legal advice)

| Component | License | Verdict |
|---|---|---|
| build123d | Apache-2.0 | ✅ |
| manifold3d (+ Clipper2 BSL-1.0, TBB, nanobind) | Apache-2.0, all deps permissive | ✅ no blocker for Goal 1 |
| OpenCASCADE (via `cadquery-ocp`) | LGPL-2.1 + Open CASCADE Exception | ✅ fine — dynamically linked; carry the LGPL text + an OCCT source offer |
| scad2py — original code | ochafik's (sole copyright; freely relicensable) | ✅ after adding a `LICENSE` |
| scad2py — `calc.py`, `colors.py` | **GPL-2.0+ (ported OpenSCAD)** | ⚠️ **the one blocker** — solved by `p6` clean-room rewrite |

The scad2py parser is **clean**: reimplementing the OpenSCAD *language* (an interface)
is not infringement (*Google v. Oracle*); it only *references* OpenSCAD's grammar by
URL. WASM/static-linking tightens the GPL constraint — a single `.wasm` has no
separate-process escape hatch, so the `p6` fix is a hard prerequisite for any browser
build.

---

## 4. Critical path

```
Goal 2 ──► Phase 0: swap in p6 clean-room files, add scad2py LICENSE   [blocks all of Goal 2]
              │
Goal 1 ──► Phase 1: build123d[manifold] extra — free functions + fast bake (p7)
              │                                  [the MVP; standalone value to build123d users]
              ▼
           Phase 2: MeshPart value type + face-identity selectors (p8)
              │
              ▼
Goal 2 ──► Phase 3: Build123dRenderer (Option A), hybrid routing onto the Goal-1 backend
           Phase 4: scad2b3d source transpiler (Option B)
```

Goal 1 Phase 1 delivers value to build123d users **on its own** and is the lowest-risk
starting point. Goal 2's Phase 0 (the GPL fix) is independent and can happen in
parallel immediately. See [`ROADMAP.md`](ROADMAP.md).

## 5. Top risks (all with prototyped mitigations)

1. **Un-welded tessellation → silent empty boolean.** Mitigation: mandatory vertex
   weld; make it a `tessellate(weld=True)` option. (`p1`, `p8`)
2. **Implicit mid-chain mesh→BREP bake erases the speedup.** Mitigation: a
   `MeshPart`-returning API that makes baking a deliberate verb. (`p2`)
3. **Silent geometry degradation** (faceted result lying as exact; fillet on a mesh).
   Mitigation: raise loudly on curved-surface selectors / fillet after meshing. (`p8`)
4. **OCC O(n²) boolean cliff** reached via naive implicit union. Mitigation: emit
   `Compound` for aggregation; route big trees to the mesh backend. (`p2`, `p5`)
5. **Accidentally shipping GPL code.** Mitigation: Phase 0; CI grep for GPL headers.
6. **manifold3d API drift** (2.3.x vs 3.x). Mitigation: pin `manifold3d >=3.4,<4`.

## 6. What this is not

Research and prototypes only. Nothing here is production code, nothing is merged, no
build123d source was touched. The prototypes are reference implementations that prove
the designs work; turning them into shippable features is the roadmap's job.
