# 11 — WASM / pyodide packaging for build123d

**Research agent for ochafik · Date: 2026-05-22**
**Question:** Can build123d (and a build123d + manifold3d + scad2py stack) realistically
run in the browser via pyodide / WebAssembly — and what does WASM static linking do to
the OCCT / GPL license analysis?

> **Scope of this doc.** This is the *deployment / distribution / packaging* derisking
> for build123d-in-the-browser. It does **not** re-derive the license reasoning — that
> lives in `ddocs/research/06-license-analysis.md` (§3 OCCT, §6 GPL). It builds on it,
> applying it to the static-linking case that WASM forces.
>
> **Companion docs.**
> - `ddocs/research/06-license-analysis.md` — OCCT LGPL-2.1 + exception; the GPL landmine.
> - `ddocs/design/scad2py-gpl-remediation.md` — the clean-room rewrite that removes the
>   GPL blocker (prototype in `ddocs/prototypes/p6_clean_room_scad2py_math/`).
> - `ddocs/research/03-manifold3d-deep-dive.md` — manifold3d as a geometry backend.
> - The parallel CadQuery effort wrote `cadquery/ddocs/prototypes/p5-wasm-pyodide/README.md`
>   — CadQuery and build123d share the *identical* `cadquery-ocp` / `OCP` wrapper, so the
>   WASM-of-OCP analysis transfers almost verbatim. This doc re-verifies the build123d-
>   specific parts and does not duplicate the rest.
>
> **Not legal advice.** §4 is an engineering reading of license texts. Items marked
> "→ counsel" need a real lawyer before any distribution.

---

## 0. TL;DR verdict

| Path | Verdict | Why |
|---|---|---|
| **build123d (pure-Python layer) in pyodide** | **Works** | build123d is pure Python; `micropip.install("build123d")` installs the `.py`. The blocker is its native dep, `OCP`. |
| **`OCP` in pyodide (the native blocker)** | **Solved — and it exists *because of* build123d** | `OCP.wasm` (yeicor) is a published Emscripten port of OCCT 7.9.3 + the OCP pybind11 bindings, whose tagline is literally *"Build123d in the browser!"*. Wheel ≈ 22 MB compressed. |
| **Full build123d + OCP in browser** | **Works today** | `build123d-sandbox` and `yet-another-cad-viewer`'s playground already run build123d fully in-browser on OCP.wasm. Cold load tens of seconds / ~30–60 MB. |
| **manifold3d in WASM** | **Easy — manifold *is* a WASM library** | manifold compiles to wasm trivially; it powers OpenSCAD's web playground and three.js's CSG. No emscripten PyPI wheel, but a wheel is a small, well-trodden build (ochafik did manifold3d 2.4.5). |
| **build123d + manifold + scad2py browser stack** | **Feasible — ONE legal blocker** | All pieces are wasm-ready and lightweight on the manifold path. But scad2py's two GPL files (`calc.py`/`colors.py`) **must** be the clean-room rewrites before any `.wasm` bundle ships — WASM static linking removes every "separate process" escape hatch. |

**One-line recommendation.** Two browser stories: (a) a **light manifold-backed mesh
build** — fully permissive, ~1 MB geometry payload, the natural home for scad2py-in-browser;
(b) **full build123d on OCP.wasm** as the heavy "real B-Rep / STEP" mode, reusing the
*already-built* OCP.wasm wheel (do not rebuild OCCT yourself). Either way: GPL-clean
scad2py first.

---

## 1. Why WASM at all

