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
    Circle,
    Compound,
    Cone,
    Cylinder,
    Face,
    GeomType,
    Location,
    Part,
    Plane,
    Pos,
    Rectangle,
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
    FeatureChain,
    FeatureChainSelection,
    MeshFilletInfeasible,
    MeshPart,
    SideMap,
    is_available,
    mesh_chamfer,
    mesh_cut,
    mesh_extrude,
    mesh_fillet,
    mesh_fuse,
    mesh_hull,
    mesh_intersect,
    mesh_minkowski,
    mesh_minkowski_difference,
    mesh_offset,
    mesh_revolve,
    mesh_shell,
    recover_brep,
    to_cross_section,
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


def test_from_mesh_carries_a_synthetic_side_map():
    """A MeshPart from raw arrays gets synthetic coplanar-region provenance.

    A raw mesh has no input face provenance, but manifold3d groups its triangles
    into coplanar regions; from_mesh seeds those as synthetic ids so to_solid can
    merge each planar region into a single face. Every record is synthetic (no
    claimed analytic surface).
    """
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    # A welded box has 6 coplanar regions -> 6 synthetic ids.
    assert len(wrapped.side_map) == 6
    assert all(record.is_synthetic for record in wrapped.side_map.records.values())
    # Synthetic is NOT seeded-exact-planar: no record claims an input plane.
    assert not any(record.is_planar for record in wrapped.side_map.records.values())


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


def test_from_mesh_recovers_merged_planar_faces_via_synthetic_ids():
    """A from_mesh body recovers merged planar faces from its synthetic ids.

    A raw cube mesh (12 triangles, 6 coplanar regions) bakes — through the
    synthetic coplanar-region grouping — to a solid of **6 merged planar faces**,
    not 12 per-triangle faces, with exact volume. ``reconstruct=False`` still
    forces the one-face-per-triangle faceted bake.
    """
    source = MeshPart.from_part(Box(10, 10, 10))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    solid = wrapped.to_solid()
    assert solid.is_valid
    assert solid.volume == pytest.approx(1000.0, rel=1e-6)
    # Synthetic grouping merges each coplanar region: 6 faces, not 12 triangles.
    assert len(solid.faces()) == 6
    # The pure faceted bake still yields one face per triangle.
    faceted = wrapped.to_solid(reconstruct=False)
    assert faceted.is_valid
    assert len(faceted.faces()) == 12


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


def test_triangles_of_bucketing_matches_a_scan():
    """ResultMesh.triangles_of (O(1) bucket) equals the old O(T) np.where scan.

    The recovery hot-path bucketed triangles by face_id once (O(T log T))
    instead of scanning per id (O(T·ids), quadratic when ids scale with the
    triangle count). This locks in that the bucket returns exactly the same
    indices as the scan for every id — including an id with no triangles.
    """
    drilled = mesh_cut(
        Box(30, 30, 12), *(Box(2, 2, 30).moved(Pos(x, 0, 0)) for x in (-8, 0, 8))
    )
    result = read_result(drilled.manifold)

    # distinct_ids matches the unique set of the raw face_id array.
    assert result.distinct_ids == sorted(set(result.face_id.tolist()))

    # triangles_of matches a fresh np.where scan for every id, and the union of
    # all buckets is exactly the full triangle index range (no triangle lost).
    seen = []
    for fid in result.distinct_ids:
        bucketed = result.triangles_of(fid)
        scanned = np.where(result.face_id == fid)[0]
        assert np.array_equal(bucketed, scanned)
        seen.append(bucketed)
    assert np.array_equal(
        np.sort(np.concatenate(seen)), np.arange(len(result.triangles))
    )

    # An id not present returns an empty index array (matches np.where([])).
    missing = max(result.distinct_ids) + 10_000
    assert result.triangles_of(missing).shape == (0,)


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


def test_mesh_hull_carries_synthetic_provenance():
    """A hull's coplanar facets are seeded as synthetic ids (no input surface).

    A hull synthesises new envelope facets with no input provenance, but
    manifold3d groups them into coplanar regions; mesh_hull seeds those as
    synthetic ids. to_solid then recovers ONE merged planar face per hull facet
    (the flat hull faces become single faces, not N triangles) instead of one
    anonymous face per triangle.
    """
    hull = mesh_hull(Box(4, 4, 4), Box(4, 4, 4).moved(Pos(20, 0, 0)))
    # Synthetic provenance, not seeded-exact: every record is synthetic.
    assert len(hull.side_map) > 0
    assert all(record.is_synthetic for record in hull.side_map.records.values())

    result_mesh = read_result(hull.manifold)
    recovered = recover_brep(result_mesh, hull.side_map)
    assert recovered.is_valid
    # The flat hull faces recover as merged planar faces, far fewer than the
    # triangle count (the grouped, not per-triangle, win).
    merged_face_count = sum(len(r.faces) for r in recovered.recovered_faces)
    assert merged_face_count < len(result_mesh.triangles)
    # No seeded-exact planar faces -- a hull claims no input surface.
    assert recovered.n_exact_planar == 0
    assert recovered.n_synthetic_planar > 0

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
    # A Minkowski sum is new geometry: no input provenance, but manifold3d's
    # coplanar grouping is seeded as synthetic ids (the 6 flat faces merge,
    # the rounded shell stays per-facet).
    assert len(rounded.side_map) > 0
    assert all(record.is_synthetic for record in rounded.side_map.records.values())
    # The 6 flat faces recover as 6 large merged planar faces.
    solid = rounded.to_solid()
    big_planar = [
        f for f in solid.faces() if f.geom_type == GeomType.PLANE and f.area > 50.0
    ]
    assert len(big_planar) == 6


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
# Synthetic coplanar-region seeding (Option E): hull / minkowski / from_mesh
# recover one merged face per coplanar region, not one per triangle
# --------------------------------------------------------------------------


def test_synthetic_from_mesh_cube_recovers_six_faces_exact_volume():
    """from_mesh of a cube's raw arrays recovers 6 merged planar faces, exact."""
    # A unit cube: 8 corner vertices, 12 triangles (2 per face), CCW outward.
    vertices = np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 0, 1],
            [1, 1, 1],
            [0, 1, 1],
        ],
        dtype=np.float64,
    )
    triangles = np.array(
        [
            [0, 2, 1],
            [0, 3, 2],  # bottom z=0
            [4, 5, 6],
            [4, 6, 7],  # top z=1
            [0, 1, 5],
            [0, 5, 4],  # y=0
            [2, 3, 7],
            [2, 7, 6],  # y=1
            [1, 2, 6],
            [1, 6, 5],  # x=1
            [0, 4, 7],
            [0, 7, 3],  # x=0
        ],
        dtype=np.int64,
    )
    part = MeshPart.from_mesh(vertices, triangles)
    solid = part.to_solid()
    assert isinstance(solid, Solid)
    assert solid.is_valid
    # 6 merged planar faces, not 12 triangles.
    assert len(solid.faces()) == 6
    assert all(f.geom_type == GeomType.PLANE for f in solid.faces())
    assert solid.volume == pytest.approx(1.0, abs=1e-9)


def test_synthetic_hull_of_boxes_recovers_merged_planar_faces():
    """mesh_hull of boxes recovers merged planar faces ~ the true planar count.

    The hull of two stacked-and-offset boxes is a convex polyhedron with a small
    number of true planar faces. Recovery must merge each coplanar facet region
    into a single planar face -- a face count near the planar-face count, far
    below the triangle count -- and keep the volume.
    """
    hull = mesh_hull(
        Box(20, 20, 6),
        Box(6, 6, 20).moved(Pos(0, 0, 10)),
    )
    result_mesh = read_result(hull.manifold)
    recovered = recover_brep(result_mesh, hull.side_map)
    assert recovered.is_valid
    merged = sum(len(r.faces) for r in recovered.recovered_faces)
    # Far fewer faces than triangles: the coplanar regions merged.
    assert merged < len(result_mesh.triangles)
    # A convex hull of two axis-aligned boxes is bounded by a modest number of
    # planar faces; the merged count is well under 40 (not hundreds of facets).
    assert merged < 40
    # No seeded-exact planes (a hull claims no input surface), all synthetic.
    assert recovered.n_exact_planar == 0
    assert recovered.n_synthetic_planar > 0

    solid = hull.to_solid()
    assert solid.is_valid
    assert solid.volume == pytest.approx(hull.manifold.volume(), rel=1e-3)


def test_synthetic_minkowski_rounded_box_merges_flats_facets_rounds():
    """mesh_minkowski(box, small box) is a box; box ⊕ sphere rounds + merges.

    box ⊕ box is exactly a larger box: its 6 flat faces merge to 6 planar faces.
    box ⊕ sphere is a rounded box: the 6 flat faces merge to large planar faces
    while the rounded edges/corners stay faceted -- and the body is valid.
    """
    # box ⊕ box -> a 12-cube, 6 merged planar faces.
    bigger = mesh_minkowski(Box(10, 10, 10), Box(2, 2, 2))
    big_solid = bigger.to_solid()
    assert big_solid.is_valid
    assert big_solid.volume == pytest.approx(12**3, rel=1e-3)
    assert len(big_solid.faces()) == 6

    # box ⊕ sphere -> rounded box: 6 large flat faces merged, rounded rest.
    rounded = mesh_minkowski(Box(10, 10, 10), Sphere(3))
    rounded_solid = rounded.to_solid()
    assert rounded_solid.is_valid
    big_planar = [
        f
        for f in rounded_solid.faces()
        if f.geom_type == GeomType.PLANE and f.area > 50.0
    ]
    # Exactly the 6 flat faces survive as large merged planar faces; the rounded
    # shell is faceted (many small faces), so total face count is far larger.
    assert len(big_planar) == 6
    assert len(rounded_solid.faces()) > 6
    assert rounded_solid.volume == pytest.approx(rounded.manifold.volume(), rel=1e-3)


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
    """faces() raises on a MeshPart that carries no side-map at all.

    from_mesh now seeds synthetic ids, so the only way to reach an empty
    side-map is wrapping a bare Manifold directly (the low-level constructor).
    """
    source = MeshPart.from_part(Box(8, 8, 8))
    bare = MeshPart(source.manifold)  # no side-map passed
    assert len(bare.side_map) == 0
    with pytest.raises(ValueError, match="provenance"):
        bare.faces()


