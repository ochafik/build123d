# 08 — Caching Infrastructure: build123d, OpenSCAD, scad2py

> Research deep-dive for: (1) bringing manifold3d mesh operations into build123d,
> and (2) wiring the `scad2py` OpenSCAD transpiler to build123d.
>
> Scope of this doc: **caching — in-memory and on-disk** — across all three
> systems. The owner's question: *"explore potential caching infra in build123d
> (does one exist?), both in memory and on disk, maybe similar to what OpenSCAD
> does."*
>
> Companion docs (do not duplicate): `01-build123d-architecture.md`,
> `02-build123d-mesh-paths.md`, `04-scad2py-architecture.md`. This doc
> cross-references their findings on the tessellate↔sew round trip, the
> `csg.Node`/`FastKey` machinery, and the manifold-backend insertion points.

---

## 0. TL;DR

| System | In-memory cache | On-disk cache | Cache key |
|---|---|---|---|
| **build123d** | **None for geometry.** Only one tiny ad-hoc dict (`_color_cache` in `importers.py`). OCCT *implicitly* caches `Poly_Triangulation` *on* the `TopoDS_Face`. | **None native.** `persistence.py` enables `pickle`; `.brep`/`.step` files can be hand-rolled into a cache but nothing does. | n/a — no cache layer to key. `Shape.__hash__` exists but is identity-ish, not a content hash. |
| **OpenSCAD** | `GeometryCache` + `CGALCache`, both LRU, byte-size-bounded, configurable. `render()` forces a subtree into the cache. | **Yes** — GSoC-2020 *persistent cache* (`--cache=file` or `--cache=redis`), Boost-serialized geometry. | **Textual dump of the CSG subtree** (`Tree::getIdString`), whitespace-stripped. |
| **scad2py** | `CachingVisitor` + the `@renderer` decorator: a content-addressed in-memory cache of rendered geometry, keyed by `FastKey`. Working, effective. | **None.** `cli.py` has a *commented-out* `.scad`→`.py` transpile cache. The `.pth` codec is an import hook, not a cache. | **`FastKey`** = interned hash of the `str(node)` OpenSCAD-syntax dump of the CSG subtree. |

The headline: **build123d has essentially no geometry caching infrastructure**, in
memory or on disk. OpenSCAD and scad2py both independently arrived at the *same*
core idea — **hash the CSG subtree's textual serialization, use that as the cache
key** — because CSG trees have abundant repeated sub-expressions. A manifold3d
backend for build123d, especially one fed by scad2py, should adopt that idea
directly; `scad2py.FastKey` is a ready-made implementation.

---

## 1. build123d caching today

### 1.1 Exhaustive search result: there is almost nothing

A full grep of `src/build123d/` for `lru_cache`, `@cache`, `cached_property`,
`functools`, `memo`, `cache` turns up:

- `build_common.py:49` — `import functools`, used only at `build_common.py:1346`
  for `@functools.wraps(func)` inside a decorator. **Not caching.**
- `topology/shape_core.py:55` — `from functools import reduce`. **Not caching.**
- `importers.py:160-173,247-248` — `_color_cache`, the *only* genuine cache in
  the library (detailed in §1.2).
- Various `__deepcopy__`/`memo` hits (`geometry.py`, `shape_core.py:1033`) — these
  are the `memo` dict that `copy.deepcopy` *passes in*, an aliasing guard, **not a
  persistent cache**.

There is **no `functools.lru_cache`, no `@cached_property`, no memoized boolean
results, no tessellation cache, no module/sub-expression cache** anywhere in
build123d. The library is a thin, mostly-stateless wrapper over OCP/OpenCASCADE;
it does not attempt to cache OCCT operations.

### 1.2 The one real cache: `importers._color_cache`

`importers.py:247-248` declares `_color_cache: dict[int, Quantity_ColorRGBA | None]`
inside `import_step` / `import_step_as_assembly`. `get_shape_color_from_cache`
(`importers.py:160-173`) keys it by `obj.TShape().__hash__()` — the hash of the
underlying **`TopoDS_TShape`** (the shared geometry record), not the located
`TopoDS_Shape`. The cache exists purely because *"Retrieving color info is
expensive"* (`importers.py:247`) — STEP color lookups walk the XCAF label tree.

This is instructive in two ways:

1. It confirms build123d devs *do* reach for caching when an operation is
   measurably expensive — they just have not done so for geometry.
2. It demonstrates the **`TShape().__hash__()` keying trick**: instances that
   share a TShape (e.g. via `Shape.share_topo()`) collide intentionally, which is
   exactly the dedup behavior you want — but it is a *pointer-identity* hash, not
   a content hash (see §1.5).

### 1.3 `persistence.py` — pickle support, the closest thing to disk persistence

`persistence.py` (160 lines, full read) is build123d's *only* serialization
infrastructure. It does **not** implement a cache; it makes OCP objects
*picklable* so that standard `pickle`/`copy.deepcopy` work on build123d `Shape`s.

Mechanism:

- `serialize_shape(shape)` (`persistence.py:54-64`) writes a `TopoDS_Shape` to a
  `BytesIO` via **`BinTools.Write_s`** — OCCT's binary BREP serializer. Returns
  raw `bytes`.
- `deserialize_shape(buffer)` (`:67-77`) reverses it via `BinTools.Read_s`, then
  `downcast`s to the concrete subclass.
- `serialize_location` / `deserialize_location` (`:80-133`) hand-roll a 7-float
  pack (3 translation + 4 quaternion) for `TopLoc_Location`.
- `reduce_shape` / `reduce_location` (`:136-143`) are `__reduce__`-style
  callables: `(deserialize_shape, (serialize_shape(shape),))`.
- `modify_copyreg()` (`:146-159`) registers those reducers in `copyreg.pickle`
  for all nine `TopoDS_*` types plus `TopLoc_Location`.

