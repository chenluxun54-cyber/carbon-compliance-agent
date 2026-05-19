"""
双碳 Agent — FastAPI 后端
启动：cd carbon_skill && uvicorn agent:app --reload --port 8000
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

# ── API Key：优先读环境变量，本地开发回退到 skill_entry.py 的值 ──
ANTHROPIC_API_KEY = os.environ.get(
    "ANTHROPIC_API_KEY",
    "cr_5c871c335489e0bd9f0a5ae2a4f250b4bdf4e28e166b33d8d9747068e9ddc166"
)

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

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

# ── Tool 定义 ─────────────────────────────────────────────────────
TOOLS = [
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

# ── 会话存储（内存）─────────────────────────────────────────────
sessions: dict[str, list] = {}


def execute_carbon_score(company_id: str, report_year: int) -> str:
    """直接调用 DataLoader + CarbonScorer，不触发 Claude 子调用"""
    data = _loader.fetch(company_id, report_year)
    result = _scorer.score(data)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── Agent 主循环（SSE 流式生成器）───────────────────────────────
async def agent_stream(session_id: str, user_message: str):
    if session_id not in sessions:
        sessions[session_id] = []

    messages = sessions[session_id]
    messages.append({"role": "user", "content": user_message})

    full_response_text = ""

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            # 把当前助手消息加入历史
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    # 助手在调用工具前说的话，直接流出
                    yield f"data: {json.dumps({'type': 'token', 'content': block.text})}\n\n"
                    full_response_text += block.text

                elif block.type == "tool_use":
                    cid = block.input.get("company_id", "")
                    yr  = block.input.get("report_year", "")
                    yield f"data: {json.dumps({'type': 'status', 'content': f'🔍 正在查询 {cid}（{yr} 年）碳评分数据...'})}\n\n"

                    # 执行工具（同步，用 asyncio 跑到线程池避免阻塞）
                    loop = asyncio.get_event_loop()
                    try:
                        result_str = await loop.run_in_executor(
                            None,
                            lambda: execute_carbon_score(block.input["company_id"], block.input["report_year"])
                        )
                        yield f"data: {json.dumps({'type': 'status', 'content': '✅ 数据获取完成，正在生成分析...'})}\n\n"
                    except Exception as e:
                        result_str = f"错误：{str(e)}"
                        yield f"data: {json.dumps({'type': 'status', 'content': f'❌ 查询失败：{str(e)}'})}\n\n"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            # stop_reason == "end_turn"：流式输出最终文本
            text = ""
            for block in response.content:
                if block.type == "text":
                    text += block.text

            messages.append({"role": "assistant", "content": text})
            full_response_text += text

            # 按词流式输出，模拟打字效果
            words = text.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                await asyncio.sleep(0.018)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            break


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


@app.get("/companies")
async def get_companies():
    """返回数据库中所有企业列表（供前端下拉框使用）"""
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
    """直接返回某企业某年度的完整评分 JSON"""
    loop = asyncio.get_event_loop()
    data   = await loop.run_in_executor(None, lambda: _loader.fetch(company_id, year))
    result = await loop.run_in_executor(None, lambda: _scorer.score(data))
    return result


@app.get("/history/{company_id}")
async def get_history(company_id: str):
    """返回该企业最近3年的总分和维度得分（用于趋势图）"""
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
