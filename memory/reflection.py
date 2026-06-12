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


def _ngram_embed(texts: list, dim: int = 384) -> list:
    import hashlib
    import numpy as np
    result = []
    for text in texts:
        vec = np.zeros(dim, dtype=np.float32)
        text = text.lower()
        for i in range(max(1, len(text) - 1)):
            gram = text[i:i+2]
            idx = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % dim
            vec[idx] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        result.append(vec.tolist())
    return result


def _load_encoder():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2", local_files_only=True)
    except Exception as exc:
        log.info("Neural encoder not in local cache (%s) — using offline n-gram fallback", type(exc).__name__)
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

    def _embed(self, texts: list) -> list:
        """Always returns embeddings — neural if model loaded, n-gram fallback otherwise."""
        enc = self._get_encoder()
        if enc is not None:
            return enc.encode(texts, normalize_embeddings=True).tolist()
        return _ngram_embed(texts)

    # ── write ─────────────────────────────────────────────────────

    def store(self, session_id: str, content: str, error_type: str = "general") -> None:
        """Store a reflection about a mistake or poor interaction outcome."""
        if not content.strip():
            return
        emb = self._embed([content])
        self._col.add(
            ids        = [str(uuid.uuid4())],
            embeddings = [emb[0]],
            documents  = [content],
            metadatas  = [{"session_id": session_id, "error_type": error_type, "timestamp": time.time()}],
        )

    # ── recall ────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        """Retrieve past reflections relevant to the current query."""
        if self._col.count() == 0:
            return []
        n = min(top_k, self._col.count())
        try:
            emb = self._embed([query])[0]
            res = self._col.query(
                query_embeddings = [emb],
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
