"""
双碳 Agent — FastAPI 后端
启动：cd carbon_skill && uvicorn agent:app --reload --port 8000

MODEL_PROVIDER=anthropic  → Claude (默认)
MODEL_PROVIDER=minimax    → MiniMax（abab6.5s-chat 等）
"""

import json
import os
import sys
import uuid
import asyncio
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ── 确保从 carbon_skill 目录运行（让 DataLoader 能找到 xlsx 文件）──
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from data_loader import DataLoader
from scorer import CarbonScorer

# ── Provider 配置 ─────────────────────────────────────────────────
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "anthropic").lower()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MINIMAX_API_KEY   = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_MODEL     = os.environ.get("MINIMAX_MODEL", "abab6.5s-chat")

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# MiniMax uses OpenAI-compatible API — import lazily so openai isn't required when using Anthropic
_openai_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(
            api_key=MINIMAX_API_KEY,
            base_url="https://api.minimax.chat/v1",
        )
    return _openai_client

# ── 预初始化数据组件 ──────────────────────────────────────────────
_loader = DataLoader()
_scorer = CarbonScorer()

# ── System Prompt ─────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一位专业的双碳（碳达峰、碳中和）咨询顾问 Agent。

你具备以下能力：
1. 解答碳排放、碳交易、碳中和政策、ESG 相关问题
2. 使用 carbon_score 工具查询数据库中企业的碳表现评分

使用 carbon_score 工具的时机：
- 用户明确询问某企业的碳评分、碳表现、碳数据时
- 用户提供了企业ID（格式：COMP_XXX）时
- 如果用户未提供企业ID或年度，请先询问

数据库中可用的企业ID示例：COMP_001 ~ COMP_010，年度：2024

