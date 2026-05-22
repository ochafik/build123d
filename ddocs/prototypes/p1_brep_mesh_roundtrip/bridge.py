"""
bridge.py -- the BREP <-> mesh interop primitive for build123d + manifold3d.

This module is the *foundational* round-trip layer the rest of the prototype
(and any future manifold3d backend) builds on.  Two directions:

    solid_to_manifold(shape)   build123d Shape  -> manifold3d.Manifold
    manifold_to_solid(man)     manifold3d.Manifold -> build123d Solid/Shell

KEY FACT (research doc 02, sec 1.2): OCC's `Shape.tessellate()` produces a
*non-indexed soup at face seams* -- vertices are shared within a face but never
between faces.  A Box -> 24 vertices, not 8.  manifold3d requires a properly
indexed, watertight, 2-manifold mesh, so the OUT leg MUST weld coincident
vertices before handing the mesh to manifold3d.

Tested against: build123d 0.1.dev (fork), manifold3d 3.4.1.
manifold3d 3.4.1's float32 data struct is called `Mesh` (what 2.x docs call
`MeshGL`); there is also a double-precision `Mesh64`.  We use `Mesh64` for the
OUT leg so OCC's double-precision vertices survive without float32 truncation.
"""

from __future__ import annotations

import time

import numpy as np
import manifold3d as m3d

from build123d import Shape, Solid, Shell, Compound

# OCP imports -- the low-level OpenCASCADE handles we need for the IN leg.
from OCP.gp import gp_Pnt
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeSolid,
)
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_ShapeEnum


# ---------------------------------------------------------------------------
# OUT leg:  build123d Shape -> manifold3d.Manifold
# ---------------------------------------------------------------------------

def tessellate_to_arrays(shape: Shape, tolerance: float = 1e-3,
                         angular_tolerance: float = 0.1):
    """Tessellate a build123d Shape into raw numpy vertex/triangle arrays.

    Thin wrapper over `Shape.tessellate()` (shape_core.py:2241).  Returns the
    *un-welded* soup exactly as OCC produces it -- duplicate vertices at face
    seams included.  Welding is a separate, explicit step (see `weld_vertices`)
    so callers can measure the dedup ratio.

    Returns
    -------
    verts : float64 (N, 3)
    tris  : uint32  (M, 3)
    """
    bd_verts, bd_tris = shape.tessellate(tolerance, angular_tolerance)
    verts = np.array([(v.X, v.Y, v.Z) for v in bd_verts], dtype=np.float64)
    tris = np.array(bd_tris, dtype=np.uint32)
    return verts, tris


def weld_vertices(verts: np.ndarray, tris: np.ndarray, decimals: int = 6):
    """Merge coincident vertices by grid-snapping, re-index triangles.

    OCC tessellation never dedups across face seams, so a watertight solid
    comes out as a vertex soup.  manifold3d's `Manifold(mesh)` constructor
    merges *only* according to explicit merge vectors -- it does NOT weld by
    distance -- so an un-welded soup is reported `NotManifold` (every seam edge
    is used by 1 triangle on each side, but the two sides reference *different*
    vertex indices => topologically open).

    This mirrors `Mesher._create_3mf_mesh` (mesher.py:312): round each vertex
    to `decimals` places (TOLERANCE 1e-6 -> 6 digits) and build a
    snapped-coord -> index map.  Grid-snap, not a true spatial cluster, but it
    matches build123d's own dedup and is what OCC's 1e-6 tolerance implies.

    Degenerate triangles (two welded indices coincide) are dropped.

    Returns
    -------
    welded_verts : float64 (K, 3)   K <= N
    welded_tris  : uint32  (L, 3)   L <= M
    """
    quantized = np.round(verts, decimals)
    # `unique` with axis=0 returns the unique rows plus, via inverse, a map
    # old-index -> new-index.  That inverse IS the re-indexing we need.
    uniq, inverse = np.unique(quantized, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)  # numpy>=2 returns shape (N,1) sometimes

    # Average the original (un-snapped) coordinates that map to each new vertex
    # so we keep full float precision rather than the snapped grid coordinate.
    welded = np.zeros((len(uniq), 3), dtype=np.float64)
    counts = np.zeros(len(uniq), dtype=np.int64)
    np.add.at(welded, inverse, verts)
    np.add.at(counts, inverse, 1)
    welded /= counts[:, None]

    new_tris = inverse[tris]
    # Drop triangles that became degenerate after welding.
    a, b, c = new_tris[:, 0], new_tris[:, 1], new_tris[:, 2]
    keep = (a != b) & (b != c) & (a != c)
    new_tris = new_tris[keep].astype(np.uint32)
    return welded, new_tris


