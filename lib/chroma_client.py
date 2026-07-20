"""Shared ChromaDB client — HttpClient (fast) with PersistentClient fallback.

Usage:
    from lib.chroma_client import get_client, get_collection
"""
import os
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("ONNXRUNTIME_DISABLE_TELEMETRY", "1")
os.environ.setdefault("ORT_LOG_LEVEL", "ERROR")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("ONNXRUNTIME_SESSION_THREAD_POOL_SIZE", "2")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

CHROMA_HOST = os.environ.get("CORTEX_CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CORTEX_CHROMA_PORT", "8100"))
DB_PATH = os.path.expanduser("~/.claude/cortex-db")

# Canonical collection (768-dim, gte-modernbert via the embed sidecar).
DEFAULT_COLLECTION = os.environ.get("CORTEX_COLLECTION", "claude_memories_v2")
# Legacy 384-dim MiniLM collection — kept as an instant rollback + degraded fallback.
LEGACY_COLLECTION = "claude_memories"

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client

    import chromadb

    try:
        c = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        c.heartbeat()
        _client = c
        return _client
    except Exception:
        pass

    # Fallback: embedded mode (slow cold start)
    try:
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)
    except Exception:
        pass
    _client = chromadb.PersistentClient(path=DB_PATH)
    return _client


def _embedding_module():
    # Importable both as `lib.embedding` (MCP server: cortex root on path) and as
    # `embedding` (shell hooks: lib/ inserted on path).
    try:
        import embedding as _emb
    except ImportError:
        from lib import embedding as _emb
    return _emb


def get_collection(name=None):
    """Return the cortex collection.

    Default: the 768-dim `claude_memories_v2` collection embedded via the warm
    sidecar (lib/embedding.py). If embeddings genuinely can't be produced — the
    sidecar is down AND the in-process model library isn't importable, or the
    embedding module fails to load — degrade to the legacy 384-dim
    `claude_memories` with Chroma's default embedder so cortex still functions
    (with the old-quality retrieval) rather than hard-erroring at query time.
    """
    client = get_client()
    coll_name = name or DEFAULT_COLLECTION

    # The legacy 384-dim collection uses Chroma's default embedder.
    if coll_name == LEGACY_COLLECTION:
        return client.get_or_create_collection(
            name=LEGACY_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    # Every other collection is 768-dim and MUST be opened with the sidecar EF —
    # opening it plain makes Chroma fall back to its 384-dim default embedder and
    # dimension-mismatch on the first query/add.
    try:
        emb = _embedding_module()
        if emb.embedding_available():
            return client.get_or_create_collection(
                name=coll_name,
                metadata={"hnsw:space": "cosine"},
                embedding_function=emb.get_embedding_function(),
            )
    except Exception:
        pass
    # Embeddings genuinely unavailable — degrade to the legacy collection so cortex
    # still functions (old-quality retrieval) rather than hard-erroring at query time.
    return client.get_or_create_collection(
        name=LEGACY_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
