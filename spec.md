# Carbon Compliance Agent — Product Specification

> **阅读说明**
> 本文件描述产品"应该"表现成什么样，不是当前代码实际行为的镜像。
> 架构信息（文件职责、依赖图、启动命令）见 `agent.md`，本文件不重复。
> 标注 ⚠️ 的条目表示当前设计意图不确定，需要产品方确认后才能写入 spec 并驱动修复。

---

## 子系统 1 — 企业碳评分

### 1. 目的

对数据库中的 A 股上市企业，按六大维度计算 0–100 分的碳表现综合评分，并以行业内同年度企业为基准，给出各指标的行业分位排名。

### 2. 核心行为

**输入**：`company_id`（字符串，如 `COMP_001`）+ `report_year`（整数，如 `2024`）

**处理流程**：

1. 从 `carbon_database.xlsx` 的 `company_data` sheet 加载目标企业行，同时加载同行业、同年度全部企业行用于计算基准。
2. 对每个**线性指标**（共 17 个，见 `config.py` `LINEAR_INDICATORS`）：
   - 在行业组内按 `rank(method="min", ascending=True)` 排名。
   - `percentile = rank / sample_size`。
   - 对标注为 `is_inverse=True` 的指标（越低越好），翻转为 `percentile = 1 - percentile`。
   - 得分 = `round(percentile × max_score, 1)`。
3. 对每个**直接赋分指标**（5 个：`certification_type`、`carbon_target_completeness`、`disclosure_level`、`supply_chain_disclosure`、`data_submission_lag_months`）：按 `config.py` `DIRECT_SCORING_RULES` 查表赋分，`data_submission_lag_months` 单独用阶梯规则（≤3月=1.0，≤6月=0.7，≤12月=0.3，>12月=0.0）。
4. 将 22 个指标的得分按 6 个维度汇总（`config.py` `DIMENSIONS`），每个维度得分 = 该维度所属指标得分之和，四舍五入到 1 位小数。
5. `total_score = sum(6个维度得分)`，四舍五入到 1 位小数，范围 0–100。

**输出 JSON 结构**（顶层字段）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `company_id` | str | 企业 ID |
| `company_name` | str | 企业名称 |
| `industry` | str | 行业 |
| `report_year` | int | 报告年度 |
| `sample_size` | int | 行业基准样本量（同行业同年度企业数） |
| `total_score` | float | 综合得分 0–100 |
| `dimensions` | array | 6 个维度详情（含各指标得分与分位） |
| `flags` | array | 数据缺失/替代处理说明文本 |

`dimensions` 每项包含：`id`、`name`、`score`、`max_score`、`percentage`（score/max_score×100）、`indicators`（指标字典）。

`indicators` 每项包含：`score`、`max_score`、`percentile`（0–1）、`method`（`"linear"` 或 `"direct"`）、`missing`（布尔）；线性指标额外有 `direction`（`"positive"` 或 `"inverse"`）。

**输出不应包含的字段**：`main_hotspot`、`carbon_footprint`、与产品级碳足迹计算相关的任何字段。这些属于子系统 3，不属于企业评分输出。

> ⚠️ 待确认：当前 `scorer.py` 的 `score()` 方法末尾（约 lines 98–127）存在一段产品碳足迹计算逻辑（用 `electricity_kwh`、`weight_kg`、`transport_distance_km` 等计算 `carbon_footprint`，并返回 `main_hotspot`）。这些字段不属于企业评分数据。请确认：**这段代码是错误引入的 bug，应完整删除**，还是有其他设计意图？当前 spec 认定为 bug（应删除）。

### 3. 边界（本版本不做）

- 不支持跨行业横向对比（评分只在同行业内排名）。
- 不支持同一企业多年度在同一次请求中计算（`/history` 端点通过多次单年度调用实现）。
- 不对缺失数据进行插值或预测（除 `water_recycling_rate` 的特殊处理，见下）。

### 4. 异常情况处理

| 情况 | 期望行为 |
|------|---------|
| 企业 ID 或年份在数据库中不存在 | 抛出 `ValueError("未找到企业 {id} 在 {year} 年的数据")`，由 FastAPI 层转为 HTTP 422 |
| 某指标数据缺失（NaN）| 该指标得 0 分，`missing=True`，在 `flags` 数组中追加说明文本 |
| `water_recycling_rate` 缺失 | 使用行业中位数排名（`rank = round(sample_size / 2)`）替代，而非得 0 分，在 `flags` 中注明替代处理 |
| 行业样本量为 0 或 1 | 所有线性指标得满分（percentile=1）；⚠️ 待确认：是否应返回警告而非直接给满分？ |
| `yoy_carbon_reduction_rate` / `yoy_carbon_intensity_improve` 为负数 | clip 到 0（即不允许"比上年排放增加"对得分产生负向贡献），属于设计决定而非 bug |

