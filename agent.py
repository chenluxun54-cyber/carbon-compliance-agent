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
    calc_transport, calc_packaging, calc_end_of_life, summarize_footprint,
)
from report_template import generate_report_html
from memory.manager import memory_manager

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
_AUTO_NAME_SYSTEM = (
    "你是一个对话命名助手。根据用户的第一条消息，用不超过10个汉字为这段对话起一个简洁的名称。"
    "只输出名称本身，不加标点，不加引号，不加任何解释。"
)

_MEMORY_EXTRACT_SYSTEM = """\
你是一个对话记忆提取助手。请仔细阅读以下用户与AI顾问的对话记录，提取有价值的持久化记忆。

输出严格遵守以下 JSON 格式，不得包含任何额外文字：
{
  "user_profile": ["<事实1>", "<事实2>"],
  "qa_pattern":   ["<问答模式1>"],
  "preference":   ["<偏好1>"],
  "summary":      "<本次对话的100字以内摘要>"
}

提取规则：
- user_profile：用户透露的身份信息（行业、公司、职位、产品、地区）；最多5条；无则空数组
- qa_pattern：用户反复询问或获得有效回答的问题模式，如"用户关心CBAM对钢铁出口的影响"；最多4条；无则空数组
- preference：用户明显的回答偏好（格式、简洁度、关注点）；最多3条；无则空数组
- summary：整段对话核心主题与结论，100字以内，供未来对话参考

重要：只提取对话中真实出现的信息，不推断或捏造；输出必须是合法JSON，不加代码块标记。\
"""


def _build_memory_context(session_id: str, query: str = "") -> str:
    """Build semantic memory context block (Tier 3 facts + Tier 4 reflections)."""
    try:
        return memory_manager.build_memory_block(session_id, query)
    except Exception:
        return ""


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
CALC_SYSTEM_PROMPT = """你是一位亲切的产品碳足迹计算助手，帮用户算清楚一件产品从生产到报废的碳排放量。

【工作方式】
- 全程用简单日常语言，不说"范围一""排放因子""LCA"等专业术语
- 每次只问一个问题
- 用户说了什么数据，立刻调用 record_data 工具记录下来
- record_data 返回后，在回复里告诉用户记录了什么、还缺什么
- 当 record_data 返回 "auto_calculated": true，系统已自动计算完成，用 result_summary 字段的数据向用户播报结果，格式见下方【计算完成回复格式】

【必填信息（5项，全部收集到才能计算）】
- 产品名称 + 每件重量（克或千克）
- 工厂所在省份/地区（不同省电网碳排放不同）
- 生产一件产品用多少度电
- 主要原材料和各自重量（千克）
  可识别材料：钢铁、铝、铜、锌、镍、塑料（PP/PE/ABS/PET/PC/PVC/PA/PU）、
  玻璃、纸板、木材、PCB、锂电池、碳纤维、橡胶、陶瓷、涤纶、羊毛、皮革等

【可选信息（用户回答"不知道"/"没有"/"跳过"就跳过，不要催）】
- 生产时用天然气/柴油/煤吗
- 产品发货距离和运输方式（公路/铁路/海运）
- 外包装材料和重量（瓦楞纸箱、PE薄膜、泡沫塑料等）
- 产品报废后怎么处理（填埋/焚烧/回收）

【用户说了一大堆信息时】
一次性提取所有能识别的字段，全部放进一个 record_data 调用里。

【回复格式（每次 record_data 后，还有缺失数据时）】
"记下了 ✅ [刚记录的内容简述]。还需要 N 项：[中文字段名]。[下一个问题]"
如果可选项没问过，在必填项都收集完之前适时询问可选项。

【计算完成回复格式（auto_calculated: true 时）】
直接用以下模板回复（数据来自 result_summary 字段）：
"✅ 计算完成！
**[产品名称]** 每件碳足迹：**[total_kgco2e] kg CO₂e**
相当于开车 [analogy_km] 公里的排放量。
最大排放来源：[hotspot]，占 [hotspot_pct]%。
报告已生成，点击下方按钮即可下载。"

【电量参考值（用户不确定时提供）】
轻工业品：0.3~1 度；电子产品：1~5 度；重型机械：5~50 度

【禁止行为】
- 一次提多个问题
- 编造数据（不确定时给参考值并在 assumptions 里标注"行业估算"）
"""

