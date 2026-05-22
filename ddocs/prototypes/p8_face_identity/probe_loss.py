"""
probe_loss.py -- QUANTIFY THE LOSS.

What investigation point 1 asks: take a build123d Box, tessellate it, do a
manifold boolean, convert back, and SHOW that the 6 named faces become N
triangles with no identity.

This probe runs build123d's *own* selector API (`.faces()`, `sort_by`,
`filter_by(GeomType)`, `filter_by(Plane)`) on:
  (a) a native build123d BREP boolean       -- the baseline
  (b) the same boolean routed through manifold3d and sewn back to a Solid

and prints the side-by-side numbers. Nothing is asserted; this is evidence.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python probe_loss.py
"""
from build123d import Box, Cylinder, Axis, GeomType, Plane

from b3d_manifold import tagged_original, manifold_to_b3d_solid


def hr(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def report(name, solid):
    """Run build123d's selector API on a solid and print what works."""
    faces = solid.faces()
    planar = faces.filter_by(GeomType.PLANE)
    cyl = faces.filter_by(GeomType.CYLINDER)
    print(f"  [{name}]")
    print(f"    .faces()                       -> {len(faces)} faces")
    print(f"    .filter_by(GeomType.PLANE)      -> {len(planar)} planar")
    print(f"    .filter_by(GeomType.CYLINDER)   -> {len(cyl)} cylindrical")
    try:
        top = faces.sort_by(Axis.Z)[-1]
        print(f"    .faces().sort_by(Axis.Z)[-1]    -> 1 face, "
              f"area={top.area:.2f}, geom={top.geom_type}")
    except Exception as e:
        print(f"    .faces().sort_by(Axis.Z)[-1]    -> FAILED: {e}")
    try:
        xy = faces.filter_by(Plane.XY)
        print(f"    .filter_by(Plane.XY)            -> {len(xy)} faces")
    except Exception as e:
        print(f"    .filter_by(Plane.XY)            -> FAILED: {e}")
    print(f"    .edges()                        -> {len(solid.edges())} edges")
    print(f"    .vertices()                     -> "
          f"{len(solid.vertices())} vertices")


# --------------------------------------------------------------------------
hr("PROBE LOSS 1 -- a plain Box: 6 named faces vs N triangle-faces")
box = Box(10, 10, 10)
print("  build123d Box(10,10,10) -- the IDEAL: 6 analytic planar faces.")
report("build123d BREP Box", box)

man, _ = tagged_original(box)
sewn = manifold_to_b3d_solid(man.to_mesh())
print()
print("  Same box, tessellated -> manifold3d -> sewn back to a Solid:")
report("manifold round-trip", sewn)
print()
print("  LOSS: a Box's 6 logical faces become "
      f"{len(sewn.faces())} triangle-faces. Each triangle is its OWN")
print("  analytic PLANE (geom_type still says PLANE -- misleadingly), so")
print("  filter_by(GeomType.PLANE) returns every triangle. .sort_by(Axis.Z)")
print("  picks ONE arbitrary triangle of the top, not the whole top face.")


# --------------------------------------------------------------------------
hr("PROBE LOSS 2 -- a real boolean: Box - Cylinder")
box = Box(20, 20, 10)
cyl = Cylinder(4, 30)

brep = box - cyl
print("  (a) NATIVE build123d BREP boolean  box - cylinder:")
report("BREP boolean", brep)

man_a, ida = tagged_original(Box(20, 20, 10))
man_b, idb = tagged_original(Cylinder(4, 30))
mesh = (man_a - man_b).to_mesh()
sewn = manifold_to_b3d_solid(mesh)
print()
print("  (b) SAME boolean via manifold3d, sewn back:")
report("manifold boolean", sewn)
print()
print("  LOSS SUMMARY:")
print(f"    BREP    : {len(brep.faces())} faces "
      f"({len(brep.faces().filter_by(GeomType.PLANE))} planar, "
      f"{len(brep.faces().filter_by(GeomType.CYLINDER))} cylindrical)")
print(f"    manifold: {len(sewn.faces())} faces "
      f"({len(sewn.faces().filter_by(GeomType.PLANE))} planar, "
      f"{len(sewn.faces().filter_by(GeomType.CYLINDER))} cylindrical)")
print("    -> the cylindrical bore wall is GONE as an analytic face: it is")
print("       now a fan of flat triangles. geom_type==CYLINDER count drops")
print("       to 0. The 7-face BREP result becomes a triangle soup.")
print()
print("  This is the loss. probe_tags.py shows what is RECOVERABLE.")