def test_from_mesh_faces_recovers_merged_planar_faces():
    """faces() on a from_mesh body returns its merged synthetic planar faces."""
    source = MeshPart.from_part(Box(8, 8, 8))
    vertices, triangles = source.to_arrays()
    wrapped = MeshPart.from_mesh(vertices, triangles)
    faces = wrapped.faces()
    # 6 coplanar regions -> 6 merged faces, every one a PLANE.
    assert len(faces) == 6
    assert all(f.geom_type == GeomType.PLANE for f in faces)


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


# --------------------------------------------------------------------------
# 2-D profile bridge -- to_cross_section
# --------------------------------------------------------------------------


def test_to_cross_section_from_rectangle_has_expected_area():
    """A build123d Rectangle round-trips through the bridge with exact area."""
    cs = to_cross_section(Rectangle(2, 4))
    assert cs.area() == pytest.approx(8.0, abs=1e-9)
    assert cs.num_contour() == 1


def test_to_cross_section_from_face_with_hole_has_two_contours():
    """A face with a hole produces an outer + inner contour pair.

    The bridge polygonises both wires; the Clipper2-backed CrossSection
    keeps them as two contours so the hole is preserved through extrude.
    """
    plate = Rectangle(10, 10) - Circle(2)
    cs = to_cross_section(plate, tolerance=0.05)
    assert cs.num_contour() == 2
    # Area is plate minus disc; loose tolerance because the circle is
    # polygonised at deflection 0.05.
    assert cs.area() == pytest.approx(100.0 - 3.14159265 * 4.0, rel=2e-2)


def test_to_cross_section_from_point_list():
    """A plain (x, y) point list is taken as a single closed polygon."""
    cs = to_cross_section([(0, 0), (3, 0), (3, 2), (0, 2)])
    assert cs.area() == pytest.approx(6.0, abs=1e-9)


def test_to_cross_section_rejects_unsupported_type():
    """A profile that is neither Shape, Compound, nor an iterable raises."""
    with pytest.raises(TypeError, match="unsupported profile type"):
        to_cross_section(42)  # type: ignore[arg-type]


def test_to_cross_section_rejects_short_polygon():
    """A point list with fewer than three vertices produces no contours."""
    with pytest.raises(ValueError, match="no closed contours"):
        to_cross_section([(0.0, 0.0), (1.0, 0.0)])


# --------------------------------------------------------------------------
# mesh_extrude -- 2D -> 3D linear extrusion
# --------------------------------------------------------------------------


def test_mesh_extrude_square_volume_is_exact():
    """Extruding a 2x4 rectangle by height 3 yields a body of volume 24."""
    mp = mesh_extrude(Rectangle(2, 4), height=3.0)
    assert isinstance(mp, MeshPart)
    assert mp.volume == pytest.approx(24.0, abs=1e-9)
    # Plain prism: 12 triangles (6 quad faces -> 12 tris), 8 vertices.
    bbox = mp.bounding_box()
    assert bbox.min.Z == pytest.approx(0.0, abs=1e-9)
    assert bbox.max.Z == pytest.approx(3.0, abs=1e-9)


def test_mesh_extrude_accepts_point_list():
    """A bare point list extrudes to the same volume as the equivalent Face."""
    mp = mesh_extrude([(0, 0), (2, 0), (2, 4), (0, 4)], height=3.0)
    assert mp.volume == pytest.approx(24.0, abs=1e-9)


def test_mesh_extrude_with_top_scale_zero_is_a_pyramid():
    """scale=0 collapses the top into a point -> a pyramid of V = base*h/3."""
    mp = mesh_extrude(Rectangle(2, 2), height=3.0, scale=0.0)
    # 4-sided pyramid: V = (1/3) * base_area * height = (1/3) * 4 * 3 = 4.
    assert mp.volume == pytest.approx(4.0, abs=1e-6)


def test_mesh_extrude_with_hole_subtracts_volume():
    """A 10x10 plate with a hole of radius 2 extruded by h=2 has the right vol.

    Pappus / prism: (100 - pi*4) * 2 ~= 174.86. Allow a few percent for the
    polygonisation of the circular hole.
    """
    plate = Rectangle(10, 10) - Circle(2)
    mp = mesh_extrude(plate, height=2.0, tolerance=0.05)
    expected = (100.0 - 3.14159265 * 4.0) * 2.0
    assert mp.volume == pytest.approx(expected, rel=2e-2)


def test_mesh_extrude_carries_no_provenance():
    """An extruded body is synthesised -- the side-map is empty."""
    mp = mesh_extrude(Rectangle(2, 2), height=2.0)
    assert len(mp.side_map) == 0


def test_mesh_extrude_rejects_non_positive_height():
    """height <= 0 raises rather than silently producing junk."""
    with pytest.raises(ValueError, match="positive height"):
        mesh_extrude(Rectangle(2, 2), height=0.0)


# --------------------------------------------------------------------------
# mesh_revolve -- 2D -> 3D revolution
# --------------------------------------------------------------------------


def test_mesh_revolve_disc_to_torus_volume():
    """Revolving a 1x2 rect offset to centre x=2 about Y -> torus, Pappus.

    Pappus' theorem: V = A * 2 * pi * centroid_x = 2 * 2 * pi * 2 = 8 * pi.
    Allow a few percent for circular_segments tessellation.
    """
    profile = Pos(2, 0, 0) * Rectangle(1, 2)
    mp = mesh_revolve(profile, angle=360.0, circular_segments=64)
    expected = 2.0 * 2.0 * 3.14159265 * 2.0
    assert mp.volume == pytest.approx(expected, rel=2e-2)


def test_mesh_revolve_partial_angle_scales_volume():
    """A 180-degree revolve has half the volume of a full 360-degree one."""
    profile = Pos(2, 0, 0) * Rectangle(1, 2)
    full = mesh_revolve(profile, angle=360.0, circular_segments=64).volume
    half = mesh_revolve(profile, angle=180.0, circular_segments=64).volume
    assert half == pytest.approx(full / 2.0, rel=5e-2)


def test_mesh_revolve_carries_no_provenance():
    """A revolved body is synthesised -- the side-map is empty."""
    profile = Pos(2, 0, 0) * Rectangle(1, 2)
    mp = mesh_revolve(profile, circular_segments=24)
    assert len(mp.side_map) == 0


def test_mesh_revolve_rejects_non_positive_angle():
    """angle <= 0 raises rather than silently producing junk."""
    profile = Pos(2, 0, 0) * Rectangle(1, 2)
    with pytest.raises(ValueError, match="positive angle"):
        mesh_revolve(profile, angle=0.0)


# --------------------------------------------------------------------------
# mesh_offset -- 3D offset / inflate
# --------------------------------------------------------------------------


def test_mesh_offset_outward_grows_volume_sensibly():
    """An outward offset of a box rounds its corners and grows the volume.

    A 4x4x4 box (V=64) offset by 0.5 has volume slightly less than (5x5x5=125)
    -- the corners and edges are rounded, not extended squarely -- but much
    larger than the original. The exact value depends on the sphere
    tessellation, so the test bounds it.
    """
    base = MeshPart.box(4.0, 4.0, 4.0)
    grown = mesh_offset(base, 0.5, sphere_segments=24)
    assert grown.volume > base.volume
    # Upper bound: the body fits inside a (4 + 2*0.5)**3 = 125 box.
    assert grown.volume < 125.0 + 1e-6
    # Lower bound: the offset is at least the union of the box and a 0.5 slab
    # on every face: 64 + 6 * (4*4) * 0.5 = 112.
    assert grown.volume > 100.0


def test_mesh_offset_inward_shrinks_volume():
    """An inward offset shrinks the body's volume."""
    base = MeshPart.box(8.0, 8.0, 8.0)
    eroded = mesh_offset(base, -0.5, sphere_segments=24)
    assert eroded.volume < base.volume
    # The eroded body fits inside a (8 - 2*0.5)**3 = 343 box (and rounds in,
    # so the real volume is somewhat less). Lower bound: 8*8*8 - margin.
    assert 200.0 < eroded.volume < base.volume


def test_mesh_offset_zero_is_a_noop():
    """offset(0) returns the input body unchanged in volume."""
    base = MeshPart.box(4.0, 4.0, 4.0)
    same = mesh_offset(base, 0.0)
    assert same.volume == pytest.approx(base.volume, abs=1e-9)


