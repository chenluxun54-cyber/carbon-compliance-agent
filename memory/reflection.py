"""
Tier 4 — ReflectionMemory: separate Chroma collection for self-review.

Triggered by:
  • Tool call failures (exception caught in agent loop)
  • Negative user feedback keywords: 不对/错了/重试/不是这个/你搞错了

Stores LLM-summarised reflections about what went wrong and how to avoid it.
Retrieved before each LLM turn to prevent repeated mistakes.

Chroma collection: "reflections" (separate from "facts")
"""

import os
import time
import uuid
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CHROMA_PATH = str(Path(__file__).parent.parent / "chroma_db")

# Keywords that signal negative user feedback
NEGATIVE_SIGNALS = ["不对", "错了", "重试", "不是这个", "你搞错了", "不对啊", "再试", "不行", "不准确"]


def is_negative_feedback(text: str) -> bool:
    return any(kw in text for kw in NEGATIVE_SIGNALS)


def _load_encoder():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    except Exception as exc:
        log.warning("SentenceTransformer unavailable (%s) — reflection search degraded", exc)
        return None


class ReflectionMemory:
    def __init__(self, chroma_path: str = CHROMA_PATH):
        import chromadb

        self._encoder = _load_encoder()
        client = chromadb.PersistentClient(path=chroma_path)
        self._col = client.get_or_create_collection(
            name="reflections",
            metadata={"hnsw:space": "cosine"},
        )
        log.info("ReflectionMemory ready — %d reflections in store", self._col.count())

    def _get_encoder(self):
        if self._encoder is None:
            self._encoder = _load_encoder()
        return self._encoder

    def _embed(self, texts: list[str]) -> list:
        enc = self._get_encoder()
        if enc is None:
            return None
        return enc.encode(texts, normalize_embeddings=True).tolist()

    # ── write ─────────────────────────────────────────────────────

    def store(self, session_id: str, content: str, error_type: str = "general") -> None:
        """Store a reflection about a mistake or poor interaction outcome."""
        if not content.strip():
            return
        emb = self._embed([content])
        add_kwargs: dict = dict(
            ids       = [str(uuid.uuid4())],
            documents = [content],
            metadatas = [{"session_id": session_id, "error_type": error_type, "timestamp": time.time()}],
        )
        if emb is not None:
            add_kwargs["embeddings"] = [emb[0]]
        self._col.add(**add_kwargs)

    # ── recall ────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        """Retrieve past reflections relevant to the current query."""
        if self._col.count() == 0:
            return []
        n = min(top_k, self._col.count())
        try:
            emb = self._embed([query])
            if emb is not None:
                res = self._col.query(
                    query_embeddings = [emb[0]],
                    n_results        = n,
                    include          = ["documents", "metadatas", "distances"],
                )
                docs  = res["documents"][0]
                metas = res["metadatas"][0]
                dists = res["distances"][0]
                return [
                    {"text": doc, "error_type": m.get("error_type",""), "distance": round(d,4)}
                    for doc, m, d in zip(docs, metas, dists)
                ]
            else:
                # Fallback: return most recent reflections
                raw = self._col.get(include=["documents", "metadatas"])
                return [
                    {"text": d, "error_type": m.get("error_type",""), "distance": 0.0}
                    for d, m in zip(raw["documents"][-n:], raw["metadatas"][-n:])
                ]
        except Exception:
            return []

    # ── governance ────────────────────────────────────────────────

    def delete_user(self, user_id: str) -> int:
        """Delete reflections tied to sessions whose id starts with user_id prefix.
        Since user_id == session_id in single-user mode, deletes the exact session."""
        try:
            existing = self._col.get(where={"session_id": user_id})
            ids = existing.get("ids", [])
            if ids:
                self._col.delete(ids=ids)
            return len(ids)
        except Exception:
            return 0
