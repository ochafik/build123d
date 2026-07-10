"""§K.49 open item, round 3 -- lead (b): direct 3-D triangle-triangle
self-intersection scan between DIFFERENT bores' fillet bands, on the
post-id-collision-fix mesh (grid 5, deterministically BRepCheck-invalid with
zero attributable subshape -- see round 1/2 findings and §K.50).

Each triangle of the *unseeded* (fillet-tool) region is assigned to its
nearest hole centre (in XY); candidate cross-bore triangle pairs are pruned
with a cKDTree on triangle centroids (only pairs within a tight radius can
possibly intersect, since bores are 9mm apart and each band only reaches
~2.5mm from its own centre); each candidate pair gets an exact 3-D
triangle-triangle intersection test (Moller's algorithm via trimesh if
available, else a hand-rolled separating-axis test).

Usage:
    PYTHONPATH=<worktree>/src <venv>/bin/python -u ddocs/design/scale_invalidity_diag_v3.py
"""
import itertools
import time

import numpy as np
from scipy.spatial import cKDTree

import ddocs.design.scale_invalidity_diag_v2 as d2


def log(msg):
    print(msg, flush=True)


def tri_tri_intersect(t1, t2, eps=1e-9):
    """Exact 3-D triangle-triangle intersection test (Moller 1997 style,
    via signed-distance-to-plane + interval overlap). Returns True if the
    (closed) triangles intersect (touching at a shared point/edge counts as
    intersecting -- callers must exclude candidate pairs that are legitimately
    adjacent/shared-edge before calling this, if that distinction matters).
    """
    def plane(tri):
        n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        return n, -np.dot(n, tri[0])

    n1, d1 = plane(t1)
    n2, d2_ = plane(t2)

    # Signed distances of t2's verts to t1's plane.
    dist2 = np.dot(t2, n1) + d1
    if np.all(dist2 > eps) or np.all(dist2 < -eps):
        return False
    dist1 = np.dot(t1, n2) + d2_
    if np.all(dist1 > eps) or np.all(dist1 < -eps):
        return False

    # Coplanar-ish case: fall back to a 2-D separating-axis test projected
    # onto the dominant axis of n1.
    if np.linalg.norm(np.cross(n1, n2)) < eps:
        axis = np.argmax(np.abs(n1))
        axes = [a for a in range(3) if a != axis]
        p1 = t1[:, axes]
        p2 = t2[:, axes]

        def edges(poly):
            return [(poly[i], poly[(i + 1) % 3]) for i in range(3)]

        def project(poly, axis_vec):
            vals = poly @ axis_vec
            return vals.min(), vals.max()

        for poly_a, poly_b in ((p1, p2), (p2, p1)):
            for a, b in edges(poly_a):
                edge_vec = b - a
                axis_vec = np.array([-edge_vec[1], edge_vec[0]])
                if np.linalg.norm(axis_vec) < eps:
                    continue
                min_a, max_a = project(poly_a, axis_vec)
                min_b, max_b = project(poly_b, axis_vec)
                if max_a < min_b - eps or max_b < min_a - eps:
                    return False
        return True

    # General 3-D case: intersection line of the two planes; test whether the
    # two triangles' overlap intervals along that line intersect.
    line_dir = np.cross(n1, n2)

    def interval_on_line(tri, n_other, d_other, dist_other):
        # Parametrize each triangle edge crossing the OTHER plane; find the
        # two intersection points with the shared line, project onto line_dir.
        pts = []
        for i in range(3):
            a, b = tri[i], tri[(i + 1) % 3]
            da, db = dist_other[i], dist_other[(i + 1) % 3]
            if da * db < 0:
                t = da / (da - db)
                pts.append(a + t * (b - a))
            elif abs(da) < eps:
                pts.append(a)
        if len(pts) < 2:
            return None
        vals = [np.dot(p, line_dir) for p in pts]
        return min(vals), max(vals)

    i1 = interval_on_line(t1, n2, d2_, dist1)
    i2 = interval_on_line(t2, n1, d1, dist2)
    if i1 is None or i2 is None:
        return False
    return not (i1[1] < i2[0] - eps or i2[1] < i1[0] - eps)


