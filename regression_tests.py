"""
regression_tests.py — Carbon Compliance Agent 回归测试集

覆盖三个子系统的确定性行为：
  S1  企业碳评分 (scorer.py / data_loader.py)
  S2  政策库    (policies.py)
  S3  碳足迹计算引擎 (calculator.py / compliance.py)

运行方式：
  cd ~/Desktop/carbon_skill
  python3 regression_tests.py

不依赖 LLM / HTTP server，全部纯函数调用，秒级完成。
每个 assert 失败时会打印差异，最后汇总通过/失败数。
"""

import sys
import traceback
from data_loader import DataLoader
from scorer import CarbonScorer
from policies import POLICIES
from calculator import (
    calc_scope1_fuel, calc_scope2, calc_upstream_materials,
    calc_packaging, calc_transport, calc_end_of_life, summarize_footprint,
)
from compliance import check_cbam, iso14067_checklist

loader = DataLoader()
scorer = CarbonScorer()

PASS = "✅ PASS"
FAIL = "❌ FAIL"

results = []


def _d(result: dict, dim_id: str) -> float:
    for d in result["dimensions"]:
        if d["id"] == dim_id:
            return d["score"]
    raise KeyError(dim_id)


def expect(name: str, actual, expected, tolerance=None):
    if tolerance is not None:
        ok = abs(actual - expected) <= tolerance
    else:
        ok = actual == expected
    status = PASS if ok else FAIL
    results.append((name, ok))
    if not ok:
        print(f"  {FAIL}  {name}")
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")
    else:
        print(f"  {PASS}  {name}")


def expect_raises(name: str, fn, exc_type, substring=None):
    try:
        fn()
        results.append((name, False))
        print(f"  {FAIL}  {name}  (no exception raised)")
    except exc_type as e:
        ok = (substring is None) or (substring in str(e))
        results.append((name, ok))
        if ok:
            print(f"  {PASS}  {name}")
        else:
            print(f"  {FAIL}  {name}  (wrong message: {e})")
    except Exception as e:
        results.append((name, False))
        print(f"  {FAIL}  {name}  (wrong exception type: {type(e).__name__}: {e})")


# ---------------------------------------------------------------------------
# S1 — 企业碳评分
# ---------------------------------------------------------------------------
print("\n── S1  企业碳评分 ──────────────────────────────")

# COMP_001 / 2024: golden values captured 2026-07-01
data1 = loader.fetch("COMP_001", 2024)
r1 = scorer.score(data1)
expect("S1-01  COMP_001 总分",        r1["total_score"],  55.8)
expect("S1-02  COMP_001 样本量",       r1["sample_size"],  10)
expect("S1-03  COMP_001 行业",         r1["industry"],     "汽车零部件制造")
expect("S1-04  COMP_001 D1 碳排放强度",  _d(r1,"D1"),      20.1)
expect("S1-05  COMP_001 D2 能源结构",    _d(r1,"D2"),       6.3)
expect("S1-06  COMP_001 D3 减碳动态",    _d(r1,"D3"),       7.7)
expect("S1-07  COMP_001 D4 资源利用",    _d(r1,"D4"),       7.0)
expect("S1-08  COMP_001 D5 碳管理",      _d(r1,"D5"),      10.3)
expect("S1-09  COMP_001 D6 信息披露",    _d(r1,"D6"),       4.4)

# COMP_005 / 2024
data5 = loader.fetch("COMP_005", 2024)
r5 = scorer.score(data5)
expect("S1-10  COMP_005 总分",  r5["total_score"], 48.8)
expect("S1-11  COMP_005 D5",    _d(r5,"D5"),       14.7)

# COMP_010 / 2024
data10 = loader.fetch("COMP_010", 2024)
r10 = scorer.score(data10)
expect("S1-12  COMP_010 总分",  r10["total_score"], 43.1)

# 历史数据拉取
hist = loader.fetch_history("COMP_001", years=3)
expect("S1-13  COMP_001 历史3年条数",   len(hist),              3)
expect("S1-14  COMP_001 历史年份集合",  sorted(h["report_year"] for h in hist), [2022, 2023, 2024])

