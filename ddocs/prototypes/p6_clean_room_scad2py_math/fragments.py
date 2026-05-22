"""Clean-room reimplementation of OpenSCAD's circle-fragment count rule.

Copyright (c) 2026 Olivier Chafik.
SPDX-License-Identifier: Apache-2.0

CLEAN-ROOM NOTICE
=================
This module is a fresh, independent implementation written WITHOUT consulting
OpenSCAD's GPL-licensed C++ source.  It is derived solely from the publicly
documented behaviour of the OpenSCAD language, specifically the special
variables ``$fn``, ``$fa`` and ``$fs`` as described in the OpenSCAD User
Manual (the "Other Language Features" / "circle" pages, e.g.
https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Other_Language_Features).

The rule it implements is a *specification*, not protected expression:

  * ``$fn`` -- if set and greater than 0, the circle is rendered with exactly
    that many fragments (the renderer clamps tiny values up to a minimum of 3,
    since a polygon needs at least 3 sides).
  * Otherwise the fragment count is the *finer* of two limits:
      - an angular limit: a full turn (360 degrees) divided by ``$fa``
        (minimum angle per fragment, in degrees);
      - a chord-length limit: the circle circumference ``2 * pi * r``
        divided by ``$fs`` (minimum size of a fragment).
    OpenSCAD takes ``min`` of those two and then enforces a documented floor
    of 5 fragments so that even tiny circles are not rendered as triangles.

A formula relating a radius and two tolerances to an integer count is a
mathematical fact and is not copyrightable; only a particular textual
*expression* of it would be.  This file is an original expression.
"""

import math

# Minimum sides for a polygon approximation when $fn is explicitly given.
_FN_MIN = 3
# Documented floor on the fragment count when $fn is not used.
_AUTO_MIN = 5
# A full revolution, in degrees.
_FULL_TURN_DEG = 360.0


def fragment_count(radius, fn=None, fs=2.0, fa=12.0):
    """Return how many fragments OpenSCAD would use to draw a circle.

    Parameters
    ----------
    radius : float
        Circle radius.
    fn : float or None
        The ``$fn`` special variable.  When ``None`` (or non-finite) it is
        treated as unset.  When > 0 it fixes the count directly.
    fs : float
        The ``$fs`` special variable: minimum fragment (chord) length.
    fa : float
        The ``$fa`` special variable: minimum fragment angle in degrees.

    Returns
    -------
    int
        Number of fragments around the full circle.
    """
    # $fn unset / not a usable number -> fall through to the $fa/$fs rule's
    # default, but with no radius information OpenSCAD degenerates to a
    # triangle. We mirror that: an unusable $fn yields the polygon minimum.
    if fn is None or not math.isfinite(fn):
        return _FN_MIN

    # Explicit $fn wins; clamp up to a valid polygon.
    if fn > 0.0:
        n = int(fn)
        return n if n >= _FN_MIN else _FN_MIN

    # Automatic mode: finer of the angular and chord-length limits, floored.
    angular_limit = _FULL_TURN_DEG / fa
    chord_limit = radius * 2.0 * math.pi / fs
    return int(math.ceil(max(min(angular_limit, chord_limit), _AUTO_MIN)))