**Implications for caching:**

- A build123d `Shape` *can* be turned into `bytes` and written to disk **today**,
  via `pickle.dumps(shape)` (after `modify_copyreg()` runs) or directly via
  `BinTools.Write_s`. The serialized payload is OCCT's binary BREP format.
- This is the raw material for a disk cache — but **nothing in build123d builds
  one.** There is no "store this `Shape` under content-hash `H`, look it up
  later" layer. `persistence.py` is a *capability*, not a *cache*.
- The `Location` serializer is **lossy/approximate**: it packs floats with
  `struct.pack("f", ...)` — single-precision 32-bit. Round-tripping a `Location`
  through pickle loses precision. A disk cache that stores located shapes must
  either accept this or serialize via `BinTools` (which keeps the location inside
  the BREP stream).

### 1.4 BREP / STEP / glTF export — disk formats, but not a cache

`exporters3d.py` gives `export_brep` (`:232-249`, a one-liner over
`BRepTools.Write_s`), `export_step`, `export_stl`, `export_gltf`. A `.brep` file
*is* effectively the same binary payload `persistence.serialize_shape` produces
(`BinTools` vs `BRepTools` — both BREP, slightly different framing). So:

> **A `.brep` file is a viable on-disk representation of an exact build123d
> Shape.** If you wanted a persistent geometry cache for *exact* build123d
> solids, `.brep` blobs keyed by content hash is the natural format — no new
> serializer needed.

But export is **not** caching: it is user-driven, path-driven, and has no
lookup/invalidation logic. There is no `import_brep`-on-cache-hit path.

A critical anti-caching detail in the *mesh* export path: `export_gltf`
(`exporters3d.py:276-322`) explicitly does `BRepTools.Clean_s(to_export.wrapped)`
at `:322` *after* writing — it **deletes the triangulation it just computed**.
Comment at `:321`: `# Reset tessellation`. So even the implicit in-shape
triangulation cache (§1.6) is actively *discarded* by the exporter. build123d
treats tessellation as throwaway.

### 1.5 `Shape.__hash__` / `__eq__` — is there content-based hashing?

`shape_core.py:1053-1074`:

```python
def __eq__(self, other) -> bool:
    # "Two shapes are considered the same if they share the same TShape with
    #  the same Locations. Orientations may differ."
    if isinstance(other, Shape):
        return self.is_same(other)        # -> self.wrapped.IsSame(other.wrapped)
    return NotImplemented

def __hash__(self) -> int:
    if self._wrapped is None:
        return 0
    return hash(self.wrapped)             # hash of the OCP TopoDS_Shape
```

- `__eq__` delegates to `is_same` → `TopoDS_Shape::IsSame` (`shape_core.py:1557-1570`).
  `IsSame` is true iff two shapes **share the same `TShape` *and* the same
  `Location`** (orientation may differ). `is_equal` (`:1542-1555` →
  `IsEqual`) additionally requires orientation match.
- `__hash__` is `hash(self.wrapped)` — the hash of the pybind11-wrapped
  `TopoDS_Shape`. In OCP this maps to OCCT's `TopoDS_Shape` hashing, which mixes
  the **`TShape` pointer**, the location, and orientation.

**Is this a content hash usable as a cache key? No — and this is the crux.**

`Shape.__hash__`/`__eq__` are **structural-identity** comparisons, not
**content** comparisons. Two boxes built independently with identical dimensions
(`Box(1,1,1)` twice) have *different* `TShape` objects → different hash, not
equal. OCCT's `IsSame` answers *"is this literally the same topological record?"*
— useful for the dedup in `composite.py:759` and `three_d.py:976`
(`# Remove duplicates using Shape's __hash__`), and for ancestry walks via
`TopTools_ShapeMapHasher` (`shape_core.py:2826`). It is **useless as a cache key
for "have I computed a shape geometrically equal to this before?"**.

A true content-addressed cache for build123d would need a *separate* hash
function — one derived from the construction recipe (CSG subtree) or from a
canonicalized geometric fingerprint (vertex coords + topology, à la `BinTools`
bytes hashed). See §4.

`geometry.py` *does* have proper content-based `__hash__`/`__eq__` for the value
types — `Vector` (`:479-490`), `Axis` (`:761-790`), `Location` (`:1904-1915`),
`Plane` (`:2944-2955`) — all via a `_key()` that rounds coordinates
(`geometry.py:102`, `_rounded_vertex_key` in `brep_from_stl.py:178-181`). These
*are* cache-key-quality, but they cover lightweight geometry primitives, not
`Shape`s.

### 1.6 OCCT's implicit in-shape triangulation cache

This is the *one* place build123d gets caching "for free" from the kernel —
worth documenting because it is easy to miss.

When `BRepMesh_IncrementalMesh` runs, OCCT **stores the resulting
`Poly_Triangulation` directly on each `TopoDS_Face`** (inside the face's
`TopoDS_TFace`). It is retrieved later with `BRep_Tool::Triangulation_s`. This is
an *implicit, in-memory, in-the-object* mesh cache.

build123d's code relies on exactly this:

- `Shape.mesh(tolerance, ...)` (`shape_core.py:1604-1620`): **guards** the meshing
  call — `if not BRepTools.Triangulation_s(self.wrapped, tolerance):` then
  `BRepMesh_IncrementalMesh(...)`. So calling `mesh()` twice at the same tolerance
  is cheap the second time: the guard sees the existing triangulation and skips.
- `Shape.tessellate(tolerance, ...)` (`shape_core.py:2241-2288`) calls `mesh()`
  then reads back `BRep_Tool.Triangulation_s` per face (`:2257`).
- `Mesher` (`mesher.py:280-310`) and `exporters3d` likewise read
  `BRep_Tool.Triangulation_s` after meshing.

