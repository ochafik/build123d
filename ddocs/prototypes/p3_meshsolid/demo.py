"""
demo.py -- end-to-end exercise of the MeshSolid prototype.

Run with the project venv:
    /Users/ochafik/github/.ddocs-venv/bin/python demo.py

It:
  1. builds a couple of build123d BREP parts and converts them to MeshSolid;
  2. runs a chain of fast booleans that would be slow/fragile in pure OCC
     (a 6x6x6 grid of spheres subtracted from a block -> 216 coincident-ish
     booleans), and times it against the native OCC equivalent;
  3. keeps a color through the boolean chain;
  4. exports STL directly from the mesh;
  5. converts one mesh result back to a build123d BREP Solid and STEP-exports.
"""

import os
import time

import build123d as bd
from build123d import Box, Cylinder, Sphere, Color, Pos, Rotation

from meshsolid import MeshSolid, mesh_fuse, mesh_cut

HERE = os.path.dirname(os.path.abspath(__file__))


def banner(text):
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)


# --------------------------------------------------------------------------
banner("1. Build build123d BREP parts, convert to MeshSolid")

native_block = Box(40, 40, 20)
native_block.color = Color("steelblue")
print(f"  native build123d Box: {native_block!r}, color={native_block.color}")

block = MeshSolid.from_build123d(native_block)
print(f"  -> MeshSolid (tessellated): {block!r}")

# A primitive built directly in mesh space (mirrors build123d's Sphere).
mesh_sphere = MeshSolid.sphere(8, segments=48, color=Color("orange"))
print(f"  MeshSolid.sphere primitive: {mesh_sphere!r}")

# Construction straight from raw (vertices, triangles) -- an unwelded soup,
# welded automatically by Mesh.merge().
verts = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
         (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
raw = MeshSolid.from_mesh(verts, tris)
print(f"  MeshSolid.from_mesh (raw cube): {raw!r}")


# --------------------------------------------------------------------------
banner("2. A chain of fast booleans -- the perf point of the exercise")

# 216 spheres in a grid, all subtracted from one block. In pure OCC this is
# 216 sequential BRepAlgoAPI_Cut + ShapeUpgrade cleans on a growing solid,
# with lots of near-coincident geometry -- the classic perf cliff.
N = 6
spacing = 6.0
offset = -(N - 1) * spacing / 2

t0 = time.time()
drill = MeshSolid.box(60, 60, 30, color=Color("steelblue"))
cutters = []
for i in range(N):
    for j in range(N):
        for k in range(2):
            cutters.append(
                MeshSolid.sphere(3.2, segments=24)
                .translate((offset + i * spacing,
                            offset + j * spacing,
                            -6 + k * 12)))
# Fuse all cutters in one batch pass, then a single subtract.
all_cutters = MeshSolid.fuse_all(cutters)
result = drill - all_cutters
# Force evaluation by touching geometry (manifold ops are lazy).
v = result.volume
t_mesh = time.time() - t0
print(f"  MeshSolid: {len(cutters)} spheres fused + subtracted in "
      f"{t_mesh*1000:.1f} ms")
print(f"  result: {result!r}")

# Same operation in native OCC build123d, for comparison.
t0 = time.time()
try:
    occ_block = Box(60, 60, 30)
    occ_cutters = []
    for i in range(N):
        for j in range(N):
            for k in range(2):
                occ_cutters.append(
                    Pos(offset + i * spacing,
                        offset + j * spacing,
                        -6 + k * 12) * Sphere(3.2))
    occ_result = occ_block
    for c in occ_cutters:
        occ_result = occ_result - c
    _ = occ_result.volume
    t_occ = time.time() - t0
    print(f"  native OCC: same {len(occ_cutters)} subtractions in "
          f"{t_occ*1000:.1f} ms")
    print(f"  speedup: {t_occ / t_mesh:.1f}x")
except Exception as exc:  # OCC can outright fail on coincident geometry
    print(f"  native OCC: FAILED ({type(exc).__name__}: {exc})")


# --------------------------------------------------------------------------
banner("3. Color carried through a boolean chain")

red_box = MeshSolid.box(20, 20, 20, color=Color("red"))
blue_sphere = (MeshSolid.sphere(12, segments=48, color=Color("blue"))
               .translate((10, 0, 0)))

combined = red_box + blue_sphere
print(f"  red box + blue sphere -> dominant color: {combined._dominant_color()}")

# Mixed operand: MeshSolid minus a *native* build123d Part (auto-converted).
native_drill = Pos(0, 0, 0) * Cylinder(5, 40)
holed = combined - native_drill
print(f"  combined - native Cylinder (auto-tessellated): {holed!r}")
print(f"  color survives the mixed-operand cut: {holed._dominant_color()}")


# --------------------------------------------------------------------------
banner("4. Export STL directly from the mesh (cheap)")

stl_path = os.path.join(HERE, "demo_result.stl")
result.export_stl(stl_path)
size = os.path.getsize(stl_path)
print(f"  wrote {stl_path}  ({size} bytes)")

tmf_path = os.path.join(HERE, "demo_colored.3mf")
holed.export_3mf(tmf_path)
print(f"  wrote {tmf_path}  ({os.path.getsize(tmf_path)} bytes)")


# --------------------------------------------------------------------------
banner("5. Convert a MeshSolid result back to a build123d BREP Solid")

t0 = time.time()
bd_solid = holed.to_solid()
t_bake = time.time() - t0
print(f"  to_solid() bake took {t_bake*1000:.1f} ms")
print(f"  -> build123d {type(bd_solid).__name__}: "
      f"is_valid={bd_solid.is_valid}, "
      f"volume={bd_solid.volume:.2f} (mesh said {holed.volume:.2f}), "
      f"faces={len(bd_solid.faces())}, color={bd_solid.color}")

# The BREP solid is now a first-class build123d citizen: STEP export works.
step_path = os.path.join(HERE, "demo_baked.step")
bd.export_step(bd_solid, step_path)
print(f"  STEP-exported the baked solid: {step_path} "
      f"({os.path.getsize(step_path)} bytes)")

# And it round-trips back into the algebra API.
further = bd_solid - Box(50, 5, 50)
print(f"  baked solid further booleaned in native OCC: "
      f"volume={further.volume:.2f}")


banner("demo complete")
