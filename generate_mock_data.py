"""
generate_mock_data.py
生成多行业、多年度的测试数据
"""
import pandas as pd
import numpy as np

np.random.seed(42)

INDUSTRIES = {
    "汽车零部件制造": [
        ("COMP_001", "某某制造有限公司"),
        ("COMP_002", "甲方科技制造股份有限公司"),
        ("COMP_003", "联合汽配有限公司"),
        ("COMP_004", "宏达零部件制造有限公司"),
        ("COMP_005", "新能汽配集团有限公司"),
        ("COMP_006", "星辰制造有限公司"),
        ("COMP_007", "远大零件股份有限公司"),
        ("COMP_008", "精工汽配有限公司"),
        ("COMP_009", "绿能制造有限公司"),
        ("COMP_010", "华北汽配有限公司"),
    ],
    "钢铁冶炼": [
        ("COMP_011", "北方钢铁集团有限公司"),
        ("COMP_012", "龙腾特钢股份有限公司"),
        ("COMP_013", "华东钢业有限公司"),
        ("COMP_014", "中原钢铁制造有限公司"),
        ("COMP_015", "西部钢业股份公司"),
        ("COMP_016", "南方特种钢有限公司"),
        ("COMP_017", "新世纪钢铁集团"),
        ("COMP_018", "绿色钢铁科技有限公司"),
        ("COMP_019", "精诚钢业有限公司"),
        ("COMP_020", "天工钢铁股份有限公司"),
    ],
    "化工制造": [
        ("COMP_021", "华化集团有限公司"),
        ("COMP_022", "新材料科技股份有限公司"),
        ("COMP_023", "绿色化工有限公司"),
        ("COMP_024", "精细化学品有限公司"),
        ("COMP_025", "东方化学工业集团"),
        ("COMP_026", "联合化工制造有限公司"),
        ("COMP_027", "中原化工股份有限公司"),
        ("COMP_028", "蓝天化学有限公司"),
        ("COMP_029", "远大化工集团有限公司"),
        ("COMP_030", "未来材料科技有限公司"),
    ],
}

# 行业碳排放特征参数（钢铁/化工排放更高，节能设备应用不同）
INDUSTRY_PARAMS = {
    "汽车零部件制造": dict(cpr=(0.5, 2.5), ei=(80, 300), cpe=(4, 20)),
    "钢铁冶炼":       dict(cpr=(2.0, 8.0), ei=(300, 900), cpe=(15, 60)),
    "化工制造":       dict(cpr=(1.5, 6.0), ei=(200, 700), cpe=(10, 40)),
}

CERT_CHOICES = ["national_green_factory", "iso14064_and_iso50001",
                "iso14064_or_iso50001", "provincial_green_factory", "none"]
CERT_PROBS   = [0.05, 0.10, 0.30, 0.20, 0.35]

TARGET_CHOICES = ["all_three", "two", "one", "none"]
TARGET_PROBS   = [0.10, 0.20, 0.30, 0.40]

DISC_CHOICES = ["independent_verified_framework", "independent_verified",
                "independent_unverified", "annual_report_partial", "none"]
DISC_PROBS   = [0.05, 0.15, 0.20, 0.30, 0.30]

SC_CHOICES = ["active", "passive", "none"]
SC_PROBS   = [0.20, 0.30, 0.50]