# 输出 JSON 结构校验
expect("S1-15  输出包含 company_id",    "company_id"   in r1, True)
expect("S1-16  输出包含 total_score",   "total_score"  in r1, True)
expect("S1-17  输出包含 dimensions",    "dimensions"   in r1, True)
expect("S1-18  输出包含 flags",         "flags"        in r1, True)
expect("S1-19  输出不含 main_hotspot",  "main_hotspot" not in r1, True)
expect("S1-20  dimensions 共6项",       len(r1["dimensions"]), 6)

# 企业不存在
expect_raises("S1-21  COMP_999 抛 ValueError",
              lambda: loader.fetch("COMP_999", 2024),
              ValueError, "COMP_999")


# ---------------------------------------------------------------------------
# S2 — 政策库
# ---------------------------------------------------------------------------
print("\n── S2  政策库 ──────────────────────────────────")

def _search(keyword=None, industry=None, jurisdiction=None):
    data = POLICIES
    if jurisdiction:
        data = [p for p in data if p["jurisdiction"] == jurisdiction]
    if industry:
        data = [p for p in data if industry in p.get("industries", []) or "all" in p.get("industries", [])]
    if keyword:
        kw = keyword.lower()
        data = [p for p in data if kw in p["name"].lower() or kw in p["summary"].lower()]
    return data

all_p = _search()
expect("S2-01  全部政策共15条",           len(all_p), 15)

china_p = _search(jurisdiction="中国")
expect("S2-02  中国政策共6条",            len(china_p), 6)
expect("S2-03  中国政策含 CN_ETS",        any(p["id"] == "CN_ETS" for p in china_p), True)
expect("S2-04  中国政策含 CN_3060",       any(p["id"] == "CN_3060" for p in china_p), True)

eu_p = _search(jurisdiction="欧盟")
expect("S2-05  欧盟政策共3条",            len(eu_p), 3)
expect("S2-06  欧盟政策含 CBAM",          any(p["id"] == "CBAM" for p in eu_p), True)
expect("S2-07  欧盟政策含 EU_ETS",        any(p["id"] == "EU_ETS" for p in eu_p), True)
expect("S2-08  欧盟政策含 CSRD",          any(p["id"] == "CSRD" for p in eu_p), True)

cbam_p = _search(keyword="CBAM")
expect("S2-09  关键词 CBAM 匹配1条",      len(cbam_p), 1)
expect("S2-10  匹配结果 id=CBAM",         cbam_p[0]["id"], "CBAM")

none_p = _search(keyword="ZZZZNOTEXIST")
expect("S2-11  无匹配返回空列表",          none_p, [])

# 政策详情字段检查
cbam_detail = next(p for p in POLICIES if p["id"] == "CBAM")
expect("S2-12  CBAM 地区=欧盟",           cbam_detail["jurisdiction"], "欧盟")
expect("S2-13  CBAM 有 key_requirements", len(cbam_detail.get("key_requirements", [])) >= 1, True)
expect("S2-14  CBAM 有 compliance_examples", len(cbam_detail.get("compliance_examples", [])) >= 1, True)

# 合规案例字段完整性
ex = cbam_detail["compliance_examples"][0]
for field in ["company", "country", "industry", "problem", "action", "result"]:
    expect(f"S2-15  CBAM example.{field} 存在", field in ex, True)

# AND 过滤: 欧盟 + keyword
eu_cbam = _search(keyword="CBAM", jurisdiction="欧盟")
expect("S2-16  欧盟+CBAM AND 过滤正确",   len(eu_cbam), 1)

# 欧盟 + 不存在关键词
eu_none = _search(keyword="ZZZZNOTEXIST", jurisdiction="欧盟")
expect("S2-17  欧盟+无效关键词返回空",    eu_none, [])


# ---------------------------------------------------------------------------
# S3 — 碳足迹计算引擎
# ---------------------------------------------------------------------------
print("\n── S3  碳足迹计算引擎 ──────────────────────────")

