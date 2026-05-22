# 06 — Open-Source License Analysis: build123d + manifold3d + OpenCASCADE + scad2py

> **NOT LEGAL ADVICE.** I am Claude, an AI assistant, not a lawyer. This document is a
> technical engineering analysis of license texts to help you reason about the project.
> It is intended to inform a conversation with a qualified open-source attorney, not to
> replace one. License interpretation (especially around "derivative work" and "linking")
> is genuinely contested in case law; where that is true, this document says so explicitly.
> Before shipping anything where the answer matters commercially, get a real legal review.

**Date:** 2026-05-22
**Scope:** Two concrete goals being evaluated —
1. Adding `manifold3d`-based mesh operations into build123d (upstreamed and/or as a separate package).
2. Importing the owner's `scad2py` OpenSCAD transpiler and wiring it to build123d.

---

## 1. Executive verdict (read this first)

| Goal | Verdict | Why |
|---|---|---|
| Ship a manifold3d mesh feature in build123d | **GO** | manifold3d is Apache-2.0, same as build123d. Pure-Apache combination. |
| Merge scad2py into build123d, or ship as sibling package | **GO, but TWO files must be dealt with first** | The owner is the sole copyright holder of his *original* scad2py code, so relicensing it to Apache-2.0 is unobstructed. **However**, `scad2py/calc.py` and `scad2py/colors.py` carry **GPL-2.0-or-later headers, copyright Clifford Wolf / Marius Kintel** — they are line-by-line ports of OpenSCAD C++ and are **NOT the owner's copyright**. They cannot be relicensed and cannot enter an Apache-2.0 build123d. See §6.4 / §8. |
| Distribute build123d wheels (current state) | **GO** | The OCCT (LGPL+exception) dependency is dynamically linked via a separate wheel. Add a NOTICE file. |
| Re-license / close-source any of this | **NO for OCCT path** | OCCT is LGPL — you can build *on top of* it but cannot relicense it. Not a problem for an open-source project; just be aware. |
| Ship scad2py's `examples/` as-is | **CAUTION** | Some `.scad` examples (`bosl2_*.scad`) are derived from BOSL2 (BSD-2-Clause) and need attribution; others have unclear provenance. |
| Depend on CoACD | **OK** | CoACD is MIT (permissive). Only an *optional* dep in scad2py anyway. |

**One-paragraph summary:** Everything in the core stack is permissively licensed
(Apache-2.0 / MIT / BSD / Boost) **except OpenCASCADE Technology (OCCT)**, which is
**LGPL-2.1 + the Open CASCADE Exception 1.0**. Because OCCT is consumed as a
*dynamically linked, separately distributed* binary (the `cadquery-ocp` / `cadquery-ocp-novtk`
wheel), and because build123d is itself open source, the LGPL imposes only mild,
satisfiable obligations (notice + ability for users to swap the library). The owner is the
**sole copyright holder of his original scad2py code**, so he can place that code under any
license he chooses (Apache-2.0) with zero relicensing friction — the missing LICENSE file
is a formality, not a legal blocker. **The one genuine GPL landmine** is that two scad2py
files (`calc.py`, `colors.py`) are *not* the owner's code — they are GPL-2.0+ ports of
OpenSCAD C++ by Clifford Wolf / Marius Kintel, and they are *actively imported by core
scad2py runtime modules*. Those two files must be re-implemented or removed before scad2py
can ship inside Apache-2.0 build123d (see §6.4). The transpiler *architecture* itself does
not create a GPL obligation — that conclusion stands. Remaining *action items* are
housekeeping: add a `LICENSE` file to scad2py, add a `NOTICE` file, clean up example-file
provenance, and resolve the two GPL-headed files.

---

## 2. Component inventory and confirmed licenses

Each row below is backed by a file path inspected on this machine or a cited URL.

| Component | License | Evidence |
|---|---|---|
| **build123d** | Apache-2.0 | `/Users/ochafik/github/build123d/LICENSE` (full Apache 2.0 text); `pyproject.toml` → `license = {text = "Apache-2.0"}` |
| **OpenCASCADE Technology (OCCT)** | **LGPL-2.1 + Open CASCADE Exception 1.0** | opencascade.com / OCCT repo `OCCT_LGPL_EXCEPTION.txt`, `LICENSE_LGPL_21.txt`; SPDX `OCCT-exception-1.0` |
| **cadquery-ocp-novtk** (`OCP`) | OCCT = LGPL-2.1+exc; pybind11 binding layer = generally permissive | wheel `/tmp/pipcheck/cadquery_ocp_novtk-7.9.3.1-cp310-cp310-macosx_11_0_arm64.whl` |
| **manifold3d** 2.3.1 | Apache-2.0 | `…/site-packages/manifold3d-2.3.1.dist-info/licenses/LICENSE` (full Apache text); `METADATA` classifier `License :: OSI Approved :: Apache Software License` |
| **Clipper2** (bundled in Manifold) | Boost Software License 1.0 | Manifold `METADATA` lists Clipper2 dependency; Clipper2 upstream license |
| **quickhull** (bundled in Manifold) | Public domain / Unlicense | Manifold `METADATA` lists quickhull; akuukka/quickhull upstream |
| **GLM, oneTBB, Thrust** (Manifold build-time) | MIT / Apache-2.0 / Apache-2.0 | Manifold `METADATA` system-dependency list |
| **scad2py** | *Intended* Apache-2.0, **no LICENSE file present** | `/Users/ochafik/github/scad2py/setup.py` commented block says `license="http://www.apache.org/licenses/LICENSE-2.0"`; no `LICENSE` file in repo |
| **OpenSCAD** | GPL-2.0-or-later | openscad.org — **referenced by scad2py but NOT linked** (see §6) |
| **ply** | BSD (3-clause style) | scad2py `requirements.txt`; installed `ply` metadata → `License: BSD` |
| **trimesh** (and ochafik fork) | MIT | installed `trimesh` metadata → `MIT`; fork `git+https://github.com/ochafik/trimesh.git@ochafik-svg-io-color` |
| **CoACD** | **MIT** (permissive — confirmed) | github.com/SarahWeiii/CoACD `LICENSE` — *optional* dep in scad2py |
| **shapely** | BSD-3-Clause | installed metadata → `BSD 3-Clause` |
| **rtree** | MIT | installed metadata → `MIT` |
| **pyrender** | MIT | upstream mmatl/pyrender |
| **gradio** | Apache-2.0 | upstream gradio-app/gradio |
| **lib3mf** (build123d dep) | BSD-2-Clause | build123d `pyproject.toml` dep; 3MF Consortium lib3mf upstream |
| **ezdxf** (build123d dep) | MIT | build123d `pyproject.toml` dep; mozman/ezdxf upstream |
| **ocpsvg** (build123d dep) | Apache-2.0 | build123d `pyproject.toml` dep |
| **ocp_gordon** (build123d dep) | Apache-2.0 (a re-impl of OCCT Gordon-surface code) | build123d `pyproject.toml` dep |
| **numpy / scipy / sympy / scikit-learn / svgpathtools / anytree / webcolors / ipython** | BSD / MIT family | build123d `pyproject.toml` deps — all permissive |

