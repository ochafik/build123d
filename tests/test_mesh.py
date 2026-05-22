"""

build123d mesh backend tests

name: test_mesh.py

desc: Unit tests for the build123d.mesh manifold3d backend (Phase 0):
      Solid.from_mesh, Shape.tessellate(weld=True), and the free-function
      CSG API (mesh_fuse / mesh_cut / mesh_intersect).

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

from build123d import Box, Compound, Cylinder, Pos, Solid, Sphere

# manifold3d is an optional extra; skip the whole module if it is absent. The
# build123d.mesh imports below must therefore follow this importorskip.
manifold3d = pytest.importorskip("manifold3d")

# pylint: disable=wrong-import-position
from build123d.mesh import (  # noqa: E402
    MeshPart,
    is_available,
    mesh_cut,
    mesh_fuse,
    mesh_intersect,
)
from build123d.mesh.bridge import shape_to_manifold  # noqa: E402

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
    manifold = shape_to_manifold(Box(10, 10, 10))
    assert manifold.status() == manifold3d.Error.NoError
    assert not manifold.is_empty()
    assert manifold.volume() == pytest.approx(1000.0, rel=1e-6)


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
