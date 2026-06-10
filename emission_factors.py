"""
Emission factor database — Phase 2 expanded (16 → 45 materials).
All values are traceable to named sources.
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
    # Metals
    "钢铁": 1.85,
    "铸铁": 1.51,
    "不锈钢": 6.15,
    "铝": 8.24,
    "再生铝": 0.68,
    "铜": 3.96,
    "锌": 3.86,
    "锡": 16.1,
    "镍": 13.3,
    "铅": 1.69,
    "钛": 35.5,
    # Plastics
    "塑料_PP": 1.94,
    "塑料_PE": 1.87,
    "塑料_HDPE": 1.93,
    "塑料_LDPE": 2.01,
    "塑料_ABS": 3.15,
    "塑料_PET": 2.73,
    "塑料_PVC": 3.10,
    "塑料_PC": 6.33,
    "塑料_PA": 7.91,   # 尼龙
    "塑料_PS": 3.41,
    "塑料_PU": 4.20,   # 聚氨酯
    # Composites & Advanced
    "玻璃": 0.86,
    "碳纤维": 29.5,
    "玻璃纤维": 2.54,
    "环氧树脂": 6.34,
    "合成橡胶": 3.68,
    "橡胶": 2.85,
    "硅材料": 45.2,
    # Natural / Bio-based
    "纸板": 0.94,
    "木材": 0.46,
    "棉花": 5.89,
    "涤纶": 5.55,
    "尼龙": 7.91,
    "羊毛": 24.5,
    "皮革": 17.0,
    # Electronics
    "PCB电路板": 28.5,
    "锂电池": 12.3,
    "芯片_半导体": 120.0,
    "电容": 18.7,
    "稀土_混合": 35.0,
    # Chemicals & Coatings
    "油漆_溶剂型": 3.71,
    "油漆_水性": 1.85,
    "润滑油": 0.87,
    "陶瓷": 0.73,
    # Food / Bio (future verticals)
    "小麦": 0.63,
    "大豆": 1.76,
    "猪肉": 7.61,
    "牛肉": 27.0,
}

# kgCO2e/kg packaging material — upstream production
# Source: Ecoinvent v3.9 / Franklin Associates
PACKAGING_FACTORS: dict[str, float] = {
    "瓦楞纸箱": 1.12,
    "PE薄膜": 2.43,
    "泡沫塑料_EPS": 3.29,
    "铝箔": 11.5,
    "玻璃瓶": 0.91,
    "铁罐": 2.14,
    "木托盘": 0.31,
    "PP包装": 1.94,
    "PET瓶": 2.73,
    "纸袋": 0.80,
}

# kgCO2e/kg — end-of-life treatment (generic mixed waste)
# Source: IPCC AR6 / Ecoinvent waste treatment processes
EOL_FACTORS: dict[str, float] = {
    "填埋": 0.48,    # landfill with gas capture
    "焚烧": 1.85,    # incineration, average plastics/mixed
    "回收": -0.35,   # recycling credit (avoided primary production)
    "堆肥": 0.12,    # composting (bio-based materials only)
    "未知": 0.48,    # default to landfill
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
    "packaging": "Ecoinvent v3.9 / Franklin Associates 包装生命周期数据",
    "eol": "IPCC AR6 / Ecoinvent 废物处理过程数据",
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


def _fuzzy_match(name: str, factors: dict) -> tuple:
    """Generic fuzzy match against a factors dict. Returns (value, matched_key)."""
    if name in factors:
        return factors[name], name
    for key, val in factors.items():
        if key in name or name in key:
            return val, key
    return None, None


def get_material_factor(name: str) -> tuple:
    """Fuzzy-match a material name to MATERIAL_FACTORS. Returns (ef, matched_key)."""
    return _fuzzy_match(name, MATERIAL_FACTORS)


def get_packaging_factor(name: str) -> tuple:
    """Fuzzy-match a packaging material name. Returns (ef, matched_key)."""
    return _fuzzy_match(name, PACKAGING_FACTORS)
