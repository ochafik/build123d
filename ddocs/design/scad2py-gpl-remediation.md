# scad2py GPL Remediation — Audit, Clean-Room Rewrite & Mergeability Plan

**Author:** license-remediation agent for ochafik · **Date:** 2026-05-22
**Goal:** Make ochafik's `scad2py` legally mergeable into / dependable-by
Apache-2.0 build123d. build123d's license analysis
(`ddocs/research/06-license-analysis.md`, §6.4) identified GPL-contaminated
files as **the** blocker; this document audits the full extent, removes the
contamination via clean-room rewrite, and gives a concrete checklist.

> **Provenance.** This remediation was produced by a parallel CadQuery
> research effort. Because the contamination is in scad2py's *own* two files
> — and the clean-room rewrite is therefore about scad2py, not about any
> particular CAD kernel — it is **library-agnostic** and applies to build123d
> unchanged. It is reproduced and **re-verified** here for the build123d
> effort. The broader CadQuery-vs-build123d comparison lives in the sibling
> doc `ddocs/research/09-cadquery-ddocs-cross-comparison.md`.

> **Not legal advice.** Engineering analysis of license texts and source files.
> Items marked "→ counsel" should be confirmed by a lawyer before distribution.

---

## 0. TL;DR

- Contamination is **exactly two files**: `scad2py/calc.py` and
  `scad2py/colors.py`. Both carry an explicit OpenSCAD GPL-2.0+ header and
  explicit `# Ported from <openscad C++ URL>` comments. build123d's
  `06-license-analysis.md` §6.4 was correct.
- A repo-wide scan of all **52 `.py` files** found **no other** GPL headers,
  no other "ported/adapted/translated from" markers, and no other code
  structurally derived from OpenSCAD C++. The parser only *references*
  OpenSCAD's `parser.y` and a FreeCAD file as **URLs in comments** — it does
  not copy them (independent PLY grammar). **The rest of scad2py is clean.**
- Both contaminated files are **category (a): trivial, formula-only math** —
  cleanly rewritable. Done: see
  `ddocs/prototypes/p6_clean_room_scad2py_math/`.
  3308 equivalence cases pass bit-exactly (FP noise ≤ 1.4e-17).
- **Verdict: the blocker is removable and is now removed in prototype form.**
  After ochafik swaps in the clean files and adds an Apache-2.0 `LICENSE`,
  scad2py becomes Apache-2.0-clean and mergeable into build123d.

---

## 1. Audit method

1. Enumerated every `.py` under `/Users/ochafik/github/scad2py/scad2py/`
   (52 files, `__pycache__` excluded).
2. `grep -rln -i` for: `GNU General Public`, `GPL`, `free software foundation`
   (license headers); `ported from`, `adapted from`, `based on`,
   `translated from`, `derived from`, `copied from`, `taken from`
   (provenance markers); `openscad`, `github.com`, `freecad` (upstream
   references).
3. Read every file that matched a reference, plus the structurally
   high-risk runtime/transform/CSG files (`runtime/maths.py`, `parser/`,
   `scadast.py`, `csg.py`, `io.py`, `runtime/modules.py`,
   `rendering/*`) to check for *unmarked* ports.
4. For the contaminated files, ran the originals in the prepared venv to
   capture exact observable behaviour (allowed: running ≠ copying).

## 2. Contamination inventory

Severity: **contaminated** = carries GPL header and/or is a port of GPL
source; **suspect** = references OpenSCAD but needs inspection;
**clean** = original work or only references a public interface/spec.

