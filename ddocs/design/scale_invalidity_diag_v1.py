"""§K.49 open item -- diagnose the whole-solid-only BRepCheck_Analyzer failure.

Sweeps grid 3, 4, 5 perforated panels (same pattern as bake_scaling_v1.py),
bypassing the _SHAPE_FIX_SOLID_FACE_LIMIT gate (diagnostic-only monkeypatch)
so recover_brep's normal check+repair path runs at every scale instead of
being skipped. At the SMALLEST grid where the *raw* (pre-ShapeFix) solid
fails BRepCheck_Analyzer.IsValid() but every individual subshape passes,
walk the analyzer's per-subshape Result/StatusOnShape maps to find exactly
which contextual status fires and on what geometry.

Usage:
    PYTHONPATH=<worktree>/src <venv>/bin/python -u ddocs/design/scale_invalidity_diag_v1.py
"""
import signal
import sys
import time

import numpy as np

from build123d import Location
from build123d.mesh import mesh_cut
from build123d.mesh.bridge import read_result
from build123d.mesh import mesh_part as mesh_part_mod
from build123d.mesh import recovery as recovery_mod
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_WIRE, TopAbs_EDGE, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps


class Timeout(Exception):
    pass


def _alarm(_sig, _frame):
    raise Timeout()


signal.signal(signal.SIGALRM, _alarm)


def log(msg):
    print(msg, flush=True)


def explore(shape, kind):
    out = []
    exp = TopExp_Explorer(shape, kind)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def bbox_of(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmin, ymin, zmin, xmax, ymax, zmax)


def build_case(grid_n, pitch=9.0, thickness=4.0, radius=0.5):
    """Mirror bake_scaling_v1.run()'s geometry-building steps only."""
    span = (grid_n - 1) * pitch
    side = span + 30.0
    from build123d.mesh import MeshPart

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
    return result


def recover_gate_bypassed(result, cap_s=240):
    """Call recover_brep with the face-count gate patched wide open.

    Diagnostic-only: makes recover_brep run its check(+repair) path
    regardless of solid size, instead of leaving is_valid=None above the
    normal _SHAPE_FIX_SOLID_FACE_LIMIT bound.
    """
    orig_limit = recovery_mod._SHAPE_FIX_SOLID_FACE_LIMIT
    recovery_mod._SHAPE_FIX_SOLID_FACE_LIMIT = 10**9
    try:
        signal.alarm(cap_s)
        result_mesh = read_result(result.manifold)
        recovered = recovery_mod.recover_brep(result_mesh, result._side_map)
        signal.alarm(0)
        return recovered
    finally:
        recovery_mod._SHAPE_FIX_SOLID_FACE_LIMIT = orig_limit
        signal.alarm(0)


