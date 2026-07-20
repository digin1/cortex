#!/usr/bin/env python3
"""Rebuild a cortex vector collection with the current sidecar embedder.

One-time migration / re-embed utility. Reads documents+metadata from a SOURCE
collection (the canonical text is preserved there), then creates the TARGET
collection with the SidecarEmbeddingFunction and re-embeds every document via the
warm embed_service sidecar (batched, precomputed vectors).

The SOURCE collection is only READ, never modified. Safe to re-run (idempotent:
it drops and recreates TARGET).

Usage:
    python3 rebuild_embeddings.py --source claude_memories --target claude_memories_v2
Requires the embed sidecar to be running (embed_service.py / cortex-embed.service).
"""
import argparse
import sys
import time

sys.path.insert(0, ".")
import chromadb
from lib.embedding import get_embedding_function, _embed_via_sidecar, EMBED_HOST, EMBED_PORT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="claude_memories")
    ap.add_argument("--target", default="claude_memories_v2")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    if args.source == args.target:
        print("ERROR: --source and --target must differ. In-place rebuild would delete "
              "the collection after reading it into RAM, risking data loss on a crash. "
              "Rebuild into a new target, then repoint CORTEX_COLLECTION.")
        sys.exit(2)

    # Fail fast if the sidecar isn't reachable — we do NOT want to silently
    # fall back to slow per-process in-proc loads for a 379-doc rebuild.
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{EMBED_HOST}:{EMBED_PORT}/health", timeout=5) as r:
            import json
            h = json.loads(r.read())
            print(f"sidecar OK: model={h.get('model')} dim={h.get('dim')}")
    except Exception as e:
        print(f"ERROR: embed sidecar not reachable at {EMBED_HOST}:{EMBED_PORT} ({e}). "
              f"Start embed_service.py first.")
        sys.exit(1)

    client = chromadb.HttpClient(host=args.host, port=args.port)

    src = client.get_collection(args.source)
    data = src.get(include=["documents", "metadatas"])
    ids, docs, metas = data["ids"], data["documents"], data["metadatas"]
    print(f"source {args.source!r}: {len(ids)} documents (read-only)")

    # Drop + recreate target with the sidecar EF. Note: cortex always opens this
    # collection via chroma_client.get_collection(), which passes the sidecar EF
    # explicitly — a *plain* reopen (no EF) would fall back to Chroma's 384-dim
    # default and dimension-mismatch, so never open it plain.
    try:
        client.delete_collection(args.target)
        print(f"dropped existing {args.target!r}")
    except Exception:
        pass
    target = client.create_collection(
        name=args.target,
        metadata={"hnsw:space": "cosine"},
        embedding_function=get_embedding_function(),
    )

    t = time.time()
    done = 0
    for i in range(0, len(ids), args.batch):
        b_ids = ids[i:i + args.batch]
        b_docs = docs[i:i + args.batch]
        b_metas = metas[i:i + args.batch]
        b_vecs = _embed_via_sidecar(b_docs)  # sidecar-only: abort loudly if it dies mid-run
        target.add(ids=b_ids, documents=b_docs, metadatas=b_metas, embeddings=b_vecs)
        done += len(b_ids)
        print(f"  {done}/{len(ids)} embedded ({time.time()-t:.0f}s)", flush=True)

    n = target.count()
    print(f"DONE: {args.target!r} has {n} documents in {time.time()-t:.0f}s")
    assert n == len(ids), f"count mismatch: {n} != {len(ids)}"

    # sanity query
    r = target.query(query_texts=["EIDF ImagePullBackOff registry token fix"], n_results=1)
    print("sanity query top:", r["ids"][0][0],
          f"({round((1-r['distances'][0][0])*100,1)}%)")


if __name__ == "__main__":
    main()
