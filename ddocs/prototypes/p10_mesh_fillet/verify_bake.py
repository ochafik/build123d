"""p10 -- verify that a mesh-filleted result still bakes back to a build123d
Solid, and time both approaches.

The faceted fillet result has *new* triangle ids that the original side-map does
not cover, so faceID-grouped exact recovery (p9) does not apply. What this
checks is the honest fallback: the faceted result still imports as a valid
build123d Solid via the faceted bake -- so a mesh-filleted MeshPart is not a
dead end, it is just (correctly) a faceted Solid.
"""

from __future__ import annotations

import time

import numpy as np
import build123d.mesh as bm
from build123d import Box

from feature_graph import extract_feature_edges, build_chains, chains_by_pair
from explicit import selective_fillet, selective_chamfer
from sdf_approach import BoxSDF, selective_box_sdf, remesh_sdf


def main() -> None:
    print("=" * 78)
    print("VERIFY -- mesh-fillet result bakes back to a build123d Solid")
    print("=" * 78)

    base = bm.MeshPart.from_part(Box(20, 20, 20)).manifold
    rm = bm.bridge.read_result(base)
    chains = build_chains(
        extract_feature_edges(rm.vertices, rm.triangles, rm.face_id)
    )
    one = chains[0]

    t0 = time.perf_counter()
    fl = selective_fillet(base, rm.vertices, one, radius=4.0, segments=12)
    t_explicit = time.perf_counter() - t0

    # bake the faceted result back to a build123d Solid via from_mesh
    mp = bm.MeshPart(fl)
    vol_mesh = mp.volume
    solid = mp.to_solid(reconstruct=False)  # faceted bake (new ids, no recovery)
    print(f"  explicit fillet  -> Solid: is_valid={solid.is_valid} "
          f"faces={len(solid.faces())} mesh_vol={vol_mesh:.2f} "
          f"solid_vol={solid.volume:.2f}")

    # SDF result bake-back
    box_sdf = BoxSDF(center=[0, 0, 0], half=[10, 10, 10])
    t0 = time.perf_counter()
    man = remesh_sdf(
        selective_box_sdf(box_sdf, (0, 2), k=4.0, kind="fillet"),
        (-13, -13, -13, 13, 13, 13),
        edge_length=1.0,
    )
    t_sdf = time.perf_counter() - t0
    mp2 = bm.MeshPart(man)
    solid2 = mp2.to_solid(reconstruct=False)
    print(f"  SDF fillet       -> Solid: is_valid={solid2.is_valid} "
          f"faces={len(solid2.faces())} mesh_vol={mp2.volume:.2f} "
          f"solid_vol={solid2.volume:.2f}")

    print()
    print("  TIMING (single box edge, comparable fillet):")
    print(f"    explicit  inset+bridge boolean : {t_explicit*1000:7.1f} ms  "
          f"({fl.num_tri()} tris)")
    print(f"    SDF       level_set remesh     : {t_sdf*1000:7.1f} ms  "
          f"({man.num_tri()} tris)")
    print()
    print("  Note: the faceted fillet result carries NEW triangle ids the "
          "original\n  side-map does not cover -- so p9 exact faceID recovery "
          "does NOT apply.\n  A mesh fillet is a one-way faceted operation by "
          "construction.")


if __name__ == "__main__":
    main()
