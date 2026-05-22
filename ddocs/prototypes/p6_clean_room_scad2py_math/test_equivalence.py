"""Equivalence tests: clean-room math vs. the original (GPL) scad2py modules.

Copyright (c) 2026 Olivier Chafik.
SPDX-License-Identifier: Apache-2.0

This test proves the clean-room reimplementations in this directory
(``fragments.py``, ``transforms.py``, ``colors.py``) are numerically and
behaviourally equivalent to the contaminated originals
(``scad2py/calc.py``, ``scad2py/colors.py``).

It IMPORTS and RUNS the original GPL-derived code to generate expected
values.  That is fine: running GPL software to produce test fixtures does not
make this test a derivative work -- only *copying* GPL source would.  The
clean-room modules themselves were written without consulting OpenSCAD's C++.

Run with:
    PYTHONPATH=/Users/ochafik/github/scad2py \\
      /Users/ochafik/github/.ddocs-venv/bin/python \\
      -m pytest ddocs/prototypes/p6_clean_room_scad2py_math/test_equivalence.py -q

or directly:
    PYTHONPATH=/Users/ochafik/github/scad2py \\
      /Users/ochafik/github/.ddocs-venv/bin/python \\
      ddocs/prototypes/p6_clean_room_scad2py_math/test_equivalence.py
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import colors as new_colors          # clean-room
import fragments as new_fragments    # clean-room
import transforms as new_transforms  # clean-room

# Originals (GPL-derived) -- imported only to generate expected outputs.
import scad2py.calc as orig_calc
import scad2py.colors as orig_colors


TOL = 1e-12


def _check(label, got, expected, tol=TOL):
    got = np.asarray(got, dtype=float)
    expected = np.asarray(expected, dtype=float)
    assert got.shape == expected.shape, f"{label}: shape {got.shape} != {expected.shape}"
    diff = float(np.max(np.abs(got - expected))) if got.size else 0.0
    assert diff <= tol, f"{label}: max abs diff {diff} > {tol}\n got={got}\n exp={expected}"
    return diff


# ---------------------------------------------------------------------------
# fragment_count vs. get_fragments_from_r
# ---------------------------------------------------------------------------
def test_fragment_count():
    cases = []
    for r in [0.1, 0.5, 1, 2, 5, 10, 50, 100, 1000]:
        for fn in [None, 0, 1, 2, 3, 4, 8, 16, 100, float("inf"), float("nan")]:
            for fs in [0.1, 0.5, 1, 2, 5]:
                for fa in [1, 5, 6, 12, 30, 45]:
                    cases.append((r, fn, fs, fa))

    max_diff = 0
    for r, fn, fs, fa in cases:
        expected = orig_calc.get_fragments_from_r(r, fn, fs, fa)
        got = new_fragments.fragment_count(r, fn=fn, fs=fs, fa=fa)
        assert got == expected, (
            f"fragment_count(r={r}, fn={fn}, fs={fs}, fa={fa}): "
            f"got {got}, expected {expected}"
        )
        assert isinstance(got, int)
        max_diff = max(max_diff, abs(got - expected))
    print(f"  fragment_count: {len(cases)} cases, all exact")


# ---------------------------------------------------------------------------
# rotation_matrix (Euler XYZ) vs. calc.rotation_matrix
# ---------------------------------------------------------------------------
def test_rotation_matrix():
    angle_sets = [
        [0, 0, 0], [30, 0, 0], [0, 45, 0], [0, 0, 90],
        [10, 20, 30], [90, 90, 90], [-45, 15, 200], [360, 180, 720],
        [12.5, -7.25, 33.3], [1, 2, 3],
        [30], [30, 45],  # short forms -> missing axes are 0
    ]
    worst = 0.0
    for angles in angle_sets:
        expected = orig_calc.rotation_matrix(a=list(angles))
        got = new_transforms.rotation_matrix(angles)
        worst = max(worst, _check(f"rotation_matrix({angles})", got, expected))
    print(f"  rotation_matrix: {len(angle_sets)} cases, max abs diff {worst:.2e}")


# ---------------------------------------------------------------------------
# mirror_matrix vs. calc.mirror_matrix
# ---------------------------------------------------------------------------
def test_mirror_matrix():
    vectors = [
        [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 1, 1],
        [1, 2, 3], [-1, 2, -3], [0.5, 0.25, 0.75], [3, 4, 0],
        [1, 0],          # 2D normal -> z = 0
        [0, 0, 0],       # degenerate -> identity (2x3 in the original)
    ]
    worst = 0.0
    for v in vectors:
        expected = orig_calc.mirror_matrix(v)
        got = new_transforms.mirror_matrix(v)
        # The original returns a 2x3 slice for the zero vector; the clean-room
        # version returns a full 4x4 identity. Compare on common semantics:
        # the linear action on points. For the zero vector both must be an
        # identity transform.
        if list(v[:3]) + ([0] * (3 - len(v))) == [0, 0, 0] or all(c == 0 for c in v):
            assert np.allclose(got, np.identity(4)), f"mirror({v}) should be identity"
            assert np.allclose(expected, np.identity(4)[:2]), "orig zero-vec sanity"
            continue
        worst = max(worst, _check(f"mirror_matrix({v})", got, expected))
    print(f"  mirror_matrix: {len(vectors)} cases, max abs diff {worst:.2e}")


# ---------------------------------------------------------------------------
# matrix_4x4_to_2x3 vs. calc.matrix3d_to_2d
# ---------------------------------------------------------------------------
def test_matrix_4x4_to_2x3():
    mats = [
        orig_calc.rotation_matrix(a=[0, 0, 30]),
        orig_calc.rotation_matrix(a=[0, 0, -90]),
        orig_calc.mirror_matrix([1, 0, 0]),
        np.array([
            [1.0, 2.0, 3.0, 4.0],
            [5.0, 6.0, 7.0, 8.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]),
    ]
    worst = 0.0
    for m in mats:
        expected = orig_calc.matrix3d_to_2d(m)
        got = new_transforms.matrix_4x4_to_2x3(m)
        worst = max(worst, _check("matrix_4x4_to_2x3", got, expected))
    # 2x3 passthrough
    already = np.array([[1.0, 0.0, 5.0], [0.0, 1.0, 6.0]])
    assert np.array_equal(new_transforms.matrix_4x4_to_2x3(already), already)
    print(f"  matrix_4x4_to_2x3: {len(mats)} cases + passthrough, max abs diff {worst:.2e}")


# ---------------------------------------------------------------------------
# parse_color vs. colors.parse_color
# ---------------------------------------------------------------------------
def test_parse_color():
    string_cases = ["#ff0000", "#00FF00", "#aabbcc", "#11223344", "#FFFFFFFF"]
    name_cases = sorted(orig_colors.webcolors.keys()) + ["Red", "STEELBLUE", "Black"]
    vec_cases = [
        [0.1, 0.2, 0.3], [0.1, 0.2, 0.3, 0.4], (1, 1, 1),
        np.array([0.5, 0.5, 0.5]), [0, 0, 0, 0],
    ]
    worst = 0.0
    for c in string_cases:
        worst = max(worst, _check(f"parse_color({c!r})",
                                  new_colors.parse_color(c),
                                  orig_colors.parse_color(c)))
    for c in name_cases:
        worst = max(worst, _check(f"parse_color({c!r})",
                                  new_colors.parse_color(c),
                                  orig_colors.parse_color(c)))
    for c in vec_cases:
        worst = max(worst, _check(f"parse_color({c!r})",
                                  new_colors.parse_color(c),
                                  orig_colors.parse_color(c)))
    print(f"  parse_color: {len(string_cases)+len(name_cases)+len(vec_cases)} "
          f"cases, max abs diff {worst:.2e}")


# ---------------------------------------------------------------------------
# Named-colour table and scheme equality
# ---------------------------------------------------------------------------
def test_color_tables_match():
    orig = orig_colors.webcolors
    new = new_colors.NAMED_COLORS
    assert set(orig.keys()) == set(new.keys()), (
        f"name mismatch: only-orig={set(orig)-set(new)}, "
        f"only-new={set(new)-set(orig)}"
    )
    for name in orig:
        assert tuple(orig[name]) == tuple(new[name]), (
            f"{name}: orig {orig[name]} != new {new[name]}"
        )

    for key, val in orig_colors.color_scheme.items():
        assert tuple(val) == tuple(new_colors.COLOR_SCHEME[key]), (
            f"color_scheme[{key}]: {val} != {new_colors.COLOR_SCHEME[key]}"
        )
    assert np.array_equal(orig_colors.default_color, new_colors.DEFAULT_COLOR)
    assert orig_colors.background_node_color == new_colors.BACKGROUND_NODE_COLOR
    assert orig_colors.debug_node_color == new_colors.DEBUG_NODE_COLOR
    print(f"  color tables: {len(new)} named colors + scheme, all exact")


def _run_all():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print()
    if failures:
        print(f"{failures} test(s) FAILED")
        sys.exit(1)
    print("All clean-room equivalence tests PASSED.")


if __name__ == "__main__":
    _run_all()
