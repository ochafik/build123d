"""

build123d mesh backend tests

name: test_mesh.py

desc: Unit tests for the build123d.mesh manifold3d backend:
      Solid.from_mesh, Shape.tessellate(weld=True), the free-function CSG API
      (mesh_fuse / mesh_cut / mesh_intersect), MeshPart CSG operators and
      transforms, systematic faceID seeding, and faceID-grouped exact B-rep
      reconstruction.

license:

    Copyright 2026 Gumyr

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

"""

import sys
import time
from math import pi

import numpy as np
import pytest

from build123d import (
    Axis,
    Box,
    Compound,
    Cone,
    Cylinder,
    Face,
    GeomType,
    Location,
    Part,
    Plane,
    Pos,
    ShapeList,
    Solid,
    SortBy,
    Sphere,
    Torus,
)

# manifold3d is an optional extra; skip the whole module if it is absent. The
# build123d.mesh imports below must therefore follow this importorskip.
manifold3d = pytest.importorskip("manifold3d")

# pylint: disable=wrong-import-position
from build123d.mesh import (  # noqa: E402
    MeshPart,
    SideMap,
    is_available,
    mesh_cut,
    mesh_fuse,
    mesh_hull,
    mesh_intersect,
    mesh_minkowski,
    mesh_minkowski_difference,
    recover_brep,
)
from build123d.mesh.bridge import read_result, shape_to_manifold  # noqa: E402

# --------------------------------------------------------------------------
# Packaging / import-isolation
# --------------------------------------------------------------------------


def test_import_build123d_does_not_import_manifold3d():
    """`import build123d` must not pull in manifold3d (the optional extra)."""
    # The mesh tests themselves import manifold3d, so check what a fresh
    # interpreter loads instead of inspecting this process's sys.modules.
    code = (
        "import sys, build123d; "
        "assert 'manifold3d' not in sys.modules, 'manifold3d leaked into core'"
    )
    import subprocess  # pylint: disable=import-outside-toplevel

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_is_available():
    """is_available reports True when the extra is installed."""
    assert is_available() is True


# --------------------------------------------------------------------------
# Core addition #1 - Shape.tessellate(weld=True)
# --------------------------------------------------------------------------


def test_tessellate_weld_dedups_box_to_eight_vertices():
    """A welded box mesh has 8 vertices, not the 24-vertex per-face soup."""
    box = Box(10, 10, 10)

    soup_vertices, soup_triangles = box.tessellate(0.5)
    welded_vertices, welded_triangles = box.tessellate(0.5, weld=True)

    assert len(soup_vertices) == 24
    assert len(welded_vertices) == 8
    # Welding only merges vertices; the triangle count is unchanged.
    assert len(welded_triangles) == len(soup_triangles) == 12


def test_tessellate_weld_default_is_unchanged():
    """tessellate(weld=False) keeps the historical per-face soup behaviour."""
    box = Box(10, 10, 10)
    default_vertices, default_triangles = box.tessellate(0.5)
    explicit_vertices, explicit_triangles = box.tessellate(0.5, weld=False)
    assert len(default_vertices) == len(explicit_vertices) == 24
    assert default_triangles == explicit_triangles


# --------------------------------------------------------------------------
# Core addition #2 - Solid.from_mesh
# --------------------------------------------------------------------------


def test_from_mesh_box_is_valid_with_correct_volume():
    """Solid.from_mesh rebuilds a box: valid, exact volume."""
    box = Box(10, 10, 10)
    vertices, triangles = box.tessellate(0.5, weld=True)
    solid = Solid.from_mesh(vertices, triangles)
    assert isinstance(solid, Solid)
    assert solid.is_valid
    assert solid.volume == pytest.approx(1000.0, rel=1e-6)


def test_from_mesh_sphere_is_valid_with_close_volume():
    """Solid.from_mesh rebuilds a faceted sphere: valid, volume close."""
    sphere = Sphere(5)
    vertices, triangles = sphere.tessellate(0.1, weld=True)
    solid = Solid.from_mesh(vertices, triangles)
    assert isinstance(solid, Solid)
    assert solid.is_valid
    # Faceting under-fills the sphere slightly.
    assert solid.volume == pytest.approx(4 / 3 * pi * 5**3, rel=0.05)


def test_from_mesh_multi_body_compound_yields_compound():
    """A mesh of two disjoint bodies rebuilds into a valid Compound."""
    compound = Compound(children=[Box(4, 4, 4), Box(4, 4, 4).moved(Pos(20, 0, 0))])
    vertices, triangles = compound.tessellate(0.5, weld=True)
    result = Solid.from_mesh(vertices, triangles)
    assert isinstance(result, Compound)
    assert result.is_valid
    assert len(result.solids()) == 2
    assert result.volume == pytest.approx(128.0, rel=1e-6)


def test_from_mesh_solid_with_void_keeps_the_void():
    """A hollow box rebuilds into a single Solid that keeps its internal void."""
    hollow = Box(20, 20, 20) - Box(8, 8, 8)
    vertices, triangles = hollow.tessellate(0.5, weld=True)
    result = Solid.from_mesh(vertices, triangles)
    assert isinstance(result, Solid)
    assert result.is_valid
    assert result.volume == pytest.approx(20**3 - 8**3, rel=1e-6)


def test_from_mesh_rejects_empty_mesh():
    """Solid.from_mesh raises on an empty triangle array."""
    with pytest.raises(ValueError):
        Solid.from_mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=int))