### 5. 与其他子系统的接口

见 `agent.md` §Architecture。子系统 1 是独立的读取+计算层，不调用子系统 2 或 3。`agent.py` 的 `carbon_score` 工具直接调用 `DataLoader.fetch()` + `CarbonScorer.score()`，结果通过工具返回给 LLM 后用于对话。

---

## 子系统 2 — 政策与法规库

### 1. 目的

提供一个可搜索的全球碳政策知识库，支持按关键词、行业、地区筛选，并能返回政策完整详情（含真实企业合规案例），供 LLM 在对话中引用。

### 2. 核心行为

**策略列表查询**（`search_policies` 工具 / `GET /policies`）：

- 输入：可选参数 `keyword`、`industry`、`jurisdiction`（三者均可为空）。
- 处理：先按 `jurisdiction` 过滤，再按 `industry` 过滤（`industries` 字段包含 `"all"` 则命中所有行业），再按 `keyword` 对 `name` 和 `summary` 做大小写不敏感子串搜索。三个过滤条件均为 AND 关系。
- 输出：匹配政策列表，每项包含 `id`、`name`、`jurisdiction`、`category`、`summary`（截断至 120 字符 + `…`）、`industries`。无匹配时返回空数组 `[]`，不报错。

**政策详情查询**（`get_policy_detail` 工具 / `GET /policies/{policy_id}`）：

- 输入：`policy_id`（精确 ID，如 `"CBAM"`）。
- 输出：完整政策对象，包含 `key_requirements`（数组）和 `compliance_examples`（含 `company`、`country`、`industry`、`problem`、`action`、`result`、`example_url`）。

**LLM 使用规范**（非 API 层但属于产品预期行为）：

- 解释具体政策时，必须先调用 `get_policy_detail` 获取完整信息，再引用其中的 `compliance_examples` 举实例说明。禁止从训练数据编造政策内容或案例数据。
- 按行业推荐政策时，优先选择 `industries` 字段包含该行业或 `"all"` 的政策。

**政策库覆盖范围**（当前版本）：

| 地区 | 数量 |
|------|------|
| 全球 | 6 |
| 欧盟 | 3 |
| 中国 | 6 |
| 合计 | 15 |

### 3. 边界

- 政策数据静态存储于 `policies.py`，本版本不支持动态增删或外部数据源接入。
- 不提供政策法条全文，仅提供摘要、关键要求和企业案例。
- 不支持多关键词 OR 搜索，`keyword` 参数为单一字符串子串匹配。

### 4. 异常情况处理

| 情况 | 期望行为 |
|------|---------|
| `policy_id` 不存在 | 返回 `{"error": "未找到政策 {id}，请检查ID是否正确"}` |
| `search_policies` 三个参数均为空 | 返回全部 15 条政策 |
| 搜索无匹配 | 返回空数组 `[]`，不报错，LLM 应告知用户未找到匹配政策 |

### 5. 与其他子系统的接口

见 `agent.md` §Subsystem 2。子系统 2 不依赖子系统 1 或 3。差距分析（Gap Analysis）在后端预先调用 `search_policies`，将结果直接注入 prompt，不需要 LLM 再调用工具。

---

## 子系统 3 — 产品碳足迹计算器

### 1. 目的

通过对话式数据收集，计算单件产品从原材料生产到报废的全生命周期碳足迹（LCA，ISO 14067 框架），输出各阶段排放明细、最大排放来源（热点）、CBAM 合规状态和 ISO 14067 合规清单，并生成可下载的 HTML 报告。

### 2. 核心行为

#### 2a. 数据收集（LLM 对话层）

必须收集 5 项必填字段，才能触发计算：

| 字段 | 说明 |
|------|------|
| `product_name` | 产品名称（字符串） |
| `weight_kg` | 每件成品重量（kg，正数） |
| `region` | 工厂所在省份或地区（用于匹配电网因子） |
| `electricity_kwh` | 生产每件产品用电量（kWh，正数） |
| `materials` | 主要原材料列表（至少 1 种，格式：`[{"name": str, "kg": float}]`） |