def solid_to_manifold(shape: Shape, tolerance: float = 1e-3,
                      angular_tolerance: float = 0.1, decimals: int = 6):
    """build123d Shape -> manifold3d.Manifold (the OUT leg).

    Pipeline: tessellate -> weld coincident vertices -> Mesh64 -> Manifold.

    Returns
    -------
    man    : manifold3d.Manifold      (check `.status()` / `.is_empty()`!)
    info   : dict of measured numbers (raw/welded counts, timings)
    """
    info = {}

    t0 = time.perf_counter()
    raw_verts, raw_tris = tessellate_to_arrays(shape, tolerance, angular_tolerance)
    t1 = time.perf_counter()

    welded_verts, welded_tris = weld_vertices(raw_verts, raw_tris, decimals)
    t2 = time.perf_counter()

    # Mesh64 = double-precision data struct; keeps OCC's f64 vertices intact.
    mesh = m3d.Mesh64(
        vert_properties=np.ascontiguousarray(welded_verts, dtype=np.float64),
        tri_verts=np.ascontiguousarray(welded_tris, dtype=np.uint32),
    )
    man = m3d.Manifold(mesh)
    t3 = time.perf_counter()

    info.update(
        raw_verts=len(raw_verts),
        raw_tris=len(raw_tris),
        welded_verts=len(welded_verts),
        welded_tris=len(welded_tris),
        dedup_ratio=len(raw_verts) / max(1, len(welded_verts)),
        t_tessellate=t1 - t0,
        t_weld=t2 - t1,
        t_build_manifold=t3 - t2,
        status=str(man.status()),
        is_empty=man.is_empty(),
    )
    if not man.is_empty():
        info["genus"] = man.genus()
        info["volume"] = man.volume()
        info["surface_area"] = man.surface_area()
    return man, info


# ---------------------------------------------------------------------------
# IN leg:  manifold3d.Manifold -> build123d Solid
# ---------------------------------------------------------------------------

def manifold_to_arrays(man: m3d.Manifold):
    """Extract welded numpy vertex/triangle arrays from a Manifold.

    manifold3d output is *already* watertight, 2-manifold, indexed -- no weld
    needed on this leg.  We pull positions (cols 0-2 of vert_properties).
    """
    mesh = man.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.uint32)
    return verts, tris


def manifold_to_solid(man: m3d.Manifold):
    """manifold3d.Manifold -> build123d Solid (the IN leg).

    Uses the *only* mechanism in build123d that yields a real Solid from an
    arbitrary mesh: `Mesher._get_shape`'s approach (mesher.py:460) -- build one
    planar TopoDS_Face per triangle, feed them all into a single
    `BRepBuilderAPI_Sewing`, then `BRepBuilderAPI_MakeSolid` on the sewn shell.

    This is faithful to build123d's own code path; we reimplement it here
    (rather than calling Mesher) to avoid a round-trip through lib3mf/3MF files
    and to measure the sewing cost directly.

    Returns
    -------
    shape  : build123d Solid (if the sewn shell is manifold) or Shell
    info   : dict of measured numbers (timings, validity, face count)
    """
    info = {}
    verts, tris = manifold_to_arrays(man)
    info["in_verts"] = len(verts)
    info["in_tris"] = len(tris)

    t0 = time.perf_counter()
    gp_pnts = [gp_Pnt(float(x), float(y), float(z)) for x, y, z in verts]

    sewing = BRepBuilderAPI_Sewing()
    facet_props = GProp_GProps()
    n_added = 0
    n_zero_area = 0
    for tri in tris:
        p0, p1, p2 = gp_pnts[tri[0]], gp_pnts[tri[1]], gp_pnts[tri[2]]
        # 3 points -> closed triangular wire -> planar face.
        poly = BRepBuilderAPI_MakePolygon(p0, p1, p2, Close=True)
        face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
        # Drop zero-area facets (mirrors Mesher: Mass()==0 is invalid).
        BRepGProp.SurfaceProperties_s(face, facet_props)
        if facet_props.Mass() != 0:
            sewing.Add(face)
            n_added += 1
        else:
            n_zero_area += 1
    t1 = time.perf_counter()

    sewing.Perform()  # <-- the bottleneck; super-linear in face count.
    sewed = _downcast(sewing.SewedShape())
    t2 = time.perf_counter()

    # The sewn result is either one Shell or a Compound of Shells (parts with
    # internal voids).  Mirror Mesher: largest-bbox shell is the outer shell.
    if isinstance(sewed, TopoDS_Compound):
        shells = []
        exp = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
        while exp.More():
            shells.append(Shell(TopoDS.Shell_s(exp.Current())))
            exp.Next()
    else:
        assert isinstance(sewed, TopoDS_Shell), f"unexpected sewn type {type(sewed)}"
        shells = [Shell(sewed)]

    # --- Shell classification ---------------------------------------------
    # `Mesher._get_shape` has a real bug here: it treats the largest-bbox shell
    # as the outer shell and ALL others as internal voids.  That is wrong for a
    # mesh with multiple *disjoint* solids (e.g. a build123d Compound) -- those
    # extra shells are separate solids, not voids, and feeding them to
    # `MakeSolid` as voids yields an INVALID Solid.
    #
    # Correct rule: shell B is a void of shell A iff B is geometrically nested
    # inside A.  We classify by bounding-box containment (cheap, good enough for
    # the closed manifold meshes manifold3d emits) and then group: each
    # top-level (non-nested) shell becomes its own Solid, with its nested shells
    # added as voids.  Multiple top-level solids -> a Compound.
    not_manifold = [s for s in shells if not s.is_manifold]
    groups = _group_shells_into_solids(shells)

    if not_manifold:
        # At least one shell did not close -> cannot build a valid Solid.
        # Return the (silently degraded) collection of shells, as Mesher would.
        shape = not_manifold[0] if len(not_manifold) == 1 else Compound(children=not_manifold)
        kind = "Shell"
    else:
        solids = []
        for outer, inners in groups:
            builder = BRepBuilderAPI_MakeSolid(outer.wrapped)
            for inner in inners:
                builder.Add(inner.wrapped)
            solids.append(Solid(builder.Solid()))
        if len(solids) == 1:
            shape = solids[0]
            kind = "Solid"
        else:
            shape = Compound(children=solids)
            kind = "Compound"
    t3 = time.perf_counter()

    info.update(
        n_faces_added=n_added,
        n_zero_area_dropped=n_zero_area,
        n_shells=len(shells),
        n_solids=len(groups),
        result_kind=kind,
        outer_is_manifold=len(not_manifold) == 0,
        t_build_faces=t1 - t0,
        t_sewing=t2 - t1,
        t_make_solid=t3 - t2,
        t_total=t3 - t0,
    )
    return shape, info


