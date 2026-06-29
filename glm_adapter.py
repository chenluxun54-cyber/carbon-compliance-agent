"""
GLM adapter — wraps openai.AsyncOpenAI (Zhipu's OpenAI-compatible endpoint)
to expose the same interface that agent_runner.py expects from anthropic.AsyncAnthropic.

Translates:
  Anthropic tool defs → OpenAI function defs
  Anthropic message list → OpenAI message list
  OpenAI streaming chunks / blocking response → Anthropic-shaped objects
"""

import json
from openai import AsyncOpenAI


# ── Format converters ─────────────────────────────────────────────

def _ant_tools_to_openai(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in (tools or [])
    ]


def _ant_messages_to_openai(system: str, messages: list) -> list:
    result = []
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b["input"], ensure_ascii=False),
                    },
                }
                for b in content if b.get("type") == "tool_use"
            ]
            entry: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            result.append(entry)

        elif role == "user":
            tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if tool_results:
                for b in tool_results:
                    result.append({
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": b.get("content", ""),
                    })
                for b in text_blocks:
                    result.append({"role": "user", "content": b.get("text", "")})
            else:
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                result.append({"role": "user", "content": text})

    return result


# ── Mock Anthropic-shaped objects ─────────────────────────────────

class _TextBlock:
    type = "text"
    def __init__(self, text: str):
        self.text = text
    def model_dump(self):
        return {"type": "text", "text": self.text}


class _ToolUseBlock:
    type = "tool_use"
    def __init__(self, id: str, name: str, input: dict):
        self.id = id
        self.name = name
        self.input = input
    def model_dump(self):
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


class _FinalMessage:
    def __init__(self, content: list, stop_reason: str):
        self.content = content
        self.stop_reason = stop_reason


class _Event:
    def __init__(self, event_type: str, **attrs):
        self.type = event_type
        for k, v in attrs.items():
            setattr(self, k, v)


class _ContentBlockHeader:
    def __init__(self, block_type: str, name: str = None):
        self.type = block_type
        self.name = name


class _TextDelta:
    type = "text_delta"
    def __init__(self, text: str):
        self.text = text


# ── Streaming context manager ─────────────────────────────────────

class _GLMStream:
    """Async context manager + async iterable yielding fake Anthropic events."""

    def __init__(self, oai_client, model, system, tools, messages, extra_kwargs):
        self._oai = oai_client
        self._model = model
        self._system = system
        self._tools = tools
        self._messages = messages
        self._extra = extra_kwargs
        self._final: _FinalMessage | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def __aiter__(self):
        return self._generate()

    async def _generate(self):
        oai_msgs = _ant_messages_to_openai(self._system, self._messages)
        oai_tools = _ant_tools_to_openai(self._tools)

        kwargs: dict = {}
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        text_parts: list[str] = []
        partials: dict[int, dict] = {}  # index → {id, name, arguments}
        finish_reason = "stop"

        stream = await self._oai.chat.completions.create(
            model=self._model,
            messages=oai_msgs,
            max_tokens=4096,
            stream=True,
            **kwargs,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            if delta.content:
                text_parts.append(delta.content)
                yield _Event("content_block_delta", delta=_TextDelta(delta.content))

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in partials:
                        name = (tc.function.name or "") if tc.function else ""
                        partials[idx] = {"id": tc.id or "", "name": name, "arguments": ""}
                        yield _Event(
                            "content_block_start",
                            content_block=_ContentBlockHeader("tool_use", name=name),
                        )
                    else:
                        if tc.id:
                            partials[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                partials[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                partials[idx]["arguments"] += tc.function.arguments

        blocks: list = []
        full_text = "".join(text_parts)
        if full_text:
            blocks.append(_TextBlock(full_text))
        for idx in sorted(partials):
            p = partials[idx]
            try:
                inp = json.loads(p["arguments"]) if p["arguments"] else {}
            except json.JSONDecodeError:
                inp = {}
            blocks.append(_ToolUseBlock(p["id"], p["name"], inp))

        stop_reason = "tool_use" if partials else "end_turn"
        self._final = _FinalMessage(blocks, stop_reason)

    async def get_final_message(self) -> _FinalMessage:
        return self._final


# ── Blocking call ─────────────────────────────────────────────────

class _GLMMessages:
    def __init__(self, oai_client: AsyncOpenAI, model: str):
        self._oai = oai_client
        self._model = model

    def stream(self, model, max_tokens, system, tools, messages, **kwargs) -> _GLMStream:
        return _GLMStream(self._oai, model, system, tools, messages, kwargs)

    async def create(self, model, max_tokens, system, tools, messages, **kwargs) -> _FinalMessage:
        oai_msgs = _ant_messages_to_openai(system, messages)
        oai_tools = _ant_tools_to_openai(tools)

        oai_kwargs: dict = {}
        if oai_tools:
            oai_kwargs["tools"] = oai_tools
            oai_kwargs["tool_choice"] = "auto"

        response = await self._oai.chat.completions.create(
            model=model,
            messages=oai_msgs,
            max_tokens=max_tokens,
            **oai_kwargs,
        )

        choice = response.choices[0]
        msg = choice.message

        blocks: list = []
        if msg.content:
            blocks.append(_TextBlock(msg.content))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    inp = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    inp = {}
                blocks.append(_ToolUseBlock(tc.id, tc.function.name, inp))

        stop_reason = "tool_use" if msg.tool_calls else "end_turn"
        return _FinalMessage(blocks, stop_reason)


# ── Public client ─────────────────────────────────────────────────

class GLMClient:
    """Drop-in replacement for anthropic.AsyncAnthropic, routing to Zhipu GLM."""

    def __init__(self, api_key: str, base_url: str):
        oai = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.messages = _GLMMessages(oai, "")  # model passed per-call by agent_runner
