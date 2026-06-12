"""
MemoryManager — orchestrates all 4 memory tiers.

Write pipeline (call order per chat turn):
  1. on_user_message()   → Tier 1 (sliding window) + Tier 2 (Redis)
  2. on_turn_complete()  → Tier 2 flush + async LLM fact extraction → Tier 3
                         → reflection store if negative signal detected

Recall pipeline (called before each LLM generation):
  build_context(session_id, query) →
      Tier 1: sliding window messages (returned directly as message list)
    + Tier 3: top-5 semantic facts   (injected as system prompt block)
    + Tier 4: top-3 reflections       (injected as system prompt block)

Governance:
  wipe_user(user_id)    — deletes Tier 3 + Tier 4 data for that user
  wipe_session(sid)     — deletes Tier 1 + Tier 2 data for that session

user_id convention: equals session_id (single-user mode; ready to decouple when auth added)
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from memory.short_term   import TransientMemory
from memory.session_store import SessionStore
from memory.long_term    import LongTermMemory
from memory.reflection   import ReflectionMemory, is_negative_feedback

log = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self):
        self.short_term = TransientMemory(max_window=20)
        self.session    = SessionStore()
        self.long_term  = LongTermMemory()
        self.reflection = ReflectionMemory()
        log.info("MemoryManager initialised — all 4 tiers ready")

    # ─────────────────────────────────────────────────────────────
    # Write pipeline
    # ─────────────────────────────────────────────────────────────

    def sync_from_messages(self, session_id: str, messages: list) -> None:
        """Called after session restore — populate Tier 1 from the full message list."""
        self.short_term.set(session_id, messages)

    def flush_messages(self, session_id: str, messages: list) -> None:
        """Persist current message list to Redis (Tier 2). Synchronous."""
        self.session.save_messages(session_id, messages)

    async def on_turn_complete(
        self,
        session_id: str,
        messages: list,
        client,          # anthropic.AsyncAnthropic client
        model: str,
        memory_extract_system: str,
        user_message: str = "",
    ) -> None:
        """
        Called after each agent turn completes. Does three things:
          1. Flush messages to Redis
          2. If ≥4 real user turns and not yet summarised → extract facts to Tier 3
          3. If negative feedback signal → store reflection to Tier 4
        """
        loop = asyncio.get_event_loop()

        # 1. Flush to Redis
        await loop.run_in_executor(None, lambda: self.session.save_messages(session_id, messages))

        # 2. Fact extraction gate
        already = await loop.run_in_executor(None, lambda: self.session.is_summarized(session_id))
        if not already:
            real_user_count = sum(
                1 for m in messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and not m["content"].strip().startswith("[系统上下文]")
            )
            if real_user_count >= 4:
                asyncio.create_task(
                    self._extract_facts(session_id, messages, client, model, memory_extract_system)
                )

        # 3. Negative feedback → reflection
        if user_message and is_negative_feedback(user_message):
            asyncio.create_task(
                self._store_negative_reflection(session_id, user_message, messages, client, model)
            )

    async def on_tool_error(self, session_id: str, tool_name: str, error: str) -> None:
        """Store a reflection when a tool call fails."""
        content = f"工具调用失败 [{tool_name}]: {error[:300]}"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self.reflection.store(session_id, content, error_type="tool_failure")
        )

    # ─────────────────────────────────────────────────────────────
    # Recall pipeline
    # ─────────────────────────────────────────────────────────────

    def build_memory_block(self, session_id: str, query: str) -> str:
        """
        Returns a formatted string to append to the system prompt.
        Combines Tier 3 facts and Tier 4 reflections.
        """
        user_id = session_id  # single-user: user_id == session_id
        parts = []

        facts = self.long_term.retrieve(user_id=user_id, query=query, top_k=5)
        if facts:
            by_type: dict[str, list[str]] = {}
            for f in facts:
                by_type.setdefault(f["memory_type"], []).append(f["text"])
            lines = ["【长期记忆】"]
            if by_type.get("user_profile"):
                lines.append("• 用户画像：" + "；".join(by_type["user_profile"][:3]))
            if by_type.get("preference"):
                lines.append("• 偏好与习惯：" + "；".join(by_type["preference"][:2]))
            if by_type.get("qa_pattern"):
                lines.append("• 常见问题模式：" + "；".join(by_type["qa_pattern"][:3]))
            if by_type.get("summary"):
                lines.append("• 近期摘要：" + by_type["summary"][0][:200])
            parts.append("\n".join(lines))

        reflections = self.reflection.retrieve(query=query, top_k=3)
        if reflections:
            lines = ["【过往反思（避免重蹈覆辙）】"]
            for r in reflections:
                lines.append(f"• {r['text'][:200]}")
            parts.append("\n".join(lines))

        if not parts:
            return ""
        block = "\n\n".join(parts)
        return block[:2000] + ("…" if len(block) > 2000 else "")

    # ─────────────────────────────────────────────────────────────
    # Governance
    # ─────────────────────────────────────────────────────────────

    def wipe_session(self, session_id: str) -> None:
        """Delete all Tier 1 + Tier 2 data for a session."""
        self.short_term.clear(session_id)
        self.session.delete(session_id)

    def wipe_user(self, user_id: str) -> dict:
        """Delete all Tier 3 + Tier 4 data for a user. Returns counts."""
        facts_deleted       = self.long_term.delete_user(user_id)
        reflections_deleted = self.reflection.delete_user(user_id)
        return {"facts_deleted": facts_deleted, "reflections_deleted": reflections_deleted}

    # ─────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────

    async def _extract_facts(
        self,
        session_id: str,
        messages: list,
        client,
        model: str,
        system_prompt: str,
    ) -> None:
        """LLM fact extraction → store to Tier 3. Fire-and-forget."""
        try:
            loop = asyncio.get_event_loop()
            display = await loop.run_in_executor(None, lambda: self.session.load_display(session_id))

            real = [t for t in display if not t.get("text", "").startswith("[系统上下文]")]
            conversation_text = "\n".join(
                f"{'用户' if t['role'] == 'user' else '助手'}: {t['text'][:400]}"
                for t in real
            )
            if len(conversation_text) > 4000:
                conversation_text = conversation_text[:4000] + "\n[...已截断]"

            response = await client.messages.create(
                model=model, max_tokens=600, system=system_prompt,
                messages=[{"role": "user", "content": conversation_text}],
            )
            raw = "".join(b.text for b in response.content if b.type == "text").strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            data = json.loads(raw)

            facts = []
            for fact in (data.get("user_profile") or [])[:5]:
                if isinstance(fact, str) and fact.strip():
                    facts.append({"text": fact.strip(), "memory_type": "user_profile"})
            for pattern in (data.get("qa_pattern") or [])[:4]:
                if isinstance(pattern, str) and pattern.strip():
                    facts.append({"text": pattern.strip(), "memory_type": "qa_pattern"})
            for pref in (data.get("preference") or [])[:3]:
                if isinstance(pref, str) and pref.strip():
                    facts.append({"text": pref.strip(), "memory_type": "preference"})
            summary = (data.get("summary") or "").strip()
            if summary:
                facts.append({"text": summary, "memory_type": "summary"})

            user_id = session_id
            await loop.run_in_executor(None, lambda: self.long_term.store_facts(user_id, facts))
            await loop.run_in_executor(None, lambda: self.session.mark_summarized(session_id))
            log.info("Extracted %d facts for session %s", len(facts), session_id[:8])
        except Exception as exc:
            log.debug("Fact extraction failed: %s", exc)

    async def _store_negative_reflection(
        self,
        session_id: str,
        user_message: str,
        messages: list,
        client,
        model: str,
    ) -> None:
        """Summarise the negative feedback via LLM and store as a reflection."""
        try:
            recent = messages[-6:] if len(messages) >= 6 else messages
            context = "\n".join(
                f"{'用户' if m['role']=='user' else '助手'}: "
                + (m["content"][:300] if isinstance(m.get("content"), str) else "[工具调用]")
                for m in recent
            )
            prompt = (
                "以下是一段对话，用户最后反馈说有问题。请用1-2句话总结出：助手犯了什么错误，以及下次应该怎么做。\n\n"
                + context
            )
            response = await client.messages.create(
                model=model, max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = "".join(b.text for b in response.content if b.type == "text").strip()
            if summary:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: self.reflection.store(session_id, summary, error_type="negative_feedback")
                )
        except Exception as exc:
            log.debug("Reflection store failed: %s", exc)


# Module-level singleton — imported by agent.py
memory_manager = MemoryManager()
