"""Hybrid retrieval for cortex.

Combines three signals and degrades gracefully if any is unavailable:
  1. Dense  — ChromaDB vector search (gte-modernbert via the embed sidecar).
  2. BM25   — in-memory keyword search over all docs (rescues exact-token queries
              like filenames, flags, node IDs that dense embeddings fumble).
  3. Rerank — a cross-encoder re-scores the fused candidates for final ordering
              (the single biggest ordering-quality lift).

Dense and BM25 are fused with Reciprocal Rank Fusion (RRF), then the top fused
candidates are reranked. If the reranker is unavailable, RRF order stands; if
BM25 is unavailable, dense order stands; if dense fails, BM25 stands.
"""
import math
import re

try:  # dual import: `embedding` (hooks: lib/ on path) or `lib.embedding` (MCP server)
    import embedding as _emb
except ImportError:
    from lib import embedding as _emb

RRF_K = 60            # standard RRF damping constant
DENSE_K = 25          # dense candidates to pull
BM25_K = 25           # bm25 candidates to consider
FUSE_CANDIDATES = 20  # how many fused candidates to send to the reranker

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text):
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


def _rrf(rankings):
    """rankings: list of ordered id-lists. Returns {id: rrf_score}."""
    scores = {}
    for ranked in rankings:
        for rank, mid in enumerate(ranked, start=1):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _dense(collection, query, k, where=None):
    try:
        r = collection.query(query_texts=[query], n_results=k,
                             where=where if where else None)
    except Exception:
        return [], {}
    ids = r["ids"][0]
    dists = r["distances"][0] if r.get("distances") else [None] * len(ids)
    docs = r["documents"][0] if r.get("documents") else [None] * len(ids)
    metas = r["metadatas"][0] if r.get("metadatas") else [None] * len(ids)
    info = {mid: {"document": docs[i], "metadata": metas[i], "distance": dists[i]}
            for i, mid in enumerate(ids)}
    return ids, info


def _bm25(collection, query, k, where=None):
    try:
        from rank_bm25 import BM25Okapi
        data = collection.get(include=["documents", "metadatas"],
                              where=where if where else None)
        ids, docs, metas = data["ids"], data["documents"], data["metadatas"]
        corpus = [_tokenize(d) for d in docs]
        q_set = set(_tokenize(query))
        # Need query tokens and at least one non-empty doc (else BM25Okapi divides
        # by a zero average doc length).
        if not q_set or not any(corpus):
            return [], {}
        scores = BM25Okapi(corpus).get_scores(list(q_set))
        ranked = sorted(range(len(ids)), key=lambda i: -scores[i])
        # Keep docs that actually contain a query token. BM25 raw scores can be 0
        # or negative for genuine matches in small corpora, so filter on token
        # overlap rather than score sign (RRF only needs the rank order).
        ranked = [i for i in ranked if q_set & set(corpus[i])][:k]
    except Exception:
        return [], {}
    order = [ids[i] for i in ranked]
    info = {ids[i]: {"document": docs[i], "metadata": metas[i], "distance": None}
            for i in ranked}
    return order, info


def hybrid_search(collection, query, n_results=5, rerank=True, where=None):
    """Return up to n_results dicts {id, document, metadata, distance, rerank_score},
    ordered best-first. Never raises for retrieval issues — degrades instead.
    `where` is an optional ChromaDB metadata filter (e.g. {"type": "feedback"})."""
    dense_order, dense_info = _dense(collection, query, DENSE_K, where)
    bm25_order, bm25_info = _bm25(collection, query, BM25_K, where)

    if not dense_order and not bm25_order:
        return []

    # merged doc/metadata store (dense info wins — it carries the distance)
    store = dict(bm25_info)
    store.update(dense_info)

    rrf = _rrf([dense_order, bm25_order])
    candidates = sorted(rrf, key=lambda mid: -rrf[mid])[:FUSE_CANDIDATES]

    results = [{
        "id": mid,
        "document": store.get(mid, {}).get("document"),
        "metadata": store.get(mid, {}).get("metadata") or {},
        "distance": store.get(mid, {}).get("distance"),
        "rrf_score": rrf[mid],
        "rerank_score": None,
    } for mid in candidates]

    if rerank and results:
        docs = [r["document"] or "" for r in results]
        scores = _emb.rerank_scores(query, docs)
        if scores is not None and len(scores) == len(results):
            for r, s in zip(results, scores):
                r["rerank_score"] = float(s)
            results.sort(key=lambda r: r["rerank_score"], reverse=True)

    return results[:n_results]


def rerank_relevance_pct(rerank_score):
    """Map a cross-encoder logit to a 0-100% relevance for display (sigmoid)."""
    if rerank_score is None:
        return None
    return round(100.0 / (1.0 + math.exp(-rerank_score)), 1)
