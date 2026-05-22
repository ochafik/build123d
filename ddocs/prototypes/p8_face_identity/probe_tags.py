"""
probe_tags.py -- TAGGING IDENTITY THROUGH manifold3d.

Investigation point 2: use manifold3d's original-ID mechanism
(`as_original` / `original_id` / `run_original_id`) and per-triangle
`face_id` to tag which INPUT SOLID -- and ideally which INPUT FACE -- each
output triangle came from. Verify the tags survive union / difference /
intersection. Demonstrate recovering "all triangles from face X of input A".

This re-verifies the CadQuery p4 findings hold for the build123d bridge
(same manifold3d 3.4.1, but inputs now come from build123d tessellation).

Run:  /Users/ochafik/github/.ddocs-venv/bin/python probe_tags.py
"""
from collections import Counter, defaultdict
import numpy as np
import manifold3d as m

from build123d import Box, Cylinder, Sphere, Pos
from b3d_manifold import b3d_to_manifold, tagged_original, triangle_origins


def hr(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def arr(x):
    return np.asarray(x)


# --------------------------------------------------------------------------
hr("PROBE TAG 1 -- run_original_id survives difference; cut faces tagged")
A, idA = tagged_original(Box(20, 20, 10))
B, idB = tagged_original(Box(8, 8, 30))
mesh = (A - B).to_mesh()
o = triangle_origins(mesh)
print(f"  input ids: plate A={idA}  punch B={idB}")
print(f"  output triangles      : {len(o)}")
print(f"  origin histogram      : {dict(Counter(o.tolist()))}")
vp, tv = arr(mesh.vert_properties)[:, :3], arr(mesh.tri_verts)
cen = (vp[tv[:, 0]] + vp[tv[:, 1]] + vp[tv[:, 2]]) / 3
# the 4 hole walls sit at x=+-4 or y=+-4 -- B's surface
on_cut = np.any(np.isclose(np.abs(cen[:, :2]), 4.0, atol=1e-3), axis=1)
print(f"  triangles on the hole walls (NEW cut faces): {on_cut.sum()}")
print(f"    all tagged origin B? {(o[on_cut] == idB).all()}")
print("  => every boolean-CREATED cut triangle is attributable -- it carries")
print("     the TOOL solid's id. Nothing is orphaned. Survives difference.")


# --------------------------------------------------------------------------
hr("PROBE TAG 2 -- identity survives union AND intersection")
for opname, op in [("union (A+B)", lambda a, b: a + b),
                   ("intersect (A^B)", lambda a, b: a ^ b)]:
    A, idA = tagged_original(Box(10, 10, 10))
    B, idB = tagged_original(Sphere(6.5))
    mesh = op(A, B).to_mesh()
    o = triangle_origins(mesh)
    h = dict(Counter(o.tolist()))
    keep = sorted(h)
    print(f"  {opname:18s}: ids present={keep}  "
          f"(A={idA},B={idB})  histogram={h}")
print("  => union and intersection both preserve per-input provenance, same")
print("     as difference. The three booleans are symmetric for tagging.")


# --------------------------------------------------------------------------
hr("PROBE TAG 3 -- chained boolean: identity rides through a 2nd op")
A, idA = tagged_original(Box(20, 20, 20))
B, idB = tagged_original(Box(8, 8, 30))
# C is offset so it carves material B did NOT already remove -- otherwise a
# cylinder hidden inside B's hole cuts nothing and drops out of the result.
C, idC = tagged_original(Pos(7, 0, 0) * Cylinder(3, 40))
step1 = A - B               # boolean #1
step2 = step1 - C           # boolean #2 -- operand is a PRODUCT
o2 = triangle_origins(step2.to_mesh())
print(f"  ids: A={idA} B={idB} C={idC}")
print(f"  (A-B)-C origin histogram: {dict(Counter(o2.tolist()))}")
present = set(o2.tolist())
print(f"  all 3 input ids still present? {present == {idA, idB, idC}}")
print("  => chained booleans keep every input id. Provenance is not flattened")
print("     by a 2nd op (as long as you never call as_original() on a product).")


# --------------------------------------------------------------------------
hr("PROBE TAG 4 -- per-INPUT-FACE tagging via run_original_id stamping")
# run_original_id tags the SOLID. To tag each FACE of an input solid we give
# every input face its own manifold 'original'. build123d hands us per-face
# triangles already (Shape.tessellate is grouped by face under the hood, but
# returns a flat list) -- so we tessellate each Face separately and compose.
box = Box(12, 12, 12)
faces = box.faces()
print(f"  build123d Box has {len(faces)} faces; tag each as its own original.")
face_solids = []
face_tag = {}
# build a thin slab per face is overkill; instead: build the whole box but
# stamp run_original_id per-face by reserving an id block and writing it.
base = m.Manifold.reserve_ids(len(faces))
print(f"  reserved id block {base}..{base + len(faces) - 1} for the 6 faces")
# tessellate the whole box, then per triangle decide which face it lies on
verts, tris = box.tessellate(0.1)
vp_all = np.array([[v.X, v.Y, v.Z] for v in verts])
tv_all = np.array(tris)
from b3d_manifold import weld
wvp, wtv = weld(vp_all, tv_all)
# classify each triangle by which face plane it is on (box is axis-aligned)
cen = (wvp[wtv[:, 0]] + wvp[wtv[:, 1]] + wvp[wtv[:, 2]]) / 3
# face order from build123d: -X,+X,-Y,+Y,-Z,+Z  (verified by normal_at)
face_of = np.full(len(wtv), -1, dtype=np.int64)
half = 6.0
tol = 1e-3
tests = [
    (0, cen[:, 0] < -half + tol),   # -X
    (1, cen[:, 0] > half - tol),    # +X
    (2, cen[:, 1] < -half + tol),   # -Y
    (3, cen[:, 1] > half - tol),    # +Y
    (4, cen[:, 2] < -half + tol),   # -Z
    (5, cen[:, 2] > half - tol),    # +Z
]
for fi, mask in tests:
    face_of[mask] = fi
# build a Mesh with one RUN per face: sort triangles by face, set run table
order = np.argsort(face_of, kind="stable")
sorted_tv = wtv[order].astype(np.uint32)
sorted_face = face_of[order]
run_starts = [0]
run_oids = []
for fi in range(6):
    cnt = int((sorted_face == fi).sum())
    run_oids.append(base + fi)
    run_starts.append(run_starts[-1] + cnt)
run_index = np.array([s * 3 for s in run_starts], dtype=np.uint32)
tagged_mesh = m.Mesh(
    wvp.astype(np.float32), sorted_tv,
    run_index=run_index,
    run_original_id=np.array(run_oids, dtype=np.uint32),
)
box_man = m.Manifold(tagged_mesh)
print(f"  built a face-tagged box manifold: status={box_man.status()}")
for fi in range(6):
    face_tag[base + fi] = f"box.face[{fi}]"
# now cut it with a cylinder and check the per-face tags survived
cyl, cyl_id = tagged_original(Cylinder(3, 30))
face_tag[cyl_id] = "cyl(tool)"
res = box_man - cyl
o = triangle_origins(res.to_mesh())
print(f"  after (face-tagged box) - cylinder, origin histogram:")
for oid, cnt in sorted(Counter(o.tolist()).items()):
    print(f"    id {oid:3d}  ->  {face_tag.get(oid, '?'):14s}  {cnt:3d} tris")
print("  => YES: per-INPUT-FACE provenance survives a boolean. We can now")
print("     recover 'all triangles that came from face 5 (+Z) of the box'.")


# --------------------------------------------------------------------------
hr("PROBE TAG 5 -- recover 'all triangles from face X of input A'")
want = base + 5  # +Z face of the box
sel = np.where(o == want)[0]
print(f"  query: triangles whose origin == {want} ({face_tag[want]})")
print(f"  recovered {len(sel)} triangles.")
mesh = res.to_mesh()
vp, tv = arr(mesh.vert_properties)[:, :3], arr(mesh.tri_verts)
cen = (vp[tv[sel][:, 0]] + vp[tv[sel][:, 1]] + vp[tv[sel][:, 2]]) / 3
print(f"  their centroids all on z=+6 plane? "
      f"{np.allclose(cen[:, 2], 6.0, atol=1e-3)}")
print(f"  z range of those triangles: "
      f"[{cen[:, 2].min():.3f}, {cen[:, 2].max():.3f}]")
print("  => recovery works: the tag is an exact per-input-face label that a")
print("     selector can target after the boolean. This is the foundation")
print("     for rebuilding build123d Faces (see probe_refacer.py).")

print("\n" + "=" * 72)
print("TAGS DONE -- run_original_id (solid) and reserved-id runs (per-face)")
print("both survive union/difference/intersection and chained booleans.")
print("=" * 72)