def _group_shells_into_solids(shells):
    """Group sewn shells into (outer, [voids]) tuples by bbox nesting.

    Returns one tuple per top-level solid.  A shell whose bounding box is
    strictly inside another shell's bbox is classified as a void of the
    smallest such enclosing shell.
    """
    boxes = [s.bounding_box() for s in shells]

    def inside(inner_bb, outer_bb):
        eps = 1e-7
        return (outer_bb.min.X - eps <= inner_bb.min.X and
                outer_bb.min.Y - eps <= inner_bb.min.Y and
                outer_bb.min.Z - eps <= inner_bb.min.Z and
                inner_bb.max.X <= outer_bb.max.X + eps and
                inner_bb.max.Y <= outer_bb.max.Y + eps and
                inner_bb.max.Z <= outer_bb.max.Z + eps)

    def bb_vol(bb):
        s = bb.size
        return s.X * s.Y * s.Z

    parent = [None] * len(shells)
    for i in range(len(shells)):
        for j in range(len(shells)):
            if i == j:
                continue
            if inside(boxes[i], boxes[j]):
                # j encloses i; keep the *smallest* such enclosing shell.
                if parent[i] is None or bb_vol(boxes[j]) < bb_vol(boxes[parent[i]]):
                    parent[i] = j

    groups = []
    for i in range(len(shells)):
        if parent[i] is None:  # a top-level (outer) shell
            voids = [shells[k] for k in range(len(shells)) if parent[k] == i]
            groups.append((shells[i], voids))
    return groups


def _downcast(shape):
    """Cast a generic TopoDS_Shape down to its concrete subclass.

    `BRepBuilderAPI_Sewing.SewedShape()` returns a generic TopoDS_Shape; we
    need to know whether it is a Shell or a Compound.  build123d has an
    internal `downcast` but it is not part of the public API, so we replicate
    the minimal logic here.
    """
    from OCP.TopAbs import TopAbs_ShapeEnum as _SE
    kind = shape.ShapeType()
    if kind == _SE.TopAbs_COMPOUND:
        return TopoDS.Compound_s(shape)
    if kind == _SE.TopAbs_SHELL:
        return TopoDS.Shell_s(shape)
    if kind == _SE.TopAbs_SOLID:
        return TopoDS.Solid_s(shape)
    if kind == _SE.TopAbs_FACE:
        return TopoDS.Face_s(shape)
    return shape