CALC_TOOLS = [
    {
        "name": "record_data",
        "description": "用户提供了任何碳足迹计算数据时立即调用，记录已收集的字段并返回当前进度。可同时传入多个字段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name":           {"type": "string",  "description": "产品名称"},
                "weight_kg":              {"type": "number",  "description": "每件重量（千克）"},
                "region":                 {"type": "string",  "description": "工厂所在省份或地区"},
                "electricity_kwh":        {"type": "number",  "description": "生产一件产品用电量（度）"},
                "materials": {
                    "type": "array",
                    "description": "原材料列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kg":   {"type": "number"}
                        },
                        "required": ["name", "kg"]
                    }
                },
                "fuel_type":              {"type": "string",  "description": "燃料类型，如 天然气_m3、柴油_L"},
                "fuel_quantity":          {"type": "number",  "description": "燃料用量"},
                "transport_distance_km":  {"type": "number",  "description": "运输距离（千米）"},
                "transport_mode":         {"type": "string",  "description": "运输方式：公路/铁路/海运/航空"},
                "packaging": {
                    "type": "array",
                    "description": "包装材料列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kg":   {"type": "number"}
                        },
                        "required": ["name", "kg"]
                    }
                },
                "end_of_life_method":     {"type": "string",  "description": "报废处置方式：填埋/焚烧/回收/混合/堆肥"},
                "recycled_pct":           {"type": "number",  "description": "回收比例 0-100，处置方式为混合时使用"}
            },
            "required": []
        }
    },
]

# ── 会话存储（内存）─────────────────────────────────────────────
sessions: dict[str, list] = {}
# Tracks which sessions are currently in calc sub-agent mode
session_states: dict[str, str] = {}  # session_id → "main" | "calc"
# Signals from execute_tool to agent_stream that sub-agent should start
_sub_agent_pending: dict[str, dict] = {}
# Stores the last finalize_footprint result per session (for report download)
calc_results: dict[str, dict] = {}
# Active session_id being processed by the calc sub-agent (for result storage)
_active_calc_session: dict[str, str] = {"current": ""}
# Accumulated data state per calc session
calc_data_state: dict[str, dict] = {}


def execute_carbon_score(company_id: str, report_year: int) -> str:
    data = _loader.fetch(company_id, report_year)
    result = _scorer.score(data)
    return json.dumps(result, ensure_ascii=False, indent=2)


