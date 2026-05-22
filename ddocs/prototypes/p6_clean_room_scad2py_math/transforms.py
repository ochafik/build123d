"""Clean-room reimplementation of OpenSCAD's rotate/mirror transform matrices.

Copyright (c) 2026 Olivier Chafik.
SPDX-License-Identifier: Apache-2.0

CLEAN-ROOM NOTICE
=================
This module is a fresh, independent implementation written WITHOUT consulting
OpenSCAD's GPL-licensed C++ source.  It is derived from:

  * the publicly documented behaviour of the OpenSCAD ``rotate()`` and
    ``mirror()`` transformations (OpenSCAD User Manual, "Transformations"),
    and
  * standard, textbook linear algebra.

The two pieces of behaviour reimplemented here are:

1.  ``rotate([x, y, z])`` -- an Euler-angle rotation.  Per the OpenSCAD
    manual, this rotates the child "x degrees about the X axis, y degrees
    about the Y axis and z degrees about the Z axis", applied in that order.
    Composing single-axis rotations gives  R = Rz . Ry . Rx .  The
    single-axis rotation matrices are the canonical ones found in any linear
    algebra or graphics text.

2.  ``mirror(v)`` -- reflection across the plane through the origin whose
    normal is ``v``.  This is the standard Householder reflection
    ``M = I - 2 * (v v^T) / (v . v)``.  When ``v`` is the zero vector the
    transform is the identity (a degenerate normal cannot define a plane).

Rotation matrices and the Householder reflection are mathematical facts, not
copyrightable expression.  This file is original expression of them.

The scalar ``rotate(angle, axis=v)`` (axis-angle) form is intentionally NOT
implemented here: in the code being replaced that path was dead (it raised
``NotImplementedError``), so there is nothing to preserve.  ``rotate_axis``
below provides a clean axis-angle implementation should it ever be wanted; it
uses Rodrigues' rotation formula (also a public mathematical fact).
"""

import math

import numpy as np


def _deg_sin_cos(angle_deg):
    """Sine and cosine of an angle given in degrees."""
    rad = math.radians(angle_deg)
    return math.sin(rad), math.cos(rad)


def _rot_x(angle_deg):
    """3x3 rotation about the X axis."""
    s, c = _deg_sin_cos(angle_deg)
    return np.array([
        [1.0, 0.0, 0.0],
        [0.0,   c,  -s],
        [0.0,   s,   c],
    ])


def _rot_y(angle_deg):
    """3x3 rotation about the Y axis."""
    s, c = _deg_sin_cos(angle_deg)
    return np.array([
        [  c, 0.0,   s],
        [0.0, 1.0, 0.0],
        [ -s, 0.0,   c],
    ])


def _rot_z(angle_deg):
    """3x3 rotation about the Z axis."""
    s, c = _deg_sin_cos(angle_deg)
    return np.array([
        [  c,  -s, 0.0],
        [  s,   c, 0.0],
        [0.0, 0.0, 1.0],
    ])


def _embed_3x3_as_4x4(r):
    """Place a 3x3 linear map into a 4x4 homogeneous matrix (no translation)."""
    m = np.identity(4)
    m[:3, :3] = r
    return m


def rotation_matrix(angles):
    """4x4 homogeneous matrix for OpenSCAD ``rotate([x, y, z])``.

    ``angles`` is a sequence of up to three angles in degrees, interpreted as
    rotations about the X, Y and Z axes respectively.  Missing entries are
    treated as 0.  The composition order is Rz . Ry . Rx, matching OpenSCAD
    (X applied first, then Y, then Z).
    """
    vals = list(angles) + [0.0, 0.0, 0.0]
    ax, ay, az = float(vals[0]), float(vals[1]), float(vals[2])
    r = _rot_z(az) @ _rot_y(ay) @ _rot_x(ax)
    return _embed_3x3_as_4x4(r)


def rotate_axis(angle_deg, axis):
    """4x4 homogeneous matrix for an axis-angle rotation (Rodrigues' formula).

    ``angle_deg`` is the rotation angle in degrees, ``axis`` is the rotation
    axis (need not be unit length).  A zero-length axis yields the identity.
    Provided for completeness; the original code's axis-angle path was dead.
    """
    v = np.asarray(axis, dtype=float)
    norm = math.sqrt(float(v @ v))
    if norm == 0.0:
        return np.identity(4)
    u = v / norm
    s, c = _deg_sin_cos(angle_deg)
    # Cross-product (skew-symmetric) matrix of the unit axis.
    k = np.array([
        [0.0,   -u[2],  u[1]],
        [u[2],   0.0,  -u[0]],
        [-u[1],  u[0],  0.0],
    ])
    r = np.identity(3) + s * k + (1.0 - c) * (k @ k)
    return _embed_3x3_as_4x4(r)


def mirror_matrix(normal):
    """4x4 homogeneous reflection matrix for OpenSCAD ``mirror(v)``.

    Reflects across the plane through the origin with the given ``normal``.
    A zero normal yields the identity (no usable plane).  ``normal`` may be
    2D, in which case its Z component is taken as 0.
    """
    n = list(normal)
    if len(n) == 2:
        n = [n[0], n[1], 0.0]
    v = np.asarray(n[:3], dtype=float)

    denom = float(v @ v)
    if denom == 0.0:
        return np.identity(4)

    # Householder reflection: M = I - 2 (v v^T) / (v . v).
    reflect3 = np.identity(3) - 2.0 * np.outer(v, v) / denom
    return _embed_3x3_as_4x4(reflect3)


def matrix_4x4_to_2x3(m):
    """Reduce a 3D homogeneous 4x4 transform to a 2D affine 2x3 transform.

    Keeps the upper-left 2x2 linear part and the X/Y translation column,
    dropping the Z row/column.  If ``m`` is already a 2x3 matrix it is
    returned unchanged.
    """
    arr = np.asarray(m)
    if arr.shape == (2, 3):
        return arr
    return np.array([
        [arr[0][0], arr[0][1], arr[0][3]],
        [arr[1][0], arr[1][1], arr[1][3]],
    ])
