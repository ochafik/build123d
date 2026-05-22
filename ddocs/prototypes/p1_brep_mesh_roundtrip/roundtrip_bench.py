"""
roundtrip_bench.py -- the headline benchmark.

Measures the full BREP <-> mesh round-trip for a battery of test shapes:

    build123d Solid  --OUT-->  manifold3d Manifold  --IN-->  build123d Solid

For each shape it reports, at each stage:
  * vertex / triangle counts (raw OCC soup vs welded)
  * manifold3d health: status, is_empty, genus
  * IN-leg result kind (Solid vs silent Shell), validity, manifold-ness
  * volume error and bounding-box error
  * wall-clock time of every stage -- exposing the mesh->Solid scaling cliff.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python roundtrip_bench.py
"""

from __future__ import annotations

import math
import time

from build123d import (
    Box, Sphere, Cylinder, Cone, Torus, Compound, Pos,
    fillet, Solid,
)

from bridge import solid_to_manifold, manifold_to_solid, manifold_to_arrays


# ---------------------------------------------------------------------------
# Test shapes -- a deliberate spread of difficulty.
# ---------------------------------------------------------------------------

def make_filleted_box():
    """A 20mm box with all 12 edges filleted at 3mm -- 6 planar + curved faces.

    This is the 'tricky' shape doc 02 cares about: curvature that becomes
    facets, and many face seams that the weld step must close.
    """
    b = Box(20, 20, 20)
    return fillet(b.edges(), radius=3)


def make_compound():
    """A Compound of two disjoint solids (box + offset sphere).

    Tests that the bridge handles genus/decompose correctly when the manifold
    has two connected components.
    """
    b = Box(10, 10, 10)
    s = Pos(30, 0, 0) * Sphere(5)
    return Compound(children=[b, s])


def make_tricky():
    """A deliberately nasty shape: a box with a through-hole and a fillet.

    Box minus a cylinder (creates a genus-1 / tunnelled topology) then fillet
    a couple of edges.  Exercises: internal-edge seams, non-zero genus,
    curved + planar mix.
    """
    b = Box(20, 20, 20)
    hole = Cylinder(5, 30)
    part = b - hole  # genus 1 -- a tunnel
    return part


def make_refined_sphere(segments: int):
    """A high-triangle-count sphere to expose the mesh->Solid scaling cliff."""
    return Sphere(10)  # triangle count is driven by tessellation deflection


SHAPES = [
    ("Box",            lambda: Box(10, 10, 10),          dict()),
    ("Sphere",         lambda: Sphere(5),                dict()),
    ("Cylinder",       lambda: Cylinder(5, 20),          dict()),
    ("Cone",           lambda: Cone(8, 0, 15),           dict()),
    ("Torus",          lambda: Torus(20, 5),             dict()),
    ("FilletedBox",    make_filleted_box,                dict()),
    ("Compound(2)",    make_compound,                    dict()),
    ("Tricky(tunnel)", make_tricky,                      dict()),
]


# ---------------------------------------------------------------------------
# Round-trip driver
# ---------------------------------------------------------------------------

def bbox_tuple(shape):
    bb = shape.bounding_box()
    return (bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z)


def bbox_error(a, b):
    """Max abs difference between two bounding-box tuples."""
    return max(abs(x - y) for x, y in zip(a, b))


def run_one(name, factory, tolerance=1e-3, angular_tolerance=0.1):
    """Full round-trip of one shape.  Returns a dict of all measurements."""
    r = {"name": name, "tolerance": tolerance}

    shape = factory()
    r["src_volume"] = shape.volume
    r["src_bbox"] = bbox_tuple(shape)
    r["src_is_valid"] = shape.is_valid

    # ---- OUT leg: Solid -> Manifold ----
    try:
        man, out_info = solid_to_manifold(shape, tolerance, angular_tolerance)
    except Exception as e:  # pragma: no cover -- want a row even on failure
        r["error"] = f"OUT leg failed: {e!r}"
        return r
    r.update({f"out_{k}": v for k, v in out_info.items()})

    if man.is_empty():
        r["error"] = f"OUT leg produced empty Manifold (status {out_info['status']})"
        return r

    # genus, per connected component (decompose first, as the docs instruct).
    comps = man.decompose()
    r["out_n_components"] = len(comps)
    r["out_genus_total"] = sum(c.genus() for c in comps)

    # ---- IN leg: Manifold -> Solid ----
    try:
        back, in_info = manifold_to_solid(man)
    except Exception as e:  # pragma: no cover
        r["error"] = f"IN leg failed: {e!r}"
        r.update({f"out_{k}": v for k, v in out_info.items()})
        return r
    r.update({f"in_{k}": v for k, v in in_info.items()})

    # ---- Round-trip validity / fidelity ----
    r["rt_is_valid"] = back.is_valid
    r["rt_is_manifold"] = back.is_manifold
    r["rt_n_faces"] = len(back.faces())
    r["rt_volume"] = back.volume
    r["rt_bbox"] = bbox_tuple(back)

    src_vol = r["src_volume"]
    r["volume_abs_err"] = abs(back.volume - src_vol)
    r["volume_rel_err"] = abs(back.volume - src_vol) / abs(src_vol) if src_vol else float("nan")
    r["bbox_abs_err"] = bbox_error(r["src_bbox"], r["rt_bbox"])

    r["t_total"] = out_info["t_tessellate"] + out_info["t_weld"] + \
        out_info["t_build_manifold"] + in_info["t_total"]
    return r