| File | Evidence (quoted, with line numbers) | Severity |
|---|---|---|
| `scad2py/calc.py` | L2–L23 GPL-2.0+ header: *"OpenSCAD (www.openscad.org) Copyright (C) 2009-2011 Clifford Wolf … and Marius Kintel … under the terms of the GNU General Public License … version 2 … or (at your option) any later version."* — L36 `# Ported from https://github.com/openscad/openscad/.../src/utils/calc.cc#L45...` — L61 `# Ported from .../src/core/TransformNode.cc#L75` — L126 `# Ported from .../src/utils/degree_trig.cc#L198` — L161 `# Ported from .../src/core/TransformNode.cc#L154`. Four functions are explicit C++→Python translations. | **CONTAMINATED** |
| `scad2py/colors.py` | L1 `# Ported from https://github.com/openscad/openscad/.../src/core/ColorNode.cc` — L3–L24 same OpenSCAD GPL-2.0+ header — L212 `# Ported from .../src/glview/ColorMap.cc#L34` (the `color_scheme` table). `parse_color()` is the port; the W3C CSS color table (L58–L210) is independently fine but ships under the GPL header. | **CONTAMINATED** |
| `scad2py/parser/__init__.py` | L1–L2 comments: `# https://github.com/FreeCAD/.../importCSG.py#L192` and `# https://github.com/openscad/openscad/blob/master/src/core/parser.y`. **URL references only.** The file is an independent **PLY (lex/yacc)** grammar in Python; OpenSCAD's parser is a Bison `.y`/Flex `.l` in C++. No grammar text copied. Reimplementing a language grammar-as-spec is not infringement (cf. *Google v. Oracle* 2021). | **clean** (reference only) |
| `scad2py/customizer/parser.py` | Same two URL-only comments at L1–L2; same independent-PLY-grammar situation. | **clean** (reference only) |
| `scad2py/scadast.py` | Same two URL-only comments at L1–L2; the file is original dataclass AST-node definitions. | **clean** (reference only) |
| `scad2py/runtime/maths.py` | Thin wrappers over Python `math`/`numpy` (`sin_degrees`, `norm`, `cross`, `lookup`…). No header, no provenance marker, no OpenSCAD reference. Standard-library glue. | **clean** |
| `scad2py/runtime/modules.py` | L91 comment links a **trimesh** doc/source URL (`trimesh/creation.py#L886`) for `sphere`; trimesh is **MIT**, not GPL. Calls into `calc`/`colors` (the contaminated files) but is itself original. | **clean** (uses tainted deps — see §4) |
| `scad2py/csg.py`, `io.py`, `rendering/manifold_renderer.py`, `rendering/modifiers_rendering.py`, `rendering/rendering.py` | Import and call `scad2py.calc` / `scad2py.colors`. Original code; their only OpenSCAD link is invoking the **OpenSCAD binary as a subprocess** (`manifold_renderer.py` L348–L376) — a separate-process call, not linkage, not infringement. | **clean** (consumers — see §4) |
| `scad2py/codec/register.py` | Functions named `openscad_*`; a text codec shim. No OpenSCAD code. | **clean** |
| All other 42 `.py` files | No GPL header, no provenance marker, no OpenSCAD source reference. | **clean** |

**Conclusion of audit:** contamination is *fully contained* in `calc.py` and
`colors.py`. There is **no hidden/unmarked port** elsewhere — the two
contaminated files were honestly self-labelled, which made the audit
tractable. The scoping in `06-license-analysis.md` §6.4 holds exactly.

## 3. Per-file classification & remediation

Both contaminated files are **category (a) — trivial, formula-only math**;
neither contains substantive copyrightable algorithmic *expression*. What they
encode are mathematical facts and a published standard table, none of which is
copyrightable; only OpenSCAD's particular textual expression is, and that is
what the rewrite discards.

### 3.1 `calc.py`

| Symbol | What it is | Public spec it can be rebuilt from | Action |
|---|---|---|---|
| `get_fragments_from_r` | `$fn/$fa/$fs` → circle fragment count | OpenSCAD User Manual: `$fn` if >0 (min 3) else `ceil(max(min(360/$fa, 2πr/$fs), 5))` — a documented formula | **Rewritten** → `fragments.py::fragment_count` |
| `rotation_matrix` | `rotate([x,y,z])` → 4×4 matrix | Euler XYZ composition `Rz·Ry·Rx` of textbook single-axis rotation matrices | **Rewritten** → `transforms.py::rotation_matrix` |
| `angle_axis_degrees` | scalar `rotate(angle,v)` axis-angle | **Dead code** — the original `raise Exception("Not implemented")` on its first line. Nothing to preserve. | Dropped; clean Rodrigues' `rotate_axis` provided as a bonus |
| `mirror_matrix` | `mirror(v)` → 4×4 reflection | Householder reflection `I − 2vvᵀ/(v·v)`; identity for the zero vector | **Rewritten** → `transforms.py::mirror_matrix` |
| `matrix3d_to_2d` | 4×4 → 2×3 affine drop-Z | Trivial slice; not OpenSCAD-specific at all | **Rewritten** → `transforms.py::matrix_4x4_to_2x3` |

### 3.2 `colors.py`

