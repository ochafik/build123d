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
    Cylinder,
    GeomType,
    Location,
    Part,
    Pos,
    Solid,
    Sphere,
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
    mesh_intersect,
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