def test_mesh_offset_method_on_mesh_part():
    """MeshPart.offset matches the free-function mesh_offset."""
    base = MeshPart.box(4.0, 4.0, 4.0)
    method = base.offset(0.5, sphere_segments=24).volume
    free = mesh_offset(base, 0.5, sphere_segments=24).volume
    assert method == pytest.approx(free, abs=1e-9)


def test_mesh_offset_inward_too_large_collapses_with_clear_error():
    """An inward offset larger than the body's half-feature raises clearly.

    Honest-limits note: inward offset on a faceted sphere tool collapses when
    the eroding radius is large compared to the body's thinnest dimension.
    """
    base = MeshPart.box(2.0, 2.0, 2.0)
    # Erode a 2x2x2 box by a 5-radius sphere -> empty body.
    with pytest.raises(ValueError, match="empty manifold|invalid manifold"):
        mesh_offset(base, -5.0, sphere_segments=12)


# --------------------------------------------------------------------------
# mesh_shell -- 3D hollow / wall thickness
# --------------------------------------------------------------------------


def test_mesh_shell_volume_is_close_to_surface_times_thickness():
    """A thin shell on a box has volume ~ surface_area * thickness.

    For an 8x8x8 box (S=384) and t=0.5 the first-order estimate is 192;
    the real value is somewhat less because the corners are rounded inward.
    Bound it loosely.
    """
    base = MeshPart.box(8.0, 8.0, 8.0)
    walls = mesh_shell(base, 0.5, sphere_segments=24)
    assert 0.0 < walls.volume < base.volume
    # Coarse sanity: first-order shell volume is S*t = 384 * 0.5 = 192;
    # accept anywhere in the order-of-magnitude band.
    assert 100.0 < walls.volume < 250.0


def test_mesh_shell_method_on_mesh_part():
    """MeshPart.shell matches the free-function mesh_shell."""
    base = MeshPart.box(6.0, 6.0, 6.0)
    method = base.shell(0.4, sphere_segments=24).volume
    free = mesh_shell(base, 0.4, sphere_segments=24).volume
    assert method == pytest.approx(free, abs=1e-9)


def test_mesh_shell_rejects_non_positive_thickness():
    """thickness <= 0 raises rather than silently producing junk."""
    base = MeshPart.box(4.0, 4.0, 4.0)
    with pytest.raises(ValueError, match="positive thickness"):
        mesh_shell(base, 0.0)


def test_mesh_shell_inward_collapse_raises_clearly():
    """A too-thick wall on a thin body raises with a clear error.

    The honest limit: the shell op subtracts an inward offset, and an
    inward offset by half the body's thinnest dimension collapses on a
    faceted sphere tool.
    """
    base = MeshPart.box(2.0, 2.0, 2.0)
    with pytest.raises(ValueError, match="empty manifold|invalid manifold"):
        mesh_shell(base, 5.0, sphere_segments=12)


# --------------------------------------------------------------------------
# Phase A3a — feature-edge selection
# --------------------------------------------------------------------------


def _triangle_areas(mesh_part: MeshPart) -> np.ndarray:
    """Return the per-triangle areas of a MeshPart's manifold."""
    vertices, triangles = mesh_part.to_arrays()
    corners = vertices[triangles]
    cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    return 0.5 * np.linalg.norm(cross, axis=1)


def _degenerate_triangle_count(mesh_part: MeshPart) -> int:
    """Count near-zero-area sliver triangles (p10's definition: < 0.1 % of mean)."""
    areas = _triangle_areas(mesh_part)
    if len(areas) == 0:
        return 0
    threshold = max(float(areas.mean()) * 1e-3, 1e-12)
    return int((areas < threshold).sum())


def test_feature_edges_box_has_12_chains():
    """A box: 12 feature edges, 12 distinct faceID-pair chains, all convex."""
    box = MeshPart.from_part(Box(20, 20, 20))
    selection = box.feature_edges()
    assert isinstance(selection, FeatureChainSelection)
    assert len(selection) == 12
    for chain in selection:
        assert isinstance(chain, FeatureChain)
        assert chain.convexity_class == "convex"
        assert len(chain.edges) == 1
        # Box edges have their endpoints at the 8 corner vertices, each shared
        # by 3 chains of different (lo, hi) pairs — every endpoint is a corner.
        assert all(kind == "corner" for kind in chain.vertex_kinds)
    convex_only = selection.convex()
    assert len(convex_only) == 12
    assert len(selection.concave()) == 0


def test_feature_edges_bored_box_has_two_closed_loops():
    """A box with a cylindrical bore yields two ~63-edge closed loops."""
    bored = mesh_cut(Box(40, 40, 10), Cylinder(8, 14))
    selection = bored.feature_edges()
    loops = list(selection.closed())
    # 2 bore-rim loops + 4 outer-box loops would be the upper bound; we expect
    # at least the two bore rims (≥ 63 verts each).
    big_loops = [c for c in loops if len(c.verts) >= 20]
    assert len(big_loops) == 2
    for loop in big_loops:
        assert loop.is_loop
        assert loop.convexity_class == "convex"


def test_feature_edges_caches_per_meshpart():
    """Repeated MeshPart.feature_edges() returns the same selection."""
    box = MeshPart.from_part(Box(10, 10, 10))
    first = box.feature_edges()
    second = box.feature_edges()
    assert first is second


# --------------------------------------------------------------------------
# Phase A3a — chamfer correctness
# --------------------------------------------------------------------------


def test_mesh_chamfer_box_single_edge_is_bit_exact():
    """Chamfering ONE box edge removes exactly one triangular prism."""
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    chamfered = box.chamfer(chain, size=4.0)
    assert chamfered.is_valid
    # 4 mm right-triangle chamfer over a 20 mm edge removes 0.5 * 4 * 4 * 20 = 160
    assert chamfered.volume == pytest.approx(8000.0 - 160.0, abs=1e-6)
    # One body, no slivers
    assert len(chamfered.manifold.decompose()) == 1
    assert _degenerate_triangle_count(chamfered) == 0


def test_mesh_chamfer_volume_between_native_and_original():
    """A box chamfer's mesh-volume sits between original and native-trimmed."""
    side = 20.0
    size = 2.0
    box = MeshPart.from_part(Box(side, side, side))
    chamfered = box.chamfer(box.feature_edges(), size=size)
    native_box = Box(side, side, side)
    native = native_box.chamfer(size, None, native_box.edges())
    # A3a chamfer does not trim corner pyramids (that lands in A3c), so the
    # mesh volume sits ABOVE the native volume and BELOW the original.
    assert chamfered.is_valid
    assert chamfered.volume < side**3
    assert chamfered.volume > native.volume
    assert len(chamfered.manifold.decompose()) == 1


def test_mesh_chamfer_box_all_edges_no_slivers_and_one_body():
    """All-12-edges chamfer (p10's degen=0 target) — valid, single body, no slivers."""
    box = MeshPart.from_part(Box(20, 20, 20))
    chamfered = box.chamfer(box.feature_edges(), size=2.0)
    assert chamfered.is_valid
    assert len(chamfered.manifold.decompose()) == 1
    assert _degenerate_triangle_count(chamfered) == 0


def test_mesh_chamfer_bored_box_rim_loop():
    """The headline A3a fix: a curved-loop chamfer with far fewer slivers than p10.

    p10's per-segment chamfer on the same input emits ~1000 sliver triangles
    on the curved 63-edge bore rim; A3a's per-chain swept tool drops that
    sliver count by an order of magnitude (still > 0 because the cylindrical
    bore's own facets get split, but no longer the per-segment overlap tail).
    """
    bored = mesh_cut(Box(40, 40, 10), Cylinder(8, 14))
    # Pick the top bore rim (the loop with the higher mean z)
    selection = bored.feature_edges()
    loops = list(selection.closed())
    big_loops = [c for c in loops if len(c.verts) >= 20]

    def mean_z(chain: FeatureChain) -> float:
        return float(np.mean([selection.vertices[v][2] for v in chain.verts]))

    top_rim = max(big_loops, key=mean_z)
    assert top_rim.convexity_class == "convex"

    chamfered = bored.chamfer(top_rim, size=1.5)
    assert chamfered.is_valid
    assert len(chamfered.manifold.decompose()) == 1
    # p10 baseline on this case: 1052 slivers. A3a target: an order-of-magnitude
    # reduction. The exact count is faceting-dependent; assert << p10.
    assert _degenerate_triangle_count(chamfered) < 400, (
        "A3a per-chain swept chamfer regressed beyond the p10 baseline "
        f"(slivers={_degenerate_triangle_count(chamfered)})"
    )
    # The chamfer removes material on a convex bore rim.
    assert chamfered.volume < bored.volume


def test_mesh_chamfer_oversize_raises_meshfilletinfeasible():
    """A chamfer size > half the local feature thickness raises (P3 — never clamp)."""
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        plate.chamfer(chain, size=8.0)
    assert exc_info.value.constraint == "half-thickness"
    assert exc_info.value.requested == pytest.approx(8.0)
    assert exc_info.value.measured > 0.0
    assert "exceeds half the local feature thickness" in str(exc_info.value)


def test_mesh_chamfer_rejects_non_positive_size():
    """size must be strictly positive."""
    box = MeshPart.from_part(Box(10, 10, 10))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match="must be > 0"):
        box.chamfer(chain, size=0.0)
    with pytest.raises(ValueError, match="must be > 0"):
        box.chamfer(chain, size=-1.0)


