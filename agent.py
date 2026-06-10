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
from calculator import (
    calc_scope2, calc_upstream_materials, calc_scope1_fuel,
    calc_transport, summarize_footprint,
)

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
5. 使用 start_product_calc 工具启动产品碳足迹计算，帮助用户计算单个产品的碳排放量

【start_product_calc 工具使用时机】
- 用户想计算某个产品的碳排放/碳足迹时，立即调用此工具
- 如果用户已经提供了产品描述，将其作为 product_hint 传入
- 调用后会启动专门的计算子智能体接管对话

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
_ASK_CLIENT_TOOL = {
    "name": "ask_client",
    "description": "当你需要客户提供公开数据中没有的信息时调用此工具。调用后对话将暂停等待客户回答。",
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
    _ASK_CLIENT_TOOL,
    {
        "name": "start_product_calc",
        "description": "启动产品碳足迹计算子智能体。当用户想要计算某个产品的碳排放量/碳足迹时调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_hint": {
                    "type": "string",
                    "description": "用户提到的产品描述（如有），作为子智能体的初始上下文"
                }
            }
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

# ── Sub-agent: 产品碳足迹计算 ────────────────────────────────────
CALC_SYSTEM_PROMPT = """你是一位专业的产品碳足迹计算顾问，帮助制造业企业计算单个产品的碳排放量。

你通过对话逐步收集所需数据，然后调用 finalize_footprint 工具输出计算结果。

【对话规则 — 严格遵守】
1. 每次只问一个问题，绝不同时提多个问题
2. 每个问题说明为什么需要这个信息（1句话即可）
3. 用户不知道某数据时，给出行业参考值供其选择，最终报告中标注"行业估算"
4. 在适当时机告知进度（如"还剩2个问题就可以计算了"）
5. 全程不使用专业术语（不说"范围一""排放因子""LCA"等）
6. 收集完所有必要数据后立即调用 finalize_footprint

【必须按顺序收集的信息】

第1步：产品 + 重量
→ ask_client："您想计算哪款产品的碳排放？单个产品大概多重（克或千克）？"
  （目的：确定计算基准，即"每件产品"）

第2步：工厂所在省份
→ ask_client："工厂在哪个省份或地区？"
  （原因：不同地区电网结构不同，影响用电的碳排放量）

第3步：生产用电量
→ ask_client："生产一件这样的产品，大概需要多少度电？"
  （原因：电力通常是制造业最大的碳排放来源）
  参考值：轻工产品 0.3-1度；电子产品 1-5度；重型机械 5-50度

第4步：主要原材料
→ ask_client："产品主要用了哪些材料？各用多少千克？"
  （原因：原材料通常占产品碳足迹的 50-80%）
  可识别材料：钢铁、铝、铜、塑料（PP/PE/ABS）、玻璃、纸板、木材、PCB电路板、锂电池、橡胶、陶瓷

第5步（可选）：直接燃料
→ ask_client："生产过程中有用到天然气、柴油或煤炭吗？"
  无或不确定则跳过（传0）

第6步（可选）：运输
→ ask_client："产品通常运多远交给客户？用什么运输方式（公路/铁路/海运）？"
  无或不确定则跳过（传0）

第7步：调用 finalize_footprint
→ 收集完以上数据后立即调用 finalize_footprint，传入所有参数

【处理用户已提供信息的情况】
如果用户在第一条消息中已经提供了部分信息，跳过对应步骤，直接询问下一个未知信息。

【禁止行为】
- 一次问多个问题
- 未收集完数据就调用 finalize_footprint
- 编造数据（不确定时使用参考值并标注）
"""

CALC_TOOLS = [
    _ASK_CLIENT_TOOL,
    {
        "name": "finalize_footprint",
        "description": "收集完所有数据后调用此工具进行碳排放计算，返回完整结果供你解释给用户。",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "产品名称"},
                "functional_unit": {"type": "string", "description": "计算基准，如 '每件（300g铝制水杯）'"},
                "electricity_kwh": {"type": "number", "description": "生产单件产品的用电量（度）"},
                "region": {"type": "string", "description": "工厂所在省份或地区，如 浙江、华东、全国平均"},
                "materials": {
                    "type": "array",
                    "description": "原材料列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kg": {"type": "number"}
                        },
                        "required": ["name", "kg"]
                    }
                },
                "fuel_type": {"type": "string", "description": "燃料类型，如 天然气_m3、柴油_L；无则传空字符串"},
                "fuel_quantity": {"type": "number", "description": "燃料用量；无则传0"},
                "transport_weight_kg": {"type": "number", "description": "产品重量（kg）"},
                "transport_distance_km": {"type": "number", "description": "运输距离（km）；无则传0"},
                "transport_mode": {"type": "string", "description": "运输方式：公路/铁路/海运/航空"},
                "assumptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本次计算中使用的假设或默认值（如有）"
                }
            },
            "required": ["product_name", "functional_unit", "electricity_kwh", "region", "materials"]
        }
    }
]

# ── 会话存储（内存）─────────────────────────────────────────────
sessions: dict[str, list] = {}
# Tracks which sessions are currently in calc sub-agent mode
session_states: dict[str, str] = {}  # session_id → "main" | "calc"
# Signals from execute_tool to agent_stream that sub-agent should start
_sub_agent_pending: dict[str, dict] = {}


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


