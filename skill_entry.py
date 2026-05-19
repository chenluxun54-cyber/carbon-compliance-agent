"""
skill_entry.py
双碳 Agent — 碳表现评分 Skill 完整入口
使用前：把第 14 行的 YOUR_API_KEY_HERE 替换成您的 Anthropic API Key
"""

import json
import sys
import os

# ─────────────────────────────────────────────────
# ★ 在这里填入您的 API Key ★
ANTHROPIC_API_KEY = "cr_5c871c335489e0bd9f0a5ae2a4f250b4bdf4e28e166b33d8d9747068e9ddc166"
# ─────────────────────────────────────────────────

import anthropic
from data_loader import DataLoader
from scorer import CarbonScorer

loader = DataLoader()
scorer = CarbonScorer()
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """
你是一位专业的双碳咨询顾问，负责向企业管理层解读碳表现评分报告。

输出格式要求：
1. 首行给出总分和行业分位排名的一句话总结
2. 用简洁段落逐维度点评，突出最强项和最弱项
3. 最后列出 2-3 个优先改善建议，每条要具体可操作
4. 如有数据缺失或估算项，在末尾单独说明
5. 全程使用中文，专业简洁，适合管理层阅读
"""


def generate_report(result: dict) -> str:
    """调用 Claude API 生成自然语言报告"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                "请根据以下评分结果，生成一份面向企业管理层的碳表现报告：\n\n"
                + json.dumps(result, ensure_ascii=False, indent=2)
            )
        }]
    )
    return response.content[0].text


def run(company_id: str, report_year: int) -> dict:
    """
    Skill 统一入口
    输入：企业ID + 年度
    输出：结构化评分 JSON + 自然语言报告
    """
    print(f"\n{'='*55}")
    print(f"  碳表现评分 Skill 启动")
    print(f"  企业：{company_id}　年度：{report_year}")
    print(f"{'='*55}")

    # Step 1：从 Excel 自动读取数据并计算排名
    print("\n[1/3] 读取 Excel 数据 + 计算行业排名...")
    data = loader.fetch(company_id, report_year)
    print(f"  ✅ {data['company_name']} / {data['industry']}")
    print(f"      行业样本：{data['sample_size']} 家企业")

    # Step 2：计算评分
    print("\n[2/3] 计算各维度评分...")
    result = scorer.score(data)
    print(f"  ✅ 总分：{result['total_score']} / 100")
    for dim in result["dimensions"]:
        bar = "█" * int(dim["percentage"] / 5) + "░" * (20 - int(dim["percentage"] / 5))
        print(f"      {dim['name']:<10} [{bar}] {dim['score']}/{dim['max_score']}")

    # Step 3：调用 Claude API 生成报告
    print("\n[3/3] 调用 Claude API 生成报告...")
    report = generate_report(result)
    print("  ✅ 报告生成完成\n")

    print("=" * 55)
    print(report)
    print("=" * 55)

    return {
        "score_result": result,
        "report": report
    }


if __name__ == "__main__":
    # 命令行用法：python skill_entry.py COMP_001 2024
    company_id  = sys.argv[1] if len(sys.argv) > 1 else "COMP_001"
    report_year = int(sys.argv[2]) if len(sys.argv) > 2 else 2024
    run(company_id, report_year)