def execute_record_data(session_id: str, inputs: dict) -> str:
    """Accumulate calc fields into session state, return progress JSON."""
    _DEFAULTS = {
        "product_name": "", "weight_kg": 0.0, "region": "",
        "electricity_kwh": 0.0, "materials": [],
        "fuel_type": "", "fuel_quantity": 0.0,
        "transport_distance_km": 0.0, "transport_mode": "公路",
        "packaging": [], "end_of_life_method": "", "recycled_pct": 0.0,
    }
    state = calc_data_state.setdefault(session_id, dict(_DEFAULTS))

    for k, v in inputs.items():
        if k not in _DEFAULTS:
            continue
        if k in ("materials", "packaging"):
            if v:
                existing = {m["name"] for m in state[k]}
                for item in v:
                    if item.get("name") and item["name"] not in existing:
                        state[k].append(item)
                        existing.add(item["name"])
        elif v is not None and v != "" and v != 0 and v != 0.0:
            state[k] = v

    # Auto-derive functional_unit for finalize_footprint
    if state["product_name"] and state["weight_kg"] and not state.get("functional_unit"):
        w = state["weight_kg"]
        label = f"{w*1000:.0f}g" if w < 1 else f"{w:.2f}kg".rstrip("0").rstrip(".")
        state["functional_unit"] = f"每件（{label} {state['product_name']}）"

    required = {
        "产品名称": bool(state["product_name"]),
        "重量":     state["weight_kg"] > 0,
        "工厂地区": bool(state["region"]),
        "生产用电量": state["electricity_kwh"] > 0,
        "主要原材料": len(state["materials"]) > 0,
    }
    collected = sum(required.values())
    missing_labels = [k for k, ok in required.items() if not ok]

    auto_result = None
    if collected == 5 and session_id not in calc_results:
        auto_result = _auto_finalize(session_id, state)

    out: dict = {
        "recorded":       [k for k, v in inputs.items() if k in _DEFAULTS],
        "collected":      collected,
        "total":          5,
        "all_required":   collected == 5,
        "missing_labels": missing_labels,
    }
    if auto_result:
        out["auto_calculated"] = True
        out["result_summary"] = auto_result
    return json.dumps(out, ensure_ascii=False)


def _auto_finalize(session_id: str, state: dict):
    """Build finalize_footprint inputs from accumulated state and run calculation."""
    try:
        total_mat_kg = sum(m.get("kg", 0) for m in state.get("materials", []))
        merged = {
            "product_name":              state["product_name"],
            "functional_unit":           state.get("functional_unit") or f"每件（{state['product_name']}）",
            "electricity_kwh":           state["electricity_kwh"],
            "region":                    state["region"],
            "materials":                 state["materials"],
            "fuel_type":                 state.get("fuel_type", ""),
            "fuel_quantity":             state.get("fuel_quantity", 0),
            "transport_weight_kg":       total_mat_kg,
            "transport_distance_km":     state.get("transport_distance_km", 0),
            "transport_mode":            state.get("transport_mode", "公路"),
            "packaging":                 state.get("packaging", []),
            "end_of_life_method":        state.get("end_of_life_method", ""),
            "end_of_life_recycled_pct":  state.get("recycled_pct", 0),
            "assumptions":               [],
        }
        result_json = execute_finalize_footprint(merged)
        return json.loads(result_json)
    except Exception:
        return None


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
    packaging = calc_packaging(inputs.get("packaging", []))
    transport = calc_transport(
        float(inputs.get("transport_weight_kg", 0)),
        float(inputs.get("transport_distance_km", 0)),
        inputs.get("transport_mode", "公路"),
    )
    total_weight = sum(m.get("kg", 0) for m in inputs.get("materials", []))
    total_weight += sum(p.get("kg", 0) for p in inputs.get("packaging", []))
    end_of_life = calc_end_of_life(
        weight_kg=total_weight,
        disposal_method=inputs.get("end_of_life_method", ""),
        recycled_pct=float(inputs.get("end_of_life_recycled_pct", 0)),
    )
    result = summarize_footprint(
        product_name=inputs["product_name"],
        functional_unit=inputs["functional_unit"],
        scope1=scope1,
        scope2=scope2,
        materials=materials,
        transport=transport,
        packaging=packaging,
        end_of_life=end_of_life,
        assumptions=inputs.get("assumptions", []),
        certification_standard=inputs.get("certification_standard") or None,
    )
    # Store full result for report download
    sid = _active_calc_session.get("current", "")
    if sid:
        calc_results[sid] = result

    # Return a concise summary to the LLM — full detail is in calc_results
    summary = {
        "status": "calculation_complete",
        "product_name": result["product_name"],
        "functional_unit": result["functional_unit"],
        "total_kgco2e": result["total_kgco2e"],
        "analogy_km": result["analogy_km"],
        "hotspot": result["hotspot"],
        "hotspot_pct": result["hotspot_pct"],
        "scope_summary": result["scope_summary"],
        "top_sources": result["breakdown"][:3],
        "assumptions": result["assumptions"],
        "unknowns": result["unknowns"],
        "iso14067_overall": result.get("compliance", {}).get("iso14067_overall"),
        "cbam_covered": result.get("compliance", {}).get("cbam", {}).get("covered"),
    }
    return json.dumps(summary, ensure_ascii=False)


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
    elif tool_name == "record_data":
        sid = _active_calc_session.get("current", "")
        return execute_record_data(sid, inputs)
    elif tool_name == "finalize_footprint":
        # Fill any missing optional fields from calc_data_state
        sid = _active_calc_session.get("current", "")
        state = calc_data_state.get(sid, {})
        merged = dict(inputs)
        for k in ("region", "fuel_type", "fuel_quantity", "transport_distance_km",
                  "transport_mode", "packaging", "end_of_life_method", "recycled_pct",
                  "functional_unit"):
            if not merged.get(k) and state.get(k):
                merged[k] = state[k]
        return execute_finalize_footprint(merged)
    elif tool_name == "start_product_calc":
        # Sentinel: agent_stream() detects this and switches to calc sub-agent
        return json.dumps({"__sub_agent__": "calc", "product_hint": inputs.get("product_hint", "")})
    return json.dumps({"error": f"未知工具: {tool_name}"})


