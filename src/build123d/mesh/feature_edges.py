"""
build123d mesh

name: feature_edges.py

desc:

The faceID feature-edge graph and its chain machinery — the substrate the mesh
chamfer/fillet operations select against (design §1.3, A3a phasing).

A **feature edge** is a mesh edge whose two incident triangles carry different
seeded ``face_id``s — i.e. a boundary between two analytic input faces. Because
``shape_to_manifold`` seeds every triangle of every input :class:`~build123d.Face`
with a globally unique id (preserved through every boolean), the set of feature
edges is exactly the set of analytic-face boundaries.

Feature edges sharing one ``(lo, hi)`` faceID pair are grouped into ordered
:class:`FeatureChain` s — straight single edges, open chains, and closed loops
all alike. A chain is the *selection unit*: a user never names a triangle index,
only a chain (typically via a :class:`FeatureChainSelection` query).

Each chain also carries:

* a **convexity class** — ``"convex"``, ``"concave"``, ``"flat"`` or
  ``"mixed"`` — derived from a robust per-edge convexity sign (the same
  far-vertex projection test the p10 prototype validated);
* per-vertex **kind tags** — ``"interior"``, ``"corner"`` (a vertex shared with
  another chain of a different ``pair``) or ``"endpoint"`` (open-chain ends).

A3a's chamfer only operates on chains with no ``"corner"`` vertices (multi-chain
corner blending is A3c — see :mod:`build123d.mesh.fillet`).

license:

    Copyright 2026 Gumyr

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Literal, Sequence

import numpy as np

# Sign threshold (along an edge) below which an edge counts as "flat" — i.e. the
# two adjacent faces are nearly coplanar and no chamfer/fillet is needed.
_FLAT_TOL = 1e-6


# ---------------------------------------------------------------------------
# data structures
# ---------------------------------------------------------------------------


ConvexityClass = Literal["convex", "concave", "flat", "mixed"]
VertexKind = Literal["interior", "corner", "endpoint"]


# pylint: disable=too-many-instance-attributes  # 10 fields, all load-bearing
@dataclass
class FeatureEdge:
    """One feature edge — a mesh edge between two differently-tagged triangles.

    Attributes:
        v0 (int): lower-index vertex of the shared mesh edge (``v0 < v1``).
        v1 (int): higher-index vertex.
        tri_a (int): one adjacent triangle index.
        tri_b (int): the other adjacent triangle index.
        fid_a (int): seeded ``face_id`` of ``tri_a``.
        fid_b (int): seeded ``face_id`` of ``tri_b`` (always ``fid_a != fid_b``).
        normal_a (np.ndarray): outward unit normal of ``tri_a``.
        normal_b (np.ndarray): outward unit normal of ``tri_b``.
        far_a (int): the vertex of ``tri_a`` not on the shared edge.
        far_b (int): the vertex of ``tri_b`` not on the shared edge.
    """

    v0: int
    v1: int
    tri_a: int
    tri_b: int
    fid_a: int
    fid_b: int
    normal_a: np.ndarray
    normal_b: np.ndarray
    far_a: int = -1
    far_b: int = -1

    @property
    def pair(self) -> tuple[int, int]:
        """The unordered ``(lo, hi)`` faceID pair — the chain key."""
        return (min(self.fid_a, self.fid_b), max(self.fid_a, self.fid_b))


@dataclass
class FeatureChain:
    """An ordered chain (or loop) of feature edges sharing one faceID pair.

    Attributes:
        pair (tuple[int, int]): the ``(lo, hi)`` faceID pair every edge separates.
        verts (list[int]): ordered vertex indices along the chain. A loop holds
            ``n`` verts and ``n`` edges (no repeated endpoint); an open chain
            holds ``n`` verts and ``n - 1`` edges.
        is_loop (bool): True if the chain closes on itself.
        edges (list[FeatureEdge]): the FeatureEdge objects in path order.
        convexity_class (ConvexityClass): chain-level convexity tag — set by
            :func:`classify_chain_convexity`. ``"mixed"`` means the chain's edges
            disagree in sign (a sweep tool must split at the sign flip).
        vertex_kinds (list[VertexKind]): per-vertex tag aligned with ``verts``,
            set by :func:`classify_vertex_kinds`. ``"corner"`` vertices are
            shared with another chain of a different ``pair`` and require A3c's
            setback-and-ball corner construction.
    """

    pair: tuple[int, int]
    verts: list[int]
    is_loop: bool
    edges: list[FeatureEdge] = field(default_factory=list)
    convexity_class: ConvexityClass = "flat"
    vertex_kinds: list[VertexKind] = field(default_factory=list)

    def __repr__(self) -> str:
        kind = "loop" if self.is_loop else "chain"
        return (
            f"FeatureChain(pair={self.pair}, {kind}, "
            f"{len(self.verts)} verts, {len(self.edges)} edges, "
            f"convexity_class={self.convexity_class!r})"
        )


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _unit(vector: np.ndarray) -> np.ndarray:
    """Normalize a 3-vector; return the input unchanged if it is near-zero."""
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


def triangle_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Return ``(M, 3)`` unit outward normals (one per triangle, CCW winding).

    Args:
        vertices (np.ndarray): ``(N, 3)`` vertex coordinates.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices.

    Returns:
        np.ndarray: ``(M, 3)`` unit normals.
    """
    corners = vertices[triangles]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


