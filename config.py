EXCEL_PATH = "carbon_database.xlsx"
SHEET_COMPANY = "company_data"
SHEET_RANKINGS = "industry_rankings"

LINEAR_INDICATORS = [
    ("carbon_per_revenue",              13, True),
    ("energy_intensity",                10, True),
    ("carbon_per_employee",              5, True),
    ("non_fossil_energy_ratio",          8, False),
    ("renewable_electricity_ratio",      7, False),
    ("energy_saving_equipment_ratio",    2, False),
    ("yoy_carbon_reduction_rate",        7, False),
    ("yoy_carbon_intensity_improve",     6, False),
    ("green_investment_ratio",           4, False),
    ("avg_green_projects",               1, False),
    ("solid_waste_utilization_rate",     5, False),
    ("water_recycling_rate",             4, False),
    ("carbon_removal_rate",              2, False),
    ("energy_data_auto_collection",      5, False),
    ("energy_platform_functions",        5, False),
    ("product_carbon_footprint_ratio",   5, False),
    ("supply_chain_measures_count",      3, False),
]

DIRECT_SCORING_RULES = {
    "certification_type": {
        "national_green_factory":     2.0,
        "iso14064_and_iso50001":      1.8,
        "iso14064_or_iso50001":       1.2,
        "provincial_green_factory":   1.0,
        "none":                       0.0,
    },
    "carbon_target_completeness": {
        "all_three":  1.0,
        "two":        0.7,
        "one":        0.4,
        "none":       0.0,
    },
    "disclosure_level": {
        "independent_verified_framework": 3.0,
        "independent_verified":           2.4,
        "independent_unverified":         1.8,
        "annual_report_partial":          1.0,
        "none":                           0.0,
    },
    "supply_chain_disclosure": {
        "active":   1.0,
        "passive":  0.5,
        "none":     0.0,
    },
}

DIMENSIONS = [
    {
        "id": "D1", "name": "碳排放强度", "max_score": 28,
        "indicators": [
            "carbon_per_revenue", "energy_intensity", "carbon_per_employee"
        ]
    },
    {
        "id": "D2", "name": "能源结构清洁度", "max_score": 17,
        "indicators": [
            "non_fossil_energy_ratio", "renewable_electricity_ratio",
            "energy_saving_equipment_ratio"
        ]
    },
    {
        "id": "D3", "name": "减碳动态表现", "max_score": 18,
        "indicators": [
            "yoy_carbon_reduction_rate", "yoy_carbon_intensity_improve",
            "green_investment_ratio", "avg_green_projects"
        ]
    },
    {
        "id": "D4", "name": "资源利用效率", "max_score": 11,
        "indicators": [
            "solid_waste_utilization_rate", "water_recycling_rate",
            "carbon_removal_rate"
        ]
    },
    {
        "id": "D5", "name": "碳管理成熟度", "max_score": 21,
        "indicators": [
            "energy_data_auto_collection", "energy_platform_functions",
            "product_carbon_footprint_ratio", "supply_chain_measures_count",
            "certification_type", "carbon_target_completeness"
        ]
    },
    {
        "id": "D6", "name": "信息披露透明度", "max_score": 5,
        "indicators": [
            "disclosure_level", "supply_chain_disclosure",
            "data_submission_lag_months"
        ]
    },
]
