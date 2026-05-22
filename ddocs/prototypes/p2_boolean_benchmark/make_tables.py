"""Render results.json into the markdown tables used in RESULTS.md.

Usage:
    python make_tables.py [results.json]
Prints markdown to stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _fmt(x, nd=4):
    if x is None:
        return "-"
    if isinstance(x, float):
        if x != x:  # nan
            return "-"
        return f"{x:.{nd}f}"
    return str(x)


def _cell_status(cell):
    s = cell["status"]
    if s == "ok":
        return None
    if s == "timeout":
        return "TIMEOUT"
    if s == "crash":
        return f"CRASH({cell.get('exitcode')})"
    return "ERROR"


def pair_rows(cells, occ_label, man_label):
    """Group OCC/manifold cells by arg into rows."""
    by_arg = {}
    for c in cells:
        by_arg.setdefault(c["arg"], {})[c["label"]] = c
    rows = []
    for arg in sorted(by_arg, key=lambda a: (isinstance(a, str), a)):
        rows.append((arg, by_arg[arg].get(occ_label), by_arg[arg].get(man_label)))
    return rows


def speedup(occ, man):
    if occ is None or man is None:
        return "-"
    if occ["status"] != "ok" or man["status"] != "ok":
        return "-"
    o = occ["data"].get("wall")
    m = man["data"].get("wall")
    if not o or not m:
        return "-"
    return f"{o / m:.1f}x"


def speedup_bake(occ, man):
    if occ is None or man is None or occ["status"] != "ok" or man["status"] != "ok":
        return "-"
    o = occ["data"].get("wall")
    m = man["data"].get("wall_with_bake")
    if not o or not m:
        return "-"  # bake skipped (large mesh) -> no comparable number
    return f"{o / m:.1f}x"


def table_scaling(cells, occ_label, man_label, n_header):
    rows = pair_rows(cells, occ_label, man_label)
    out = []
    out.append(
        f"| {n_header} | OCC wall (s) | OCC valid | manifold bool (s) | "
        "manifold +bake (s) | manifold tris | speedup (bool) | speedup (+bake) |"
    )
    out.append("|--:|--:|:--:|--:|--:|--:|--:|--:|")
    for arg, occ, man in rows:
        if occ and occ["status"] == "ok":
            od = occ["data"]
            occ_w = _fmt(od.get("wall"))
            occ_v = "yes" if od.get("valid") else "NO"
        else:
            occ_w = _cell_status(occ) or "-"
            occ_v = "-"
        if man and man["status"] == "ok":
            md = man["data"]
            man_b = _fmt(md.get("wall"))
            man_bb = _fmt(md.get("wall_with_bake"))
            man_t = _fmt(md.get("tris"), 0)
        else:
            man_b = _cell_status(man) or "-"
            man_bb = "-"
            man_t = "-"
        out.append(
            f"| {arg} | {occ_w} | {occ_v} | {man_b} | {man_bb} | {man_t} | "
            f"{speedup(occ, man)} | {speedup_bake(occ, man)} |"
        )
    return "\n".join(out)


def table_degenerate(cells):
    rows = pair_rows(cells, "occ_degenerate", "manifold_degenerate")
    out = []
    out.append(
        "| case | expected vol | OCC vol | OCC valid | OCC time (s) | "
        "manifold vol | manifold empty | manifold time (s) | verdict |"
    )
    out.append("|:--|--:|--:|:--:|--:|--:|:--:|--:|:--|")
    for arg, occ, man in rows:
        exp = occ.get("expected_volume") if occ else None
        if occ and occ["status"] == "ok":
            od = occ["data"]
            occ_v = _fmt(od.get("volume"), 3)
            occ_valid = "yes" if od.get("valid") else "NO"
            occ_t = _fmt(od.get("wall"))
        else:
            occ_v = _cell_status(occ) or "-"
            occ_valid = "-"
            occ_t = "-"
        if man and man["status"] == "ok":
            md = man["data"]
            man_v = _fmt(md.get("volume"), 3)
            man_e = "yes" if md.get("empty") else "no"
            man_t = _fmt(md.get("wall"))
        else:
            man_v = _cell_status(man) or "-"
            man_e = "-"
            man_t = "-"

        verdict = "both ok"
        if exp is not None:
            occ_ok = (
                occ
                and occ["status"] == "ok"
                and abs(occ["data"].get("volume", 1e9) - exp) < 1e-3
            )
            man_ok = (
                man
                and man["status"] == "ok"
                and abs(man["data"].get("volume", 1e9) - exp) < 1e-3
            )
            if occ_ok and man_ok:
                verdict = "both correct"
            elif man_ok and not occ_ok:
                verdict = "OCC WRONG, manifold ok"
            elif occ_ok and not man_ok:
                verdict = "manifold WRONG, OCC ok"
            else:
                verdict = "both wrong"
        out.append(
            f"| {arg} | {_fmt(exp, 1)} | {occ_v} | {occ_valid} | {occ_t} | "
            f"{man_v} | {man_e} | {man_t} | {verdict} |"
        )
    return "\n".join(out)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results.json")
    data = json.loads(path.read_text())
    wl = data["workloads"]

    print("### Workload 1 - Union of N overlapping boxes (grid)\n")
    print(table_scaling(wl["union_grid"], "occ_union_grid", "manifold_union_grid", "N"))
    print()
    print("### Workload 2 - Deep CSG tree (alternating diff/union)\n")
    print(table_scaling(wl["csg_tree"], "occ_csg_tree", "manifold_csg_tree", "depth"))
    print()
    print("### Workload 3 - Coincident / degenerate cases\n")
    print(table_degenerate(wl["degenerate"]))
    print()
    print("### Workload 4 - One body minus N drilled holes\n")
    print(table_scaling(wl["drill"], "occ_drill", "manifold_drill", "N holes"))


if __name__ == "__main__":
    main()