def make_row(cid, cname, industry, year, seed_offset=0):
    rng = np.random.RandomState(abs(hash(cid)) % (2**31) + year + seed_offset)
    p = INDUSTRY_PARAMS[industry]

    # 年份越新，各正向指标略有改善（模拟进步趋势）
    yr_factor = 1.0 + (year - 2022) * 0.03

    cpr = round(rng.uniform(*p["cpr"]), 4)
    ei  = round(rng.uniform(*p["ei"]), 1)
    cpe = round(rng.uniform(*p["cpe"]), 2)

    nfer = round(min(rng.uniform(5, 45) * yr_factor, 60), 1)
    rer  = round(min(rng.uniform(3, 35) * yr_factor, 50), 1)
    eser = round(rng.uniform(20, 85), 1)

    yoy_cr = round(max(rng.uniform(-5, 22), 0), 1) if year > 2022 else 0.0
    yoy_ci = round(max(rng.uniform(-3, 18), 0), 1) if year > 2022 else 0.0
    gir    = round(rng.uniform(0.1, 2.5), 3)
    agp    = round(rng.uniform(1, 12), 1)

    swur = round(rng.uniform(50, 99), 1)
    wrur = round(rng.uniform(40, 95), 1)
    crr  = round(rng.uniform(0, 4), 2)

    edac  = round(rng.uniform(20, 95) * yr_factor, 1)
    epf   = int(rng.randint(2, 13))
    pcfr  = round(rng.uniform(0, 85) * yr_factor, 1)
    scmc  = int(rng.randint(0, 7))

    cert   = rng.choice(CERT_CHOICES, p=CERT_PROBS)
    target = rng.choice(TARGET_CHOICES, p=TARGET_PROBS)
    disc   = rng.choice(DISC_CHOICES, p=DISC_PROBS)
    scd    = rng.choice(SC_CHOICES, p=SC_PROBS)
    lag    = int(rng.choice([1,2,3,4,5,6,8,10,14], p=[0.1,0.1,0.2,0.1,0.1,0.1,0.1,0.1,0.1]))

    return {
        "company_id":   cid, "company_name": cname,
        "industry":     industry, "report_year": year,
        "carbon_per_revenue":               cpr,
        "energy_intensity":                 ei,
        "carbon_per_employee":              cpe,
        "non_fossil_energy_ratio":          nfer,
        "renewable_electricity_ratio":      rer,
        "energy_saving_equipment_ratio":    eser,
        "yoy_carbon_reduction_rate":        yoy_cr,
        "yoy_carbon_intensity_improve":     yoy_ci,
        "green_investment_ratio":           gir,
        "avg_green_projects":               agp,
        "solid_waste_utilization_rate":     swur,
        "water_recycling_rate":             wrur,
        "carbon_removal_rate":              crr,
        "energy_data_auto_collection":      edac,
        "energy_platform_functions":        epf,
        "product_carbon_footprint_ratio":   pcfr,
        "supply_chain_measures_count":      scmc,
        "certification_type":               cert,
        "carbon_target_completeness":       target,
        "disclosure_level":                 disc,
        "supply_chain_disclosure":          scd,
        "data_submission_lag_months":       lag,
    }


rows = []
for industry, companies in INDUSTRIES.items():
    for cid, cname in companies:
        for year in [2022, 2023, 2024]:
            rows.append(make_row(cid, cname, industry, year))

# Fix COMP_001 as a mid-high performer (汽车零部件制造, all three years)
comp001_overrides = {
    "carbon_per_revenue":            0.85,
    "energy_intensity":              120.5,
    "carbon_per_employee":           8.2,
    "non_fossil_energy_ratio":       18.5,
    "renewable_electricity_ratio":   12.0,
    "energy_saving_equipment_ratio": 45.0,
    "green_investment_ratio":        0.8,
    "avg_green_projects":            5.0,
    "solid_waste_utilization_rate":  85.0,
    "water_recycling_rate":          70.0,
    "carbon_removal_rate":           0.0,
    "energy_data_auto_collection":   60.0,
    "energy_platform_functions":     7,
    "product_carbon_footprint_ratio":50.0,
    "supply_chain_measures_count":   2,
    "certification_type":            "iso14064_or_iso50001",
    "carbon_target_completeness":    "two",
    "disclosure_level":              "independent_verified",
    "supply_chain_disclosure":       "active",
    "data_submission_lag_months":    2,
}
year_overrides = {2022: {"yoy_carbon_reduction_rate": 3.1, "yoy_carbon_intensity_improve": 5.4},
                  2023: {"yoy_carbon_reduction_rate": 8.3, "yoy_carbon_intensity_improve": 12.1},
                  2024: {"yoy_carbon_reduction_rate": 8.3, "yoy_carbon_intensity_improve": 12.1}}

for row in rows:
    if row["company_id"] == "COMP_001":
        row.update(comp001_overrides)
        row.update(year_overrides[row["report_year"]])

df = pd.DataFrame(rows)

with pd.ExcelWriter("carbon_database.xlsx", engine="openpyxl") as writer:
    df.to_excel(writer, sheet_name="company_data", index=False)
    pd.DataFrame().to_excel(writer, sheet_name="industry_rankings", index=False)

print(f"✅ 数据生成完成：carbon_database.xlsx")
print(f"   {len(df)} 条记录 | {len(INDUSTRIES)} 个行业 | {sum(len(v) for v in INDUSTRIES.values())} 家企业 | 2022-2024 年")
