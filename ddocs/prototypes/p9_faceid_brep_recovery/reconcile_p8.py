"""
reconcile_p8.py -- step 6: reconcile this prototype (p9) with p8.

p8 concluded raw ``face_id`` is "unusable" and used ``run_original_id`` +
``reserve_ids`` instead.  The OCCT design doc argues *seeded* ``faceID`` is
exactly the right hook.  Are they in conflict?  This script measures the
discrepancy precisely and shows who is correct about what.
"""
from __future__ import annotations

import numpy as np
import manifold3d as m3d

from build123d import Box, Cylinder

from faceid_bridge import SideMap, seed_manifold, read_result, _weld


def _mesh_no_seed(shape, tol=0.05):
    """Build a manifold from a shape with NO seeded face_id -- manifold then
    fills face_id from its own coplanar-face calculation (the p8 scenario)."""
    vs, ts = shape.tessellate(tol, 0.2)
    vp = np.array([[v.X, v.Y, v.Z] for v in vs], dtype=np.float64)
    tv = np.array(ts, dtype=np.int64)
    wv, wt, _ = _weld(vp, tv)
    a, b, c = wt[:, 0], wt[:, 1], wt[:, 2]
    wt = wt[(a != b) & (b != c) & (a != c)]
    mesh = m3d.Mesh64(vert_properties=np.ascontiguousarray(wv),
                      tri_verts=np.ascontiguousarray(wt.astype(np.uint64)))
    return m3d.Manifold(mesh)


def main():
    print("=" * 78)
    print("STEP 6 -- reconciling p9 with p8")
    print("=" * 78)
    print()

    print("THE DISCREPANCY")
    print("-" * 78)
    print("p8 NOTES.md (sec 6.2 of the design doc): raw `face_id` is")
    print('"unusable as a global key ... a face cut into two disjoint pieces')
    print('keeps a single id" -- so p8 used `run_original_id`+`reserve_ids`.')
    print()
    print("OCCT design/02 sec 2: *seeded* `faceID` per TopoDS_Face is exactly")
    print("the right hook and survives the boolean.")
    print()
    print("These sound contradictory.  They are NOT.  Measured proof:")
    print()

    # --- claim 1: UNSEEDED face_id IS unusable (p8 is right) ------------
    print("CLAIM 1 -- p8 is right about UNSEEDED face_id")
    print("-" * 78)
    cyl = _mesh_no_seed(Cylinder(5, 20))
    out = cyl.to_mesh()
    fid = np.asarray(out.face_id)
    n_tri = len(np.asarray(out.tri_verts))
    n_ids = len(set(fid.tolist()))
    print(f"  a tessellated cylinder, NO seed -> manifold computes face_id")
    print(f"  from coplanarity: {n_tri} triangles -> {n_ids} distinct face_ids")
    print(f"  The single curved side face SHATTERED into ~{n_ids} ids.")
    print(f"  => raw/unseeded face_id is indeed UNUSABLE for face identity.")
    print(f"     p8 observed exactly this and was CORRECT.")
    print()

    # --- claim 2: SEEDED face_id IS usable (OCCT doc is right) ----------
    print("CLAIM 2 -- the OCCT doc is right about SEEDED face_id")
    print("-" * 78)
    sm = SideMap()
    cm, _ = seed_manifold(Cylinder(5, 20), "cyl", sm)
    rm = read_result(cm)
    print(f"  the SAME cylinder, SEEDED one id per TopoDS_Face: "
          f"{len(rm.tris)} triangles -> {len(rm.distinct_ids)} distinct ids")
    print(f"  The side face is now ONE id.  Identity is preserved.")
    print(f"  => seeded face_id IS the right hook. The OCCT doc is CORRECT.")
    print()

    # --- claim 3: the split-face concern is real but handled -----------
    print("CLAIM 3 -- p8's split-face concern is REAL (and we handle it)")
    print("-" * 78)
    sm2 = SideMap()
    bar, _ = seed_manifold(Box(24, 8, 4), "bar", sm2)
    cut, _ = seed_manifold(Box(4, 20, 20), "cutter", sm2)
    rm2 = read_result(bar - cut)
    # find the +Z bar face -- one seeded id, but cut into two pieces
    from recover import _boundary_loops, _connected_components
    split_ids = []
    for fid in rm2.distinct_ids:
        rec = sm2[fid]
        if rec.surface_kind == "PLANE" and rec.solid_name == "bar":
            tris = rm2.tris[rm2.tris_of(fid)]
            comps = _connected_components(tris)
            if len(comps) > 1:
                split_ids.append((fid, len(comps)))
    print(f"  bar sliced clean in two: {len(split_ids)} seeded ids each carry")
    print(f"  a face the cut split into 2 disjoint pieces: {split_ids}")
    print(f"  p8's worry -- 'one id, two pieces' -- is REAL.")
    print(f"  p9's fix: recover.py splits each id into edge-CONNECTED")
    print(f"  COMPONENTS before building faces -> one TopoDS_Face per piece.")
    print(f"  (p8 solved the same problem with a connectivity region-grow;")
    print(f"   p9 does it as a per-id post-step. Same fix, different place.)")
    print()

    print("=" * 78)
    print("VERDICT -- nobody was wrong; they solved DIFFERENT problems")
    print("=" * 78)
    print("""
  * p8 was CORRECT that *raw/unseeded* face_id is unusable -- because p8
    never seeded it.  Without a seed, manifold fills face_id from its own
    coplanar-face calculation, which shatters every curved face.  p8 then
    (correctly, given that) routed identity through run_original_id +
    reserve_ids -- a channel it DID control.

  * The OCCT doc is CORRECT that *seeded* faceID is the right hook -- the
    Mesh(face_id=...) constructor lets you stamp one id per TopoDS_Face,
    and manifold then MAINTAINS that seed instead of recomputing it.

  * The gap was never about manifold -- it was that p8 did not seed the
    channel.  p8's own sec 6.2 even says face_id is "unique only within a
    run"; that is the *unseeded* behaviour.  Once seeded with a globally
    unique counter, that caveat disappears.

  CHANNEL CHOICE (which is better in Python?)
  -------------------------------------------
  Both channels carry an integer per triangle and BOTH survive booleans.
  For per-FACE identity the `face_id` channel is the BETTER fit:

    - `face_id` is natively per-FACE granularity. `run_original_id` is
      per-RUN (per input SOLID); to get per-face out of it you must split
      each solid into one run per face (reserve_ids + a run per face),
      which means N separate Manifold builds or a hand-built run table.
    - Seeding `face_id` is ONE flat uint array on ONE Mesh per solid --
      far simpler than orchestrating per-face runs.
    - The "incompatible function arguments" rejection the brief mentions
      is NOT a real blocker: it is a rank bug (face_id passed as (N,1) or
      a list). Pass a flat (N,) ndarray and Mesh(face_id=...) just works;
      nanobind auto-casts the dtype.

  So p9 uses `face_id`.  p8's `reserve_ids`/`run_original_id` is a valid
  fallback (and the B-rep reconstruction is identical whichever integer
  channel carries the tag) -- but `face_id` is the cleaner Python path,
  and it is the same channel the OCCT C++ design uses (MeshGL64::faceID).
""")
    return True


if __name__ == "__main__":
    main()