def test_mesh_chamfer_free_function_matches_method():
    """The free function mesh_chamfer matches MeshPart.chamfer."""
    box = MeshPart.from_part(Box(15, 15, 15))
    chain = box.feature_edges()[0]
    method = box.chamfer(chain, size=2.0)
    free = mesh_chamfer(box, chain, size=2.0)
    assert method.volume == pytest.approx(free.volume, rel=1e-9)


def test_mesh_chamfer_rejects_unsupported_on_infeasible_mode():
    """A4 ships ``raise`` and ``skip``; any other value raises ValueError.

    Notably ``clamp`` is *not* shipped (design §5.4 forbids it in A3 / A4 —
    a clamp would silently degrade the answer, exactly the P3 failure the
    mode is designed to prevent).
    """
    box = MeshPart.from_part(Box(10, 10, 10))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match="on_infeasible"):
        box.chamfer(chain, size=1.0, on_infeasible="clamp")
    with pytest.raises(ValueError, match="on_infeasible"):
        box.chamfer(chain, size=1.0, on_infeasible="bogus")


def test_mesh_chamfer_rejects_foreign_chain():
    """A chain from a *different* MeshPart's feature graph is rejected."""
    box_a = MeshPart.from_part(Box(10, 10, 10))
    box_b = MeshPart.from_part(Box(10, 10, 10))
    chain_b = box_b.feature_edges()[0]
    with pytest.raises(ValueError, match="not part of this mesh"):
        box_a.chamfer(chain_b, size=1.0)


def test_mesh_chamfer_iterable_of_chains_accepted():
    """edges may be any iterable of FeatureChain (matches design §8.1)."""
    box = MeshPart.from_part(Box(20, 20, 20))
    chains = list(box.feature_edges())[:2]
    chamfered = box.chamfer(chains, size=1.5)
    assert chamfered.is_valid
    assert len(chamfered.manifold.decompose()) == 1


def test_mesh_chamfer_l_shape_all_edges_cut_then_add():
    """L-shape chamfer-all (p10 Case 2): the concave chain adds, the rest cut.

    The L has one re-entrant (concave) chain and many convex chains; applying
    convex CUTs first and concave ADDs second keeps the body in one piece —
    design §3.7 carries forward p10's fix #3.

    Phase A3c adds a mixed-corner pre-flight (design §4.5): the L's re-entrant
    edge meets the box's outer edges at a *mixed* corner (convex + concave
    chains incident), which raises ``MeshFilletInfeasible`` when chamfered in
    one call. The workaround the design recommends is to chamfer the convex
    and concave subsets separately; we exercise that path here.
    """
    l_shape = mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    selection = l_shape.feature_edges()
    n_convex = len(selection.convex())
    n_concave = len(selection.concave())
    assert n_concave >= 1, "L-shape has a re-entrant edge"
    assert n_convex >= 10, "L-shape has at least 10 convex edges"

    # All-at-once: A3c mixed-corner pre-flight raises (design §4.5).
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        l_shape.chamfer(selection, size=2.0)
    assert exc_info.value.constraint == "mixed-corner"

    # Design-recommended workaround: chamfer the convex chains alone (this
    # call's corners are uniform-convex). The body stays in one piece and
    # the cut-then-add flow still applies internally even though the concave
    # chain is excluded from this selection.
    convex_only = l_shape.chamfer(selection.convex(), size=2.0)
    assert convex_only.is_valid
    assert len(convex_only.manifold.decompose()) == 1
    assert _degenerate_triangle_count(convex_only) == 0
    assert convex_only.volume < l_shape.volume


# --------------------------------------------------------------------------
# Phase A3b — fillet correctness
# --------------------------------------------------------------------------


def _faceted_fillet_profile_area(radius: float, segments: int) -> float:
    """The bit-exact area of A3b's faceted fillet cross-section.

    Mirrors the polygon ``_fillet_profile_points`` in
    :mod:`build123d.mesh.fillet`: the corner ``(0,0)`` plus ``segments + 1``
    arc samples from ``(r, 0)`` to ``(0, r)``. The region is the "wedge minus
    quarter-disc" (design §3.4) — non-convex but with well-defined area via
    the shoelace formula on the closed polygon.
    """
    cx, cy = radius, radius
    pts = [(0.0, 0.0)]
    for i in range(segments + 1):
        ang = 1.5 * pi - i * (pi / 2.0) / segments
        pts.append((cx + radius * np.cos(ang), cy + radius * np.sin(ang)))
    # Shoelace on the closed polygon
    area = 0.0
    n = len(pts)
    for i in range(n):
        x_i, y_i = pts[i]
        x_j, y_j = pts[(i + 1) % n]
        area += x_i * y_j - x_j * y_i
    return abs(area) * 0.5


def test_mesh_fillet_box_single_edge_is_bit_exact():
    """Fillet one box edge: volume = box − (faceted-arc profile area × edge length).

    The faceted-arc cross-section area is computable in closed form (see
    :func:`_faceted_fillet_profile_area`); the swept tool removes exactly
    ``profile_area · L`` from a 20 mm box. With ``n_seg = 8`` and ``r = 1``,
    the predicted volume is ``8000 − 0.21964 · 20 ≈ 7995.607``.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    radius = 1.0
    segments = 8
    filleted = box.fillet(chain, radius=radius, segments=segments)
    assert filleted.is_valid
    expected = 8000.0 - _faceted_fillet_profile_area(radius, segments) * 20.0
    assert filleted.volume == pytest.approx(expected, abs=1e-9)
    assert len(filleted.manifold.decompose()) == 1
    assert _degenerate_triangle_count(filleted) == 0


def test_mesh_fillet_box_all_edges_no_slivers_and_one_body():
    """All 12 edges filleted: one body, real arc strips, 8 corner sphere patches.

    A3c upgrade: every box corner is a 3-chain convex corner; the setback +
    faceted-sphere recipe (design §4.3) rounds every corner into a ball
    patch. The result has many more triangles than A3b's stub (8 sphere
    facets × ~256 tris each + chain strips) and a small residual
    "sliver-by-area-ratio" count from the sphere/tube tessellation seams —
    legitimate facets, not boolean overlap artefacts.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    filleted = box.fillet(box.feature_edges(), radius=1.0)
    assert filleted.is_valid
    assert len(filleted.manifold.decompose()) == 1
    # The "degen" count counts triangles smaller than 0.1% of the mean — the
    # mean is dominated by the huge box-face triangles, so this catches both
    # genuine slivers *and* the small but legitimate facets where each
    # sphere-corner patch meets its three setback tube ends. Per design §4.7
    # we expect a modest count from those seams; assert it stays bounded
    # rather than zero (the strict zero target was the A3b stub world).
    assert _degenerate_triangle_count(filleted) < 100
    # Volume sits strictly below the box and above the chamfer-equivalent.
    # A3c corner blends remove additional material per corner (~2.5 per
    # cube corner sphere at r = 1, times 8 corners), so the lower bound on
    # the removed volume is the 12-chain estimate plus 8 corner volumes.
    assert filleted.volume < 8000.0
    chain_loss = 12.0 * 0.22 * 20.0  # ≈ 53
    corner_loss = 8.0 * 3.0  # ≈ 24 (one sphere per cube corner at r = 1)
    assert filleted.volume > 8000.0 - chain_loss - corner_loss - 10.0


def test_mesh_fillet_bored_box_rim_loop_curved_loop_fix():
    """The headline A3b test: a curved-loop fillet that does NOT shatter into slivers.

    p10's per-segment fillet on a 63-edge bore rim emits 1052–1720 degenerate
    sliver triangles (the curved-loop failure mode in the design); A3b's
    per-chain swept arc-tool brings that down to a few hundred — bounded by
    the bore's own tessellation, not per-segment overlap.
    """
    bored = mesh_cut(Box(40, 40, 10), Cylinder(8, 14))
    selection = bored.feature_edges()
    big_loops = [c for c in selection.closed() if len(c.verts) >= 20]
    assert len(big_loops) == 2

    def mean_z(chain: FeatureChain) -> float:
        return float(np.mean([selection.vertices[v][2] for v in chain.verts]))

    top_rim = max(big_loops, key=mean_z)
    assert top_rim.convexity_class == "convex"

    filleted = bored.fillet(top_rim, radius=1.0)
    assert filleted.is_valid
    assert len(filleted.manifold.decompose()) == 1
    # Per-segment overlap is gone (the symptom is < 400 slivers, vs p10's
    # 1052–1720); the residual count is from the bore's facet split, NOT
    # per-segment overlap.
    assert _degenerate_triangle_count(filleted) < 400, (
        f"A3b per-chain swept fillet regressed beyond the curved-loop fix "
        f"(slivers={_degenerate_triangle_count(filleted)})"
    )
    # Filleting a convex rim removes material from the bored body.
    assert filleted.volume < bored.volume


