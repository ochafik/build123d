# p8 -- Face / selector identity through manifold3d booleans (build123d)

Derisks the **named top integration risk** for a manifold3d mesh backend in
build123d: *build123d's whole UX is selector-driven --
`.faces()`, `.edges()`, `.vertices()`, `sort_by`, `filter_by`, `group_by` --
users select topology to fillet/chamfer/place joints. A triangle mesh has no
faces, only triangles. Can selector identity survive a manifold boolean?*

Short answer: **partially.** Provenance and planar-face selectors are
recoverable and rebuild into *genuine build123d `Face` objects* that
build123d's own `ShapeList` operators accept. Analytic curved-face selectors,
`geom_type`, and fillet/chamfer-after-boolean are **permanently lost**. Full
VERDICT at the bottom.

This is the build123d counterpart of the CadQuery `p4-face-identity` study.
The manifold3d identity mechanism is the same; this prototype re-verifies it
end-to-end against **build123d** inputs (`Box`, `Cylinder`, `tessellate()`),
**build123d** sewing (`BRepBuilderAPI_Sewing`), and -- crucially -- runs
**build123d's real `ShapeList` selectors** on the reconstructed faces.

All claims below were run live against the ddocs venv: build123d
`0.1.dev2749` (editable fork), manifold3d 3.4.1, cadquery-ocp-novtk 7.9.3.1,
trimesh 4.12.2, numpy 2.2.6, Python 3.10, macOS arm64.

## How to run

```bash
VENV=/Users/ochafik/github/.ddocs-venv/bin/python
cd /Users/ochafik/github/build123d/ddocs/prototypes/p8_face_identity

$VENV b3d_manifold.py          # bridge self-test (build123d <-> manifold3d)
$VENV probe_loss.py            # point 1: QUANTIFY the loss
$VENV probe_tags.py            # point 2: tag identity through manifold3d
$VENV probe_refacer.py         # point 3: rebuild build123d selectors
$VENV probe_brep_contrast.py   # point 4: how OCC preserves provenance
$VENV demo_select.py           # end-to-end tag + select + honest failures
$VENV -m pytest test_p8.py -v  # 15 asserts -- all pass
```

## Files

- `b3d_manifold.py` -- the **build123d <-> manifold3d bridge**. Tessellate,
  **weld** (critical, see below), build `Manifold`, tag with `as_original`,
  extract `run_original_id`, sew back to a `Solid`.
- `refacer.py` -- the **re-facer**: region-grows a tagged manifold mesh into
  `ReFace` clusters, then turns each planar cluster into a real build123d
  `Face` via `BRepBuilderAPI_Sewing` + `ShapeUpgrade_UnifySameDomain`.
- `probe_loss.py` -- point 1: the loss, quantified with build123d's own API.
- `probe_tags.py` -- point 2: identity tagging through all 3 booleans + chain.
- `probe_refacer.py` -- point 3: rebuilding selectors; fidelity assessment.
- `probe_brep_contrast.py` -- point 4: OCC `History`/`IsSame` provenance.
- `demo_select.py` -- a `MeshPart` with `.faces()` / `.faces_from()`; the
  worked answer to `manifold_boolean(A,B).faces().sort_by(Axis.Z)[-1].fillet()`.
- `test_p8.py` -- pytest, 15 tests, all passing.

---

## The one bridge gotcha that must be flagged first

build123d's `Shape.tessellate(tol)` returns triangles with **per-face
duplicated vertices**: a 10mm `Box` tessellates to **24 verts / 12 tris** --
each of the 8 corners appears 3x, once per incident face. manifold3d's
`Manifold(Mesh(...))` constructor treats that as **non-manifold** (every edge
is used once, because the neighbour triangle references a *different* vertex
index at the same position). Result: `status() == Error.NotManifold`,
`volume() == 0`, an **empty boolean** -- silently.

Verified (`test_p8.py::test_unwelded_tessellation_is_nonmanifold`):

```
Box->Manifold status: Error.NotManifold volume: 0.0 genus: 1
```

=> A manifold backend **must weld coincident vertices** (snap-round + dedupe)
before handing the mesh to manifold3d. After welding (24 -> 8 verts):

```
welded status: Error.NoError volume: 1000.0 genus: 0
```

This is the build123d-specific lesson; the CadQuery p4 study did not hit it
because it built `Manifold`s directly with `m.Manifold.cube(...)`.

---

## POINT 1 -- The loss, quantified

`probe_loss.py` runs build123d's *own* selector API on a native BREP boolean
vs the same boolean routed through manifold3d and sewn back.