可选字段（用户说"不知道/跳过"则忽略）：燃料类型与用量、运输距离与方式、包装材料、报废处置方式（含回收比例）。

**收集规则**：

- LLM 每次只问一个问题，不得一次追问多项。
- 用户提供数据后，立即调用 `record_data` 工具记录，不得先确认再调用。
- 5 项必填字段全部收集完毕后自动触发计算，不需要用户额外确认。
- 同一对话内处理第二个产品时，若用户声明"与上个产品同一工厂/材料"，应从历史对话中提取已知数据复用，不得重复追问。

#### 2b. 碳足迹计算（纯确定性引擎）

计算由 `calculator.py` 纯函数完成，LLM 不参与数学计算。

**Scope 2（生产用电）**：

- 排放 = `electricity_kwh × grid_ef`，`grid_ef` 从 `emission_factors.py` `GRID_FACTORS` 按省份→电网大区→全国平均依次查找。
- 如省份无法映射到已知电网大区，使用全国平均因子 0.5568 kgCO₂e/kWh，并在结果中注明。

**Scope 3 上游材料**：

- 对每种材料，用 `MATERIAL_FACTORS` 做模糊匹配（完全匹配优先，其次子串匹配）。
- 排放 = `kg × ef`，无法匹配的材料加入 `unknowns` 列表（不报错，但结果中披露）。

**Scope 1（燃料燃烧，可选）**：

- 排放 = `quantity × fuel_ef`，`fuel_ef` 从 `FUEL_FACTORS` 精确匹配 `fuel_type` 键名。
- 未传入或 `quantity <= 0` 时跳过，贡献为 0。

**运输（可选）**：

- 排放 = `(weight_kg / 1000) × distance_km × transport_ef`。
- `transport_ef` 从 `TRANSPORT_FACTORS` 按运输方式查找，未知方式默认公路。
- `distance_km <= 0` 时跳过。

**包装（可选）**：

- 与上游材料同逻辑，使用 `PACKAGING_FACTORS`。

**报废（可选）**：

- 排放 = `total_weight_kg × eol_ef`，`total_weight_kg` = 原材料重量 + 包装重量。
- `回收` 因子为 -0.35（负值，碳汇），`混合` 按回收比例加权。

**汇总**：

- `total_kgco2e = scope1 + scope2 + scope3_materials + scope3_packaging + transport + eol`，保留 3 位小数。
- `analogy_km = round(total_kgco2e / 0.221, 1)`（基准：普通燃油车 0.221 kgCO₂e/km，IPCC AR6）。
- `hotspot` = `breakdown` 中 `value_kgco2e` 最大的一项来源描述。
- `breakdown` 按 `value_kgco2e` 降序排列。

#### 2c. 合规检查（自动随计算执行）

**CBAM 检查**：

- 遍历原材料列表，对每种材料名用触发词列表模糊匹配 CBAM 覆盖行业（钢铁、铝、水泥、化肥、电力、氢）。
- `covered=True` 时，估算成本 = `total_kgco2e / 1000 × 65.0 EUR/tonne`（EU ETS 参考价，非实时）。

**ISO 14067 合规清单**（9 项）：

| 序号 | 检查项 | 通过条件 |
|------|--------|---------|
| 1 | 功能单位已声明 | `functional_unit` 不为空且不仅为 `"每件"` |
| 2 | 系统边界已定义 | `scope_summary` 中至少一项不为 0 |
| 3 | 范围三（上游材料）已覆盖 | `scope3_upstream_materials > 0` |
| 4 | 范围二（生产用电）已计入 | `scope2_electricity > 0` |
| 5 | 排放因子来源已引用 | `breakdown` 中每项均有 `ef_source` |
| 6 | 假设与默认值已文档化 | `assumptions` 列表不为空 |
| 7 | 不确定材料已披露 | `unknowns` 为空（pass）；1–2 种（partial）；3 种及以上（fail） |
| 8 | 温室气体覆盖范围已声明 | 始终通过（报告模板固定声明 CO₂/CH₄/N₂O） |
| 9 | 分配方法已声明 | 始终通过（报告模板固定声明质量分配法） |

整体状态：任一项 `fail` → `fail`；否则任一项 `partial` → `partial`；否则 `pass`。

