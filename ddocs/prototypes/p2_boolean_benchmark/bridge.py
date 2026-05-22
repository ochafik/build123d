"""build123d <-> manifold3d bridge helpers + a hard wall-clock timeout.

Shared by every benchmark workload script. Nothing here mutates build123d
source; it only calls public APIs (`Shape.tessellate`) and the OCC sewing
primitives the way `build123d.mesher` does internally.

manifold3d here is 3.4.1 (not the 2.3.1 in research doc 03):
  * `Manifold.volume()` / `surface_area()` are methods, not properties.
  * `batch_boolean(manifolds, OpType.Add|Subtract|Intersect)` exists.
  * `Mesh(vert_properties, tri_verts, ..., tolerance=)` constructor.
"""

from __future__ import annotations

import multiprocessing as mp
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# build123d -> mesh -> manifold3d
# ---------------------------------------------------------------------------


def shape_to_manifold(shape, tolerance: float = 1e-2, weld_digits: int = 6):
    """Tessellate a build123d Shape and weld it into a manifold3d.Manifold.

    build123d's `tessellate()` emits a *non-indexed soup*: every face brings
    its own vertex block, so seam vertices are duplicated. manifold3d rejects
    that as `Error.NotManifold`. We grid-snap to `weld_digits` decimals (the
    same scheme `Mesher._create_3mf_mesh` uses) and dedup before handing it to
    manifold3d.

    Returns (Manifold, info dict).
    """
    import manifold3d as mf

    verts, tris = shape.tessellate(tolerance)
    if not verts or not tris:
        return mf.Manifold(), {"raw_verts": 0, "raw_tris": 0}

    vp = np.array([(v.X, v.Y, v.Z) for v in verts], dtype=np.float64)
    tv = np.array(tris, dtype=np.int64)

    snapped = np.round(vp, weld_digits)
    uniq, inv = np.unique(snapped, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    tv_w = inv[tv]
    good = (
        (tv_w[:, 0] != tv_w[:, 1])
        & (tv_w[:, 1] != tv_w[:, 2])
        & (tv_w[:, 0] != tv_w[:, 2])
    )
    tv_w = tv_w[good]

    mesh = mf.Mesh(uniq.astype(np.float32), tv_w.astype(np.uint32))
    man = mf.Manifold(mesh)
    info = {
        "raw_verts": len(vp),
        "raw_tris": len(tv),
        "welded_verts": len(uniq),
        "welded_tris": len(tv_w),
        "status": str(man.status()),
    }
    return man, info


# ---------------------------------------------------------------------------
# manifold3d -> mesh -> build123d Solid  (the expensive "bake" leg)
# ---------------------------------------------------------------------------


def manifold_to_solid(man):
    """Sew a manifold3d.Manifold back into an OCC Solid.

    Mirrors `build123d.mesher.Mesher._get_shape`: one planar TopoDS_Face per
    triangle, all fed to a single BRepBuilderAPI_Sewing. This is the only
    real mesh->Solid path build123d has, and it is the leg that may dominate
    the manifold timings -- which is exactly why the benchmark reports
    manifold timings both with and without it.

    Returns (Solid-or-Shell, info dict).
    """
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeSolid,
        BRepBuilderAPI_Sewing,
    )
    from OCP.BRepGProp import BRepGProp
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from build123d.topology import Shell, Solid
    from build123d.topology.shape_core import downcast

    om = man.to_mesh()
    vp = np.asarray(om.vert_properties)[:, :3].astype(np.float64)
    tv = np.asarray(om.tri_verts).astype(np.int64)
    if len(vp) == 0 or len(tv) == 0:
        return None, {"verts": 0, "tris": 0}

    gp_pnts = [gp_Pnt(float(p[0]), float(p[1]), float(p[2])) for p in vp]
    sew = BRepBuilderAPI_Sewing()
    props = GProp_GProps()
    for tri in tv:
        poly = BRepBuilderAPI_MakePolygon(
            gp_pnts[tri[0]], gp_pnts[tri[1]], gp_pnts[tri[2]], Close=True
        )
        facet = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
        BRepGProp.SurfaceProperties_s(facet, props)
        if props.Mass() != 0:
            sew.Add(facet)
    sew.Perform()
    sewed = downcast(sew.SewedShape())

    info = {"verts": len(vp), "tris": len(tv)}

    # Sewing may yield a single Shell, or a Compound of several Shells
    # (a body with internal voids / disjoint regions). Mirror Mesher._get_shape:
    # collect every shell, build a Solid on the manifold ones.
    topods_shells = []
    if sewed.ShapeType() == TopAbs_ShapeEnum.TopAbs_SHELL:
        topods_shells.append(TopoDS.Shell_s(sewed))
    else:
        exp = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
        while exp.More():
            topods_shells.append(TopoDS.Shell_s(exp.Current()))
            exp.Next()

    if not topods_shells:
        info["result"] = "no shell"
        return None, info

    shells = [Shell(s) for s in topods_shells]
    info["n_shells"] = len(shells)
    if all(sh.is_manifold for sh in shells):
        # outer shell = largest bbox; rest are voids
        shells.sort(key=lambda sh: sh.bounding_box().diagonal, reverse=True)
        try:
            mk = BRepBuilderAPI_MakeSolid()
            mk.Add(shells[0].wrapped)
            for inner in shells[1:]:
                mk.Add(inner.wrapped)
            solid = Solid(downcast(mk.Solid()))
            info["result"] = "solid"
            return solid, info
        except Exception as e:  # noqa: BLE001
            info["result"] = f"solid build failed: {e}"
            return shells[0], info
    info["result"] = "open shell"
    return shells[0], info


# ---------------------------------------------------------------------------
# subprocess timeout harness
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """One measured run of one method on one workload."""

    method: str
    n: int
    ok: bool = False
    timed_out: bool = False
    wall: float = float("nan")
    volume: float = float("nan")
    valid: bool | None = None
    tris: int | None = None
    error: str = ""
    extra: dict = field(default_factory=dict)


def _worker(fn, args, q):
    try:
        res = fn(*args)
        q.put(("ok", res))
    except Exception:  # noqa: BLE001 - benchmark harness must catch everything
        q.put(("err", traceback.format_exc()))


def run_with_timeout(fn, args, timeout_s: float):
    """Run `fn(*args)` in a child process; kill it if it exceeds `timeout_s`.

    Returns (status, payload):
      status == "ok"      -> payload is the function's return value
      status == "timeout" -> payload is None (child was killed)
      status == "err"     -> payload is a traceback string
      status == "crash"   -> payload is the exit code (segfault etc.)

    A hard kill is the only way to survive an OCC boolean that hangs and a
    manifold3d call that segfaults (doc 03 reports an intermittent exit 139).
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_worker, args=(fn, args, q))
    p.start()
    p.join(timeout_s)
    if p.is_alive():
        # SIGTERM first; OCC's tight C++ sew loop ignores it, so escalate to
        # SIGKILL if the child does not die within a short grace period.
        p.terminate()
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join(5)
        return "timeout", None
    if not q.empty():
        status, payload = q.get()
        return status, payload
    # process exited without putting anything -> crash (segfault)
    return "crash", p.exitcode


def timed(fn, *args):
    """Call fn(*args), return (wall_seconds, result)."""
    t0 = time.perf_counter()
    res = fn(*args)
    return time.perf_counter() - t0, res
