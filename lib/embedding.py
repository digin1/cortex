"""Cortex embedding function — talks to the warm embed_service sidecar, with a
transparent in-process fallback so cortex keeps working (just slower) if the
sidecar is down.

Used by lib/chroma_client.get_collection() so EVERY caller (MCP server + all
hooks) shares one embedder without changes.

The sidecar and the in-process fallback produce IDENTICAL vectors (same model,
normalize_embeddings=True), so query embeddings always land in the same space
as the stored document embeddings regardless of which path served them.
"""
import json
import os
import urllib.request

import numpy as np

try:
    # Subclassing the real base makes Chroma persist this as a proper (non-legacy)
    # embedding function instead of {"type": "legacy"}.
    from chromadb.api.types import EmbeddingFunction as _ChromaEF
except Exception:  # chromadb not importable in this context
    class _ChromaEF:  # minimal shim
        pass

MODEL_NAME = os.environ.get("CORTEX_EMBED_MODEL", "Alibaba-NLP/gte-modernbert-base")
RERANK_MODEL = os.environ.get("CORTEX_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
EMBED_HOST = os.environ.get("CORTEX_EMBED_HOST", "127.0.0.1")
EMBED_PORT = int(os.environ.get("CORTEX_EMBED_PORT", "8110"))
_SIDECAR_URL = f"http://{EMBED_HOST}:{EMBED_PORT}/embed"
_RERANK_URL = f"http://{EMBED_HOST}:{EMBED_PORT}/rerank"
_SIDECAR_TIMEOUT = float(os.environ.get("CORTEX_EMBED_TIMEOUT", "10"))

_local_model = None       # lazy in-process embedder fallback, cached per process
_local_reranker = None    # lazy in-process reranker fallback, cached per process


def _embed_via_sidecar(texts):
    payload = json.dumps({"texts": list(texts)}).encode("utf-8")
    req = urllib.request.Request(
        _SIDECAR_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_SIDECAR_TIMEOUT) as resp:
        embs = json.loads(resp.read())["embeddings"]
    # chromadb expects each embedding as a numpy array (it calls .tolist())
    return [np.asarray(e, dtype=np.float32) for e in embs]


def _embed_local(texts):
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer(MODEL_NAME, trust_remote_code=False)
    vecs = _local_model.encode(list(texts), normalize_embeddings=True, batch_size=32)
    return [np.asarray(v, dtype=np.float32) for v in vecs]


def embed_texts(texts):
    """Return embeddings for texts. Sidecar first, in-process model as fallback."""
    if not texts:
        return []
    try:
        return _embed_via_sidecar(texts)
    except Exception:
        return _embed_local(texts)


def _rerank_via_sidecar(query, documents):
    payload = json.dumps({"query": query, "documents": list(documents)}).encode("utf-8")
    req = urllib.request.Request(
        _RERANK_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_SIDECAR_TIMEOUT) as resp:
        return json.loads(resp.read())["scores"]


def _rerank_local(query, documents):
    global _local_reranker
    if _local_reranker is None:
        from sentence_transformers import CrossEncoder
        _local_reranker = CrossEncoder(RERANK_MODEL)
    return [float(s) for s in _local_reranker.predict([(query, d) for d in documents])]


def rerank_scores(query, documents):
    """Cross-encoder relevance scores for (query, doc) pairs. Sidecar first,
    in-process CrossEncoder as fallback. Returns [] for empty input; on total
    failure returns None so the caller can fall back to its input ordering."""
    if not documents:
        return []
    try:
        return _rerank_via_sidecar(query, documents)
    except Exception:
        try:
            return _rerank_local(query, documents)
        except Exception:
            return None


def embedding_available():
    """True iff embeddings can actually be produced right now — either the sidecar
    is healthy, or the in-process model library is importable as a fallback.

    Used by chroma_client.get_collection() to decide whether to serve the 768-dim
    collection or genuinely degrade to the legacy 384-dim one, so cortex never
    hands back a collection it can't embed against.
    """
    try:
        url = f"http://{EMBED_HOST}:{EMBED_PORT}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            h = json.loads(resp.read())
            if h.get("status") == "ok":
                # Sidecar up AND serving the expected model (a different model would
                # embed into an incompatible vector space). Mismatch -> not usable.
                return h.get("model") == MODEL_NAME
    except Exception:
        pass
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


class SidecarEmbeddingFunction(_ChromaEF):
    """ChromaDB embedding function backed by the cortex embed sidecar.

    Subclasses chromadb's EmbeddingFunction so the collection persists a proper
    (non-legacy) EF config. Both add and query paths share one embedding space.
    """

    def __call__(self, input):
        return embed_texts(list(input))

    def embed_documents(self, input):
        return embed_texts(list(input))

    def embed_query(self, input):
        return embed_texts(list(input))

    def is_legacy(self):
        return False

    @staticmethod
    def name():
        return "cortex_sidecar"

    def default_space(self):
        return "cosine"

    def supported_spaces(self):
        return ["cosine"]

    def get_config(self):
        return {"model": MODEL_NAME, "host": EMBED_HOST, "port": EMBED_PORT}

    @staticmethod
    def build_from_config(config):
        # Config is informational; the function always reads live env / defaults.
        return SidecarEmbeddingFunction()


def get_embedding_function():
    return SidecarEmbeddingFunction()