**Caveats / why this is a weak cache:**

1. **Tolerance-blind.** The guard `BRepTools.Triangulation_s(shape, tolerance)`
   checks whether a triangulation *exists that satisfies `tolerance`*. But
   `tessellate()` re-reads whatever triangulation is on the face — if a coarser
   one was cached, you get coarse triangles. There is no per-(tolerance) keyed
   set of triangulations; a face holds **one** triangulation.
2. **Destroyed on export.** `export_gltf` calls `BRepTools.Clean_s` (§1.4),
   wiping it. `BRepTools.Clean_s` is the standard "free the mesh memory" call.
3. **Lost on copy.** `Shape.__deepcopy__` (`shape_core.py:1033-1051`) copies via
   `BRepBuilderAPI_Copy(self.wrapped)`. By default `BRepBuilderAPI_Copy` does
   **not** copy the triangulation (`copyMesh=false`) — so a deepcopy of a meshed
   shape loses the cached mesh. `share_topo()` (`:1015-1031`) *does* keep it
   (shares the TShape).
4. **Not content-addressed.** It is attached to *this* `TopoDS` object. Build the
   same box again → no triangulation, must re-mesh.

So OCCT gives build123d a *per-object, single-tolerance, fragile* mesh cache.
There is no cross-object, content-keyed, or persistent layer above it.

### 1.7 Summary of build123d's caching posture

- **In-memory geometry cache:** none. (Plus the OCCT implicit per-face
  triangulation, §1.6, and the trivial `_color_cache`, §1.2.)
- **On-disk cache:** none. (But `persistence.py` + `.brep` export give all the
  *serialization* primitives one would need — §1.3, §1.4.)
- **Content hash for keying:** none for `Shape` (`__hash__` is identity-ish,
  §1.5). Value types in `geometry.py` *do* have content hashes.
- **Cache invalidation:** n/a — nothing to invalidate.

build123d is built on the assumption that the user holds Python references to the
`Shape`s they care about, and that OCCT operations are "fast enough." For
interactive/iterative workflows (ocp_vscode live preview, scad2py re-runs) that
assumption breaks down — which is the motivation for §4.

---

## 2. OpenSCAD's caching model

OpenSCAD is the reference design here: it has been refined over a decade
specifically because CSG re-evaluation is the dominant cost of the
edit-render-edit loop.

### 2.1 Two in-memory caches: `GeometryCache` and `CGALCache`

OpenSCAD keeps **two** LRU caches, both global singletons:

- **`GeometryCache`** — caches `Geometry` results: `PolySet` (mesh),
  `Polygon2d` (2D). This is the "fast" / preview-path cache (`$preview`,
  the F5 render).
- **`CGALCache`** — caches the heavy `CGAL_Nef_polyhedron` results — the exact
  Nef-polyhedron representation produced by the F6 "render" path.

Both derive from a generic `Cache<Key, T>` template (`src/utils/cache.h`). Key
properties of that class:

- **LRU eviction.** Insert order / use order is tracked; on overflow the
  least-recently-used entry is dropped.
- **Byte-size-bounded, not entry-count-bounded.** Each entry reports a *cost* in
  bytes (`Geometry::memsize()` / Nef memory estimate). The cache holds a running
  total and `trim()`s down to `maxCost` whenever it is exceeded.
