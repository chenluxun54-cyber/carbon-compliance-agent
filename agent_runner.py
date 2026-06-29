"""
AgentRunner — standalone agent loop, decoupled from FastAPI.

First call uses streaming so the first token arrives immediately (prevents
MiniMax initial-response timeouts). Follow-up calls after tool execution use
messages.create() — MiniMax's Anthropic-compatible endpoint has issues with
streaming for tool-result turns.

Yields event dicts that callers convert to SSE, print, or assert against.
"""

import asyncio
import json
import re
from typing import AsyncGenerator, Callable


def _extract_ts_record_data(text: str):
    """Parse MiniMax TypeScript-style tool calls from text response.

    MiniMax sometimes outputs `functions.record_data({...})` as a code block
    instead of using the tool-use API. This extracts the args so we can call
    the tool ourselves.
    """
    m = re.search(r'functions\.record_data\((\{.*?\})\)', text, re.DOTALL)
    if not m:
        return None
    args_str = m.group(1)
    # Quote unquoted JS object keys: { key: "v" } → { "key": "v" }
    args_str = re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', args_str)
    try:
        return json.loads(args_str)
    except (json.JSONDecodeError, Exception):
        return None


def _strip_ts_tool_calls(text: str) -> str:
    """Remove TypeScript function-call code blocks from text before showing to user."""
    cleaned = re.sub(
        r'```(?:typescript)?\s*\nfunctions\.\w+\(.*?\)\s*\n```',
        '', text, flags=re.DOTALL,
    )
    # Also strip bare (non-fenced) typescript tool calls
    cleaned = re.sub(r'```typescript\s*functions\.record_data\(.*?\)\s*```', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()


_STATUS_MSGS: dict[str, str] = {
    "carbon_score":        "🔍 正在查询企业碳评分数据…",
    "search_policies":     "📜 正在搜索政策库…",
    "get_policy_detail":   "📋 正在获取政策详情…",
    "ask_client":          "❓ 需要向客户确认信息…",
    "start_product_calc":  "🧮 正在启动产品碳足迹计算…",
    "finalize_footprint":  "⚙️ 正在计算碳排放量…",
    "record_data":         "📝 记录数据…",
}

# Only show "数据获取完成" when real user-data is retrieved or calculated
_DATA_TOOLS = {"carbon_score", "finalize_footprint"}


def _sanitize_messages(messages: list) -> list:
    """
    Remove orphaned tool_result entries that have no matching tool_use block
    in the preceding assistant message.  This can happen when ts_fallback IDs
    were written into session history by an older version of the code.
    Handles both pure tool_result messages and mixed-content messages.
    Mutates the list in-place and returns it.
    """
    # Build a set of all tool_use IDs that appear in assistant messages
    valid_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        valid_ids.add(block["id"])

    # Strip orphaned tool_result blocks from any user message (pure or mixed)
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            ):
                cleaned = [
                    b for b in content
                    if not (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and b.get("tool_use_id") not in valid_ids
                    )
                ]
                if not cleaned:
                    messages.pop(i)
                    continue  # don't increment
                elif len(cleaned) != len(content):
                    messages[i] = {**msg, "content": cleaned}
        i += 1
    return messages


