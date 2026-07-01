"""
Deterministic carbon footprint calculation engine.
All functions are pure — no LLM involved. LLM interprets; this module calculates.
"""
from __future__ import annotations

from emission_factors import (
    GRID_FACTORS, MATERIAL_FACTORS, FUEL_FACTORS, TRANSPORT_FACTORS,
    EOL_FACTORS, EF_SOURCES, resolve_region, get_material_factor, get_packaging_factor,
)
from compliance import check_cbam, iso14067_checklist

# Average passenger car CO2e intensity (IPCC AR6, kgCO2e/km)
_CAR_KG_PER_KM = 0.221


def calc_scope2(electricity_kwh: float, region: str) -> dict:
    grid_key = resolve_region(region)
    ef = GRID_FACTORS.get(grid_key)
    if ef is None:
        ef = GRID_FACTORS.get("全国平均", 0.5568)
        grid_key = "全国平均"
    value = electricity_kwh * ef
    result = {
        "value_kgco2e": round(value, 4),
        "electricity_kwh": electricity_kwh,
        "grid_region": grid_key,
        "ef_kgco2e_per_kwh": ef,
        "source": EF_SOURCES["grid"],
    }
    # Add warning when province couldn't be mapped to a known grid region
    if grid_key == "全国平均" and region not in GRID_FACTORS:
        result["warning"] = f"省份/地区\"{region}\"无法映射到已知电网大区，使用全国平均因子 {ef} kgCO₂e/kWh。"
    return result


def calc_upstream_materials(items: list) -> dict:
    """items: [{"name": "铝", "kg": 0.27}, ...]"""
    breakdown = []
    total = 0.0
    unknowns = []

    for item in items:
        name = item.get("name", "")
        kg = float(item.get("kg", 0))
        ef, matched = get_material_factor(name)
        if ef is None:
            unknowns.append(name)
            continue
        value = kg * ef
        total += value
        breakdown.append({
            "name": name,
            "matched_key": matched,
            "kg": kg,
            "ef_kgco2e_per_kg": ef,
            "value_kgco2e": round(value, 4),
        })

    return {
        "total_kgco2e": round(total, 4),
        "breakdown": breakdown,
        "unknowns": unknowns,
        "source": EF_SOURCES["material"],
    }


def calc_packaging(items: list) -> dict:
    """Same structure as calc_upstream_materials but uses PACKAGING_FACTORS."""
    breakdown = []
    total = 0.0
    unknowns = []

    for item in items:
        name = item.get("name", "")
        kg = float(item.get("kg", 0))
        ef, matched = get_packaging_factor(name)
        if ef is None:
            unknowns.append(name)
            continue
        value = kg * ef
        total += value
        breakdown.append({
            "name": name,
            "matched_key": matched,
            "kg": kg,
            "ef_kgco2e_per_kg": ef,
            "value_kgco2e": round(value, 4),
        })

    return {
        "total_kgco2e": round(total, 4),
        "breakdown": breakdown,
        "unknowns": unknowns,
        "source": EF_SOURCES["packaging"],
    }


def calc_scope1_fuel(fuel_type: str, quantity: float) -> dict:
    if not fuel_type or quantity <= 0:
        return {"value_kgco2e": 0.0, "skipped": True}
    ef = FUEL_FACTORS.get(fuel_type)
    if ef is None:
        return {"value_kgco2e": 0.0, "error": f"未知燃料类型: {fuel_type}"}
    value = quantity * ef
    return {
        "value_kgco2e": round(value, 4),
        "fuel_type": fuel_type,
        "quantity": quantity,
        "ef": ef,
        "source": EF_SOURCES["fuel"],
    }


def calc_transport(weight_kg: float, distance_km: float, mode: str) -> dict:
    if distance_km <= 0:
        return {"value_kgco2e": 0.0, "skipped": True}
    ef = TRANSPORT_FACTORS.get(mode, TRANSPORT_FACTORS["公路"])
    weight_tonnes = weight_kg / 1000
    value = weight_tonnes * distance_km * ef
    return {
        "value_kgco2e": round(value, 4),
        "weight_kg": weight_kg,
        "distance_km": distance_km,
        "mode": mode,
        "ef_kgco2e_per_tonne_km": ef,
        "source": EF_SOURCES["transport"],
    }


def calc_end_of_life(weight_kg: float, disposal_method: str, recycled_pct: float = 0.0) -> dict:
    """
    weight_kg: total product weight (materials + packaging)
    disposal_method: 填埋 | 焚烧 | 回收 | 混合 | 堆肥 | 未知
    recycled_pct: 0-100, used when method is 混合
    """
    if weight_kg <= 0 or not disposal_method:
        return {"value_kgco2e": 0.0, "skipped": True}

    if disposal_method == "混合":
        r = min(max(recycled_pct, 0), 100) / 100
        ef = EOL_FACTORS["回收"] * r + EOL_FACTORS["填埋"] * (1 - r)
    else:
        ef = EOL_FACTORS.get(disposal_method, EOL_FACTORS["未知"])

    value = weight_kg * ef
    return {
        "value_kgco2e": round(value, 4),
        "weight_kg": weight_kg,
        "disposal_method": disposal_method,
        "recycled_pct": recycled_pct if disposal_method == "混合" else None,
        "ef_kgco2e_per_kg": round(ef, 4),
        "source": EF_SOURCES["eol"],
    }


