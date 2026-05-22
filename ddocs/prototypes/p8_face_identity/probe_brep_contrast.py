"""
probe_brep_contrast.py -- how OCC booleans PRESERVE face provenance,
for contrast with the mesh tag-and-rebuild approach.

Investigation point 4: contrast with how OCC booleans DO preserve face
provenance -- the History / Modified / Generated / IsDeleted API of
`BRepAlgoAPI_*`. Note what build123d users currently rely on.

The point: a BREP boolean does not RE-ATTRIBUTE faces, it PRESERVES the
actual `TopoDS_Face` objects. An input face untouched by the boolean comes
out as the SAME object (`IsSame()==True`); a modified one is reachable via
`History.Modified()`. build123d users get selector stability for FREE -- the
faces are still real analytic BREP faces with intact `geom_type`.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python probe_brep_contrast.py
"""
from build123d import Box, Cylinder, Pos, Axis, GeomType, Solid
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.TopTools import TopTools_ListOfShape


def hr(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def occ_cut_with_history(base, tool):
    """Run an OCC cut with history filling enabled; return (result, op)."""
    args = TopTools_ListOfShape()
    args.Append(base.wrapped)
    tools = TopTools_ListOfShape()
    tools.Append(tool.wrapped)
    op = BRepAlgoAPI_Cut()
    op.SetArguments(args)
    op.SetTools(tools)
    op.SetToFillHistory(True)
    op.Build()
    return Solid(op.Shape()), op


# --------------------------------------------------------------------------
hr("BREP CONTRAST 1 -- selectors keep working after a boolean, zero bookkeeping")
box = Box(20, 20, 10)
result = box - Cylinder(4, 30)
print(f"  box - cylinder (build123d native): {len(result.faces())} faces")
top = result.faces().sort_by(Axis.Z)[-1]
print(f"  result.faces().sort_by(Axis.Z)[-1]  -> 1 face, geom={top.geom_type}")
print(f"  filter_by(GeomType.CYLINDER)        -> "
      f"{len(result.faces().filter_by(GeomType.CYLINDER))} (the bore wall, "
      "still an ANALYTIC cylinder)")
print(f"  filter_by(GeomType.PLANE)           -> "
      f"{len(result.faces().filter_by(GeomType.PLANE))}")
print("  => the >Z selector AND the analytic CYLINDER type survive the")
print("     boolean with no tagging. The faces are still real BREP faces.")


# --------------------------------------------------------------------------
hr("BREP CONTRAST 2 -- an untouched face is the SAME TopoDS_Face object")
box = Box(20, 20, 10)
notch = Pos(0, 0, 5) * Box(4, 4, 4)   # a notch that does NOT reach the bottom
bottom_before = box.faces().sort_by(Axis.Z)[0]

result, op = occ_cut_with_history(box, notch)
print(f"  cut a notch in the top; the BOTTOM face is untouched.")
print(f"  History.IsDeleted(bottom)  -> {op.IsDeleted(bottom_before.wrapped)}")
print(f"  History.Modified(bottom)   -> "
      f"{op.Modified(bottom_before.wrapped).Size()} faces "
      "(0 == it was not reshaped)")
same = [f for f in result.faces()
        if f.wrapped.IsSame(bottom_before.wrapped)]
print(f"  result faces IsSame() as the original bottom face: {len(same)}")
print("  => an untouched face is LITERALLY the same TopoDS_Face after the")
print("     boolean. IsSame()==True. A mesh boolean CANNOT do this -- a mesh")
print("     has no faces, only triangles, and produces an all-new triangle set.")


# --------------------------------------------------------------------------
hr("BREP CONTRAST 3 -- History maps a MODIFIED face to its descendants")
box = Box(20, 20, 10)
bottom_before = box.faces().sort_by(Axis.Z)[0]
# this time the cylinder pierces all the way -> the bottom becomes an annulus
result, op = occ_cut_with_history(box, Cylinder(4, 30))
mod = op.Modified(bottom_before.wrapped)
gen = op.Generated(bottom_before.wrapped)
print(f"  cylinder pierces the box fully; the bottom face is reshaped.")
print(f"  History.IsDeleted(bottom)  -> {op.IsDeleted(bottom_before.wrapped)}")
print(f"  History.Modified(bottom)   -> {mod.Size()} descendant face(s) "
      "(the annulus the bottom became)")
print(f"  History.Generated(bottom)  -> {gen.Size()} newly-generated face(s)")
print("  => even when a face IS reshaped, OCC hands you an explicit map from")
print("     the input face to its output descendant(s). build123d does not")
print("     surface this API directly, but `_bool_op` (shape_core.py:2459)")
print("     could -- it is the BREP equivalent of run_original_id, and it is")
print("     EXACT (real faces), not a reconstructed approximation.")


# --------------------------------------------------------------------------
hr("WHAT build123d USERS CURRENTLY RELY ON")
print("""  build123d's whole selector UX assumes BREP face identity:

   * ShapeList.sort_by(Axis.Z)         picks the extreme FACE  (not triangle)
   * ShapeList.filter_by(GeomType.X)   needs an analytic surface type per face
   * ShapeList.filter_by(Plane.XY)     needs a real face normal + plane
   * ShapeList.group_by(...)           groups whole faces
   * .edges() / .vertices()            need exact topological edges/vertices
   * fillet(edges, r) / chamfer(...)   need an analytic edge shared by two
                                       analytic faces, plus a blend surface
   * RigidJoint / RevoluteJoint        are PLACED on a selected face/edge

  All of this is FREE in BREP mode: a boolean preserves or History-maps every
  face. None of it is free in mesh mode -- it must be reconstructed (planar
  faces: yes, via the re-facer; curved faces & fillets: no).""")

print("\n" + "=" * 72)
print("CONTRAST DONE -- BREP preserves face identity intrinsically (IsSame +")
print("History); the mesh path must REBUILD a weaker, partial notion of it.")
print("=" * 72)
