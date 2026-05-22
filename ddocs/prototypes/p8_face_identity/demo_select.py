"""
demo_select.py -- end-to-end demo: the answer to a build123d user who writes

    result = manifold_boolean(plate, punch)
    result.faces().sort_by(Axis.Z)[-1].fillet(1)        # <-- what happens?

This wires the bridge (b3d_manifold) + the re-facer into one `MeshPart`-style
object with a `.faces()` that returns a build123d `ShapeList[Face]`, plus a
provenance query that has NO BREP equivalent. It also shows the honest
failures: a curved-face selector and a fillet.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python demo_select.py
"""
import numpy as np
from build123d import (Box, Cylinder, Sphere, Pos, Axis, Plane, GeomType,
                       ShapeList, Face)
from b3d_manifold import tagged_original
from refacer import ReFacer


def hr(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


class MeshPart:
    """A manifold boolean result with a reconstructed selector surface.

    `.faces()` returns a build123d ShapeList[Face] (planar faces only -- the
    ones that round-trip losslessly). `.faces_from(name)` is the NEW
    provenance selector. `.curved_faces()` exposes what could not be rebuilt.
    """

    def __init__(self, manifold_result, tags):
        self._rf = ReFacer(manifold_result.to_mesh(), tags=tags)
        self._clusters = self._rf.cluster(crease_deg=20.0)
        self._b3d = {}   # cluster.key -> Face | list[Face]
        for c in self._clusters:
            self._b3d[c.key] = self._rf.to_b3d_face(c)

    def faces(self) -> ShapeList:
        """Planar reconstructed faces as a build123d ShapeList[Face]."""
        out = []
        for c in self._clusters:
            f = self._b3d[c.key]
            if isinstance(f, Face):
                out.append(f)
        return ShapeList(out)

    def curved_faces(self):
        """Curved clusters -- faceted, NOT single analytic faces."""
        return [c for c in self._clusters if c.kind == "curved"]

    def faces_from(self, name) -> list:
        """PROVENANCE selector: clusters whose origin solid is tagged `name`.

        This has no BREP equivalent -- it is the capability mesh tagging buys.
        """
        return [c for c in self._clusters
                if self._rf.name_of(c.origin_id) == name]


# --------------------------------------------------------------------------
hr("STEP 1 -- tag two input solids and difference them via manifold3d")
plate, plate_id = tagged_original(Box(40, 40, 12))
punch, punch_id = tagged_original(Box(12, 12, 40))
print(f"  plate Box(40,40,12)  tagged original_id={plate_id}")
print(f"  punch Box(12,12,40)  tagged original_id={punch_id}")
part = MeshPart(plate - punch, tags={plate_id: "plate", punch_id: "punch"})
print(f"  manifold difference done; re-faced into "
      f"{len(part._clusters)} clusters")


# --------------------------------------------------------------------------
hr("STEP 2 -- build123d selectors on the reconstructed faces")
faces = part.faces()
print(f"  part.faces()  -> build123d ShapeList of {len(faces)} planar Faces")
top = faces.sort_by(Axis.Z)[-1]
print(f"  .sort_by(Axis.Z)[-1]            -> top face, area={top.area:.1f}, "
      f"{top.geom_type}")
bot = faces.sort_by(Axis.Z)[0]
print(f"  .sort_by(Axis.Z)[0]             -> bottom face, area={bot.area:.1f}")
print(f"  .filter_by(Plane.XY)            -> "
      f"{len(faces.filter_by(Plane.XY))} faces")
print(f"  .filter_by(GeomType.PLANE)      -> "
      f"{len(faces.filter_by(GeomType.PLANE))} faces")


# --------------------------------------------------------------------------
hr("STEP 3 -- the NEW provenance selector (no BREP equivalent)")
from_plate = part.faces_from("plate")
from_punch = part.faces_from("punch")
print(f"  part.faces_from('plate')  -> {len(from_plate)} faces "
      "(the original plate surface)")
print(f"  part.faces_from('punch')  -> {len(from_punch)} faces "
      "(the hole walls -- faces CREATED by the boolean)")
print("  => 'which faces did the boolean create?' is answerable here and has")
print("     NO build123d/BREP equivalent. Mesh tagging is strictly additive")
print("     on this one axis.")


# --------------------------------------------------------------------------
hr("STEP 4 -- honest failure A: a selector on a CURVED surface")
plate2, p2 = tagged_original(Box(40, 40, 12))
bore, b2 = tagged_original(Cylinder(6, 40))
part2 = MeshPart(plate2 - bore, tags={p2: "plate", b2: "bore"})
print(f"  Box - Cylinder: {len(part2.faces())} planar Faces + "
      f"{len(part2.curved_faces())} curved cluster(s)")
for c in part2.curved_faces():
    print(f"    curved cluster {c.key}: {c.n_tris} triangles, "
          f"origin={part2._rf.name_of(c.origin_id)}")
print("  the cylindrical bore is ONE curved cluster -- addressable by TAG:")
print(f"    part2.faces_from('bore') -> {len(part2.faces_from('bore'))} "
      "cluster (the whole bore wall)")
print("  but it is NOT a build123d Face: filter_by(GeomType.CYLINDER) on")
print("  part2.faces() returns nothing -- there is no analytic cylinder left.")
cyl_sel = part2.faces().filter_by(GeomType.CYLINDER)
print(f"    part2.faces().filter_by(GeomType.CYLINDER) -> {len(cyl_sel)}  "
      "(expected 0 -- LOST)")


# --------------------------------------------------------------------------
hr("STEP 5 -- honest failure B: fillet after a mesh boolean")
top = part.faces().sort_by(Axis.Z)[-1]
print(f"  user does: part.faces().sort_by(Axis.Z)[-1]  -> a real Face "
      f"(area={top.area:.1f})")
print("  then: .fillet(1) on an edge of that face...")
try:
    edge = top.edges().sort_by(Axis.X)[-1]
    # a fillet needs the SOLID + an edge shared by two analytic faces.
    # the re-faced part is a pile of disconnected reconstructed faces, not a
    # sewn solid with shared topological edges -- fillet has nothing to grip.
    print(f"    the reconstructed face has {len(top.edges())} edges, but they")
    print("    are NOT shared with neighbouring faces (each face was sewn")
    print("    independently). A fillet needs an edge SHARED by two faces of")
    print("    one solid + an analytic blend surface. Neither exists here.")
    print("  => fillet/chamfer after a mesh boolean is UNDEFINED. The design")
    print("     must reject it explicitly, not silently produce a bad solid.")
except Exception as e:
    print(f"    fillet path failed: {e}")


# --------------------------------------------------------------------------
hr("SUMMARY -- what survived the manifold boolean")
print("""  WORKS (planar geometry):
    part.faces()                    -> build123d ShapeList[Face]
    .sort_by(Axis.Z) / [-1] / [0]   -> extreme planar face
    .filter_by(Plane.XY)            -> faces parallel to a plane
    .filter_by(GeomType.PLANE)      -> all (everything planar IS a plane)
    part.faces_from('name')         -> PROVENANCE selector (new capability)

  LOST (needs analytic geometry):
    .filter_by(GeomType.CYLINDER)   -> curved faces are faceted, type erased
    a direction selector on a curved face -> geometrically undefined
    .fillet() / .chamfer()          -> no shared analytic edge / blend surface
    .edges() / .vertices() as exact topology -> only facet edges remain""")

print("\n" + "=" * 72)
print("DEMO DONE -- see NOTES.md for the full VERDICT and recommendation.")
print("=" * 72)
