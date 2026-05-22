"""Measure the manifold3d -> OCC Solid 'bake' cost as a function of triangle
count, so RESULTS.md can report the back-conversion scaling honestly.

The bake (`bridge.manifold_to_solid`) builds one planar TopoDS_Face per
triangle and feeds them all to a single BRepBuilderAPI_Sewing -- the only
real mesh->Solid path build123d has (mirrors `Mesher._get_shape`). It is the
leg that dominates a manifold pipeline if you insist on a BREP result.

Run:  python bake_scaling.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import manifold3d as mf

from bridge import manifold_to_solid


def main():
    # spheres at increasing segment counts -> increasing triangle counts
    rows = []
    for segs in [16, 24, 32, 48, 64, 96, 128]:
        man = mf.Manifold.sphere(10, segs)
        tris = man.num_tri()
        t0 = time.perf_counter()
        solid, info = manifold_to_solid(man)
        dt = time.perf_counter() - t0
        per_tri_us = dt / tris * 1e6
        rows.append({"tris": tris, "t_bake": dt, "us_per_tri": per_tri_us,
                     "result": info.get("result")})
        print(f"  tris={tris:7d}  bake={dt:8.3f}s  {per_tri_us:6.1f} us/tri  "
              f"-> {info.get('result')}", flush=True)

    out = Path(__file__).parent / "bake_scaling.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
