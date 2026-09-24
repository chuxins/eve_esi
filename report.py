"""生成 EVE 钱包 HTML 报告。

读取 MySQL 中的余额快照与钱包流水，生成一份自包含的 HTML 报告
（内嵌趋势图 + 汇总统计 + 流水明细表），无需外网即可查看。

用法：
    python report.py                          # 所有角色，生成 report.html
    python report.py --char chuxins1          # 指定角色
    python report.py --limit 500              # 每个角色取最近 500 条快照
    python report.py -o my_report.html        # 自定义输出文件名
"""

import argparse
import base64
import html
import io
import os
import sys
from datetime import datetime, timedelta, timezone

# 复用 charts.py 的公共辅助：MPLCONFIGDIR 指向可写目录（根文件系统可能只读）、
# 设置 Agg 后端与中文字体探测，避免本文件重复实现。
from charts import setup_chinese_font

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from esi_client import ESIClient, translate_description
from main import NoCharactersError, get_db, load_config, resolve_characters

OUTPUT_DEFAULT = "report.html"


def _fmt(value):
    """金额千分位格式化。"""
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def make_balance_chart(history, title):
    """根据余额快照生成 PNG 图表（深色主题，与报告页面协调），返回 base64 字符串。"""
    fig, ax = plt.subplots(figsize=(10, 4.2))
    fig.patch.set_alpha(0)
    ax.set_facecolor("#0d1728")
    times = [h["recorded_at"] for h in reversed(history)]
    values = [float(h["balance"]) for h in reversed(history)]
    ax.plot(times, values, marker="o", markersize=3.5, linewidth=1.8,
            color="#38bdf8", zorder=3)
    ax.fill_between(times, values, min(values), color="#38bdf8", alpha=0.12, zorder=1)
    ax.set_title(title, color="#e8eef8", fontsize=12, pad=12)
    for spine in ax.spines.values():
        spine.set_color("#223351")
    ax.tick_params(colors="#93a7c6", labelsize=9)
    ax.grid(True, color="#223351", alpha=0.6, linewidth=0.8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=30)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, p: f"{v:,.0f}")
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0b1220")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_report(config, db, char_arg, limit):
    try:
        chars = resolve_characters(db, char_arg)
    except NoCharactersError:
        # 无角色时生成空报告：auto_query 定时任务依赖 build_report 不抛异常，
        # 若在此 sys.exit/抛错会让常驻进程退出。
        chars = []
    has_cjk = setup_chinese_font()

    cards_html = ""
    charts_html = ""
    summary_rows = ""
    journal_html = ""

    for c in chars:
        cid = c["character_id"]
        name = html.escape(c["character_name"])

        history = db.get_balance_history(cid, limit=limit)
        journal = db.get_journal(cid, limit=50)

        # 当前余额 / 变动
        current = float(history[0]["balance"]) if history else 0.0
        change = 0.0
        if len(history) >= 2:
            change = current - float(history[-1]["balance"])

        # 流水汇总
        summary = ESIClient.summarize(journal)

        trend_title = "余额变动趋势" if has_cjk else "Balance Trend"
        if history:
            img = make_balance_chart(history, f"{c['character_name']} - {trend_title}")
            charts_html += f"""
        <section class="panel chart">
            <h2>{name} · {trend_title}</h2>
            <img class="chart-img" src="data:image/png;base64,{img}" alt="balance chart">
            <div class="chart-stats">
                <div class="stat"><div class="k">区间收入</div><div class="v pos num">{_fmt(summary['total_income'])}</div></div>
                <div class="stat"><div class="k">区间支出</div><div class="v neg num">{_fmt(summary['total_expense'])}</div></div>
                <div class="stat"><div class="k">净变动</div><div class="v {'pos' if summary['net'] >= 0 else 'neg'} num">{_fmt(summary['net'])}</div></div>
            </div>
        </section>"""
        else:
            charts_html += f"""
        <section class="panel chart">
            <h2>{name}</h2>
            <p class="empty">暂无余额历史数据，请先运行 auto_query.py 或 main.py 采集。</p>
        </section>"""

        # 角色卡片
        trend_cls = "up" if change > 0 else ("down" if change < 0 else "flat")
        trend_icon = "▲" if change > 0 else ("▼" if change < 0 else "•")
        cards_html += f"""
        <div class="card">
            <div class="card-head">
                <div>
                    <div class="card-name">{name}</div>
                    <div class="card-id">ID: {c['character_id']}</div>
                </div>
                <div class="card-trend {trend_cls}">{trend_icon}</div>
            </div>
            <div class="card-balance num">{_fmt(current)}<span class="isk">ISK</span></div>
            <div class="card-change {trend_cls}">区间变动 {change:+,.0f} ISK</div>
            <div class="card-stats">
                <div class="stat"><div class="k">收入</div><div class="v pos num">{_fmt(summary['total_income'])}</div></div>
                <div class="stat"><div class="k">支出</div><div class="v neg num">{_fmt(summary['total_expense'])}</div></div>
                <div class="stat"><div class="k">净变动</div><div class="v {'pos' if summary['net'] >= 0 else 'neg'} num">{_fmt(summary['net'])}</div></div>
                <div class="stat"><div class="k">税费</div><div class="v num">{_fmt(summary['tax_total'])}</div></div>
            </div>
            <div class="card-meta">快照 {len(history)} 条 · 流水 {summary['count']} 条</div>
        </div>"""

        # 汇总行
        summary_rows += f"""
        <tr>
            <td><span class="td-name">{name}</span></td>
            <td class="r num">{_fmt(current)}</td>
            <td class="r num pos">{_fmt(summary['total_income'])}</td>
            <td class="r num neg">{_fmt(summary['total_expense'])}</td>
            <td class="r num">{_fmt(summary['tax_total'])}</td>
            <td class="r num {'pos' if summary['net'] >= 0 else 'neg'}">{_fmt(summary['net'])}</td>
            <td class="r num">{summary['count']}</td>
        </tr>"""

        # 流水明细
        rows = ""
        for e in journal[:30]:
            d = e.get("journal_date") or e.get("date")
            d = str(d)[:19].replace("T", " ")
            amount = float(e.get("amount", 0.0) or 0.0)
            cls = "pos" if amount >= 0 else "neg"
            desc = html.escape(translate_description(e.get("description")))
            rows += (
                f"<tr><td>{d}</td><td class='r num {cls}'>{amount:+,.2f}</td>"
                f"<td class='r num'>{_fmt(e.get('balance'))}</td><td>{desc}</td></tr>"
            )
        journal_html += f"""
        <section class="panel journal">
            <h2>{name} · 最近流水<span class="sub">最近 {len(journal[:30])} 条</span></h2>
            <div class="table-wrap">
            <table>
                <thead><tr>
                    <th>时间</th><th class="r">变动 (ISK)</th><th class="r">变动后余额</th><th>描述</th>
                </tr></thead>
                <tbody>{rows}</tbody>
            </table>
            </div>
        </section>"""

    generated_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    title = "EVE 钱包余额报告"
    page_title = title if has_cjk else "EVE Wallet Report"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