def summarize_footprint(
    product_name: str,
    functional_unit: str,
    scope1: dict,
    scope2: dict,
    materials: dict,
    transport: dict,
    packaging: dict = None,
    end_of_life: dict = None,
    assumptions: list = None,
    certification_standard: str = None,
) -> dict:
    s1 = scope1.get("value_kgco2e", 0.0)
    s2 = scope2.get("value_kgco2e", 0.0)
    mat = materials.get("total_kgco2e", 0.0)
    tr = transport.get("value_kgco2e", 0.0)
    pkg = (packaging or {}).get("total_kgco2e", 0.0)
    eol = (end_of_life or {}).get("value_kgco2e", 0.0)
    total = s1 + s2 + mat + tr + pkg + eol

    def pct(v):
        return round(v / total * 100) if total > 0 else 0

    breakdown = []
    if mat > 0:
        for item in materials.get("breakdown", []):
            breakdown.append({
                "source": f"上游原材料（{item['name']} {item['kg']}kg）",
                "value_kgco2e": item["value_kgco2e"],
                "pct": pct(item["value_kgco2e"]),
                "ef_used": item["ef_kgco2e_per_kg"],
                "ef_source": EF_SOURCES["material"],
            })
    if pkg > 0:
        for item in (packaging or {}).get("breakdown", []):
            breakdown.append({
                "source": f"包装材料（{item['name']} {item['kg']}kg）",
                "value_kgco2e": item["value_kgco2e"],
                "pct": pct(item["value_kgco2e"]),
                "ef_used": item["ef_kgco2e_per_kg"],
                "ef_source": EF_SOURCES["packaging"],
            })
    if s2 > 0:
        breakdown.append({
            "source": f"生产用电（{scope2['grid_region']}电网）",
            "value_kgco2e": s2,
            "pct": pct(s2),
            "ef_used": scope2["ef_kgco2e_per_kwh"],
            "ef_source": EF_SOURCES["grid"],
        })
    if s1 > 0:
        breakdown.append({
            "source": f"直接燃料燃烧（{scope1.get('fuel_type', '')}）",
            "value_kgco2e": s1,
            "pct": pct(s1),
            "ef_used": scope1.get("ef"),
            "ef_source": EF_SOURCES["fuel"],
        })
    if tr > 0:
        breakdown.append({
            "source": f"运输（{transport.get('mode', '')} {transport.get('distance_km', 0)}km）",
            "value_kgco2e": tr,
            "pct": pct(tr),
            "ef_used": transport.get("ef_kgco2e_per_tonne_km"),
            "ef_source": EF_SOURCES["transport"],
        })
    if eol != 0:
        label = "报废处置" if eol >= 0 else "报废回收（碳汇）"
        breakdown.append({
            "source": f"{label}（{(end_of_life or {}).get('disposal_method', '')}）",
            "value_kgco2e": eol,
            "pct": pct(eol),
            "ef_used": (end_of_life or {}).get("ef_kgco2e_per_kg"),
            "ef_source": EF_SOURCES["eol"],
        })

    breakdown.sort(key=lambda x: x["value_kgco2e"], reverse=True)
    hotspot = breakdown[0] if breakdown else None

    all_unknowns = list(materials.get("unknowns", []))
    if packaging:
        all_unknowns += packaging.get("unknowns", [])

    out = {
        "product_name": product_name,
        "functional_unit": functional_unit,
        "total_kgco2e": round(total, 3),
        "analogy_km": round(total / _CAR_KG_PER_KM, 1),
        "breakdown": breakdown,
        "hotspot": hotspot["source"] if hotspot else None,
        "hotspot_pct": hotspot["pct"] if hotspot else 0,
        "unknowns": all_unknowns,
        "assumptions": assumptions or [],
        "scope_summary": {
            "scope1_direct": round(s1, 3),
            "scope2_electricity": round(s2, 3),
            "scope3_upstream_materials": round(mat, 3),
            "scope3_packaging": round(pkg, 3),
            "scope3_transport": round(tr, 3),
            "scope3_end_of_life": round(eol, 3),
        },
    }

    # ── Compliance layer (MVP3) ──────────────────────────────────────
    _mat_list = [{"name": b["name"], "kg": b["kg"]} for b in materials.get("breakdown", [])]
    cbam_result = check_cbam(_mat_list, total)
    checklist = iso14067_checklist(out)
    overall = (
        "fail" if any(i["status"] == "fail" for i in checklist) else
        "partial" if any(i["status"] == "partial" for i in checklist) else
        "pass"
    )
    out["compliance"] = {
        "cbam": cbam_result,
        "iso14067_checklist": checklist,
        "iso14067_overall": overall,
        "certification_standard_requested": certification_standard,
    }
    return out
