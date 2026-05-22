# 07 — Prior Art & Ecosystem Survey

**Scope.** Background research for two goals on the `ochafik/build123d` fork:
1. Bring **Manifold** (`manifold3d`) mesh boolean / CSG operations into `build123d`.
2. Wire the owner's **`scad2py`** OpenSCAD transpiler to build123d.

This document surveys prior art so we reuse existing work and learn from others'
mistakes. **Knowledge cutoff is January 2026; today is May 2026.** Where a claim
depends on post-cutoff state it is flagged as *verified via web* (live fetch) or
*uncertain*.

Method: local source inspection of `build123d` and `scad2py`, plus extensive
WebSearch / WebFetch. Every external claim carries a URL.

---

## 1. build123d project context

**What it is.** `build123d` is a Python parametric **BREP** CAD framework built on
the OpenCASCADE (OCCT) kernel via the OCP Python bindings. It is the spiritual
successor to CadQuery, with builder-mode and algebra-mode APIs.
Repo: <https://github.com/gumyr/build123d>. Owner's fork: `ochafik/build123d`.
*Verified:* docs at <https://build123d.readthedocs.io/en/latest/introduction.html>.

**Contribution norms** (from local `CONTRIBUTING.md`):
- Docstrings + tests required for all new code.
- `pylint` and `mypy` must pass; `black` formatting required.
- Tests run via `python -m pytest -n auto`.
- Editable install `pip install -e ".[development]"`.
- Implication for a manifold integration: it must ship with tests, type
  annotations, and clean lint. An optional dependency keeps the core install lean.