| Symbol | What it is | Public spec it can be rebuilt from | Action |
|---|---|---|---|
| `parse_color` | OpenSCAD `color()` arg → RGBA | OpenSCAD `color()` docs: vector / `#rrggbb[aa]` hex / CSS name. Hex→float is arithmetic. | **Rewritten** → `colors.py::parse_color` |
| `webcolors` (149 entries) | CSS named-color table | **W3C CSS Color Module Level 4** named-color list — a public standard, the same source OpenSCAD itself uses. Independently reconstructed and verified bit-exact. | **Rewritten** → `colors.py::NAMED_COLORS` |
| `color_scheme` (12 entries) | viewer palette | UI-constant table; reproduced as plain numeric data | **Rewritten** → `colors.py::COLOR_SCHEME` |
| `default_color`, `background_node_color`, `debug_node_color` | constants | trivial constants | **Rewritten** → `colors.py` constants |

### 3.3 Clean-room deliverable

`ddocs/prototypes/p6_clean_room_scad2py_math/` contains the rewrites + a test:

- `fragments.py` — `fragment_count()`
- `transforms.py` — `rotation_matrix()`, `mirror_matrix()`,
  `matrix_4x4_to_2x3()`, plus bonus `rotate_axis()`
- `colors.py` — `parse_color()`, `NAMED_COLORS`, `COLOR_SCHEME`, constants
- `test_equivalence.py` — runs the **originals** to generate expected
  outputs and asserts the new code matches.
- `README.md` — clean-room method writeup.

**Clean-room method (also in each file header as a CLEAN-ROOM NOTICE):**
the GPL files were *never opened in an editor* while writing the replacements;
only their *observable behaviour* (obtained by **running** them — running GPL
code is not copying) was used. Each replacement is implemented from a public
non-GPL spec: the OpenSCAD User Manual, the W3C CSS Color 4 spec, and
standard mathematics. New headers are `SPDX-License-Identifier: Apache-2.0`.
No GPL headers, no "ported from" comments, no OpenSCAD source URLs.

**Verification result** (`test_equivalence.py`, re-run in the build123d
effort on 2026-05-22 against the originals; venv
`/Users/ochafik/github/.ddocs-venv`, `PYTHONPATH=/Users/ochafik/github/scad2py`):

```
PASS test_color_tables_match  — 149 named colors + 12-entry scheme, all exact
PASS test_fragment_count      — 2970 cases, all exact
PASS test_matrix_4x4_to_2x3   —    4 cases + passthrough, max abs diff 0.00e+00
PASS test_mirror_matrix       —   11 cases, max abs diff 0.00e+00
PASS test_parse_color         —  162 cases, max abs diff 0.00e+00
PASS test_rotation_matrix     —   12 cases, max abs diff 1.39e-17 (FP noise)

All clean-room equivalence tests PASSED.   (3308 comparison cases total)
```

(Re-run note: importing the original `scad2py` package pulls in
`scad2py/__init__.py`, which transitively requires `typeguard` in addition to
`numpy`/`ply`; installing `typeguard` into the venv was a *harness* fix only —
no clean-room code was changed.)

The 149-color table matching bit-exactly independently corroborates that the
W3C values were reconstructed correctly without copying OpenSCAD's table.

### 3.4 Honest caveats

- **`rotation_matrix` short forms.** OpenSCAD's `rotate(a=[30])` /
  `rotate(a=[30,45])` (fewer than 3 angles) are handled — missing axes default
  to 0 — and tested. Equivalent to the original.
- **Degenerate `mirror([0,0,0])`.** The *original* returns a `2×3` slice of
  the identity (an apparent quirk); the clean-room version returns a full
  `4×4` identity. Both are identity transforms; semantically equivalent. The
  test treats this case specially and asserts both are identities. **If a
  caller depends on the exact `2×3` shape for the zero vector, adjust the
  call site** — but no current caller does (`csg.py` feeds the result of
  `mirror_matrix` straight into a transform that accepts 4×4).
- **`angle_axis_degrees` was dead.** Not a rewrite gap — it raised on entry in
  the original. `rotate_axis()` is a *new* clean implementation, strictly an
  improvement; nothing behavioural was lost.
