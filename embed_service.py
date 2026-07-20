#!/usr/bin/env python3
"""Cortex embedding sidecar — keeps the embedding model warm in ONE long-lived
process so per-prompt hooks (recall.sh) don't pay a ~6s cold model load.

Both the MCP server and the shell hooks call this over HTTP via
lib/embedding.py's SidecarEmbeddingFunction. If this service is down, callers
fall back to an in-process model load (correct, just slow), so cortex never
hard-breaks — this service only makes it fast.

Endpoints:
  GET  /health            -> {"status":"ok","model":...,"dim":...}
  POST /embed  {"texts":[...]}  -> {"embeddings":[[...],...],"model":...}

Bind: 127.0.0.1:$CORTEX_EMBED_PORT (default 8110), loopback only.
Run:  /usr/bin/python3 embed_service.py     (or via the systemd --user unit)
"""
import json
import os
import sys
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "4")

MODEL_NAME = os.environ.get("CORTEX_EMBED_MODEL", "Alibaba-NLP/gte-modernbert-base")
HOST = os.environ.get("CORTEX_EMBED_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORTEX_EMBED_PORT", "8110"))
MAX_BODY = 8 * 1024 * 1024  # 8 MB request cap

_model = None
_dim = None


def _load_model():
    global _model, _dim
    if _model is None:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(MODEL_NAME, trust_remote_code=False)
        # embed a probe to learn the dimension
        v = m.encode(["probe"], normalize_embeddings=True)
        _dim = int(len(v[0]))
        _model = m
        print(f"[cortex-embed] loaded {MODEL_NAME} (dim={_dim}, ctx={m.max_seq_length})",
              file=sys.stderr, flush=True)
    return _model


def _embed(texts):
    m = _load_model()
    vecs = m.encode(list(texts), normalize_embeddings=True, batch_size=32)
    return [list(map(float, v)) for v in vecs]


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._json(200, {"status": "ok", "model": MODEL_NAME, "dim": _dim})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/embed":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "bad content-length"})
            return
        try:
            data = json.loads(self.rfile.read(length))
            texts = data["texts"]
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                raise ValueError("texts must be a list of strings")
        except Exception as e:
            self._json(400, {"error": f"bad request: {e}"})
            return
        try:
            embs = _embed(texts)
            self._json(200, {"embeddings": embs, "model": MODEL_NAME})
        except Exception as e:
            self._json(500, {"error": f"embed failed: {e}"})

    def log_message(self, format, *args):
        pass  # quiet; systemd/journal captures stderr prints above


def main():
    _load_model()  # warm at startup so the first request is already fast
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[cortex-embed] serving on http://{HOST}:{PORT}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
