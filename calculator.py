"""
Deterministic carbon footprint calculation engine.
All functions are pure — no LLM involved. LLM interprets; this module calculates.
"""
from __future__ import annotations

from emission_factors import (
    GRID_FACTORS, MATERIAL_FACTORS, FUEL_FACTORS, TRANSPORT_FACTORS,
    EF_SOURCES, resolve_region, get_material_factor,
)

# Average passenger car CO2e intensity (IPCC AR6, kgCO2e/km)
_CAR_KG_PER_KM = 0.221


def calc_scope2(electricity_kwh: float, region: str) -> dict:
    grid_key = resolve_region(region)
    ef = GRID_FACTORS[grid_key]
    value = electricity_kwh * ef
    return {
        "value_kgco2e": round(value, 4),
        "electricity_kwh": electricity_kwh,
        "grid_region": grid_key,
        "ef_kgco2e_per_kwh": ef,
        "source": EF_SOURCES["grid"],
    }


def calc_upstream_materials(items: list[dict]) -> dict:
    """
    items: [{"name": "铝", "kg": 0.27}, ...]
    Returns breakdown per material + total.
    """
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


def summarize_footprint(
    product_name: str,
    functional_unit: str,
    scope1: dict,
    scope2: dict,
    materials: dict,
    transport: dict,
    assumptions: list[str] | None = None,
) -> dict:
    s1 = scope1.get("value_kgco2e", 0.0)
    s2 = scope2.get("value_kgco2e", 0.0)
    mat = materials.get("total_kgco2e", 0.0)
    tr = transport.get("value_kgco2e", 0.0)
    total = s1 + s2 + mat + tr

    def pct(v):
        return round(v / total * 100) if total > 0 else 0

    breakdown = []
    if mat > 0:
        for item in materials.get("breakdown", []):
            breakdown.append({
                "source": f"上游原材料（{item['name']} {item['kg']}kg）",
                "value_kgco2e": item["value_kgco2e"],
                "pct": pct(item["value_kgco2e"]),
            })
    if s2 > 0:
        breakdown.append({
            "source": f"生产用电（{scope2['grid_region']}电网）",
            "value_kgco2e": s2,
            "pct": pct(s2),
        })
    if s1 > 0:
        breakdown.append({
            "source": f"直接燃料燃烧（{scope1.get('fuel_type', '')}）",
            "value_kgco2e": s1,
            "pct": pct(s1),
        })
    if tr > 0:
        breakdown.append({
            "source": f"运输（{transport.get('mode', '')} {transport.get('distance_km', 0)}km）",
            "value_kgco2e": tr,
            "pct": pct(tr),
        })

    breakdown.sort(key=lambda x: x["value_kgco2e"], reverse=True)
    hotspot = breakdown[0] if breakdown else None

    return {
        "product_name": product_name,
        "functional_unit": functional_unit,
        "total_kgco2e": round(total, 3),
        "analogy_km": round(total / _CAR_KG_PER_KM, 1),
        "breakdown": breakdown,
        "hotspot": hotspot["source"] if hotspot else None,
        "hotspot_pct": hotspot["pct"] if hotspot else 0,
        "unknowns": materials.get("unknowns", []),
        "assumptions": assumptions or [],
    }