Browser-based CAD removes the single biggest friction in the build123d funnel: the
install. build123d's native footprint is dominated by `cadquery-ocp-novtk` — a ~150 MB
class native extension plus ~70+ bundled OCCT `libTK*` shared libraries (see doc 06 §3.3
and the CadQuery companion's doc 02). For a new user that means a multi-hundred-MB
download, a conda/pip dance, and platform-specific wheels. A browser build replaces all
of that with a URL.

Three concrete reasons it matters *for this project specifically*:

1. **scad2py's customizer already targets pyodide.** This is not hypothetical. The
   scad2py repo carries **two** pyodide cross-build environments —
   `/Users/ochafik/github/scad2py/.pyodide-xbuildenv/` (pyodide 0.25.x era, CPython
   3.11.3) and `.pyodide-xbuildenv-0.26.0/` (pyodide 0.26.0, CPython 3.12.1) — and
   `pyodide-build` is a line in `/Users/ochafik/github/scad2py/requirements.txt`. The
   `build/` tree has two emscripten target dirs (`bdist.emscripten_3_1_46_wasm32/`,
   `bdist.emscripten_3_1_58_wasm32/`) — now empty, but their existence proves
   `pyodide build` was run against scad2py twice. The customizer (`scad2py/customizer/`,
   a gradio UI) is the user-facing surface ochafik wanted in the browser.

2. **build123d-in-the-browser is a *real, shipping* use case.** It is not a thought
   experiment. `build123d-sandbox` (jojain) and `yet-another-cad-viewer` (yeicor-3d)
   already run build123d fully client-side. A WASM build of build123d is the obvious
   delivery vehicle for tutorials, docs playgrounds, "share an editable parametric model"
   links, and the scad2py customizer.

3. **Static linking changes the OCCT license analysis.** Doc 06 reasoned about the
   *native* case, where OCCT arrives as ~70 separate swappable `.dylib`s — the canonical
   LGPL-§6-compliant arrangement. A pyodide build statically links *everything* into one
   `.wasm`. That is a different LGPL §6 fact pattern, and it is the *worst* case for the
   GPL contamination in scad2py. §4 below works this through.

---

## 2. The build123d stack, layer by layer, in pyodide

"Does build123d run in pyodide" decomposes into "does each layer run in pyodide". Status
of each, with `✅` = verified-available, `⚠️` = available with caveats, `🔨` = needs a
build but is tractable.

### 2.1 Python itself — ✅

Pyodide *is* CPython compiled to WebAssembly/Emscripten. The pyodide runtime is ~10 MB
download, ~4–5 s first load (cached thereafter). No issue.

### 2.2 numpy / scipy — ✅

Both are in the **official pyodide distribution** (shipped as part of pyodide's package
set, loadable via `pyodide.loadPackage` / `micropip`). build123d hard-depends on
`numpy >= 2` (`pyproject.toml`); pyodide ships numpy 2.x in recent releases. scipy is
also in-distribution. **Verified-class fact** — numpy/scipy have been pyodide staples for
years. scad2py's own `pyscript.json` lists `numpy`/`scipy` as packages and they resolve
from the pyodide CDN.

### 2.3 `OCP` — the native blocker — solved by **OCP.wasm**

This is the crux. build123d's `pyproject.toml` pins `cadquery-ocp-novtk >= 7.9, < 8.0`.
`OCP` is the *only* heavy native extension build123d needs; build123d's own package is
pure Python (every operation bottoms out in `import OCP`). So "build123d in pyodide" ≡
"OCP in pyodide" ≡ "OCCT 7.9.x compiled to wasm32 with the pybind11 layer relinked".

**That has been done, and it was done with build123d as the explicit target.**

- **`github.com/yeicor/OCP.wasm`** — verified from the repo: its tagline is literally
  **"Build123d in the browser!"** and the README states the project exists to bring
  "the full power of build123d" to the browser. So the "built specifically with build123d
  in mind" claim in the brief is **confirmed** — OCP.wasm is a build123d-motivated effort,
  not a generic OCP port. (CadQuery shares the *same* OCP wrapper, so CadQuery rides along
  for free, but build123d is the stated driver — and yeicor focused test-validation on
  build123d's test suite, not CadQuery's.)
- It builds **OCCT `V7_9_3`** from the Open-Cascade-SAS source — the *same* OCCT version
  bundled in the native `cadquery-ocp 7.9.3.1` wheel build123d uses. Same kernel, same
  B-Rep semantics. (Verified in the CadQuery p5 doc against the OCP.wasm build scripts;
  consistent with build123d's `>= 7.9, < 8.0` pin. **Uncertain:** whether OCP.wasm has
  since moved to a hypothetical OCCT 7.9.4/7.9.x point release — knowledge cutoff Jan
  2026; check the repo's pinned tag before relying on an exact patch version.)
- It builds the **real OCP pybind11 binding sources** (the same `OCP_src_stubs` artifact
  the native `cadquery-ocp` wheel is generated from). This is genuinely
  "`cadquery-ocp`, recompiled for wasm32" — not a reimplementation.
- **VTK is removed** — the package is `cadquery-ocp-novtk` (matching build123d's own
  dependency, which is *also* the `-novtk` variant — see `pyproject.toml`). Modeling,
  topology, data-exchange (STEP/glTF via rapidjson, text via freetype) are kept;
  visualization toolkits (`AIS`, `V3d`, `OpenGl`, `IVtk*`) are gone. This is a *good*
  match: build123d's exporters that build123d-sandbox exercises (STL/STEP/glTF/3MF) do
  not need VTK.
- OCCT is compiled **`BUILD_LIBRARY_TYPE="Static"`** — all `libTK*` toolkits baked into
  the single `.wasm` module. **This is the static-linking case §4 is about.**
- **Size (from the CadQuery p5 doc's PyPI measurement):** the current optimized wheel
  `cadquery-ocp-novtk-OCP.wasm` is **≈ 21.8 MB compressed**; a debug build is ~40 MB; an
  earlier 7.8.x build was ~22.9 MB. Wheel tag `cp313 ... pyemscripten_2025_0_wasm32`
  (Python 3.13, recent pyodide ABI). So the OCP WASM payload is **~22 MB**, far below the
  ~150 MB native `.so` — because VTK is stripped, wasm is denser than native arm64, and
  `wasm-opt` + dead-code elimination shrink it further.
- **OCP.wasm's own license is MIT.** That governs the *build scripts*, not OCCT — OCCT
  inside the `.wasm` is still LGPL-2.1 + exception (see §4.1).

**You do not build this yourself.** Cross-compiling OCCT to wasm is genuinely hard (LTO
disabled because it needs ~50 GB RAM; OCCT/OCP/pybind11/rapidjson patches; legacy-SjLj
exception handling; a `repair_wasm.py` post-pass — all documented in the CadQuery p5
doc). The good news: it is *already solved and published*. build123d consumes the
`cadquery-ocp-novtk` OCP.wasm wheel via `micropip`; the OCCT-to-wasm mountain is climbed.

> **`opencascade.js` is NOT a substitute.** `opencascade.js` ("ocjs") is a mature
> Emscripten port of the *same* OCCT, but it exposes a **JavaScript/TypeScript** API via
> Embind — not a Python `OCP` module. build123d does `import OCP` (pybind11). You cannot
> point that import at an ocjs `.wasm`. ocjs is only relevant as proof OCCT-to-wasm is
> mature, and as a build-flag reference. The Python-relevant artifact is OCP.wasm.

### 2.4 manifold3d as a wasm wheel — 🔨 (small, well-trodden)

manifold3d to WebAssembly is *not in question* (see §5 — manifold *is* a wasm library).
The Python *wheel* situation, precisely:

- **No `emscripten_*_wasm32` wheel for `manifold3d` on PyPI.** PyPI does not host wasm
  wheels at all (PEP 783 is the future plan; not yet generally available). Verified-class.
- **manifold3d is not in the official pyodide distribution** either.
- **But ochafik already built one.** scad2py's `index.html` references
  `manifold3d-2.4.5-py3-none-emscripten_3_1_58_wasm32.whl`, and
  `/Users/ochafik/github/scad2py/requirements.txt` carries a commented line
  `# git+https://github.com/ochafik/manifold.git@pyodide-build` — **ochafik forked
  manifold specifically to add a pyodide build**. So an emscripten manifold3d wheel was
  produced once (manifold 2.4.5 era; current upstream is 3.x). Rebuilding a current one
  is a small task: manifold is a compact C++ library whose only deps (Clipper2, oneTBB,
  nanobind, glm) are all permissive and wasm-friendly, and `pyodide-build` handles it.

### 2.5 The lighter Python deps

build123d's other `pyproject.toml` deps, status in pyodide:

| Dep | pyodide status | Notes |
|---|---|---|
| `numpy`, `scipy`, `scikit-learn` | ✅ in-distribution | scikit-learn ships in pyodide. |
| `sympy` | ✅ in-distribution | Pure Python — trivial. |
| `svgpathtools`, `anytree`, `trianglesolver` | ✅ pure Python | `micropip` from PyPI. |
| `typing_extensions` | ✅ pure Python | trivial. |
| `ipython` | ⚠️ pure-ish | works under pyodide; build123d only uses it lightly. |
| `webcolors` | ✅ pure Python | trivial. |
| `ezdxf` | ⚠️ pure Python core | ezdxf core is pure Python (MIT) → `micropip`-installable; its optional C-accelerated parts are not required. **Likely fine; verify the exact features build123d uses.** |
| `ocpsvg`, `ocp_gordon` | ⚠️ pure Python, but depend on `OCP` | Both are Apache-2.0 pure-Python build123d deps that themselves call into `OCP`. They install fine; they work only once OCP.wasm is loaded. |
| `lib3mf` / `py-lib3mf` | ⚠️ native | `lib3mf` is a native C++ library with Python bindings (BSD-2). **No known pyodide wheel** — knowledge-cutoff-uncertain. 3MF *export* would need either a lib3mf wasm build or a pure-Python 3MF writer fallback. build123d's STL/STEP/glTF exporters do not need it. **Likely the one missing-wheel gap** for full export parity. |
| `svgpathtools` / SVG I/O | ✅ | pure Python. |
| `ply` (scad2py's parser) | ✅ in-distribution / pure Python | PLY is pure Python, BSD; scad2py's `pyscript.json` lists it. |

**`scad2py`'s extra deps in pyodide:** `lxml` (in pyodide distribution), `shapely`
(in-distribution — native GEOS compiled for wasm by the pyodide team), `svg.path`
(pure Python), `typeguard` (pure Python), `pyrender` (⚠️ needs OpenGL — *not* browser-
friendly; scad2py should not rely on `pyrender` in the browser, use `model-viewer` /
glTF instead, as `index.html` already does), `trimesh` (pure Python core, MIT — but
scad2py pins a personal fork `ochafik/trimesh@ochafik-svg-io-color`; see doc 06 §6.3 —
that fork must be upstreamed/published before a clean browser build). `numba` is
commented out in scad2py's requirements — good, numba does **not** run in pyodide.

**`ply` note:** PLY (the OpenSCAD-grammar parser scad2py uses) writes a `parser.out` /
generated tables file at first run. In pyodide's in-memory FS this is fine but ephemeral;
prefer shipping pre-generated parser tables or `write_tables=False` to avoid a per-load
parser-generation cost.

---

## 3. build123d-specific WASM notes

**What build123d needs to run in pyodide:** essentially just (a) the pure-Python
`build123d` wheel via `micropip`, (b) the OCP.wasm `cadquery-ocp-novtk` wheel, (c) numpy
+ the light pure-Python deps. The hard part — OCP — is the published wheel.

**Known blockers / caveats (verified vs uncertain):**

- **`lib3mf` native dep — likely the one real gap.** 3MF export needs the `lib3mf`
  native library; no pyodide wheel is known (uncertain — re-check at build time). Mitigation:
  treat 3MF as an optional export, or add a pure-Python 3MF writer. STL/STEP/glTF do not
  need it. *(Verified: build123d declares `lib3mf` as a dependency in `pyproject.toml`;
  uncertain: whether a wasm build exists.)*
- **VTK-dependent features absent.** OCP.wasm is the `-novtk` variant — which is exactly
  what build123d's `pyproject.toml` already depends on, so this is *not a regression* for
  build123d. Any build123d code path that reaches VTK (some viewers) is unavailable in
  the browser; build123d's standard mesh/STEP/glTF exporters are fine.
- **`ocp_vscode` visualization** is an optional extra — irrelevant in-browser; use
  glTF + `<model-viewer>` (the pattern scad2py's `index.html` already uses) or
  `yet-another-cad-viewer`.
- **OCCT version match.** build123d pins `cadquery-ocp-novtk >= 7.9, < 8.0`; OCP.wasm
  builds OCCT 7.9.3 — inside the pin. Keep them aligned: if build123d bumps to OCCT 8.x,
  OCP.wasm must follow.
- **Threading.** OCCT/OCP in pyodide runs effectively single-threaded (pyodide threading
  via wasm threads is limited and needs cross-origin-isolation headers). Heavy Booleans
  will block the main thread — run pyodide in a Web Worker. This is a UX concern, not a
  correctness blocker.

**Bundle size & cold load (the honest numbers):**

| Component | Compressed payload |
|---|---|
| pyodide runtime | ~10 MB |
| numpy + scipy + light deps | ~10–15 MB |
| `OCP.wasm` (`cadquery-ocp-novtk`) | **~22 MB** |
| build123d pure-Python wheel | < 1 MB |
| **Full build123d + OCP total** | **~40–50 MB, tens of seconds cold load** |

This is **heavy but viable** — `build123d-sandbox` ships it today. OCP.wasm's README
explicitly warns "the initial load is a bit slow ... editing and re-running is faster"
(verified). Acceptable for a docs playground / "share an editable model" use case;
heavy for a casual one-off tool. Browser caching makes the *second* visit fast.

By contrast a **manifold-only** browser build (no OCP) is **~20–25 MB total** (pyodide +
numpy + ~1 MB manifold) — see §5/§6.

---

## 4. The license angle — WASM static-links everything

WASM changes the licensing calculus because **a pyodide/wasm build statically links
everything into one module** — there are no separate, swappable shared libraries. This
section *applies* doc 06 (do not re-derive it here); it covers the two distinct concerns.

### 4.1 OCCT — LGPL-2.1 + Open CASCADE Exception, now statically linked

doc 06 §3 establishes: OCCT is **LGPL-2.1 + the Open CASCADE Exception 1.0**; the
exception lets *header-derived* material in your object code be distributed "under terms
of your choice" given a prominent notice; and in the *native* `cadquery-ocp` wheel OCCT
arrives as ~70 separate `.dylib`s, so LGPL §6's relink-enablement obligation is satisfied
*structurally* (a user can swap a modified `libTK*`). doc 06's §10 even flagged that
"static linking is fine **with conditions**".

WASM makes the static-linking case **concrete and unavoidable**:

- OCP.wasm builds OCCT `BUILD_LIBRARY_TYPE="Static"` — every `libTK*` toolkit is fused
  into the single `OCP*.wasm`. There is no separate library to swap.
- The **Open CASCADE Exception still does its job**: your bundle does *not* become LGPL
  merely because OCCT headers (which contain substantial inline/template/CDL code) got
  inlined into the `.wasm`. build123d's own code and the OCP binding code stay
  Apache-2.0-licensable. The exception's only condition — a prominent notice that the
  software "makes use of or is based on facilities provided by Open CASCADE Technology"
  — must appear in the WASM bundle's docs/about page.
- **But** LGPL-2.1 §6's *relink-enablement* obligation for the statically-linked
  *compiled bodies* of OCCT is a separate clause the exception does not explicitly waive
  (doc 06 §10 point 3; CadQuery p5 §6.1). For the native wheel this is satisfied by the
  separate-`.dylib` structure. For a *statically linked* `.wasm`, the conservative
  reading is that whoever **distributes** the bundle should also make available: the
  exact-version OCCT **source** (7.9.3), a buildable project, and the prominent notice —
  so a user could in principle rebuild a modified OCCT and relink. **→ counsel.**
- OCP.wasm itself is MIT-licensed and (per the CadQuery p5 finding) does **not** currently
  ship the LGPL/exception texts or an OCCT source offer alongside its wheels — the same
  upstream compliance gap doc 06 §3.3 noted for the native `cadquery-ocp` wheel, now
  inherited by the WASM wheel. **Action if build123d redistributes a bundle containing
  OCP.wasm:** carry the LGPL-2.1 text + the Open CASCADE Exception text + an OCCT 7.9.3
  source offer/link + the prominent notice.

**Bottom line:** OCCT-in-WASM is *not* a copyleft blocker for build123d — the exception
holds and build123d stays Apache-2.0. It *does* raise a §6 relink/notice compliance task
that the native dynamically-linked case mostly hid. Treat it as a documentation/NOTICE
deliverable, get counsel sign-off, and do not strip the OCCT notice.

### 4.2 GPL contamination — the hard blocker for scad2py-in-WASM

doc 06 §6.4 and `ddocs/design/scad2py-gpl-remediation.md` establish: exactly two scad2py
files — `scad2py/calc.py` and `scad2py/colors.py` — are **GPL-2.0-or-later** (explicit
OpenSCAD GPL headers, `# Ported from <openscad C++ URL>` comments, copyright Clifford
Wolf / Marius Kintel — *not* ochafik's, hence not relicensable by him). They are on the
geometry hot path (`get_fragments_from_r`, `parse_color`, color tables — imported by
`csg.py`, `io.py`, `runtime/modules.py`, the renderers).

**WASM static linking is the *worst case* for GPL taint, and it removes every escape
hatch:**

- In a *native* deployment one might argue mitigations: scad2py invokes the OpenSCAD
  binary as a **subprocess** (the remediation doc §2 notes `manifold_renderer.py` shells
  out to `openscad` — a separate-process call, not linkage). Process separation is a
  recognized GPL boundary.
- In a **single `.wasm`** there is **no process boundary, no separate library, no
  dlopen** — scad2py's GPL Python, Apache build123d, Apache manifold, LGPL OCCT are *all*
  fused into one statically linked artifact. GPL-2.0's copyleft then reaches the **entire
  `.wasm`**: the whole bundle would have to be offered under GPL with complete
  corresponding source. **There is no separate-process escape hatch in WASM.**
- A GPL-headed `.py` file compiled into a pyodide bundle (even though it's "just Python",
  it ships *inside* the distributed `.wasm`/wheel artifact) **taints the whole bundle**.

**Therefore: scad2py's `calc.py` and `colors.py` MUST be replaced with the clean-room
rewrites before *any* WASM bundle is built or distributed.** This is a hard prerequisite,
not a nicety.

- The fix already exists: `ddocs/design/scad2py-gpl-remediation.md` + the prototype
  `ddocs/prototypes/p6_clean_room_scad2py_math/` — `get_fragments_from_r` rewritten from
  the documented `$fn/$fa/$fs` formula, the color tables rebuilt from the W3C CSS Color 4
  spec, `parse_color` rewritten from OpenSCAD's *documentation*. 3308 equivalence cases
  pass bit-exact. After ochafik swaps the clean files in (remediation checklist §"Code
  remediation"), scad2py is Apache-2.0-clean.
- **Note:** scad2py's *existing* `index.html` browser attempt already `micropip.install`s
  a `scad2py-0.0.0-py3-none-any.whl` — meaning a **GPL-contaminated wheel/`.wasm` was
  already produced locally**. Fine for ochafik's private experimentation; **not** fine to
  publish or distribute. Distribution is the trigger for GPL obligations.

### 4.3 Everything else in the WASM bundle is permissively licensed

manifold3d (Apache-2.0) + its bundled deps — Clipper2 (Boost-1.0), oneTBB (Apache),
nanobind (BSD), glm (MIT), quickhull (public domain) — are all permissive; **no copyleft
anywhere in the manifold stack** (doc 06 §2, §7). build123d is Apache-2.0. numpy/scipy/
shapely/ply/svg.path/typeguard are BSD/MIT family. So a **manifold-only WASM build, with
scad2py's GPL files remediated, has no licensing blocker at all** — it is a pure
permissive bundle. The OCP.wasm path adds only the §4.1 LGPL notice obligation.

---

## 5. manifold3d in WASM — a point *in favor* of a manifold backend

A key strategic finding: **manifold compiles to WebAssembly cleanly — manifold's primary
*web* identity already *is* a WASM build.** This makes a manifold geometry backend
arguably **more wasm-friendly than the heavy OCP.wasm path**.

- The npm package **`manifold-3d` *is* the WASM build** of the same Manifold C++ library
  the `manifold3d` Python wheel wraps. It is a first-class, maintained artifact — not an
  afterthought.
- It already **powers real web apps**: it is the default geometry backend of the official
  **OpenSCAD Web Playground** (`openscad/openscad-playground`), powers **ManifoldCAD**,
  and is used for CSG in **three.js** ecosystem tools. Manifold was *designed* to be
  portable and parallel; its dependency set (Clipper2 / oneTBB / nanobind / glm) is small
  and all permissive — exactly the profile that compiles to wasm without drama.
- manifold's own repo documents an `emcmake cmake` / `emmake` WASM build path.
- **Payload contrast:** a `manifold3d` emscripten *wheel* is **~0.5–1 MB** — roughly
  *two orders of magnitude* smaller than OCP.wasm's ~22 MB. A manifold-only browser build
  is ~20–25 MB total (dominated by pyodide+numpy); the OCP path is ~40–50 MB.
- **The only gap is packaging, not portability.** There is no emscripten `manifold3d`
  wheel on PyPI and none in the pyodide distribution — but ochafik *already built one*
  (the 2.4.5 wheel referenced in `scad2py/index.html`, via his
  `ochafik/manifold@pyodide-build` fork). Rebuilding for current manifold 3.x is a small,
  well-trodden job.

**Why this matters for build123d strategy.** If build123d is gaining a manifold3d-based
mesh path anyway (Goal 1 in doc 06; doc 03), then the *same* manifold backend is the
cheap, clean, fast browser story — no OCP.wasm, no 22 MB payload, no LGPL §6 relink
question. A manifold backend is not merely "also wasm-compatible"; it is *materially
better* for the browser than the OCP path. This is a genuine point in favor of investing
in the manifold backend.

The caveat: manifold is a **mesh CSG** kernel — no exact B-Rep, no parametric fillets on
curved surfaces, no STEP. The manifold path gives you OpenSCAD-style mesh modeling
(which is exactly what scad2py needs) and fast viewing; it does *not* replace OCCT for
serious parametric B-Rep CAD. That is the §6 trade-off.

---

## 6. Recommendations

### 6.1 Two browser stories, deliberately

| | Manifold-backed WASM build | Full build123d + OCP.wasm |
|---|---|---|
| Geometry payload | ~0.5–1 MB | ~22 MB |
| Total cold load | ~20–25 MB, seconds | ~40–50 MB, tens of seconds |
| Geometry capability | mesh CSG only, no exact B-Rep / STEP | full OCCT: B-Rep, fillets, STEP/glTF I/O |
| License | fully permissive (after scad2py GPL fix) | + OCCT LGPL §6 notice obligation (§4.1) |
| Build effort | small (rebuild manifold3d wasm wheel) | none — consume the published OCP.wasm wheel |
| Best for | scad2py customizer, OpenSCAD-style CSG, fast demos | real parametric CAD playground, STEP in browser |

Ship **both**, with the manifold build as the *default lightweight* entry point and the
OCP.wasm build as an *opt-in heavy mode*.

### 6.2 Is a build123d + manifold + scad2py browser stack feasible?

**Yes — feasible, with one mandatory prerequisite.** Every layer is wasm-ready:
build123d pure-Python ✅, OCP via OCP.wasm ✅ (or skipped on the manifold path),
manifold3d 🔨 (small wheel build, done before), scad2py pure-Python ✅, numpy/scipy/ply/
shapely ✅. The CadQuery-side reconstruction shows ochafik *already* got manifold3d
running standalone in the browser and scad2py transpilation running in pyodide — the last
scad2py commit is literally `4c06339 "pyodide index.html (Broken)"`, i.e. the integration
of the full parse → transpile → exec → render loop was unfinished, **not** blocked by any
fundamental wasm limitation.

The **one mandatory prerequisite** is legal, not technical: scad2py's `calc.py` /
`colors.py` must be the clean-room rewrites *before any `.wasm` bundle is built or
distributed* (§4.2). WASM static linking removes the separate-process argument entirely.

### 6.3 The path / what to prototype next

1. **GPL-clean scad2py first.** Apply `ddocs/design/scad2py-gpl-remediation.md`'s code
   remediation — swap `calc.py`/`colors.py` for the `p6_clean_room_scad2py_math/`
   rewrites, add an Apache-2.0 `LICENSE`. Hard gate on everything downstream.
2. **Rebuild a current `manifold3d` emscripten wheel.** ochafik did 2.4.5 via the
   `ochafik/manifold@pyodide-build` fork; redo for manifold 3.x against a current pyodide
   xbuild env. Ideally upstream a pyodide-distribution recipe so it is maintained, rather
   than carrying a private wheel.
3. **Prototype the light path:** scad2py customizer → manifold3d backend → glTF →
   `<model-viewer>`, fully in pyodide, in a Web Worker. This is the resurrection of
   scad2py's `index.html`, finished. No OCP. Smallest payload, cleanest license, and it
   validates the scad2py-in-browser story end to end.
4. **Validate the heavy path against the published wheel:** `micropip.install` the
   `cadquery-ocp-novtk` OCP.wasm wheel + the pure-Python build123d wheel; run a build123d
   STEP/glTF export in pyodide. Do **not** build OCCT yourself — consume the wheel. This
   is mostly already proven by `build123d-sandbox`; the build123d-side task is confirming
   build123d's own exporters (notably the `lib3mf` gap, §3) behave.
5. **Keep heavy OCCT work server-side / native.** The ~150 MB native OCP, fidelity-
   critical STEP I/O, slow Booleans — those belong on a real machine. Browser = fast mesh
   CSG + viewing + light B-Rep playground; server/native = production B-Rep.
6. **Carry the license texts.** Any distributed WASM bundle containing OCP.wasm: add the
   LGPL-2.1 + Open CASCADE Exception texts, an OCCT 7.9.3 source offer/link, and the
   prominent OCCT notice (§4.1). Get counsel sign-off on the §4.1 static-link / §6 relink
   question. The manifold-only bundle needs only the ordinary permissive attributions.

---

## 7. Sources

**Verified from local files:**
- `/Users/ochafik/github/build123d/pyproject.toml` — build123d deps, `cadquery-ocp-novtk
  >= 7.9, < 8.0`, `lib3mf`, `ocpsvg`, `ocp_gordon`.
- `/Users/ochafik/github/build123d/ddocs/research/06-license-analysis.md` — OCCT
  LGPL-2.1 + exception; GPL `calc.py`/`colors.py`; static-linking note (§10).
- `/Users/ochafik/github/build123d/ddocs/design/scad2py-gpl-remediation.md` — clean-room
  rewrite; prototype `ddocs/prototypes/p6_clean_room_scad2py_math/`; 3308 equivalence
  cases pass.
- `/Users/ochafik/github/scad2py/` — `.pyodide-xbuildenv/` (pyodide 0.25.x, CPython
  3.11.3), `.pyodide-xbuildenv-0.26.0/` (pyodide 0.26.0, CPython 3.12.1),
  `build/bdist.emscripten_3_1_46_wasm32/` + `.../3_1_58_wasm32/`, `requirements.txt`
  (`pyodide-build`, commented `git+https://github.com/ochafik/manifold.git@pyodide-build`),
  `index.html` / `index-manifold*.html` / `pyscript*.json`.
- `cadquery/ddocs/prototypes/p5-wasm-pyodide/README.md` — companion CadQuery WASM
  analysis (OCP.wasm OCCT 7.9.3, ~21.8 MB wheel, static linking, build difficulty).

**Web sources (verified at fetch time, 2026-05-22; knowledge cutoff Jan 2026):**
- OCP.wasm — "Build123d in the browser!": https://github.com/yeicor/OCP.wasm
- CadQuery discussion "OCP.wasm: OpenCascade in WebAssembly (build123d now runs in the
  browser)": https://github.com/CadQuery/cadquery/discussions/1876
- `cadquery-ocp-novtk-OCP.wasm` wheels on PyPI:
  https://pypi.org/project/cadquery-ocp-novtk-OCP.wasm/
- Yet Another CAD Viewer (build123d/CadQuery browser viewer + playground):
  https://github.com/yeicor-3d/yet-another-cad-viewer
- build123d-sandbox (build123d browser playground on OCP.wasm) — listed in
  https://github.com/phillipthelen/awesome-build123d
- manifold3d on PyPI (no emscripten wheel): https://pypi.org/project/manifold3d/
- Manifold (WASM via npm `manifold-3d`): https://github.com/elalish/manifold ·
  https://www.npmjs.com/package/manifold-3d
- OpenSCAD Web Playground (manifold = default backend):
  https://github.com/openscad/openscad-playground
- Pyodide — packages / micropip / wasm constraints:
  https://pyodide.org/en/stable/usage/loading-packages.html ·
  https://pyodide.org/en/stable/usage/wasm-constraints.html
- PEP 783 (emscripten wheels on PyPI — future):
  https://discuss.python.org/t/emscripten-tag-in-wheels-emscripten-4-0-9/104410
- opencascade.js (OCCT → JavaScript, not Python): https://ocjs.org/

**Uncertain (flagged inline):** exact current OCCT patch version in OCP.wasm; existence
of a `lib3mf` wasm build; whether `manifold3d` has since gained an official pyodide
recipe. Verify against live repos before relying on these.

---

*End of doc 11. License sections are an engineering reading of license texts, not legal
advice — confirm §4.1's static-link / LGPL §6 question with counsel before distributing
any WASM bundle containing OCP.wasm.*