def _footprint(product_name, weight_kg, region, electricity_kwh, materials,
               fuel_type="", fuel_qty=0, transport_km=0, transport_mode="公路",
               packaging=None, eol_method="", eol_pct=0.0, functional_unit="每件"):
    scope1 = calc_scope1_fuel(fuel_type, fuel_qty)
    scope2 = calc_scope2(electricity_kwh, region)
    upstream = calc_upstream_materials(materials)
    transport = calc_transport(weight_kg, transport_km, transport_mode)
    pkg = calc_packaging(packaging or [])
    eol_weight = weight_kg + pkg["total_kgco2e"]  # approximation; use weight for eol
    eol = calc_end_of_life(weight_kg, eol_method, eol_pct) if eol_method else {"value_kgco2e": 0.0, "skipped": True}
    summary = summarize_footprint(
        product_name=product_name,
        functional_unit=functional_unit,
        scope1=scope1,
        scope2=scope2,
        materials=upstream,
        transport=transport,
        packaging=pkg,
        end_of_life=eol,
    )
    return summary, upstream, scope2, transport, pkg, eol

# ── Case A: 铝制水杯（浙江，已知材料）──
sumA, upA, s2A, trA, pkA, eolA = _footprint(
    product_name="铝制水杯", weight_kg=0.3,
    region="浙江", electricity_kwh=0.8,
    materials=[{"name": "铝", "kg": 0.27}, {"name": "塑料", "kg": 0.03}],
)
expect("S3-01  铝杯 scope2 值",            s2A["value_kgco2e"],  0.4648)
expect("S3-02  铝杯 upstream 合计",        upA["total_kgco2e"],  2.283)
expect("S3-03  铝杯 total_kgco2e",         sumA["total_kgco2e"], 2.748)
expect("S3-04  铝杯 unknowns 为空",        upA["unknowns"],       [])
expect("S3-05  铝杯 hotspot 含'铝'",       "铝" in sumA.get("hotspot",""), True)

# ── Case B: 竹纤维杯（未知材料降级）──
sumB, upB, s2B, *_ = _footprint(
    product_name="竹纤维杯", weight_kg=0.2,
    region="四川", electricity_kwh=0.5,
    materials=[{"name": "竹纤维", "kg": 0.2}],
)
expect("S3-06  竹纤维杯 unknowns=['竹纤维']", upB["unknowns"], ["竹纤维"])
expect("S3-07  竹纤维杯 upstream=0",          upB["total_kgco2e"], 0.0)
# total = scope2 only (bamboo skipped)
expect("S3-08  竹纤维杯 total≈scope2",         sumB["total_kgco2e"], s2B["value_kgco2e"], tolerance=0.01)

# ── Case C: 锂电池（含运输+包装+回收报废）──
scope1C = calc_scope1_fuel("", 0)
scope2C = calc_scope2(5.0, "江苏")
upC = calc_upstream_materials([{"name": "锂电池", "kg": 2.0}])
trC = calc_transport(2.0, 800, "公路")
pkC = calc_packaging([{"name": "瓦楞纸", "kg": 0.1}])
eolC = calc_end_of_life(2.1, "回收")
sumC = summarize_footprint(
    product_name="锂电池", functional_unit="每件锂电池（2kg）",
    scope1=scope1C, scope2=scope2C, materials=upC,
    transport=trC, packaging=pkC, end_of_life=eolC,
)
expect("S3-09  锂电池 scope2",              scope2C["value_kgco2e"], 2.905)
expect("S3-10  锂电池 upstream",            upC["total_kgco2e"],     24.6)
expect("S3-11  锂电池 transport",           trC["value_kgco2e"],     0.1544)
expect("S3-12  锂电池 packaging",           pkC["total_kgco2e"],     0.112)
expect("S3-13  锂电池 eol (负值=碳汇)",    eolC["value_kgco2e"],    -0.7350)
expect("S3-14  锂电池 total_kgco2e",        sumC["total_kgco2e"],    27.036)
expect("S3-15  锂电池 unknowns 为空",       upC["unknowns"],          [])

# ── Case D: 运输 distance=0 跳过 ──
tr_zero = calc_transport(1.0, 0, "公路")
expect("S3-16  transport distance=0 跳过", tr_zero["value_kgco2e"], 0.0)
expect("S3-17  transport distance=0 有skipped字段", tr_zero.get("skipped"), True)

