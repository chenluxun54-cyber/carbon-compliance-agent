"""
detector.py
------------------
独立于产品本身(GLM)之外的"判断者"agent:实时监听每一条用户消息,
判断是否在描述一个bug,如果是,提炼成rubric格式(状态/动作/期望/实际/原话)
写进 bugs.md,供之后Claude Code直接读取作为权威的修复输入。

设计原则:
- 跟GLM完全不共享session/历史,避免"自己改自己作业"的判断偏差
- 同时参考trace.jsonl里的技术信号(点击/console报错/请求失败)作为辅助证据
- 写之前先看一眼已有的bugs.md,是同一个问题就累加"复现次数",不重复开条目
- 整个调用fire-and-forget,内部吞掉所有异常——detector出问题绝不能拖垮或
  打断产品本身正常回复用户的流程

集成方式(在你现有处理用户消息的地方,比如收到消息、转发给GLM做SSE流式
回复的那个函数里):

    import asyncio
    from detector import evaluate_and_log

    asyncio.create_task(evaluate_and_log(
        user_message=这条用户消息的纯文本,
        recent_messages=最近几轮对话,格式 [{"role": "user"/"assistant", "content": "纯文本"}],
    ))

不要 await 这个调用——fire-and-forget,否则会拖慢产品本身的响应速度。

注意:现在GLM那边自己写markdown记录错误的逻辑可以下线了,bugs.md由这个
detector统一维护,避免两边各写一份、互相打架。

跑独立冒烟测试(不依赖FastAPI,先确认detector本身工作正常再接入):
    python detector.py
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from anthropic import AsyncAnthropic

# ---- 配置 ----
# 这个调用跑在每一条消息上,优先选快且便宜的模型;如果实测发现漏判/误判
# 偏多,换成 "claude-sonnet-4-6" 即可,接口完全一样
DETECTOR_MODEL = "claude-haiku-4-5-20251001"
TRACE_FILE = Path("trace.jsonl")
BUGS_FILE = Path("bugs.md")
TRACE_WINDOW_SECONDS = 30          # 往前看多少秒内的trace事件
MAX_CONTEXT_MESSAGES = 8           # 往前带几条对话消息作为上下文
MAX_EXISTING_BUGS_FOR_DEDUP = 20   # 去重判断最多带几条历史bug摘要

_client = AsyncAnthropic()
_bugs_lock = asyncio.Lock()

_TOOL = {
    "name": "log_bug_report",
    "description": (
        "判断用户这条消息是否在描述产品的一个具体问题/bug。"
        "只有用户明确指出'结果不对/行为有问题/应该是怎样而不是怎样'才算,"
        "单纯提问、正常使用、或正面反馈都不算bug。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_bug_report": {
                "type": "boolean",
                "description": "这条消息是否在描述一个具体的产品问题",
            },
            "duplicate_of": {
                "type": "string",
                "description": (
                    "如果跟下面给出的已有bug列表里某一条本质是同一个问题,"
                    "填那一条的编号(比如'003');如果不是重复问题,留空字符串"
                ),
            },
            "state": {
                "type": "string",
                "description": "问题发生前的状态/上下文,一句话说清楚",
            },
            "action": {
                "type": "string",
                "description": "触发问题的具体动作,一句话说清楚",
            },
            "expected": {
                "type": "string",
                "description": "期望的正确行为/结果",
            },
            "actual": {
                "type": "string",
                "description": "实际发生的、有问题的行为/结果",
            },
            "original_quote": {
                "type": "string",
                "description": "用户原话,逐字保留,不要转述、不要润色",
            },
        },
        "required": ["is_bug_report"],
    },
}


def _read_recent_trace(window_seconds: int = TRACE_WINDOW_SECONDS) -> list[dict]:
    """读trace.jsonl里最近window_seconds秒内的事件;读不到就返回空列表,不报错"""
    if not TRACE_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    events: list[dict] = []
    try:
        lines = TRACE_FILE.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-200:]:  # 只看最近200行,避免文件变大后每次全量扫描
            try:
                ev = json.loads(line)
                ts = datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    events.append(ev)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except Exception:
        return []
    return events


def _load_existing_bugs() -> list[dict]:
    """解析bugs.md,返回未修复的 [{id, state, action}, ...] 供去重判断用。
    已标记 '修复状态: fixed' 的条目不参与去重——若同一问题再次出现应开新条目。"""
    if not BUGS_FILE.exists():
        return []
    text = BUGS_FILE.read_text(encoding="utf-8")
    # 每个 Bug 块以 "## Bug #NNN" 开头，到下一个 ## 或文件末尾结束
    blocks = re.split(r"(?=## Bug #\d+)", text)
    results = []
    for block in blocks:
        id_m = re.search(r"## Bug #(\d+)", block)
        if not id_m:
            continue
        bug_id = id_m.group(1)
        # 跳过已修复条目
        if re.search(r"^- 修复状态: fixed", block, re.MULTILINE):
            continue
        state_m  = re.search(r"^- 状态: (.*)", block, re.MULTILINE)
        action_m = re.search(r"^- 动作: (.*)", block, re.MULTILINE)
        if state_m and action_m:
            results.append({"id": bug_id, "state": state_m.group(1), "action": action_m.group(1)})
    return results[-MAX_EXISTING_BUGS_FOR_DEDUP:]


def _next_bug_id() -> str:
    if not BUGS_FILE.exists():
        return "001"
    text = BUGS_FILE.read_text(encoding="utf-8")
    ids = [int(m) for m in re.findall(r"## Bug #(\d+)", text)]
    return f"{(max(ids) + 1) if ids else 1:03d}"


def _normalize_bug_id(raw: str) -> str:
    """把模型可能给出的'3' / '#003' / 'Bug 3' 之类统一成文件里实际用的'003'格式"""
    digits = re.sub(r"\D", "", raw or "")
    return f"{int(digits):03d}" if digits else ""


async def _bump_recurrence(bug_id: str, original_quote: str) -> None:
    """同一个bug又出现了一次:复现次数+1,把这次的原话追加进去,不新开条目"""
    text = BUGS_FILE.read_text(encoding="utf-8")
    pattern = rf"(## Bug #{bug_id}\n(?:.*\n)*?- 复现次数: )(\d+)(\n)"
    m = re.search(pattern, text)
    if not m:
        return  # 没找到对应编号,放弃累加(极少数情况下模型编号给错了)
    new_count = int(m.group(2)) + 1
    note = f"  - 复现于 {datetime.now(timezone.utc).isoformat()}: \"{original_quote}\"\n"
    replacement = m.group(1) + str(new_count) + m.group(3) + note
    text = text[: m.start()] + replacement + text[m.end():]
    BUGS_FILE.write_text(text, encoding="utf-8")


async def _append_new_bug(entry: dict) -> None:
    bug_id = _next_bug_id()
    block = (
        f"## Bug #{bug_id}\n"
        f"- 状态: {entry.get('state', '')}\n"
        f"- 动作: {entry.get('action', '')}\n"
        f"- 期望: {entry.get('expected', '')}\n"
        f"- 实际: {entry.get('actual', '')}\n"
        f"- 原话: \"{entry.get('original_quote', '')}\"\n"
        f"- 首次记录: {datetime.now(timezone.utc).isoformat()}\n"
        f"- 复现次数: 1\n"
        f"- 修复状态: open\n\n"
    )
    with BUGS_FILE.open("a", encoding="utf-8") as f:
        f.write(block)


async def evaluate_and_log(user_message: str, recent_messages: list[dict] | None = None) -> None:
    """
    主入口:判断这条用户消息是否在描述bug,是的话写进bugs.md(新条目或累加复现次数)。
    Fire-and-forget设计——内部吞掉所有异常,绝不让detector的问题影响主产品。
    """
    try:
        trace_events = _read_recent_trace()
        existing_bugs = _load_existing_bugs()

        context_lines = [
            f"{m.get('role')}: {m.get('content')}"
            for m in (recent_messages or [])[-MAX_CONTEXT_MESSAGES:]
        ]

        prompt = (
            "最近几轮对话:\n" + ("\n".join(context_lines) or "(无)") + "\n\n"
            "本条待判断的用户消息:\n" + user_message + "\n\n"
            "同一时间窗口内的技术信号(trace.jsonl,可能为空):\n"
            + (json.dumps(trace_events, ensure_ascii=False) if trace_events else "(无)") + "\n\n"
            "已记录的历史bug列表(用于判断这次是不是同一个问题的重复反馈):\n"
            + (json.dumps(existing_bugs, ensure_ascii=False) if existing_bugs else "(无)")
        )

        response = await _client.messages.create(
            model=DETECTOR_MODEL,
            max_tokens=1024,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "log_bug_report"},
            messages=[{"role": "user", "content": prompt}],
        )

        result = next((b.input for b in response.content if b.type == "tool_use"), None)
        if not result or not result.get("is_bug_report"):
            return

        async with _bugs_lock:
            dup_id = _normalize_bug_id(result.get("duplicate_of", ""))
            if dup_id:
                await _bump_recurrence(dup_id, result.get("original_quote", user_message))
            else:
                await _append_new_bug(result)

    except Exception as e:
        # 静默失败:detector绝不能让产品本身的对话流程崩掉或卡住
        print(f"[detector] skipped due to error: {e}")


if __name__ == "__main__":
    # 独立冒烟测试,不依赖FastAPI: python detector.py
    # 跑之前确认 ANTHROPIC_API_KEY 环境变量已经设置好
    async def _smoke_test() -> None:
        print("测试1:一句明显的bug反馈 ...")
        await evaluate_and_log(
            user_message="这个产品的碳排放评分算出来是负数,明显不对,应该是正数才对吧",
            recent_messages=[
                {"role": "assistant", "content": "这款产品的碳排放评分是 -42 分。"},
            ],
        )
        print("测试2:一句正常使用消息,不应该被记成bug ...")
        await evaluate_and_log(
            user_message="谢谢,这个解释很清楚",
            recent_messages=[],
        )
        print("\n完成。当前 bugs.md 内容:\n")
        if BUGS_FILE.exists():
            print(BUGS_FILE.read_text(encoding="utf-8"))
        else:
            print("(bugs.md 还不存在——如果测试1理应判成bug却没有文件,说明上面可能报错了,往上翻看日志)")

    asyncio.run(_smoke_test())
