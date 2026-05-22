# p6 — Clean-room rewrite of scad2py's GPL-contaminated math

**Purpose.** Replace the two GPL-2.0+ contaminated files in scad2py
(`scad2py/calc.py`, `scad2py/colors.py`) with fresh, Apache-2.0-licensable
implementations written from public specifications only — see
`ddocs/design/scad2py-gpl-remediation.md` for the full audit and plan.

This deliverable was produced by a parallel CadQuery research effort. Because
the contamination is in scad2py's *own* files, the clean-room rewrite is
library-agnostic and is reproduced here for build123d unchanged.

## Files

| File | Replaces | Clean-room source used |
|---|---|---|
| `fragments.py` | `calc.get_fragments_from_r` | OpenSCAD User Manual: `$fn/$fa/$fs` semantics |
| `transforms.py` | `calc.rotation_matrix`, `calc.mirror_matrix`, `calc.matrix3d_to_2d` | OpenSCAD User Manual (rotate/mirror) + textbook linear algebra |
| `colors.py` | `colors.parse_color`, `colors.webcolors`, `colors.color_scheme`, constants | OpenSCAD `color()` docs + W3C CSS Color Module Level 4 named-color table |
| `test_equivalence.py` | — | Proves numerical/behavioral equivalence to the originals |

## Clean-room method

1. The originals were **never opened in a text editor while writing** the new
   code. The only thing read from them was their *observable behaviour*,
   obtained by **running** them (running GPL code to generate test fixtures is
   not copying — only reproducing source text would be).
2. Each new file is implemented from a public, non-GPL **specification**: the
   OpenSCAD User Manual (language docs), the W3C CSS Color 4 spec, and
   standard mathematics (Euler-angle rotation matrices, the Householder
   reflection, Rodrigues' formula).
3. New file headers carry `SPDX-License-Identifier: Apache-2.0` and a
   CLEAN-ROOM NOTICE documenting the method. **No GPL headers, no "ported
   from" comments, no OpenSCAD source URLs.**

## Result

`test_equivalence.py` runs 3308 comparison cases against the originals
(re-verified in the build123d effort, 2026-05-22):

```
fragment_count:   2970 cases, all exact
rotation_matrix:    12 cases, max abs diff 1.39e-17 (FP noise)
mirror_matrix:      11 cases, max abs diff 0.00e+00
matrix_4x4_to_2x3:   4 cases + passthrough, exact
parse_color:       162 cases, max abs diff 0.00e+00
color tables:      149 named colors + 12-entry scheme, all exact
```

All pass. The reimplementations are drop-in equivalent.

## Running the tests

```sh
PYTHONPATH=/Users/ochafik/github/scad2py \
  /Users/ochafik/github/.ddocs-venv/bin/python \
  ddocs/prototypes/p6_clean_room_scad2py_math/test_equivalence.py
```

## Note on the axis-angle path

`calc.angle_axis_degrees` in the original **raises `Exception("Not
implemented")`** — the scalar `rotate(angle, v=axis)` path is dead code.
`transforms.py` therefore does not need to preserve it, but provides a clean
`rotate_axis()` (Rodrigues' formula) in case it is wanted later.