def edge_convexity_sign(vertices: np.ndarray, edge: FeatureEdge) -> float:
    """Robust convex/concave classification of one feature edge.

    Convexity is reported as a signed scalar: ``> 0`` convex, ``< 0`` concave,
    ``≈ 0`` flat (within :data:`_FLAT_TOL`). The magnitude is the unsigned
    dihedral component ``|dot(cross(n_a, n_b), edge_dir)|`` and the sign is
    forced by the *far-vertex projection* test — ordering-independent and
    robust on chamfer/sliver-prone inputs, matching the p10 prototype's
    ``edge_convexity``.

    Args:
        vertices (np.ndarray): ``(N, 3)`` vertex coordinates.
        edge (FeatureEdge): the feature edge to classify.

    Returns:
        float: signed convexity scalar (positive convex, negative concave).
    """
    p_0 = vertices[edge.v0]
    p_1 = vertices[edge.v1]
    edge_dir = _unit(p_1 - p_0)
    n_a = _unit(edge.normal_a)
    n_b = _unit(edge.normal_b)
    magnitude = abs(float(np.dot(np.cross(n_a, n_b), edge_dir)))
    if edge.far_a >= 0 and edge.far_b >= 0:
        signed = float(np.dot(n_a, vertices[edge.far_b] - p_0))
        signed += float(np.dot(n_b, vertices[edge.far_a] - p_0))
        convex = signed < 0.0
    else:
        convex = float(np.dot(np.cross(n_a, n_b), edge_dir)) > 0.0
    convexity = magnitude if convex else -magnitude
    if magnitude < _FLAT_TOL:
        return 0.0
    return convexity


# ---------------------------------------------------------------------------
# feature edge extraction + chain building
# ---------------------------------------------------------------------------


def extract_feature_edges(
    vertices: np.ndarray, triangles: np.ndarray, face_id: np.ndarray
) -> list[FeatureEdge]:
    """Find every mesh edge whose two adjacent triangles carry different faceIDs.

    Args:
        vertices (np.ndarray): ``(N, 3)`` vertex coordinates.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices.
        face_id (np.ndarray): ``(M,)`` seeded face id per triangle.

    Returns:
        list[FeatureEdge]: one entry per qualifying mesh edge.
    """
    normals = triangle_normals(vertices, triangles)
    edge_tris: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for tri_index, tri in enumerate(triangles):
        a, b, c = (int(x) for x in tri)
        fid = int(face_id[tri_index])
        for start, end in ((a, b), (b, c), (c, a)):
            key = (start, end) if start < end else (end, start)
            edge_tris[key].append((tri_index, fid))

    feature_edges: list[FeatureEdge] = []
    for (v_0, v_1), incident in edge_tris.items():
        if len(incident) != 2:
            # Non-manifold or boundary edge — a closed manifold never has these,
            # but guard so callers do not silently produce wrong chains.
            continue
        (tri_a, fid_a), (tri_b, fid_b) = incident
        if fid_a == fid_b:
            continue
        shared = {v_0, v_1}
        far_a = next(int(v) for v in triangles[tri_a] if int(v) not in shared)
        far_b = next(int(v) for v in triangles[tri_b] if int(v) not in shared)
        feature_edges.append(
            FeatureEdge(
                v0=v_0,
                v1=v_1,
                tri_a=tri_a,
                tri_b=tri_b,
                fid_a=fid_a,
                fid_b=fid_b,
                normal_a=normals[tri_a],
                normal_b=normals[tri_b],
                far_a=far_a,
                far_b=far_b,
            )
        )
    return feature_edges


