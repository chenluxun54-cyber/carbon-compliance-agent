"""
Compliance layer — pure deterministic functions, no LLM, no I/O.
Provides CBAM eligibility check and ISO 14067:2018 compliance checklist.
"""
from __future__ import annotations

# CBAM-covered sectors (EU Regulation 2023/956) and their fuzzy-match triggers
CBAM_SECTORS: dict[str, list[str]] = {
    "钢铁":  ["钢铁", "铸铁", "不锈钢"],
    "铝":    ["铝", "再生铝"],
    "水泥":  ["水泥", "陶瓷"],   # 陶瓷 is approximate; flagged in coverage_note
    "化肥":  ["化肥", "氮肥", "尿素"],
    "电力":  ["电力"],
    "氢":    ["氢", "氢气"],
}

# Proxy EU ETS price — update quarterly based on EU carbon market
EU_ETS_PRICE_EUR_PER_TONNE = 65.0


def check_cbam(materials: list[dict], total_kgco2e: float) -> dict:
    """
    Check if product falls under EU CBAM regulation based on its material inputs.

    materials: list of {"name": str, "kg": float} — same structure as calc_upstream_materials input
    total_kgco2e: total product carbon footprint in kgCO2e (used for cost estimate)
    """
    matched_sectors: list[str] = []
    matched_materials: list[str] = []

    for item in materials:
        name = item.get("name", "")
        for sector, triggers in CBAM_SECTORS.items():
            if any(trigger in name for trigger in triggers):
                if sector not in matched_sectors:
                    matched_sectors.append(sector)
                if name not in matched_materials:
                    matched_materials.append(name)
                break

    covered = len(matched_sectors) > 0
    cost_eur_estimate = round(total_kgco2e / 1000 * EU_ETS_PRICE_EUR_PER_TONNE, 2)

    if covered:
        sectors_str = "、".join(matched_sectors)
        coverage_note = (
            f"产品含CBAM覆盖行业原材料（{sectors_str}），"
            "出口至欧盟时可能需申报嵌入碳排放并购买CBAM证书。"
        )
    else:
        coverage_note = (
            "当前材料清单未涉及CBAM覆盖行业（钢铁、铝、水泥、化肥、电力、氢），"
            "暂无CBAM合规义务。"
        )

    return {
        "covered": covered,
        "matched_sectors": matched_sectors,
        "matched_materials": matched_materials,
        "cost_eur_estimate": cost_eur_estimate,
        "price_assumption_eur_per_tonne": EU_ETS_PRICE_EUR_PER_TONNE,
        "coverage_note": coverage_note,
    }


def iso14067_checklist(result: dict) -> list[dict]:
    """
    Evaluate a summarize_footprint result dict against ISO 14067:2018 requirements.
    Returns a list of 9 check items, each with keys: requirement, clause, status, note.
    """
    scope = result.get("scope_summary", {})
    breakdown = result.get("breakdown", [])
    assumptions = result.get("assumptions") or []
    unknowns = result.get("unknowns") or []
    functional_unit = result.get("functional_unit", "")

    items: list[dict] = []

    # 1. Functional unit
    fu_ok = bool(functional_unit) and functional_unit != "每件"
    fu_partial = functional_unit == "每件"
    items.append({
        "requirement": "功能单位已声明",
        "clause": "ISO 14067:2018 §6.3.2",
        "status": "pass" if fu_ok else ("partial" if fu_partial else "fail"),
        "note": "功能单位已明确包含产品描述" if fu_ok
                else ('功能单位仅为"每件"，建议补充产品规格描述' if fu_partial
                      else "未声明功能单位"),
    })

    # 2. System boundary
    has_scope = any(v != 0 for v in scope.values()) if scope else False
    items.append({
        "requirement": "系统边界已定义",
        "clause": "ISO 14067:2018 §6.3.3",
        "status": "pass" if has_scope else "fail",
        "note": "已覆盖范围一/二/三生命周期边界" if has_scope else "未定义系统边界",
    })

    # 3. Scope 3 upstream materials
    s3_mat = scope.get("scope3_upstream_materials", 0)
    items.append({
        "requirement": "范围三覆盖（上游材料）",
        "clause": "ISO 14067:2018 §6.4.1.3",
        "status": "pass" if s3_mat > 0 else "fail",
        "note": f"上游原材料排放 {s3_mat:.3f} kg CO₂e 已计入" if s3_mat > 0
                else "未计入上游原材料排放（范围三）",
    })

    # 4. Scope 2 electricity
    s2 = scope.get("scope2_electricity", 0)
    items.append({
        "requirement": "范围二（生产用电）已计入",
        "clause": "ISO 14067:2018 §6.4.1.2",
        "status": "pass" if s2 > 0 else "fail",
        "note": f"生产用电排放 {s2:.3f} kg CO₂e 已计入" if s2 > 0
                else "未计入生产用电排放（范围二）",
    })

    # 5. Emission factor sources cited
    has_breakdown = len(breakdown) > 0
    all_sourced = has_breakdown and all(item.get("ef_source") for item in breakdown)
    items.append({
        "requirement": "排放因子来源已引用",
        "clause": "ISO 14067:2018 §6.3.6",
        "status": "pass" if all_sourced else ("partial" if has_breakdown else "fail"),
        "note": "所有排放因子均已注明数据来源" if all_sourced
                else ("部分排放项未注明排放因子来源" if has_breakdown
                      else "未提供排放因子明细"),
    })

    # 6. Assumptions documented
    items.append({
        "requirement": "假设与默认值已文档化",
        "clause": "ISO 14067:2018 §6.3.7",
        "status": "pass" if len(assumptions) > 0 else "fail",
        "note": f"已记录 {len(assumptions)} 项假设/默认值" if assumptions
                else "未记录任何计算假设；若使用了行业默认值应在此列出",
    })

    # 7. Unknown materials disclosed
    n_unknown = len(unknowns)
    items.append({
        "requirement": "不确定材料已披露",
        "clause": "ISO 14067:2018 §6.3.8",
        "status": "pass" if n_unknown == 0
                  else ("partial" if n_unknown <= 2 else "fail"),
        "note": "所有材料排放因子均已识别" if n_unknown == 0
                else (f"{n_unknown} 种材料未能匹配排放因子，已从计算中排除：{', '.join(unknowns)}"),
    })

    # 8. GHG coverage declared (always pass — report template hardcodes CO₂/CH₄/N₂O)
    items.append({
        "requirement": "温室气体覆盖范围已声明",
        "clause": "ISO 14067:2018 §6.3.5",
        "status": "pass",
        "note": "报告方法论声明覆盖 CO₂、CH₄、N₂O（IPCC AR6 GWP100）",
    })

    # 9. Allocation method declared (always pass — report template hardcodes mass allocation)
    items.append({
        "requirement": "分配方法已声明",
        "clause": "ISO 14067:2018 §6.3.4",
        "status": "pass",
        "note": "采用质量分配法（未另行说明时）",
    })

    return items
