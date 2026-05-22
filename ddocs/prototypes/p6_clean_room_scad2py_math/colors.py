"""Clean-room reimplementation of OpenSCAD's color() argument parsing.

Copyright (c) 2026 Olivier Chafik.
SPDX-License-Identifier: Apache-2.0

CLEAN-ROOM NOTICE
=================
This module is a fresh, independent implementation written WITHOUT consulting
OpenSCAD's GPL-licensed C++ source.  It is derived from:

  * the publicly documented behaviour of the OpenSCAD ``color()`` module
    (OpenSCAD User Manual, "Transformations / color"), which accepts either
    an ``[r, g, b]`` / ``[r, g, b, a]`` vector of 0..1 floats, a ``"#rrggbb"``
    or ``"#rrggbbaa"`` hex string, or a CSS colour name; and
  * the W3C "CSS Color Module Level 4" specification's named-colour table
    (https://www.w3.org/TR/css-color-4/#named-colors).  The named-colour
    sRGB values are a published W3C standard -- factual data, not OpenSCAD
    expression.  OpenSCAD itself takes the same table from the same W3C
    source; reproducing the W3C table independently is therefore clean.

The numeric facts (a hex pair is a byte; a byte maps to a 0..1 float by
dividing by 255; the CSS named-colour values) are not copyrightable.  This
file is an original expression of the parsing logic.

The one OpenSCAD-specific addition is the ``"transparent"`` keyword (RGBA
0,0,0,0); it is a trivial convention, named in OpenSCAD's public docs.
"""

import numpy as np

# ---------------------------------------------------------------------------
# Named colours.
#
# The 148 CSS Color Module Level 4 "named colors" plus OpenSCAD's own
# "transparent".  Values are sRGB 8-bit (0..255); alpha is 255 (opaque) for
# every CSS colour and 0 for "transparent".  Sourced from the W3C CSS Color 4
# specification, which is the same public source OpenSCAD uses.
# ---------------------------------------------------------------------------
_CSS_NAMED_RGB = {
    "aliceblue": (240, 248, 255), "antiquewhite": (250, 235, 215),
    "aqua": (0, 255, 255), "aquamarine": (127, 255, 212),
    "azure": (240, 255, 255), "beige": (245, 245, 220),
    "bisque": (255, 228, 196), "black": (0, 0, 0),
    "blanchedalmond": (255, 235, 205), "blue": (0, 0, 255),
    "blueviolet": (138, 43, 226), "brown": (165, 42, 42),
    "burlywood": (222, 184, 135), "cadetblue": (95, 158, 160),
    "chartreuse": (127, 255, 0), "chocolate": (210, 105, 30),
    "coral": (255, 127, 80), "cornflowerblue": (100, 149, 237),
    "cornsilk": (255, 248, 220), "crimson": (220, 20, 60),
    "cyan": (0, 255, 255), "darkblue": (0, 0, 139),
    "darkcyan": (0, 139, 139), "darkgoldenrod": (184, 134, 11),
    "darkgray": (169, 169, 169), "darkgreen": (0, 100, 0),
    "darkgrey": (169, 169, 169), "darkkhaki": (189, 183, 107),
    "darkmagenta": (139, 0, 139), "darkolivegreen": (85, 107, 47),
    "darkorange": (255, 140, 0), "darkorchid": (153, 50, 204),
    "darkred": (139, 0, 0), "darksalmon": (233, 150, 122),
    "darkseagreen": (143, 188, 143), "darkslateblue": (72, 61, 139),
    "darkslategray": (47, 79, 79), "darkslategrey": (47, 79, 79),
    "darkturquoise": (0, 206, 209), "darkviolet": (148, 0, 211),
    "deeppink": (255, 20, 147), "deepskyblue": (0, 191, 255),
    "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105),
    "dodgerblue": (30, 144, 255), "firebrick": (178, 34, 34),
    "floralwhite": (255, 250, 240), "forestgreen": (34, 139, 34),
    "fuchsia": (255, 0, 255), "gainsboro": (220, 220, 220),
    "ghostwhite": (248, 248, 255), "gold": (255, 215, 0),
    "goldenrod": (218, 165, 32), "gray": (128, 128, 128),
    "green": (0, 128, 0), "greenyellow": (173, 255, 47),
    "grey": (128, 128, 128), "honeydew": (240, 255, 240),
    "hotpink": (255, 105, 180), "indianred": (205, 92, 92),
    "indigo": (75, 0, 130), "ivory": (255, 255, 240),
    "khaki": (240, 230, 140), "lavender": (230, 230, 250),
    "lavenderblush": (255, 240, 245), "lawngreen": (124, 252, 0),
    "lemonchiffon": (255, 250, 205), "lightblue": (173, 216, 230),
    "lightcoral": (240, 128, 128), "lightcyan": (224, 255, 255),
    "lightgoldenrodyellow": (250, 250, 210), "lightgray": (211, 211, 211),
    "lightgreen": (144, 238, 144), "lightgrey": (211, 211, 211),
    "lightpink": (255, 182, 193), "lightsalmon": (255, 160, 122),
    "lightseagreen": (32, 178, 170), "lightskyblue": (135, 206, 250),
    "lightslategray": (119, 136, 153), "lightslategrey": (119, 136, 153),
    "lightsteelblue": (176, 196, 222), "lightyellow": (255, 255, 224),
    "lime": (0, 255, 0), "limegreen": (50, 205, 50),
    "linen": (250, 240, 230), "magenta": (255, 0, 255),
    "maroon": (128, 0, 0), "mediumaquamarine": (102, 205, 170),
    "mediumblue": (0, 0, 205), "mediumorchid": (186, 85, 211),
    "mediumpurple": (147, 112, 219), "mediumseagreen": (60, 179, 113),
    "mediumslateblue": (123, 104, 238), "mediumspringgreen": (0, 250, 154),
    "mediumturquoise": (72, 209, 204), "mediumvioletred": (199, 21, 133),
    "midnightblue": (25, 25, 112), "mintcream": (245, 255, 250),
    "mistyrose": (255, 228, 225), "moccasin": (255, 228, 181),
    "navajowhite": (255, 222, 173), "navy": (0, 0, 128),
    "oldlace": (253, 245, 230), "olive": (128, 128, 0),
    "olivedrab": (107, 142, 35), "orange": (255, 165, 0),
    "orangered": (255, 69, 0), "orchid": (218, 112, 214),
    "palegoldenrod": (238, 232, 170), "palegreen": (152, 251, 152),
    "paleturquoise": (175, 238, 238), "palevioletred": (219, 112, 147),
    "papayawhip": (255, 239, 213), "peachpuff": (255, 218, 185),
    "peru": (205, 133, 63), "pink": (255, 192, 203),
    "plum": (221, 160, 221), "powderblue": (176, 224, 230),
    "purple": (128, 0, 128), "rebeccapurple": (102, 51, 153),
    "red": (255, 0, 0), "rosybrown": (188, 143, 143),
    "royalblue": (65, 105, 225), "saddlebrown": (139, 69, 19),
    "salmon": (250, 128, 114), "sandybrown": (244, 164, 96),
    "seagreen": (46, 139, 87), "seashell": (255, 245, 238),
    "sienna": (160, 82, 45), "silver": (192, 192, 192),
    "skyblue": (135, 206, 235), "slateblue": (106, 90, 205),
    "slategray": (112, 128, 144), "slategrey": (112, 128, 144),
    "snow": (255, 250, 250), "springgreen": (0, 255, 127),
    "steelblue": (70, 130, 180), "tan": (210, 180, 140),
    "teal": (0, 128, 128), "thistle": (216, 191, 216),
    "tomato": (255, 99, 71), "turquoise": (64, 224, 208),
    "violet": (238, 130, 238), "wheat": (245, 222, 179),
    "white": (255, 255, 255), "whitesmoke": (245, 245, 245),
    "yellow": (255, 255, 0), "yellowgreen": (154, 205, 50),
}