# ── AgentRunner 单例（依赖 execute_tool，须在其后定义）──────────
_runner = AgentRunner(client=client, model=cfg["model"], execute_tool=execute_tool)
_calc_runner = AgentRunner(client=client, model=cfg["model"], execute_tool=execute_tool, max_iterations=20, force_blocking=True)


async def _run_calc_sub_agent(session_id: str, product_hint: str):
    """Run the product carbon footprint sub-agent for the first turn."""
    _active_calc_session["current"] = session_id
    messages = sessions[session_id]
    intro = product_hint if product_hint else "请开始帮我计算产品碳足迹。"
    messages.append({"role": "user", "content": intro})

    async for event in _calc_runner.run(messages, CALC_SYSTEM_PROMPT, CALC_TOOLS):
        yield event

    _active_calc_session["current"] = ""

    if session_id in calc_results:
        yield {"type": "calc_complete", "session_id": session_id}
        session_states.pop(session_id, None)
        calc_data_state.pop(session_id, None)


async def _persist(session_id: str) -> None:
    """Flush current message list to Redis (Tier 2)."""
    msgs = sessions.get(session_id, [])
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: memory_manager.session.save_messages(session_id, msgs))


async def _auto_name_session(session_id: str, first_user_msg: str) -> None:
    """Best-effort: generate a ≤10 char session name via Claude and save it."""
    try:
        response = await client.messages.create(
            model=cfg["model"],
            max_tokens=30,
            system=_AUTO_NAME_SYSTEM,
            messages=[{"role": "user", "content": first_user_msg[:200]}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        raw = raw.strip('"""\'「」【】《》（）()').strip()
        name = raw[:10] if raw else ""
        if name:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: memory_manager.session.set_name(session_id, name))
    except Exception:
        pass


async def _maybe_auto_name(session_id: str) -> None:
    """Fire auto-naming only after the very first completed assistant turn."""
    try:
        msgs = sessions.get(session_id, [])
        # Count plain-text assistant turns
        assistant_turns = sum(
            1 for m in msgs
            if m.get("role") == "assistant"
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        )
        if assistant_turns != 1:
            return
        # Check DB for existing name
        loop = asyncio.get_event_loop()
        existing_name = await loop.run_in_executor(None, lambda: memory_manager.session.get_name(session_id))
        if existing_name:
            return
        # Find first real user message
        first_user_msg = next(
            (
                m["content"] for m in msgs
                if m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and not m["content"].strip().startswith("[系统上下文]")
            ),
            None,
        )
        if not first_user_msg:
            return
        await _auto_name_session(session_id, first_user_msg)
    except Exception:
        pass


async def _maybe_extract_memories(session_id: str, user_message: str = "") -> None:
    """Delegate post-turn memory work (fact extraction + reflection) to MemoryManager."""
    try:
        msgs = sessions.get(session_id, [])
        await memory_manager.on_turn_complete(
            session_id=session_id,
            messages=msgs,
            client=client,
            model=cfg["model"],
            memory_extract_system=_MEMORY_EXTRACT_SYSTEM,
            user_message=user_message,
        )
    except Exception:
        pass


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
        _active_calc_session["current"] = session_id
        try:
            async for event in _calc_runner.run(messages, CALC_SYSTEM_PROMPT, CALC_TOOLS):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if session_id in calc_results:
                yield f"data: {json.dumps({'type': 'calc_complete', 'session_id': session_id})}\n\n"
                session_states.pop(session_id, None)
                calc_data_state.pop(session_id, None)
        except Exception as e:
            err_msg = str(e)
            if "authentication" in err_msg.lower() or "401" in err_msg:
                err_msg = "API Key 无效，请检查环境变量后重启服务。"
            yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"
        finally:
            _active_calc_session["current"] = ""
            await _persist(session_id)
            asyncio.create_task(_maybe_auto_name(session_id))
            asyncio.create_task(_maybe_extract_memories(session_id, user_message))
        return

    messages.append({"role": "user", "content": user_message})

    memory_ctx = _build_memory_context(session_id, user_message)
    dynamic_prompt = SYSTEM_PROMPT + ("\n\n" + memory_ctx if memory_ctx else "")

    try:
        sub_agent_triggered = False
        hint = ""
        async for event in _runner.run(messages, dynamic_prompt, TOOLS):
            if not sub_agent_triggered:
                last_user = next(
                    (m for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), list)),
                    None,
                )
                if last_user:
                    for tr in last_user["content"]:
                        if isinstance(tr, dict) and tr.get("type") == "tool_result":
                            try:
                                payload = json.loads(tr.get("content", "{}"))
                                if isinstance(payload, dict) and payload.get("__sub_agent__") == "calc":
                                    sub_agent_triggered = True
                                    hint = payload.get("product_hint", "")
                                    session_states[session_id] = "calc"
                                    break
                            except (json.JSONDecodeError, TypeError, AttributeError):
                                pass
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        if sub_agent_triggered:
            yield f"data: {json.dumps({'type': 'status', 'content': '🧮 启动产品碳足迹计算...'})}\n\n"
            async for event in _run_calc_sub_agent(session_id, hint):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    except Exception as e:
        err_msg = str(e)
        if "authentication" in err_msg.lower() or "401" in err_msg:
            err_msg = "API Key 无效，请检查环境变量后重启服务。"
        yield f"data: {json.dumps({'type': 'error', 'content': err_msg})}\n\n"
    finally:
        await _persist(session_id)
        asyncio.create_task(_maybe_auto_name(session_id))
        asyncio.create_task(_maybe_extract_memories(session_id, user_message))