:root {{
  --bg-1: #0a1120; --bg-2: #0f1b31;
  --panel: #111c30; --panel-2: #16233c;
  --border: #223351; --border-soft: #1a2a44;
  --text: #e8eef8; --muted: #93a7c6; --faint: #64789a;
  --accent: #38bdf8;
  --green: #34d399; --red: #fb7185;
  --radius: 16px;
  --shadow: 0 10px 30px rgba(2, 8, 23, .45);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  color: var(--text);
  background:
    radial-gradient(1100px 560px at 85% -10%, #16294d 0%, transparent 60%),
    radial-gradient(900px 500px at -10% 110%, #12203f 0%, transparent 55%),
    linear-gradient(180deg, var(--bg-1), var(--bg-2));
  background-attachment: fixed;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}}
.num {{ font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; }}
.container {{ max-width: 1160px; margin: 0 auto; padding: 28px 20px 48px; }}
/* ---------- 头部 ---------- */
.hero {{ padding: 14px 0 24px; }}
.hero h1 {{ font-size: 28px; font-weight: 800; letter-spacing: .5px;
  background: linear-gradient(90deg, #eef4ff, #7dd3fc);
  -webkit-background-clip: text; background-clip: text; color: transparent; }}
.hero .badges {{ margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
.badge {{ font-size: 12px; padding: 5px 11px; border-radius: 999px;
  background: rgba(56, 189, 248, .1); border: 1px solid rgba(56, 189, 248, .25);
  color: #9bd8fd; }}
.badge.muted {{ background: rgba(147, 167, 198, .08); border-color: var(--border);
  color: var(--muted); }}
/* ---------- 面板 ---------- */
.panel {{ background: linear-gradient(180deg, var(--panel-2), var(--panel));
  border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); padding: 20px; }}
.panel + .panel {{ margin-top: 20px; }}
.panel h2 {{ font-size: 17px; font-weight: 700; margin-bottom: 14px; }}
.panel h2 .sub {{ font-size: 12px; color: var(--muted); font-weight: 500; margin-left: 8px; }}
/* ---------- 角色卡片 ---------- */
.cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(252px, 1fr));
  gap: 14px; margin-bottom: 20px; }}
.card {{ background: linear-gradient(180deg, var(--panel-2), var(--panel));
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px; box-shadow: var(--shadow);
  transition: transform .15s ease, border-color .15s ease; }}
.card:hover {{ transform: translateY(-2px); border-color: #2e4673; }}
.card-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }}
.card-name {{ font-size: 15px; font-weight: 700; }}
.card-id {{ font-size: 11px; color: var(--faint); margin-top: 2px; }}
.card-trend {{ width: 34px; height: 34px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; font-size: 14px;
  background: rgba(147, 167, 198, .08); border: 1px solid var(--border); }}
.card-trend.up {{ color: var(--green); border-color: rgba(52, 211, 153, .35);
  background: rgba(52, 211, 153, .08); }}
.card-trend.down {{ color: var(--red); border-color: rgba(251, 113, 133, .35);
  background: rgba(251, 113, 133, .08); }}
.card-trend.flat {{ color: var(--muted); }}
.card-balance {{ font-size: 25px; font-weight: 800; color: var(--accent);
  margin: 14px 0 2px; }}
.card-balance .isk {{ font-size: 12px; color: var(--muted); font-weight: 500; margin-left: 4px; }}
.card-change {{ font-size: 13px; margin-bottom: 12px; }}
.card-change.up {{ color: var(--green); }} .card-change.down {{ color: var(--red); }}
.card-change.flat {{ color: var(--muted); }}
.card-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.stat {{ background: rgba(255, 255, 255, .03); border: 1px solid var(--border-soft);
  border-radius: 10px; padding: 8px 10px; }}
.stat .k {{ font-size: 11px; color: var(--muted); }}
.stat .v {{ font-size: 13px; font-weight: 600; margin-top: 2px; }}
.stat .v.pos {{ color: var(--green); }} .stat .v.neg {{ color: var(--red); }}
.card-meta {{ margin-top: 10px; font-size: 12px; color: var(--faint); }}
/* ---------- 表格 ---------- */
.table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
thead th {{ text-align: left; padding: 10px 12px; color: var(--muted); font-weight: 600;
  border-bottom: 1px solid var(--border); background: rgba(255, 255, 255, .025);
  position: sticky; top: 0; }}
tbody td {{ padding: 9px 12px; border-bottom: 1px solid var(--border-soft);
  vertical-align: top; }}
tbody tr:hover {{ background: rgba(56, 189, 248, .05); }}
.td-name {{ font-weight: 600; }}
td.r, th.r {{ text-align: right; }}
td.pos {{ color: var(--green); }} td.neg {{ color: var(--red); }}
/* ---------- 图表 ---------- */
.chart-img {{ width: 100%; height: auto; border-radius: 10px;
  border: 1px solid var(--border-soft); }}
.chart-stats {{ margin-top: 14px; display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }}
/* ---------- 其它 ---------- */
.empty {{ color: var(--muted); padding: 18px 0; text-align: center;
  border: 1px dashed var(--border); border-radius: 10px; }}
footer {{ margin-top: 28px; text-align: center; color: var(--faint); font-size: 12px; }}
@media (max-width: 640px) {{
  .container {{ padding: 16px 12px 32px; }}
  .hero h1 {{ font-size: 22px; }}
  .card-balance {{ font-size: 21px; }}
  .panel {{ padding: 16px; }}
}}
</style>
</head>
<body>
<div class="container">
  <header class="hero">
    <h1>🪙 {title}</h1>
    <div class="badges">
      <span class="badge">生成时间 {generated_at}</span>
      <span class="badge muted">角色 {len(chars)} 个</span>
      <span class="badge muted">数据来源 EVE ESI / MySQL</span>
    </div>
  </header>
  <section class="cards">{cards_html}</section>
  <section class="panel summary">
    <h2>收支汇总<span class="sub">基于最近流水统计</span></h2>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>角色</th><th class="r">当前余额</th><th class="r">总收入</th>
          <th class="r">总支出</th><th class="r">税费</th><th class="r">净变动</th><th class="r">流水条数</th>
        </tr></thead>
        <tbody>{summary_rows}</tbody>
      </table>
    </div>
  </section>
  {charts_html}
  {journal_html}
  <footer>EVE 钱包报告 · 由 auto_query.py 自动生成 · 数据来源 EVE ESI</footer>
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="生成 EVE 钱包 HTML 报告")
    parser.add_argument("--char", metavar="ID或名称", help="指定角色（默认所有角色）")
    parser.add_argument("--limit", type=int, default=200, help="每个角色取最近 N 条快照（默认 200）")
    parser.add_argument("-o", "--output", default=OUTPUT_DEFAULT, help=f"输出文件名（默认 {OUTPUT_DEFAULT}）")
    args = parser.parse_args()

    config = load_config()
    db = get_db(config)
    content = build_report(config, db, args.char, args.limit)

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ HTML 报告已生成: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
