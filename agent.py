"""
双碳 Agent — FastAPI 后端
启动：cd carbon_skill && uvicorn agent:app --reload --port 8000

MODEL_PROVIDER=anthropic  → Claude (默认)
MODEL_PROVIDER=minimax    → MiniMax（Anthropic-compatible endpoint）
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

from agent_runner import AgentRunner

# ── 确保从 carbon_skill 目录运行（让 DataLoader 能找到 xlsx 文件）──
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from data_loader import DataLoader
from scorer import CarbonScorer
from policies import POLICIES

# ── Provider 配置 ─────────────────────────────────────────────────
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "anthropic").lower()

PROVIDERS = {
    "anthropic": {
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "base_url": None,
        "model": "claude-sonnet-4-6",
    },
    "minimax": {
        "api_key": os.environ.get("MINIMAX_API_KEY", ""),
        "base_url": "https://api.minimaxi.com/anthropic",
        "model": os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01"),
    },
}

cfg = PROVIDERS[MODEL_PROVIDER]

client = anthropic.AsyncAnthropic(
    api_key=cfg["api_key"],
    **({"base_url": cfg["base_url"]} if cfg["base_url"] else {}),
)

# ── 预初始化数据组件 ──────────────────────────────────────────────
_loader = DataLoader()
_scorer = CarbonScorer()

# ── System Prompt ─────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一位专业的双碳（碳达峰、碳中和）咨询顾问 Agent。

你具备以下能力：
1. 解答碳排放、碳交易、碳中和政策、ESG 相关问题
2. 使用 carbon_score 工具查询数据库中企业的碳表现评分
3. 使用 search_policies 工具搜索全球碳政策库
4. 使用 get_policy_detail 工具获取政策详情和企业合规案例

【carbon_score 工具使用时机】
- 用户明确询问某企业的碳评分、碳表现、碳数据时
- 用户提供了企业ID（格式：COMP_XXX）时
- 如果用户未提供企业ID或年度，请先询问
- 数据库中可用企业ID：COMP_001 ~ COMP_010，年度：2024

【search_policies 工具使用时机】
- 用户询问某行业适用哪些碳政策时，传入 industry 参数
- 用户询问某地区（欧盟/中国/全球）的政策时，传入 jurisdiction 参数
- 用户泛问"有哪些碳政策"时，可不传参数获取全部列表

【get_policy_detail 工具使用时机】
- 用户询问某具体政策的详细内容时，必须调用此工具获取完整信息
- 解释任何政策时，务必引用工具返回的真实企业合规案例，用具体行动和数据让说明生动易懂
- 若已知当前企业所在行业，主动推荐最相关政策

回答要求：
- 全程使用专业简洁的中文
- 解释政策时必须结合企业案例，帮助客户理解如何在实践中落实
- 向特定行业企业推荐政策时，优先推荐与其行业强相关的政策
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
    },
    {
        "name": "search_policies",
        "description": "搜索全球碳政策库，可按关键词、行业或地区筛选。返回匹配的政策列表（含名称、地区、摘要）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如 CBAM、碳市场、可再生能源"
                },
                "industry": {
                    "type": "string",
                    "description": "行业名称，如：钢铁、电力、化工、水泥、制造业、金融"
                },
                "jurisdiction": {
                    "type": "string",
                    "description": "地区，可选值：全球、欧盟、中国"
                }
            }
        }
    },
    {
        "name": "get_policy_detail",
        "description": "获取指定政策的完整详情，包含关键合规要求和真实企业合规案例。解释具体政策时必须调用此工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "policy_id": {
                    "type": "string",
                    "description": "政策ID，如 PARIS_AGREEMENT、EU_ETS、CBAM、CN_ETS、SBTI 等"
                }
            },
            "required": ["policy_id"]
        }
    },
    {
        "name": "ask_client",
        "description": "当你需要客户提供公开数据中没有的信息时调用此工具。例如：内部碳排放目标、认证情况、未披露的能耗数据等。调用后对话将暂停等待客户回答。",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "向客户提出的具体问题，语言简洁清晰"
                },
                "field_name": {
                    "type": "string",
                    "description": "字段标识符，如 carbon_target、certification_type、renewable_ratio"
                },
                "field_type": {
                    "type": "string",
                    "enum": ["text", "number", "choice", "yesno"],
                    "description": "回答类型：text=文本, number=数值, choice=多选一, yesno=是/否"
                }
            },
            "required": ["question", "field_name", "field_type"]
        }
    }
]

# ── Gap Analysis System Prompt ───────────────────────────────────
GAP_ANALYSIS_SYSTEM_PROMPT = """你是一位资深双碳合规顾问，专门为企业生成合规差距分析与改进路线图。

你会收到一份企业的六维度碳评分 JSON，格式包含：
- total_score / 100 总分
- dimensions 数组：每个维度的 score、max_score、percentage、各指标的 score / percentile / missing
- company_id、company_name、industry、report_year、sample_size

你的任务：
1. 识别最薄弱的 2~3 个维度（percentage 最低）
2. 针对薄弱维度，调用 search_policies 找到最相关的政策（传入 industry 参数）
3. 对关键政策调用 get_policy_detail 获取具体合规要求，供路线图参考
4. 输出一份结构清晰的中文 Markdown 分析报告，包含以下四个部分：

---

## 📊 总体评价
- 总分、行业百分位、与行业标杆差距（简明 1~2 段）

## ⚠️ 主要差距
用 Markdown 表格列出最薄弱的 2~3 个维度：
| 维度 | 当前得分 | 满分 | 达成率 | 距 80% 达标线差距 | 最弱指标 |
（最弱指标：percentile 最低的 1~2 个，说明其数值含义）

