"""
AgentRunner — standalone agent loop, decoupled from FastAPI.

Yields event dicts that callers convert to SSE, print, or assert against.
Supports all tools including ask_client, which pauses the loop and surfaces
a question to the caller so the human can answer before the next turn.
"""

import asyncio
import json
from typing import AsyncGenerator, Callable


_STATUS_MSGS: dict[str, str] = {
    "carbon_score":      "🔍 正在查询企业碳评分数据…",
    "search_policies":   "📜 正在搜索政策库…",
    "get_policy_detail": "📋 正在获取政策详情…",
    "ask_client":        "❓ 需要向客户确认信息…",
}


class AgentRunner:
    """
    Reusable agent loop. Use from FastAPI, CLI, or tests:

        runner = AgentRunner(client, model, execute_tool)
        async for event in runner.run(messages, system_prompt, tools):
            ...  # handle event dict

    Event types:
      {"type": "token",      "content": "..."}          — streamed text chunk
      {"type": "status",     "content": "..."}          — tool-call status banner
      {"type": "ask_client", "question": "...",          — agent needs client input;
                             "field": "...",               loop is paused, caller should
                             "field_type": "..."}          surface question and resume
      {"type": "done"}                                   — turn complete
      {"type": "error",      "content": "..."}          — fatal error
    """

    def __init__(
        self,
        client,
        model: str,
        execute_tool: Callable[[str, dict], str],
        max_iterations: int = 10,
    ):
        self.client = client
        self.model = model
        self.execute_tool = execute_tool
        self.max_iterations = max_iterations

    async def run(
        self,
        messages: list,
        system_prompt: str,
        tools: list,
    ) -> AsyncGenerator[dict, None]:
        for iteration in range(self.max_iterations):
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                asked_client = False

                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        yield {"type": "token", "content": block.text}

                    elif block.type == "tool_use":
                        yield {"type": "status", "content": _STATUS_MSGS.get(
                            block.name, f"🔧 正在调用工具 {block.name}…"
                        )}

                        if block.name == "ask_client":
                            # Pause loop — surface question to caller
                            yield {
                                "type":       "ask_client",
                                "question":   block.input.get("question", ""),
                                "field":      block.input.get("field_name", ""),
                                "field_type": block.input.get("field_type", "text"),
                            }
                            # Provide a placeholder tool_result so the message
                            # history stays valid; the real answer arrives as
                            # the next user message via /chat.
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     "问题已发送给客户，等待回答。",
                            })
                            asked_client = True

                        else:
                            _block = block
                            try:
                                loop = asyncio.get_event_loop()
                                result_str = await loop.run_in_executor(
                                    None,
                                    lambda: self.execute_tool(_block.name, _block.input),
                                )
                                yield {"type": "status", "content": "✅ 数据获取完成，正在生成分析…"}
                            except Exception as err:
                                result_str = f"错误：{err}"
                                yield {"type": "status", "content": f"❌ 查询失败：{err}"}

                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     result_str,
                            })

                messages.append({"role": "user", "content": tool_results})

                if asked_client:
                    # End this SSE turn; loop resumes when user replies via /chat
                    yield {"type": "done"}
                    return

            else:
                # end_turn — stream final text and finish
                text = "".join(
                    block.text for block in response.content if block.type == "text"
                )
                messages.append({"role": "assistant", "content": text})

                words = text.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield {"type": "token", "content": chunk}
                    await asyncio.sleep(0.018)

                yield {"type": "done"}
                return

        yield {"type": "error", "content": "已达最大迭代次数限制，对话终止。请尝试重新提问。"}