# ── Case E: 未知省份 → 使用默认因子 ──
scope2_unknown = calc_scope2(1.0, "火星省")
expect("S3-18  未知省份有warning字段",     "warning" in scope2_unknown, True)
expect("S3-19  未知省份 ef>0",             scope2_unknown["ef_kgco2e_per_kwh"] > 0, True)

# ── Case F: 燃料 qty<=0 跳过 ──
fuel_zero = calc_scope1_fuel("天然气", 0)
expect("S3-20  fuel qty=0 跳过",          fuel_zero["value_kgco2e"], 0.0)
expect("S3-21  fuel qty=0 有skipped字段", fuel_zero.get("skipped"), True)

# ── Case G: CBAM 检查 ──
cbam_al = check_cbam([{"name": "铝", "kg": 0.27}], total_kgco2e=2.748)
expect("S3-22  铝 covered=True",           cbam_al["covered"], True)
expect("S3-23  铝 matched_sectors含'铝'",  "铝" in cbam_al["matched_sectors"], True)
expect("S3-24  铝 cost_eur_estimate>0",    cbam_al["cost_eur_estimate"] > 0, True)

cbam_bamboo = check_cbam([{"name": "竹纤维", "kg": 0.2}], total_kgco2e=0.5)
expect("S3-25  竹纤维 covered=False",      cbam_bamboo["covered"], False)
expect("S3-26  竹纤维 matched_sectors=[]", cbam_bamboo["matched_sectors"], [])

# ── Case H: ISO 14067 清单 ──
# 铝杯 with proper functional unit → should be well-scored
scope2H = calc_scope2(0.8, "浙江")
upH = calc_upstream_materials([{"name": "铝", "kg": 0.27}, {"name": "塑料", "kg": 0.03}])
sumH = summarize_footprint(
    product_name="铝制水杯", functional_unit="每件铝制水杯（0.3kg）",
    scope1=calc_scope1_fuel("", 0), scope2=scope2H, materials=upH,
    transport=calc_transport(0.3, 0, "公路"),
    packaging=calc_packaging([]), end_of_life={"value_kgco2e": 0.0, "skipped": True},
)
iso = iso14067_checklist(sumH)
expect("S3-27  ISO 14067清单共9项",        len(iso), 9)
statuses = {item["requirement"]: item["status"] for item in iso}
expect("S3-28  温室气体覆盖声明=pass",     statuses.get("温室气体覆盖范围已声明"), "pass")
expect("S3-29  分配方法声明=pass",         statuses.get("分配方法已声明"),         "pass")
expect("S3-30  功能单位非'每件'→pass",     statuses.get("功能单位已声明"),         "pass")

# 功能单位仅"每件" → partial
sumH2 = {**sumH, "functional_unit": "每件"}
iso2 = iso14067_checklist(sumH2)
s2 = {item["requirement"]: item["status"] for item in iso2}
expect("S3-31  功能单位='每件'→partial",   s2.get("功能单位已声明"), "partial")

# unknowns=3种 → fail
sumH3 = {**sumH, "unknowns": ["A", "B", "C"]}
iso3 = iso14067_checklist(sumH3)
s3 = {item["requirement"]: item["status"] for item in iso3}
expect("S3-32  3种unknown→不确定材料=fail", s3.get("不确定材料已披露"), "fail")

# unknowns=1种 → partial
sumH4 = {**sumH, "unknowns": ["A"]}
iso4 = iso14067_checklist(sumH4)
s4 = {item["requirement"]: item["status"] for item in iso4}
expect("S3-33  1种unknown→不确定材料=partial", s4.get("不确定材料已披露"), "partial")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("\n── 结果汇总 ─────────────────────────────────────")
passed = sum(1 for _, ok in results if ok)
failed = len(results) - passed
print(f"\n  {passed}/{len(results)} passed,  {failed} failed")
if failed:
    print("\n  失败项：")
    for name, ok in results:
        if not ok:
            print(f"    • {name}")
    sys.exit(1)
else:
    print("\n  全部通过 ✅")