> ⚠️ 待确认：ISO 14067 第 6 项"假设与默认值"要求 `assumptions` 列表不为空。但在当前代码流程中，`assumptions` 默认为 `[]`，常规用户使用时该项几乎总是 `fail`。设计意图是否应改为：若计算中使用了行业默认值（如未知材料跳过、运输默认公路），应自动将这些情况写入 `assumptions`？还是保持现状要求 LLM/用户手动声明？

#### 2d. 结果展示与下载

- 计算完成后，聊天界面显示内联摘要气泡（含总排放、类比行驶里程、最大热点来源及占比）和下载按钮。
- 用户点击"查看完整报告"或"下载 HTML"访问 `/footprint-report/{session_id}`，返回完整 HTML 报告（含分排放明细表、ISO 14067 合规清单、CBAM 状态）。
- 同一 session 内同时只保留最近一次计算结果；若用户发起第二次产品计算，前一份报告链接应仍可访问直到 session 结束。

> ⚠️ 待确认：用户请求第二个产品时，前一个产品的报告是否应保留可访问？当前代码 `calc_results[session_id]` 只保留最后一次结果，前一份报告链接会失效。如果期望是"每次计算各自可访问"，需要用不同 key 存储。

### 3. 边界

- 本版本计算功能单位为**单件产品**，不支持批量计算或年度总产量估算。
- 材料数据库覆盖约 45 种材料（见 `emission_factors.py`），不在列表内的材料跳过计算并在 `unknowns` 中披露，不阻断整体计算。
- 电网因子仅覆盖中国大陆七大电网区域，境外生产场景使用全国平均因子并标注警告。
- 不支持第三方核查或 EPD（环境产品声明）认证申请，ISO 14067 清单仅为自评工具。

### 4. 异常情况处理

| 情况 | 期望行为 |
|------|---------|
| 材料名无法匹配任何已知因子 | 该材料贡献计为 0，加入 `unknowns` 列表，计算继续 |
| 省份/地区无法匹配任何电网大区 | 使用全国平均因子（0.5568 kgCO₂e/kWh），在结果中加 `warning` 字段说明 |
| 5 项必填数据未收集完就触发计算 | 不触发计算，LLM 告知缺失字段并继续收集 |
| 用户要求下载报告但 session 无计算结果 | 返回 `{"error": "未找到计算结果，请先完成碳足迹计算"}`，HTTP 200 |
| `end_of_life_method` 不在已知列表 | 使用 `未知` 因子（0.48 kgCO₂e/kg，等同于填埋）处理 |
| `transport_mode` 不在已知列表 | 使用公路默认因子（0.0965 kgCO₂e/tonne-km） |

### 5. 与其他子系统的接口

见 `agent.md` §Subsystem 3。计算引擎（`calculator.py`、`emission_factors.py`、`compliance.py`）是纯函数层，不依赖子系统 1 或 2。LLM 通过 `record_data` 工具收集数据后，由 `execute_record_data()` 在 5 项满足时自动调用 `_auto_finalize()` 触发计算，不需要额外工具调用。

---

## 跨子系统公共行为

### 流式响应（SSE）

- 所有 LLM 生成内容通过 SSE 流式传输，事件格式为 `data: {JSON}\n\n`。
- 事件类型：`token`（文字块）、`status`（工具调用状态提示）、`error`（出错终止）、`done`（生成完毕）、`progress`（碳足迹收集进度）、`calc_complete`（计算完成，附摘要数据）。
- keepalive 心跳每 8 秒发送一次（`": keepalive\n\n"`），防止连接超时。

### 错误处理

- API Key 未设置：立即返回 `type: error`，提示对应环境变量名，不进入 LLM 调用。
- `authentication` / `401` 类错误：统一转换为中文提示"API Key 无效，请检查环境变量后重启服务。"，屏蔽原始错误信息。
- 其他内部异常：以 `type: error` 事件返回原始错误信息字符串，前端展示为红色警告气泡。

### 记忆与学习

- 每条用户消息在 LLM 响应完成后，由 `detector.py` 独立判断是否为 bug 反馈并写入 `bugs.md`（fire-and-forget，不影响主流程）。
- 对话历史和用户偏好写入 Tier 2–4 记忆层（Redis + Chroma），在 session 恢复和后续对话中注入 prompt。
- 开发者在对话中给出纠错指令时，LLM 应立即在当次回复中展示改正后的结果（不仅说"记住了"），并将规则写入 `agent.md` 自主学习规则节。
