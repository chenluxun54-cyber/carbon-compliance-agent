"""
HTML methodology report generator for product carbon footprint results.
Self-contained HTML (inline CSS, no external dependencies).
"""
from __future__ import annotations

from datetime import date


def generate_report_html(result: dict) -> str:
    product = result.get("product_name", "未知产品")
    fu = result.get("functional_unit", "每件")
    total = result.get("total_kgco2e", 0)
    analogy = result.get("analogy_km", 0)
    hotspot = result.get("hotspot", "")
    hotspot_pct = result.get("hotspot_pct", 0)
    breakdown = result.get("breakdown", [])
    assumptions = result.get("assumptions", [])
    unknowns = result.get("unknowns", [])
    scope_summary = result.get("scope_summary", {})
    today = date.today().strftime("%Y年%m月%d日")

    # ── Breakdown table rows ──
    rows_html = ""
    for item in breakdown:
        v = item.get("value_kgco2e", 0)
        p = item.get("pct", 0)
        bar_color = "#16a34a" if v >= 0 else "#2563eb"
        sign = "+" if v >= 0 else ""
        rows_html += f"""
        <tr>
          <td>{item['source']}</td>
          <td style="text-align:right;font-weight:600">{sign}{v:.3f} kg</td>
          <td style="text-align:right">{p}%</td>
          <td style="width:140px">
            <div style="background:#f3f4f6;border-radius:4px;height:10px;overflow:hidden">
              <div style="background:{bar_color};width:{min(abs(p),100)}%;height:10px"></div>
            </div>
          </td>
        </tr>"""

    # ── Scope summary rows ──
    scope_labels = {
        "scope1_direct": "范围一：直接排放（燃料燃烧）",
        "scope2_electricity": "范围二：间接排放（生产用电）",
        "scope3_upstream_materials": "范围三：上游原材料",
        "scope3_packaging": "范围三：包装材料",
        "scope3_transport": "范围三：运输",
        "scope3_end_of_life": "范围三：报废处置",
    }
    scope_rows = ""
    for key, label in scope_labels.items():
        v = scope_summary.get(key, 0)
        if v == 0:
            continue
        sign = "+" if v >= 0 else ""
        scope_rows += f"<tr><td>{label}</td><td style='text-align:right;font-weight:600'>{sign}{v:.3f} kg CO₂e</td></tr>"

    # ── EF source table ──
    ef_rows = ""
    seen_sources = set()
    for item in breakdown:
        src = item.get("ef_source", "")
        ef = item.get("ef_used")
        if src and src not in seen_sources:
            ef_str = f"{ef:.4f}" if ef is not None else "—"
            ef_rows += f"<tr><td>{item['source']}</td><td>{ef_str}</td><td>{src}</td></tr>"
            seen_sources.add(src)

    # ── Assumptions ──
    assumptions_html = ""
    if assumptions:
        items_html = "".join(f"<li>{a}</li>" for a in assumptions)
        assumptions_html = f"<ul style='margin:8px 0 0 0;padding-left:20px'>{items_html}</ul>"
    else:
        assumptions_html = "<p style='color:#6b7280;font-style:italic'>本次计算所有数据均来自用户实测值，无假设数据。</p>"

    unknowns_html = ""
    if unknowns:
        unknowns_html = f"""
        <div style="background:#fef9c3;border:1px solid #fde047;border-radius:8px;padding:12px;margin-top:12px">
          <strong>⚠ 以下材料未能识别排放因子，已从计算中排除：</strong>
          {', '.join(unknowns)}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>产品碳足迹方法论报告 — {product}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif; color: #111827;
         background: #f9fafb; padding: 24px; font-size: 14px; }}
  .page {{ max-width: 800px; margin: 0 auto; background: #fff;
           border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.1); }}
  .cover {{ background: linear-gradient(135deg,#14532d,#16a34a); color:#fff; padding:40px; }}
  .cover h1 {{ font-size:26px; margin-bottom:8px; }}
  .cover .sub {{ opacity:.85; font-size:14px; }}
  .cover .big {{ font-size:42px; font-weight:700; margin:16px 0 4px; }}
  .cover .unit {{ opacity:.8; font-size:13px; }}
  .section {{ padding:28px 36px; border-bottom:1px solid #e5e7eb; }}
  .section:last-child {{ border-bottom:none; }}
  h2 {{ font-size:17px; color:#16a34a; margin-bottom:16px;
        padding-bottom:8px; border-bottom:2px solid #dcfce7; }}
  h3 {{ font-size:14px; color:#374151; margin:16px 0 8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ background:#f3f4f6; padding:8px 10px; text-align:left;
        font-weight:600; color:#374151; border-bottom:2px solid #e5e7eb; }}
  td {{ padding:8px 10px; border-bottom:1px solid #f3f4f6; vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:20px;
            font-size:12px; font-weight:600; }}
  .badge-green {{ background:#dcfce7; color:#14532d; }}
  .analogy {{ background:#f0fdf4; border-left:4px solid #16a34a; padding:12px 16px;
              border-radius:0 8px 8px 0; margin:16px 0; }}
  .print-btn {{ display:block; width:160px; margin:0 0 16px auto;
                background:#16a34a; color:#fff; border:none; border-radius:8px;
                padding:10px 0; text-align:center; cursor:pointer; font-size:14px;
                font-weight:600; }}
  .print-btn:hover {{ background:#14532d; }}
  .footer {{ background:#f9fafb; padding:20px 36px; font-size:12px; color:#6b7280; }}
  @media print {{
    body {{ background:#fff; padding:0; }}
    .page {{ box-shadow:none; border-radius:0; }}
    .print-btn {{ display:none; }}
  }}
</style>
</head>
<body>
<div class="page">

  <!-- Cover -->
  <div class="cover">
    <div style="display:flex;justify-content:space-between;align-items:flex-start">
      <div>
        <div class="sub">产品碳足迹方法论报告</div>
        <h1>🌿 {product}</h1>
        <div class="sub">计算基准：{fu}</div>
      </div>
      <div style="text-align:right">
        <div class="sub">报告日期</div>
        <div style="font-size:16px;font-weight:600;margin-top:4px">{today}</div>
      </div>
    </div>
    <div class="big">{total:.3f} <span style="font-size:20px">kg CO₂e</span></div>
    <div class="unit">每件产品全生命周期碳排放量</div>
  </div>

  <!-- Print button -->
  <div style="padding:16px 36px 0">
    <button class="print-btn" onclick="window.print()">🖨 打印 / 保存PDF</button>
  </div>

  <!-- Section 1: Results summary -->
  <div class="section">
    <h2>📊 碳足迹结果</h2>
    <div class="analogy">
      <strong>{product}</strong> 每件碳足迹为 <strong>{total:.3f} kg CO₂e</strong>，
      相当于驾驶普通乘用车行驶 <strong>{analogy} 公里</strong> 产生的碳排放。
    </div>
    {f'<p style="margin-bottom:12px">最大排放来源：<span class="badge badge-green">{hotspot}</span>，占总排放 <strong>{hotspot_pct}%</strong></p>' if hotspot else ''}
    <table>
      <thead><tr><th>排放来源</th><th style="text-align:right">排放量</th><th style="text-align:right">占比</th><th>可视化</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <!-- Section 2: Scope breakdown -->
  <div class="section">
    <h2>🔍 排放范围汇总</h2>
    <table>
      <thead><tr><th>排放范围</th><th style="text-align:right">数值（kg CO₂e）</th></tr></thead>
      <tbody>{scope_rows}</tbody>
    </table>
    <p style="margin-top:12px;color:#6b7280;font-size:12px">
      范围划分依据 GHG Protocol 产品标准 / ISO 14067 系统边界定义
    </p>
  </div>

  <!-- Section 3: EF sources -->
  <div class="section">
    <h2>📋 排放因子数据来源</h2>
    <table>
      <thead><tr><th>排放项目</th><th>排放因子</th><th>数据来源</th></tr></thead>
      <tbody>{ef_rows}</tbody>
    </table>
  </div>

  <!-- Section 4: Data quality -->
  <div class="section">
    <h2>✅ 数据质量说明</h2>
    <h3>计算假设与默认值</h3>
    {assumptions_html}
    {unknowns_html}
  </div>

  <!-- Section 5: Methodology -->
  <div class="section">
    <h2>📝 方法论声明</h2>
    <table>
      <tbody>
        <tr><td style="width:160px;color:#6b7280">系统边界</td><td>摇篮到坟墓（Cradle-to-Grave），含原材料、生产、运输、包装、报废处置</td></tr>
        <tr><td style="color:#6b7280">功能单位</td><td>{fu}</td></tr>
        <tr><td style="color:#6b7280">分配方法</td><td>质量分配（未明确说明时）</td></tr>
        <tr><td style="color:#6b7280">全球增温潜势</td><td>IPCC AR6 第六次评估报告 100年 GWP 值</td></tr>
        <tr><td style="color:#6b7280">温室气体覆盖</td><td>CO₂、CH₄、N₂O（其他 GHG 如有单独说明）</td></tr>
        <tr><td style="color:#6b7280">标准对齐</td><td>与 ISO 14067:2018、GHG Protocol 产品标准方法论兼容</td></tr>
        <tr><td style="color:#6b7280">数据截止年</td><td>2023年（电网因子）/ Ecoinvent v3.9（材料因子）</td></tr>
        <tr><td style="color:#6b7280">计算工具</td><td>双碳合规 AI 平台产品碳足迹计算引擎 v2.0</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Footer -->
  <div class="footer">
    <p>本报告由 <strong>双碳合规 AI 平台</strong> 自动生成。报告中的排放因子数据来自公开数据库（Ecoinvent、IPCC、中国国家标准），
    计算结果仅供参考。如需正式认证，请联系经认可的第三方核查机构（如 SGS、Bureau Veritas）。</p>
    <p style="margin-top:8px">生成时间：{today} &nbsp;|&nbsp; 方法论版本：Phase 2 MVP</p>
  </div>

</div>
</body>
</html>"""