def build_chains(feature_edges: list[FeatureEdge]) -> list[FeatureChain]:
    """Group feature edges into ordered chains/loops, one set per faceID pair.

    Two feature edges join into the same chain iff they share a vertex *and*
    the same ``(lo, hi)`` faceID pair. (At a 3-face corner three different
    pairs pass through the corner vertex; they must not weld into one chain —
    they are three chains meeting at one shared vertex, the substrate the A3c
    corner blend operates on.)

    The resulting chains have empty ``convexity_class`` / ``vertex_kinds``;
    call :func:`classify_chain_convexity` and :func:`classify_vertex_kinds` to
    populate them.

    Args:
        feature_edges (list[FeatureEdge]): the feature edges from
            :func:`extract_feature_edges`.

    Returns:
        list[FeatureChain]: chains, each with vertices in path order.
    """
    by_pair: dict[tuple[int, int], list[FeatureEdge]] = defaultdict(list)
    for edge in feature_edges:
        by_pair[edge.pair].append(edge)

    chains: list[FeatureChain] = []
    for pair, edges in by_pair.items():
        adjacency: dict[int, list[tuple[int, FeatureEdge]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.v0].append((edge.v1, edge))
            adjacency[edge.v1].append((edge.v0, edge))

        used: set[tuple[int, int]] = set()

        def edge_key(edge: FeatureEdge) -> tuple[int, int]:
            return (edge.v0, edge.v1)

        # Start chains from degree-1 endpoints first (open chains), then loops.
        endpoints = [v for v, nbrs in adjacency.items() if len(nbrs) == 1]
        starts = endpoints + [v for v in adjacency if v not in endpoints]

        for start in starts:
            for first_next, first_edge in adjacency[start]:
                if edge_key(first_edge) in used:
                    continue
                verts = [start]
                edge_list: list[FeatureEdge] = []
                cur = start
                step_to, step_edge = first_next, first_edge
                while True:
                    used.add(edge_key(step_edge))
                    verts.append(step_to)
                    edge_list.append(step_edge)
                    prev = cur
                    cur = step_to
                    if cur == start:
                        break
                    cont = [
                        (w, candidate)
                        for (w, candidate) in adjacency[cur]
                        if edge_key(candidate) not in used and w != prev
                    ]
                    if not cont:
                        cont = [
                            (w, candidate)
                            for (w, candidate) in adjacency[cur]
                            if edge_key(candidate) not in used
                        ]
                    if not cont:
                        break
                    step_to, step_edge = cont[0]
                is_loop = len(verts) > 2 and verts[0] == verts[-1]
                if is_loop:
                    verts = verts[:-1]
                chains.append(
                    FeatureChain(
                        pair=pair,
                        verts=verts,
                        is_loop=is_loop,
                        edges=edge_list,
                    )
                )
    return chains


def chains_by_pair(
    chains: Iterable[FeatureChain],
) -> dict[tuple[int, int], list[FeatureChain]]:
    """Index chains by their ``(lo, hi)`` faceID pair for selective lookup.

    Args:
        chains: any iterable of :class:`FeatureChain`.

    Returns:
        dict[tuple[int, int], list[FeatureChain]]: pair → chains map.
    """
    out: dict[tuple[int, int], list[FeatureChain]] = defaultdict(list)
    for chain in chains:
        out[chain.pair].append(chain)
    return dict(out)


# ---------------------------------------------------------------------------
# classification — convexity and vertex kinds
# ---------------------------------------------------------------------------


def classify_chain_convexity(
    chain: FeatureChain, vertices: np.ndarray
) -> ConvexityClass:
    """Tag a chain ``"convex"`` / ``"concave"`` / ``"flat"`` / ``"mixed"``.

    Every edge's signed convexity is computed; the chain takes the strong tag
    iff all non-flat edges share a sign, else ``"mixed"``. A chain whose every
    edge is flat is ``"flat"``.

    Args:
        chain (FeatureChain): the chain to classify (mutated in place — the
            returned value is also assigned to ``chain.convexity_class``).
        vertices (np.ndarray): the underlying ``(N, 3)`` vertex array.

    Returns:
        ConvexityClass: the resolved tag.
    """
    signs = [edge_convexity_sign(vertices, edge) for edge in chain.edges]
    has_convex = any(s > _FLAT_TOL for s in signs)
    has_concave = any(s < -_FLAT_TOL for s in signs)
    if has_convex and has_concave:
        chain.convexity_class = "mixed"
    elif has_convex:
        chain.convexity_class = "convex"
    elif has_concave:
        chain.convexity_class = "concave"
    else:
        chain.convexity_class = "flat"
    return chain.convexity_class


def classify_vertex_kinds(chains: list[FeatureChain]) -> None:
    """Tag every vertex of every chain as interior / corner / endpoint.

    A vertex is a **corner** iff it appears in two or more chains with
    *different* ``pair`` values (design §1.3). An endpoint is the start / end
    of an open chain; everything else is interior.

    Args:
        chains (list[FeatureChain]): chains to classify (mutated in place).
    """
    # Map vertex -> set of distinct pairs that touch it
    pairs_at_vertex: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for chain in chains:
        for v_index in chain.verts:
            pairs_at_vertex[v_index].add(chain.pair)

    for chain in chains:
        kinds: list[VertexKind] = []
        n_verts = len(chain.verts)
        for index, vertex in enumerate(chain.verts):
            kind: VertexKind
            if len(pairs_at_vertex[vertex]) >= 2:
                kind = "corner"
            elif not chain.is_loop and index in (0, n_verts - 1):
                kind = "endpoint"
            else:
                kind = "interior"
            kinds.append(kind)
        chain.vertex_kinds = kinds


# ---------------------------------------------------------------------------
# selection — the user-facing query type
# ---------------------------------------------------------------------------


class FeatureChainSelection:
    """A ``ShapeList``-flavoured collection of :class:`FeatureChain` s.

    The query surface for selecting mesh feature chains by faceID provenance
    (design §2). Created via :meth:`build123d.mesh.MeshPart.feature_edges`; the
    chainable filter methods (:meth:`convex`, :meth:`concave`, :meth:`of_source`,
    :meth:`closed`, …) return new selections so a fluent pipeline reads the
    same as build123d's native selectors.

    Selectors are faceID-based; no caller ever sees a triangle index. The
    selection also exposes :attr:`vertices`, :attr:`triangles`, :attr:`face_id`
    and :attr:`side_map` so downstream consumers (the swept-tool builder in
    :mod:`build123d.mesh.fillet`) have everything they need without re-reading
    the host mesh.
    """

    __slots__ = ("_chains", "_vertices", "_triangles", "_face_id", "_side_map")

    def __init__(
        self,
        chains: Sequence[FeatureChain],
        vertices: np.ndarray,
        triangles: np.ndarray,
        face_id: np.ndarray,
        side_map: object,
    ):
        """Wrap a list of chains with the mesh data they index into.

        Usually called by :meth:`MeshPart.feature_edges`, not directly.

        Args:
            chains: the chains in this selection (in their original order).
            vertices (np.ndarray): the host mesh ``(N, 3)`` vertex array.
            triangles (np.ndarray): the host mesh ``(M, 3)`` triangle vertex
                indices.
            face_id (np.ndarray): the ``(M,)`` per-triangle seeded face id.
            side_map: the host :class:`~build123d.mesh.bridge.SideMap` for
                provenance-based queries (kept as ``object`` to avoid a cyclic
                import; the type is :class:`~build123d.mesh.bridge.SideMap`).
        """
        self._chains = list(chains)
        self._vertices = vertices
        self._triangles = triangles
        self._face_id = face_id
        self._side_map = side_map

    # ---- properties ----

    @property
    def chains(self) -> list[FeatureChain]:
        """The chains currently in this selection."""
        return self._chains

    @property
    def vertices(self) -> np.ndarray:
        """The host mesh ``(N, 3)`` vertex array."""
        return self._vertices

    @property
    def triangles(self) -> np.ndarray:
        """The host mesh ``(M, 3)`` triangle vertex indices."""
        return self._triangles

    @property
    def face_id(self) -> np.ndarray:
        """The host mesh ``(M,)`` per-triangle seeded face id."""
        return self._face_id

    @property
    def side_map(self) -> object:
        """The host mesh :class:`SideMap` (returned as the originating object)."""
        return self._side_map

    # ---- chainable filters ----

    def _with(self, chains: Iterable[FeatureChain]) -> "FeatureChainSelection":
        """Helper: return a new selection with the same backing arrays."""
        return FeatureChainSelection(
            list(chains),
            self._vertices,
            self._triangles,
            self._face_id,
            self._side_map,
        )

    def convex(self) -> "FeatureChainSelection":
        """Chains whose every edge is convex (``convexity_class == "convex"``)."""
        return self._with(c for c in self._chains if c.convexity_class == "convex")

    def concave(self) -> "FeatureChainSelection":
        """Chains whose every edge is concave."""
        return self._with(c for c in self._chains if c.convexity_class == "concave")

    def flat(self) -> "FeatureChainSelection":
        """Chains whose every edge is near-flat (no chamfer/fillet needed)."""
        return self._with(c for c in self._chains if c.convexity_class == "flat")

    def closed(self) -> "FeatureChainSelection":
        """Chains that close on themselves (faceted loops, hole rims, …)."""
        return self._with(c for c in self._chains if c.is_loop)

    def open(self) -> "FeatureChainSelection":  # noqa: A003 — chain-API verb
        """Chains with two distinct endpoints (open arcs)."""
        return self._with(c for c in self._chains if not c.is_loop)

    def of_source(self, source: str) -> "FeatureChainSelection":
        """Chains touching at least one face whose ``FaceRecord.source == source``.

        Args:
            source (str): a side-map ``source`` name (e.g. ``"box"``,
                ``"cylinder"``) recorded at tessellation time.

        Returns:
            FeatureChainSelection: chains with a matching face on either side
            of every edge.
        """
        records = getattr(self._side_map, "records", {})

        def touches(chain: FeatureChain) -> bool:
            for fid in chain.pair:
                record = records.get(int(fid))
                if record is not None and getattr(record, "source", None) == source:
                    return True
            return False

        return self._with(c for c in self._chains if touches(c))

    def between(self, source_a: str, source_b: str) -> "FeatureChainSelection":
        """Chains whose ``pair`` straddles ``source_a`` on one side, ``source_b``
        on the other (e.g. the rim where a box top meets a cylinder lateral).

        Args:
            source_a (str): one side-map source name.
            source_b (str): the other side-map source name.

        Returns:
            FeatureChainSelection: matching chains (either ordering accepted).
        """
        records = getattr(self._side_map, "records", {})

        def straddles(chain: FeatureChain) -> bool:
            rec_a = records.get(int(chain.pair[0]))
            rec_b = records.get(int(chain.pair[1]))
            if rec_a is None or rec_b is None:
                return False
            sources = (
                getattr(rec_a, "source", None),
                getattr(rec_b, "source", None),
            )
            return set(sources) == {source_a, source_b}

        return self._with(c for c in self._chains if straddles(c))

    def of_face(self, face_id: int) -> "FeatureChainSelection":
        """Chains that have ``face_id`` on either side of the pair.

        Args:
            face_id (int): a seeded face id.

        Returns:
            FeatureChainSelection: matching chains.
        """
        target = int(face_id)
        return self._with(c for c in self._chains if target in c.pair)

    def filter(
        self, predicate: Callable[[FeatureChain], bool]
    ) -> "FeatureChainSelection":
        """Custom filter — ``predicate(chain) -> bool`` selects chains to keep.

        Args:
            predicate: a function returning True for chains to keep.

        Returns:
            FeatureChainSelection: chains for which ``predicate`` is True.
        """
        return self._with(c for c in self._chains if predicate(c))

    # ---- collection-flavoured dunders ----

    def __iter__(self) -> Iterator[FeatureChain]:
        return iter(self._chains)

    def __len__(self) -> int:
        return len(self._chains)

    def __bool__(self) -> bool:
        return bool(self._chains)

    def __getitem__(self, index: int) -> FeatureChain:
        return self._chains[index]

    def __repr__(self) -> str:
        return f"FeatureChainSelection({len(self._chains)} chains)"


# ---------------------------------------------------------------------------
# convenience — one-call extraction of the whole chain-graph from a mesh
# ---------------------------------------------------------------------------


def build_feature_graph(
    vertices: np.ndarray,
    triangles: np.ndarray,
    face_id: np.ndarray,
) -> list[FeatureChain]:
    """Extract feature edges, build chains, and classify every chain & vertex.

    A one-call convenience for callers (the chamfer/fillet operations) that
    want a fully classified chain graph in one shot.

    Args:
        vertices (np.ndarray): ``(N, 3)`` vertex coordinates.
        triangles (np.ndarray): ``(M, 3)`` triangle vertex indices.
        face_id (np.ndarray): ``(M,)`` seeded face id per triangle.

    Returns:
        list[FeatureChain]: every chain, with ``convexity_class`` and
        ``vertex_kinds`` populated.
    """
    feature_edges = extract_feature_edges(vertices, triangles, face_id)
    chains = build_chains(feature_edges)
    for chain in chains:
        classify_chain_convexity(chain, vertices)
    classify_vertex_kinds(chains)
    return chains


__all__ = [
    "ConvexityClass",
    "FeatureChain",
    "FeatureChainSelection",
    "FeatureEdge",
    "VertexKind",
    "build_chains",
    "build_feature_graph",
    "chains_by_pair",
    "classify_chain_convexity",
    "classify_vertex_kinds",
    "edge_convexity_sign",
    "extract_feature_edges",
    "triangle_normals",
]