### A plain `Box(10,10,10)` round-trip

| build123d query | BREP Box | manifold round-trip |
|---|---|---|
| `.faces()` | **6** | **12** |
| `.filter_by(GeomType.PLANE)` | 6 | 12 |
| `.faces().sort_by(Axis.Z)[-1]` area | **100.00** | **50.00** (one triangle!) |
| `.filter_by(Plane.XY)` | 2 | 4 |
| `.edges()` | 12 | 18 |

A `Box`'s 6 logical faces become **12 triangle-faces**. Each triangle is its
own analytic `PLANE`, so `geom_type` still *says* `PLANE` (misleadingly) and
`filter_by(GeomType.PLANE)` returns every triangle. `.sort_by(Axis.Z)[-1]`
returns **one arbitrary triangle of the top** (area 50, half the real 100).

### A real boolean `Box(20,20,10) - Cylinder(4,30)`

| | faces | planar | cylindrical |
|---|---|---|---|
| **BREP boolean** | **7** | 6 | **1** |
| **manifold boolean** | **268** | 268 | **0** |

The 7-face BREP result becomes a **268-triangle soup**. The cylindrical bore
wall -- one analytic `CYLINDER` face in BREP -- is gone: it is now a fan of
134 flat triangle-pairs, and `filter_by(GeomType.CYLINDER)` drops to **0**.
`.faces().sort_by(Axis.Z)[-1]` returns a 2.02-area sliver.

**The loss is total if nothing is tagged.** A naive `tessellate -> manifold
boolean -> sew` destroys every selector build123d users rely on.

---

## POINT 2 -- Tagging identity through manifold3d

`probe_tags.py`. manifold3d gives per-triangle identity channels that survive
booleans. Verified live against build123d-sourced inputs:

| Mechanism | What it tracks | Survives booleans? |
|---|---|---|
| `Manifold.as_original()` | marks an input solid; assigns a monotonic id | the id rides every output triangle's run |
| `Mesh.run_original_id` + `run_index` | per-triangle origin **input solid** | **yes** -- union, difference, intersection, chained |
| `Manifold.reserve_ids(n)` + stamped `run_original_id` | **stable, user-controlled** id block | **yes** -- works per-INPUT-FACE |
| `Mesh.face_id` | per-coplanar-region id, unique only within a run | yes, but collides across runs |

`run_index` is in **HALFEDGE units** (3 per triangle) -- triangle range of
run `r` is `run_index[r]//3 : run_index[r+1]//3`. A wrong assumption here
silently mis-attributes every triangle.

### Real output

**TAG 1 -- difference; cut faces are tagged, not orphaned:**
```
input ids: plate A=2  punch B=4
origin histogram      : {2: 24, 4: 8}
triangles on the hole walls (NEW cut faces): 8
  all tagged origin B? True
```
Every boolean-CREATED cut triangle carries the **tool** solid's id.

**TAG 2 -- identity survives union AND intersection:**
```
union (A+B)       : ids present=[8, 10]   histogram={8: 462, 10: 2012}
intersect (A^B)   : ids present=[14, 16]  histogram={14: 426, 16: 884}
```