> Note on `cadquery-ocp-novtk` wheel inspection: the wheel's `.dist-info/` contains only
> `METADATA`, `RECORD`, `WHEEL`, `top_level.txt`, `entry_points` — **no `LICENSE` file is
> bundled inside the wheel**. This is itself a (minor) compliance gap in the *upstream*
> `cadquery-ocp` package, not in build123d, but it matters because build123d redistributes
> users to it. The wheel ships compiled OCCT shared libraries under `OCP/.dylibs/`
> (`libTKernel.7.9.3.dylib`, `libTKBO.*.dylib`, `libTKTopAlgo.*`, `libTKBool.*`, etc. — the
> full set of OCCT "TK" toolkits) plus the `OCP` pybind11 binding `.so`. So the wheel **is**
> a binary redistribution of OCCT.

---

## 3. The OpenCASCADE license — the crux of the whole analysis

### 3.1 Base license: LGPL-2.1

OCCT is licensed under the **GNU Lesser General Public License, version 2.1**. The LGPL is
a *weak copyleft* license. Its central bargain (relevant clauses paraphrased; the binding
text is in `LICENSE_LGPL_21.txt`):

- **§2** — You may modify the Library, but modifications to *the Library itself* must be
  released under the LGPL.
- **§5 ("a work that uses the Library")** — A program that merely *uses* the Library's
  interfaces, but contains no derivative of the Library, is **not** covered by the LGPL
  and may be licensed however you like. Calling OCCT's API from build123d/OCP makes
  build123d "a work that uses the Library."
- **§6** — This is the operative clause for distributing a *combined* work (your program
  linked with the Library). The LGPL §6 normally requires that you distribute the work in
  a way that lets the end user **replace the LGPL'd library with a modified version and
  relink**. In practice §6 is satisfied by **dynamic linking** (ship the library as a
  separate `.so`/`.dylib`/`.dll` the user can swap) OR, for static linking, by also
  shipping the *object files* of your own code so the user can relink.
- **§6 also** requires: a prominent notice that the Library is used and covered by the
  LGPL, and a copy of the LGPL.

### 3.2 The Open CASCADE Exception 1.0 — what it actually changes

OCCT does **not** ship under bare LGPL-2.1. It ships under LGPL-2.1 **plus** the
*Open CASCADE Exception 1.0*. The **complete verbatim text** of that exception
(SPDX identifier `OCCT-exception-1.0`; file `OCCT_LGPL_EXCEPTION.txt` in the OCCT repo) is:

> *"Open CASCADE Exception (version 1.0) to GNU LGPL version 2.1.*
>
> *The object code (i.e. not a source) form of a "work that uses the Library" can
> incorporate material from a header file that is part of the Library. As a special
> exception to the GNU Lesser General Public License version 2.1, you may distribute
> such object code incorporating material from header files provided with the Open
> CASCADE Technology libraries (including code of CDL generic classes) under terms of
> your choice, provided that you give prominent notice in supporting documentation to
> this code that it makes use of or is based on facilities provided by the Open CASCADE
> Technology software."*

**What problem the exception solves.** Bare LGPL-2.1 has a well-known sharp edge for C/C++
libraries: LGPL §6's last paragraph says that if a header file you `#include` contains
more than "numerical parameters, data structure layouts and accessors, and small macros
and small inline functions (ten lines or less in length)", then the *object code that
embeds that header content* is itself treated as a derivative of the Library — not as a
mere "work that uses the Library." OCCT's headers contain substantial inline code,
templates, and CDL-generated generic classes. Under bare LGPL-2.1, that would arguably
drag any compiled program that includes OCCT headers under the LGPL's §6 relink obligation
even when the author thought they were only "using" the library.

**What the exception does, precisely.** The exception **amends the LGPL-2.1 §6 boundary**.
It declares that the *object code* form of a "work that uses the Library" **may
incorporate material from OCCT header files** and may then be distributed **"under terms
of your choice"** — i.e. you are *not* forced into LGPL §6's relink obligation merely
because OCCT's header content ended up inlined into your binary. The single condition is
a **"prominent notice in supporting documentation"** that the code uses / is based on OCCT.

**The practical effect for someone distributing software that links OCCT:**

1. Your *own* code (build123d, the OCP bindings, an application) that calls OCCT and
   inlines OCCT header material into its compiled binary **does not become LGPL-covered**.
   You may license your own code under Apache-2.0 (or anything).
2. You still must give **prominent notice** that OCCT is used.
3. The exception covers **header material only**. The OCCT *libraries themselves*
   (`libTK*.so/.dylib`) remain LGPL-2.1. If you *modify the OCCT source*, those
   modifications are still LGPL. You cannot relicense OCCT.
4. Because the exception removes the relink trap, you are effectively free to **statically
   link OCCT into a proprietary, closed-source program** and still keep your own code
   proprietary — *provided* you ship the notice and (best practice) make the OCCT source
   and any OCCT modifications available. Open CASCADE's own licensing page confirms static
   linking into commercial software is permitted with notice + license-copy obligations.

For build123d this is even easier than the proprietary case: build123d is itself
Apache-2.0 open source, OCCT is consumed as a **separate dynamically linked wheel**, and
nobody is modifying OCCT. The obligations reduce to **notice + redistributing the license
text**.

