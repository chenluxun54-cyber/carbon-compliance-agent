"""
test_skill.py
完整测试，不依赖Claude API，使用模拟报告文字验证流程
"""
import sys
import json
from data_loader import DataLoader
from scorer import CarbonScorer

loader = DataLoader()
scorer = CarbonScorer()


def generate_report_text(result: dict) -> str:
    """
    本地报告生成（测试用，不调用API）
    正式版替换为 Claude API 调用
    """
    total = result["total_score"]
    sample = result["sample_size"]
    company = result["company_name"]
    year = result["report_year"]
    dims = result["dimensions"]

    # 计算行业分位
    # 此处模拟，正式版由历史数据库算
    percentile_mock = 0.72
    rank_str = f"行业前 {round((1 - percentile_mock) * 100)}%"

    lines = []
    lines.append("=" * 55)
    lines.append(f"  碳表现评分报告")
    lines.append(f"  {company}　{year} 年度")
    lines.append("=" * 55)

    lines.append(f"\n【总分】{total} / 100 分　　{rank_str}")
    lines.append(f"参考样本：同行业 {sample} 家企业\n")

    lines.append("【各维度得分】")
    best = max(dims, key=lambda d: d["percentage"])
    worst = min(dims, key=lambda d: d["percentage"])

    for d in dims:
        bar_filled = int(d["percentage"] / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        tag = ""
        if d["id"] == best["id"]:
            tag = "  ⭐ 最强项"
        elif d["id"] == worst["id"]:
            tag = "  ⚠️  最弱项"
        lines.append(
            f"  {d['name']:<10} [{bar}] "
            f"{d['score']:>4.1f}/{d['max_score']}分 "
            f"({d['percentage']}%){tag}"
        )

    lines.append(f"\n【强项】{best['name']}（{best['percentage']}%）")
    lines.append(f"  表现优秀，建议保持并对外宣传以提升品牌形象。")

    lines.append(f"\n【最需改善】{worst['name']}（{worst['percentage']}%）")
    lines.append(f"  该维度得分偏低，是提升综合排名的优先突破口。")

    # 找出子指标最低3项
    all_indicators = []
    for d in dims:
        for iid, idata in d["indicators"].items():
            if idata.get("method") == "linear":
                pct = idata.get("percentile", 0)
                all_indicators.append((iid, d["name"], pct, idata["score"], idata["max_score"]))
    all_indicators.sort(key=lambda x: x[2])

    lines.append("\n【优先改善的子指标 Top 3】")
    for i, (iid, dname, pct, sc, ms) in enumerate(all_indicators[:3], 1):
        lines.append(
            f"  {i}. [{dname}] {iid}　"
            f"当前得分 {sc}/{ms}（行业分位 {round(pct*100,1)}%）"
        )

    if result["flags"]:
        lines.append("\n【数据说明】")
        for f in result["flags"]:
            lines.append(f"  · {f}")

    lines.append("\n" + "=" * 55)
    return "\n".join(lines)


def run_test(company_id: str, report_year: int):
    print(f"\n{'='*55}")
    print(f"  开始测试：{company_id} / {report_year} 年")
    print(f"{'='*55}")

    # Step 1: 读取数据
    print("\n[1/3] 从 Excel 读取数据 + 计算排名...")
    data = loader.fetch(company_id, report_year)
    print(f"  ✅ 读取成功")
    print(f"  企业：{data['company_name']}")
    print(f"  行业：{data['industry']}")
    print(f"  样本量：{data['sample_size']} 家企业")
    print(f"  排名字段样例：rank_carbon_per_revenue = {data.get('rank_carbon_per_revenue')}")

    # Step 2: 计算评分
    print("\n[2/3] 计算评分...")
    result = scorer.score(data)
    print(f"  ✅ 评分完成：总分 {result['total_score']} / 100")
    for dim in result["dimensions"]:
        print(f"     {dim['name']}: {dim['score']}/{dim['max_score']}")

    # Step 3: 生成报告
    print("\n[3/3] 生成报告...")
    report = generate_report_text(result)
    print("\n" + report)

    # 输出完整JSON供调试
    print("\n【完整评分 JSON（供调试）】")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    return result


def run_all_tests():
    # 测试1：正常流程
    print("\n" + "🧪 " * 20)
    print("测试 1：正常流程（COMP_001 / 2024）")
    run_test("COMP_001", 2024)

    # 测试2：历史数据拉取
    print("\n" + "🧪 " * 20)
    print("测试 2：历史数据拉取（COMP_001 近3年）")
    history = loader.fetch_history("COMP_001", years=3)
    print(f"  ✅ 拉取到 {len(history)} 年历史数据")
    for h in history:
        print(f"     {h['report_year']} 年：碳强度 {h.get('carbon_per_revenue')} tCO₂e/万元")

    # 测试3：企业不存在
    print("\n" + "🧪 " * 20)
    print("测试 3：企业ID不存在（应报错）")
    try:
        loader.fetch("COMP_999", 2024)
    except ValueError as e:
        print(f"  ✅ 正确捕获错误：{e}")

    # 测试4：另一家企业对比
    print("\n" + "🧪 " * 20)
    print("测试 4：对比企业 COMP_005 / 2024")
    run_test("COMP_005", 2024)

    print("\n" + "✅ " * 20)
    print("全部测试完成")


if __name__ == "__main__":
    company_id = sys.argv[1] if len(sys.argv) > 1 else None

    if company_id:
        year = int(sys.argv[2]) if len(sys.argv) > 2 else 2024
        run_test(company_id, year)
    else:
        run_all_tests()
