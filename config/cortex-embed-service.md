# Cortex Embed Sidecar Setup

Cortex embeds memories with **gte-modernbert-base** (768-dim, 8192-token context)
instead of ChromaDB's default `all-MiniLM-L6-v2` (which silently truncated every
memory at 256 tokens). The model is served by a small always-on "sidecar" so
per-prompt hooks (`recall.sh`) don't cold-load a torch model on every prompt.

## Why a sidecar

Loading the model in-process costs ~6s + ~1.3 GB. `recall.sh` runs on *every*
user prompt, so it must not pay that. The sidecar keeps the model warm in one
long-lived process; callers embed over loopback HTTP in ~60 ms. Result: the
per-prompt recall path is ~0.5 s (faster than the old MiniLM default) with the
far better embeddings.

## Service Details

- **Host/Port:** `127.0.0.1:8110` (loopback only; override with `CORTEX_EMBED_PORT`)
- **Model:** `Alibaba-NLP/gte-modernbert-base` (override with `CORTEX_EMBED_MODEL`)
- **Script:** `~/.claude/skills/cortex/embed_service.py`
- **Endpoints:** `GET /health`, `POST /embed {"texts":[...]}`
- **Collection:** `claude_memories_v2` (768-dim). The old `claude_memories`
  (384-dim) is kept as an instant rollback + degraded fallback.

## Install

```bash
pip install --user sentence-transformers        # pulls transformers + torch
cp ~/.claude/skills/cortex/cortex-embed.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cortex-embed.service
curl -s http://127.0.0.1:8110/health            # {"status":"ok","model":...,"dim":768}
```

## Migrate existing memories (one-time)

Re-embed everything from the legacy collection into the 768-dim one:

```bash
python3 ~/.claude/skills/cortex/rebuild_embeddings.py \
    --source claude_memories --target claude_memories_v2
```

Re-run this whenever you change `CORTEX_EMBED_MODEL` (the vector space changes).

## Survive reboot / logout

`systemctl --user` units die on logout unless lingering is enabled (once, needs sudo):

```bash
sudo loginctl enable-linger $USER
```

## Fallbacks (cortex never hard-breaks)

- Sidecar **down** but `sentence-transformers` installed → the embedding function
  loads the model in-process (correct, just slow) — see `lib/embedding.py`.
- Sidecar down **and** `sentence-transformers` missing → `get_collection()` degrades
  to the legacy 384-dim `claude_memories` with Chroma's default embedder.

## Recalibrated thresholds

The new model's cosine distances are tighter than MiniLM's, so two thresholds were
retuned for it (revert these if you change models):
- `mcp_server.py` `DEDUP_DISTANCE_THRESHOLD` 0.15 → **0.06**
- `recall.sh` recall gates 0.6/0.7/0.75/0.85 → **0.40/0.45/0.48/0.55**
