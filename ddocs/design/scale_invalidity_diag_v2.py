"""§K.49 open item, round 2 -- grid 4 already fails, and NOT with zero
attributable subshapes: round 1 (scale_invalidity_diag_v1.py) found 2/42277
FACEs individually BRepCheck-invalid at grid 4 (the smallest failing case).
This tags every recovered Face with its provenance (exact planar / planar
off-plane-coplanarity-fallback / faceted-curved / faceted-unseeded) so the
two bad faces can be attributed to a specific recovery path and geometry.

Usage:
    PYTHONPATH=<worktree>/src <venv>/bin/python -u ddocs/design/scale_invalidity_diag_v2.py
"""
import signal
import time
from collections import defaultdict

import numpy as np

from build123d import Location
from build123d.mesh import MeshPart, mesh_cut
from build123d.mesh.bridge import read_result
from build123d.mesh import recovery as recovery_mod
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
from OCP.TopoDS import TopoDS_Shell, TopoDS
from build123d.topology.utils import group_shells_into_solids
from build123d import Shell as B3DShell


class Timeout(Exception):
    pass


def _alarm(_sig, _frame):
    raise Timeout()


signal.signal(signal.SIGALRM, _alarm)


def log(msg):
    print(msg, flush=True)


def bbox_of(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box.Get()


def build_case(grid_n, pitch=9.0, thickness=4.0, radius=0.5):
    span = (grid_n - 1) * pitch
    side = span + 30.0
    panel = MeshPart.box(side, side, thickness)
    start = -span / 2.0
    holes = [
        MeshPart.cylinder(radius=2.0, height=thickness * 3).move(
            Location((start + i * pitch, start + j * pitch, 0))
        )
        for i in range(grid_n)
        for j in range(grid_n)
    ]
    drilled = mesh_cut(panel, *holes)
    chains = drilled.feature_edges()
    result = drilled.fillet(chains, radius=radius, on_infeasible="skip")
    return result, holes


def tagged_recover(result):
    """Reimplements recover_brep's core loop with provenance tagging on every
    built Face (python-object identity keyed), plus per-face triangle-index
    provenance for faceted (one-triangle-per-face) builds.
    """
    face_tags: dict[int, str] = {}
    face_tri: dict[int, int] = {}  # id(face) -> triangle_index, for faceted builds
    face_fid: dict[int, int] = {}  # id(face) -> originating face_id

    orig_triangle_faces = recovery_mod._triangle_faces
    context = {"tag": "unknown", "face_id": -1}

    def tagged_triangle_faces(result_mesh, triangle_indices, topology, component_of_triangle, faces_by_component):
        faces = orig_triangle_faces(result_mesh, triangle_indices, topology, component_of_triangle, faces_by_component)
        # _triangle_faces silently skips triangles whose wire/face build
        # failed, so faces and triangle_indices are not guaranteed same
        # length/order in general -- but in practice (no skips) they zip.
        ti = list(triangle_indices)
        for i, face in enumerate(faces):
            face_tags[id(face)] = context["tag"]
            face_fid[id(face)] = context["face_id"]
            if i < len(ti):
                face_tri[id(face)] = int(ti[i])
        return faces

    recovery_mod._triangle_faces = tagged_triangle_faces
    try:
        result_mesh = read_result(result.manifold)
        side_map = result._side_map
        result_mesh_w = recovery_mod._weld_degenerate_triangles(result_mesh)
        topology = recovery_mod._SharedTopology(
            recovery_mod._vertex_positions(result_mesh_w, side_map)
        )
        component_of_triangle = recovery_mod.connected_components_by_vertex(
            result_mesh_w.triangles, len(result_mesh_w.vertices)
        )
        faces_by_component = defaultdict(list)

        for face_id in result_mesh_w.distinct_ids:
            context["face_id"] = face_id
            if face_id not in side_map:
                context["tag"] = "faceted-unseeded"
                recovery_mod._faceted_patch(
                    result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
                )
                continue
            record = side_map[face_id]
            if record.is_synthetic:
                context["tag"] = "synthetic"
                recovery_mod._recover_synthetic_face(
                    result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
                )
            elif record.is_planar:
                context["tag"] = "planar-offplane-fallback"  # only path that calls _triangle_faces
                before = set()
                for lst in faces_by_component.values():
                    before.update(id(f) for f in lst)
                recovered, n_fb = recovery_mod._recover_planar_face(
                    result_mesh_w, face_id, record, topology, component_of_triangle, faces_by_component
                )
                # Faces built by the exact-plane path (not _triangle_faces)
                # never hit tagged_triangle_faces -- tag them here directly.
                for f in recovered.faces:
                    if id(f) not in face_tags:
                        face_tags[id(f)] = "planar-exact"
                        face_fid[id(f)] = face_id
            else:
                context["tag"] = f"faceted-curved:{record.surface_kind}"
                recovery_mod._faceted_patch(
                    result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
                )
    finally:
        recovery_mod._triangle_faces = orig_triangle_faces

    builder = BRep_Builder()
    shells = []
    for component in sorted(faces_by_component):
        faces = faces_by_component[component]
        if not faces:
            continue
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        for face in faces:
            builder.Add(shell, face.wrapped)
        shell.Closed(True)
        shells.append(B3DShell(shell))

    raw_solids = []
    for outer_shell, void_shells in group_shells_into_solids(shells):
        solid_builder = BRepBuilderAPI_MakeSolid(outer_shell.wrapped)
        for void_shell in void_shells:
            solid_builder.Add(void_shell.wrapped)
        if not solid_builder.IsDone():
            continue
        raw_solids.append(TopoDS.Solid_s(solid_builder.Solid()))

    return raw_solids, faces_by_component, face_tags, face_tri, face_fid, result_mesh_w, topology


def main():
    grid_n = 4
    log(f"grid {grid_n}: building...")
    result, holes = build_case(grid_n)
    log("building recovery with provenance tags...")
    raw_solids, faces_by_component, face_tags, face_tri, face_fid, result_mesh_w, topology = tagged_recover(result)
    log(f"{len(raw_solids)} raw solid(s)")

    tag_counts = defaultdict(int)
    for t in face_tags.values():
        tag_counts[t] += 1
    log(f"tag histogram: {dict(tag_counts)}")

    for i, raw_solid in enumerate(raw_solids):
        log(f"solid[{i}]:")
        analyzer = BRepCheck_Analyzer(raw_solid)
        log(f"  IsValid = {analyzer.IsValid()}")

        all_faces = [f for lst in faces_by_component.values() for f in lst]
        log(f"  total tracked faces: {len(all_faces)}")
        bad = []
        for f in all_faces:
            if not analyzer.IsValid(f.wrapped):
                bad.append(f)
        log(f"  bad (standalone-invalid) faces: {len(bad)}")
        for f in bad:
            tag = face_tags.get(id(f), "UNTAGGED")
            fid = face_fid.get(id(f), -1)
            tri = face_tri.get(id(f), None)
            bbox = bbox_of(f.wrapped)
            fres = analyzer.Result(f.wrapped)
            statuses = [s.name for s in fres.Status()]
            log(f"    tag={tag} face_id={fid} triangle_index={tri} bbox={bbox}")
            log(f"    face blind BRepCheck status: {statuses}")
            if tri is not None:
                tri_verts = result_mesh_w.triangles[tri]
                pts = result_mesh_w.vertices[tri_verts]
                log(f"    triangle vertices: {pts.tolist()}")
                # Find nearest hole center to attribute which bore/band.
                centroid = pts.mean(axis=0)
                log(f"    triangle centroid: {centroid.tolist()}")


if __name__ == "__main__":
    main()
