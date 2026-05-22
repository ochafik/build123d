"""Validity & downstream usability of the directly-assembled build123d Solid.

A fast reconstruction is worthless if the resulting Solid cannot be used.
This probe checks the direct-nofix output against real build123d operations:

  1. build123d-native validity: `Solid.is_valid`, `.is_manifold`
  2. `.faces()` / `.edges()` / `.vertices()` selection (ShapeList ops)
  3. boolean: `solid - other` via build123d operators
  4. STEP export + re-import round-trip (volume preserved?)
  5. winding robustness of the no-fix shortcut: deliberately break the mesh
     winding and confirm +fix recovers while no-fix degrades.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -u probe_validity.py
"""
import os
import tempfile

import manifold3d as m
from build123d import Box, export_step, import_step

from common import volume, is_valid, count_distinct, manifold_to_arrays, timed
from m2b import mesh_to_solid_direct, mesh_to_solid_direct_nofix


def section(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def probe_b3d_api(name, man):
    """Direct-nofix solid -> exercise build123d's own Shape API on it."""
    verts, tris = manifold_to_arrays(man)
    solid, info = mesh_to_solid_direct_nofix(verts, tris)
    print(f"\n[{name}]  {len(tris)} tris -> {type(solid).__name__} "
          f"(kind={info['result_kind']})")
    if solid is None:
        print("  reconstruction FAILED")
        return

    # build123d-native validity
    try:
        print(f"  Solid.is_valid     : {solid.is_valid}")
    except Exception as e:
        print(f"  Solid.is_valid     : raised {type(e).__name__}: {e}")
    try:
        print(f"  Solid.is_manifold  : {solid.is_manifold}")
    except Exception as e:
        print(f"  Solid.is_manifold  : raised {type(e).__name__}: {e}")

    # ShapeList selection
    try:
        faces = solid.faces()
        edges = solid.edges()
        verts_sel = solid.vertices()
        print(f"  .faces()/.edges()/.vertices(): {len(faces)} / {len(edges)} "
              f"/ {len(verts_sel)}")
    except Exception as e:
        print(f"  .faces() selection : raised {type(e).__name__}: {e}")

    # bounding box + volume via build123d
    try:
        bb = solid.bounding_box()
        print(f"  .bounding_box()    : "
              f"({bb.min.X:.2f},{bb.min.Y:.2f},{bb.min.Z:.2f}) .. "
              f"({bb.max.X:.2f},{bb.max.Y:.2f},{bb.max.Z:.2f})")
        print(f"  .volume            : {solid.volume:.4f} "
              f"(manifold ref {man.volume():.4f})")
    except Exception as e:
        print(f"  .bounding_box/.volume: raised {type(e).__name__}: {e}")


def probe_boolean(name, man):
    """Reconstruct, then run a build123d boolean against a native Box."""
    verts, tris = manifold_to_arrays(man)
    solid, _ = mesh_to_solid_direct_nofix(verts, tris)
    if solid is None:
        print(f"\n[{name}] reconstruction failed")
        return
    print(f"\n[{name}]  reconstructed solid - native Box(8,8,40):")
    cutter = Box(8, 8, 40)
    # reference: do the same cut in manifold space
    vc, tc = manifold_to_arrays(
        m.Manifold.cube([8, 8, 40], center=True))
    man_ref = (man - m.Manifold.cube([8, 8, 40], center=True)).volume()
    try:
        (cut, dt) = timed(lambda: solid - cutter)
        v = volume(cut)
        verr = abs(v - man_ref) / man_ref * 100 if man_ref else float("nan")
        print(f"  build123d boolean  : {dt*1e3:.1f} ms  "
              f"valid={is_valid(cut)}  vol={v:.4f}  "
              f"manifold ref={man_ref:.4f}  err={verr:.4f}%")
        print(f"  result faces       : {len(cut.faces())}")
    except Exception as e:
        print(f"  build123d boolean  : raised {type(e).__name__}: {e}")


def probe_step(name, man):
    """Reconstruct -> STEP export -> re-import -> check volume preserved."""
    verts, tris = manifold_to_arrays(man)
    solid, _ = mesh_to_solid_direct_nofix(verts, tris)
    if solid is None:
        print(f"\n[{name}] reconstruction failed")
        return
    path = os.path.join(tempfile.gettempdir(),
                        f"p7_{name.replace(' ', '_')}.step")
    try:
        _, t_w = timed(export_step, solid, path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        reimported, t_r = timed(import_step, path)
        v = reimported.volume
        verr = abs(v - man.volume()) / man.volume() * 100
        print(f"\n[{name}]  STEP round-trip:")
        print(f"  export   : {t_w*1e3:.1f} ms  {size/1024:.1f} KB")
        print(f"  re-import: {t_r*1e3:.1f} ms  valid={reimported.is_valid}  "
              f"faces={len(reimported.faces())}  vol={v:.4f}  err={verr:.4f}%")
    except Exception as e:
        print(f"\n[{name}]  STEP round-trip raised {type(e).__name__}: {e}")


def probe_winding(name, man):
    """No-fix path relies on consistent outward winding. Flip every 5th
    triangle and confirm: +fix recovers, no-fix produces a wrong result."""
    verts, tris = manifold_to_arrays(man)
    man_vol = man.volume()
    broken = tris.copy()
    broken[::5] = broken[::5][:, ::-1]
    print(f"\n[{name}]  winding robustness ({len(tris)} tris, "
          f"{len(tris[::5])} flipped):")
    for label, fn in [("no-fix ", mesh_to_solid_direct_nofix),
                       ("+fix   ", mesh_to_solid_direct)]:
        solid, _ = fn(verts, broken)
        if solid is None:
            print(f"  {label}: NO SOLID")
            continue
        try:
            v = volume(solid)
            verr = abs(v - man_vol) / man_vol * 100
        except Exception:
            v, verr = float("nan"), float("nan")
        print(f"  {label}: valid={is_valid(solid)!s:5s}  "
              f"vol={v:.4f}  err={verr:.4f}%")


def main():
    print("=== Validity & downstream-usability of direct-assembled Solids ===")

    section("1. build123d Shape API on the reconstructed Solid")
    probe_b3d_api("cube", m.Manifold.cube([10, 10, 10], center=True))
    probe_b3d_api("sphere", m.Manifold.sphere(10, circular_segments=48))
    probe_b3d_api("cube w/ hole",
                  m.Manifold.cube([20, 20, 10], center=True)
                  - m.Manifold.cylinder(20, 4, circular_segments=32)
                  .translate([0, 0, -10]))

    section("2. build123d boolean on a reconstructed Solid")
    probe_boolean("sphere", m.Manifold.sphere(12, circular_segments=48))
    probe_boolean("cylinder", m.Manifold.cylinder(30, 10, circular_segments=48))

    section("3. STEP export round-trip")
    probe_step("cube", m.Manifold.cube([10, 10, 10], center=True))
    probe_step("sphere", m.Manifold.sphere(10, circular_segments=48))

    section("4. winding robustness of the no-fix shortcut")
    print("(manifold3d guarantees consistent winding; what if it is broken?)")
    probe_winding("cube", m.Manifold.cube([10, 10, 10], center=True))
    probe_winding("sphere", m.Manifold.sphere(10, circular_segments=32))


if __name__ == "__main__":
    main()
