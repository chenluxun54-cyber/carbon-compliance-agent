"""
Emission factor database — Tier-1 hardcoded values for MVP Phase 1.
All values are traceable to a named source.
"""
from __future__ import annotations

# kgCO2e/kWh — China regional grid, 2023 national average
# Source: China Electricity Council (中电联) 2023 grid emission factors
GRID_FACTORS: dict[str, float] = {
    "华东": 0.5810,
    "华南": 0.5271,
    "华北": 0.6419,
    "东北": 0.5669,
    "西北": 0.6569,
    "西南": 0.3394,
    "华中": 0.5257,
    "全国平均": 0.5568,
}

# kgCO2e/kg material — upstream production (cradle-to-gate)
# Source: Ecoinvent v3.9 proxy values, China geography where available
MATERIAL_FACTORS: dict[str, float] = {
    "钢铁": 1.85,
    "铝": 8.24,
    "再生铝": 0.68,
    "铜": 3.96,
    "塑料_PP": 1.94,
    "塑料_PE": 1.87,
    "塑料_ABS": 3.15,
    "玻璃": 0.86,
    "纸板": 0.94,
    "木材": 0.46,
    "PCB电路板": 28.5,
    "锂电池": 12.3,
    "橡胶": 2.85,
    "陶瓷": 0.73,
    "棉花": 5.89,
    "不锈钢": 6.15,
}

# kgCO2e/unit fuel — direct combustion (Scope 1)
# Source: IPCC AR6 Table A.IV.2 / China GHG accounting guidelines
FUEL_FACTORS: dict[str, float] = {
    "天然气_m3": 2.162,
    "柴油_L": 2.630,
    "汽油_L": 2.310,
    "煤_kg": 2.660,
    "液化石油气_kg": 3.101,
}

# kgCO2e/tonne-km — freight transport
# Source: GLEC Framework 2023 / China MOT emission factors
TRANSPORT_FACTORS: dict[str, float] = {
    "公路": 0.0965,
    "铁路": 0.0209,
    "海运": 0.0102,
    "航空": 0.6020,
}

# Province → grid region mapping
PROVINCE_TO_GRID: dict[str, str] = {
    "上海": "华东", "浙江": "华东", "江苏": "华东", "安徽": "华东", "福建": "华东",
    "广东": "华南", "广西": "华南", "海南": "华南",
    "北京": "华北", "天津": "华北", "河北": "华北", "山西": "华北",
    "山东": "华北", "内蒙古": "华北",
    "辽宁": "东北", "吉林": "东北", "黑龙江": "东北",
    "陕西": "西北", "甘肃": "西北", "青海": "西北", "宁夏": "西北", "新疆": "西北",
    "四川": "西南", "重庆": "西南", "贵州": "西南", "西藏": "西南", "云南": "西南",
    "湖北": "华中", "湖南": "华中", "江西": "华中", "河南": "华中",
}

EF_SOURCES = {
    "grid": "中国电力企业联合会 2023年电网排放因子",
    "material": "Ecoinvent v3.9 / 中国地区数据",
    "fuel": "IPCC AR6 / 中国温室气体核算指南",
    "transport": "GLEC Framework 2023 / 中国交通运输部",
}


def resolve_region(location: str) -> str:
    """Map a province name or grid region name to a canonical GRID_FACTORS key."""
    if location in GRID_FACTORS:
        return location
    for province, grid in PROVINCE_TO_GRID.items():
        if province in location:
            return grid
    return "全国平均"


def get_material_factor(name: str) -> tuple[float | None, str | None]:
    """Fuzzy-match a material name to MATERIAL_FACTORS. Returns (ef, matched_key)."""
    if name in MATERIAL_FACTORS:
        return MATERIAL_FACTORS[name], name
    for key, val in MATERIAL_FACTORS.items():
        if key in name or name in key:
            return val, key
    return None, None