**Sources:**
- [Open CASCADE Exception 1.0 — SPDX](https://spdx.org/licenses/OCCT-exception-1.0.html)
- [Licensing — Open CASCADE Technology](https://dev.opencascade.org/resources/licensing)
- [OCCT_LGPL_EXCEPTION.txt — OCCT repo](https://github.com/Open-Cascade-SAS/OCCT/blob/master/OCCT_LGPL_EXCEPTION.txt)
- [Open CASCADE Technology changes its license to GNU LGPL](https://dev.opencascade.org/content/open-cascade-technology-changes-its-license-gnu-lgpl)

### 3.3 cadquery-ocp / cadquery-ocp-novtk — how OCCT actually reaches you

`cadquery-ocp-novtk` (importable as `OCP`) is the PyPI wheel that build123d depends on
(`cadquery-ocp-novtk >= 7.9, < 8.0`). The wheel inspected
(`cadquery_ocp_novtk-7.9.3.1-cp310-cp310-macosx_11_0_arm64.whl`) contains:

- **`OCP/.dylibs/libTK*.dylib`** — the compiled OCCT toolkits. These are **LGPL-2.1 +
  exception**. Redistributing the wheel = redistributing OCCT in binary form.
- **`OCP/*.so`** — the pybind11-generated Python binding extension. The OCP binding code
  (cadquery's `OCP` project, generated by `pywrap`) is the "work that uses the Library."
  The OCP project is generally distributed under permissive terms (Apache-2.0-style); the
  binding code being a "work that uses the Library" plus the OCCT exception means OCP can
  be permissively licensed even though it links OCCT.
- Bundled image/IO libs (`libwebp`, `libjpegxr`, `libOpenEXR*`, `libIex`, `libIlmThread`)
  — these are upstream OCCT optional 3rd-party deps under BSD/permissive licenses.

**Compliance gap noticed:** the wheel's `.dist-info/` ships **no LICENSE file**. The wheel
*is* a binary redistribution of LGPL'd OCCT, so strictly LGPL §6 wants the license text and
notice to travel with it. This is an *upstream `cadquery-ocp` issue*, not a build123d bug —
but build123d's own NOTICE file should still name OCCT and point at the LGPL (see §9).

---

## 4. The Apache-2.0 license — obligations build123d must meet

build123d, manifold3d, ocpsvg, ocp_gordon, and gradio are all Apache-2.0. Apache-2.0 is a
permissive license. Its obligations when you redistribute Apache-2.0 code (or a derivative):

- **§4(a)** — give recipients a copy of the License.
- **§4(b)** — keep "carry forward" notices: modified files must carry prominent notices
  stating you changed them.
- **§4(c)** — retain all copyright, patent, trademark, attribution notices from the source.
- **§4(d)** — **the NOTICE file**: if the work you received includes a `NOTICE` file, your
  redistribution must include a readable copy of the attribution notices from it.
- **§3 — Patent grant.** *This is the often-overlooked clause.* Each Apache-2.0 contributor
  grants you a **royalty-free patent license** to their contributions. The grant
  **terminates** for anyone who initiates patent litigation alleging the work infringes a
  patent. This is a *feature* for build123d: pulling in manifold3d (Apache-2.0) means you
  get Google/Emmett Lalish's patent grant for Manifold's algorithms (the manifold mesh
  Boolean is novel work — the explicit patent grant is genuinely valuable here). Apache-2.0
  is one-way compatible *into* GPLv3 but not GPLv2 — irrelevant here since nothing is GPL.

**Net:** combining build123d (Apache) with manifold3d (Apache) and scad2py (Apache, once
licensed) is a **pure Apache-2.0 combination** — the simplest possible case. You owe a
`LICENSE` + `NOTICE` and modified-file notices. Nothing more.

---

## 5. License compatibility matrix

Direction matters. "A → B" below means *"can a project under license A incorporate / link
code under license B and still ship under A?"*

| Your project (A) | Incorporating code under (B) | Combinable? | Notes |
|---|---|---|---|
| Apache-2.0 | Apache-2.0 | **Yes** | Trivial. build123d + manifold3d + scad2py. |
| Apache-2.0 | MIT / BSD-2 / BSD-3 / Boost-1.0 / Unlicense | **Yes** | Permissive-into-permissive. Keep attributions. (trimesh, ply, shapely, rtree, lib3mf, ezdxf, Clipper2, quickhull.) |
| Apache-2.0 | **LGPL-2.1** (as a *dynamically linked* separate lib) | **Yes** | LGPL §5/§6: a "work that uses the Library." Your code stays Apache. This is exactly build123d → OCCT. |
| Apache-2.0 | **LGPL-2.1 statically linked** | **Yes, with conditions** | Bare LGPL §6 needs relink ability (ship your `.o` files or dynamic-link). **OCCT's exception removes the header trap**, making this clean. |
| Apache-2.0 | OCCT (LGPL-2.1 **+ exception 1.0**) | **Yes** | Easiest LGPL case — exception explicitly permits "terms of your choice" for object code that inlines OCCT headers, with a notice. |
| Apache-2.0 | **GPL-2.0 / GPL-3.0** | **No** (one-way) | GPL is strong copyleft: a *combined/derivative* work must ship under GPL. You cannot keep it Apache. **Not triggered here** — see §6. |
| GPL-2.0+ project | Apache-2.0 code | GPL-2.0: contested; GPL-3.0: yes | One-way: GPL can absorb Apache (GPLv3), not vice-versa. Not relevant to this stack. |
| Apache-2.0 | "Research-only" / "non-commercial" license | **No** | Would poison an OSS project. **Checked: CoACD is MIT, not research-only — no landmine.** |

**Key asymmetries to internalize:**
- Permissive (Apache/MIT/BSD/Boost) → flows freely into anything.
- LGPL → flows into your permissive project **as a separable library**; your code stays
  permissive; the LGPL'd library stays LGPL.
- GPL → strong copyleft; would force the *whole combined work* to GPL. The only way to be
  "GPL-clean" is to **not create a combined/derivative work with GPL code**.

---

## 6. The GPL contamination question — does OpenSCAD's GPL reach scad2py?

This is the subtlest question in the brief, so it gets its own section.

### 6.1 The facts

- **OpenSCAD** (the application) is **GPL-2.0-or-later**.
- scad2py is an **OpenSCAD-language transpiler**: it parses `.scad` source and emits Python.
- scad2py does **not** `import`, link, embed, fork, subprocess, or copy any OpenSCAD code.
- The provenance comments at the top of scad2py's parser files
  (`/Users/ochafik/github/scad2py/scad2py/scadast.py` line 1-2, and identically in
  `scad2py/parser/__init__.py` and `scad2py/customizer/parser.py`) read:
  ```
  # https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/OpenSCAD/importCSG.py#L192
  # https://github.com/openscad/openscad/blob/master/src/core/parser.y
  ```
  i.e. the grammar/AST design was **informed by reading** OpenSCAD's `parser.y` (a Bison
  grammar) and FreeCAD's `importCSG.py`. The actual scad2py parser is a **fresh
  re-implementation** using PLY (Python Lex-Yacc) — its own `tokrules.py`, its own
  precedence table, its own grammar rules in Python.

### 6.2 Does transpiling OpenSCAD source create a GPL obligation? — No.

Two independent reasons:

**(a) A transpiler/compiler is not a derivative work of the programs it processes.**
This is the same well-settled principle that lets GCC (GPL) compile proprietary programs,
or lets a proprietary compiler compile GPL code: *the tool and the input are separate
works.* The GPL FAQ itself states that the output of a program is not covered by the
program's license merely because the program produced it. By the same token, scad2py
processing a `.scad` file does not make scad2py a derivative of OpenSCAD, and does not make
the *user's* `.scad` files derivatives of scad2py. The GPL travels with *OpenSCAD's code*,
not with *the OpenSCAD language*.

**(b) A re-implemented grammar / language interface is not a derivative work of the
reference implementation.** A programming-language grammar is, functionally, an **API / a
specification of an interface**. Re-implementing it from scratch — even after *reading* the
reference grammar to understand the language — is the textbook "clean-ish reimplementation"
scenario. The closest authority is **Google LLC v. Oracle America, Inc.** (U.S. Supreme
Court, 2021): Google's reimplementation of the Java SE API declarations was held to be
**fair use**, and the Court's reasoning treated reimplementing an interface to achieve
interoperability as strongly favored. A Bison `parser.y` is even *less* protectable than
Java's API surface in important respects: grammar productions are heavily dictated by the
language being described ("merger doctrine" — when there are few ways to express an idea,
the expression is not protected), and scad2py expresses them in a *different formalism*
(PLY/Python, not Bison/C++ with OpenSCAD's semantic actions).

**Caveats / where care is warranted:**
- The conclusion holds **only if scad2py's parser is genuinely a re-implementation, not a
  line-by-line copy** of `parser.y` with the C++ stripped out. The inspected files
  (`scad2py/parser/__init__.py`, `tokrules.py`) are written in idiomatic PLY/Python with
  their own structure — consistent with re-implementation. Keep it that way; do not
  copy-paste OpenSCAD's Bison semantic-action C++ or comments verbatim.
- `FreeCAD/.../importCSG.py` — **FreeCAD is LGPL-2.1+** (not GPL). importCSG.py is a CSG
  *importer*. Same analysis: scad2py was *informed by reading* it, not copied from it. If
  any non-trivial block were ever lifted verbatim from importCSG.py, that block would be
  LGPL and would need attribution — but LGPL is still compatible with an Apache-2.0
  project (§5). Worth a quick self-audit, but it is not a GPL problem.
- **Best practice:** rephrase the provenance comments so they cannot be misread as "this
  file is copied from those files." Something like:
  `# Grammar re-implemented in PLY; OpenSCAD's parser.y and FreeCAD's importCSG.py were`
  `# consulted as references for the OpenSCAD language. No code copied from either.`

### 6.3 Does scad2py depend on any GPL code transitively (via PyPI dependencies)? — No.

Walking scad2py's `requirements.txt`:
`build`, `gradio` (Apache-2.0), `lxml` (BSD-3), `manifold3d` (Apache-2.0),
`mypy` (MIT), `numpy/scipy` (BSD), `ply` (BSD), `pyrender` (MIT), `rtree` (MIT),
`shapely` (BSD-3), `pyodide-build` (MPL-2.0 — weak copyleft, file-level, **not** a problem
for a dependency), `svg.path` (MIT), `typeguard` (MIT), the **trimesh fork** (MIT),
and the *commented-out optional* `coacd` (**MIT**, confirmed) / `vhacdx`.

**No GPL anywhere in scad2py's external dependency graph.** *However*, "no GPL via
dependencies" is **not** the same as "no GPL in the scad2py tree" — see §6.4, which is the
real landmine.

> One real risk worth flagging in the dependency list: scad2py depends on a **personal
> fork of trimesh** (`git+https://github.com/ochafik/trimesh.git@ochafik-svg-io-color`).
> trimesh is MIT, so the fork is fine *license-wise*, but a `git+https` dependency on a
> personal branch is a **supply-chain / maintenance** liability if scad2py is merged into
> build123d. build123d should not ship a hard dependency on an unreleased personal fork —
> either upstream the SVG-color patch to trimesh, publish the fork to PyPI, or vendor the
> small patch. This is an engineering concern, not strictly a licensing one, but it would
> block a clean build123d release.

### 6.4 GPL code that IS inside the scad2py tree — `calc.py` and `colors.py` (LANDMINE)

The owner has stated that all scad2py code is his own copyright *"unless stated
otherwise."* A scan of the scad2py source for copyright/license headers surfaces exactly
the *"otherwise"* case, and it is significant.

**Two files carry explicit third-party copyright + GPL headers:**

- `/Users/ochafik/github/scad2py/scad2py/calc.py` (186 lines)
- `/Users/ochafik/github/scad2py/scad2py/colors.py` (231 lines)

Both begin with the verbatim OpenSCAD GPL header:

```
#  OpenSCAD (www.openscad.org)
#  Copyright (C) 2009-2011 Clifford Wolf <clifford@clifford.at> and
#                          Marius Kintel <marius@kintel.net>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#  ...
```

and their bodies are **explicitly labelled line-by-line ports** of OpenSCAD C++ source —
the in-file comments cite the exact upstream files and line numbers, e.g.:

- `calc.py` → ports of `openscad/src/utils/calc.cc`, `src/core/TransformNode.cc`,
  `src/utils/degree_trig.cc`.
- `colors.py` → ports of `openscad/src/core/ColorNode.cc`, `src/glview/ColorMap.cc`.

**Why this matters — these are NOT covered by the owner's "I can relicense it" statement.**
The copyright in `calc.py`/`colors.py` belongs to **Clifford Wolf and Marius Kintel**, not
to ochafik. A "port" / translation of source code from C++ to Python is, under copyright
law, a **derivative work** — translation is one of the enumerated exclusive rights of a
copyright holder. So these two files are **derivative works of GPL-2.0-or-later code**, and
**they are themselves GPL-2.0-or-later**. The owner cannot relicense them; only Clifford
Wolf / Marius Kintel could.

This is materially different from the §6.2 conclusion. §6.2 says scad2py's *grammar/parser
architecture* is a clean re-implementation that does not create a GPL obligation — that
remains true and was verified. But `calc.py` and `colors.py` are **not** re-implementations
of an interface; by their own headers and comments they are *direct translations of
OpenSCAD's implementation code*. That is exactly the kind of copying that the GPL reaches.

**They are not dead code.** A reverse-dependency scan shows `scad2py.calc` and
`scad2py.colors` are imported by core runtime modules — `scad2py/csg.py`, `scad2py/io.py`,
`scad2py/runtime/modules.py`, `scad2py/rendering/rendering.py`,
`scad2py/rendering/manifold_renderer.py`, `scad2py/rendering/modifiers_rendering.py`.
`get_fragments_from_r` (the `$fn/$fs/$fa` circle-tessellation function) and the OpenSCAD
color tables / `parse_color` are on the **hot path** of producing geometry. So scad2py *as
it stands today* is a combined work that includes GPL-2.0+ code.

**Consequence for the build123d merge.** You **cannot** drop `calc.py`/`colors.py` into an
Apache-2.0 build123d (or even ship scad2py-as-Apache-2.0) while those files remain GPL
ports. The Apache-2.0 ↔ GPL-2.0 incompatibility (§5) bites here. Two clean fixes:

1. **Re-implement both files from the specification, not the source.** The functionality
   is small and almost entirely *mathematical / tabular*: `get_fragments_from_r` is a
   documented formula (`$fn`, `$fs`, `$fa` → fragment count); the OpenSCAD color tables are
   essentially the **CSS/X11 named-color list** (those RGB values are facts — colour
   *names mapped to RGB triples are not copyrightable expression*) plus a small number of
   OpenSCAD-specific scheme colours. Rewrite both from the OpenSCAD *documentation* / the
   CSS colour spec, by someone who has **not** copied the `.cc` files, and place the
   rewrite under Apache-2.0 with the owner's own copyright. Then the GPL headers come off
   legitimately. (The math formula itself is not copyrightable; only OpenSCAD's particular
   *expression* of it is — a fresh expression is fine.)
2. **Or** keep them GPL and *exclude* them from anything Apache-2.0 — not viable if scad2py
   is to be merged into build123d, since they are on the hot path.

Option 1 is the right answer and is a few hours of work. Until it is done, **scad2py is a
GPL-2.0-or-later combined work**, regardless of the owner's intent to license it Apache-2.0.

> Note: the OpenSCAD GPL header in these files includes the historical *"CGAL linking
> exception"* (permission to link with CGAL). That exception only widens what GPL'd
> OpenSCAD may *link*; it does **not** turn the file permissive and does **not** help an
> Apache-2.0 consumer. Ignore it for this analysis.

---

## 7. Goal 1 in detail — shipping a manifold3d feature in build123d

- **Licenses:** build123d Apache-2.0, manifold3d Apache-2.0. Manifold bundles **Clipper2
  (Boost-1.0)** and **quickhull (public domain)**, both permissive and Apache-compatible.
- **Linking model:** `manifold3d` is a normal PyPI wheel with a self-contained compiled
  extension. build123d would `import manifold3d` — an ordinary runtime dependency, no
  static linking of build123d's code into Manifold or vice versa.
- **Verdict:** **Fully clear.** Whether the feature is upstreamed into build123d proper or
  shipped as a separate sibling package (e.g. `build123d-manifold`), the licensing is
  identical and trivial — pure Apache-2.0.
- **Obligations:** add `manifold3d` (and, in the NOTICE file, Manifold + Clipper2 +
  quickhull attributions) to build123d's dependency list and NOTICE. Apache §4(d) means if
  Manifold ships a `NOTICE` file, its attributions must be carried forward.
- **Patent note:** Apache §3 gives build123d an explicit patent grant covering Manifold's
  novel mesh-Boolean algorithm. This is a positive — a reason to *prefer* manifold3d over a
  hypothetical non-Apache mesh library.

---

## 8. Goal 2 in detail — bringing scad2py into build123d

- **Copyright ownership / relicensing.** The owner (olivier.chafik@gmail.com) has confirmed
  he is the **sole copyright holder of all original scad2py code**. That is the decisive
  fact for the *owned* code: with no third-party contributors, there are **no other
  copyrights to reconcile**, so he can place his original code under **any license he
  chooses** — Apache-2.0, MIT, a dual license, or contribute it *directly into build123d*
  under Apache-2.0 — simply by deciding to. There is **zero relicensing friction** and no
  CLA/permission round-trip. See the dedicated subsection §8.1 below.
- **The missing `LICENSE` file is a formality, not a legal blocker.** scad2py's repo has no
  `LICENSE` file; `setup.py` only contains a *commented-out* block referencing the Apache
  URL, and the active `setup()` call passes no `license=`. This should be fixed (add the
  Apache-2.0 text + SPDX `# SPDX-License-Identifier: Apache-2.0` headers), but because the
  owner holds all rights to the original code, doing so is a one-line decision he is fully
  entitled to make — not a blocked relicensing.
- **The real blocker is NOT the owned code — it is `calc.py` / `colors.py` (§6.4).** These
  two files are *"stated otherwise"*: they carry GPL-2.0+ headers and are line-by-line
  ports of OpenSCAD C++ by Clifford Wolf / Marius Kintel. They are **not the owner's
  copyright** and **cannot be relicensed by him**. They are on the geometry hot path
  (imported by `csg.py`, `io.py`, `runtime/modules.py`, the renderers). **They must be
  re-implemented from spec (or removed) before scad2py merges into Apache-2.0 build123d.**
  This is the one genuine GPL issue and it is the gating action item for Goal 2.
- **Transpiler architecture ↔ GPL:** covered in §6.2 — the grammar/parser re-implementation
  does not create a GPL obligation. That conclusion is unchanged and is *separate from* the
  `calc.py`/`colors.py` problem (which is copied implementation code, not a re-implemented
  interface).
- **Third-party dependencies are unaffected by the owner's scad2py copyright.** The owner
  owning scad2py says nothing about `ply`, the trimesh fork, `manifold3d`, `CoACD`,
  `shapely`, etc. — those remain governed by *their own* licenses (all permissive: BSD /
  MIT / Apache-2.0 / Boost — see §2, §6.3). No relicensing of a dependency is implied or
  possible. Two dependency clean-ups before a build123d merge:
  1. The **trimesh personal-fork** `git+https` dependency (§6.3 risk box) — replace with a
     released package or vendored patch.
  2. **CoACD** is only an *optional* convex-decomposition dependency used by
     `scad2py/minkowski_impl.py` (and it is **commented out** in `requirements.txt`). It is
     MIT, so even if enabled it is fine. Keep it optional (`extras_require`) so a core
     build123d install does not pull a heavyweight native package.
- **Example-file provenance — the one genuine caution.** scad2py ships ~120 files in
  `examples/`. Inspection shows:
  - `examples/bosl2_math.scad` and `examples/bosl2_utility.scad` contain BOSL2-style
    documentation comments (`// Constant: PHI`, `// Synopsis:`, `// Topics:`,
    `// See Also:`) — these are **derived from BOSL2** (BelfrySCAD/BOSL2), which is
    **BSD-2-Clause**. README.md also links directly to a BOSL2 GitHub source file.
  - `examples/a11y.scad` carries a Reddit-thread URL in its header — third-party authored,
    license unstated.
  - Others (`CSG.scad`, `box.scad`, etc.) look like generic/original CSG demos.
  - **Verdict:** BSD-2-Clause is Apache-compatible, so shipping BOSL2-derived examples is
    *permitted* — but BSD-2 requires the **copyright notice + license text be retained**.
    The current `bosl2_*.scad` files do **not** carry a BOSL2 copyright header.
    **Action item before shipping these in build123d:** either (a) add proper BOSL2
    attribution + the BSD-2-Clause notice to those files and credit them in NOTICE, or
    (b) drop the BOSL2-derived examples and replace them with originals, and (c) confirm
    provenance/licensing of any other third-party-sourced `.scad` files (e.g. `a11y.scad`).
    This is a low-severity issue (test fixtures, permissive upstream) but it is a real
    attribution obligation and the kind of thing a build123d code review would flag.

### 8.1 Relicensing scad2py — explicitly unobstructed for the owner's own code

Because the owner is the **sole author and copyright holder of all original scad2py code**,
the relicensing question for that code is genuinely trivial, and worth stating plainly:

- **A copyright holder may license his own work however he likes, as many times as he
  likes.** Putting code on GitHub without a `LICENSE` file does not surrender any rights;
  it just means no license has been *granted to others* yet. The owner can grant one at any
  time. The earlier draft of this document framed the missing `LICENSE` file as scad2py
  being *"all rights reserved"* — that is technically true *for third parties*, but it is
  **not a relicensing obstacle for the owner himself**. He simply decides.
- **No CLA, no contributor sign-off, no permission round-trip** is needed for the original
  scad2py code, because there are no other contributors whose copyright would have to be
  collected. (If scad2py *had* outside contributors, each contributor's copyright in their
  contribution would normally need their agreement to relicense — that situation does not
  apply here.)
- **Merging scad2py's original source into build123d under Apache-2.0 is therefore
  unobstructed from a copyright-ownership standpoint.** The owner can: (a) add an Apache-2.0
  `LICENSE` to scad2py and depend on it; (b) dual-license scad2py; or (c) copy scad2py's
  source files directly into the build123d tree under Apache-2.0 with build123d's headers.
  Any of these is his to choose.
- **This subsection covers ONLY the owner's own code.** It does **not** extend to:
  - **`calc.py` / `colors.py`** — GPL-2.0+, copyright Clifford Wolf / Marius Kintel. The
    owner cannot relicense these; they must be re-implemented from spec or removed (§6.4).
  - **The BOSL2-derived `examples/bosl2_*.scad`** — third-party BSD-2-Clause; the owner
    cannot relicense them, only attribute them (§8 example-provenance bullet).
  - **Third-party PyPI dependencies** (`ply`, trimesh fork, `manifold3d`, `CoACD`, etc.) —
    governed by their own licenses; unaffected by the owner's copyright in scad2py.
  - Any other file flagged by the *"unless stated otherwise"* self-audit (action item
    9.2.8). The scan run for this document found `calc.py` and `colors.py` as the only
    files with a non-ochafik copyright header; a fuller audit before merge is still
    advisable to catch any vendored snippet without a header.

**Bottom line for §8.1:** the owner's own scad2py code → Apache-2.0 → into build123d is a
free, friction-free decision. The only things standing between scad2py and a clean
Apache-2.0 merge are the *non-owned* artifacts (the two GPL files and the BOSL2 examples),
not any limitation on the owner's authority over his own work.

---

## 9. Concrete obligations checklist — what to actually do

### 9.1 What the owner CAN do (green light)

- **Ship a manifold3d-based mesh feature in build123d** — upstreamed or as a sibling
  package. Pure Apache-2.0. No restrictions.
- **License his own scad2py code however he wants** — Apache-2.0, MIT, dual, or copied
  straight into build123d under Apache-2.0. As sole copyright holder of the original code
  there is **zero relicensing friction** (§8.1). Adding the missing `LICENSE` file is a
  formality fully within his authority, not a blocker.
- **Merge scad2py into build123d**, or ship it as a sibling package — *after* the two
  non-owned GPL files `calc.py`/`colors.py` are re-implemented from spec or removed (§6.4),
  and BOSL2 example attribution is fixed. The transpiler *architecture* itself is GPL-clean.
- **Distribute build123d wheels** that depend (via `cadquery-ocp-novtk`) on LGPL'd OCCT —
  because OCCT arrives as a *separate, dynamically linked* wheel and build123d is open
  source. LGPL §5 "work that uses the Library" + the OCCT exception cover this.
- **Keep build123d (and the new code) under Apache-2.0** — the LGPL dependency does **not**
  force build123d to LGPL; only OCCT itself stays LGPL.
- **Statically link OCCT** (if you ever ship a bundled binary) and still keep your own code
  Apache — the OCCT Exception 1.0 explicitly removes the LGPL header trap, requiring only a
  prominent notice.
- **Use CoACD** as an optional dependency — it is MIT.

### 9.2 What the owner must be careful about / must do first (yellow / action items)

1. **Add a `LICENSE` file to scad2py** (full Apache-2.0 text) and set `license` in its
   packaging metadata, plus SPDX `# SPDX-License-Identifier: Apache-2.0` headers. This is a
   *formality* — as sole copyright holder of the original code the owner is fully entitled
   to do this unilaterally (§8.1) — but it must actually be done so downstream users have a
   grant and the build123d merge is clean.
2. **GATING: re-implement or remove `scad2py/calc.py` and `scad2py/colors.py`.** These are
   GPL-2.0+ ports of OpenSCAD C++ (copyright Wolf / Kintel — *not* the owner's, despite the
   owner owning the rest of scad2py) and they are on the geometry hot path (§6.4).
   Re-implement `get_fragments_from_r` from the documented `$fn/$fs/$fa` formula and rebuild
   the colour tables from the CSS/X11 named-colour spec — by someone who has not read the
   `.cc` files — then place the rewrites under Apache-2.0. Until this is done, scad2py is a
   GPL-2.0+ combined work and **cannot** legally enter Apache-2.0 build123d.
3. **Add/refresh a `NOTICE` file in build123d** that attributes, at minimum:
   - OpenCASCADE Technology — LGPL-2.1 + Open CASCADE Exception 1.0 (with a pointer to the
     LGPL text and the exception text, and a statement that build123d "makes use of /
     is based on facilities provided by the Open CASCADE Technology software" — that exact
     kind of wording is what the OCCT exception's *"prominent notice"* condition asks for).
   - Manifold (Apache-2.0) and its bundled Clipper2 (Boost-1.0) and quickhull, once the
     manifold3d feature lands.
   - trimesh (MIT), ply (BSD), shapely (BSD), rtree (MIT), pyrender (MIT), gradio
     (Apache-2.0) once scad2py lands.
   - lib3mf (BSD-2-Clause), ezdxf (MIT), ocpsvg/ocp_gordon (Apache-2.0) — existing deps.
4. **OCCT / LGPL §6 redistribution obligation.** If build123d ever distributes a wheel that
   *itself bundles* the OCCT `libTK*` binaries (today it does not — it depends on the
   separate `cadquery-ocp` wheel), then build123d must also: ship the LGPL-2.1 text, ship
   the OCCT notice, and provide a **written offer / link to the OCCT corresponding source**
   so users can rebuild/relink a modified OCCT. As long as build123d only *depends on*
   `cadquery-ocp-novtk` (separate package), that §6 burden sits with the `cadquery-ocp`
   maintainers — but build123d should still name OCCT in NOTICE and link to OCCT's source
   repo as a courtesy and to satisfy the exception's "prominent notice" clause.
5. **Example-file provenance.** Add BOSL2 (BSD-2-Clause) attribution to `bosl2_*.scad`, or
   remove them; audit `a11y.scad` and other third-party `.scad` examples for license.
   These are third-party files — the owner's copyright over scad2py does not extend to them
   and cannot relicense them; he can only attribute (or drop) them.
6. **trimesh personal fork.** Do not ship a build123d release with a `git+https` dependency
   on `ochafik/trimesh@ochafik-svg-io-color`. Upstream the patch, publish the fork, or
   vendor it. (Engineering issue with licensing-adjacent packaging impact.)
7. **Provenance comments in scad2py parser files** — reword so they unambiguously say
   "re-implemented; reference consulted; no code copied," to pre-empt any
   derivative-work misreading.
8. **"Unless stated otherwise" self-audit.** The owner stated all scad2py code is his
   *except where headers state otherwise*. The scan run for this document found exactly two
   such files — `calc.py` and `colors.py` (item 2 above) — by their explicit GPL headers.
   Before merge, run a fuller audit: grep every source file for non-ochafik copyright lines
   *and* check for vendored third-party snippets that may lack a header (e.g. an algorithm
   copy-pasted without attribution). Also confirm no block of scad2py's parser was lifted
   verbatim from OpenSCAD's `parser.y` (GPL) or FreeCAD's `importCSG.py` (LGPL); the
   inspected parser files look like genuine PLY re-implementations, but a deliberate check
   is cheap insurance.

### 9.3 What the owner CANNOT do (red light)

- **Cannot relicense OCCT** or strip its LGPL — OCCT stays LGPL-2.1 + exception forever;
  build123d builds *on top of* it, it does not own it. (Not a practical limitation for an
  OSS project, just a hard fact.)
- **Cannot drop the OCCT "prominent notice"** — the OCCT exception's *only* condition is
  that notice; omitting it forfeits the exception and drops you back to bare LGPL-2.1 §6.
- **Cannot ship `scad2py/calc.py` or `scad2py/colors.py` as-is inside Apache-2.0
  build123d** — they are GPL-2.0+ ports of OpenSCAD C++, copyright Wolf / Kintel. The owner
  does **not** hold copyright in them and **cannot relicense them**; they must be
  re-implemented from spec or removed (§6.4, action item 9.2.2). This is the one hard GPL
  blocker and it is *separate from* the owner's freedom to relicense his own code.
- **Cannot relicense any third-party code** he did not write — the BOSL2 `.scad` examples
  (BSD-2-Clause), the `calc.py`/`colors.py` GPL ports, or any PyPI dependency. Owning the
  rest of scad2py grants no authority over code authored by others.
- **Cannot copy GPL'd OpenSCAD *implementation* source** (translating C++ to Python counts
  as copying — that is exactly how `calc.py`/`colors.py` became GPL). Re-implementing the
  *grammar/interface* from a reading of `parser.y` is fine (§6.2); translating OpenSCAD's
  *implementation code* is not.
- **Cannot ship the BOSL2-derived `.scad` examples without BSD-2-Clause attribution** —
  permissive does not mean attribution-free.
- **Cannot merge scad2py into build123d while `calc.py`/`colors.py` remain GPL** — fix
  action item 9.2.2 first. (The missing `LICENSE` file, by contrast, is the owner's to add
  at will and is not itself a blocker — §8.1.)

---

## 10. Why the Apache ↔ LGPL combination is fine — the precise reasoning

The brief asks specifically whether build123d (Apache-2.0) depending on OCCT
(LGPL-2.1 + exception) is acceptable. It is, for these stacked reasons:

1. **build123d never links OCCT statically.** It `import OCP` at runtime; OCP's compiled
   extension dynamically loads the OCCT `libTK*` shared libraries. Under LGPL §6, dynamic
   linking is the canonical compliant arrangement — the end user can replace the OCCT
   `.dylib`/`.so`/`.dll` and the program will pick up the new one.
2. **build123d is "a work that uses the Library" (LGPL §5).** It contains no derivative of
   OCCT's source; it only calls OCCT's published interface through OCP. §5 explicitly says
   such a work "is not a derivative work of the Library" and "falls outside the scope of
   this License."
3. **The OCCT Exception 1.0 closes the only remaining gap.** The one way bare LGPL-2.1
   could have reached into build123d's binaries is §6's header-inlining clause (OCCT
   headers contain substantial inline/template code). The exception explicitly permits
   distributing object code that incorporates OCCT header material "under terms of your
   choice" — so build123d's compiled artifacts (and OCP's) can be Apache-2.0.
4. **Apache-2.0 imposes nothing incompatible.** Apache-2.0 only demands notices and a
   patent peace clause. LGPL-2.1 demands notice + relink ability for the library. These do
   not conflict; both can be satisfied simultaneously. (Note: Apache-2.0 and *GPLv2* are
   FSF-considered-incompatible, but **LGPL-2.1 used as a separable library is a different
   case** — you are not merging Apache and LGPL into one combined source work; you are
   linking a separable LGPL library, which both licenses contemplate.)
5. **The new code changes nothing — once cleaned up.** Adding manifold3d (Apache) keeps it
   Apache-on-Apache. Adding scad2py's *original* code (Apache, once licensed by its sole
   owner) keeps it Apache-on-Apache. The **one exception** is scad2py's `calc.py`/`colors.py`,
   which are GPL ports of OpenSCAD *implementation* code (§6.4) — those must be re-implemented
   from spec before scad2py merges. Neither manifold3d nor the cleaned scad2py introduces a
   static link to OCCT.

**Conclusion:** the current build123d → OCCT relationship is compliant, and both project
goals can preserve that compliance. The manifold3d feature is pure housekeeping. For
scad2py, the owner's sole copyright over his original code means relicensing it to
Apache-2.0 is friction-free — the real work is *(a)* re-implementing the two GPL-headed
files `calc.py`/`colors.py` from spec, *(b)* attributing or dropping the BOSL2 examples,
*(c)* adding LICENSE/NOTICE files, and *(d)* replacing the trimesh `git+https` pin. Of
those, only *(a)* is a true legal blocker; the rest is hygiene.

---

## 11. Sources

- build123d LICENSE — `/Users/ochafik/github/build123d/LICENSE` (Apache-2.0, full text)
- build123d deps — `/Users/ochafik/github/build123d/pyproject.toml`
- manifold3d 2.3.1 — `…/miniforge/base/lib/python3.10/site-packages/manifold3d-2.3.1.dist-info/{METADATA,licenses/LICENSE,licenses/AUTHORS}`
- cadquery-ocp-novtk wheel — `/tmp/pipcheck/cadquery_ocp_novtk-7.9.3.1-cp310-cp310-macosx_11_0_arm64.whl` (`.dist-info/METADATA`, `OCP/.dylibs/libTK*.dylib`)
- scad2py — `/Users/ochafik/github/scad2py/{setup.py,requirements.txt,README.md}`, `scad2py/scadast.py`, `scad2py/parser/__init__.py`, `scad2py/parser/tokrules.py`, `scad2py/minkowski_impl.py`, `examples/bosl2_*.scad`
- scad2py GPL-headed files — `/Users/ochafik/github/scad2py/scad2py/calc.py` and `…/scad2py/colors.py` (verbatim OpenSCAD GPL-2.0+ header, copyright Clifford Wolf / Marius Kintel; in-file comments cite the upstream `.cc` source files they were ported from); imported by `scad2py/{csg.py,io.py,runtime/modules.py,rendering/rendering.py,rendering/manifold_renderer.py,rendering/modifiers_rendering.py}`
- [Open CASCADE Exception 1.0 — SPDX](https://spdx.org/licenses/OCCT-exception-1.0.html)
- [Licensing — Open CASCADE Technology](https://dev.opencascade.org/resources/licensing)
- [OCCT_LGPL_EXCEPTION.txt — OCCT GitHub repo](https://github.com/Open-Cascade-SAS/OCCT/blob/master/OCCT_LGPL_EXCEPTION.txt)
- [OCCT relicensed to LGPL — Open CASCADE announcement](https://dev.opencascade.org/content/open-cascade-technology-changes-its-license-gnu-lgpl)
- [CoACD LICENSE — github.com/SarahWeiii/CoACD](https://github.com/SarahWeiii/CoACD/blob/main/LICENSE) (MIT)
- GNU LGPL-2.1 text — distributed by OCCT as `LICENSE_LGPL_21.txt`
- *Google LLC v. Oracle America, Inc.*, 593 U.S. ___ (2021) — API reimplementation / fair use
- GNU GPL FAQ — output of a program is not covered by the program's license

---

*End of analysis. Reminder: this is an engineering review of license texts, not legal
advice. Have an attorney confirm anything that carries commercial risk — especially the
derivative-work reasoning in §6, which is the most interpretation-dependent part.*