# Full RGBA lookup: every CSS colour opaque, plus OpenSCAD's "transparent".
NAMED_COLORS = {name: (r, g, b, 255) for name, (r, g, b) in _CSS_NAMED_RGB.items()}
NAMED_COLORS["transparent"] = (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Viewer colour scheme.
#
# RGBA constants for the default OpenSCAD render colours.  These are interface
# constants (a UI palette), reproduced as plain numeric data.
# ---------------------------------------------------------------------------
COLOR_SCHEME = {
    "background":          (0xff, 0xff, 0xe5, 0xff),
    "background_stop":     (0xff, 0xff, 0xe5, 0xff),
    "axes":                (0x00, 0x00, 0x00, 0xff),
    "opencsg_face_front":  (0xf9, 0xd7, 0x2c, 0xff),
    "opencsg_face_back":   (0x9d, 0xcb, 0x51, 0xff),
    "cgal_face_front":     (0xf9, 0xd7, 0x2c, 0xff),
    "cgal_face_2d":        (0x00, 0xbf, 0x99, 0xff),
    "cgal_face_back":      (0x9d, 0xcb, 0x51, 0xff),
    "cgal_edge_front":     (0xff, 0xec, 0x5e, 0xff),
    "cgal_edge_back":      (0xab, 0xd8, 0x56, 0xff),
    "cgal_edge_2d":        (0xff, 0x00, 0x00, 0xff),
    "crosshair":           (0x80, 0x00, 0x00, 0xff),
}

DEFAULT_COLOR = np.array(COLOR_SCHEME["cgal_face_front"])
BACKGROUND_NODE_COLOR = [0.5, 0.5, 0.5, 0.5]
DEBUG_NODE_COLOR = [1.0, 0.0, 0.0, 0.5]


def _hex_pair_to_unit(text, start):
    """Convert two hex digits at ``text[start:start+2]`` to a 0..1 float."""
    return int(text[start:start + 2], 16) / 255.0


def parse_color(value):
    """Parse an OpenSCAD ``color()`` argument into an RGBA float array.

    Accepts:
      * a 3- or 4-element numeric sequence (already 0..1 floats); a 3-element
        input gets alpha 1.0;
      * a ``"#rrggbb"`` or ``"#rrggbbaa"`` hex string (case-insensitive);
      * a CSS / OpenSCAD colour name (case-insensitive).

    Returns a length-4 ``numpy`` float array ``[r, g, b, a]`` with each
    component in 0..1.
    """
    if isinstance(value, (list, tuple, np.ndarray)):
        comps = list(value)
        if len(comps) == 3:
            return np.array([comps[0], comps[1], comps[2], 1.0], dtype=float)
        if len(comps) == 4:
            return np.array(comps, dtype=float)
        raise ValueError(f"Invalid color vector (need 3 or 4 components): {value!r}")

    if not isinstance(value, str):
        raise TypeError(f"Color must be a sequence or string, got {type(value).__name__}")

    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 6:
            rgb = [_hex_pair_to_unit(digits, i) for i in (0, 2, 4)]
            return np.array(rgb + [1.0], dtype=float)
        if len(digits) == 8:
            rgba = [_hex_pair_to_unit(digits, i) for i in (0, 2, 4, 6)]
            return np.array(rgba, dtype=float)
        raise ValueError(f"Invalid hex color: {value!r}")

    rgba = NAMED_COLORS.get(value.lower())
    if rgba is None:
        raise ValueError(f"Unknown color name: {value!r}")
    return np.array([c / 255.0 for c in rgba], dtype=float)