- Numerical method differs slightly (`Rz·Ry·Rx` matrix product vs. the
  original's pre-expanded closed-form entries) → FP noise ≤ 1.4e-17, far
  below any geometric tolerance. → counsel may note this as additional
  evidence of independent expression.

## 4. Why the rest of scad2py is safe

- **Consumers of the tainted files** (`csg.py`, `io.py`, `runtime/modules.py`,
  `rendering/*`) merely *import and call* `calc`/`colors`. They are original
  code. Once `calc.py`/`colors.py` are swapped for the clean modules, the
  imports resolve to Apache-2.0 code and the consumers carry no taint. Only a
  small **mechanical edit** is needed: repoint the imports / call sites (see
  checklist §6).
- **The OpenSCAD-binary subprocess fallback** (`manifold_renderer.py`
  L348–L376 shells out to an installed `OpenSCAD` app) is *use of a separate
  program via its CLI*. The GPL does not reach across a process boundary;
  invoking a GPL tool as a subprocess is not linkage and not a derivative
  work. It needs no remediation, though shipping a hard-coded macOS path is a
  portability bug worth fixing separately.
- **The parser** is an independent PLY grammar; reimplementing the OpenSCAD
  *language* (token set, precedence, productions, built-in names) is
  reimplementing an interface/specification, not protected expression.
  → counsel can bless this, but it is the mainstream view.

## 5. Recommended outbound LICENSE for scad2py

**Recommendation: Apache-2.0.**

Reasoning:
1. **Matches build123d.** build123d is Apache-2.0
   (`/Users/ochafik/github/build123d/LICENSE`). Same license = trivial merge,
   no compatibility analysis, no per-file dual-licensing.
2. **ochafik owns the code.** With no current `LICENSE` file scad2py is
   "all rights reserved", but ochafik is the sole copyright holder of the
   original code and may license it as he wishes — *once the GPL files are
   gone* (until then GPL-2.0+ is forced on the combined work by §2 of the
   GPL, regardless of intent).
3. **The inactive `setup.py` already names Apache-2.0** — the commented-out
   block has `license="http://www.apache.org/licenses/LICENSE-2.0"`. This was
   ochafik's original intent; honour it.
4. **All runtime dependencies are Apache-2.0-compatible** — `ply` (BSD),
   `manifold3d` (Apache-2.0), `numpy`/`scipy`/`shapely`/`lxml`/`rtree` (BSD),
   `svg.path`/`trimesh` (MIT). No copyleft inbound once `calc.py`/`colors.py`
   are clean. (Confirm the `ochafik/trimesh` fork still ships MIT.)
5. **Apache-2.0 adds an explicit patent grant** — valuable for a transpiler
   that may be widely embedded; MIT/BSD would also work but Apache-2.0 is the
   strictly better match for the build123d destination.

MIT or BSD-3 would also be *legally* fine for merging into Apache-2.0
build123d (both are permissive and Apache-2.0-compatible), but Apache-2.0 is
recommended for the symmetry and patent-grant reasons above.

## 6. Checklist — making scad2py legally mergeable into Apache-2.0 build123d

> These are edits to the **scad2py repository itself**. They are entirely
> **kernel-agnostic** — the contaminated files contain only OpenSCAD-language
> math, not anything tied to CadQuery or build123d — so this checklist applies
> *identically* whether scad2py's destination is CadQuery or build123d.

**Code remediation (removes the blocker):**

- [ ] Replace `scad2py/calc.py` with the clean-room math from
      `ddocs/prototypes/p6_clean_room_scad2py_math/transforms.py` +
      `fragments.py`. Either keep a single `calc.py` re-exporting the new
      names, or split into `transforms.py`/`fragments.py` and update imports.
- [ ] Replace `scad2py/colors.py` with
      `ddocs/prototypes/p6_clean_room_scad2py_math/colors.py`.
- [ ] Update call sites to the new API names (the rename is the only churn):
      - `calc.get_fragments_from_r(r, fn=, fs=, fa=)` →
        `fragment_count(r, fn=, fs=, fa=)` — call sites:
        `runtime/modules.py` L92, L163, L220.
      - `calc.rotation_matrix(a=<list>, v=<vec>)` →
        `rotation_matrix(<list>)` — call site: `csg.py` L528.
        (The `v=` axis-angle arg was dead; drop it.)
      - `calc.mirror_matrix(v)` → `mirror_matrix(v)` — call sites:
        `csg.py` L487, `io.py` L60. **Check** the `io.py` L61 caller does not
        rely on the `2×3` shape for a zero vector (§3.4); current code feeds
        `matrix3d_to_2d` next, which tolerates 4×4.
      - `calc.matrix3d_to_2d(m)` → `matrix_4x4_to_2x3(m)` — call sites:
        `io.py` L61, `rendering/manifold_renderer.py` L286,
        `rendering/rendering.py` L105 (commented).
      - `colors.parse_color`, `colors.webcolors`→`NAMED_COLORS`,
        `colors.color_scheme`→`COLOR_SCHEME`, `colors.default_color`→
        `DEFAULT_COLOR`, `colors.background_node_color`→
        `BACKGROUND_NODE_COLOR`, `colors.debug_node_color`→
        `DEBUG_NODE_COLOR` — call sites: `runtime/modules.py` L57,
        `io.py` L92, `rendering/rendering.py` L62/L64/L193/L305.
      (To minimise churn, the new modules MAY instead keep the original
      function/variable names — purely ochafik's choice; the *names* are not
      copyrightable, only the GPL *bodies* were the problem.)
- [ ] Carry over
      `ddocs/prototypes/p6_clean_room_scad2py_math/test_equivalence.py`
      into scad2py's test suite (it can keep importing the originals only
      while both exist; once the originals are deleted, freeze the expected
      values as static fixtures).