## 🗺️ 改进路线图
对每个薄弱维度给出具体改进建议，格式：
**[维度名]**
- 具体行动：（2~3 条可落地的措施，越量化越好）
- 预期提分：（保守估计可提升多少分）
- 建议时间表：（短期 <6个月 / 中期 6~18个月 / 长期 >18个月）
- 参考政策：（列出工具返回的相关政策名称）

## 📜 关键合规政策
列出与本企业最相关的 2~3 条政策，每条包含：政策名、适用原因（1句话）

---

输出要求：
- 全程使用中文
- 数据引用必须来自输入的 JSON，不要编造百分位或分数
- 路线图措施必须具体可操作，避免空洞建议
- 篇幅控制在 600~900 字之间
"""

# ── 会话存储（内存）─────────────────────────────────────────────
sessions: dict[str, list] = {}


def execute_carbon_score(company_id: str, report_year: int) -> str:
    data = _loader.fetch(company_id, report_year)
    result = _scorer.score(data)
    return json.dumps(result, ensure_ascii=False, indent=2)


def execute_search_policies(keyword: str = None, industry: str = None, jurisdiction: str = None) -> str:
    results = POLICIES
    if jurisdiction:
        results = [p for p in results if p["jurisdiction"] == jurisdiction]
    if industry:
        results = [p for p in results if "all" in p["industries"] or industry in p["industries"]]
    if keyword:
        kw = keyword.lower()
        results = [p for p in results if
                   kw in p["name"].lower() or kw in p["summary"].lower()
                   or any(kw in t for t in p["tags"])]
    return json.dumps([{
        "id": p["id"], "name": p["name"], "jurisdiction": p["jurisdiction"],
        "category": p["category"], "summary": p["summary"][:120] + "…",
        "industries": p["industries"]
    } for p in results], ensure_ascii=False, indent=2)


def execute_get_policy_detail(policy_id: str) -> str:
    policy = next((p for p in POLICIES if p["id"] == policy_id), None)
    if not policy:
        return json.dumps({"error": f"未找到政策 {policy_id}，请检查ID是否正确"}, ensure_ascii=False)
    return json.dumps(policy, ensure_ascii=False, indent=2)


def execute_tool(tool_name: str, inputs: dict) -> str:
    if tool_name == "carbon_score":
        return execute_carbon_score(inputs["company_id"], inputs["report_year"])
    elif tool_name == "search_policies":
        return execute_search_policies(
            keyword=inputs.get("keyword"),
            industry=inputs.get("industry"),
            jurisdiction=inputs.get("jurisdiction"),
        )
    elif tool_name == "get_policy_detail":
        return execute_get_policy_detail(inputs["policy_id"])
    return json.dumps({"error": f"未知工具: {tool_name}"})


# ── AgentRunner 单例（依赖 execute_tool，须在其后定义）──────────
_runner = AgentRunner(client=client, model=cfg["model"], execute_tool=execute_tool)


# ── Agent 主循环（SSE 流式生成器）───────────────────────────────
async def agent_stream(session_id: str, user_message: str):
    if not cfg["api_key"]:
        key_var = "ANTHROPIC_API_KEY" if MODEL_PROVIDER == "anthropic" else "MINIMAX_API_KEY"
        yield f"data: {json.dumps({'type': 'error', 'content': f'未设置 {key_var} 环境变量。'})}\n\n"
        return

    if session_id not in sessions:
        sessions[session_id] = []

    messages = sessions[session_id]
    messages.append({"role": "user", "content": user_message})

    try:
        async for event in _runner.run(messages, SYSTEM_PROMPT, TOOLS):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as e:
        err_msg = str(e)
        if "authentication" in err_msg.lower() or "401" in err_msg:
            err_msg = "API Key 无效，请检查环境变量后重启服务。"
        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"


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
    return {"provider": MODEL_PROVIDER, "model": cfg["model"]}


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


@app.get("/policies")
async def list_policies(keyword: str = None, industry: str = None, jurisdiction: str = None):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: execute_search_policies(keyword, industry, jurisdiction)
    )
    return json.loads(result)


@app.get("/policies/{policy_id}")
async def get_policy(policy_id: str):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: execute_get_policy_detail(policy_id)
    )
    return json.loads(result)


# ── Gap Analysis Stream ──────────────────────────────────────────
async def gap_analysis_stream(score_data: dict):
    if not cfg["api_key"]:
        key_var = "ANTHROPIC_API_KEY" if MODEL_PROVIDER == "anthropic" else "MINIMAX_API_KEY"
        yield f"data: {json.dumps({'type': 'error', 'content': f'未设置 {key_var} 环境变量。'})}\n\n"
        return

    user_msg = (
        f"请对以下企业的碳评分数据进行合规差距分析，并生成改进路线图。\n\n"
        f"```json\n{json.dumps(score_data, ensure_ascii=False, indent=2)}\n```"
    )
    messages = [{"role": "user", "content": user_msg}]

    try:
        async for event in _runner.run(messages, GAP_ANALYSIS_SYSTEM_PROMPT, TOOLS):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as e:
        err_msg = str(e)
        if "authentication" in err_msg.lower() or "401" in err_msg:
            err_msg = "API Key 无效，请检查环境变量后重启服务。"
        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"


@app.post("/gap_analysis")
async def gap_analysis_endpoint(request: Request):
    body = await request.json()
    company_id  = body.get("company_id", "").strip()
    report_year = int(body.get("report_year", 0))
    if not company_id or not report_year:
        return {"error": "缺少 company_id 或 report_year"}
    loop = asyncio.get_event_loop()
    raw   = await loop.run_in_executor(None, lambda: _loader.fetch(company_id, report_year))
    score = await loop.run_in_executor(None, lambda: _scorer.score(raw))
    return StreamingResponse(
        gap_analysis_stream(score),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