- `setMaxCost(int m) { mx = m; trim(mx); }` — the limit is settable
  (`cache.h`). It is an `int`, so capped at `INT_MAX` ≈ 2 GB — a known wart
  (openscad/openscad#2401, "$15 awarded").
- **"Node didn't fit into cache"** warning: if a *single* result's `memsize`
  already exceeds `maxCost`, it cannot be cached at all and OpenSCAD emits
  `WARNING: GeometryEvaluator: Node didn't fit into cache`. The remedy users are
  told is to raise the cache size in Preferences.

Default sizes are configurable in **Preferences → Advanced** ("Turn off rendering
at N elements", "CGAL cache size", "PolySet/geometry cache size"). The point: the
cache is *size-bounded and tunable*, not unbounded.

### 2.2 The cache key — a textual dump of the CSG subtree

This is the single most important design idea to carry over.

`GeometryEvaluator::smartCacheGet` and `smartCacheInsert` key the caches with
**`this->tree.getIdString(node)`**.

`Tree::getIdString(node)` (`src/core/Tree.cc`) returns a **string serialization
of the entire CSG subtree rooted at `node`** — produced by `NodeDumper`. Key
details from the source:

- `getString(node)` produces an *indented, human-readable* dump.
- `getIdString(node)` produces the **same dump with whitespace stripped**. The
  comment is explicit: *"the ID string is stripped for whitespace. Especially
  indentation whitespace is important to strip to enable cache hits for
  equivalent nodes."*
- The dump strings are themselves memoized in a per-(dumper-config) `NodeCache`
  keyed by node identity (`nodecachemap` keyed by an `(indent, idString-mode)`
  tuple) — so `getIdString` is cheap on repeat.

So the **cache key for a geometry result is the canonicalized text of the recipe
that produces it.** Two different `union()`s with structurally identical children
produce identical id-strings → identical key → cache hit, *even though they are
different node objects in the tree*. This is content-addressing of the CSG graph.

OpenSCAD's `union()`/`group()` implicit wrapping can create *trivially different*
keys for identical meshes (e.g. an extra implicit union, a no-op `color()` or
`render()`). openscad/openscad#2218 ("avoid caching of trivially
different/identical meshes") added a patch that compares an incoming object's
*size and key substring* against entries already in the cache to avoid storing
duplicate copies — i.e. a second dedup layer on top of the id-string key.

### 2.3 `render()` — forcing a subtree into the cache

The `render()` module is OpenSCAD's user-facing cache control. Wrapping a subtree
in `render() { ... }` forces OpenSCAD to **evaluate that subtree to a concrete
(CGAL) geometry and cache it** — collapsing it to a single cached `PolySet`. On
subsequent renders the whole subtree is one cache hit. It is the manual lever for
"this part is expensive and stable, freeze it."

This matters for a build123d/scad2py port: it is the precedent for an explicit
"pin this sub-result" API (`@cached` / `freeze()` — see §4.5).

### 2.4 `$preview` vs render — two evaluation modes, two caches

OpenSCAD distinguishes the **preview** (`$preview == true`, F5, OpenCSG
rasterization, fast, approximate, uses `GeometryCache`) from the **render**
(`$preview == false`, F6, exact CGAL, slow, uses `CGALCache`). Cache hits are
mode-specific because the cached *type* differs (PolySet vs Nef). A model can
branch on `$preview` to show cheap geometry in preview and exact geometry on
render — and each gets its own cache entries.

For a manifold backend this distinction *softens*: manifold3d booleans are exact
*and* fast, so the "two-tier preview vs render" split that motivated two caches is
less necessary — one cache of manifold results can serve both. (Cross-ref §4.6.)

### 2.5 On-disk / persistent cache — GSoC 2020

OpenSCAD's in-memory caches die with the process. The **GSoC 2020 "Persistent
Cache" project** (openscad/openscad#3417, PR #3316, Wikibooks
`OpenSCAD_User_Manual/WIP/Persistent_Cache`) added an **on-disk / networked**
geometry cache:

- **Two backends:**
  - **Local filesystem** — `--cache=file` on the CLI, or a checkbox in
    Preferences → Advanced.
  - **Redis** — `--cache=redis,127.0.0.1,6379,password` (needs Hiredis ≥ 0.14.1).
- **What is serialized:** the intermediate geometry classes — `Polygon2d`,
  `PolySet`, `Vector2d`, `Vector3d` — via **Boost.Serialization**;
  `CGAL_Nef_polyhedron_3` via CGAL's Nef IO modules.
- **Key strategy:** *the same id-string key* — every rendered geometry is stored
  under its CSG-subtree id-string. The persistent layer is wired into
  `GeometryEvaluator`'s insert/check/retrieve calls, alongside the in-memory
  caches.
- **Status:** completed as a GSoC deliverable in a single PR; **the issue does
  not record it being merged to master**, and current OpenSCAD does not ship a
  documented `--cache=` flag in the stable manual. Treat it as *proof-of-concept
  precedent, not shipped infrastructure.*
- **Known gaps the project itself flagged:** no eviction policy for the
  persistent store (it grows unbounded), Boost.Serialization complexity, a
  `shared_ptr` serialization bug in Boost 1.58.

**Takeaway:** OpenSCAD's *idea* of a persistent geometry cache exists and is
keyed identically to the in-memory cache (CSG-subtree text). The execution was a
prototype that stalled — largely on serialization plumbing and the missing
eviction story. Anyone building the same for build123d should learn from both:
the key strategy is sound; budget for eviction and a robust serializer from day
one.

---

## 3. scad2py's caching

scad2py already contains a **working, content-addressed, in-memory cache** of
rendered CSG geometry. It is the most directly reusable artifact for a build123d
manifold backend. (Architecture context: `04-scad2py-architecture.md` §"Rendering".)

### 3.1 `FastKey` — the content hash (`scad2py/utils.py`)

`utils.py` defines the key type:

- **`HashedString`** — wraps a `str`, precomputes and caches `hash(s)`. `__eq__`
  compares the underlying strings. It is a string whose hash is computed once.
- **`FastKey`** — wraps a `HashedString`, with an **interning** table:
  - `FastKey.interned_keys: dict[HashedString, FastKey]` — class-level global.
  - `FastKey.intern(s: str) -> FastKey` — builds a `HashedString`, looks it up in
    the table, returns the existing `FastKey` if present, else inserts a new one.
  - Crucially, `FastKey.__eq__` is `return self is other` — **identity
    comparison**. This is sound *only because* interning guarantees one `FastKey`
    object per distinct string. After interning, equality is a pointer
    compare and hashing is a cached int — i.e. dictionary operations on `FastKey`
    are as cheap as possible.

So `FastKey` is: *intern the content string once, then treat keys as identities.*

### 3.2 How a `csg.Node` derives its key (`scad2py/csg.py`)

`csg.Node` (the base CSG dataclass) carries two lazily-computed fields,
`_str` and `_key`:

- `Node.to_str(indent=None)` (`csg.py`) serializes the node to **OpenSCAD `.csg`
  syntax**: `call_to_str()` emits `name(args, kwargs)`, `children_to_str()`
  recursively emits ` { child; child; }`. For groups/transforms this recursively
  serializes the **entire subtree**.
- `Node.__str__` memoizes the result into `self._str`.
- `Node.key` (property) — `if self._key is None: self._key = FastKey.intern(str(self))`.

So a node's **key is the interned `FastKey` of the OpenSCAD-syntax text of the
whole subtree rooted at that node.** This is *exactly* OpenSCAD's
`Tree::getIdString` strategy (§2.2) — independently arrived at. Two structurally
identical subtrees, even if built by different code paths, serialize to the same
string → intern to the same `FastKey` → hit the same cache entry.

Notes:
- `AbstractTransform.to_str` (`csg.py`) canonicalizes all transforms to
  `multmatrix(...)` form before serializing — so `translate([1,0,0])` and an
  equivalent `multmatrix` produce the *same* key. That is deliberate key
  normalization, the analogue of OpenSCAD's whitespace-stripping.
- Leaf primitives serialize their args (`cube(size=[1,1,1], center=false)` etc.),
  so leaves with identical parameters share keys.
- `Node.__hash__`/`__eq__` themselves `raise NotImplementedError` — nodes are
  *never* used as dict keys directly; **`node.key` is always the handle.**

### 3.3 `CachingVisitor` — subtree dedup (`scad2py/rendering/caching.py`)

`CachingVisitor(csg.DefaultVisitor)` walks the CSG tree (with
`transform_children=True`, so it rewrites the tree in place) and **deduplicates
structurally identical subtrees into shared node objects**:

```python
class _CacheEntry:
    def __init__(self, node): self.node = node; self.refcount = 1

class CachingVisitor(csg.DefaultVisitor):
    def __init__(self):
        super().__init__(transform_children=True)
        self.cache: Dict[csg.FastKey, _CacheEntry] = {}

    def is_reused(self, n):
        entry = self.cache.get(n.key)
        return entry is not None and entry.refcount > 1

    def visitNode(self, n):
        cached = self.cache.get(n.key)
        if cached is not None:
            cached.refcount += 1
            return cached.node          # replace this subtree with the shared one
        else:
            self.cache[n.key] = _CacheEntry(n)
            return super().visitNode(n)  # recurse
```

- First time a subtree's `FastKey` is seen: store it, recurse into children.
- Subsequent times: bump `refcount`, **return the already-seen node object** —
  the tree now has a *shared* subtree (a DAG, not a tree).
- `is_reused(n)` answers *"does this subtree appear more than once?"* — true iff
  `refcount > 1`.

So `CachingVisitor` turns the CSG **tree into a DAG**, collapsing repeats. This
is the structural-sharing pass; it does not render anything yet.

`count_distinct_nodes` (`caching.py`) is a debugging helper that counts unique
`id()`s reachable — used (in commented-out code) to measure how much the dedup
shrank the tree.

### 3.4 The `@renderer` decorator — caching rendered geometry (`manifold_renderer.py`)

`ManifoldRenderer` is the visitor that executes `csg.Node`s into
`manifold3d.Manifold` / `CrossSection`. Every `visitX` method is wrapped with the
`@renderer` decorator (`manifold_renderer.py`):

```python
def renderer(method):
    def wrapper(self, n):
        if not self.is_reused(n):
            # subtree appears once -> just render, no point caching
            self._stack.append(n); r = method(self, n); self._stack.pop()
        else:
            r = self.rendered_nodes_by_key.get(n.key)
            if r is not None:
                return clone(r)                       # CACHE HIT
            else:
                self._stack.append(n); r = method(self, n); self._stack.pop()
                self.rendered_nodes_by_key[n.key] = clone(r)   # populate
        return r
    return wrapper
```

Key behaviors:

1. **`is_reused` gate.** The renderer only *bothers* to cache subtrees that
   `CachingVisitor` flagged as appearing ≥ 2×. Render-once subtrees skip the
   cache entirely — no memory wasted on entries that will never be hit. This is a
   neat refinement over a naive "cache everything."
2. **Cache store:** `rendered_nodes_by_key: Dict[FastKey, _RenderedGeometry]` —
   the rendered `Manifold`/`CrossSection`/`Group` keyed by the same `FastKey`.
3. **`clone()` on both store and retrieve** (`manifold_renderer.py:36-52`). Because
   `manifold3d` geometry is mutated in place by subsequent ops (`+=`, `^=`,
   transforms), the cache stores a *copy* and hands out *copies*. For a
   `Manifold`, `clone` does `r.to_mesh()` → `Manifold(mesh)`; for a `CrossSection`
   it currently returns the same object (a comment notes the copy was elided);
   for a `Group` it deep-copies recursively. **`clone` is cheap precisely because
   `manifold3d` meshes are cheap to copy** — a flat vertex/triangle buffer. This
   is the property that does *not* hold for OCCT `Shape`s (cross-ref §4.4 and
   `04-scad2py-architecture.md` open question #4).

### 3.5 How it is all wired (`rendering/rendering.py`)

`render_geom` (`rendering.py:26-73`) is the single orchestration point:

```python
caching = CachingVisitor()
scene_transformer = SceneTransformer(..., is_reused=caching.is_reused)
renderer = ManifoldRenderer(is_reused=caching.is_reused)

def render(n):
    n = n.accept(caching)            # 1. dedup tree -> DAG
    n = n.accept(scene_transformer)  # 2. bubble colors/transforms (also is_reused-aware)
    r = n.accept(renderer)           # 3. render, with @renderer caching
    return r
```

So the pipeline is **dedup → scene-transform → render-with-cache**, and a single
`is_reused` predicate (closed over `CachingVisitor.cache`) is shared by all three
stages. The cache lifetime is **one `render_geom` call** — it is *not* persisted
across invocations of the program. Re-running scad2py on the same `.scad` file
re-parses, re-transpiles, re-execs, and re-renders from scratch.

### 3.6 No on-disk cache — but two near-misses

scad2py has **no persistent cache**. Two related artifacts:

- **`cli.py` — commented-out `.scad`→`.py` transpile cache.** `run_cli`
  (`cli.py`) contains a *disabled* block:
  ```python
  # filename = os.path.basename(file)
  # cachefile = os.path.join(dirname, filename + '.py')
  # if os.path.exists(cachefile) and os.path.getmtime(cachefile) > os.path.getmtime(file):
  #     ... exec(cached code) ...
  ```
  i.e. *"if a `.py` next to the `.scad` is newer than the `.scad`, skip
  parse+transpile and just `exec` the cached Python."* Keyed by **mtime**, not
  content hash. This would cache the *front half* of the pipeline (parse →
  transpile), not geometry. It is commented out; the live path always
  re-transpiles and even writes a fixed `tmp.py` (`cli.py`, near the end:
  `with open('tmp.py','wt') ... exec("import tmp; ...")`).
- **`codec/register.py` + `openscad.pth` — an import *codec*, not a cache.**
  `openscad.pth` is a one-line `.pth` file: `scad2py.codec.register`. Python
  executes `.pth` lines starting with `import` at interpreter startup; this one
  imports `scad2py.codec.register`, whose module-level code calls
  `codecs.register(search_function)`. `search_function` registers an `openscad`
  text codec so a file with `# -*- coding: openscad -*-` is transpiled on
  import via `PyxlIncrementalDecoder`/`PyxlStreamReader`. This is a
  *transparent-import* mechanism (write `.scad`-ish code, import it as if it were
  Python) — **it performs zero caching**; every import re-runs `transpile`. The
  `.pyc` that CPython writes for the *resulting* module is the only thing cached,
  and only by the standard bytecode-cache machinery, which is keyed on the
  *source file's* mtime/size — and would not see edits made through the codec
  correctly. Worth knowing it exists; not a geometry cache.

### 3.7 Summary of scad2py's caching posture

- **In-memory:** a real content-addressed cache of rendered geometry —
  `CachingVisitor` (tree→DAG dedup) + `@renderer` (`FastKey → Manifold`), gated
  by `is_reused`. Effective on repetitive models (the typical OpenSCAD model:
  `for` loops instantiating the same part N times all collapse to one render +
  N cheap `clone`s).
- **Cache key:** `FastKey` = interned hash of the OpenSCAD-syntax text dump of
  the CSG subtree — *the same idea as OpenSCAD's `getIdString`*.
- **On-disk:** none. (`cli.py` mtime-cache commented out; `.pth` codec is not a
  cache.)
- **Lifetime:** one `render_geom` call; nothing survives the process.

---

## 4. Design implications — caching for a manifold3d backend in build123d

### 4.1 The opportunity

Three facts combine:

1. build123d has **no geometry cache at all** (§1).
2. scad2py already has a **working content-addressed CSG cache** (§3) and, if
   wired to build123d, would *drive* build123d through exactly the kind of
   repetitive CSG workload caching is built for.
3. A manifold3d backend (cross-ref `01` §7.4, `02`, `03`) introduces a *mesh*
   representation alongside BREP — and **meshes are cheap to hash, cheap to copy,
   and cheap to serialize**, unlike OCCT `Shape`s. Caching becomes *much* more
   tractable on the mesh side.

### 4.2 What is worth building — in-memory

**(a) A content-hash-keyed cache of tessellations.**

The recurring expensive operation in any build123d↔mesh bridge is
`Shape.tessellate(tolerance)` — it runs `BRepMesh_IncrementalMesh` (cross-ref
`01` §7.2, `02`). Today OCCT's per-face implicit cache (§1.6) helps *if you keep
the same `TopoDS` object and the same tolerance*, and is destroyed by `Clean_s`.
A cache `(content_hash(shape), tolerance) → (verts, tris)` would survive copies,
survive `Clean_s`, and hit across structurally equal shapes.

The hard part is `content_hash(shape)` — `Shape.__hash__` does **not** work
(§1.5). Options:
- Hash the `BinTools.Write_s` bytes (`persistence.serialize_shape` already
  produces them) — `hashlib.sha256(serialize_shape(shape.wrapped))`. Exact and
  robust, but serialization itself has a cost; only worth it if meshing dominates.
- Hash the *construction recipe* if one is available — which, for a scad2py-fed
  pipeline, it **always is**: the `csg.Node` subtree. This is far cheaper.

**(b) A cache of manifold boolean results, keyed by the CSG recipe.**

This is the big win and it should be **keyed off the CSG tree, not off the OCCT
`Shape`s** — i.e. adopt scad2py's `FastKey` directly. When scad2py drives
build123d, the `csg.Node` tree *is* the recipe; `node.key` is already computed.
A manifold-backed build123d renderer should:

- reuse `CachingVisitor` to dedup the tree to a DAG;
- key rendered `Manifold`s (or sewn `Solid`s) by `node.key`;
- gate caching on `is_reused` exactly as `@renderer` does.

In other words: **for the scad2py→build123d path, the cache is already
designed** — it is scad2py's `caching.py` + `@renderer`, with the renderer's leaf
ops swapped to produce build123d/manifold geometry. `04-scad2py-architecture.md`
open question #4 ("does the caching layer still pay off") resolves cleanly *if*
the backend keeps geometry on the manifold side: `clone()` stays cheap.

**(c) For build123d used directly (no scad2py), a recipe cache is harder.**

build123d's fluent/builder API does not retain a serializable construction tree —
a `Box` is constructed and the recipe is gone. So a direct-build123d cache can
only be *content*-keyed (hash the BREP bytes), which is the §4.2(a) approach.
This is worth it for `tessellate` but probably *not* worth it for booleans
themselves when the BREP kernel is in use (serializing both operands to hash them
can cost as much as a small boolean). It becomes worth it once a manifold backend
makes the *cached* thing a cheap mesh.

### 4.3 What is worth building — on-disk

A persistent geometry cache for build123d is **plausible and the serialization
primitives already exist** (§1.3–1.4), but its value depends entirely on the
representation:

| Cached representation | On-disk format | Key | Verdict |
|---|---|---|---|
| Exact `Shape` (BREP) | `.brep` blob (`BinTools.Write_s`) | content hash of recipe or BREP bytes | Viable; needed if you want to skip slow OCCT *constructions* (lofts, fillets, large fuses) across runs. |
| Tessellation | mesh blob (npz of `verts`+`tris`, or `.glb`) | `(recipe_hash, tolerance)` | **Cheap, high-value.** Mesh blobs are tiny and trivial to (de)serialize. Best first persistent target. |
| `manifold3d.Manifold` | `MeshGL` arrays (npz) | recipe hash | Cheap; equivalent to the mesh blob. manifold3d picklability is unconfirmed (`01` open question #6) — store the raw `MeshGL` arrays, not the object. |

A concrete shape of it: a `~/.cache/build123d/` directory of content-addressed
blobs (`<sha256>.brep` / `<sha256>.npz`), an index, and an LRU eviction by total
bytes — i.e. **OpenSCAD's `--cache=file` design** (§2.5), and learning from its
gaps: **ship eviction from day one** (OpenSCAD's persistent cache notoriously
omitted it) and prefer a simple, stable serializer (npz / `BinTools` bytes) over
Boost.Serialization-style fragility.

**Is on-disk caching worth it?** Verdict: **yes for the scad2py-driven workflow,
marginal for interactive build123d.**
- scad2py re-runs the *whole* pipeline (parse→transpile→exec→render) on every
  invocation (§3.5). A persistent geometry cache keyed by `node.key` would make
  the second run of an unchanged-or-mostly-unchanged model near-instant — this is
  the *single biggest* responsiveness win available, and directly mirrors why
  OpenSCAD wanted a persistent cache.
- For interactive build123d in a notebook/`ocp_vscode`, the process is long-lived
  and an **in-memory** cache already captures the repeated-render benefit; disk
  adds value mainly across kernel restarts. Lower priority.

### 4.4 How a manifold backend changes the cost/benefit

Cross-ref `01` §7, `03`. A manifold3d backend makes booleans **cheap and exact**.
That shifts the caching calculus:

- **Booleans cheap ⇒ caching boolean *results* matters less per-op**, but the
  *aggregate* still matters for big repetitive trees — and the `is_reused` gate
  (§3.4) already restricts caching to genuine repeats, so the cache stays small
  and high-hit-rate.
- **The expensive op moves.** With manifold booleans, the new bottlenecks are
  (i) BREP→mesh `tessellate` of the *leaf* primitives entering the manifold
  world, and (ii) mesh→BREP *sewing* of results if exact output is needed
  (`Mesher._get_shape`, `01` §7.3). **Those** are what a cache should target —
  cache leaf tessellations (§4.2a) and cache sewn solids.
- **Meshes are cacheable; BREP `Shape`s are awkward.** `manifold3d` geometry is a
  flat buffer: cheap `hash`, cheap `clone` (scad2py relies on exactly this,
  §3.4), cheap serialize (npz). OCCT `Shape`s are not — `__hash__` is identity
  (§1.5), `deepcopy` goes through `BRepBuilderAPI_Copy`, serialization needs
  `BinTools`. **So a manifold backend doesn't just enable fast booleans — it
  makes caching itself feasible**, because the thing you cache is now a mesh.
- This resolves `04` open question #4 in the affirmative: the `FastKey`/`clone`
  caching layer **does** still pay off — *provided the cached artifact is the
  manifold mesh*, not a re-typed OCCT `Solid`.

### 4.5 Recommended caching design sketch

A layered design, smallest-useful-first:

**Layer 0 — respect OCCT's implicit mesh cache.** Stop throwing it away
gratuitously. The `BRepTools.Clean_s` in `export_gltf` (`exporters3d.py:322`) and
any new mesh-backend code should `Clean_s` only when memory pressure demands it,
not unconditionally. Free win, no new code.

**Layer 1 — in-memory recipe cache (scad2py-driven path).** For the
scad2py→build123d backend, **reuse `scad2py/rendering/caching.py` as-is**:
`CachingVisitor` for tree→DAG dedup, an `@renderer`-style decorator keying
rendered geometry by `csg.Node.key` (`FastKey`), gated by `is_reused`. The only
change vs scad2py's manifold renderer is what the leaf/op methods produce
(build123d `Shape`s or manifold meshes). `clone()` stays cheap iff the cached
artifact is a mesh. This is *the* recommended first deliverable for caching — it
is mostly *already written*.

**Layer 2 — in-memory tessellation cache (general build123d).** A module-level
`dict[(content_hash, tolerance, angular_tolerance), (verts, tris)]`, consulted by
`Shape.tessellate`. `content_hash` = `sha256(serialize_shape(self.wrapped))` from
`persistence.py`, computed lazily and itself memoized on the `Shape` instance
(`self._content_hash`). Bound it LRU by entry count or bytes (OpenSCAD's
`Cache<>` model). Optional, opt-in via a global flag — keeps default behavior
unchanged.

**Layer 3 — on-disk persistent cache.** `~/.cache/build123d/` of
content-addressed blobs: `.npz` (verts/tris or `MeshGL`) for meshes, `.brep` for
exact shapes. A small JSON/SQLite index. **LRU eviction by total size, present
from v1** (OpenSCAD's lesson). Key = the same recipe/content hash as Layer 1/2.
Primary beneficiary: scad2py re-runs (§4.3). Make it opt-in
(`BUILD123D_CACHE_DIR` env var, mirroring OpenSCAD's `--cache=file`).

**Cross-cutting — the key function.** Standardize one `content_key(obj)`:
- If a `csg.Node`/recipe is available → `node.key` (`FastKey`, free).
- Else for a bare `Shape` → `sha256(BinTools.Write_s bytes)`, memoized on the
  instance.
Both must be **stable across processes** (so Layer 3 works) — `FastKey`'s text
dump is; Python's `hash()` of a `TopoDS_Shape` is *not* (randomized,
identity-based) and must never be used for the disk layer.

**Explicit pin API.** Mirror OpenSCAD's `render()`: an optional
`freeze()`/`@cached` marker on a sub-result that forces it into the cache even if
`is_reused` did not flag it — for the "this part is expensive and I will not
touch it" case.

### 4.6 One cache, not two

OpenSCAD keeps `GeometryCache` *and* `CGALCache` because preview (PolySet) and
render (Nef) are different representations (§2.4). A manifold3d backend collapses
that: manifold booleans are exact *and* fast, so preview and final geometry can be
the *same* manifold mesh → **a single cache suffices**. Only keep a second,
BREP-keyed cache if/when exact STEP export with fillets is in scope — and that
cache is small (only the shapes the user explicitly exports exactly).

---

## 5. Recommendations

1. **Adopt scad2py's `FastKey` + `CachingVisitor` + `@renderer` design wholesale**
   for the scad2py→build123d backend. It is a correct, working, content-addressed
   CSG cache and it is *already written* (`scad2py/utils.py`,
   `scad2py/rendering/caching.py`, `manifold_renderer.py`). The CSG-subtree-text
   key is the same strategy OpenSCAD validated over a decade (`Tree::getIdString`).
   Do not invent a new keying scheme.
2. **Make the cached artifact a mesh (`manifold3d` `MeshGL` / verts+tris), not an
   OCCT `Shape`.** Meshes are cheap to hash, copy and serialize; `Shape`s are not
   (`Shape.__hash__` is identity-based, `shape_core.py:1070`; copy goes through
   `BRepBuilderAPI_Copy`). This is what keeps `clone()` cheap and makes both the
   in-memory and on-disk caches feasible. It resolves `04` open question #4.
3. **First deliverable: in-memory recipe cache** (Layer 1). Second: **in-memory
   `tessellate` cache** (Layer 2) — small, opt-in, high value for the BREP→mesh
   bridge. Both before any disk work.
4. **Build an on-disk cache only after the in-memory layers prove out**, and only
   if scad2py re-run latency is a real complaint. When you do: content-addressed
   blob store under `~/.cache/build123d/`, `.npz` for meshes / `.brep` for exact
   shapes (the serializers already exist — `persistence.serialize_shape`,
   `export_brep`), **LRU eviction by total bytes from day one** (OpenSCAD's
   persistent-cache project omitted eviction and that was a known defect). Opt-in
   via env var, à la OpenSCAD's `--cache=file`.
5. **Define one stable `content_key`**: `node.key` when a recipe exists,
   `sha256(BinTools bytes)` otherwise. Never use Python `hash(TopoDS_Shape)` for
   anything persistent — it is process-randomized and identity-based.
6. **Stop discarding the implicit triangulation gratuitously.** Reconsider the
   unconditional `BRepTools.Clean_s` in `export_gltf` (`exporters3d.py:322`); only
   clean under memory pressure. Zero-cost improvement to the existing implicit
   per-face mesh cache.
7. **One cache, not OpenSCAD's two.** A manifold backend makes exact==fast, so the
   preview/render cache split is unnecessary; keep a single mesh cache, plus a
   tiny BREP cache only if exact STEP-with-fillets export is in scope.
8. **Add an explicit pin API** (`freeze()` / `@cached`) mirroring OpenSCAD's
   `render()` for expensive-and-stable subtrees the `is_reused` heuristic misses.

---

## 6. Open questions

1. **`content_key` cost vs. benefit.** Hashing a bare `Shape` requires
   `BinTools.Write_s` (full BREP serialization). For cheap shapes that may cost as
   much as recomputing. Should the content-hash path be restricted to "shapes
   whose construction is known to be expensive," or always-on with the recipe
   key as the fast path and BREP-hash only as fallback? Needs benchmarking
   (cross-ref `01` open question #4 — quantify the manifold win).
2. **manifold3d picklability / stable serialization.** `01` open question #6
   flags that `manifold3d.Manifold` picklability is unknown. For the disk cache,
   confirm whether to store the `Manifold` object or just its `MeshGL` arrays
   (verts/tris/properties as npz). The arrays are the safe bet — but does
   round-tripping `MeshGL → Manifold` re-trigger manifold validation cost?
3. **Cache invalidation across `$fn`/`$fa`/`$fs` and tolerance.** scad2py's
   `FastKey` is derived from the *post-fragment-resolution* `csg.Node` text — does
   a `$fn` change already alter the key (because the resolved arc segment count
   changes the serialized geometry), or must `$fn`/deflection be folded into the
   cache key explicitly? (Cross-ref `04` open question #8.) For build123d's
   tessellation cache the tolerance *must* be part of the key (§4.2a).
4. **DAG-shared subtrees and mutation.** `CachingVisitor` turns the tree into a
   DAG with shared nodes. scad2py's `@renderer` defends against mutation with
   `clone()`. If a build123d backend ever mutates a `Shape` in place
   (`shape.wrapped.Location(...)`, `shape_core.py:1601`), a shared cached node
   could be corrupted. Confirm the build123d-backed renderer is fully
   copy-on-use, or that all ops are pure.
5. **Persistent-cache key portability.** A disk cache shared across machines /
   build123d versions needs the key (and ideally the blob format) to be stable
   across OCCT/manifold3d versions. OCCT BREP format is versioned; `MeshGL`
   layout could change. Should cached blobs carry a `(occt_version,
   manifold_version, format_version)` tag and invalidate on mismatch?
6. **Where the cache lives in a hybrid backend.** If build123d keeps *both* a BREP
   `Shape` and a manifold mesh (the "dual representation" of `01` §7.4 option 3),
   does the cache key the BREP, the mesh, or the recipe? Recommendation in §4.5 is
   "the recipe when available, else BREP bytes" — but a dual-representation Shape
   blurs this; needs a decision once the backend's data model is fixed.
7. **Scope of the on-disk cache for direct build123d users.** §4.3 concludes disk
   caching is marginal for interactive build123d (long-lived process). Is there a
   concrete interactive workflow (CI render farms, `ocp_vscode` across restarts,
   doc-build pipelines) where it *does* pay off enough to justify the eviction +
   versioning complexity?