def main():
    grid_n = 5
    pitch = 9.0
    span = (grid_n - 1) * pitch
    start = -span / 2.0
    hole_centers = np.array(
        [
            [start + i * pitch, start + j * pitch]
            for i in range(grid_n)
            for j in range(grid_n)
        ]
    )
    log(f"grid {grid_n}: {len(hole_centers)} hole centres, pitch={pitch}")

    result, holes = d2.build_case(grid_n)
    raw_solids, faces_by_component, face_tags, face_tri, face_fid, result_mesh_w, topology = (
        d2.tagged_recover(result)
    )
    log(f"recovered {len(raw_solids)} solid(s)")

    # Pull every faceted-unseeded (fillet-tool) triangle's centroid + verts.
    tri_idx = []
    for fid_key, tag in face_tags.items():
        pass
    # face_tri maps id(face) -> source triangle index for faceted builds;
    # face_tags maps id(face) -> tag string. Walk faces_by_component directly.
    unseeded_tris = []
    for lst in faces_by_component.values():
        for f in lst:
            tag = face_tags.get(id(f))
            if tag == "faceted-unseeded":
                ti = face_tri.get(id(f))
                if ti is not None:
                    unseeded_tris.append(ti)
    unseeded_tris = np.array(sorted(set(unseeded_tris)))
    log(f"{len(unseeded_tris)} faceted-unseeded (fillet-tool) triangles")

    tri_verts = result_mesh_w.triangles[unseeded_tris]
    tri_coords = result_mesh_w.vertices[tri_verts]  # (N, 3, 3)
    centroids = tri_coords.mean(axis=1)[:, :2]  # XY only, for nearest-hole assignment

    # Nearest hole centre per triangle.
    hole_tree = cKDTree(hole_centers)
    _, nearest_hole = hole_tree.query(centroids)

    n_bands = len(set(nearest_hole.tolist()))
    log(f"triangles span {n_bands} distinct nearest-hole bands")

    # Candidate cross-band pairs: prune with a 3-D cKDTree on centroids,
    # radius generous enough to catch any real near-touch (bore reach ~2.5mm,
    # so anything within 1.0mm of a DIFFERENT band's triangle is suspicious;
    # pitch is 9mm so this cannot accidentally span 3+ bores).
    centroids_3d = tri_coords.mean(axis=1)
    tree = cKDTree(centroids_3d)
    t0 = time.time()
    pairs = tree.query_pairs(r=1.0, output_type="ndarray")
    log(f"{len(pairs)} candidate close triangle pairs (radius=1.0mm) in {time.time()-t0:.1f}s")

    cross_band_pairs = pairs[nearest_hole[pairs[:, 0]] != nearest_hole[pairs[:, 1]]]
    log(f"{len(cross_band_pairs)} of those are CROSS-band (different nearest hole)")

    if len(cross_band_pairs) == 0:
        log("LEAD (b) VERDICT: no cross-band triangle pairs even come within "
            "1.0mm of each other -- no candidate for genuine 3-D "
            "self-intersection between distinct fillet bands.")
        return

    n_intersecting = 0
    examples = []
    for a, b in cross_band_pairs:
        # Skip pairs sharing a vertex (legitimately adjacent, not a genuine
        # cross-band collision) -- shouldn't occur across different nearest-
        # hole bands, but guard anyway.
        va, vb = set(tri_verts[a].tolist()), set(tri_verts[b].tolist())
        if va & vb:
            continue
        if tri_tri_intersect(tri_coords[a], tri_coords[b]):
            n_intersecting += 1
            if len(examples) < 5:
                examples.append((int(unseeded_tris[a]), int(unseeded_tris[b]),
                                  int(nearest_hole[a]), int(nearest_hole[b])))

    log(f"LEAD (b) VERDICT: {n_intersecting} genuinely-intersecting cross-band "
        f"triangle pairs found (out of {len(cross_band_pairs)} candidates).")
    for ex in examples:
        log(f"  example: triangle {ex[0]} (band {ex[2]}) x triangle {ex[1]} (band {ex[3]})")


if __name__ == "__main__":
    main()