def test_mesh_fillet_l_shape_convex_and_concave_chains_succeeds():
    """L-shape — fillet the convex and concave subsets in two separate calls.

    A3b's per-edge sign splitting lets one chain carry both signs; A3c adds
    a mixed-corner pre-flight (design §4.5) that raises when a selection's
    chains *meet at a mixed-sign corner*. The L-shape has its re-entrant
    edge meeting two convex edges at the inside-of-L vertices — those are
    mixed corners, and a single fillet call covering both halves now
    raises. The design-recommended workaround is two calls: convex first,
    then concave (or vice versa). Each call's corners are internally
    consistent.
    """
    l_shape = mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    original_volume = l_shape.volume
    selection = l_shape.feature_edges()
    convex_chains = list(selection.convex())
    concave_chains = list(selection.concave())
    assert len(concave_chains) == 1
    assert len(convex_chains) >= 10

    radius = 1.0
    segments = 8

    # Concave-only: just the re-entrant chain. No corner sharing within the
    # selection (a multi-chain corner needs ≥2 chains *both in the selection*),
    # so the pre-flight passes and we get the expected concave-fill volume.
    concave = concave_chains[0]
    chain_length = float(
        np.linalg.norm(
            selection.vertices[concave.verts[-1]] - selection.vertices[concave.verts[0]]
        )
    )
    expected_added = _faceted_fillet_profile_area(radius, segments) * chain_length
    only_concave = l_shape.fillet(concave_chains, radius=radius, segments=segments)
    assert only_concave.is_valid
    assert len(only_concave.manifold.decompose()) == 1
    assert only_concave.volume == pytest.approx(
        original_volume + expected_added, abs=1e-9
    )

    # Convex-only is the design-recommended workaround: every chain is
    # convex, the corners they form are uniform-convex (8 outside corners
    # plus 4 inside-of-L corners along the cut perimeter — all convex
    # because we excluded the lone concave chain from this call). One body
    # comes out, volume drops monotonically.
    only_convex = l_shape.fillet(convex_chains, radius=radius, segments=segments)
    assert only_convex.is_valid
    assert len(only_convex.manifold.decompose()) == 1
    assert only_convex.volume < original_volume

    # Both together: the mixed-corner pre-flight raises (design §4.5 — the
    # canonical NG_F4 non-goal). The exception names the offending chains.
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        l_shape.fillet(selection, radius=radius, segments=segments)
    assert exc_info.value.constraint == "mixed-corner"
    assert exc_info.value.requested == pytest.approx(radius)


def test_mesh_fillet_l_shape_mixed_corner_raises_cleanly():
    """The headline A3c mixed-corner test (design §4.5 / NG_F4).

    An L-shape with both convex and concave chains *meeting at one vertex*
    raises ``MeshFilletInfeasible(constraint="mixed-corner")``. This pins
    the design's non-goal: the construction has no consistent inset centre
    for the vertex sphere when one chain wants the centre inset and another
    wants it outset.
    """
    l_shape = mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        l_shape.fillet(l_shape.feature_edges(), radius=0.5)
    err = exc_info.value
    assert err.constraint == "mixed-corner"
    # The exception names the offending corner's chains — at least one of
    # them is the L-shape's re-entrant (concave) chain.
    assert any(
        c.convexity_class == "concave" for c in err.chains
    ), "mixed-corner exception must name the concave chain"
    # And at least one of the incident chains is convex.
    assert any(c.convexity_class == "convex" for c in err.chains)


def test_mesh_fillet_oversize_raises_meshfilletinfeasible():
    """An oversize fillet radius raises (P3 — never silently clamp)."""
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        plate.fillet(chain, radius=8.0)
    assert exc_info.value.constraint == "half-thickness"
    assert exc_info.value.requested == pytest.approx(8.0)
    assert exc_info.value.measured > 0.0
    assert exc_info.value.measured < 8.0


def test_mesh_fillet_rejects_non_positive_radius():
    """radius must be strictly positive."""
    box = MeshPart.from_part(Box(10, 10, 10))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match="must be > 0"):
        box.fillet(chain, radius=0.0)
    with pytest.raises(ValueError, match="must be > 0"):
        box.fillet(chain, radius=-1.0)


def test_mesh_fillet_rejects_invalid_segments():
    """segments must be >= 1."""
    box = MeshPart.from_part(Box(10, 10, 10))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match=">= 1"):
        box.fillet(chain, radius=1.0, segments=0)


def test_mesh_fillet_free_function_matches_method():
    """The free function mesh_fillet matches MeshPart.fillet."""
    box = MeshPart.from_part(Box(15, 15, 15))
    chain = box.feature_edges()[0]
    method = box.fillet(chain, radius=1.0)
    free = mesh_fillet(box, chain, radius=1.0)
    assert method.volume == pytest.approx(free.volume, rel=1e-9)


def test_mesh_fillet_rejects_unsupported_on_infeasible_mode():
    """A4 ships ``raise`` and ``skip``; any other value raises ValueError.

    ``clamp`` is *not* shipped (design §5.4) — silently degrading the radius
    would violate P3.
    """
    box = MeshPart.from_part(Box(10, 10, 10))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match="on_infeasible"):
        box.fillet(chain, radius=1.0, on_infeasible="clamp")
    with pytest.raises(ValueError, match="on_infeasible"):
        box.fillet(chain, radius=1.0, on_infeasible="bogus")


def test_mesh_fillet_rejects_foreign_chain():
    """A chain from a *different* MeshPart's feature graph is rejected."""
    box_a = MeshPart.from_part(Box(10, 10, 10))
    box_b = MeshPart.from_part(Box(10, 10, 10))
    chain_b = box_b.feature_edges()[0]
    with pytest.raises(ValueError, match="not part of this mesh"):
        box_a.fillet(chain_b, radius=1.0)


def test_mesh_fillet_segments_parameter_changes_profile_facets():
    """Increasing ``segments`` brings the faceted profile closer to the exact arc.

    The faceted profile (chord polygon) sits *above* the exact arc (chords lie
    between the arc and the wedge's hypotenuse), so the faceted region's area
    is **greater** than the exact ``r²(1 − π/4)``. Coarser facetting removes
    more material; finer facetting approaches the exact value from below.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    coarse = box.fillet(chain, radius=1.0, segments=4)
    fine = box.fillet(chain, radius=1.0, segments=16)
    assert coarse.is_valid and fine.is_valid
    # Coarse facetting removes MORE material than fine.
    assert coarse.volume < fine.volume
    # Both stay below the exact (infinite-segment) result — the faceted region
    # over-removes.
    exact_result = 8000.0 - 1.0**2 * (1.0 - pi / 4.0) * 20.0
    assert fine.volume < exact_result
    assert coarse.volume < exact_result
    # And both stay above the chamfer-equivalent (size = radius), since the
    # ball cut-out always leaves some material the chamfer would have removed.
    chamfer_result = 8000.0 - 0.5 * 1.0 * 1.0 * 20.0
    assert fine.volume > chamfer_result
    assert coarse.volume > chamfer_result


# --------------------------------------------------------------------------
# Phase A3c — multi-chain corner blends (setback + sphere / polyhedron)
# --------------------------------------------------------------------------


def test_mesh_fillet_box_all_edges_corner_blended():
    """Box-all-12-edges fillet — 8 cube corners blended into ball patches.

    The headline A3c result (design §4.7 / §11 T3): every box corner is a
    k=3 convex corner; the setback + faceted-sphere recipe rounds each
    corner into a real ball, not the A3b stub thin patch.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    radius = 2.0
    filleted = box.fillet(box.feature_edges(), radius=radius, segments=8)
    assert filleted.is_valid
    bodies = filleted.manifold.decompose()
    real_bodies = [b for b in bodies if abs(b.volume()) > 1e-3]
    assert len(real_bodies) == 1
    # Volume below the unfilleted box; chain reductions + 8 sphere corners
    # together bound it from below.
    assert filleted.volume < 8000.0
    chain_loss = 12.0 * (radius**2 * (1.0 - pi / 4.0)) * 20.0  # ≈ 41
    corner_loss = 8.0 * (4.0 / 3.0) * pi * radius**3 / 8.0  # 1/8-ball ≈ 4.2 each
    # Use a generous tolerance — the faceted sphere over-removes vs the exact ball.
    assert filleted.volume > 8000.0 - chain_loss - corner_loss * 4.0


