# Draft reply for gumyr/build123d#1228 — DO NOT POST without ochafik's review

> Context: gumyr's last comment (2026-02-08): *"This seems like a huge can of
> worms - how would uses know the boundary between manifold objects vs. occt
> objects? I would expect that users would have all kinds of expectations that
> couldn't be met."* The thread has been dormant since. This reply answers the
> boundary question with the working design, offers evidence, and proposes
> low-commitment paths (split PRs or a separate package) so the maintainer
> never has to swallow an 8k-line drop.

---

@gumyr I think the boundary question is *the* right question, and I'd like to
offer a concrete answer — I've built a working implementation of this feature
to find out where the can of worms actually is, and the boundary turned out to
be expressible in the type system rather than in user expectations.

**Design: the mesh world is a separate value type, not a `Shape`.**

- `MeshPart` is a small immutable value type that wraps a
  `manifold3d.Manifold`. It is deliberately **not** a `Shape` subclass: you
  can't pass it to a fillet, a `BuildPart`, an exporter, or anything else that
  expects OCCT topology. There is no implicit mixing anywhere — a user can't
  *accidentally* cross the boundary, so there's no expectation to disappoint.
- Crossing is always explicit, in both directions:
  - `MeshPart.from_part(shape, deflection=...)` — OCCT → mesh (tessellation,
    obviously lossy for curved faces, and documented as such);
  - `mesh_part.to_solid()` / `recover_brep(mesh_part)` — mesh → OCCT, which
    returns the solid **plus a `RecoveryResult` report** stating exactly which
    faces came back as exact analytic planes and which remain faceted patches.
    Nothing is silently approximated: the user is told, per face group, what
    they got.
- Face provenance (manifold3d's `faceID`s, seeded at tessellation time)
  survives booleans, so after `mesh_a - mesh_b` we know which output triangles
  came from which original face. For planar faces the recovery is **exact**
  (same plane, re-bounded), and a planar face adjacent to a faceted patch
  shares its `TopoDS_Edge`s with it, so mixed results are valid B-rep by
  construction — that was the hardest part, and it works.

So the user-facing contract is short: *inside `build123d.mesh` everything is a
mesh and booleans are manifold3d's (fast, robust, guaranteed-manifold output);
the moment you want B-rep semantics you call `to_solid()` and get an honest
report of what is exact vs faceted.* That's the whole boundary.

**Why bother:** the payoff is exactly the workflow @jdegenstein described
(grafting parametric geometry onto an STL), plus raw scale on
tessellation-tolerant work. One data point: a 900-hole perforated panel —
the boolean itself is ~0.2 s in the mesh domain, and even an all-edges
mesh-domain deburr chamfer of all 1 812 bore edges completes in ~30 s.
And unlike the OpenSCAD STL-import experience mentioned in the OP, manifold3d
refuses non-manifold input loudly instead of failing silently.

The implementation lives on a branch of my fork
(`ochafik/build123d@feat/manifold-mesh-backend`): optional
`build123d[manifold]` extra, no hard dependency, ~10 modules under
`build123d/mesh/`, 167 tests, docs. Installable today:

```
pip install 'build123d[manifold] @ git+https://github.com/ochafik/build123d.git@feat/manifold-mesh-backend'
```

I'm deliberately **not** opening a PR of that size uninvited. In fact, I've
since verified the whole backend runs as a **standalone `build123d-mesh`
package against stock PyPI build123d** (same 169-test suite, all passing) —
so the zero-burden outcome for this repo is fully on the table: this issue
could close with a pointer to a companion package, and build123d itself never
carries a line of it.

The one thing I'd offer upstream either way is tiny and independent of
meshes: `Shape.__add__/__sub__/__and__` currently *raise* on an unrecognized
operand type, where returning `NotImplemented` would let Python try the
reflected operator — that's what lets `Box(...) - some_foreign_type` work for
*any* third-party interop type, not just this one. It's a ~15-line,
behavior-preserving change (it only alters cases that already raise
TypeError) and I'd be happy to PR just that.

If instead there *is* appetite for it in-tree, I'd propose a short series of
reviewable PRs (`MeshPart` + conversions first, booleans second, recovery
third) rather than one drop. Happy with any of these outcomes; mostly I
wanted to report that the boundary problem has a clean answer before this
issue gets decided on the assumption that it doesn't.