def execute_finalize_footprint(inputs: dict) -> str:
    scope1 = calc_scope1_fuel(
        inputs.get("fuel_type", ""),
        float(inputs.get("fuel_quantity", 0)),
    )
    scope2 = calc_scope2(
        float(inputs["electricity_kwh"]),
        inputs["region"],
    )
    materials = calc_upstream_materials(inputs.get("materials", []))
    transport = calc_transport(
        float(inputs.get("transport_weight_kg", 0)),
        float(inputs.get("transport_distance_km", 0)),
        inputs.get("transport_mode", "公路"),
    )
    result = summarize_footprint(
        product_name=inputs["product_name"],
        functional_unit=inputs["functional_unit"],
        scope1=scope1,
        scope2=scope2,
        materials=materials,
        transport=transport,
        assumptions=inputs.get("assumptions", []),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


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
    elif tool_name == "finalize_footprint":
        return execute_finalize_footprint(inputs)
    elif tool_name == "start_product_calc":
        # Sentinel: agent_stream() detects this and switches to calc sub-agent
        return json.dumps({"__sub_agent__": "calc", "product_hint": inputs.get("product_hint", "")})
    return json.dumps({"error": f"未知工具: {tool_name}"})


# ── AgentRunner 单例（依赖 execute_tool，须在其后定义）──────────
_runner = AgentRunner(client=client, model=cfg["model"], execute_tool=execute_tool)
_calc_runner = AgentRunner(client=client, model=cfg["model"], execute_tool=execute_tool, max_iterations=20)


async def _run_calc_sub_agent(session_id: str, product_hint: str):
    """Run the product carbon footprint sub-agent on the session."""
    messages = sessions[session_id]
    # Inject product hint as the opening context for the sub-agent
    if product_hint:
        messages.append({"role": "user", "content": product_hint})
    else:
        messages.append({"role": "user", "content": "请开始收集产品碳足迹计算所需的信息。"})
    async for event in _calc_runner.run(messages, CALC_SYSTEM_PROMPT, CALC_TOOLS):
        yield event
    # Sub-agent finished (either ask_client pause or finalize_footprint done)
    # Reset state so next user message goes back to main agent
    if session_states.get(session_id) == "calc":
        session_states.pop(session_id, None)


# ── Agent 主循环（SSE 流式生成器）───────────────────────────────
async def agent_stream(session_id: str, user_message: str):
    if not cfg["api_key"]:
        key_var = "ANTHROPIC_API_KEY" if MODEL_PROVIDER == "anthropic" else "MINIMAX_API_KEY"
        yield f"data: {json.dumps({'type': 'error', 'content': f'未设置 {key_var} 环境变量。'})}\n\n"
        return

    if session_id not in sessions:
        sessions[session_id] = []

    messages = sessions[session_id]

    # Route to calc sub-agent if session is in calc mode
    if session_states.get(session_id) == "calc":
        messages.append({"role": "user", "content": user_message})
        try:
            async for event in _calc_runner.run(messages, CALC_SYSTEM_PROMPT, CALC_TOOLS):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            # After sub-agent finishes its turn, check if it's done
            # (ask_client pauses → stays in calc mode; finalize_footprint end_turn → exit calc mode)
            last_assistant = next(
                (m for m in reversed(messages) if m.get("role") == "assistant"),
                None,
            )
            if last_assistant:
                content = last_assistant.get("content", "")
                text = content if isinstance(content, str) else ""
                # If the last assistant turn produced a final answer (not ask_client), exit calc mode
                if text and session_states.get(session_id) == "calc":
                    session_states.pop(session_id, None)
        except Exception as e:
            err_msg = str(e)
            if "authentication" in err_msg.lower() or "401" in err_msg:
                err_msg = "API Key 无效，请检查环境变量后重启服务。"
            yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"
        return

    messages.append({"role": "user", "content": user_message})

    try:
        sub_agent_triggered = False
        async for event in _runner.run(messages, SYSTEM_PROMPT, TOOLS):
            # Detect sub-agent sentinel hidden inside a status event
            # (the runner already executed start_product_calc and got the sentinel JSON back)
            if not sub_agent_triggered:
                # Check if the last tool result in messages is a sub-agent sentinel
                last_user = next(
                    (m for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), list)),
                    None,
                )
                if last_user:
                    for tr in last_user["content"]:
                        if isinstance(tr, dict) and tr.get("type") == "tool_result":
                            try:
                                payload = json.loads(tr.get("content", "{}"))
                                if payload.get("__sub_agent__") == "calc":
                                    sub_agent_triggered = True
                                    hint = payload.get("product_hint", "")
                                    session_states[session_id] = "calc"
                                    break
                            except (json.JSONDecodeError, TypeError):
                                pass

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        # After main agent loop, start sub-agent if triggered
        if sub_agent_triggered:
            yield f"data: {json.dumps({'type': 'status', 'content': '🧮 启动产品碳足迹计算...'})}\n\n"
            async for event in _run_calc_sub_agent(session_id, hint):
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