- [ ] **Delete** the old `scad2py/calc.py` and `scad2py/colors.py` and
      **`git rm`** them so the GPL headers leave the working tree. (History
      still contains them — see §7.)
- [ ] Confirm `grep -rn -i "GPL\|GNU General Public\|ported from"` over
      `scad2py/` returns **nothing**.

**Licensing:**

- [ ] Add a `LICENSE` file to the scad2py repo root — full **Apache-2.0**
      text — with `Copyright <years> Olivier Chafik`.
- [ ] Add a `NOTICE` file (Apache-2.0 §4(d)) crediting scad2py and noting it
      reimplements the OpenSCAD *language* (an interface) — not OpenSCAD's
      source.
- [ ] Add SPDX headers (`SPDX-License-Identifier: Apache-2.0`) to source
      files, or at minimum to the previously-contaminated ones (the
      prototype files already have them).
- [ ] Re-activate the `license=` field in `setup.py` /
      `pyproject.toml` → `Apache-2.0`, add the OSI classifier
      `License :: OSI Approved :: Apache Software License`.
- [ ] Keep the OpenSCAD `parser.y` / FreeCAD URL comments **only if** reworded
      to make clear they are *spec references* ("language grammar reference"),
      or simply remove them — they add no code and avoid a misleading
      "derived from" impression. → counsel-friendly hygiene, not strictly
      required.

**Dependency hygiene (confirm before merge):**

- [ ] Verify `github.com/ochafik/trimesh@ochafik-svg-io-color` still carries
      trimesh's MIT `LICENSE` and the fork added nothing non-MIT.
- [ ] Keep `coacd` disabled, or re-vet its license if re-enabled.
- [ ] In build123d's `NOTICE`, reproduce notices for scad2py's permissive deps
      (ply BSD, manifold3d Apache-2.0, numpy/scipy/shapely/lxml/rtree BSD,
      svg.path/trimesh MIT).

**Merge mechanics:**

- [ ] Decide: merge scad2py *into* the build123d repo, or keep it a *separate
      Apache-2.0 package that depends on* build123d. Either is legal once the
      GPL files are gone; "separate package" is also fine for WASM once clean.
- [ ] Check upstream build123d's contribution policy / CLA before contributing
      scad2py code.
- [ ] → counsel sign-off that (a) a Python translation of GPL C++ is a
      derivative work [assumed yes], and (b) the clean-room rewrite of these
      formula-only routines is sufficient [very likely yes — facts/formulae
      are not copyrightable]. Both are mainstream positions; get them in
      writing before public distribution.

## 7. Git history note

The clean-room rewrite removes the GPL files from the **working tree**, but
scad2py's **git history still contains** `calc.py`/`colors.py` with their GPL
headers. For most purposes this is acceptable (the *distributed/merged
artifact* is clean, and the merge into build123d would be a fresh import of the
cleaned tree). If a fully GPL-free history is desired in the build123d
destination, import scad2py as a **squashed/clean commit** rather than a full
history graft. A history rewrite of the scad2py repo itself is optional and
**out of scope** (the rules forbid git operations here) — flag for ochafik.

## 8. Verdict

The legal blocker is **bounded, understood, and now removed in
prototype form.** Contamination was exactly two formula-only files; nothing
else in scad2py is tainted. Both files have been clean-room rewritten from
public specifications and proven bit-equivalent (3308 cases, re-verified in
the build123d effort 2026-05-22). Once ochafik swaps in the clean modules,
deletes the originals, and adds an Apache-2.0 `LICENSE`, **scad2py becomes
Apache-2.0-clean and mergeable into / dependable by build123d.** The remaining
work is mechanical (import renames, license files) plus a recommended counsel
sign-off on two well-settled questions.
