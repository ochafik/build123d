# `build123d[manifold]` — a fast, robust mesh-CSG backend

Implements [#1228](https://github.com/gumyr/build123d/issues/1228) — an **optional** `manifold3d`-backed mesh-CSG path for build123d. Off by default, zero impact when the extra isn't installed; opt in with `pip install build123d[manifold]`.

## Why

build123d's booleans route through OpenCASCADE's `BOPAlgo`. That kernel is exact and irreplaceable for analytic modelling — but its booleans scale **super-linearly** on many-operand and chained CSG. A 1 000-hole iterated drill *does not finish in 120 s*; the same job on `manifold3d` takes ~0.5 s. manifold3d booleans are **20–130× faster** and scale ~linearly, and it offers operations OCC has no implementation for at all — **3D convex hull, Minkowski sum, level-set/SDF**.

This PR adds that as a *parallel tier*, not a replacement. OCC stays the default and only kernel for analytic surfaces, fillets/chamfers on B-rep, lofts/sweeps/offsets, and STEP/IGES. The manifold tier handles bulk/chained CSG and the ops OCC lacks — and, crucially, hands geometry **back** to a real, filletable B-rep wherever the result is planar.

## The headline design decision

> **manifold3d enters as a standalone value type, `MeshPart` — NOT a `Shape` subclass, and NOT an implicit `.wrapped` swap.** Every crossing of the mesh ↔ B-rep boundary is an explicit, named verb.

A `Shape` is by contract an exact-B-rep entity (analytic faces, fillet, STEP). A faceted mesh is none of those. Making `MeshPart` an `isinstance(x, Shape)` would be a lie that propagates into every consumer. A sibling type keeps the capability boundary honest and statically visible. `Shape.__init__` also hard-wires `downcast()` to `TopoDS`, so a polymorphic `.wrapped` would be deep, risky core surgery for an optional feature — out of scope by design.

## What's in it

A new `build123d.mesh` sub-package (10 modules, ~8 100 lines) plus small, self-contained additions to `topology/`.

**The value type & CSG** (`mesh_part.py`, `ops.py`)
- `MeshPart` — wraps a `manifold3d.Manifold` **plus a faceID provenance side-map**. `+ - &` operators and `mesh_fuse` / `mesh_cut` / `mesh_intersect` free functions (N-way in one `batch_boolean`). Stay in mesh space across a whole chain; pay the B-rep bake at most once, at the end.
- Capabilities OCC lacks: `mesh_hull`, `mesh_minkowski` (+ `_difference`), and native mesh-domain `mesh_extrude` / `mesh_revolve` / `mesh_offset` / `mesh_shell`.

**The OUT leg** (`bridge.py`) — `Shape → seeded Manifold`
- Per-face tessellation, a grid-snap **vertex weld** (OCC tessellates faces independently, so shared-edge vertices must be merged or manifold3d rejects the soup as non-manifold), and **systematic faceID seeding**: every triangle gets a globally-unique id tracing it to its originating `TopoDS_Face`, with that face's exact surface recorded in a `SideMap`. The id survives every boolean — this is what makes recovery possible.

**The IN leg** (`recovery.py`) — `seeded Manifold → analytic Solid`
- faceID-grouped reconstruction over a **shared topology**: one `TopoDS_Vertex` per result-mesh vertex and one shared `TopoDS_Edge` per mesh-edge, so a planar face and an adjacent faceted patch reference the *same* edges and the shell is **valid by construction**.
- For each **planar** group: an **exact** `TopoDS_Face` on the *known input* `Geom_Plane`, boundary vertices projected onto that plane (erasing tessellation jitter). **For an all-planar CSG result the recovered B-rep is bit-exact to the native boolean** — same face/edge counts, exact volume, and `BRepFilletAPI` fillet/chamfer run on it for real.
- **Curved** groups (round bores, spheres, revolves) are kept faceted: faceID *identifies* the surface, but exact re-trim of a known cylinder/sphere is out of scope (see Non-goals). **hull / Minkowski / `from_mesh`** operands get *synthetic* faceIDs so they group into one faceted region rather than shattering per-triangle.
- Mixed planar+curved results stay **valid AND keep their exact planar faces** — no all-faceted fallback.

**Mesh-domain finishing** (`feature_edges.py`, `fillet.py`, `corners.py`)
- `mesh_fillet` / `mesh_chamfer` operate directly on a `MeshPart` — for the case where the deliverable is a *faceted* edge-break (deburring a perforated panel, breaking honeycomb cell edges) and round-tripping through B-rep is pure cost. Per-chain swept-tool construction, per-edge convex/concave sign splitting, ACIS-style multi-chain corner blends, variable-radius and `on_infeasible='skip'` graceful batching. Infeasible requests raise `MeshFilletInfeasible` (never silently clamp).

**Core additions** (`topology/`, reusable beyond the extra)
- `Shape.tessellate(weld=True)` — a welded, indexed tessellation.
- `Solid.from_mesh(vertices, triangles)` — direct `TopoDS_Shell` assembly, ~19 k tri/s, linear (the inverse of #835; avoids the super-linear per-triangle `BRepBuilderAPI_Sewing` path).
- A `NotImplemented`-returning protocol on `Shape.__add__/__sub__/__and__` so mixed `Shape`/`MeshPart` operator expressions resolve cleanly.

## Performance (measured)

| workload | OCC B-rep | manifold tier |
|---|--:|--:|
| 1 000 iterated holes | DNF (>120 s) | ~0.5 s |
| many-operand cut / chained union | super-linear | 20–130×, ~linear |
| all-planar CSG → recover_brep | (native) | bit-exact, filletable |
| 1 800-chain panel deburr (`mesh_fillet`) | structurally hard | ~30 s, ~linear |

Recovery and fillet were both profiled and de-quadratic'd (shared-topology recovery; a cKDTree radius query + a shared `(vertex,face_id)→normal` index in the fillet pre-flight). The bp17 1 800-chain deburr went from ~18 min to ~30 s.

## Packaging — opt-in, zero footprint when absent

```toml
[project.optional-dependencies]
manifold = ["manifold3d >= 3.4, < 4"]
```

`import build123d` never imports `manifold3d`. With the extra absent, `build123d.mesh` raises a clear, actionable `ImportError`; nothing else changes. `manifold3d` ships no-compile wheels for all platforms and has no required runtime deps (both Apache-2.0).

## Tests

`tests/test_mesh.py` — **167 tests** (~2 750 lines), `pytest.importorskip("manifold3d")` so the suite is skipped wholesale when the extra is absent. Covers: import isolation, weld, `Solid.from_mesh`, the CSG operators/free-functions, faceID seeding + survival, exact planar recovery (bit-exact volume, analytic `PLANE` faces, real fillet/chamfer on recovered edges), mixed-provenance validity, hull/Minkowski/primitives, the native ops, feature-edge chains, and the mesh fillet/chamfer paths incl. infeasibility and skip-mode.

## Non-goals / known limits

- **No exact re-trim of curved surfaces (Tier C).** A round bore recovers faceted; faceID makes the curved case *well-posed* (the surface is known) but the re-fit of boundary curves + p-curves is a separate research effort.
- **`MeshPart` is not a `Shape`** and does not pretend to be — fillet/chamfer/STEP on a faceted mesh are undefined and raise rather than mis-answer.
- **The mesh↔B-rep boundary is always explicit** — no implicit coercion hides a minutes-long bake or a fidelity loss behind attribute access.

## Design docs

Full rationale and the prototype trail live in `ddocs/` and are **kept in-tree for history**: `design-manifold-in-build123d.md` (the opinionated design), `algorithms.md` (a code-anchored reference for all 55 algorithms §A–§J), `mesh-fillet-engineering.md`, and the `ddocs/prototypes/p1–p10` measurement series. A reviewer who wants only the code can read `src/build123d/mesh/**`, the four touched `topology/` files, the `pyproject` extra, and `tests/test_mesh.py`; the `ddocs/` tree is the why behind each decision.

## Files

Shippable code: `src/build123d/mesh/**` (10 modules), `src/build123d/topology/{shape_core,three_d,utils,composite}.py` (the reusable core additions), the `manifold` extra in `pyproject.toml`, and `tests/test_mesh.py` (167 tests). Design/working material (kept for history): `ddocs/**`.

---

*Draft. Scope is the `feat/manifold-mesh-backend` branch from its divergence at `d5290b1e`.*