# ── FastAPI 应用 ──────────────────────────────────────────────────
app = FastAPI(title="双碳 Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _with_keepalive(gen, interval: int = 8):
    """
    Wrap an SSE generator with keepalive pings.

    Runs the generator in a separate task so it is NEVER cancelled mid-await
    (asyncio.wait_for cancels the underlying coroutine, which corrupts async
    generators that contain awaitable operations like client.messages.create).
    Instead, a heartbeat task independently enqueues pings every `interval` seconds.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def _drain():
        try:
            async for event in gen:
                await queue.put(("data", event))
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    async def _heartbeat():
        while True:
            await asyncio.sleep(interval)
            await queue.put(("ping", None))

    drain_task = asyncio.create_task(_drain())
    ping_task  = asyncio.create_task(_heartbeat())
    try:
        while True:
            kind, value = await queue.get()
            if kind == "data":
                yield value
            elif kind == "ping":
                yield ": keepalive\n\n"
            elif kind == "error":
                yield f"data: {json.dumps({'type': 'error', 'content': str(value)}, ensure_ascii=False)}\n\n"
                break
            else:  # "done"
                break
    finally:
        ping_task.cancel()
        drain_task.cancel()
        await asyncio.gather(drain_task, ping_task, return_exceptions=True)
        await gen.aclose()


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    session_id   = body.get("session_id") or str(uuid.uuid4())
    user_message = body.get("message", "").strip()

    if not user_message:
        return {"error": "消息不能为空"}

    return StreamingResponse(
        _with_keepalive(agent_stream(session_id, user_message)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        }
    )


@app.post("/new_session")
async def new_session(request: Request):
    try:
        body = await request.json()
        existing_sid = body.get("session_id", "")
    except Exception:
        existing_sid = ""

    loop = asyncio.get_event_loop()
    if existing_sid and await loop.run_in_executor(None, lambda: memory_manager.session.exists(existing_sid)):
        msgs = await loop.run_in_executor(None, lambda: memory_manager.session.load_messages(existing_sid))
        sessions[existing_sid] = msgs
        memory_manager.short_term.set(existing_sid, msgs)
        return {"session_id": existing_sid, "restored": True, "message_count": len(msgs)}

    sid = str(uuid.uuid4())
    sessions[sid] = []
    await loop.run_in_executor(None, lambda: memory_manager.session.create(sid))
    return {"session_id": sid, "restored": False}


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


@app.get("/session/{session_id}/history")
async def get_session_history(session_id: str):
    loop = asyncio.get_event_loop()
    history = await loop.run_in_executor(None, lambda: memory_manager.session.load_display(session_id))
    return history


@app.get("/sessions")
async def get_sessions_list(limit: int = 50):
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: memory_manager.session.list(limit))
    return data


@app.delete("/session/{session_id}")
async def delete_session_endpoint(session_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: memory_manager.wipe_session(session_id))
    sessions.pop(session_id, None)
    session_states.pop(session_id, None)
    calc_results.pop(session_id, None)
    calc_data_state.pop(session_id, None)
    return {"deleted": session_id}


@app.patch("/session/{session_id}/name")
async def rename_session_endpoint(session_id: str, request: Request):
    body = await request.json()
    name = body.get("name", "").strip()[:20]
    if not name:
        return {"error": "name不能为空"}
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: memory_manager.session.set_name(session_id, name))
    return {"session_id": session_id, "name": name}


@app.delete("/user/{user_id}/memory")
async def wipe_user_memory(user_id: str):
    """Governance: wipe all Tier 3 (facts) + Tier 4 (reflections) for a user."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: memory_manager.wipe_user(user_id))
    return {"user_id": user_id, **result}


@app.get("/footprint-report/{session_id}")
async def footprint_report(session_id: str):
    result = calc_results.get(session_id)
    if not result:
        return {"error": "未找到计算结果，请先完成碳足迹计算"}
    html = generate_report_html(result)
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": "attachment; filename=carbon_footprint_report.html"},
    )


@app.get("/compliance-check/{session_id}")
async def compliance_check(session_id: str):
    result = calc_results.get(session_id)
    if not result:
        return {"error": "未找到计算结果，请先完成碳足迹计算"}
    compliance = result.get("compliance", {})
    return {
        "session_id": session_id,
        "product_name": result.get("product_name"),
        "total_kgco2e": result.get("total_kgco2e"),
        "iso14067_overall": compliance.get("iso14067_overall"),
        "iso14067_checklist": compliance.get("iso14067_checklist", []),
        "cbam": compliance.get("cbam", {}),
    }


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


@app.get("/agent-docs")
async def agent_docs():
    md_path = Path(__file__).parent / "agent.md"
    return {"content": md_path.read_text(encoding="utf-8")}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
