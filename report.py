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
    """根据余额快照生成 PNG 图表，返回 base64 字符串。"""
    fig, ax = plt.subplots(figsize=(10, 4.5))
    times = [h["recorded_at"] for h in reversed(history)]
    values = [float(h["balance"]) for h in reversed(history)]
    ax.plot(times, values, marker="o", markersize=4, linewidth=1.6, color="#3b82f6")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=30)
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, p: f"{v:,.0f}")
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
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
            charts_html += (
                f'<div class="chart"><h2>{name} · {trend_title}</h2>'
                f'<img src="data:image/png;base64,{img}" alt="balance chart"></div>'
            )
        else:
            charts_html += (
                f'<div class="chart"><h2>{name}</h2>'
                '<p class="empty">暂无余额历史数据，请先运行 auto_query.py 或 main.py 采集。</p></div>'
            )

        # 角色卡片
        trend_cls = "up" if change > 0 else ("down" if change < 0 else "flat")
        cards_html += f"""
        <div class="card">
            <div class="card-name">{name}</div>
            <div class="card-id">ID: {c['character_id']}</div>
            <div class="card-balance">{_fmt(current)} <span class="isk">ISK</span></div>
            <div class="card-change {trend_cls}">区间变动 {change:+,.0f} ISK</div>
            <div class="card-meta">快照 {len(history)} 条 · 流水 {summary['count']} 条</div>
        </div>"""

        # 汇总行
        summary_rows += f"""
        <tr>
            <td>{name}</td>
            <td>{_fmt(current)}</td>
            <td class="pos">{_fmt(summary['total_income'])}</td>
            <td class="neg">{_fmt(summary['total_expense'])}</td>
            <td>{_fmt(summary['tax_total'])}</td>
            <td class="{'pos' if summary['net'] >= 0 else 'neg'}">{_fmt(summary['net'])}</td>
            <td>{summary['count']}</td>
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
                f"<tr><td>{d}</td><td class='{cls}'>{amount:+,.2f}</td>"
                f"<td>{_fmt(e.get('balance'))}</td><td>{desc}</td></tr>"
            )
        journal_html += f"""
        <div class="journal">
            <h2>{name} · 最近流水</h2>
            <table>
                <thead><tr><th>时间</th><th>变动 (ISK)</th><th>变动后余额</th><th>描述</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""

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
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 24px; }}
.header {{ max-width: 1080px; margin: 0 auto 20px; }}
.header h1 {{ font-size: 26px; color: #f8fafc; }}
.header .meta {{ color: #94a3b8; font-size: 13px; margin-top: 6px; }}
.cards {{ max-width: 1080px; margin: 0 auto 24px; display: grid;
         grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 16px; }}
.card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px;
        padding: 18px; }}
.card-name {{ font-size: 16px; font-weight: 600; color: #f1f5f9; }}
.card-id {{ color: #94a3b8; font-size: 12px; margin: 2px 0 10px; }}
.card-balance {{ font-size: 22px; font-weight: 700; color: #38bdf8; }}
.isk {{ font-size: 12px; color: #94a3b8; }}
.card-change {{ font-size: 13px; margin-top: 8px; }}
.card-change.up {{ color: #4ade80; }} .card-change.down {{ color: #f87171; }}
.card-change.flat {{ color: #94a3b8; }}
.card-meta {{ color: #64748b; font-size: 12px; margin-top: 4px; }}
.chart, .journal, .summary {{ max-width: 1080px; margin: 0 auto 24px;
    background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; }}
.chart h2, .journal h2, .summary h2 {{ font-size: 18px; margin-bottom: 14px; color: #f1f5f9; }}
.chart img {{ width: 100%; border-radius: 8px; }}
.empty {{ color: #94a3b8; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #334155; }}
th {{ color: #94a3b8; font-weight: 600; }}
td.pos {{ color: #4ade80; }} td.neg {{ color: #f87171; }}
.journal table {{ max-height: 400px; display: block; overflow-y: auto; }}
.journal thead {{ position: sticky; top: 0; background: #1e293b; }}
</style>
</head>
<body>
<div class="header">
    <h1>{title}</h1>
    <div class="meta">生成时间：{generated_at} · 数据来源：EVE ESI / MySQL</div>
</div>
<div class="cards">{cards_html}</div>
<div class="summary">
    <h2>收支汇总</h2>
    <table>
        <thead><tr>
            <th>角色</th><th>当前余额</th><th>总收入</th><th>总支出</th>
            <th>税费</th><th>净变动</th><th>流水条数</th>
        </tr></thead>
        <tbody>{summary_rows}</tbody>
    </table>
</div>
{charts_html}
{journal_html}
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