def raw_solid_direct_check(result, cap_s=240):
    """Like recover_gate_bypassed, but skips ShapeFix_Solid entirely --
    a *direct* BRepCheck_Analyzer(raw_solid) with no repair attempt, on the
    exact solid recover_brep would have built pre-K.49-gate. Reimplements
    just the tail of recover_brep (shell/solid assembly) without the
    check-then-repair branch, so we see the truly-raw verdict.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.TopoDS import TopoDS_Shell, TopoDS
    from build123d.topology.utils import group_shells_into_solids
    from build123d import Shell as B3DShell, Solid as B3DSolid, Compound as B3DCompound

    result_mesh = read_result(result.manifold)
    side_map = result._side_map
    result_mesh_w = recovery_mod._weld_degenerate_triangles(result_mesh)
    topology = recovery_mod._SharedTopology(
        recovery_mod._vertex_positions(result_mesh_w, side_map)
    )
    component_of_triangle = recovery_mod.connected_components_by_vertex(
        result_mesh_w.triangles, len(result_mesh_w.vertices)
    )
    faces_by_component = {}
    n_planar_off_plane_faceted = 0
    from collections import defaultdict

    faces_by_component = defaultdict(list)
    for face_id in result_mesh_w.distinct_ids:
        if face_id not in side_map:
            recovery_mod._faceted_patch(
                result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
            )
            continue
        record = side_map[face_id]
        if record.is_synthetic:
            recovery_mod._recover_synthetic_face(
                result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
            )
        elif record.is_planar:
            _rec, n_fb = recovery_mod._recover_planar_face(
                result_mesh_w, face_id, record, topology, component_of_triangle, faces_by_component
            )
            n_planar_off_plane_faceted += n_fb
        else:
            recovery_mod._faceted_patch(
                result_mesh_w, face_id, topology, component_of_triangle, faces_by_component
            )

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

    return raw_solids, n_planar_off_plane_faceted, component_of_triangle, result_mesh_w, topology


def analyse_solid(raw_solid, cap_s=240):
    """Direct BRepCheck_Analyzer on the raw (unrepaired) solid; per-subshape
    breakdown of what fails at what topological level."""
    signal.alarm(cap_s)
    t0 = time.time()
    analyzer = BRepCheck_Analyzer(raw_solid)
    top_valid = analyzer.IsValid()
    t_check = time.time() - t0
    signal.alarm(0)
    log(f"    BRepCheck_Analyzer(raw_solid).IsValid() = {top_valid}  ({t_check:.1f}s)")
    if top_valid:
        return None

    # Per-subshape-kind pass/fail via IsValid(subshape) (checks the subshape
    # AND all its own subshapes -- but each call is scoped to its own subtree,
    # so a SOLID-level defect with no attributable subshape will show every
    # constituent subshape passing while the SOLID itself (checked alone,
    # which IsValid(solid_shape) does do) still fails).
    solids = explore(raw_solid, TopAbs_SOLID)
    shells = explore(raw_solid, TopAbs_SHELL)
    faces = explore(raw_solid, TopAbs_FACE)
    wires = explore(raw_solid, TopAbs_WIRE)
    edges = explore(raw_solid, TopAbs_EDGE)
    verts = explore(raw_solid, TopAbs_VERTEX)
    log(f"    counts: solids={len(solids)} shells={len(shells)} faces={len(faces)} "
        f"wires={len(wires)} edges={len(edges)} verts={len(verts)}")

    def check_all(label, shapes, cap=60):
        signal.alarm(cap)
        try:
            bad = [s for s in shapes if not analyzer.IsValid(s)]
        except Timeout:
            log(f"    [{label}] TIMEOUT after {cap}s scanning {len(shapes)} subshapes")
            return []
        finally:
            signal.alarm(0)
        log(f"    [{label}] {len(bad)}/{len(shapes)} fail IsValid() standalone")
        return bad

    bad_faces = check_all("FACE", faces)
    bad_wires = check_all("WIRE", wires)
    bad_edges = check_all("EDGE", edges)
    bad_verts = check_all("VERTEX", verts)
    bad_shells = check_all("SHELL", shells)
    bad_solids = check_all("SOLID (standalone IsValid(solid))", solids)

    # Now walk the analyzer's own Result maps for contextual statuses --
    # solid-in-context-of-nothing (top), and shell-in-context-of-solid,
    # face-in-context-of-shell.
    log("    -- contextual BRepCheck_Result walk --")
    for solid in solids:
        sres = analyzer.Result(solid)
        blind = list(sres.Status())
        log(f"    SOLID blind status: {[s.name for s in blind]}")
        # constituent shells in context of this solid
        for shell in shells:
            try:
                st = list(sres.StatusOnShape(shell))
            except Exception as exc:  # noqa: BLE001
                st = [f"ERR:{exc}"]
            if st and st != [recovery_mod.__dict__.get("_NOERR")]:
                interesting = [s for s in st if getattr(s, "name", str(s)) != "BRepCheck_NoError"]
                if interesting:
                    log(f"      SHELL {shell.HashCode(2**31-1) if hasattr(shell,'HashCode') else id(shell)} "
                        f"in-context-of-SOLID status: {[getattr(s,'name',s) for s in interesting]}  "
                        f"bbox={bbox_of(shell)}")
        # Use the InContext iterator API too, in case StatusOnShape needs
        # exact subshape instances rather than any TopoDS_Shape.
        sres.InitContextIterator()
        n_ctx = 0
        while sres.MoreShapeInContext():
            n_ctx += 1
            sres.NextShapeInContext()
        log(f"    SOLID result InContext iterator length: {n_ctx}")

    for shell in shells:
        shres = analyzer.Result(shell)
        blind = [s.name for s in shres.Status() if s.name != "BRepCheck_NoError"]
        if blind:
            log(f"    SHELL blind status: {blind}  bbox={bbox_of(shell)}")
        bad_in_shell = []
        for face in faces:
            try:
                st = [s for s in shres.StatusOnShape(face) if s.name != "BRepCheck_NoError"]
            except Exception:
                st = []
            if st:
                bad_in_shell.append((face, [s.name for s in st]))
        if bad_in_shell:
            log(f"    SHELL: {len(bad_in_shell)} faces have non-trivial in-context status")
            for face, statuses in bad_in_shell[:20]:
                log(f"      FACE in-shell-context status={statuses} bbox={bbox_of(face)}")

    return {
        "bad_faces": bad_faces,
        "bad_wires": bad_wires,
        "bad_edges": bad_edges,
        "bad_verts": bad_verts,
        "bad_shells": bad_shells,
        "bad_solids": bad_solids,
    }


def main():
    log("scale_invalidity_diag_v1 -- §K.49 open-item root cause hunt")
    log("=" * 88)
    for grid_n in (3, 4, 5):
        log(f"grid {grid_n}:")
        t0 = time.time()
        try:
            result = build_case(grid_n)
        except Timeout:
            log(f"  build TIMED OUT")
            continue
        t_build = time.time() - t0
        log(f"  built in {t_build:.1f}s")

        t0 = time.time()
        raw_solids, n_fb, component_of_triangle, result_mesh_w, topology = raw_solid_direct_check(result)
        t_recover = time.time() - t0
        log(f"  recovered {len(raw_solids)} raw solid(s) in {t_recover:.1f}s, "
            f"n_planar_off_plane_faceted={n_fb}, total faceted triangles={len(result_mesh_w.triangles)}")

        any_bad = False
        for i, raw_solid in enumerate(raw_solids):
            n_faces = 0
            exp = TopExp_Explorer(raw_solid, TopAbs_FACE)
            while exp.More():
                n_faces += 1
                exp.Next()
            log(f"  solid[{i}]: {n_faces} faces")
            try:
                info = analyse_solid(raw_solid)
            except Timeout:
                log(f"    ANALYSIS TIMED OUT")
                continue
            if info is not None:
                any_bad = True

        if any_bad:
            log(f"  >>> grid {grid_n} is the smallest failing case found so far <<<")
            break
        else:
            log(f"  grid {grid_n}: all raw solids BRepCheck-valid")
        log("-" * 88)


if __name__ == "__main__":
    main()
