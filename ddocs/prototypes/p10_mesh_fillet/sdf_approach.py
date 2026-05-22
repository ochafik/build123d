"""p10 -- approach (b): SDF rounding via manifold3d.level_set.

A fillet is, in implicit-modelling terms, a *smooth-minimum* blend. If a solid
is the union of two half-spaces ``A`` and ``B`` (the two faces meeting at the
edge), the sharp edge is ``min(dA, dB)``; replacing ``min`` with a *smooth* min
of radius r rounds that edge by exactly r. A chamfer is the same idea with a
"chamfer-min" (a clipped-plane blend) instead of a circular one.

The catch for a *selective* operation: the smooth-min must be applied **only
near the chosen feature edge**, otherwise every edge of the part rounds. So:

* build a global SDF for the part (here: exact analytic SDF for the test
  primitives -- a true generic mesh-SDF is itself a research problem, see NOTES);
* near the selected feature edge, blend the two adjacent face half-space SDFs
  with a smooth-min; far from it, keep the exact ``min``;
* remesh the whole field with ``manifold3d.level_set`` -> guaranteed manifold.

This file builds SDFs *compositionally* from the same primitives the test cases
use, so the "selective" part is honest: only the chosen edge's two half-spaces
get the smooth blend. It does not attempt to derive an SDF from an arbitrary
triangle soup -- that limitation is the headline finding for approach (b).
"""

from __future__ import annotations

import numpy as np
import manifold3d as m3d  # type: ignore


# ---------------------------------------------------------------------------
# smooth-min primitives
# ---------------------------------------------------------------------------


def smin_round(a: float, b: float, k: float) -> float:
    """Round (circular) smooth minimum -- produces a fillet of radius ~k."""
    if k <= 0.0:
        return min(a, b)
    h = max(k - abs(a - b), 0.0) / k
    return min(a, b) - h * h * k * 0.25


def smin_chamfer(a: float, b: float, k: float) -> float:
    """Chamfer smooth minimum -- produces a flat bevel of size ~k."""
    if k <= 0.0:
        return min(a, b)
    return min(min(a, b), (a - k + b) * np.sqrt(0.5))


# ---------------------------------------------------------------------------
# half-space / box SDFs (positive inside)
# ---------------------------------------------------------------------------


def halfspace_sdf(point: np.ndarray, plane_pt: np.ndarray, inward_normal: np.ndarray):
    """Signed distance to a half-space, positive on the inward-normal side."""
    return float(np.dot(point - plane_pt, inward_normal))


class BoxSDF:
    """Exact SDF of an axis-aligned box, positive inside.

    The box is treated as the intersection of 6 half-spaces; storing them
    individually lets the caller pick *which two* to smooth-blend (the selected
    edge) while keeping a hard ``min`` everywhere else.
    """

    def __init__(self, center: np.ndarray, half: np.ndarray):
        self.center = np.asarray(center, dtype=np.float64)
        self.half = np.asarray(half, dtype=np.float64)
        # 6 inward half-spaces: (plane_point, inward_normal)
        self.faces = []
        for axis in range(3):
            for sign in (+1.0, -1.0):
                normal = np.zeros(3)
                normal[axis] = -sign  # inward
                plane_pt = self.center.copy()
                plane_pt[axis] += sign * self.half[axis]
                self.faces.append((plane_pt, normal))

    def face_sdf(self, point: np.ndarray, face_index: int) -> float:
        plane_pt, normal = self.faces[face_index]
        return halfspace_sdf(point, plane_pt, normal)

    def __call__(self, x: float, y: float, z: float) -> float:
        """SDF evaluation -- accepts the (x, y, z) signature ``level_set`` uses."""
        p = np.array([x, y, z])
        return min(self.face_sdf(p, i) for i in range(6))


def selective_box_sdf(
    box: BoxSDF, edge_faces: tuple[int, int], k: float, kind: str = "fillet"
):
    """Return an SDF for ``box`` with ONE edge (face pair) rounded/chamfered.

    The two named faces are blended with a smooth-min of strength ``k``; the
    other four faces keep the hard ``min`` -- so exactly one edge changes.

    Args:
        box: the BoxSDF.
        edge_faces: indices of the two faces whose shared edge is selected.
        k: blend radius (fillet) or bevel size (chamfer).
        kind: "fillet" or "chamfer".

    Returns:
        a callable sdf(x, y, z) -> float for level_set.
    """
    blend = smin_round if kind == "fillet" else smin_chamfer
    fi, fj = edge_faces
    others = [i for i in range(6) if i not in edge_faces]

    def sdf(x: float, y: float, z: float) -> float:
        p = np.array([x, y, z])
        da = box.face_sdf(p, fi)
        db = box.face_sdf(p, fj)
        d_edge = blend(da, db, k)
        d_rest = min(box.face_sdf(p, i) for i in others)
        return min(d_edge, d_rest)

    return sdf


def selective_box_sdf_all_edges(box: BoxSDF, k: float, kind: str = "fillet"):
    """SDF for a box with ALL 12 edges rounded -- a smooth-min fold over faces."""
    blend = smin_round if kind == "fillet" else smin_chamfer

    def sdf(x: float, y: float, z: float) -> float:
        p = np.array([x, y, z])
        ds = [box.face_sdf(p, i) for i in range(6)]
        acc = ds[0]
        for d in ds[1:]:
            acc = blend(acc, d, k)
        return acc

    return sdf


# ---------------------------------------------------------------------------
# level-set remesh
# ---------------------------------------------------------------------------


def remesh_sdf(
    sdf, bounds: tuple[float, ...], edge_length: float, tolerance: float = -1.0
) -> m3d.Manifold:
    """Remesh an SDF into a Manifold via marching tetrahedra.

    Args:
        sdf: callable sdf(x, y, z) -> float, positive inside.
        bounds: (xmin, ymin, zmin, xmax, ymax, zmax) grid extent. Should pad the
            solid so the isosurface does not get clipped by the grid.
        edge_length: approx max triangle edge length -- controls grid density.
        tolerance: vertex-snap tolerance (-1 = interpolated crossing).

    Returns:
        manifold3d.Manifold: the remeshed solid.
    """
    return m3d.Manifold.level_set(
        sdf, list(bounds), edge_length, level=0.0, tolerance=tolerance
    )