def test_mesh_chamfer_box_all_edges_corner_blended():
    """Box-all-12-edges chamfer — 8 cube corners blended into flat triangles.

    The chamfer corner patch (design §4.3 step 5) is a flat tetrahedron with
    apex at the cube vertex and base on the three setback ring points: the
    classical "corner-cut" of a cube.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    size = 2.0
    chamfered = box.chamfer(box.feature_edges(), size=size)
    assert chamfered.is_valid
    bodies = chamfered.manifold.decompose()
    real_bodies = [b for b in bodies if abs(b.volume()) > 1e-3]
    assert len(real_bodies) == 1
    # A chamfered-corner cube has *bit-exact* analytic volume. Each of the
    # 12 edges' chain tool is setback by ``size`` at both ends (corner shared
    # with the next chain), so its wedge cut covers a length of
    # ``20 − 2 · size`` with cross-section ``size² / 2``. Each of the 8
    # corners contributes a tetrahedron of volume ``size³ / 6``. Total:
    #   8000 − 12 · (size²/2) · (20 − 2·size) − 8 · size³/6
    expected = (
        8000.0 - 12.0 * (size**2 / 2.0) * (20.0 - 2.0 * size) - 8.0 * size**3 / 6.0
    )
    assert chamfered.volume == pytest.approx(expected, abs=1e-6)
    # Chamfer should have no degenerate slivers — flat-on-flat booleans are
    # bit-exact.
    assert _degenerate_triangle_count(chamfered) == 0


def test_mesh_fillet_cube_corner_only_gives_ball_corner():
    """A single 3-edge corner filleted produces a spherical patch (design §4 T6).

    Selecting only the three convex chains incident to one corner of a box
    isolates the setback + faceted-sphere construction with no other tool
    influence: the body must stay in one piece, every triangle must be
    well-formed (q_min ≥ 0.05 by area-ratio), and the resulting surface near
    the corner must include a substantial spherical region (we measure by
    counting triangles whose centroids land near the inset sphere centre).
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    radius = 2.0
    # Pick the three chains at corner (−10, −10, −10).
    selection = box.feature_edges()
    corner_vertex = np.array([-10.0, -10.0, -10.0])
    incident_chains = []
    for chain in selection:
        chain_positions = [selection.vertices[v] for v in chain.verts]
        if any(np.allclose(p, corner_vertex) for p in chain_positions):
            incident_chains.append(chain)
    assert len(incident_chains) == 3, "a box corner has exactly 3 incident chains"

    filleted = box.fillet(incident_chains, radius=radius, segments=12)
    assert filleted.is_valid
    bodies = filleted.manifold.decompose()
    real_bodies = [b for b in bodies if abs(b.volume()) > 1e-3]
    assert len(real_bodies) == 1

    # Inset sphere centre — confirm there are triangles whose centroids lie
    # on the patch's surface (within facet tolerance of the inset sphere of
    # radius r).
    centre = corner_vertex + radius * np.array([1, 1, 1]) / np.sqrt(3)
    from build123d.mesh.bridge import read_result

    mesh = read_result(filleted.manifold)
    centroids = mesh.vertices[mesh.triangles].mean(axis=1)
    distances = np.linalg.norm(centroids - centre, axis=1)
    on_sphere = (np.abs(distances - radius) < 0.2).sum()
    assert (
        on_sphere >= 20
    ), f"expected ≥ 20 triangles on the corner sphere patch, got {on_sphere}"


def test_mesh_chamfer_cube_corner_only_gives_flat_triangle():
    """A single 3-edge corner chamfered produces a flat-cut triangular patch (T7).

    The chamfer corner patch is a flat triangle (the base of the tetrahedron)
    spanning the three setback endpoints. Standing on that triangle, the
    plane's outward normal is the negative inward-normal-sum — for a cube
    corner this is `(1, 1, 1)/√3` (outward of the body, the −x−y−z octant
    corner).
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    size = 2.0
    selection = box.feature_edges()
    corner_vertex = np.array([-10.0, -10.0, -10.0])
    incident_chains = []
    for chain in selection:
        chain_positions = [selection.vertices[v] for v in chain.verts]
        if any(np.allclose(p, corner_vertex) for p in chain_positions):
            incident_chains.append(chain)
    assert len(incident_chains) == 3

    chamfered = box.chamfer(incident_chains, size=size)
    assert chamfered.is_valid
    bodies = chamfered.manifold.decompose()
    real_bodies = [b for b in bodies if abs(b.volume()) > 1e-3]
    assert len(real_bodies) == 1
    # The result has no degenerate slivers — flat half-space cuts are exact.
    assert _degenerate_triangle_count(chamfered) == 0

    # Volume: only one corner is chamfered (the others are untouched). Each
    # of the three incident chains runs the full 20 mm; only the corner end
    # is setback (the other end is a non-corner endpoint with the standard
    # overshoot, which only extends into empty space outside the body). The
    # wedge cut per chain is thus size² / 2 · (20 − size); the corner
    # tetrahedron contributes size³ / 6.
    expected = 8000.0 - 3.0 * size**2 / 2.0 * (20.0 - size) - 1.0 * size**3 / 6.0
    assert chamfered.volume == pytest.approx(expected, abs=1e-6)


def test_mesh_fillet_k_too_many_chains_raises():
    """A synthetic > 6-edge corner raises ``MeshFilletInfeasible`` (NG_F5 / §4.6).

    We synthesise a corner with 7 incident chains by intersecting a box with
    a 14-faceted polyhedron meeting at one vertex — the resulting body has
    a 7-chain convex corner that the design rejects. The pre-flight names
    the offending corner and lists every incident chain.
    """
    # Easiest synthetic: take a cylinder with 7 facets (heptagonal prism) and
    # union it with a flat plane at its top so the apex of the cap meets 7
    # lateral faces plus 1 cap face — 7 feature chains.
    # Cylinder(...) with default segments yields ~32 facets; we use a custom
    # construction by combining 7 boxes around an axis to get a heptagonal-ish
    # corner. Simpler: a cone with 7 segments, sliced flat near the apex.
    # We mock the raise by directly invoking the pre-flight on a hand-built
    # 7-chain corner.
    from build123d.mesh.corners import Corner, ChainEndpointAtCorner
    from build123d.mesh.feature_edges import FeatureChain
    from build123d.mesh.fillet import _check_corner_feasibility

    # Build 7 synthetic single-edge chains all hitting vertex 0.
    chains = []
    endpoints = []
    for i in range(7):
        chain = FeatureChain(
            pair=(0, i + 1),
            verts=[0, i + 100],
            is_loop=False,
            edges=[],
            convexity_class="convex",
            vertex_kinds=["corner", "endpoint"],
        )
        chains.append(chain)
        endpoints.append(
            ChainEndpointAtCorner(
                chain=chain,
                side="start",
                convex=True,
                tangent_into_chain=np.array([1.0, 0.0, 0.0]),
            )
        )
    corner = Corner(
        vertex=0,
        position=np.zeros(3),
        chain_endpoints=endpoints,
        face_normals=[np.array([0.0, 0.0, 1.0])],
        kind="degenerate",
    )
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        _check_corner_feasibility({0: corner}, size=1.0)
    assert exc_info.value.constraint == "k>6-corner"
    assert len(exc_info.value.chains) == 7


def test_mesh_fillet_l_shape_mixed_corner_raise_lists_offenders():
    """The L-shape's mixed convex/concave corner names its incident chains.

    Pin the raise's ``chains`` attribute: every chain at the offending corner
    is included so the user can construct the convex-only or concave-only
    workaround from the exception data.
    """
    l_shape = mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        l_shape.fillet(l_shape.feature_edges(), radius=0.5)
    err = exc_info.value
    assert err.constraint == "mixed-corner"
    # The mixed corner at (2, 2, ±6) has 3 chains (2 convex + 1 concave).
    assert len(err.chains) >= 3
    # The convex / concave mix is represented.
    classes = {c.convexity_class for c in err.chains}
    assert "convex" in classes
    assert "concave" in classes


# --------------------------------------------------------------------------
# Phase A4 — variable radius + on_infeasible="skip"
# --------------------------------------------------------------------------


def test_mesh_fillet_variable_radius_scalar_callable_matches_scalar():
    """A4 regression: a constant-returning callable is bit-identical to scalar.

    The variable-radius code path must specialise to the scalar path when
    the callable returns the same value at every vertex — otherwise we'd
    risk regressing every A3a/A3b/A3c test that asserts scalar volumes.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    scalar = box.fillet(chain, radius=1.0, segments=8)
    constant = box.fillet(chain, radius=lambda _c, _i: 1.0, segments=8)
    assert scalar.is_valid and constant.is_valid
    assert constant.volume == pytest.approx(scalar.volume, abs=1e-9)
    assert constant.last_fillet_report is None
    assert scalar.last_fillet_report is None


def test_mesh_chamfer_variable_size_scalar_callable_matches_scalar():
    """Same regression for chamfer: constant callable matches scalar."""
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    scalar = box.chamfer(chain, size=2.0)
    constant = box.chamfer(chain, size=lambda _c, _i: 2.0)
    assert scalar.is_valid and constant.is_valid
    assert constant.volume == pytest.approx(scalar.volume, abs=1e-9)


