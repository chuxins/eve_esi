"""图表化展示钱包余额变动历史。

读取 MySQL wallet_balance 表中的余额快照，绘制折线图并保存为 PNG。

用法：
    python plot_balance.py                     # 所有角色，最近 200 条
    python plot_balance.py --char chuxins1     # 指定角色
    python plot_balance.py --limit 500         # 最近 500 条
    python plot_balance.py -o balance.png      # 自定义输出文件
"""

import argparse
import os
import sys

import matplotlib

matplotlib.use("Agg")  # 无显示环境（服务器）使用非交互后端
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager

from main import get_db, load_config, resolve_characters


class NoBalanceDataError(Exception):
    """没有任何可绘制的余额历史数据时抛出（供调用方区分"无数据"与一般错误）。"""


def _setup_chinese_font():
    """尝试设置中文字体，找不到则回退默认（英文标签）。"""
    candidates = [
        "Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
        "SimHei", "Microsoft YaHei", "PingFang SC",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return True
    return False


def fmt_isk(value):
    return f"{value:,.0f}"


def plot_balance(config, char_arg=None, limit=200, output="balance_history.png",
                 current_balance=None, balance_source="ESI"):
    db = get_db(config)
    chars = resolve_characters(db, char_arg)

    has_chinese = _setup_chinese_font()
    fig, ax = plt.subplots(figsize=(12, 6))

    plotted = False
    for c in chars:
        history = db.get_balance_history(c["character_id"], limit=limit)
        if not history:
            print(f"角色 {c['character_name']} 暂无余额历史数据。")
            continue
        # 按时间正序绘制
        times = [h["recorded_at"] for h in reversed(history)]
        values = [float(h["balance"]) for h in reversed(history)]
        ax.plot(times, values, marker="o", markersize=4, linewidth=1.5,
                label=f"{c['character_name']} (ID:{c['character_id']})")
        plotted = True

        if len(history) >= 2:
            first = float(history[-1]["balance"])
            last = float(history[0]["balance"])
            change = last - first
            print(f"角色 {c['character_name']}: 首 {fmt_isk(first)} ISK "
                  f"→ 末 {fmt_isk(last)} ISK，变动 {change:+,.0f} ISK")

    if not plotted:
        raise NoBalanceDataError(
            "没有任何可绘制的余额历史数据，请先运行 auto_query.py 或 main.py 采集数据。"
        )

    # 图表样式
    if has_chinese:
        title = "钱包余额变动历史"
        if current_balance is not None:
            title += f"\n当前余额：{fmt_isk(current_balance)} ISK（{balance_source}）"
        ax.set_title(title)
        ax.set_xlabel("时间")
        ax.set_ylabel("余额 (ISK)")
    else:
        title = "Wallet Balance History"
        if current_balance is not None:
            title += f"\nCurrent Balance: {fmt_isk(current_balance)} ISK ({balance_source})"
        ax.set_title(title)
        ax.set_xlabel("Time")
        ax.set_ylabel("Balance (ISK)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=45)

    # 千分位格式化 Y 轴
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, p: f"{v:,.0f}")
    )

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), output)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"✅ 图表已保存: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="图表化展示钱包余额变动历史")
    parser.add_argument("--char", metavar="ID或名称", help="指定角色（默认所有角色）")
    parser.add_argument("--limit", type=int, default=200, help="每个角色取最近 N 条快照（默认 200）")
    parser.add_argument("-o", "--output", default="balance_history.png", help="输出文件名（默认 balance_history.png）")
    args = parser.parse_args()

    config = load_config()
    plot_balance(config, char_arg=args.char, limit=args.limit, output=args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
