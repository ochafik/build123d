"""p10 -- feature-edge graph from a MeshPart's seeded faceID.

The keystone primitive: an output mesh edge whose two adjacent triangles carry
*different* ``face_id``s is a **feature edge**. This module:

* extracts every feature edge from a ``ResultMesh`` (vertices/triangles/face_id);
* groups feature edges into **chains/loops**, keyed by the (unordered) pair of
  faceIDs that meet along them -- so a "select edge between face A and face B"
  query is just a dict lookup;
* exposes the local geometry each chamfer/fillet needs: per feature-edge the two
  adjacent triangles, their faceIDs, and the two outward face normals.

A feature edge is *directed* by convention from the lower vertex index to the
higher one; the chain builder walks the undirected graph.

Run standalone for a self-test on a box.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# data structures
# ---------------------------------------------------------------------------


@dataclass
class FeatureEdge:
    """One feature edge: a mesh edge between two differently-tagged triangles.

    Attributes:
        v0, v1: vertex indices (v0 < v1).
        tri_a, tri_b: the two adjacent triangle indices.
        face_a, face_b: the faceIDs of tri_a / tri_b (face_a < face_b kept as
            the *pair key*, but tri_a still pairs with the value stored).
        fid_a, fid_b: faceID actually carried by tri_a and tri_b respectively.
        normal_a, normal_b: outward unit normals of tri_a / tri_b.
    """

    v0: int
    v1: int
    tri_a: int
    tri_b: int
    fid_a: int
    fid_b: int
    normal_a: np.ndarray
    normal_b: np.ndarray
    far_a: int = -1  # the vertex of tri_a not on the shared edge
    far_b: int = -1  # the vertex of tri_b not on the shared edge

    @property
    def pair(self) -> tuple[int, int]:
        """The unordered faceID pair (lo, hi) -- the chain key."""
        return (min(self.fid_a, self.fid_b), max(self.fid_a, self.fid_b))


@dataclass
class FeatureChain:
    """An ordered chain/loop of feature edges sharing one faceID pair.

    Attributes:
        pair: the (lo, hi) faceID pair every edge in the chain separates.
        verts: ordered vertex indices along the chain. A loop repeats nothing
            (n verts, n edges); an open chain has n verts, n-1 edges.
        is_loop: True if the chain closes on itself.
        edges: the FeatureEdge objects in path order.
    """

    pair: tuple[int, int]
    verts: list[int]
    is_loop: bool
    edges: list[FeatureEdge] = field(default_factory=list)

    def __repr__(self) -> str:
        kind = "loop" if self.is_loop else "chain"
        return (
            f"FeatureChain(pair={self.pair}, {kind}, "
            f"{len(self.verts)} verts, {len(self.edges)} edges)"
        )


# ---------------------------------------------------------------------------
# triangle normals
# ---------------------------------------------------------------------------


def triangle_normals(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Return the (M,3) unit outward normal of every triangle (CCW winding)."""
    corners = vertices[triangles]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


# ---------------------------------------------------------------------------
# feature edge extraction
# ---------------------------------------------------------------------------


def extract_feature_edges(
    vertices: np.ndarray, triangles: np.ndarray, face_id: np.ndarray
) -> list[FeatureEdge]:
    """Find every mesh edge whose two adjacent triangles carry different faceIDs.

    Args:
        vertices: (N,3) coordinates.
        triangles: (M,3) vertex indices.
        face_id: (M,) seeded faceID per triangle.

    Returns:
        list[FeatureEdge]: the feature edges (one per qualifying mesh edge).
    """
    normals = triangle_normals(vertices, triangles)
    # edge -> list of (tri_index, face_id)
    edge_tris: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for ti, tri in enumerate(triangles):
        a, b, c = (int(x) for x in tri)
        fid = int(face_id[ti])
        for s, e in ((a, b), (b, c), (c, a)):
            key = (s, e) if s < e else (e, s)
            edge_tris[key].append((ti, fid))

    feats: list[FeatureEdge] = []
    for (v0, v1), incident in edge_tris.items():
        if len(incident) != 2:
            # Non-manifold or boundary edge -- a closed manifold never has these,
            # but guard anyway.
            continue
        (ta, fa), (tb, fb) = incident
        if fa == fb:
            continue
        shared = {v0, v1}
        far_a = next(int(v) for v in triangles[ta] if int(v) not in shared)
        far_b = next(int(v) for v in triangles[tb] if int(v) not in shared)
        feats.append(
            FeatureEdge(
                v0=v0,
                v1=v1,
                tri_a=ta,
                tri_b=tb,
                fid_a=fa,
                fid_b=fb,
                normal_a=normals[ta],
                normal_b=normals[tb],
                far_a=far_a,
                far_b=far_b,
            )
        )
    return feats