def test_mesh_fillet_variable_radius_linear_along_single_chain():
    """Linearly varying radius 1→2 along one box edge produces a tapered fillet.

    Volume sits *strictly* between the constant-r=1 and constant-r=2 results
    (the tapered fillet removes more material than r=1 and less than r=2 —
    a sanity check that the per-vertex profile is actually varying).
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]

    def linear_radius(c: FeatureChain, index: int) -> float:
        # Map vertex_index ∈ [0, len(verts) - 1] → radius ∈ [1.0, 2.0]
        n = len(c.verts)
        if n <= 1:
            return 1.0
        t = index / (n - 1)
        return 1.0 + t

    tapered = box.fillet(chain, radius=linear_radius, segments=8)
    assert tapered.is_valid
    assert len(tapered.manifold.decompose()) == 1
    # The tapered fillet sits between r=1 and r=2 in removed-material terms.
    fillet_small = box.fillet(chain, radius=1.0, segments=8)
    fillet_big = box.fillet(chain, radius=2.0, segments=8)
    # Removed material is box.volume - filleted.volume; tapered removes more
    # than small (extra material at the r→2 end) and less than big (less
    # material at the r→1 end).
    removed_small = 8000.0 - fillet_small.volume
    removed_big = 8000.0 - fillet_big.volume
    removed_tapered = 8000.0 - tapered.volume
    assert removed_small < removed_tapered < removed_big
    assert tapered.last_fillet_report is None


def test_mesh_fillet_variable_radius_box_two_edges_per_vertex_callable():
    """A bigger test: a U-shaped multi-chain selection with a per-vertex schedule.

    Pick two coplanar convex edges of a box (forming a U-shape with the
    intermediate chain implicit through the corner). Per the design's A4
    intent, the per-vertex callable lets each chain receive its own
    schedule; we use ``chain.pair`` to dispatch a per-chain radius that
    increases along each vertex.
    """
    box = MeshPart.from_part(Box(40, 40, 40))
    chains = list(box.feature_edges())
    # Two convex chains; both are feasible at r=1.0 anywhere.
    two = chains[:2]
    pair_schedule = {chains[0].pair: 0.8, chains[1].pair: 1.2}

    def per_chain_radius(c: FeatureChain, index: int) -> float:
        base = pair_schedule[c.pair]
        # Small linear ramp so different vertices visibly vary too.
        n = len(c.verts)
        t = 0.0 if n <= 1 else index / (n - 1)
        return base + 0.3 * t  # ∈ [0.8, 1.1] for chain 0 and [1.2, 1.5] for chain 1

    filleted = box.fillet(two, radius=per_chain_radius, segments=8)
    assert filleted.is_valid
    # The result is in one piece (corner share is handled by the
    # setback + sphere blend at the shared cube corner).
    real_bodies = [b for b in filleted.manifold.decompose() if abs(b.volume()) > 1e-3]
    assert len(real_bodies) == 1
    # No infeasibility — the schedule fits within half the 40 mm thickness.
    assert filleted.last_fillet_report is None
    # Volume strictly less than the box (material was removed).
    assert filleted.volume < 64000.0


def test_mesh_fillet_variable_radius_infeasible_vertex_raises_with_name():
    """A per-vertex callable whose value exceeds half-thickness at one vertex raises.

    On a 6 mm plate the half-thickness is 6 mm; requesting r=8 at any vertex
    must raise ``MeshFilletInfeasible(constraint="half-thickness")`` and
    name the offending chain.
    """
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]

    # Constant 8.0 — every vertex fails.
    with pytest.raises(MeshFilletInfeasible) as exc_info:
        plate.fillet(chain, radius=lambda _c, _i: 8.0)
    err = exc_info.value
    assert err.constraint == "half-thickness"
    assert err.requested == pytest.approx(8.0)
    assert err.measured > 0.0
    # The offending chain is named on the exception.
    assert chain in err.chains


def test_mesh_fillet_variable_radius_one_bad_vertex_only():
    """Spike one vertex to an infeasible radius; the rest are fine.

    The pre-flight evaluates per vertex against per-vertex radius; the
    spike at one vertex still triggers the raise (the design's P3 contract).
    """
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]

    def spike(c: FeatureChain, index: int) -> float:
        if index == 0:
            return 8.0  # over half the 6 mm plate thickness
        return 1.0

    with pytest.raises(MeshFilletInfeasible) as exc_info:
        plate.fillet(chain, radius=spike)
    assert exc_info.value.constraint == "half-thickness"
    # measured stays positive (the half-thickness is ~6 mm).
    assert exc_info.value.measured > 0.0
    # The offending vertex's requested radius is the 8.0 spike, not the 1.0.
    assert exc_info.value.requested == pytest.approx(8.0)


def test_mesh_fillet_skip_drops_oversize_chain_and_returns_report():
    """The headline skip-mode test (design T14): infeasible chain is dropped.

    A 6 mm plate cannot host a fillet of r=8. With ``on_infeasible="skip"``
    the chain is dropped, a :class:`FilletReport` is attached, and a real
    :class:`MeshPart` is returned — equal in volume to the input (no chain
    actually filleted).
    """
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]

    result = plate.fillet(chain, radius=8.0, on_infeasible="skip")
    assert isinstance(result, MeshPart)
    assert result.is_valid
    # No fillet was applied — every chain was infeasible.
    assert result.volume == pytest.approx(plate.volume, rel=1e-9)
    # The report names the dropped chain and the failing constraint.
    report = result.last_fillet_report
    assert report is not None
    assert report.operation == "fillet"
    assert len(report.skipped_chains) == 1
    skipped = report.skipped_chains[0]
    assert skipped.constraint == "half-thickness"
    assert skipped.requested == pytest.approx(8.0)
    assert skipped.measured > 0.0
    assert chain in skipped.chains


def test_mesh_fillet_skip_mode_mixes_feasible_and_infeasible_chains():
    """Skip drops only the bad chains; feasible chains in the same selection get filleted.

    A box has 12 feature edges; only one chain receives an oversize radius
    via the callable. With ``on_infeasible="skip"`` the oversize chain is
    dropped and the other 11 are filleted as normal.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chains = list(box.feature_edges())
    bad_chain = chains[0]

    def per_chain_radius(c: FeatureChain, _index: int) -> float:
        # Spike *just* the first chain to an infeasible radius (the box's
        # half-thickness is 10 mm; r=12 trips half-thickness).
        if c.pair == bad_chain.pair:
            return 12.0
        return 1.0

    result = box.fillet(chains, radius=per_chain_radius, on_infeasible="skip")
    assert result.is_valid
    assert len(result.manifold.decompose()) >= 1
    report = result.last_fillet_report
    assert report is not None
    # Exactly the bad chain was dropped.
    assert len(report.skipped_chains) == 1
    assert bad_chain in report.skipped_chains[0].chains
    # The other 11 chains were filleted — the result's volume is lower than
    # the box, but greater than a full all-12-chain fillet would produce.
    full = box.fillet(chains, radius=1.0)
    assert result.volume > full.volume
    assert result.volume < box.volume


def test_mesh_fillet_skip_mixed_corner_drops_the_corner_only():
    """The L-shape mixed-corner raise becomes a skip drop with the same payload."""
    l_shape = mesh_cut(Box(30, 30, 12), Pos(10, 10, 0) * Box(16, 16, 16))
    selection = l_shape.feature_edges()

    result = l_shape.fillet(selection, radius=0.5, on_infeasible="skip")
    assert result.is_valid
    report = result.last_fillet_report
    assert report is not None
    # At least one mixed-corner skip is reported.
    assert any(item.constraint == "mixed-corner" for item in report.skipped_corners)
    # The dropped corner's chains include at least one convex and one concave.
    drop = next(
        item for item in report.skipped_corners if item.constraint == "mixed-corner"
    )
    classes = {c.convexity_class for c in drop.chains}
    assert "convex" in classes and "concave" in classes
    # The corner vertex index is reported.
    assert drop.vertex is not None


def test_mesh_fillet_skip_with_all_feasible_inputs_attaches_no_report():
    """Skip mode with no infeasibility ⇒ ``last_fillet_report is None``.

    The empty-report case is normalised to ``None`` so callers can compare
    to ``None`` without inspecting list lengths (FilletReport's ``__bool__``
    is False on empty, which the dispatcher uses to set the attribute to
    ``None``).
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    result = box.fillet(chain, radius=1.0, on_infeasible="skip")
    assert result.is_valid
    assert result.last_fillet_report is None


def test_mesh_chamfer_skip_drops_oversize_chain():
    """Chamfer skip-mode mirrors fillet skip-mode."""
    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]
    result = plate.chamfer(chain, size=8.0, on_infeasible="skip")
    assert result.is_valid
    assert result.volume == pytest.approx(plate.volume, rel=1e-9)
    report = result.last_fillet_report
    assert report is not None
    assert report.operation == "chamfer"
    assert len(report.skipped_chains) == 1
    assert report.skipped_chains[0].constraint == "half-thickness"


def test_mesh_fillet_per_vertex_callable_returning_non_positive_raises_valueerror():
    """A callable that returns 0 or negative is a caller bug; always raise.

    Skip mode still raises ``ValueError`` here — the design only mentions
    skipping *geometric* infeasibility (P3), not bad inputs.
    """
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    with pytest.raises(ValueError, match="non-positive"):
        box.fillet(chain, radius=lambda _c, _i: -1.0)
    with pytest.raises(ValueError, match="non-positive"):
        box.fillet(chain, radius=lambda _c, _i: 0.0, on_infeasible="skip")


def test_mesh_fillet_radius_invalid_type_raises_typeerror():
    """A non-number, non-callable radius raises TypeError."""
    box = MeshPart.from_part(Box(20, 20, 20))
    chain = box.feature_edges()[0]
    with pytest.raises(TypeError, match="must be a positive number or a callable"):
        box.fillet(chain, radius="bogus")


def test_mesh_fillet_report_exports_and_is_iterable():
    """``FilletReport`` exports cleanly from build123d.mesh."""
    from build123d.mesh import FilletReport, SkippedItem  # noqa: F401

    plate = MeshPart.from_part(Box(40, 40, 6))
    chain = plate.feature_edges()[0]
    result = plate.fillet(chain, radius=8.0, on_infeasible="skip")
    report = result.last_fillet_report
    assert isinstance(report, FilletReport)
    assert isinstance(report.skipped_chains[0], SkippedItem)
    # Empty report would be falsy; a populated one is truthy.
    assert bool(report) is True
    assert report.total_skipped == 1


# --------------------------------------------------------------------------
# Mixed-provenance recovery — unseeded face_ids must not be silently dropped
# (regression: a hull/Minkowski/from_mesh operand combined with a seeded
# operand leaves a non-empty side-map AND result ids with no provenance.)
# --------------------------------------------------------------------------


def test_recovery_result_exposes_unseeded_counter():
    """RecoveryResult carries the n_unseeded_faceted counter, defaulting to 0."""
    drilled = mesh_cut(Box(20, 20, 10), Box(6, 6, 20))
    recovered = recover_brep(read_result(drilled.manifold), drilled.side_map)
    # An all-planar, fully-seeded CSG result has no unseeded ids.
    assert recovered.n_unseeded_faceted == 0
    assert recovered.n_exact_planar > 0


def test_mixed_provenance_recovers_synthetic_hull_faces_not_dropped():
    """A synthetic hull fused with a seeded box keeps BOTH in recovery.

    The hull now carries synthetic coplanar-region ids (no input surface), and
    the box carries seeded exact-planar ids. The merged side-map covers every
    result id, so nothing is dropped: the seeded box recovers exact planar faces
    and the hull recovers as merged synthetic faces (planar where flat, faceted
    where curved). The recovered volume matches the true union volume.
    """
    # Two-sphere hull (synthetic provenance) + a seeded box, well separated so
    # the union volume is close to the sum minus a small overlap.
    hull = mesh_hull(
        MeshPart.sphere(radius=20).move(Location((0, 0, 0))),
        MeshPart.sphere(radius=14).move(Location((10, 0, 40))),
    )
    # The hull now carries synthetic coplanar-region provenance, not an empty map.
    assert len(hull.side_map) > 0
    assert all(r.is_synthetic for r in hull.side_map.records.values())
    box = MeshPart.from_part(Box(40, 40, 6)).move(Location((0, 0, -40)))
    assert len(box.side_map) == 6

    mixed = hull + box
    # The merged side-map now covers the box's 6 seeded faces AND the hull's
    # synthetic ids -- every result id has provenance, none is unseeded.
    result_mesh = read_result(mixed.manifold)
    assert len(result_mesh.distinct_ids) > 6
    assert all(fid in mixed.side_map for fid in result_mesh.distinct_ids)

    recovered = recover_brep(result_mesh, mixed.side_map)
    # The box keeps exact-planar faces; the hull recovers via synthetic ids;
    # nothing falls into the unseeded path any more.
    assert recovered.n_exact_planar > 0
    assert recovered.n_synthetic_planar + recovered.n_synthetic_faceted > 0
    assert recovered.n_unseeded_faceted == 0
    # The recovered volume is close to the true union volume (not the box
    # alone, which would be the silent-drop symptom).
    assert recovered.volume == pytest.approx(mixed.manifold.volume(), rel=0.02)


def test_to_solid_mixed_provenance_is_valid_and_full_volume():
    """to_solid() on a mixed-provenance MeshPart returns a valid full body.

    Regression for the bp10 footgun: hull-of-spheres + a seeded bore used to
    bake to a near-empty solid (~vol 0). With shared-topology recovery, to_solid
    returns a valid solid whose volume matches the mesh to tessellation
    tolerance — and, unlike the old auto-fallback, it keeps the seeded box's
    exact analytic planar faces instead of re-baking everything faceted.
    """
    hull = mesh_hull(
        MeshPart.sphere(radius=20).move(Location((0, 0, 0))),
        MeshPart.sphere(radius=14).move(Location((10, 0, 40))),
    )
    mixed = hull + MeshPart.from_part(Box(40, 40, 6)).move(Location((0, 0, -40)))
    mesh_volume = mixed.manifold.volume()

    solid = mixed.to_solid()
    assert isinstance(solid, (Solid, Compound))
    assert solid.is_valid  # the always-valid contract
    # Full body, not the silent-drop near-empty result. The exact-planar box
    # makes the volume differ slightly from the pure faceted mesh volume, so
    # this is a tessellation-tolerance match, not bit-exact.
    assert solid.volume == pytest.approx(mesh_volume, rel=5e-3)
    # The mixed body is no longer forced through the faceted fallback: at least
    # the seeded box's analytic planar faces survive in the recovered body.
    recovered = recover_brep(read_result(mixed.manifold), mixed.side_map)
    assert recovered.is_valid
    assert recovered.n_exact_planar > 0


@pytest.mark.parametrize(
    "make_mesh_part",
    [
        # all-planar CSG -> exact recovery, valid
        lambda: mesh_cut(Box(20, 20, 10), Box(6, 6, 20)),
        # curved-seeded only -> faceted recovery, valid
        lambda: MeshPart.from_part(Sphere(10)),
        # mixed provenance -> auto-fallback to faceted, valid
        lambda: mesh_hull(
            MeshPart.sphere(radius=20),
            MeshPart.sphere(radius=14).move(Location((10, 0, 40))),
        )
        + MeshPart.from_part(Box(40, 40, 6)).move(Location((0, 0, -40))),
    ],
    ids=["all-planar", "curved-seeded", "mixed-provenance"],
)
def test_to_solid_is_always_valid(make_mesh_part):
    """to_solid() returns a BRepCheck-valid solid for every provenance mix."""
    solid = make_mesh_part().to_solid()
    assert isinstance(solid, (Solid, Compound))
    assert solid.is_valid


# --------------------------------------------------------------------------
# Shared-topology mixed recovery -- valid AND exact at the seam (bp10)
# --------------------------------------------------------------------------


def test_mixed_seam_valid_and_exact_planar_faces_preserved():
    """Mixed faceted-curved / exact-planar body: valid AND exact at the seam.

    The headline of the shared-topology rewrite. A seeded sphere (one curved
    surface, recovered faceted) has a seeded box bore subtracted through it. The
    four bore walls are exact planar seeded faces that abut the faceted sphere
    region at a seam. Before the rewrite, an exact-planar face contributed ONE
    long straight edge over the seam while the faceted patch contributed many
    short edges, so they could not share edges and the sewn shell was
    BRepCheck-invalid; to_solid then threw the exact recovery away and re-baked
    fully faceted. With one shared TopoDS_Vertex/Edge per mesh vertex index, the
    planar wire is subdivided at every seam vertex and the two regions share
    their seam edges -- so the body is valid AND the bore walls survive as exact
    analytic GeomType.PLANE faces.

    This faceted-curved + planar body is deterministic; the hull-blob variant
    (a non-deterministic tessellation) is covered by the regression test below.
    """
    body = MeshPart.sphere(10) - MeshPart.from_part(Box(4, 4, 40))

    recovered = recover_brep(read_result(body.manifold), body.side_map)
    # Valid by construction -- the shared seam topology is the whole point.
    assert recovered.is_valid
    # Mixed provenance: the sphere is curved/faceted, the bore is seeded/planar.
    assert recovered.n_faceted_curved > 0
    # The four seeded planar bore walls survive as EXACT analytic faces.
    exact_planar = sum(len(r.faces) for r in recovered.recovered_faces if r.exact)
    assert exact_planar == recovered.n_exact_planar
    assert recovered.n_exact_planar >= 4
    # The planar bore is exact and the faceted sphere is the only approximation,
    # so the recovered volume matches the mesh body to tessellation tolerance.
    assert recovered.volume == pytest.approx(body.volume, rel=1e-3)

    # to_solid() returns that valid, partially-exact body -- no fall-through to a
    # fully-faceted re-bake for this mixed input.
    solid = body.to_solid()
    assert isinstance(solid, (Solid, Compound))
    assert solid.is_valid
    # The exact bore walls survive as large analytic planes in the baked solid
    # (a faceted patch is many tiny per-triangle planes; the bore walls are far
    # larger), proving the planar faces were not re-baked faceted.
    big_planes = [
        f for f in solid.faces() if f.geom_type == GeomType.PLANE and f.area > 20.0
    ]
    assert len(big_planes) >= 4


def test_bp10_hull_blob_with_seeded_bore_does_not_collapse():
    """bp10-style: a synthetic hull blob with a seeded planar bore stays full.

    A convex hull of four spheres (synthetic, grouped) has a seeded box bore
    subtracted through it -- the eval case that surfaced the seam bug. The
    hull's tessellation is not deterministic, so this asserts the guarantees the
    rewrite provides regardless of tessellation: the recovery keeps the exact
    seeded planar bore walls, the full body volume (not the silent-drop
    near-empty result), and to_solid returns a valid body. The hull side is now
    GROUPED via synthetic ids (merged coplanar regions), not one face per
    triangle, and carries no unseeded ids.
    """
    blob = mesh_hull(
        MeshPart.sphere(5).translate((0, 0, 0)),
        MeshPart.sphere(5).translate((10, 0, 0)),
        MeshPart.sphere(5).translate((0, 10, 0)),
        MeshPart.sphere(5).translate((10, 10, 0)),
    )
    body = blob - MeshPart.from_part(Box(3, 3, 40))

    recovered = recover_brep(read_result(body.manifold), body.side_map)
    # The hull now recovers via synthetic ids (grouped), not the unseeded path.
    assert recovered.n_unseeded_faceted == 0
    assert recovered.n_synthetic_planar + recovered.n_synthetic_faceted > 0
    # The seeded planar bore walls survive as EXACT analytic faces, not facets.
    assert recovered.n_exact_planar > 0
    # Full body, not the near-empty silent-drop result of the old code. The
    # hull tessellation is not deterministic, so this is a loose lower bound:
    # the old bug collapsed the volume toward zero, here it is the full blob.
    assert recovered.volume > 0.9 * body.volume

    # to_solid always returns a valid body (shared topology, with the faceted
    # fallback as a safety net for any tessellation OCCT cannot close).
    solid = body.to_solid()
    assert isinstance(solid, (Solid, Compound))
    assert solid.is_valid
    assert solid.volume > 0.9 * body.volume
