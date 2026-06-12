# Carbon Compliance Agent

An AI-powered carbon compliance assistant for Chinese A-share listed companies.
Accepts natural language queries, scores companies across 6 ESG dimensions,
provides carbon policy and regulatory guidance, and calculates product-level carbon footprints.

---

## Architecture

```
User → POST /chat → SSE stream → AgentRunner
                                      ↓
                              Tool calls (5 tools)
                        /              |               \
               carbon_score     search_policies      footprint_calc
               get_history      get_policy_detail
                    ↓                  ↓                   ↓
           carbon_database.xlsx   policies.py          calculator.py
                                                    emission_factors.py
```

---

## Subsystem 1 — Company Scoring

Rates companies from `carbon_database.xlsx` across 6 ESG dimensions and benchmarks
them against industry peers.

### Tools

| Tool | Inputs | Returns |
|------|--------|---------|
| `carbon_score` | company_id, report_year | 6-dimension scores + industry percentile |

### Scoring Dimensions (100 pts total)

| ID | Dimension | Weight |
|----|-----------|--------|
| D1 | 碳排放强度 | 28 |
| D2 | 能源结构清洁度 | 17 |
| D3 | 减碳动态表现 | 18 |
| D4 | 资源利用效率 | 11 |
| D5 | 碳管理成熟度 | 21 |
| D6 | 信息披露透明度 | 5 |

### Key Files

- `scorer.py` — scoring logic
- `config.py` — dimension weights and rules
- `data_loader.py` — loads carbon_database.xlsx

---

## Subsystem 2 — Policy & Regulatory Library

Provides searchable access to 15 global carbon policies and regulations.
Surfaces relevant laws, compliance requirements, and real-world examples
based on user queries.

### Tools

| Tool | Inputs | Returns |
|------|--------|---------|
| `search_policies` | keyword?, industry?, jurisdiction? | filtered policy list |
| `get_policy_detail` | policy_id | full policy + compliance examples |

### Coverage

| Jurisdiction | Count |
|-------------|-------|
| Global | 6 |
| EU | 3 |
| China | 6 |

### Policy Schema

Each policy contains: `id`, `name`, `jurisdiction`, `effective_date`, `category`,
`summary`, `key_requirements`, `industries`, `compliance_examples`, `tags`

### Key Files

- `policies.py` — policy database

---

## Subsystem 3 — Product Carbon Footprint Calculator

LCA-style footprint engine for individual products. Pure calculation — no LLM
involved in the math. LLM interprets inputs and presents results.

### Tools

| Tool | Inputs | Returns |
|------|--------|---------|
| `calc_footprint` | product details, materials, energy, region | scope breakdown + total kgCO2e |

### Calculation Scope

| Stage | Module |
|-------|--------|
| Scope 2 (electricity) | `calc_scope2` |
| Upstream materials | `calc_upstream_materials` |
| Scope 1 (fuel combustion) | `calc_scope1_fuel` |
| Transport | `calc_transport` |
| Packaging | `calc_packaging` |
| End-of-life | `calc_end_of_life` |

### Compliance Checks (auto-run after calculation)

- **CBAM** — flags if product is subject to EU Carbon Border Adjustment Mechanism
- **ISO 14067** — checklist for carbon footprint declaration

### Key Files

- `calculator.py` — footprint calculation engine (pure functions)
- `emission_factors.py` — EF database (grid, materials, fuels, transport)
- `compliance.py` — CBAM check + ISO 14067 checklist
- `report_template.py` — HTML report generator

---

## Shared Infrastructure

| File | Role |
|------|------|
| `agent.py` | FastAPI app, SSE streaming, tool dispatch |
| `agent_runner.py` | Standalone agent loop (decoupled from FastAPI) |
| `persistence.py` | SQLite chat history + session memory |

---

## Running

```bash
export MINIMAX_API_KEY=your-key
export MODEL_PROVIDER=minimax
python3 -m uvicorn agent:app --reload --port 8000
# open http://localhost:8000
```

Switch to Claude: `export MODEL_PROVIDER=anthropic && export ANTHROPIC_API_KEY=sk-ant-...`

Port conflict: `kill $(lsof -t -i:8000)` then restart

---

## Provider Support

| Provider | Model | Notes |
|----------|-------|-------|
| minimax (default) | MiniMax-Text-01 | First turn streams; tool-result turns use non-streaming (endpoint limitation) |
| anthropic | claude-sonnet-4-6 | Full streaming |

---

## 自主学习规则
- [2026-06-12] 当用户要报告/下载时，系统自动弹出可下载的HTML报告框，不需要文字回答
- [2026-06-12] 当用户要求生成报告时，应该提供符合要求的格式，不要提供不相关的内容