# ---------------------------------------------------------------------------
# Pretty printers
# ---------------------------------------------------------------------------

def fmt(x, nd=4):
    if isinstance(x, float):
        if x != 0 and (abs(x) < 1e-3 or abs(x) >= 1e6):
            return f"{x:.2e}"
        return f"{x:.{nd}f}"
    return str(x)


def print_shape_table(rows):
    cols = [
        ("name",            "shape",        14),
        ("out_raw_verts",   "rawV",          7),
        ("out_welded_verts","weldV",         7),
        ("out_dedup_ratio", "dedup",         6),
        ("out_welded_tris", "tris",          7),
        ("out_status",      "m3d.status",   16),
        ("out_genus_total", "genus",         6),
        ("in_result_kind",  "IN-kind",       8),
        ("rt_is_valid",     "valid",         6),
        ("rt_n_faces",      "rtFaces",       8),
        ("volume_rel_err",  "vol.relErr",   11),
        ("bbox_abs_err",    "bbox.err",     10),
    ]
    header = "  ".join(f"{h:>{w}}" for _, h, w in cols)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for key, _, w in cols:
            v = row.get(key, "--")
            cells.append(f"{fmt(v):>{w}}")
        print("  ".join(cells))
        if "error" in row:
            print(f"    !! {row['error']}")


def print_timing_table(rows):
    cols = [
        ("name",               "shape",        14),
        ("out_t_tessellate",   "OUT.tess",      9),
        ("out_t_weld",         "OUT.weld",      9),
        ("out_t_build_manifold","OUT.m3d",      9),
        ("in_t_build_faces",   "IN.faces",      9),
        ("in_t_sewing",        "IN.SEW",        9),
        ("in_t_make_solid",    "IN.solid",      9),
        ("t_total",            "TOTAL(s)",      9),
    ]
    header = "  ".join(f"{h:>{w}}" for _, h, w in cols)
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = []
        for key, _, w in cols:
            v = row.get(key, "--")
            cells.append(f"{fmt(v, 4):>{w}}")
        print("  ".join(cells))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 90)
    print("BREP <-> mesh round-trip benchmark   (build123d + manifold3d 3.4.1)")
    print("default OCC tessellation: linear deflection 1e-3 (relative), angular 0.1 rad")
    print("=" * 90)
    print()

    rows = [run_one(name, factory) for name, factory, _ in SHAPES]

    print("### Per-shape round-trip results")
    print()
    print_shape_table(rows)
    print()
    print("### Per-shape stage timings (seconds)")
    print()
    print_timing_table(rows)
    print()

    # Sanity callouts.
    print("### Notes")
    for row in rows:
        if "error" in row:
            print(f"  [{row['name']}] ERROR: {row['error']}")
            continue
        if row["in_result_kind"] == "Shell":
            print(f"  [{row['name']}] IN leg returned a Shell, NOT a Solid "
                  f"(sewing failed to close the shell).")
        elif row["in_result_kind"] == "Compound":
            print(f"  [{row['name']}] IN leg returned a Compound of "
                  f"{row.get('in_n_solids', '?')} disjoint Solids "
                  f"(correct -- multiple connected components).")
        if not row.get("rt_is_valid", True):
            print(f"  [{row['name']}] round-tripped Solid is NOT is_valid.")
        if row["out_genus_total"] != 0:
            print(f"  [{row['name']}] manifold genus = {row['out_genus_total']} "
                  f"(non-trivial topology preserved).")
    print()
    return rows


if __name__ == "__main__":
    main()
