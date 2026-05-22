#!/usr/bin/env python
"""
run.py — end-to-end driver for the Build123dRenderer prototype.

Pipeline:
    .scad file
      -> scad2py.parser.parse_units            (OpenSCAD AST)
      -> scad2py.transpiler.transpile          (Python source)
      -> exec                                  (builds a csg.Node tree)
      -> Build123dRenderer                     (build123d Shape)
      -> export STEP + STL, validity check

Usage:
    python run.py <file.scad> [<file.scad> ...]
    python run.py --all          # run every example .scad in scad2py/examples

Requires the dedicated venv at /Users/ochafik/github/.ddocs-venv-scad2py with
both scad2py (front end only) and build123d installed, and scad2py on
PYTHONPATH. The `_bootstrap()` call below adds scad2py to sys.path so the
script also works when launched directly.
"""

from __future__ import annotations

import os
import sys
import traceback

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
SCAD2PY_ROOT = "/Users/ochafik/github/scad2py"
EXAMPLES_DIR = os.path.join(SCAD2PY_ROOT, "examples")
OUT_DIR = os.path.join(PROTO_DIR, "out")


def _bootstrap():
    """Put scad2py and this prototype dir on sys.path."""
    for p in (SCAD2PY_ROOT, PROTO_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)


_bootstrap()

import scad2py  # noqa: E402  (runtime: registers builtins, sets up csg.Context)
import scad2py.csg as csg  # noqa: E402
import scad2py.parser  # noqa: E402
import scad2py.transpiler  # noqa: E402

from build123d_renderer import Build123dRenderer, UnsupportedOperation  # noqa: E402


# --------------------------------------------------------------------------
# scad2py front end: .scad -> csg.Node tree
# --------------------------------------------------------------------------

def scad_to_csg_tree(scad_path: str) -> csg.Node:
    """Run scad2py's parser + transpiler + runtime; return the root csg.Node.

    This deliberately uses ONLY scad2py's front end + csg + runtime — it never
    touches scad2py.rendering (the manifold backend), which is the seam the
    research doc identifies for Option A.
    """
    scad_path = os.path.abspath(scad_path)
    units = scad2py.parser.parse_units(scad_path)
    transpiled = scad2py.transpiler.transpile(
        units[scad_path], units, output_main=True)
    code = str(transpiled)

    # The transpiled module does `from scad2py import *` then defines an
    # @module-decorated `main()`. Calling main() inside a fresh csg.Context
    # populates a csg.Node tree as a side effect (this is exactly what
    # scad2py.main.run does, minus the manifold rendering).
    namespace: dict = {}
    exec(compile(code, f"<transpiled:{scad_path}>", "exec"), namespace)
    main = namespace["main"]

    root = csg.Group(children=[])
    with csg.Context.scoped(root):
        main()
    return root


# --------------------------------------------------------------------------
# manifold reference (sanity check) — optional
# --------------------------------------------------------------------------

def manifold_reference(root: csg.Node):
    """Render the same tree with scad2py's own ManifoldRenderer for comparison.

    Returns (volume, bbox) or None if the manifold path is unavailable/fails.
    """
    try:
        import scad2py.rendering.rendering as rr
        geoms = rr.render_geom(root)

        def first_manifold(g):
            from manifold3d import Manifold
            from scad2py.geom_types import AttributedNode
            if isinstance(g, list):
                for x in g:
                    m = first_manifold(x)
                    if m is not None:
                        return m
            elif isinstance(g, csg.Group):
                for x in g.children:
                    m = first_manifold(x)
                    if m is not None:
                        return m
            elif isinstance(g, AttributedNode):
                return first_manifold(g.child)
            elif isinstance(g, Manifold):
                return g
            return None

        m = first_manifold(geoms)
        if m is None:
            return None
        mesh = m.to_mesh()
        verts = mesh.vert_properties[:, :3]
        bbox = (verts.min(axis=0).tolist(), verts.max(axis=0).tolist())
        return (m.volume(), bbox)
    except Exception as e:  # manifold backend may be broken in this venv
        return ("ERROR", str(e))


# --------------------------------------------------------------------------
# build123d render + export
# --------------------------------------------------------------------------

