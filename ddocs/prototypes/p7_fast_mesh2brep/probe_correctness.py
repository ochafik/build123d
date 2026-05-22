"""Correctness check: do all three approaches yield a valid build123d Solid
with the right volume? Small meshes (cube, lo-res sphere, CSG results).

Also checks the Euler characteristic V-E+F of the reconstructed BREP: for a
closed genus-0 triangle mesh it must be 2, and E must equal 3F/2.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python probe_correctness.py
"""
import manifold3d as m

from common import (volume, area, is_valid, count_distinct,
                    manifold_to_arrays, timed)
from m2b import (mesh_to_solid_sewing, mesh_to_solid_direct,
                 mesh_to_solid_direct_nofix)


def check(name, man):
    verts, tris = manifold_to_arrays(man)
    man_vol = man.volume()
    print(f"\n[{name}]  {len(verts)} verts, {len(tris)} tris  "
          f"manifold vol={man_vol:.4f}  genus={man.genus()}")

    for label, fn in [("A sewing       ", mesh_to_solid_sewing),
                       ("B direct+fix   ", mesh_to_solid_direct),
                       ("B direct nofix ", mesh_to_solid_direct_nofix)]:
        (solid, info), dt = timed(fn, verts, tris)
        if solid is None:
            print(f"  {label}: NO SOLID  ({dt*1e3:.1f} ms)  info={info}")
            continue
        nf = count_distinct(solid, "face")
        ne = count_distinct(solid, "edge")
        nv = count_distinct(solid, "vertex")
        valid = is_valid(solid)
        try:
            vol = volume(solid)
            verr = abs(vol - man_vol) / man_vol * 100 if man_vol else float("nan")
        except Exception:
            vol, verr = float("nan"), float("nan")
        fails = ""
        if info.get("face_fail"):
            fails += f" face_fail={info['face_fail']}"
        if info.get("edge_fail"):
            fails += f" edge_fail={info['edge_fail']}"
        euler = nv - ne + nf
        print(f"  {label}: {dt*1e3:7.1f} ms  kind={info['result_kind']:6s} "
              f"F={nf:5d} E={ne:5d} V={nv:5d}  V-E+F={euler:+d}  "
              f"valid={valid!s:5s}  vol={vol:.4f} err={verr:.4f}%{fails}")


def main():
    print("=== mesh -> build123d Solid correctness ===")

    check("cube", m.Manifold.cube([10, 10, 10], center=True))
    check("sphere lo-res", m.Manifold.sphere(10, circular_segments=32))
    check("sphere mid-res", m.Manifold.sphere(10, circular_segments=64))
    check("sphere - cube (CSG)",
          m.Manifold.sphere(10, circular_segments=48)
          - m.Manifold.cube([8, 8, 20], center=True))
    check("cylinder", m.Manifold.cylinder(20, 8, circular_segments=48))
    check("cube with through-hole (genus 1)",
          m.Manifold.cube([20, 20, 10], center=True)
          - m.Manifold.cylinder(20, 4, circular_segments=32).translate([0, 0, -10]))


if __name__ == "__main__":
    main()