**`partcad.yaml`** declares build123d as a PartCAD package (a CAD package manager,
<https://partcad.org/>). It lists `examples/*` parts/sketches/assemblies and pins
`build123d>=0.7.1`, `ocp_vscode`. It is a *catalog* file, not a build config —
relevant only in that any new example files we add could be registered there.
PartCAD also supports OpenSCAD as a part type (per build123d's external-tools
docs, <https://build123d.readthedocs.io/en/latest/external.html>), which is a
minor data point that the build123d ecosystem already treats OpenSCAD as a
first-class neighbor.

### 1.1 Has the community asked for Manifold? — YES, explicitly

**Issue #1228 — "Consider adding optional functionality for 'remixing' STLs via
manifold3d"** (<https://github.com/gumyr/build123d/issues/1228>). *Verified via
web.* Opened **6 Feb 2026 by `jdegenstein`** (a build123d maintainer/active
contributor). Status: **open**, label *Enhancement*, milestone *Not Gating
Release 1.0.0*. Key points:
- Proposes an **optional dependency**: `pip install build123d[manifold]` (name
  TBD), with a runtime availability check.
- Goal: boolean operations between **tessellated build123d objects**, **imported
  STL meshes**, and **native `manifold3d` objects**; export back to mesh formats.
- Explicitly motivated by frustration with **OpenSCAD's STL handling** ("less than
  robust… many silent failures on valid and manifold STLs").
- Includes a pure-`manifold3d` example (`Manifold.sphere(...) + ... ; get_mesh() ;
  export_mesh("union_spheres.stl", mesh)`).
- As of the fetch: **no maintainer code response, no linked PR.** This is an open
  greenfield — the owner's work could *be* the implementation of #1228.

**This is the single most important finding.** The feature is wanted by the
project itself; the design hint (`build123d[manifold]` optional extra, runtime
detection) should be followed to maximize upstream-merge odds.

### 1.2 Related build123d issues mentioning "manifold"

From <https://github.com/gumyr/build123d/issues?q=is%3Aissue+manifold> (*verified
via web*). Note: most of these use "manifold" in the *topological* sense
(watertight), not the *library*:
- **#835 "Create Solid.make_mesh"** (open, enhancement) — wants a first-class mesh
  generation method on solids. Directly adjacent to a manifold bridge: the same
  triangulation that feeds `Solid.make_mesh` is what would feed
  `Solid → manifold3d.Manifold`.
- **#855 "is_manifold() and is_valid discrepancy"** (open, bug) — build123d
  already has `is_manifold` checks; they disagree with `is_valid`. Relevant
  because any mesh→BREP path must decide which validity notion it trusts.
- **#567 "Extrude silently fails resulting in no part"** (open) — an OCC-boolean
  silent-failure class. Reinforces #1228's "silent failure" motivation.
- **#472 "Trouble loading certain STL files"** (closed), **#350 "Mesher() can not
  handle mesh files with interior cavities/holes"** (closed), **#432 "Bug with the
  3MF exporter"** (closed), **#232 "3MF export leaves open edges"** (closed) — a
  pattern of mesh-import/-export fragility around the OCC mesh path.
- **#745 "STEP Missing Surfaces?"** (open, marked OCC core / won't-fix) — example
  of OCC limitations build123d cannot fix from the Python side.

**OCC boolean performance pain:** build123d's own docs and issues do not headline
a boolean-perf complaint, but the *sibling project does loudly* — see §2.

### 1.3 build123d's current mesh code (local source inspection)

- `src/build123d/importers.py` → `import_stl(file_name, model_unit) -> Face`.
  *Important quality finding:* the docstring states the returned object is a **bare
  `Face` reference** built with `BRep_Builder().MakeFace(face, reader)` straight
  from `RWStl.ReadFile_s`. It is **fast but not an editable solid** — "creating an
  editable model (with `Mesher`) may take minutes." So today build123d has **no
  good fast path from triangle soup to a usable BREP solid**. This is a real gap a
  Manifold bridge could partly address (Manifold gives you a guaranteed-manifold
  mesh; converting that to a faceted BREP shell is then a well-posed problem).
- `src/build123d/mesher.py` → `Mesher` class wraps **lib3mf** for 3MF/STL
  read/write. It already checks `mesh_3mf.IsManifoldAndOriented()` and warns "3mf
  mesh is not manifold", and checks `outer_shell.is_manifold`. So build123d
  *already reasons about manifoldness* at the 3MF boundary.
- `src/build123d/brep_from_stl.py` — **see §5; this is major in-tree prior art.**
- `topology/two_d.py`, `topology/one_d.py` — `is_manifold` on Shells; OCC
  `BRepBuilderAPI_NonManifoldWire` handling. Confirms build123d's vocabulary
  already includes manifold/non-manifold.

---

## 2. CadQuery — the sibling project (same OCP kernel)

**What it is.** `CadQuery` (<https://github.com/cadquery/cadquery>) is the older
Python BREP CAD library; build123d shares its OCCT kernel via **OCP**
(<https://github.com/CadQuery/OCP>, the pybind11 OCCT bindings used by *both*).
*Verified:* <https://cadquery.readthedocs.io/en/latest/intro.html>.

**OCC boolean-performance pain — confirmed and loud:**
- **Discussion #1686 "Boolean union operation of groups containing hundreds of
  thousands of elements"** (<https://github.com/CadQuery/cadquery/discussions/1686>).
  *Verified via web.* User reports unioning ~100k+ small parallelepipeds is
  "extremely slow." **Maintainer `adam-urbanczyk`'s answer: there is little to
  gain inside the current framework — "you may need to use a different (i.e. CSG)
  kernel."** This is the maintainer of CadQuery/OCP himself pointing at a CSG
  (mesh) kernel as the fix.
- **Issue #849 "[Discussion] Fuzzy Boolean operations"**
  (<https://github.com/CadQuery/cadquery/issues/849>) — CadQuery exposes OCC's
  "fuzzy" boolean tolerance knob precisely because exact OCC booleans fail on
  near-coincident geometry. Evidence that OCC booleans are *fragile*, not just
  slow.

**Manifold in CadQuery:** no integration found as of the cutoff. The kernel
relationship matters: **anything we build for build123d's OCP↔Manifold bridge is
directly portable to CadQuery**, since both sit on identical OCP/OCCT bindings.
That is an argument for keeping the bridge logic in a clean, kernel-level module
rather than entangled with builder-mode APIs.

**License:** CadQuery and OCP are Apache-2.0 — compatible with build123d
(Apache-2.0) and `manifold3d` (Apache-2.0).

**Borrow:** the kernel-level OCP triangulation/`BRep_Builder` patterns.
**Avoid:** assuming OCC booleans will scale — the sibling project's own maintainer
says they don't.

---

## 3. Manifold ecosystem — what consumes it today

**`manifold3d` / Manifold.** Geometry library by **Emmett Lalish** (Wētā FX) for
**guaranteed-manifold triangle meshes**. Repo <https://github.com/elalish/manifold>;
PyPI <https://pypi.org/project/manifold3d/>. *Verified via web:* PyPI shows
**version 3.4.1, released 24 Mar 2026**, **Apache-2.0**. Python bindings expose a
**numpy-compatible** interface (`Manifold`, `CrossSection`, `MeshGL`/`Mesh`,
level-set/SDF). Core selling point: a **guaranteed-manifold mesh Boolean
algorithm** ("believed to be the first of its kind"), parallelized (Intel TBB);
reliability first, speed second. 2D via Clipper2. Bindings also exist for
JS/TS, C++, Java, C#, Julia, OCaml, Swift, Clojure.

**Who consumes Manifold** (from
<https://github.com/elalish/manifold/discussions/340> "Users of Manifold",
*verified via web*):

| Consumer | What it is | How it uses Manifold |
|---|---|---|
| **OpenSCAD** | The reference programmatic CAD tool | Manifold is its CSG/Minkowski backend (see PR below) |
| **BRL-CAD** | Mature CSG CAD suite | Facetization + CSG eval; success rate 88.9%→98.9%, runtime 7256s→1044s on a test set |
| **Blender** | DCC tool | Manifold as a Boolean solver option in Geometry Nodes (since ~Apr 2024) |
| **Nomad Sculpt** | iPad sculpting app | Clean merge/subtract preserving topology |
| **Babylon.js, three.js-adjacent** | WebGL engines | CSG via WASM build |
| **trimesh** | Python mesh library | `manifold3d` is a boolean *engine* (see §6) |
| **badcad** | Jupyter CAD lib | OpenSCAD-like Python CAD on Manifold (see §4) |
| **Grid.Space, IFCjs, Polygonjs, OCADml** | Web/BIM/procedural tools | Manifold geometry kernel |

**OpenSCAD ↔ Manifold integration — authored by the project owner.**
**openscad/openscad PR #4533 "Use Manifold for much faster & multithreaded CSG &
minkowski operations" by `ochafik`** (<https://github.com/openscad/openscad/pull/4533>,
*verified via web*). **Merged 18 Mar 2023.** This is **the owner's own prior
art** and the most directly transferable experience:
- CSG **5–30× faster** than fast-csg; Minkowski up to **~20×** (a BOSL2 example
  4m31s→4s); a clock-drive assembly 17min (CGAL)→38s (Manifold).
- **Key technical learnings the owner already paid for:**
  - Manifold uses **single-precision floats**; OpenSCAD/OCC use double. The PR
    argued OpenSCAD already had double→single exceptions (Minkowski, transforms).
    *For build123d this precision gap is the central risk* — OCC BREP is exact;
    any round-trip through Manifold loses precision.
  - Manifold **strictly requires valid 2-manifold meshes**; non-manifold input is
    rejected → **mesh-repair logic is needed at the boundary**.
  - TBB multithreading; the owner also parallelized OpenSCAD's Minkowski
    decomposition.
  - **Licensing:** Manifold's Apache-2.0 forced OpenSCAD's combined binaries to
    **GPLv3** (from GPLv2). For build123d (already Apache-2.0) this is a non-issue
    — Apache+Apache is clean.

**Is there an existing Manifold ↔ OpenCASCADE BREP bridge?**
Searches for "manifold opencascade", "manifold brep", "mesh to brep python"
turned up **no dedicated, maintained library** doing OCC-BREP ↔ Manifold-mesh
conversion as of the cutoff. The closest things are:
- **BRL-CAD** uses Manifold *alongside* its own CSG/NURBS kernel, not as a BREP
  bridge — it facetizes BREP→mesh for Manifold, the same one-way direction we'd
  start with. (<https://github.com/elalish/manifold/discussions/340>)
- Generic OCC mesh→BREP via `BRep_Builder` + `BRepBuilderAPI_Sewing` (see §5).
**Conclusion: the OCP↔Manifold bridge is a genuine gap — there is no wheel to
reinvent here, only primitives to assemble.** *(Uncertain: a niche project could
have appeared post-cutoff; worth a fresh search before building.)*

---

## 4. OpenSCAD → other-backend transpilers & alternatives

The landscape splits into **(A) Python→OpenSCAD generators** (the *reverse* of
scad2py — they emit `.scad`), **(B) OpenSCAD-with-embedded-Python**, **(C) native
CAD libraries positioned as OpenSCAD alternatives**, and **(D) actual OpenSCAD→
CAD-library importers**. scad2py is a rare **(D')**: an OpenSCAD→Python *source
transpiler* with its own Manifold-backed runtime.

### A. Python → OpenSCAD code generators (reverse direction)
- **SolidPython / SolidPython2** — `SolidCode/SolidPython`,
  PyPI `solidpython2` (<https://github.com/SolidCode/SolidPython>,
  <https://pypi.org/project/solidpython2/>). Python API that **emits `.scad`**.
  Generalizes Tiefenbacher's `openscad` module; adds "holes". License: LGPL-2.1.
  *Relevance: low* (opposite direction) but its **object model** (a clean Python
  mirror of the OpenSCAD AST) is a useful reference for scad2py's own AST.
- **OpenPySCAD** — `openpyscad` on PyPI. Same idea, method-chaining syntax
  (`cube().translate()`). Less active than SolidPython. *Relevance: low.*
- **PythonOpenScad (POSC)** — `owebeeone/pythonopenscad`
  (<https://github.com/owebeeone/pythonopenscad>, PyPI `pythonopenscad`).
  **Highly relevant.** Started as a `.scad` generator but **now also renders
  directly to meshes via the `manifold3d` backend** (export to STL without
  OpenSCAD installed). *Verified via web.* **Documented limitation: the manifold3d
  backend does not implement `minkowski()`, `import()`, or `surface()`** — exactly
  the same gaps scad2py's README lists as missing (`minkowski`, `rotate_extrude`,
  `text`). **This is the closest analog to scad2py's runtime** and confirms
  `minkowski` on Manifold is hard for everyone. Underpins **AnchorScad**
  (`owebeeone/anchorscad-core`), a parametric modeling layer. License: LGPL-2.1.

### B. OpenSCAD with embedded Python
- **PythonSCAD / `pythonscad`** (<https://www.pythonscad.org/>) — a fork of
  OpenSCAD that makes **Python a native scripting language inside OpenSCAD**.
  *Verified via web:* as of ~Feb 2025, **OpenSCAD core devs began merging large
  chunks of PythonSCAD into upstream OpenSCAD** — i.e. embedded-Python is becoming
  a mainline OpenSCAD feature. *Relevance to scad2py: strategic.* scad2py
  transpiles OpenSCAD→Python *source*; PythonSCAD lets you *write* Python against
  OpenSCAD's kernel. They are complementary, but scad2py's "real Python debugger
  for your OpenSCAD model" pitch overlaps with what PythonSCAD now offers natively.
  scad2py's differentiator must stay: **transpilation to standalone, kernel-
  agnostic Python** (which is exactly what enables a build123d backend).

### C. Native CAD libraries as "OpenSCAD alternatives"
- **build123d / CadQuery** themselves — repeatedly framed as OpenSCAD
  alternatives that fix OpenSCAD's "syntax problem *and* representation problem"
  (BREP vs CSG-mesh), per
  <https://news.ycombinator.com/item?id=41529965> and
  <https://www.oreateai.com/blog/build123d-vs-cadquery-navigating-the-future-of-python-cad-modeling/b9e17e3134422786a0ab67c0a6d1eeda>.
  Wiring scad2py→build123d means **scad2py output gains a true BREP backend** — a
  genuine capability jump over OpenSCAD's mesh-only world (STEP export, fillets,
  NURBS).
- **dslcad** — `DSchroer/dslcad` (<https://github.com/DSchroer/dslcad>), a
  Rust interpreter for a *new* OpenSCAD-inspired language with its own viewer.
  *Relevance: low* — it replaces the language rather than bridging OpenSCAD.
- **microcad** — newer Rust CAD language (Hackaday, Nov 2025,
  <https://hackaday.com/2025/11/26/microcad-programs-cad/>). Same category as
  dslcad. *Relevance: low.*

### D. Actual OpenSCAD → CAD-library importers — **the key prior art for scad2py**
- **FreeCAD OpenSCAD workbench — `importCSG.py`**
  (<https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/OpenSCAD/importCSG.py>,
  by Keith Sloan). **The most directly comparable existing project.** *Verified
  via web.* How it works:
  1. It does **not parse `.scad` directly.** It shells out to the **OpenSCAD
     binary** to convert `.scad` → **`.csg`** (a flattened CSG tree:
     `OpenSCAD_User_Manual/CSG_Export`).
  2. It then parses the `.csg` with **PLY (Python Lex-Yacc)** — the *same parser
     toolkit scad2py uses* (scad2py's `requirements.txt` lists `ply`).
  3. It maps CSG nodes onto **FreeCAD `Part`/`Draft` BREP objects** (OCCT-backed,
     like build123d). So **a working OpenSCAD→OCC-BREP importer already exists.**
  - *Borrow:* the **`.csg` intermediate-representation trick** — converting
    `.scad`→`.csg` first eliminates OpenSCAD's hairy language semantics
    (modules, `$fn`, list comprehensions, `for`) and leaves a flat boolean tree.
    scad2py already produces a richer transpilation, but `.csg` is a useful
    *fallback / validation oracle* and a much smaller target surface. scad2py's
    own `examples/` directory contains `.csg` files (`menger.p.csg`, `out.csg`),
    so the owner has already worked with this IR.
  - *Avoid:* `importCSG.py`'s known limitations — it "does not support the full
    [CSG] format", produces non-parametric output, and has historically been
    fragile (long FreeCAD-forum bug thread). It is a *converter*, not a
    *transpiler* — it loses the source structure. scad2py's advantage is keeping
    parametric Python.
  - Also: **`KeithSloan/OpenSCAD_Alt_Import`** — an installable standalone
    variant of the same workbench. License: FreeCAD workbench is LGPL-2.

**Summary of §4:** No existing tool transpiles OpenSCAD *into build123d*. The
closest is FreeCAD's `importCSG.py` (OpenSCAD→OCC-BREP via the `.csg` IR) and
PythonOpenScad (OpenSCAD-style Python→Manifold mesh). scad2py occupies an
unfilled niche; its build123d backend would be **novel**.

---

## 5. Mesh → BREP reverse-engineering — state of the art

This matters for both goals: a Manifold result is a mesh; to be a *build123d
solid* it must become a BREP.

### 5.1 build123d's in-tree `brep_from_stl.py` — major prior art (local source)
`src/build123d/brep_from_stl.py` — **1970 lines**, header *"by gumyr with codex
gpt-5.4, April 8th 2026"*, Apache-2.0. This is **recent, in-tree, and exactly on
topic.** It reconstructs **approximate analytic BREP primitives** from a triangle
mesh:
- Entry point **`detect_primitives`**. Pipeline: build a `MeshIndex` (face
  centers, normals, adjacency) → detect **planes, spheres, cylinders** (in that
  order, strongest evidence first) → fallback to normal-grouped planes.
- Uses **`sklearn.cluster.DBSCAN`** for clustering, numpy for fitting.
- Per-primitive: broad classification → sew/connect regions → fit analytic
  primitive → region-grow across compatible adjacent faces with fit validation.
- Builders `build_plane_face`, `build_cylinder_face`, `build_sphere_face`;
  emits **build123d code strings** for the detected primitives.

**Implications:**
- build123d is **already investing in mesh→BREP**, but via *analytic primitive
  detection* (recover the design intent: "this is a cylinder"). This is the
  *hard, high-value* path (RhinoResurf / Geomagic territory — see §5.3).
- A **Manifold bridge is the complementary easy path**: when you do *not* need
  analytic surfaces, just wrap the manifold triangle mesh as a **faceted BREP
  shell** (one planar `Face` per triangle, sewn into a `Shell`/`Solid`). Fast,
  lossless w.r.t. the mesh, always works.
- **Recommendation:** treat these as two tiers. Tier 1 = faceted BREP shell from
  a Manifold mesh (cheap, for booleans/export). Tier 2 = `brep_from_stl`'s
  primitive detection (expensive, for editable analytic models). The Manifold
  bridge should *feed* Tier 2: give `detect_primitives` a clean, guaranteed-
  manifold mesh instead of raw STL soup, improving its hit rate.

### 5.2 OCC primitives for mesh→BREP
- **`BRep_Builder` + `BRepBuilderAPI_MakeFace`** — build one face per triangle.
- **`BRepBuilderAPI_Sewing`** — stitch faces into a `Shell`; **tolerance must be
  set carefully** (sew tolerance *and* per-face tolerance; a `manifold` flag
  exists). <https://dev.opencascade.org/doc/refman/html/class_b_rep_builder_a_p_i___sewing.html>.
- Then `BRep_Builder::Add` a `Shell` into a `Solid`.
- This is exactly the faceted-shell Tier-1 path; **build123d's `import_stl`
  already does the degenerate version** (one big `Face`, no sewing). The
  improvement is per-triangle faces + sewing + solid wrapping.
- *Caveat:* a BREP solid with one face per triangle is **heavy** (OCC topology is
  expensive per entity); fine for thousands of triangles, painful for millions.
  Keep large meshes as Manifold/`Mesher` objects; only convert to BREP on demand.

### 5.3 Commercial / research state of the art
General mesh→BREP (recovering editable analytic CAD) is **"the most difficult
step"** of reverse engineering
(<https://novedge.com/blogs/design-news/mesh-to-brep-from-scans-to-intentful-editable-b-reps>).
Commercial tools: **Geomagic Design X, Siemens NX, Creo, Fusion 360,
RhinoResurf/RhinoResurf** (<https://www.resurf3d.com/>). They do interactive
feature recognition + NURBS fitting + kernel healing. **Takeaway:** do *not*
attempt general freeform mesh→NURBS — it is a product category, not a feature.
`brep_from_stl.py`'s scoped approach (planes/cylinders/spheres only) is the
*correct* pragmatic subset.

---

## 6. Mesh CAD / boolean libraries in Python — alternatives & complements to Manifold

| Library | Role | Boolean engine(s) | License | Notes |
|---|---|---|---|---|
| **manifold3d** | Guaranteed-manifold mesh + CSG | own algorithm | Apache-2.0 | Fast, parallel, robust *iff* input is 2-manifold. The chosen kernel. |
| **trimesh** | Swiss-army Python mesh lib | pluggable: `manifold`, `blender`, `scad` | MIT | `trimesh.boolean` dispatches to engines; **Manifold engine is the fast default**. <https://trimesh.org/trimesh.boolean.html> |
| **pyvista / VTK** | Viz + mesh analysis on VTK | VTK `vtkBooleanOperationPolyDataFilter` | MIT (pyvista) | Booleans only for **closed/manifold** `PolyData`; VTK booleans are **notoriously fragile** on coincident faces. <https://docs.pyvista.org/examples/01-filter/boolean_operations.html> |
| **vedo** | Scientific 3D on VTK + numpy | VTK booleans | MIT | Interop with trimesh/pyvista/pymeshlab. Viz-oriented. <https://github.com/marcomusy/vedo> |
| **pymeshlab** | Python bindings to MeshLab | MeshLab filters | GPL-3.0 | **GPL — a licensing hazard** for an Apache-2.0 project; avoid as a hard dep. |
| **PyMesh** | Geometry-processing lib | CGAL, Cork, Carve, **libigl/`igl`**, Clipper | MPL-2.0 (mixed) | Multi-engine; `igl` default for 3D. Hard to build; effectively unmaintained. |
| **libigl / `igl`** | C++ geometry lib + py bindings | exact-predicate mesh booleans | MPL-2.0 | Robust (exact predicates) but slower; research-grade. |
| **CGAL (bindings)** | Exact-arithmetic geometry | Nef polyhedra / corefinement | GPL-3.0 (or commercial) | OpenSCAD's *old* backend; **slow** and **GPL**. The thing Manifold/PR#4533 replaced. |
| **MeshLib** | Commercial-ish geometry lib | own | proprietary-ish (mixed) | Markets itself as a faster, more robust CGAL alternative; auto-converts non-manifold input. <https://meshlib.io/blog/cgal-alternative-for-3d-boolean-operations/> |

**Comparison verdict (robustness/speed):**
- **Manifold** is the right kernel: fastest robust option, Apache-2.0, already
  the owner's tool of choice, already a `trimesh` engine, already OpenSCAD's
  backend. Its one hard requirement — **2-manifold input** — is the entire design
  constraint of any bridge.
- **VTK/pyvista/vedo booleans**: avoid for production CSG — fragile on coincident
  geometry; fine for visualization.
- **pymeshlab and CGAL are GPL** → cannot be a *hard* dependency of Apache-2.0
  build123d. (`trimesh` is MIT and safe.)
- **libigl** is the robustness fallback (exact predicates) if Manifold ever
  chokes, but slower and heavier.
- Research context: robust mesh booleans remain genuinely hard — see *"Interactive
  and Robust Mesh Booleans"* (<https://arxiv.org/pdf/2205.14151>). Manifold's
  guarantee is *conditional* on valid input; it is not magic.

**Mesh-repair note.** Because Manifold rejects non-manifold input, a bridge needs
a repair stage for imported STLs. Options: `trimesh`'s repair utilities,
Manifold's own `Merge`, lib3mf. build123d's `Mesher` already surfaces
manifold/oriented status — reuse it as the gate.

---

## 7. The owner's own forks

From `scad2py/requirements.txt` (local):
```
git+https://github.com/ochafik/trimesh.git@ochafik-svg-io-color
# git+https://github.com/ochafik/manifold.git@pyodide-build   (commented out)
```

### 7.1 `ochafik/trimesh` @ `ochafik-svg-io-color`
- **Active dependency** of scad2py. The `requirements.txt` comment labels it
  *"Fork of trimesh with SVG color support."*
- The branch name (`svg-io-color`) and comment indicate it adds **color support
  to trimesh's SVG import/export** path — scad2py needs this because OpenSCAD
  models can carry per-object colors (`color()`), and a stated scad2py goal is
  *"No support for colors in outputs"* being a thing it *fixes*. Upstream trimesh
  SVG I/O drops color.
- *Could not verify the exact diff* via WebFetch (GitHub branch page did not
  expose commit detail to the fetcher). **Uncertain:** whether this is upstreamed.
  *Recommendation:* check whether upstream `trimesh` (MIT) has since gained SVG
  color I/O so the fork can be dropped — a fork in a dependency chain is
  maintenance debt, and #1228's `build123d[manifold]` extra should depend only on
  PyPI packages, never a git fork.

### 7.2 `ochafik/manifold` @ `pyodide-build` (commented out)
- **Disabled** in `requirements.txt` (the line is commented; scad2py currently
  uses upstream `manifold3d` from PyPI).
- Branch name `pyodide-build` → a fork that **builds Manifold for Pyodide**
  (CPython compiled to WebAssembly), i.e. to run Manifold **in the browser**.
  scad2py's README heavily emphasizes **Colab / browser support** and the repo
  contains `pyscript.json`, `pyscript-manifold.json`, `.pyodide-xbuildenv*` —
  consistent with a WASM target.
- It is commented out because **upstream `manifold3d` now ships usable
  wheels/WASM** *(uncertain — likely, given Manifold's first-class WASM/npm story;
  worth confirming whether a Pyodide wheel exists on PyPI)*.
- *Relevance to build123d:* build123d's external-tools docs mention **`OCP.wasm`**
  (build123d deps ported to the browser,
  <https://build123d.readthedocs.io/en/latest/external.html>). If the long-term
  vision is browser-runnable build123d+Manifold, the owner's Pyodide-build
  experience for Manifold is directly reusable — but **not on the critical path**
  for the core integration.

**Net:** the trimesh fork is a live, load-bearing dependency (SVG color); the
manifold fork is dormant (Pyodide/WASM). Neither blocks the build123d work, but
the `build123d[manifold]` extra must pin **PyPI `manifold3d`**, not a fork.

---

## 8. Lessons & recommendations

**Reuse — don't rebuild:**
1. **Follow issue #1228's prescription verbatim.** Ship as an **optional extra
   `build123d[manifold]`** with **runtime detection**; do not make `manifold3d` a
   core dependency. The maintainer who filed #1228 (`jdegenstein`) has already
   blessed this shape — maximum merge-ability.
2. **Use `manifold3d` from PyPI** (Apache-2.0, v3.4.1+). Apache+Apache is clean;
   no GPL contagion. Do **not** depend on `ochafik/manifold` or any GPL mesh lib
   (`pymeshlab`, CGAL).
3. **Mine `brep_from_stl.py`.** It is in-tree, Apache-2.0, recent (Apr 2026), and
   solves the *hard half* (analytic primitive recovery). The Manifold bridge
   should produce the clean manifold mesh that `detect_primitives` consumes —
   they compose into a two-tier pipeline (faceted shell vs analytic BREP).
4. **Reuse build123d's existing manifold-awareness:** `Mesher.IsManifoldAndOriented`,
   `Shell.is_manifold`. Use them as the input-validation gate before handing a
   mesh to Manifold (which rejects non-manifold input).
5. **Lean on the owner's OpenSCAD PR #4533 experience.** The single-precision
   gotcha, the mandatory mesh-repair stage, and the Minkowski-decomposition work
   are already-solved problems for the owner — transfer them.
6. **For scad2py→build123d, study FreeCAD `importCSG.py`.** Borrow the
   **`.scad`→`.csg` intermediate-representation** idea as a *fallback path and
   validation oracle*. Same PLY toolchain scad2py already uses. Borrow *nothing*
   of its non-parametric, fragile output style.
7. **trimesh stays the glue.** It is MIT, already a scad2py dependency, and
   already has a Manifold boolean engine — useful for mesh repair and format I/O
   around the bridge.

**Genuine gaps this project must fill (no prior art to copy):**
- **An OCP-BREP ↔ Manifold-mesh converter.** No maintained library does this.
  Needs: (a) `Solid`/`Shell` → `manifold3d.Manifold` (triangulate via OCC
  incremental mesher, feed Manifold), and (b) `Manifold` → faceted BREP
  `Solid` (per-triangle faces + `BRepBuilderAPI_Sewing` + solid wrap). Both
  directions are *assemble-the-primitives* work, not research.
- **A precision/tolerance policy** for the round trip — OCC is exact, Manifold is
  float32. Decide where the lossy boundary is and document it (likely: Manifold
  is for *mesh-domain* booleans/remixing; never silently round-trip an analytic
  BREP through it).
- **A non-manifold input repair stage** for imported STLs (the #1228 motivation).
- **scad2py's build123d backend** is novel: no tool transpiles OpenSCAD *into*
  build123d. scad2py's existing AST + Manifold runtime is the foundation; the new
  work is a build123d-emitting codegen target alongside the current Manifold one.

**Things to explicitly avoid:**
- General freeform mesh→NURBS reverse engineering — a commercial product
  category, not a feature (§5.3).
- A BREP solid with one face per triangle for *million*-triangle meshes — keep
  big meshes as Manifold objects; convert lazily.
- Trusting OCC booleans to scale — CadQuery's own maintainer says they don't (§2).
- GPL dependencies in an Apache-2.0 project.
- A git-fork dependency in the `build123d[manifold]` extra.

---

## 9. Open questions

1. **#1228 ownership:** does `jdegenstein` (or another maintainer) intend to
   implement #1228 themselves, or is a PR from `ochafik/build123d` welcome? Worth
   commenting on the issue before significant work.
2. **API surface:** should Manifold integration be (a) a new geometry type
   exposed in builder/algebra modes, (b) a `Solid.to_manifold()` / `from_manifold()`
   pair plus mesh-domain boolean ops, or (c) a transparent fast-path that build123d
   picks when both operands are mesh-friendly? #1228 leans toward (b).
3. **Precision contract:** what tolerance does a Manifold round-trip guarantee,
   and how is it surfaced to users so they don't accidentally degrade an analytic
   model? (OCC double vs Manifold float32.)
4. **`Solid.make_mesh` (#835):** should the BREP→mesh half of the bridge *be* the
   implementation of #835, unifying two open issues?
5. **Faceted-BREP cost:** at what triangle count does per-triangle-face BREP
   construction become unusable? Need a benchmark to set the lazy-conversion
   threshold.
6. **scad2py codegen target:** does scad2py emit build123d *source code* (like it
   emits Python today, like FreeCAD's importer emits objects) or call build123d's
   API at runtime? Source emission keeps the "keep developing in the target
   language" promise; runtime calls are simpler.
7. **`.csg` IR:** is the OpenSCAD `.csg` flattened tree a useful validation oracle
   for scad2py→build123d, given scad2py already has richer parsing? Possibly only
   for regression tests.
8. **Fork hygiene:** has upstream `trimesh` gained SVG color I/O (letting
   `ochafik/trimesh` be retired), and does PyPI `manifold3d` now ship a Pyodide
   wheel (retiring `ochafik/manifold@pyodide-build`)? *Uncertain — verify before
   depending on anything.*
9. **Post-cutoff check:** re-run searches for "manifold opencascade brep" and
   "build123d manifold" right before building — a bridge library or an upstream
   PR could have landed between Jan 2026 (knowledge cutoff) and now.

---

## Sources

- build123d repo — <https://github.com/gumyr/build123d>
- build123d issue #1228 (manifold3d STL remixing) — <https://github.com/gumyr/build123d/issues/1228>
- build123d issues filtered "manifold" — <https://github.com/gumyr/build123d/issues?q=is%3Aissue+manifold>
- build123d docs (intro) — <https://build123d.readthedocs.io/en/latest/introduction.html>
- build123d external tools — <https://build123d.readthedocs.io/en/latest/external.html>
- build123d import/export docs — <https://build123d.readthedocs.io/en/latest/import_export.html>
- Local: `build123d/CONTRIBUTING.md`, `partcad.yaml`, `src/build123d/{importers,mesher,brep_from_stl}.py`, `src/build123d/topology/{two_d,one_d}.py`
- Local: `scad2py/requirements.txt`, `scad2py/README.md`, `scad2py/scad2py/{csg,rendering/manifold_renderer}.py`
- CadQuery repo — <https://github.com/cadquery/cadquery>
- CadQuery OCP bindings — <https://github.com/CadQuery/OCP>
- CadQuery discussion #1686 (slow booleans, "use a CSG kernel") — <https://github.com/CadQuery/cadquery/discussions/1686>
- CadQuery issue #849 (fuzzy booleans) — <https://github.com/CadQuery/cadquery/issues/849>
- CadQuery docs — <https://cadquery.readthedocs.io/en/latest/intro.html>
- Manifold repo — <https://github.com/elalish/manifold>
- manifold3d on PyPI — <https://pypi.org/project/manifold3d/>
- Manifold "Users of Manifold" discussion #340 — <https://github.com/elalish/manifold/discussions/340>
- Manifold performance discussion #383 — <https://github.com/elalish/manifold/discussions/383>
- Manifold non-manifold import discussion #471 — <https://github.com/elalish/manifold/discussions/471>
- OpenSCAD PR #4533 (Manifold backend, by ochafik) — <https://github.com/openscad/openscad/pull/4533>
- SolidPython — <https://github.com/SolidCode/SolidPython> ; solidpython2 — <https://pypi.org/project/solidpython2/>
- PythonOpenScad — <https://github.com/owebeeone/pythonopenscad> ; PyPI — <https://pypi.org/project/pythonopenscad/>
- AnchorScad core — <https://github.com/owebeeone/anchorscad-core>
- OpenPySCAD — <https://pypi.org/project/openpyscad/>
- PythonSCAD — <https://www.pythonscad.org/>
- dslcad — <https://github.com/DSchroer/dslcad>
- microcad (Hackaday) — <https://hackaday.com/2025/11/26/microcad-programs-cad/>
- FreeCAD `importCSG.py` — <https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/OpenSCAD/importCSG.py>
- KeithSloan/OpenSCAD_Alt_Import — <https://github.com/KeithSloan/OpenSCAD_Alt_Import>
- OpenSCAD CSG export format — <https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/CSG_Export>
- trimesh boolean docs — <https://trimesh.org/trimesh.boolean.html>
- trimesh repo / boolean.py — <https://github.com/mikedh/trimesh>
- badcad — <https://github.com/wrongbad/badcad>
- pyvista boolean operations — <https://docs.pyvista.org/examples/01-filter/boolean_operations.html>
- vedo — <https://github.com/marcomusy/vedo>
- PyMesh boolean docs — <https://pymesh.readthedocs.io/en/latest/mesh_boolean.html>
- MeshLib vs CGAL — <https://meshlib.io/blog/cgal-alternative-for-3d-boolean-operations/>
- "Interactive and Robust Mesh Booleans" — <https://arxiv.org/pdf/2205.14151>
- OCC BRepBuilderAPI_Sewing reference — <https://dev.opencascade.org/doc/refman/html/class_b_rep_builder_a_p_i___sewing.html>
- OCC convert triangular mesh to BRep (forum) — <https://dev.opencascade.org/content/convert-triangular-mesh-brep>
- Mesh-to-BRep (commercial landscape) — <https://novedge.com/blogs/design-news/mesh-to-brep-from-scans-to-intentful-editable-b-reps>
- RhinoResurf / RESURF — <https://www.resurf3d.com/>
- build123d vs CadQuery comparison — <https://www.oreateai.com/blog/build123d-vs-cadquery-navigating-the-future-of-python-cad-modeling/b9e17e3134422786a0ab67c0a6d1eeda>
