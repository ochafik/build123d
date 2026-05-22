"""Coplanar-merge post-pass: ShapeUpgrade_UnifySameDomain collapses adjacent
coplanar facets into single faces. A faceted BREP from a mesh has thousands
of tiny triangle faces; merging coplanar ones gives a saner topology for
.faces() selection and smaller STEP files. Measures reduction + time + cost
on flat vs curved geometry.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python -u probe_unify.py
"""
import manifold3d as m

from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

from build123d import Solid

from common import volume, is_valid, count_distinct, manifold_to_arrays, timed
from m2b import mesh_to_solid_direct_nofix


def unify(solid):
    """Run UnifySameDomain (merge coplanar faces + collinear edges) and return
    a build123d Solid wrapping the result."""
    up = ShapeUpgrade_UnifySameDomain(solid.wrapped, True, True, False)
    up.Build()
    from OCP.TopoDS import TopoDS
    shp = up.Shape()
    return Solid(TopoDS.Solid_s(shp))


def probe(name, man):
    verts, tris = manifold_to_arrays(man)
    solid, info = mesh_to_solid_direct_nofix(verts, tris)
    if solid is None:
        print(f"[{name}] reconstruction failed")
        return
    f0 = count_distinct(solid, "face")
    e0 = count_distinct(solid, "edge")
    v0 = volume(solid)

    try:
        merged, dt = timed(unify, solid)
        f1 = count_distinct(merged, "face")
        e1 = count_distinct(merged, "edge")
        v1 = volume(merged)
        valid = is_valid(merged)
        verr = abs(v1 - v0) / v0 * 100 if v0 else float("nan")
        print(f"[{name}]  ({len(tris)} tris)")
        print(f"  before unify : faces={f0:6d} edges={e0:6d}")
        print(f"  after  unify : faces={f1:6d} edges={e1:6d}  ({dt*1e3:.1f} ms)"
              f"  -> {100*(1-f1/f0):.1f}% fewer faces")
        print(f"  valid={valid}  vol {v0:.4f} -> {v1:.4f}  (err {verr:.4f}%)")
    except Exception as e:
        print(f"[{name}] unify raised {type(e).__name__}: {e}")
    print()


def main():
    print("=== Coplanar-merge post-pass (ShapeUpgrade_UnifySameDomain) ===\n")
    probe("cube (flat)", m.Manifold.cube([10, 10, 10], center=True))
    probe("cylinder (2 flat caps + faceted wall)",
          m.Manifold.cylinder(20, 8, circular_segments=48))
    probe("cube with through-hole",
          m.Manifold.cube([20, 20, 10], center=True)
          - m.Manifold.cylinder(20, 4, circular_segments=48)
          .translate([0, 0, -10]))
    probe("sphere (fully curved -- expect ~no merge)",
          m.Manifold.sphere(10, circular_segments=64))
    probe("sphere minus box (CSG, large flat cuts)",
          m.Manifold.sphere(12, circular_segments=64)
          - m.Manifold.cube([10, 10, 30], center=True))


if __name__ == "__main__":
    main()