def process(scad_path: str) -> dict:
    name = os.path.splitext(os.path.basename(scad_path))[0]
    report: dict = {"name": name, "scad": scad_path}

    # 1. front end
    try:
        root = scad_to_csg_tree(scad_path)
        report["csg"] = root.to_str("")
    except Exception as e:
        report["error"] = f"front-end failure: {e}"
        report["traceback"] = traceback.format_exc()
        return report

    # 2. build123d render
    renderer = Build123dRenderer()
    try:
        shape = root.accept(renderer)
    except UnsupportedOperation as e:
        report["error"] = f"UNSUPPORTED: {e}"
        report["warnings"] = renderer.warnings
        return report
    except Exception as e:
        report["error"] = f"render failure: {type(e).__name__}: {e}"
        report["traceback"] = traceback.format_exc()
        report["warnings"] = renderer.warnings
        return report

    report["warnings"] = list(renderer.warnings)

    if shape is None:
        report["error"] = "renderer produced no geometry"
        return report

    # 3. metrology
    try:
        report["b3d_type"] = type(shape).__name__
        report["is_valid"] = bool(shape.is_valid)
        if hasattr(shape, "volume"):
            report["volume"] = round(float(shape.volume), 4)
        bb = shape.bounding_box()
        report["bbox"] = ([round(bb.min.X, 3), round(bb.min.Y, 3),
                           round(bb.min.Z, 3)],
                          [round(bb.max.X, 3), round(bb.max.Y, 3),
                           round(bb.max.Z, 3)])
    except Exception as e:
        report["warnings"].append(f"metrology failed: {e}")

    # 4. export STL + STEP
    os.makedirs(OUT_DIR, exist_ok=True)
    from build123d import export_step, export_stl, import_step

    step_path = os.path.join(OUT_DIR, f"{name}.step")
    stl_path = os.path.join(OUT_DIR, f"{name}.stl")
    try:
        # extrude 2D results to a thin slab so STL/STEP have a 3D body
        export_shape = shape
        from build123d import Sketch, Face, Compound, extrude
        if isinstance(shape, (Sketch, Face)) or (
                isinstance(shape, Compound) and shape._dim == 2):
            export_shape = extrude(Build123dRenderer._to_sketch(shape),
                                   amount=1.0)
            report["warnings"].append(
                "2D result extruded 1mm for STL/STEP export")

        ok_step = export_step(export_shape, step_path)
        report["step"] = step_path if ok_step else "FAILED"
        ok_stl = export_stl(export_shape, stl_path)
        report["stl"] = stl_path if ok_stl else "FAILED"
    except Exception as e:
        report["error"] = f"export failure: {e}"
        report["traceback"] = traceback.format_exc()
        return report

    # 5. verify the STEP re-opens and is valid
    try:
        reimported = import_step(step_path)
        report["step_reopens"] = bool(reimported.is_valid)
        if hasattr(reimported, "volume"):
            report["step_volume"] = round(float(reimported.volume), 4)
    except Exception as e:
        report["step_reopens"] = False
        report["warnings"].append(f"STEP reimport failed: {e}")

    # 6. manifold reference (sanity comparison)
    report["manifold_ref"] = manifold_reference(root)

    return report


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def print_report(r: dict):
    print("=" * 72)
    print(f"  {r['name']}   ({r['scad']})")
    print("=" * 72)
    if "csg" in r:
        print("  csg tree:")
        for line in r["csg"].splitlines():
            print("    " + line)
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        if "traceback" in r and os.environ.get("VERBOSE"):
            print(r["traceback"])
    else:
        print(f"  build123d type : {r.get('b3d_type')}")
        print(f"  is_valid       : {r.get('is_valid')}")
        if "volume" in r:
            print(f"  volume         : {r['volume']}")
        if "bbox" in r:
            print(f"  bbox           : {r['bbox']}")
        print(f"  STEP           : {r.get('step')}")
        print(f"  STL            : {r.get('stl')}")
        print(f"  STEP reopens   : {r.get('step_reopens')} "
              f"(vol={r.get('step_volume')})")
        mref = r.get("manifold_ref")
        if mref and isinstance(mref, tuple) and mref[0] != "ERROR":
            print(f"  manifold ref   : volume={mref[0]:.4f} bbox={mref[1]}")
        elif mref and isinstance(mref, tuple):
            print(f"  manifold ref   : unavailable ({mref[1][:60]})")
    for w in r.get("warnings", []):
        print(f"  WARNING: {w}")
    print()


def main(argv: list[str]):
    if not argv:
        print(__doc__)
        return 1
    if argv == ["--all"]:
        files = sorted(os.path.join(EXAMPLES_DIR, f)
                       for f in os.listdir(EXAMPLES_DIR)
                       if f.endswith(".scad"))
    else:
        files = []
        for a in argv:
            files.append(a if os.path.isabs(a) or os.path.exists(a)
                         else os.path.join(EXAMPLES_DIR, a))

    reports = []
    for f in files:
        try:
            r = process(f)
        except Exception as e:
            r = {"name": os.path.basename(f), "scad": f,
                 "error": f"unhandled: {e}",
                 "traceback": traceback.format_exc()}
        reports.append(r)
        print_report(r)

    # summary
    ok = [r for r in reports if "error" not in r]
    bad = [r for r in reports if "error" in r]
    print("#" * 72)
    print(f"# SUMMARY: {len(ok)}/{len(reports)} rendered to build123d")
    for r in ok:
        valid = "valid" if r.get("is_valid") else "INVALID"
        sr = "STEP-ok" if r.get("step_reopens") else "STEP-bad"
        print(f"#   OK   {r['name']:<16} {valid:<8} {sr}")
    for r in bad:
        print(f"#   FAIL {r['name']:<16} {r['error'][:60]}")
    print("#" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