# --------------------------------------------------------------------------
# The bridge - R1: un-welded soup must fail loudly
# --------------------------------------------------------------------------


def test_bridge_welds_and_produces_valid_manifold():
    """shape_to_manifold tessellates+welds into a valid, non-empty Manifold."""
    manifold, side_map = shape_to_manifold(Box(10, 10, 10))
    assert manifold.status() == manifold3d.Error.NoError
    assert not manifold.is_empty()
    assert manifold.volume() == pytest.approx(1000.0, rel=1e-6)
    # A box is seeded per face: 6 face records.
    assert len(side_map) == 6


def test_bridge_r1_unwelded_soup_is_not_manifold():
    """R1: a raw un-welded soup imports as NotManifold with zero volume.

    This is the silent-failure mode the bridge's mandatory weld prevents.
    """
    vertices, triangles = Box(10, 10, 10).tessellate(0.5)  # 24-vertex soup
    vertex_array = np.ascontiguousarray(
        [(v.X, v.Y, v.Z) for v in vertices], dtype=np.float64
    )
    triangle_array = np.ascontiguousarray(triangles, dtype=np.uint64)
    soup = manifold3d.Manifold(
        manifold3d.Mesh64(vert_properties=vertex_array, tri_verts=triangle_array)
    )
    assert soup.status() == manifold3d.Error.NotManifold
    assert soup.volume() == 0.0


def test_bridge_r1_meshpart_from_part_welds_so_it_does_not_fail():
    """MeshPart.from_part routes through the welding bridge, so it succeeds."""
    mesh_part = MeshPart.from_part(Box(10, 10, 10))
    assert mesh_part.is_valid
    assert mesh_part.volume == pytest.approx(1000.0, rel=1e-6)


# --------------------------------------------------------------------------
# MeshPart
# --------------------------------------------------------------------------


def test_meshpart_from_part_queries():
    """MeshPart exposes volume, area, bounding box and validity."""
    mesh_part = MeshPart.from_part(Box(20, 10, 4))
    assert mesh_part.is_valid
    assert mesh_part.volume == pytest.approx(800.0, rel=1e-6)
    assert mesh_part.area == pytest.approx(2 * (200 + 80 + 40), rel=1e-6)
    bbox = mesh_part.bounding_box()
    assert bbox.size.X == pytest.approx(20.0, rel=1e-6)
    assert bbox.size.Y == pytest.approx(10.0, rel=1e-6)
    assert bbox.size.Z == pytest.approx(4.0, rel=1e-6)


def test_meshpart_from_mesh_round_trips():
    """MeshPart.from_mesh wraps raw arrays and Solid.from_mesh bakes them."""
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    assert wrapped.is_valid
    assert wrapped.volume == pytest.approx(512.0, rel=1e-6)


def test_meshpart_to_solid_and_to_part():
    """MeshPart bakes to a valid build123d Solid and Part."""
    mesh_part = mesh_cut(Box(20, 20, 10), Cylinder(3, 20))
    solid = mesh_part.to_solid()
    assert isinstance(solid, Solid)
    assert solid.is_valid
    assert solid.volume == pytest.approx(mesh_part.volume, rel=1e-6)
    part = mesh_part.to_part()
    assert part.volume == pytest.approx(mesh_part.volume, rel=1e-6)


def test_meshpart_export_stl(tmp_path):
    """MeshPart.export_stl writes a non-empty STL file without a BREP bake."""
    mesh_part = mesh_cut(Box(20, 20, 10), Sphere(5))
    stl_path = tmp_path / "part.stl"
    assert mesh_part.export_stl(stl_path) is True
    assert stl_path.exists()
    assert stl_path.stat().st_size > 0


