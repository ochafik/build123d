# Roadmap

A phased, incremental plan for both goals. Each phase is **independently shippable** and
ordered by value-per-risk. Effort estimates are rough engineering-week ranges for one
developer; treat them as relative sizing, not commitments.

Two tracks run mostly in parallel. **Track G1** (manifold3d in build123d) is independent
and delivers value to build123d users on its own. **Track G2** (scad2py on build123d)
depends on G1 for its hybrid backend, and is gated by a one-time GPL fix.

```
        G2-P0  GPL remediation ──────────────┐  (independent; do immediately)
                                             │
G1-P1  manifold extra: free fns + fast bake  │
   │                                         │
G1-P2  MeshPart value type                   │
   │                                         │
G1-P3  face identity / selectors             │
   │           │                             │
G1-P4 caching  └──────────┐                  │
                          ▼                  ▼
                 G2-P1  Build123dRenderer (Option A)  ◄── needs G1-P2
                          │
                 G2-P2  hybrid routing + scene model  ◄── needs G1-P3
                          │
                 G2-P3  scad2b3d source transpiler (Option B)
                          │
G1-P5 / G2-P4   WASM browser stack (forward-looking)
```

---

## Track G1 — manifold3d in build123d

Design: [`design/design-manifold-in-build123d.md`](design/design-manifold-in-build123d.md).
Packaging throughout: an opt-in `build123d[manifold]` extra (per build123d issue
**#1228**); zero impact when not installed; pin `manifold3d >= 3.4, < 4`.

### G1-P1 — Free-function API + fast bake · *MVP* · ~2–3 wk · risk: low
The minimum genuinely useful thing, with **no `Shape`-hierarchy surgery**.
- `Solid.from_mesh(verts, tris)` — the `p7` direct-shell-assembly bake (core, no
  manifold3d dependency).
- `Shape.tessellate(weld=True)` — a welding option so meshes are manifold-valid (core).
- `mesh_fuse() / mesh_cut() / mesh_intersect()` — take `Shape`s, tessellate+weld, run
  the manifold boolean, return a `Shape` (in the `[manifold]` extra).
- Acceptance: the `p2` workloads run 20–100× faster through the new functions;
  results valid; STEP export works.
- Reuses: `p1` (weld), `p7` (`m2b.py`), `p2` (`bridge.py`).

### G1-P2 — `MeshPart` value type · ~2–3 wk · risk: low–med
The ergonomic surface for staying in mesh space across a CSG chain.
- `MeshPart` wrapping a `manifold3d.Manifold`: `+ - &`, primitives, `from_part()`,
  `to_solid()`, color via per-vertex properties, direct STL/3MF export.
- Explicit, never-implicit mesh↔BREP boundary; document the asymmetry in
  `native_part - mesh_part` operator interop (`p3` found it).
- Reuses: `p3` (`meshsolid.py`).

### G1-P3 — Face identity & selectors · ~3–4 wk · risk: medium
Make meshed results usable with build123d's selector-driven API.
- Tag inputs with `run_original_id`; after a boolean, group result triangles by tag.
- "Re-facer": rebuild **planar** tagged regions into real build123d `Face`s
  (region-grow + `UnifySameDomain`) so `.faces()` / `filter_by(Plane)` / `sort_by`
  work; expose a `faces_from(tag)` provenance selector.
- **Raise** on curved-surface selectors / fillet-after-mesh — never mis-answer.
- Reuses: `p8` (`refacer.py`, `b3d_manifold.py`).

### G1-P4 — Content-hash caching · ~1–2 wk · risk: low
- A content-hash–keyed in-memory cache of tessellations and boolean results
  (`FastKey`-style, per `research/08`). Meshes are cheap to hash/clone/serialize —
  this is what makes caching feasible where it is not for OCC `Shape`s.
- Optional: an opt-in on-disk cache under `~/.cache/build123d/` with LRU eviction.

### G1-P5 — WASM (forward-looking) · effort TBD · risk: med
- manifold3d compiles to WASM cleanly and is *small* (~0.5–1 MB vs OCP.wasm's ~22 MB)
  — a manifold-backed build123d is the lighter browser story.
- See `research/11`. Not blocking; revisit when a browser product is on the table.

---

## Track G2 — scad2py on build123d

Design: [`design/design-scad2py-build123d.md`](design/design-scad2py-build123d.md).

### G2-P0 — GPL remediation · *gates all of G2* · ~1 wk · risk: low (done in prototype)
Do this **first and independently** — it needs nothing else.
- Replace `scad2py/calc.py` + `scad2py/colors.py` with the `p6` clean-room modules
  (`fragments.py`, `transforms.py`, `colors.py`); update the ~10 call sites
  (rename list in `design/scad2py-gpl-remediation.md` §6).
- `git rm` the originals; `grep` confirms no GPL headers remain.
- Add an Apache-2.0 `LICENSE` + `NOTICE` to the scad2py repo; re-activate the
  `license=` field in `setup.py`.
- Carry `p6/test_equivalence.py` into scad2py's test suite.
- Reuses: `p6` (verified bit-exact, 3,308 cases).
- → A counsel sign-off on two well-settled questions is recommended before public
  distribution (see the remediation doc).

### G2-P1 — `Build123dRenderer` (Option A core) · ~3–5 wk · risk: medium
- A `csg.Node` → build123d `Shape` visitor, sibling to `ManifoldRenderer`, plugged in
  at scad2py's single `render_geom` seam.
- Cover the clean subset: 3D/2D primitives, booleans, transforms, color,
  `linear_extrude`; exact BREP + STEP export.
- Fix the four scad2py front-end bugs `p4` surfaced (incl. the `RotateExtrude`
  constructor and `Import.dim` bugs).
- Reuses: `p4` (`build123d_renderer.py`, `run.py`).

### G2-P2 — Hybrid routing + dimension-aware scene model · ~3–4 wk · risk: med–high
- A `BackendClassifier` that picks, per `csg` subtree, build123d-BREP vs the G1
  manifold backend; convert at the boundary. `minkowski` / 3D `hull` / non-planar
  `polyhedron` / large CSG → manifold.
- A dimension-aware scene model (`p4` found a flat mixed-2D/3D `Compound` flips the
  STEP volume sign).
- Reuse scad2py's `FastKey`/`CachingVisitor`; on the BREP side, gate caching by
  `is_reused` with an honest "drop if it doesn't pay" (OCC `Shape`s clone expensively).
- **Depends on G1-P2/P3.**

### G2-P3 — `scad2b3d` source transpiler (Option B) · ~2–3 wk · risk: low
- `.scad` → readable build123d Python source; lower-risk than Option A (reuses only
  the parser, may legitimately leave `# TODO`s).
- Emit `Compound(children=[...])` for implicit union (dodges the OCC O(n²) cliff and
  is more semantically faithful); honest `NotImplementedError` for `hull`/`minkowski`/twist.
- Factor the ~120-line runtime shim into an importable module.
- Reuses: `p5` (`scad2b3d.py`).

### Merge mechanics
Recommended: ship scad2py as a **sibling Apache-2.0 package that depends on
build123d**, not as code merged into build123d's tree — cleaner dependency direction,
independent release cadence. (Legal either way once G2-P0 is done.) Confirm upstream
build123d's contribution policy before proposing #1228 work.

---

## Suggested execution order

1. **G2-P0** (GPL fix) and **G1-P1** (manifold MVP) in parallel — both low-risk, both
   independent, both immediately valuable.
2. **G1-P2**, then **G1-P3** — completes the manifold backend as a usable feature; this
   is the natural point to open/contribute to build123d issue #1228.
3. **G2-P1**, then **G2-P2** — scad2py's runtime backend, now that the hybrid target
   exists.
4. **G2-P3** (source transpiler) and **G1-P4** (caching) — independent polish, any time
   after their prerequisites.
5. **G1-P5 / WASM** — when a browser product is actually wanted.

## Decisions needed from the owner

- **#1228 engagement** — implement the manifold extra as an upstream build123d
  contribution, or maintain it as a fork/separate package first?
- **scad2py home** — sibling package (recommended) vs merged into build123d.
- **scad2py outbound license** — Apache-2.0 is recommended (matches build123d); it is
  ochafik's call for his own code.
- **Counsel sign-off** — recommended on the GPL clean-room question before public
  distribution; mainstream-safe but worth getting in writing.
- **`$fn`/resolution policy** — keep exact BREP and only facet on export, vs facet
  eagerly to match OpenSCAD output. See `research/05`.

## Open questions parked for implementation time

Each design doc carries its own "Open questions" section; the notable cross-cutting
ones: how aggressively to expose face-provenance as a first-class selector dimension
(G1-P3); whether `MeshPart` should ever auto-bake for specific exporters; and whether
the manifold backend should also accelerate build123d's *own* booleans transparently
(tempting, but breaks the "never implicit" principle — left out by design).