class AgentRunner:
    def __init__(
        self,
        client,
        model: str,
        execute_tool: Callable[[str, dict], str],
        max_iterations: int = 10,
        force_blocking: bool = False,
        tool_choice: dict = None,
    ):
        self.client = client
        self.model = model
        self.execute_tool = execute_tool
        self.max_iterations = max_iterations
        self.force_blocking = force_blocking
        self.tool_choice = tool_choice

    async def _call_streaming(self, messages, system_prompt, tools):
        """First call: stream tokens as they arrive."""
        kwargs = {}
        if self.tool_choice:
            kwargs["tool_choice"] = self.tool_choice
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
            **kwargs,
        ) as stream:
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        yield {
                            "type": "status",
                            "content": _STATUS_MSGS.get(
                                block.name, f"🔧 正在调用工具 {block.name}…"
                            ),
                        }
                elif etype == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta" and delta.text:
                        yield {"type": "token", "content": delta.text}
            final = await stream.get_final_message()
        yield {"__final__": final}

    async def _call_blocking(self, messages, system_prompt, tools):
        """Follow-up calls: non-streaming create() — more reliable with tool results."""
        kwargs = {}
        if self.tool_choice:
            kwargs["tool_choice"] = self.tool_choice
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
            **kwargs,
        )
        # Yield text tokens, stripping any TypeScript tool-call code blocks
        for block in response.content:
            if block.type == "text" and block.text:
                clean = _strip_ts_tool_calls(block.text)
                if clean:
                    yield {"type": "token", "content": clean}
        yield {"__final__": response}

    async def run(
        self,
        messages: list,
        system_prompt: str,
        tools: list,
    ) -> AsyncGenerator[dict, None]:
        _sanitize_messages(messages)  # drop any orphaned ts_fallback tool_results
        for iteration in range(self.max_iterations):
            final_message = None

            caller = self._call_blocking if self.force_blocking or iteration > 0 else self._call_streaming
            async for event in caller(messages, system_prompt, tools):
                if "__final__" in event:
                    final_message = event["__final__"]
                else:
                    yield event

            if final_message is None:
                yield {"type": "error", "content": "未收到模型响应，请重试。"}
                return

            if final_message.stop_reason == "tool_use":
                # Convert SDK ContentBlock objects to plain dicts so json.dumps works
                messages.append({
                    "role": "assistant",
                    "content": [block.model_dump() for block in final_message.content],
                })
                tool_results = []
                asked_client = False

                for block in final_message.content:
                    if block.type == "tool_use":
                        if block.name == "ask_client":
                            yield {
                                "type":       "ask_client",
                                "question":   block.input.get("question", ""),
                                "field":      block.input.get("field_name", ""),
                                "field_type": block.input.get("field_type", "text"),
                            }
                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     "问题已发送给客户，等待回答。",
                            })
                            asked_client = True

                        elif block.name == "record_data":
                            _block = block
                            auto_calculated = False
                            try:
                                result_str = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: self.execute_tool(_block.name, _block.input),
                                )
                                prog = json.loads(result_str)
                                yield {
                                    "type":           "progress",
                                    "collected":      prog.get("collected", 0),
                                    "total":          prog.get("total", 5),
                                    "all_required":   prog.get("all_required", False),
                                    "missing_labels": prog.get("missing_labels", []),
                                }
                                if prog.get("auto_calculated"):
                                    auto_calculated = True
                                    yield {"type": "status", "content": "✅ 数据收集完毕，碳足迹计算完成！"}
                            except Exception as err:
                                result_str = f"错误：{err}"

                            tool_results.append({
                                "type":        "tool_result",
                                "tool_use_id": block.id,
                                "content":     result_str,
                            })

                            # Emit summary text directly from result_summary — no extra LLM round-trip
                            if auto_calculated:
                                rs = prog.get("result_summary", {})
                                if rs and rs.get("status") == "calculation_complete":
                                    summary = (
                                        f"✅ 计算完成！\n"
                                        f"**{rs.get('product_name', '产品')}** 每件碳足迹："
                                        f"**{rs.get('total_kgco2e', 0)} kg CO₂e**\n"
                                        f"相当于驾驶普通燃油车行驶 {rs.get('analogy_km', 0)} 公里的排放量（按 0.22 kgCO₂e/km 测算）。\n"
                                        f"最大排放来源：{rs.get('hotspot', '未知')}，"
                                        f"占 {rs.get('hotspot_pct', 0)}%。\n"
                                        f"报告已生成，点击下方按钮即可下载。"
                                    )
                                    yield {"type": "token", "content": summary}
                                messages.append({"role": "user", "content": tool_results})
                                yield {"type": "done"}
                                return

                        else:
                            _block = block
                            try:
                                result_str = await asyncio.get_event_loop().run_in_executor(
                                    None,
                                    lambda: self.execute_tool(_block.name, _block.input),
                                )
                                # Sub-agent switch: exit immediately so caller can launch it
                                # without MiniMax looping on start_product_calc
                                try:
                                    payload = json.loads(result_str)
                                    if isinstance(payload, dict) and payload.get("__sub_agent__"):
                                        tool_results.append({
                                            "type":        "tool_result",
                                            "tool_use_id": block.id,
                                            "content":     result_str,
                                        })
                                        messages.append({"role": "user", "content": tool_results})
                                        yield {"type": "done"}
                                        return
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                                if _block.name in _DATA_TOOLS:
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
                    yield {"type": "done"}
                    return

            else:
                text = "".join(
                    b.text for b in final_message.content if b.type == "text"
                )

                # MiniMax fallback: detect TypeScript-style record_data calls in text
                ts_args = _extract_ts_record_data(text)
                if ts_args is not None:
                    try:
                        _args = ts_args
                        result_str = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self.execute_tool("record_data", _args)
                        )
                        prog = json.loads(result_str)
                        yield {
                            "type": "progress",
                            "collected": prog.get("collected", 0),
                            "total": prog.get("total", 5),
                            "all_required": prog.get("all_required", False),
                            "missing_labels": prog.get("missing_labels", []),
                        }
                        if prog.get("auto_calculated"):
                            yield {"type": "status", "content": "✅ 数据收集完毕，碳足迹计算完成！"}
                            rs = prog.get("result_summary", {})
                            if rs and rs.get("status") == "calculation_complete":
                                summary = (
                                    f"✅ 计算完成！\n"
                                    f"**{rs.get('product_name', '产品')}** 每件碳足迹："
                                    f"**{rs.get('total_kgco2e', 0)} kg CO₂e**\n"
                                    f"相当于驾驶普通燃油车行驶 {rs.get('analogy_km', 0)} 公里的排放量（按 0.22 kgCO₂e/km 测算）。\n"
                                    f"最大排放来源：{rs.get('hotspot', '未知')}，"
                                    f"占 {rs.get('hotspot_pct', 0)}%。\n"
                                    f"报告已生成，点击下方按钮即可下载。"
                                )
                                yield {"type": "token", "content": summary}
                            # Don't append ts_fallback tool_result — it has no matching
                            # tool_use block and would corrupt the session history.
                            # We're returning immediately so no further LLM turn needed.
                            yield {"type": "done"}
                            return
                    except Exception:
                        pass

                messages.append({"role": "assistant", "content": text})
                yield {"type": "done"}
                return

        yield {"type": "error", "content": "已达最大迭代次数限制，请尝试重新提问。"}