def test_meshpart_rejects_non_manifold_payload():
    """MeshPart() rejects a payload that is not a manifold3d.Manifold."""
    with pytest.raises(TypeError):
        MeshPart(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Free-function CSG API - correctness
# --------------------------------------------------------------------------


def test_mesh_fuse_volume_and_validity():
    """mesh_fuse unions two stacked boxes into one valid body."""
    lower = Box(20, 20, 10)
    upper = Box(20, 20, 10).moved(Pos(0, 0, 10))
    fused = mesh_fuse(lower, upper)
    assert fused.is_valid
    assert fused.volume == pytest.approx(8000.0, rel=1e-6)


def test_mesh_cut_volume_and_validity():
    """mesh_cut drills a cylinder out of a block."""
    block = Box(20, 20, 10)
    drilled = mesh_cut(block, Cylinder(3, 20))
    assert drilled.is_valid
    expected = 20 * 20 * 10 - pi * 3**2 * 10
    assert drilled.volume == pytest.approx(expected, rel=0.02)


def test_mesh_intersect_volume_and_validity():
    """mesh_intersect keeps only the overlap of two boxes."""
    cube = Box(20, 20, 20)
    column = Box(10, 10, 40)
    overlap = mesh_intersect(cube, column)
    assert overlap.is_valid
    assert overlap.volume == pytest.approx(10 * 10 * 20, rel=1e-6)


def test_mesh_cut_with_no_tools_returns_base():
    """mesh_cut with no tools is a no-op returning the base body."""
    base = Box(10, 10, 10)
    result = mesh_cut(base)
    assert result.volume == pytest.approx(1000.0, rel=1e-6)


def test_free_functions_accept_meshpart_operands():
    """The free functions accept MeshPart operands as well as Shapes."""
    block_mesh = MeshPart.from_part(Box(20, 20, 10))
    tool_mesh = MeshPart.from_part(Cylinder(3, 20))
    drilled = mesh_cut(block_mesh, tool_mesh)
    assert drilled.is_valid
    # Mixed Shape + MeshPart operands also work.
    drilled_mixed = mesh_cut(Box(20, 20, 10), tool_mesh)
    assert drilled_mixed.volume == pytest.approx(drilled.volume, rel=1e-6)


def test_free_functions_reject_bad_operands():
    """The free functions reject operands that are neither Shape nor MeshPart."""
    with pytest.raises(TypeError):
        mesh_fuse(Box(1, 1, 1), 42)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mesh_fuse()
    with pytest.raises(ValueError):
        mesh_intersect()


# --------------------------------------------------------------------------
# Speed sanity check
# --------------------------------------------------------------------------


def test_mesh_cut_drills_many_holes_quickly():
    """Drilling ~150 holes via mesh_cut finishes well under a couple seconds."""
    block = Box(100, 100, 10)
    holes = [
        Cylinder(2, 14).moved(Pos(x, y, 0))
        for x in range(-42, 43, 12)
        for y in range(-42, 43, 12)
    ]
    assert len(holes) >= 49

    start = time.perf_counter()
    drilled = mesh_cut(block, *holes)
    elapsed = time.perf_counter() - start

    assert drilled.is_valid
    assert drilled.volume < 100 * 100 * 10
    assert elapsed < 2.0, f"mesh_cut of {len(holes)} holes took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# MeshPart CSG operators
# --------------------------------------------------------------------------


def test_operator_add_matches_mesh_fuse():
    """``+`` on a MeshPart equals mesh_fuse."""
    lower = MeshPart.from_part(Box(20, 20, 10))
    upper = MeshPart.from_part(Box(20, 20, 10).moved(Pos(0, 0, 10)))
    operator_result = lower + upper
    free_result = mesh_fuse(lower, upper)
    assert operator_result.volume == pytest.approx(free_result.volume, rel=1e-9)
    assert operator_result.volume == pytest.approx(8000.0, rel=1e-6)


def test_operator_sub_matches_mesh_cut():
    """``-`` on a MeshPart equals mesh_cut."""
    block = MeshPart.from_part(Box(20, 20, 10))
    tool = MeshPart.from_part(Cylinder(3, 20))
    operator_result = block - tool
    free_result = mesh_cut(block, tool)
    assert operator_result.volume == pytest.approx(free_result.volume, rel=1e-9)


def test_operator_and_matches_mesh_intersect():
    """``&`` on a MeshPart equals mesh_intersect."""
    cube = MeshPart.from_part(Box(20, 20, 20))
    column = MeshPart.from_part(Box(10, 10, 40))
    operator_result = cube & column
    free_result = mesh_intersect(cube, column)
    assert operator_result.volume == pytest.approx(free_result.volume, rel=1e-9)
    assert operator_result.volume == pytest.approx(2000.0, rel=1e-6)


def test_operators_return_mesh_part():
    """Every CSG operator returns a MeshPart, keeping the user in mesh space."""
    block = MeshPart.from_part(Box(10, 10, 10))
    tool = MeshPart.from_part(Box(4, 4, 20))
    assert isinstance(block + tool, MeshPart)
    assert isinstance(block - tool, MeshPart)
    assert isinstance(block & tool, MeshPart)


def test_in_place_operators():
    """In-place ``+= -= &=`` operators rebind to the boolean result."""
    fused = MeshPart.from_part(Box(20, 20, 10))
    fused += MeshPart.from_part(Box(20, 20, 10).moved(Pos(0, 0, 10)))
    assert fused.volume == pytest.approx(8000.0, rel=1e-6)

    cut = MeshPart.from_part(Box(20, 20, 10))
    cut -= MeshPart.from_part(Cylinder(3, 20))
    assert cut.volume < 4000.0

    intersected = MeshPart.from_part(Box(20, 20, 20))
    intersected &= MeshPart.from_part(Box(10, 10, 40))
    assert intersected.volume == pytest.approx(2000.0, rel=1e-6)


def test_operator_coerces_shape_operand_on_the_right():
    """A Shape operand on the right of a MeshPart operator is tessellated."""
    block = MeshPart.from_part(Box(20, 20, 10))
    drilled = block - Cylinder(3, 20)  # right operand is a native Shape
    assert isinstance(drilled, MeshPart)
    assert drilled.volume < 4000.0


def test_operator_coerces_shape_operand_on_the_left():
    """A native Shape on the LEFT of a mesh operator is coerced (the §4.5 rule).

    ``native_part - mesh_part`` dispatches through the native operator, which
    defers to MeshPart's reflected ``__rsub__`` so the Shape is tessellated.
    """
    tool = MeshPart.from_part(Cylinder(3, 20))
    drilled = Box(20, 20, 10) - tool
    assert isinstance(drilled, MeshPart)
    assert drilled.volume < 4000.0

    fused = Box(10, 10, 10) + MeshPart.from_part(Box(10, 10, 10).moved(Pos(0, 0, 10)))
    assert isinstance(fused, MeshPart)
    assert fused.volume == pytest.approx(2000.0, rel=1e-6)

    overlap = Box(20, 20, 20) & MeshPart.from_part(Box(10, 10, 40))
    assert isinstance(overlap, MeshPart)
    assert overlap.volume == pytest.approx(2000.0, rel=1e-6)


def test_operator_chain_stays_in_mesh_space():
    """A chain of operators threads MeshParts and merged side-maps throughout."""
    result = (
        MeshPart.from_part(Box(30, 30, 12))
        - Cylinder(3, 20).moved(Pos(8, 0, 0))
        - Cylinder(3, 20).moved(Pos(-8, 0, 0))
    )
    assert isinstance(result, MeshPart)
    assert result.is_valid
    # Side-map merged across the whole chain: a box has 6 faces, each cylinder
    # has 3 -> 6 + 3 + 3 = 12 provenance records.
    assert len(result.side_map) == 12


def test_operator_rejects_bad_operand():
    """A MeshPart operator rejects an operand that is neither Shape nor mesh."""
    block = MeshPart.from_part(Box(10, 10, 10))
    with pytest.raises(TypeError):
        _ = block + 42  # type: ignore[operator]


# --------------------------------------------------------------------------
# MeshPart transforms
# --------------------------------------------------------------------------


def test_translate_moves_the_body():
    """translate offsets the mesh body; the side-map is unaffected."""
    part = MeshPart.from_part(Box(2, 2, 2))
    moved = part.translate([10, 0, 0])
    box = moved.bounding_box()
    assert box.min.X == pytest.approx(9.0, abs=1e-6)
    assert box.max.X == pytest.approx(11.0, abs=1e-6)
    assert moved.volume == pytest.approx(8.0, rel=1e-6)
    # faceIDs are transform-invariant: the side-map survives unchanged.
    assert len(moved.side_map) == len(part.side_map)


def test_rotate_turns_the_body():
    """rotate applies an Euler-angle rotation (degrees)."""
    part = MeshPart.from_part(Box(4, 2, 2))
    rotated = part.rotate(z=90)
    box = rotated.bounding_box()
    assert box.size.X == pytest.approx(2.0, abs=1e-6)
    assert box.size.Y == pytest.approx(4.0, abs=1e-6)


def test_scale_resizes_the_body():
    """scale accepts a uniform scalar and a per-axis vector."""
    uniform = MeshPart.from_part(Box(2, 2, 2)).scale(3)
    assert uniform.volume == pytest.approx(216.0, rel=1e-6)
    per_axis = MeshPart.from_part(Box(2, 2, 2)).scale([2, 3, 4])
    assert per_axis.volume == pytest.approx(8 * 2 * 3 * 4, rel=1e-6)


def test_move_applies_a_location():
    """move applies a build123d Location (rotation + translation)."""
    part = MeshPart.from_part(Box(2, 2, 2))
    moved = part.move(Location((5, 0, 0)))
    assert moved.bounding_box().center().X == pytest.approx(5.0, abs=1e-6)
    assert moved.volume == pytest.approx(8.0, rel=1e-6)


def test_transformed_mesh_part_still_reconstructs():
    """A transformed planar MeshPart still bakes to an exact B-rep."""
    moved = mesh_cut(Box(20, 20, 10), Box(6, 6, 20)).translate([100, 0, 0])
    solid = moved.to_solid()
    assert solid.is_valid
    assert solid.volume == pytest.approx(20 * 20 * 10 - 6 * 6 * 10, abs=1e-6)


# --------------------------------------------------------------------------
# Systematic faceID seeding
# --------------------------------------------------------------------------


def test_seeded_box_has_six_distinct_face_ids():
    """A seeded box mesh carries exactly 6 distinct face ids, one per face."""
    manifold, _ = shape_to_manifold(Box(10, 10, 10))
    result = read_result(manifold)
    assert len(result.distinct_ids) == 6


def test_seeding_tracks_input_faces_not_triangles():
    """A seeded cylinder has hundreds of triangles but only 3 face ids."""
    manifold, _ = shape_to_manifold(Cylinder(8, 16))
    result = read_result(manifold)
    assert len(result.triangles) > 50
    assert len(result.distinct_ids) == 3


def test_faceid_survives_a_boolean():
    """Seeded face ids survive a boolean: the count tracks input faces."""
    drilled = mesh_cut(Box(20, 20, 10), Box(6, 6, 20))
    result = read_result(drilled.manifold)
    # 12 input faces; a cut consumes some -> count tracks faces, not triangles.
    assert 6 <= len(result.distinct_ids) <= 12
    assert len(result.triangles) >= len(result.distinct_ids)
    # Every surviving id has a provenance record in the merged side-map.
    for face_id in result.distinct_ids:
        assert face_id in drilled.side_map


def test_side_maps_merge_without_collision():
    """Booleans merge operand side-maps; globally unique ids never collide."""
    block = MeshPart.from_part(Box(20, 20, 10))
    tool = MeshPart.from_part(Cylinder(3, 20))
    drilled = block - tool
    # The merged side-map holds every record of both operands.
    assert len(drilled.side_map) == len(block.side_map) + len(tool.side_map)


def test_from_mesh_has_an_empty_side_map():
    """A MeshPart built from raw arrays carries no provenance."""
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    assert len(wrapped.side_map) == 0
    assert not wrapped.side_map


# --------------------------------------------------------------------------
# faceID-grouped exact B-rep reconstruction
# --------------------------------------------------------------------------


def test_reconstruction_recovers_exact_planar_solid():
    """A planar mesh-CSG result bakes to a valid solid with exact volume."""
    drilled = mesh_cut(Box(20, 20, 10), Box(6, 6, 20))
    solid = drilled.to_solid()
    assert isinstance(solid, Solid)
    assert solid.is_valid
    # Exact: 20*20*10 - 6*6*10 = 3640, no faceting error.
    assert solid.volume == pytest.approx(3640.0, abs=1e-6)


def test_reconstructed_faces_are_analytic_planes():
    """Every recovered face of an all-planar result is an analytic PLANE."""
    drilled = mesh_cut(Box(40, 30, 12), Pos(0, 0, 3) * Box(14, 10, 12))
    solid = drilled.to_solid()
    for face in solid.faces():
        assert face.geom_type == GeomType.PLANE


def test_reconstruction_falls_back_to_faceted_without_side_map():
    """A MeshPart with no side-map bakes via the faceted path."""
    source = MeshPart.from_part(Box(10, 10, 10))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    solid = wrapped.to_solid()
    assert solid.is_valid
    assert solid.volume == pytest.approx(1000.0, rel=1e-6)
    # The faceted box has 12 triangle faces, not 6 merged planes.
    assert len(solid.faces()) == 12


def test_reconstruct_false_forces_the_faceted_path():
    """to_solid(reconstruct=False) forces the faceted bake even with a side-map."""
    drilled = mesh_cut(Box(20, 20, 10), Box(6, 6, 20))
    faceted = drilled.to_solid(reconstruct=False)
    exact = drilled.to_solid(reconstruct=True)
    assert faceted.is_valid and exact.is_valid
    # The faceted bake has many more faces than the exact reconstruction.
    assert len(faceted.faces()) > len(exact.faces())


def test_reconstruction_splits_a_cut_face_into_two():
    """A face the boolean cut into two disjoint pieces recovers as two faces."""
    sliced = mesh_cut(Box(24, 8, 4), Box(4, 20, 20))
    solid = sliced.to_solid()
    assert solid.is_valid
    # bar 24*8*4 - slice 4*8*4 = 768 - 128 = 640, exact.
    assert solid.volume == pytest.approx(640.0, abs=1e-6)


def test_reconstruction_keeps_an_internal_void():
    """A body with a fully-enclosed cavity recovers with the void SUBTRACTED.

    The boolean produces two shells — the outer body and the inward-facing
    cavity shell. Bounding-box nesting must classify the cavity as an internal
    void of the enclosing body (carved out), not a separate positive solid
    (which would ADD its volume).
    """
    block = Box(40, 30, 12)
    cavity = Box(14, 10, 8)  # fully enclosed -> an internal cavity
    native = block - cavity

    result = MeshPart.from_part(block) - cavity
    exact = result.to_solid(reconstruct=True)
    faceted = result.to_solid(reconstruct=False)

    assert isinstance(exact, Solid)
    assert exact.is_valid
    # Exact: 40*30*12 - 14*10*8 = 14400 - 1120 = 13280, the void is carved out.
    assert exact.volume == pytest.approx(native.volume, abs=1e-6)
    assert exact.volume == pytest.approx(13280.0, abs=1e-6)
    # Both bake paths must agree with the native boolean.
    assert faceted.volume == pytest.approx(native.volume, abs=1e-6)


def test_reconstruction_disjoint_bodies_yield_a_compound():
    """Two separated boxes fused recover as a Compound of two bodies.

    The two shells are NOT nested, so each is its own positive solid; the
    recovered volume is the sum, never a solid-with-void.
    """
    left = Box(8, 8, 8)
    right = Box(8, 8, 8).moved(Pos(40, 0, 0))
    native = left + right

    result = mesh_fuse(left, right)
    exact = result.to_solid(reconstruct=True)
    faceted = result.to_solid(reconstruct=False)

    assert isinstance(exact, Compound)
    assert exact.is_valid
    assert len(exact.solids()) == 2
    # Exact: 8**3 + 8**3 = 1024, the sum of the two disjoint bodies.
    assert exact.volume == pytest.approx(native.volume, abs=1e-6)
    assert exact.volume == pytest.approx(1024.0, abs=1e-6)
    # Both bake paths must agree with the native boolean.
    assert isinstance(faceted, Compound)
    assert faceted.volume == pytest.approx(native.volume, abs=1e-6)


def test_curved_result_reconstructs_but_stays_faceted():
    """A cylindrical bore is identified by faceID but recovered faceted."""
    drilled = mesh_cut(Box(30, 30, 12), Cylinder(5, 40))
    solid = drilled.to_solid()
    assert solid.is_valid
    # The bore is faceted: no analytic CYLINDER face survives reconstruction.
    n_cylinders = sum(1 for f in solid.faces() if f.geom_type == GeomType.CYLINDER)
    assert n_cylinders == 0


def test_recover_brep_rejects_an_empty_side_map():
    """recover_brep raises when handed a MeshPart with no provenance."""
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    with pytest.raises(ValueError):
        recover_brep(read_result(wrapped.manifold), SideMap())


# --------------------------------------------------------------------------
# THE PAYOFF -- exact B-rep recovery + real fillet on a mesh-CSG result
# --------------------------------------------------------------------------


def _recovered_pocket() -> Solid:
    """A 40x30x12 plate with a 14x10 blind pocket, via the mesh-CSG path."""
    plate = Box(40, 30, 12)
    pocket = Pos(0, 0, 3) * Box(14, 10, 12)
    recovered = mesh_cut(plate, pocket).to_solid()
    assert isinstance(recovered, Solid)
    return recovered


def test_payoff_recovered_brep_matches_native_boolean():
    """The recovered B-rep has the SAME face/edge count and EXACT volume."""
    plate = Box(40, 30, 12)
    pocket = Pos(0, 0, 3) * Box(14, 10, 12)
    native = plate - pocket

    recovered = mesh_cut(plate, pocket).to_solid()

    assert recovered.is_valid
    assert len(recovered.faces()) == len(native.faces())
    assert len(recovered.edges()) == len(native.edges())
    # Bit-exact, not "within tessellation tolerance".
    assert recovered.volume == pytest.approx(native.volume, abs=1e-6)
    assert all(f.geom_type == GeomType.PLANE for f in recovered.faces())


def test_payoff_real_fillet_on_a_recovered_edge():
    """A real build123d fillet() runs on the recovered solid and makes a
    CYLINDER blend face -- the p9 payoff."""
    solid = _recovered_pocket()
    pocket_edges = [
        e for e in solid.edges().filter_by(Axis.Z) if abs(e.length - 9.0) < 1e-3
    ]
    assert len(pocket_edges) == 4

    filleted = solid.fillet(2.0, [pocket_edges[0]])
    assert filleted.is_valid
    # A real fillet produces an analytic cylindrical blend surface.
    assert any(f.geom_type == GeomType.CYLINDER for f in filleted.faces())


def test_payoff_fillet_all_four_pocket_edges():
    """Filleting all four pocket edges yields four analytic CYLINDER faces."""
    solid = _recovered_pocket()
    pocket_edges = [
        e for e in solid.edges().filter_by(Axis.Z) if abs(e.length - 9.0) < 1e-3
    ]
    filleted = solid.fillet(2.0, pocket_edges)
    assert filleted.is_valid
    n_cylinders = sum(1 for f in filleted.faces() if f.geom_type == GeomType.CYLINDER)
    assert n_cylinders == 4


def test_payoff_real_chamfer_on_a_recovered_edge():
    """A real build123d chamfer() runs on the recovered solid."""
    solid = _recovered_pocket()
    pocket_edges = [
        e for e in solid.edges().filter_by(Axis.Z) if abs(e.length - 9.0) < 1e-3
    ]
    chamfered = solid.chamfer(1.5, None, [pocket_edges[0]])
    assert chamfered.is_valid


def test_payoff_recovered_solid_bakes_to_a_part():
    """to_part() on a reconstructed MeshPart yields a valid build123d Part."""
    part = mesh_cut(Box(40, 30, 12), Pos(0, 0, 3) * Box(14, 10, 12)).to_part()
    assert isinstance(part, Part)
    assert part.volume == pytest.approx(13140.0, abs=1e-6)


# --------------------------------------------------------------------------
# Faceted primitive constructors
# --------------------------------------------------------------------------


def test_meshpart_box_matches_build123d_box():
    """MeshPart.box has the volume of build123d's Box and full provenance."""
    mesh_box = MeshPart.box(20, 10, 4)
    assert mesh_box.is_valid
    assert mesh_box.volume == pytest.approx(Box(20, 10, 4).volume, rel=1e-6)
    # A box is six analytic faces -> six seeded provenance records.
    assert len(mesh_box.side_map) == 6


def test_meshpart_box_is_origin_centred():
    """MeshPart.box follows build123d's origin-centred convention."""
    bbox = MeshPart.box(20, 10, 4).bounding_box()
    assert bbox.center().X == pytest.approx(0.0, abs=1e-6)
    assert bbox.center().Y == pytest.approx(0.0, abs=1e-6)
    assert bbox.center().Z == pytest.approx(0.0, abs=1e-6)
    assert bbox.size.X == pytest.approx(20.0, rel=1e-6)


def test_meshpart_sphere_volume_close_to_build123d():
    """MeshPart.sphere is a faceted sphere: volume close, slightly under-filled."""
    mesh_sphere = MeshPart.sphere(5)
    assert mesh_sphere.is_valid
    # Faceting under-fills the sphere; compare against the analytic volume.
    assert mesh_sphere.volume == pytest.approx(4 / 3 * pi * 5**3, rel=0.05)
    assert mesh_sphere.volume < 4 / 3 * pi * 5**3


def test_meshpart_cylinder_volume_close_to_build123d():
    """MeshPart.cylinder is a faceted cylinder with a close volume."""
    mesh_cylinder = MeshPart.cylinder(3, 10)
    assert mesh_cylinder.is_valid
    assert mesh_cylinder.volume == pytest.approx(pi * 3**2 * 10, rel=0.02)
    # Two flat caps + one curved lateral face -> three provenance records.
    assert len(mesh_cylinder.side_map) == 3


def test_meshpart_cone_volume_close_to_build123d():
    """MeshPart.cone is a faceted cone with a close volume."""
    mesh_cone = MeshPart.cone(4, 0, 9)
    assert mesh_cone.is_valid
    assert mesh_cone.volume == pytest.approx(Cone(4, 0, 9).volume, rel=0.03)


def test_meshpart_torus_volume_close_to_build123d():
    """MeshPart.torus is a faceted torus with a close volume."""
    mesh_torus = MeshPart.torus(10, 2)
    assert mesh_torus.is_valid
    assert mesh_torus.volume == pytest.approx(Torus(10, 2).volume, rel=0.05)


def test_meshpart_primitive_is_a_csg_operand():
    """A primitive MeshPart drops straight into a CSG chain with provenance."""
    drilled = MeshPart.box(20, 20, 10) - MeshPart.cylinder(3, 20)
    assert drilled.is_valid
    assert drilled.volume == pytest.approx(20 * 20 * 10 - pi * 9 * 10, rel=0.02)


# --------------------------------------------------------------------------
# Convex hull
# --------------------------------------------------------------------------


def test_mesh_hull_of_two_boxes_is_their_envelope():
    """mesh_hull of two separated boxes is the convex envelope around them.

    Two 4x4x4 boxes 20 apart along X: the hull is a 24x4x4 prism whose ends
    are the box ends, so its volume is the prism minus nothing -- exactly the
    24*4*4 envelope (the gap between the boxes is filled by the hull).
    """
    left = Box(4, 4, 4)
    right = Box(4, 4, 4).moved(Pos(20, 0, 0))
    hull = mesh_hull(left, right)
    assert hull.is_valid
    # Convex hull of two axis-aligned cubes spanning X in [-2, 22]: a prism.
    assert hull.volume == pytest.approx(24 * 4 * 4, rel=1e-6)


def test_mesh_hull_of_a_single_box_is_the_box():
    """The convex hull of an already-convex box is the box itself."""
    hull = mesh_hull(Box(10, 6, 4))
    assert hull.volume == pytest.approx(240.0, rel=1e-6)


def test_mesh_hull_fills_a_concavity():
    """The hull of a notched body fills its concave notch.

    Slicing a 4-thick slot fully across a 10-cube leaves two prongs; the
    convex hull bridges them, so the hull is strictly larger than the notched
    body and no larger than the original cube.
    """
    notched = Box(10, 10, 10) - Box(4, 12, 6).moved(Pos(0, 0, 2))
    hull = mesh_hull(notched)
    assert hull.is_valid
    assert hull.volume > notched.volume
    # The slot is open only at the top, so the hull bridges it back to the
    # full 10x10x10 cube.
    assert hull.volume == pytest.approx(1000.0, rel=1e-6)


def test_mesh_hull_method_on_meshpart():
    """MeshPart.hull() envelops self, and self.hull(other) envelops both."""
    box = MeshPart.box(4, 4, 4)
    assert box.hull().volume == pytest.approx(64.0, rel=1e-6)
    enveloped = box.hull(Box(4, 4, 4).moved(Pos(20, 0, 0)))
    assert enveloped.volume == pytest.approx(24 * 4 * 4, rel=1e-6)


def test_mesh_hull_carries_no_provenance():
    """A hull is new geometry: the result carries an empty side-map."""
    hull = mesh_hull(Box(4, 4, 4), Box(4, 4, 4).moved(Pos(20, 0, 0)))
    assert len(hull.side_map) == 0
    # With no provenance, to_solid falls back to the faceted bake.
    solid = hull.to_solid()
    assert solid.is_valid


def test_mesh_hull_rejects_no_operands():
    """mesh_hull raises when given no operands."""
    with pytest.raises(ValueError):
        mesh_hull()


# --------------------------------------------------------------------------
# Minkowski sum / difference
# --------------------------------------------------------------------------


def test_mesh_minkowski_sphere_box_rounds_the_box():
    """Minkowski sum of a box with a sphere rounds the box's edges.

    A 10-cube dilated by a radius-3 sphere is a rounded box: the 10-cube core,
    a 3-thick slab over each of the 6 faces, a quarter-cylinder over each of
    the 12 edges, and an eighth-sphere at each of the 8 corners.
    """
    rounded = mesh_minkowski(Box(10, 10, 10), Sphere(3))
    assert rounded.is_valid
    core = 10**3
    faces = 6 * 100 * 3
    edges = 12 * 10 * (pi * 3**2 / 4)
    corners = 4 / 3 * pi * 3**3
    analytic = core + faces + edges + corners
    # The sphere is faceted, so the rounded volume is slightly under analytic.
    assert rounded.volume == pytest.approx(analytic, rel=0.05)
    assert rounded.volume < analytic
    # The rounded body is strictly larger than the bare core box.
    assert rounded.volume > core
    # A Minkowski sum is new geometry: no provenance survives.
    assert len(rounded.side_map) == 0


def test_mesh_minkowski_box_box_is_exact_convex_case():
    """Minkowski sum of two boxes is exact: a box of summed dimensions."""
    summed = mesh_minkowski(Box(10, 10, 10), Box(2, 2, 2))
    assert summed.is_valid
    # (10+2) cube -- box(+)box is exact, no faceting error.
    assert summed.volume == pytest.approx(12**3, rel=1e-6)


def test_mesh_minkowski_decompose_matches_native_for_convex():
    """The decompose backend agrees with the native backend on convex inputs."""
    native = mesh_minkowski(Sphere(3), Box(10, 10, 10), method="native")
    decompose = mesh_minkowski(Sphere(3), Box(10, 10, 10), method="decompose")
    assert decompose.is_valid
    assert decompose.volume == pytest.approx(native.volume, rel=1e-3)


def test_mesh_minkowski_decompose_rejects_nonconvex():
    """The decompose backend raises on a connected non-convex operand."""
    notched = Box(10, 10, 10) - Box(6, 6, 12).moved(Pos(3, 3, 0))
    with pytest.raises(ValueError, match="non-convex"):
        mesh_minkowski(notched, Sphere(1), method="decompose")


def test_mesh_minkowski_rejects_unknown_method():
    """mesh_minkowski raises on an unknown method name."""
    with pytest.raises(ValueError, match="method"):
        mesh_minkowski(Box(2, 2, 2), Box(2, 2, 2), method="bogus")


def test_mesh_minkowski_native_handles_nonconvex():
    """The native backend dilates a non-convex body without raising."""
    notched = Box(10, 10, 10) - Box(6, 6, 12).moved(Pos(3, 3, 0))
    dilated = mesh_minkowski(notched, Sphere(1))
    assert dilated.is_valid
    assert dilated.volume > notched.volume


def test_mesh_minkowski_difference_erodes_a_box():
    """Minkowski difference erodes a box inward by the eroding box's extent."""
    eroded = mesh_minkowski_difference(Box(20, 20, 20), Box(4, 4, 4))
    assert eroded.is_valid
    # Eroding a 20-cube by a 4-cube shrinks each face inward by 2 -> 16-cube.
    assert eroded.volume == pytest.approx(16**3, rel=1e-6)


def test_mesh_minkowski_method_on_meshpart():
    """MeshPart.minkowski / .minkowski_difference mirror the free functions."""
    summed = MeshPart.box(10, 10, 10).minkowski(Box(2, 2, 2))
    assert summed.volume == pytest.approx(12**3, rel=1e-6)
    eroded = MeshPart.box(20, 20, 20).minkowski_difference(Box(4, 4, 4))
    assert eroded.volume == pytest.approx(16**3, rel=1e-6)


# --------------------------------------------------------------------------
# faces() / faces_from() selectors
# --------------------------------------------------------------------------


def test_faces_of_a_planar_meshpart_returns_six_faces():
    """faces() on an all-planar box returns exactly six analytic faces."""
    faces = MeshPart.box(20, 10, 4).faces()
    assert isinstance(faces, ShapeList)
    assert len(faces) == 6
    assert all(isinstance(face, Face) for face in faces)
    assert all(face.geom_type == GeomType.PLANE for face in faces)


def test_faces_of_a_drilled_block_matches_native_count():
    """faces() on a planar CSG result returns the native boolean's face count."""
    plate = Box(40, 30, 12)
    pocket = Pos(0, 0, 3) * Box(14, 10, 12)
    native = plate - pocket
    faces = mesh_cut(plate, pocket).faces()
    assert len(faces) == len(native.faces())
    assert all(face.geom_type == GeomType.PLANE for face in faces)


def test_faces_supports_build123d_selectors():
    """build123d's own sort_by / filter_by / group_by work on faces()."""
    faces = MeshPart.box(20, 10, 4).faces()
    # sort_by an axis -- the real ShapeList selector, not a mock.
    sorted_by_z = faces.sort_by(Axis.Z)
    assert len(sorted_by_z) == 6
    # filter_by a plane: a 20x10x4 box has exactly two faces parallel to XY.
    xy_faces = faces.filter_by(Plane.XY)
    assert len(xy_faces) == 2
    # filter_by GeomType.PLANE keeps every face of an all-planar body.
    assert len(faces.filter_by(GeomType.PLANE)) == 6
    # group_by area buckets the three distinct face sizes of the box.
    grouped = faces.group_by(SortBy.AREA)
    assert len(grouped) == 3


def test_faces_rejects_a_meshpart_without_provenance():
    """faces() raises on a MeshPart that carries no side-map (a raw mesh)."""
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    with pytest.raises(ValueError, match="provenance"):
        wrapped.faces()


def test_analytic_faces_raises_on_a_curved_region():
    """analytic_faces() raises rather than mis-answering on curved geometry.

    The design's P3 contract: a curved mesh region recovers faceted, not
    analytic; a curved-analytic selector on it must raise a clear error, never
    silently return faceted patches as if they were analytic faces.
    """
    drilled = mesh_cut(Box(30, 30, 12), Cylinder(5, 40))
    with pytest.raises(ValueError, match="curved"):
        drilled.analytic_faces()
    # faces() still works -- it returns the faceted patches honestly.
    assert len(drilled.faces()) > 0


def test_analytic_faces_returns_planar_faces():
    """analytic_faces() returns faces for an all-planar body."""
    faces = MeshPart.box(10, 10, 10).analytic_faces()
    assert len(faces) == 6
    assert all(face.geom_type == GeomType.PLANE for face in faces)


def test_faces_from_filters_by_provenance():
    """faces_from() returns only the faces originating from a named input.

    A genuine new selector dimension: filter recovered faces by which input
    shape they trace back to (design 6.4) -- including boolean cut faces.
    """
    plate = MeshPart.from_part(Box(40, 30, 12), source="plate")
    pocket = MeshPart.from_part(Pos(0, 0, 3) * Box(14, 10, 12), source="pocket")
    drilled = mesh_cut(plate, pocket)

    all_faces = drilled.faces()
    from_plate = drilled.faces_from("plate")
    from_pocket = drilled.faces_from("pocket")

    # Every recovered face traces back to exactly one of the two inputs.
    assert len(from_plate) + len(from_pocket) == len(all_faces)
    # The plate contributes its outer faces; the pocket contributes the cut.
    assert len(from_plate) > 0
    assert len(from_pocket) > 0
    assert all(face.geom_type == GeomType.PLANE for face in from_plate)


def test_faces_from_unknown_source_is_empty():
    """faces_from() with an unknown source name returns an empty ShapeList."""
    drilled = mesh_cut(
        MeshPart.from_part(Box(20, 20, 10), source="block"),
        MeshPart.from_part(Box(6, 6, 20), source="tool"),
    )
    nothing = drilled.faces_from("does-not-exist")
    assert isinstance(nothing, ShapeList)
    assert len(nothing) == 0
