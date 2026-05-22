"""p10 -- measurement helpers: validity, volume, triangle quality."""

from __future__ import annotations

import numpy as np
import manifold3d as m3d  # type: ignore


def triangle_quality(manifold: m3d.Manifold) -> dict:
    """Compute triangle-quality stats for a Manifold.

    Two distinct things are measured -- they must not be conflated:

    * **shape quality** -- the radius ratio in [0,1] (1 = equilateral). A faceted
      fillet *legitimately* produces long thin strip triangles with a low radius
      ratio; they are valid, just not equilateral. So a low ``q_min`` alone is
      not a defect.
    * **degeneracy** -- a triangle whose area is a tiny fraction of the mean.
      *These* are the real problem: near-zero-area slivers from a boolean cut
      grazing a face. Reported as ``degenerate``.
    """
    mesh = manifold.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    if len(tris) == 0:
        return dict(min=0.0, mean=0.0, degenerate=0, n=0)
    p = verts[tris]  # (T,3,3)
    a = np.linalg.norm(p[:, 1] - p[:, 0], axis=1)
    b = np.linalg.norm(p[:, 2] - p[:, 1], axis=1)
    c = np.linalg.norm(p[:, 0] - p[:, 2], axis=1)
    s = (a + b + c) / 2.0
    area = np.sqrt(np.maximum(s * (s - a) * (s - b) * (s - c), 0.0))
    denom = a * b * c * (a + b + c)
    quality = np.where(denom > 1e-18, 16.0 * area * area / denom, 0.0)
    mean_area = area.mean() if len(area) else 0.0
    # degenerate = area < 0.1% of mean triangle area (a genuine sliver)
    degenerate = int(np.sum(area < max(mean_area * 1e-3, 1e-12)))
    return dict(
        min=float(quality.min()),
        mean=float(quality.mean()),
        degenerate=degenerate,
        n=int(len(tris)),
    )


def report(name: str, manifold: m3d.Manifold, expected_vol: float | None = None) -> dict:
    """Print and return a one-line health report for a result manifold."""
    valid = manifold.status() == m3d.Error.NoError and not manifold.is_empty()
    vol = manifold.volume() if not manifold.is_empty() else 0.0
    q = triangle_quality(manifold)
    genus_parts = manifold.decompose()
    line = (
        f"  {name:34s} valid={valid!s:5s} vol={vol:11.3f} "
        f"tris={q['n']:6d} q_min={q['min']:.3f} q_mean={q['mean']:.3f} "
        f"degen={q['degenerate']:4d} bodies={len(genus_parts)}"
    )
    if expected_vol is not None:
        err = abs(vol - expected_vol)
        line += f" volerr={err:.3f}"
    print(line)
    return dict(name=name, valid=valid, volume=vol, quality=q,
               bodies=len(genus_parts))
