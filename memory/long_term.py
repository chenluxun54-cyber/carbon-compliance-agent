"""
Tier 3 — LongTermMemory: Chroma vector DB + sentence-transformers for semantic RAG.

Embedding model: paraphrase-multilingual-MiniLM-L12-v2
  • Supports Chinese + English
  • ~120 MB download on first run
  • PyTorch 2.8 already installed — no extra deps needed

Chroma collection: "facts"
  • Persisted at ./chroma_db/ (relative to carbon_skill/)
  • Each document has metadata: user_id, memory_type, timestamp (unix float)

Time-decay reranking:
  • decay = exp(-DECAY_LAMBDA * age_days)
  • Effective score = chroma_distance * decay  (lower = better)
  • DECAY_LAMBDA = 0.01 → half-life ≈ 70 days
"""

import math
import os
import time
import uuid
import logging
from pathlib import Path

log = logging.getLogger(__name__)

EMBED_MODEL  = "paraphrase-multilingual-MiniLM-L12-v2"
DECAY_LAMBDA = 0.01   # time-decay rate (days)
CHROMA_PATH  = str(Path(__file__).parent.parent / "chroma_db")


def _load_encoder(model_name: str):
    """Load SentenceTransformer from local cache only (no network).
    Returns None if model weights are not yet downloaded — caller degrades gracefully.
    Run `python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"` once to download.
    """
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model_name, local_files_only=True)
    except Exception as exc:
        log.warning("SentenceTransformer not in local cache (%s) — semantic search disabled until model is downloaded", type(exc).__name__)
        return None


class LongTermMemory:
    def __init__(self, chroma_path: str = CHROMA_PATH, embed_model: str = EMBED_MODEL):
        import chromadb

        self._embed_model = embed_model
        self._encoder = _load_encoder(embed_model)   # None if model not yet downloaded
        self._client  = chromadb.PersistentClient(path=chroma_path)
        self._col     = self._client.get_or_create_collection(
            name="facts",
            metadata={"hnsw:space": "cosine"},
        )
        log.info("LongTermMemory ready — %d facts in store (encoder=%s)",
                 self._col.count(), "loaded" if self._encoder else "unavailable")

    def _get_encoder(self):
        """Lazy-load the encoder on first real use (e.g., after server restart with cached model)."""
        if self._encoder is None:
            self._encoder = _load_encoder(self._embed_model)
        return self._encoder

    def _embed(self, texts: list[str]) -> list:
        enc = self._get_encoder()
        if enc is None:
            return None
        return enc.encode(texts, normalize_embeddings=True).tolist()

    # ── write ─────────────────────────────────────────────────────

    def store_facts(self, user_id: str, facts: list[dict]) -> None:
        """
        facts: list of dicts with keys:
          - text (str)       — the fact content
          - memory_type (str) — user_profile | preference | qa_pattern | summary
        """
        if not facts:
            return
        valid = [f for f in facts if f.get("text", "").strip()]
        if not valid:
            return
        texts = [f["text"] for f in valid]
        embeddings = self._embed(texts)
        ts = time.time()
        if embeddings is None:
            log.debug("Skipping Chroma store — encoder not yet available")
            return
        self._col.add(
            ids        = [str(uuid.uuid4()) for _ in texts],
            embeddings = embeddings,
            documents  = texts,
            metadatas  = [
                {"user_id": user_id, "memory_type": f.get("memory_type", "fact"), "timestamp": ts}
                for f in valid
            ],
        )

    # ── recall ────────────────────────────────────────────────────

    def retrieve(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        """
        Semantic search filtered by user_id, reranked with time-decay.
        Returns list of { text, memory_type, score, age_days }.
        Falls back to recency-ordered retrieval when embeddings unavailable.
        """
        if self._col.count() == 0:
            return []
        n_results = min(top_k * 3, self._col.count())
        try:
            query_emb = self._embed([query])
            if query_emb is not None:
                res = self._col.query(
                    query_embeddings = [query_emb[0]],
                    n_results        = n_results,
                    where            = {"user_id": user_id},
                    include          = ["documents", "metadatas", "distances"],
                )
            else:
                # No encoder — retrieve most recent facts for this user
                raw = self._col.get(
                    where   = {"user_id": user_id},
                    include = ["documents", "metadatas"],
                )
                docs  = raw.get("documents", [])[-n_results:]
                metas = raw.get("metadatas", [])[-n_results:]
                return [
                    {"text": d, "memory_type": m.get("memory_type","fact"), "score": 0.0,
                     "age_days": round((time.time() - m.get("timestamp", time.time())) / 86400, 1)}
                    for d, m in zip(docs, metas)
                ]
        except Exception:
            return []

        docs      = res["documents"][0]
        metas     = res["metadatas"][0]
        distances = res["distances"][0]

        now = time.time()
        scored = []
        for doc, meta, dist in zip(docs, metas, distances):
            age_days = (now - meta.get("timestamp", now)) / 86400
            decay    = math.exp(-DECAY_LAMBDA * age_days)
            # Lower effective_score = more relevant (cosine distance is 0→2, lower = closer)
            effective = dist / (decay + 1e-9)
            scored.append({
                "text":        doc,
                "memory_type": meta.get("memory_type", "fact"),
                "score":       round(effective, 4),
                "age_days":    round(age_days, 1),
            })

        scored.sort(key=lambda x: x["score"])
        return scored[:top_k]

    # ── governance ────────────────────────────────────────────────

    def delete_user(self, user_id: str) -> int:
        """Delete all facts belonging to user_id. Returns count deleted."""
        try:
            existing = self._col.get(where={"user_id": user_id})
            ids = existing.get("ids", [])
            if ids:
                self._col.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0
