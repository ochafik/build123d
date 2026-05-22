"""
tessellation_fidelity.py -- how OCC deflection knobs trade mesh size for accuracy.

The OUT leg's only fidelity controls are the two `BRepMesh_IncrementalMesh`
parameters surfaced by `Shape.tessellate(tolerance, angular_tolerance)`:

  * linear deflection ("tolerance") -- max chord distance between the true
    surface and a facet.  RELATIVE by default (doc 02 sec 1.1): scaled by edge
    length, so 1e-3 means "0.1% of edge length", not "1e-3 mm".
  * angular deflection -- max angle between adjacent facet normals on curved
    surfaces.  Default 0.1 rad ~= 5.7 deg.

EMPIRICAL FINDING (probed first, see RESULTS.md): on these uniform-curvature
solids the *angular* tolerance is the dominant knob.  Loosening linear
deflection from 1e-2 to 1e-1 changes the sphere/cylinder/torus triangle count
NOT AT ALL -- angular tolerance caps the facet size.  Linear deflection only
starts adding triangles once it is tight enough (~1e-4) to beat the angular
budget.  This script therefore sweeps BOTH and reports triangle count vs the
resulting volume error of the welded manifold.

Run:  /Users/ochafik/github/.ddocs-venv/bin/python tessellation_fidelity.py
"""

from __future__ import annotations

import sys

from build123d import Sphere, Cylinder, Torus
from bridge import solid_to_manifold


# (label, linear deflection, angular deflection) -- coarse -> fine.
# Linear deflection is capped at 1e-4: at 1e-5 the Torus explodes into millions
# of triangles and the mesh build itself becomes the bottleneck.
SETTINGS = [
    ("very coarse",  1e-1, 0.8),
    ("coarse",       3e-2, 0.4),
    ("medium",       1e-2, 0.2),
    ("default",      1e-3, 0.1),
    ("fine",         1e-4, 0.05),
    ("very fine",    1e-4, 0.02),
]

SHAPES = [
    ("Sphere(r=10)",      lambda: Sphere(10)),
    ("Cylinder(r5,h20)",  lambda: Cylinder(5, 20)),
    ("Torus(R20,r5)",     lambda: Torus(20, 5)),
]


def _fmt(x, nd=4):
    if isinstance(x, float):
        if x != 0 and (abs(x) < 1e-3 or abs(x) >= 1e6):
            return f"{x:.2e}"
        return f"{x:.{nd}f}"
    return str(x)


def main():
    print("=" * 92, flush=True)
    print("TESSELLATION FIDELITY  --  OCC deflection knobs vs mesh size vs volume error")
    print("=" * 92, flush=True)

    for shape_name, factory in SHAPES:
        ref_volume = factory().volume  # exact BREP volume
        print()
        print(f"### {shape_name}   exact BREP volume = {ref_volume:.6f}")
        print()
        cols = [
            ("label",   "setting",     12),
            ("lin",     "lin.defl",    10),
            ("ang",     "ang.defl",    10),
            ("verts",   "verts",        8),
            ("tris",    "tris",         9),
            ("mvol",    "mesh.volume", 13),
            ("abserr",  "abs.err",     11),
            ("relerr",  "rel.err",     11),
        ]
        header = "  ".join(f"{h:>{w}}" for _, h, w in cols)
        print(header)
        print("-" * len(header))

        for label, lin, ang in SETTINGS:
            shape = factory()
            man, info = solid_to_manifold(shape, tolerance=lin,
                                          angular_tolerance=ang)
            if man.is_empty():
                print(f"  {label:>12}  -> EMPTY manifold ({info['status']})",
                      flush=True)
                continue
            mvol = info["volume"]
            abserr = abs(mvol - ref_volume)
            relerr = abserr / ref_volume
            row = dict(label=label, lin=lin, ang=ang,
                       verts=info["welded_verts"], tris=info["welded_tris"],
                       mvol=mvol, abserr=abserr, relerr=relerr)
            print("  ".join(f"{_fmt(row[k]):>{w}}" for k, _, w in cols),
                  flush=True)

    print()
    print("### Reading the numbers")
    print("  * Faceting a curved solid ALWAYS under-estimates volume (inscribed")
    print("    polyhedron) -- mesh.volume < exact volume at every setting.")
    print("  * Angular tolerance is the dominant knob here: loosening LINEAR")
    print("    deflection alone barely changes the triangle count.")
    print("  * Volume error falls roughly quadratically as the facets shrink,")
    print("    but triangle count rises just as fast -- and the IN-leg sewing")
    print("    cost is super-linear in triangle count (see scaling_cliff.py).")
    print("  * 'default' (1e-3 / 0.1 rad) already gives <0.2% volume error;")
    print("    going finer mostly buys triangles, not accuracy.")
    print(flush=True)


if __name__ == "__main__":
    main()