**TAG 3 -- chained boolean `(A-B)-C`:**
```
ids: A=20 B=22 C=24
(A-B)-C origin histogram: {20: 154, 22: 8, 24: 126}
all 3 input ids still present? True
```
(Note: `C` must carve material `B` did not already remove, else a cylinder
hidden inside `B`'s hole cuts nothing and legitimately drops out.)

**TAG 4 -- per-INPUT-FACE tagging.** `run_original_id` alone tags the *solid*.
To tag each *face*, reserve an id block with `reserve_ids(n)` and stamp one
`run` per face. A 6-face box, each face its own run, cut by a cylinder:
```
reserved id block 30..35 for the 6 faces
after (face-tagged box) - cylinder, origin histogram:
  id  30  ->  box.face[0]       2 tris
  id  34  ->  box.face[4]      67 tris
  id  35  ->  box.face[5]      67 tris
  id  43  ->  cyl(tool)       126 tris
```

**TAG 5 -- recover "all triangles from face X of input A":**
```
query: triangles whose origin == 35 (box.face[5])
recovered 67 triangles.
their centroids all on z=+6 plane? True
```

=> Per-input **and** per-input-face provenance survives every boolean. This
is the foundation for rebuilding selectors.

---

## POINT 3 -- Reconstructing build123d selectors (the re-facer)

`refacer.py` + `probe_refacer.py`. Strategy (adapted from CadQuery p4): two
triangles join the same face iff they (1) share a welded edge, (2) share an
origin id, and (3) have a dihedral angle below `crease_deg` (default 20deg).
Region-growing yields one cluster per logical face. Each **planar** cluster
is then sewn and run through `ShapeUpgrade_UnifySameDomain` -- which **merges
its coplanar triangles into a single build123d `Face`**.

### Fidelity: a plate with a punched square hole

```
mesh has 32 triangles; region-grow -> 10 clusters
  (2, 0)  planar  8 tris  plate  -> 1 Face area=364.0 GeomType.PLANE
  (2, 1)  planar  2 tris  plate  -> 1 Face area=200.0 GeomType.PLANE
  ...
  (4, 6)  planar  2 tris  punch  -> 1 Face area=60.0  GeomType.PLANE
recovered 10 faces vs BREP's 10.
```

A plate + square hole has 10 logical faces (6 plate + 4 walls). The re-facer
rebuilds **exactly 10**, each FLAT face merged into **one** build123d `Face`
with `geom_type == GeomType.PLANE` -- matching the BREP boolean's face count.

### build123d's REAL selectors run on the recovered faces

The recovered faces are genuine build123d `Face` objects, so wrapping them in
a `ShapeList` makes build123d's own operators work:

```
faces.sort_by(Axis.Z)[-1]      -> area=364.0  (the top face of the plate)
faces.sort_by(Axis.Z)[0]       -> area=364.0  (the bottom)
filter_by(GeomType.PLANE)      -> 10 faces
filter_by(Plane.XY)            -> 2 faces (top + bottom)
sort_by(area)[-1]              -> area=364.0
group_by(SortBy.AREA)          -> 3 groups
group_by(Axis.Z)               -> 3 groups
filter_by(lambda f: f.area>300)-> 2 faces
```

This is **not a parallel mock API** -- it is build123d's `ShapeList`,
`sort_by`, `filter_by`, `group_by` operating on real `Face`s.

### Fidelity ceiling: a curved surface

```
Box - Cylinder: 7 clusters (6 planar, 1 curved)
  curved cluster (10, 6): 126 triangles -> 63 build123d face(s)
    every resulting face geom_type -> GeomType.PLANE -- all flat;
    NONE is GeomType.CYLINDER.
```

A curved cluster **cannot** become a single analytic `Face`. It stays a
faceted patch -- 63 flat faces, none of type `CYLINDER`. The bore IS still
**one provenance cluster** (origin=bore), so "the bore wall" is addressable
by **tag**, but not by `GeomType` and not by a direction selector.

### Split faces handled correctly

A bar sliced clean through: the +Z face becomes two disconnected rectangles.
Connectivity region-grow yields **2 separate +Z clusters** (centroids at
x=-6 and x=+6) -- matching build123d, where one `Face` == one connected
region. (manifold's raw `face_id` would keep them as one id -- wrong.)

---

## POINT 4 -- BREP contrast: how OCC preserves provenance for free

`probe_brep_contrast.py`. A BREP boolean does **not re-attribute** faces -- it
**preserves the actual `TopoDS_Face` objects** and exposes a `History` API.

**Selectors keep working with zero bookkeeping:**
```
box - cylinder (build123d native): 7 faces
filter_by(GeomType.CYLINDER) -> 1  (the bore wall, still an ANALYTIC cylinder)
```

**An untouched face is the literal SAME object** (`BRepAlgoAPI_*` with
`SetToFillHistory(True)`):
```
cut a notch in the top; the BOTTOM face is untouched.
History.IsDeleted(bottom)  -> False
History.Modified(bottom)   -> 0 faces  (not reshaped)
result faces IsSame() as the original bottom face: 1
```

**A modified face is History-mapped to its descendants:**
```
cylinder pierces the box fully; the bottom face is reshaped.
History.Modified(bottom)   -> 1 descendant face(s)  (the annulus)
History.Generated(bottom)  -> 2 newly-generated face(s)
```

`BRepAlgoAPI_*` exposes `History()`, `Modified()`, `Generated()`,
`IsDeleted()`, `HasModified()` -- the **exact** BREP equivalent of manifold's
`run_original_id`, except it returns *real analytic faces*, not a
reconstructed approximation. build123d's `_bool_op` (`shape_core.py:2459`)
does not currently surface this API; it could, cheaply.

**What build123d users currently rely on (all FREE in BREP, none in mesh):**
`sort_by(Axis.Z)` picks an extreme *face*; `filter_by(GeomType.X)` needs an
analytic surface type; `filter_by(Plane.XY)` needs a real face normal;
`.edges()`/`.vertices()` need exact topology; `fillet`/`chamfer` need an
analytic edge shared by two analytic faces plus a blend surface; `RigidJoint`
/ `RevoluteJoint` are *placed* on a selected face/edge.

---

## VERDICT

**Selector identity through a manifold boolean is PARTIALLY RECOVERABLE.**
The risk is real but narrower than "triangle soup, all lost."

### What SURVIVES (robust, verified, 15/15 tests pass)

- **Provenance is exact and total.** Every output triangle -- including every
  boolean-CREATED cut face -- is attributable to an input solid (and, with
  `reserve_ids`, to an input *face*) via `run_original_id`. Survives union,
  difference, intersection, and chained booleans. Cut faces inherit the tool
  solid's id; nothing is orphaned.
- **Planar faces rebuild losslessly into REAL build123d `Face` objects.**
  Region-grow by connectivity + crease angle, then sew +
  `ShapeUpgrade_UnifySameDomain`, gives one `Face` per logical planar face
  with the correct count, area, and `geom_type == GeomType.PLANE`.
- **build123d's own `ShapeList` selectors work on the rebuilt faces:**
  `sort_by(Axis)`, `filter_by(Plane)`, `filter_by(GeomType.PLANE)`,
  `filter_by(callable)`, `group_by(SortBy/Axis)` -- all verified.
- **A new provenance selector** (`faces_from('name')`, "faces the boolean
  created") works and has **no BREP equivalent** -- mesh tagging is strictly
  additive on this one axis.

### What is PERMANENTLY LOST (a hard ceiling)

- **Analytic curved-face selectors.** A cylindrical/spherical face is a
  faceted patch; `geom_type == GeomType.CYLINDER` is gone (drops to 0),
  `filter_by(GeomType.CYLINDER)` returns nothing, a direction selector on a
  curved face is geometrically undefined. The curved region is trackable as
  *one provenance cluster* but not addressable by analytic geometry.
- **`fillet` / `chamfer` after a mesh boolean.** They need an analytic edge
  shared by two analytic faces plus an exact blend surface. The re-faced part
  is a pile of independently-sewn faces with no shared topological edges and
  no analytic surfaces. Fillet/chamfer is **undefined**, not merely
  unsupported.
- **Exact edges / vertices as topology.** Only facet edges remain;
  `.edges()`/`.vertices()` no longer return the BREP feature edges.

### DESIGN RECOMMENDATION

1. **Mesh booleans are an opt-in fast path, never the default.** They win on
   speed and robustness to coincident geometry (build123d's
   `ShapeUpgrade_UnifySameDomain`-per-boolean + no fuzzy tolerance pain
   points). The moment a chain needs a fillet, a chamfer, an analytic curved
   selector, or STEP-grade output, it must convert back to BREP or never
   leave it.
2. **Tag-and-rebuild, never preserve.** The bridge must (a) `as_original()`
   each input *leaf* only -- **never a product** (that flattens the run
   table); (b) keep an `{origin_id: metadata}` map; (c) after the boolean
   **rebuild** faces by region growing. Identity is reconstructed, not free.
3. **Weld tessellation output** before handing it to manifold3d -- build123d
   `tessellate()` duplicates vertices per face; un-welded it is non-manifold
   and the boolean silently returns empty.
4. **Expose face-tags as a NEW selector dimension.** `faces_from(solid_name)`
   / "cut faces" is a genuine capability BREP mode lacks. A `MeshPart` should
   surface it alongside the rebuilt `.faces()`.
5. **For planar-dominated CSG** (transpiled OpenSCAD, plate/bracket work) the
   tag-and-remerge path is fully sufficient: it rebuilds exact `Face` counts
   and all directional/plane selectors. **For curved geometry it is
   provenance-only** -- plan `geom_type` and curved selectors to be
   unavailable in mesh mode, and make that a clear error, not a silent empty
   selection.
6. **`crease_deg` (~20deg) is the one tunable** the re-facer must expose: too
   small fragments faceted curves, too large merges genuinely distinct faces.
7. **If build123d wants BREP-mode provenance too**, surface
   `BRepAlgoAPI_*.History()` from `_bool_op` -- it is the exact, lossless
   analogue of `run_original_id` and currently unused.

**The one outcome to forbid: a silent wrong result.** A naive
`tessellate -> manifold boolean -> sew` produces a 268-face triangle soup
whose `.faces().sort_by(Axis.Z)[-1]` returns a 2mm sliver and whose
`geom_type` still lies "PLANE". Mesh mode must either rebuild faces (planar)
or refuse the selector (curved/fillet) -- never mis-answer.