回答要求：
- 全程使用专业简洁的中文
- 对工具返回的评分数据，请深入解读，给出洞察和建议
- 使用 Markdown 格式，适当使用标题、列表让回答结构清晰
"""

# ── Tool 定义（Anthropic 格式）────────────────────────────────────
TOOLS_ANTHROPIC = [
    {
        "name": "carbon_score",
        "description": "查询企业的碳表现评分数据。返回碳排放强度、能源结构、减碳表现等6个维度的得分及行业排名百分位。",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {
                    "type": "string",
                    "description": "企业ID，格式为 COMP_XXX，例如 COMP_001"
                },
                "report_year": {
                    "type": "integer",
                    "description": "报告年度，例如 2024"
                }
            },
            "required": ["company_id", "report_year"]
        }
    }
]

# ── Tool 定义（OpenAI/MiniMax 格式）──────────────────────────────
TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "carbon_score",
            "description": "查询企业的碳表现评分数据。返回碳排放强度、能源结构、减碳表现等6个维度的得分及行业排名百分位。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "企业ID，格式为 COMP_XXX，例如 COMP_001"
                    },
                    "report_year": {
                        "type": "integer",
                        "description": "报告年度，例如 2024"
                    }
                },
                "required": ["company_id", "report_year"]
            }
        }
    }
]

# ── 会话存储（内存）─────────────────────────────────────────────
sessions: dict[str, list] = {}


def execute_carbon_score(company_id: str, report_year: int) -> str:
    data = _loader.fetch(company_id, report_year)
    result = _scorer.score(data)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── Anthropic Agent 流 ────────────────────────────────────────────
async def agent_stream_anthropic(session_id: str, user_message: str):
    if not ANTHROPIC_API_KEY:
        yield f"data: {json.dumps({'type': 'error', 'content': '未设置 ANTHROPIC_API_KEY 环境变量。请运行：export ANTHROPIC_API_KEY=sk-ant-...'})}\n\n"
        return

    if session_id not in sessions:
        sessions[session_id] = []

    messages = sessions[session_id]
    messages.append({"role": "user", "content": user_message})

    try:
        while True:
            response = await anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS_ANTHROPIC,
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        yield f"data: {json.dumps({'type': 'token', 'content': block.text})}\n\n"
                    elif block.type == "tool_use":
                        cid = block.input.get("company_id", "")
                        yr  = block.input.get("report_year", "")
                        yield f"data: {json.dumps({'type': 'status', 'content': f'🔍 正在查询 {cid}（{yr} 年）碳评分数据...'})}\n\n"
                        loop = asyncio.get_event_loop()
                        try:
                            result_str = await loop.run_in_executor(
                                None,
                                lambda: execute_carbon_score(block.input["company_id"], block.input["report_year"])
                            )
                            yield f"data: {json.dumps({'type': 'status', 'content': '✅ 数据获取完成，正在生成分析...'})}\n\n"
                        except Exception as tool_err:
                            result_str = f"错误：{str(tool_err)}"
                            yield f"data: {json.dumps({'type': 'status', 'content': f'❌ 查询失败：{str(tool_err)}'})}\n\n"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })
                messages.append({"role": "user", "content": tool_results})

            else:
                text = ""
                for block in response.content:
                    if block.type == "text":
                        text += block.text
                messages.append({"role": "assistant", "content": text})
                words = text.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                    await asyncio.sleep(0.018)
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

    except Exception as e:
        err_msg = str(e)
        if "authentication" in err_msg.lower() or "401" in err_msg:
            err_msg = "API Key 无效。请运行：export ANTHROPIC_API_KEY=sk-ant-... 然后重启服务。"
        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"


# ── MiniMax Agent 流 ──────────────────────────────────────────────
async def agent_stream_minimax(session_id: str, user_message: str):
    if not MINIMAX_API_KEY:
        yield f"data: {json.dumps({'type': 'error', 'content': '未设置 MINIMAX_API_KEY 环境变量。请运行：export MINIMAX_API_KEY=your-key'})}\n\n"
        return

    if session_id not in sessions:
        sessions[session_id] = []

    messages = sessions[session_id]
    # MiniMax uses system message in the messages list
    if not messages:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append({"role": "user", "content": user_message})

    client = get_openai_client()

    try:
        while True:
            response = await client.chat.completions.create(
                model=MINIMAX_MODEL,
                max_tokens=4096,
                tools=TOOLS_OPENAI,
                tool_choice="auto",
                messages=messages,
            )

            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                messages.append(msg)
                for tool_call in msg.tool_calls:
                    fn_args = json.loads(tool_call.function.arguments)
                    cid = fn_args.get("company_id", "")
                    yr  = fn_args.get("report_year", "")
                    yield f"data: {json.dumps({'type': 'status', 'content': f'🔍 正在查询 {cid}（{yr} 年）碳评分数据...'})}\n\n"
                    loop = asyncio.get_event_loop()
                    try:
                        result_str = await loop.run_in_executor(
                            None,
                            lambda: execute_carbon_score(fn_args["company_id"], fn_args["report_year"])
                        )
                        yield f"data: {json.dumps({'type': 'status', 'content': '✅ 数据获取完成，正在生成分析...'})}\n\n"
                    except Exception as tool_err:
                        result_str = f"错误：{str(tool_err)}"
                        yield f"data: {json.dumps({'type': 'status', 'content': f'❌ 查询失败：{str(tool_err)}'})}\n\n"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    })

            else:
                text = msg.content or ""
                messages.append({"role": "assistant", "content": text})
                words = text.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                    await asyncio.sleep(0.018)
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                break

    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "authentication" in err_msg.lower():
            err_msg = "MiniMax API Key 无效。请运行：export MINIMAX_API_KEY=your-key 然后重启服务。"
        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"


# ── 统一入口：根据 MODEL_PROVIDER 分发 ───────────────────────────
async def agent_stream(session_id: str, user_message: str):
    if MODEL_PROVIDER == "minimax":
        async for chunk in agent_stream_minimax(session_id, user_message):
            yield chunk
    else:
        async for chunk in agent_stream_anthropic(session_id, user_message):
            yield chunk


# ── FastAPI 应用 ──────────────────────────────────────────────────
app = FastAPI(title="双碳 Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    session_id   = body.get("session_id") or str(uuid.uuid4())
    user_message = body.get("message", "").strip()

    if not user_message:
        return {"error": "消息不能为空"}

    return StreamingResponse(
        agent_stream(session_id, user_message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        }
    )


@app.post("/new_session")
async def new_session():
    sid = str(uuid.uuid4())
    sessions[sid] = []
    return {"session_id": sid}


@app.get("/provider")
async def get_provider():
    """返回当前使用的模型 Provider"""
    return {
        "provider": MODEL_PROVIDER,
        "model": MINIMAX_MODEL if MODEL_PROVIDER == "minimax" else "claude-sonnet-4-6",
    }


@app.get("/companies")
async def get_companies():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _loader._load_excel)
    df = _loader._company_df
    result: dict[str, dict] = {}
    for _, row in df[["company_id", "company_name", "industry", "report_year"]].iterrows():
        cid = row["company_id"]
        if cid not in result:
            result[cid] = {
                "company_id":   cid,
                "company_name": row["company_name"],
                "industry":     row["industry"],
                "years":        [],
            }
        result[cid]["years"].append(int(row["report_year"]))
    for v in result.values():
        v["years"] = sorted(v["years"])
    return list(result.values())


@app.get("/score/{company_id}/{year}")
async def get_score(company_id: str, year: int):
    loop = asyncio.get_event_loop()
    data   = await loop.run_in_executor(None, lambda: _loader.fetch(company_id, year))
    result = await loop.run_in_executor(None, lambda: _scorer.score(data))
    return result


@app.get("/history/{company_id}")
async def get_history(company_id: str):
    loop    = asyncio.get_event_loop()
    history = await loop.run_in_executor(None, lambda: _loader.fetch_history(company_id))
    out = []
    for data in history:
        scored = _scorer.score(data)
        out.append({
            "report_year": scored["report_year"],
            "total_score": scored["total_score"],
            "dimensions": [
                {"id": d["id"], "name": d["name"],
                 "score": d["score"], "max_score": d["max_score"]}
                for d in scored["dimensions"]
            ],
        })
    return sorted(out, key=lambda x: x["report_year"])


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