# ---------------------------------------------------------------------------
# chain / loop grouping
# ---------------------------------------------------------------------------


def build_chains(feature_edges: list[FeatureEdge]) -> list[FeatureChain]:
    """Group feature edges into ordered chains/loops, one set per faceID pair.

    Two feature edges join into the same chain iff they share a vertex *and*
    the same (lo,hi) faceID pair. (Sharing the pair matters: at a corner where
    three faces meet, three different pairs pass through the corner vertex; they
    must not be welded into one chain.)

    Args:
        feature_edges: the edges from :func:`extract_feature_edges`.

    Returns:
        list[FeatureChain]: chains, each with vertices in path order.
    """
    by_pair: dict[tuple[int, int], list[FeatureEdge]] = defaultdict(list)
    for fe in feature_edges:
        by_pair[fe.pair].append(fe)

    chains: list[FeatureChain] = []
    for pair, edges in by_pair.items():
        # adjacency on the vertex graph restricted to this pair
        adj: dict[int, list[tuple[int, FeatureEdge]]] = defaultdict(list)
        for fe in edges:
            adj[fe.v0].append((fe.v1, fe))
            adj[fe.v1].append((fe.v0, fe))

        used: set[tuple[int, int]] = set()

        def edge_key(fe: FeatureEdge) -> tuple[int, int]:
            return (fe.v0, fe.v1)

        # Start chains from degree-1 endpoints first (open chains), then loops.
        endpoints = [v for v, nbrs in adj.items() if len(nbrs) == 1]
        starts = endpoints + [v for v in adj if v not in endpoints]

        for start in starts:
            for nxt, fe in adj[start]:
                if edge_key(fe) in used:
                    continue
                # walk
                verts = [start]
                edge_list: list[FeatureEdge] = []
                cur = start
                step_to, step_edge = nxt, fe
                while True:
                    used.add(edge_key(step_edge))
                    verts.append(step_to)
                    edge_list.append(step_edge)
                    prev = cur
                    cur = step_to
                    if cur == start:
                        break
                    # pick an unused continuation
                    cont = [
                        (w, e)
                        for (w, e) in adj[cur]
                        if edge_key(e) not in used and w != prev
                    ]
                    if not cont:
                        # also allow going back if it's the only unused edge
                        cont = [
                            (w, e) for (w, e) in adj[cur] if edge_key(e) not in used
                        ]
                    if not cont:
                        break
                    step_to, step_edge = cont[0]
                is_loop = len(verts) > 2 and verts[0] == verts[-1]
                if is_loop:
                    verts = verts[:-1]
                chains.append(
                    FeatureChain(
                        pair=pair, verts=verts, is_loop=is_loop, edges=edge_list
                    )
                )
    return chains


def chains_by_pair(chains: list[FeatureChain]) -> dict[tuple[int, int], list[FeatureChain]]:
    """Index chains by their faceID pair for selective lookup."""
    out: dict[tuple[int, int], list[FeatureChain]] = defaultdict(list)
    for ch in chains:
        out[ch.pair].append(ch)
    return dict(out)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------


def _self_test() -> None:
    import build123d.mesh as bm
    from build123d import Box

    mp = bm.MeshPart.from_part(Box(10, 10, 10))
    rm = bm.bridge.read_result(mp.manifold)
    feats = extract_feature_edges(rm.vertices, rm.triangles, rm.face_id)
    chains = build_chains(feats)
    by_pair = chains_by_pair(chains)
    print(f"box: {len(feats)} feature edges, {len(chains)} chains")
    loops = [c for c in chains if c.is_loop]
    print(f"  {len(loops)} closed loops, {len(by_pair)} distinct faceID pairs")
    # A box has 12 edges: 6 faces, each adjacent pair contributes one edge.
    assert len(feats) == 12, f"expected 12 feature edges, got {len(feats)}"
    assert len(by_pair) == 12, f"expected 12 faceID pairs, got {len(by_pair)}"
    for pair, chs in sorted(by_pair.items()):
        n_e = sum(len(c.edges) for c in chs)
        print(f"  pair {pair}: {n_e} edge(s) over {len(chs)} chain(s)")
    print("box self-test OK")


if __name__ == "__main__":
    _self_test()
