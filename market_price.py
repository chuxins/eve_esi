"""Jita 市场行情：收购价 / 出售价 / 中间价 + 近期价格走势图。

数据来源：EVE ESI 公开端点（无需授权）
- 挂单行情: GET  /v1/markets/{region_id}/orders/?type_id=...
- 历史日线: GET  /v1/markets/{region_id}/history/?type_id=...
- 名称解析: POST /v1/universe/ids/

价格口径：
- 优先取 Jita 4-4 空间站（60003760）的挂单
  收购价 = 最高买单价，出售价 = 最低卖单价，中间价 = 两者均值
- 该站无挂单时回退到 The Forge 星域（结果中会标注实际范围）
- 走势图使用 The Forge 星域日线（ESI 仅提供星域粒度）

用法：
    python3 market_price.py 三钛合金              # 查询并生成走势图
    python3 market_price.py 三钛合金*1000         # 同时给出总价
    python3 market_price.py Tritanium --days 60 -o price.png
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from charts import setup_chinese_font
import matplotlib.dates as mdates
import matplotlib.ticker
import matplotlib.pyplot as plt

from esi_client import ESIClient, ESIError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FORGE_REGION_ID = 10000002   # The Forge 星域（Jita 所在）
JITA_STATION_ID = 60003760   # Jita IV-4 Caldari Navy Assembly Plant
DEFAULT_UA = "eve-wallet-tracker/1.0"
DEFAULT_DAYS = 30
MAX_QUANTITY = 10 ** 12      # 数量上限，防止误输入产生超长数字
MAX_BATCH_ITEMS = 30         # 「批量查价」单次最多物品数
BATCH_WORKERS = 5            # 批量查询并发数


# ---------------------------------------------------------------- 查询串解析

# 用户常打成「批量查价：xxx」「- xxx」，名字两侧需要清理的分隔符/空白
_NAME_EDGE_CHARS = "：:，,、;；。 \t\u3000"


def _clean_item_name(name):
    """清理物品名两侧的空白与常见分隔符（如「：三钛合金」「三钛合金、」）。"""
    return str(name or "").strip(_NAME_EDGE_CHARS)


def parse_item_query(text):
    """解析「物品名称[*数量]」，返回 (物品名, 数量或 None)。

    - 数量为正整数，允许千分位逗号/下划线（如 1,000），全角 ＊ 等价 *
    - ``*`` 后为空（如「三钛合金*」）时数量按 1 计算
    - 名称两侧的空白与分隔符（：: ，,、等）会被剔除
    - 数量格式非法或缺少名称时抛 ValueError
    """
    text = str(text or "").replace("＊", "*")

    if "*" not in text:
        name = _clean_item_name(text)
        if not name:
            raise ValueError("缺少物品名称")
        return name, None

    name, _, qty_text = text.rpartition("*")
    name = _clean_item_name(name)
    if not name:
        raise ValueError("缺少物品名称")

    qty_text = qty_text.strip().replace(",", "").replace("_", "")
    if not qty_text:
        return name, 1  # 「物品*」→ 数量 1
    if not qty_text.isdigit():
        raise ValueError("数量需为正整数")
    quantity = int(qty_text)
    if not 1 <= quantity <= MAX_QUANTITY:
        raise ValueError(f"数量需在 1 ~ {MAX_QUANTITY:,} 之间")
    return name, quantity


def parse_batch_query(text):
    """解析批量查价文本（每行一个「物品名称[*数量]」）。

    返回 (items, errors)：
    - items  [(名称, 数量或 None), ...]
    - errors [(原始行, 错误原因), ...]（格式不合法的行）
    """
    items, errors = [], []
    for raw_line in str(text or "").splitlines():
        line = _clean_item_name(raw_line)
        if not line:
            continue
        try:
            name, quantity = parse_item_query(line)
        except ValueError as exc:
            errors.append((line, str(exc)))
            continue
        if name:
            items.append((name, quantity))
    return items, errors


# ---------------------------------------------------------------- 名称解析

def _resolve_exact(client, name, db=None):
    """精确解析：本地索引（中文名/英文名）→ ESI 中文 → ESI 英文。

    返回 (type_id, 官方名称)；查不到返回 (None, None)。
    """
    if db is not None:
        try:
            row = db.find_item_type_by_name(name)
            if row:
                return int(row["type_id"]), row["name"]
        except Exception as exc:  # 本地索引查询失败不影响主流程
            print(f"  本地物品表查询失败: {exc}")

    for language in ("zh", "en"):
        try:
            data = client.resolve_names([name], language=language)
        except ESIError as exc:
            print(f"  ESI 名称解析失败（{language}）: {exc}")
            continue
        hits = (data or {}).get("inventory_types") or []
        if hits:
            return int(hits[0]["id"]), hits[0].get("name") or name
    return None, None


def resolve_item(client, name, db=None, fuzzy_limit=10):
    """解析物品名称：先精确匹配，失败后回退本地索引模糊匹配。

    返回 None 表示查不到；否则返回：
    {"type_id", "name", "fuzzy": bool, "query", "candidates"}
    """
    name = (name or "").strip()
    if not name:
        return None

    type_id, official = _resolve_exact(client, name, db)
    if type_id:
        return {"type_id": type_id, "name": official or name,
                "fuzzy": False, "query": name, "candidates": 1}

    if db is not None:
        try:
            hits = db.search_item_types(name, limit=fuzzy_limit)
        except Exception as exc:
            print(f"  模糊匹配失败: {exc}")
            hits = []
        if hits:
            best = hits[0]
            return {"type_id": int(best["type_id"]), "name": best["name"],
                    "fuzzy": True, "query": name, "candidates": len(hits)}
    return None


# ---------------------------------------------------------------- 行情数据

def _calc_prices(orders):
    """从挂单列表计算收购价 / 出售价 / 中间价（优先 Jita 4-4，其次星域）。"""
    buys = [o for o in orders if o.get("is_buy_order")]
    sells = [o for o in orders if not o.get("is_buy_order")]

    candidates = (
        # (买单一览, 卖单一览, 范围说明)
        ([o for o in buys if o.get("location_id") == JITA_STATION_ID],
         [o for o in sells if o.get("location_id") == JITA_STATION_ID],
         "Jita 4-4"),
        (buys, sells, "The Forge 星域"),
    )
    for scope_buys, scope_sells, scope in candidates:
        buy = max((float(o["price"]) for o in scope_buys), default=None)
        sell = min((float(o["price"]) for o in scope_sells), default=None)
        if buy is not None or sell is not None:
            mid = (buy + sell) / 2 if (buy is not None and sell is not None) else None
            return {
                "buy": buy,
                "sell": sell,
                "mid": mid,
                "scope": scope,
                "buy_count": len(scope_buys),
                "sell_count": len(scope_sells),
            }
    return {
        "buy": None, "sell": None, "mid": None,
        "scope": "-", "buy_count": 0, "sell_count": 0,
    }


def fetch_history(client, type_id, days=DEFAULT_DAYS):
    """获取最近 days 天的日线（按日期升序）。days<=0 表示全部。"""
    rows = client.get_region_history(FORGE_REGION_ID, type_id) or []
    rows = sorted(rows, key=lambda r: str(r.get("date") or ""))
    if days and days > 0:
        rows = rows[-days:]
    return rows


# /v1/markets/prices/ 一次返回全量（约 1.6 万条、1MB、单次 5~25 秒），
# 因此做磁盘缓存 + 内存缓存，只在后台/首次调用时刷新。
PRICE_CACHE_PATH = os.path.join(BASE_DIR, "market_prices.json")
PRICE_CACHE_TTL = 24 * 3600  # 秒；ESI 该端点每日更新
_price_mem = {"ts": 0.0, "data": {}}


def _load_price_table():
    """读取磁盘缓存的全局参考价（进程内只读一次），返回 {type_id: average_price}。"""
    if _price_mem["data"]:
        return _price_mem["data"]
    try:
        with open(PRICE_CACHE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _price_mem["data"] = {int(k): float(v) for k, v in (payload.get("prices") or {}).items()}
        _price_mem["ts"] = float(payload.get("ts") or 0)
    except (OSError, ValueError):
        pass
    return _price_mem["data"]


def price_cache_age():
    """返回参考价缓存年龄（秒）；无缓存返回 None。"""
    _load_price_table()
    return time.time() - _price_mem["ts"] if _price_mem["ts"] else None


def refresh_price_cache(client=None, user_agent=DEFAULT_UA, quiet=False):
    """下载 ESI 全局参考价并写入缓存（磁盘 + 内存），返回缓存条目数。"""
    if client is None:
        client = ESIClient(None, user_agent)
    rows = client.get_market_prices()
    data = {}
    for r in rows:
        try:
            avg = float(r.get("average_price") or 0)
            if avg > 0:
                data[int(r["type_id"])] = avg
        except (KeyError, TypeError, ValueError):
            continue
    payload = {"ts": time.time(), "prices": {str(k): v for k, v in data.items()}}
    tmp = PRICE_CACHE_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, PRICE_CACHE_PATH)
    _price_mem.update(ts=payload["ts"], data=data)
    if not quiet:
        print(f"✅ 全局参考价已缓存：{len(data)} 条 -> {PRICE_CACHE_PATH}")
    return len(data)


def fetch_average_price(client, type_id):
    """取全局参考均价（优先缓存）。无缓存时同步刷新一次（较慢）。

    仅用作「无星域挂单也无历史」时的兜底，例如 PLEX（伊甸币）。
    """
    table = _load_price_table()
    if not table:
        print("  首次获取 ESI 全局参考价（约 5~25 秒）…")
        try:
            refresh_price_cache(client, quiet=True)
        except Exception as exc:
            print(f"  全局参考价获取失败: {exc}")
            return None
        table = _price_mem["data"]
    avg = table.get(int(type_id))
    return avg if avg else None


# ---------------------------------------------------------------- 走势图

def _price_formatter(value, _pos=None):
    """价格刻度：根数值大小自适应小数位（避免 3.9 被四舍五入成 4）。"""
    abs_v = abs(value)
    if abs_v >= 1000:
        return f"{value:,.0f}"
    if abs_v >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _volume_formatter(has_cjk):
    """成交量刻度：中文用万/亿，否则用 K/M/B。"""
    def fmt(value, _pos=None):
        if has_cjk:
            if abs(value) >= 1e8:
                return f"{value / 1e8:,.1f}亿"
            if abs(value) >= 1e4:
                return f"{value / 1e4:,.1f}万"
            return f"{value:,.0f}"
        if abs(value) >= 1e9:
            return f"{value / 1e9:,.1f}B"
        if abs(value) >= 1e6:
            return f"{value / 1e6:,.1f}M"
        if abs(value) >= 1e3:
            return f"{value / 1e3:,.1f}K"
        return f"{value:,.0f}"
    return fmt


def make_price_chart(name, history, output_path):
    """绘制价格走势图（日均价折线 + 当日最高/最低区间 + 成交量柱），返回文件路径。"""
    has_cjk = setup_chinese_font()
    dates = [datetime.strptime(str(r["date"]), "%Y-%m-%d") for r in history]
    avg = [float(r.get("average") or 0) for r in history]
    high = [float(r.get("highest") or 0) for r in history]
    low = [float(r.get("lowest") or 0) for r in history]
    volume = [float(r.get("volume") or 0) for r in history]

    label_range = "当日最高/最低" if has_cjk else "Daily high/low"
    label_avg = "日均价" if has_cjk else "Daily average"
    label_vol = "成交量" if has_cjk else "Volume"
    title = (
        f"{name} · 近 {len(history)} 天价格走势（Jita 星域）"
        if has_cjk
        else f"{name} · Price trend ({len(history)} days, The Forge)"
    )

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(dates, low, high, color="#3b82f6", alpha=0.15, label=label_range)
    ax.plot(dates, avg, marker="o", markersize=3, linewidth=1.8,
            color="#3b82f6", label=label_avg)
    ax.set_title(title)
    ax.set_ylabel("价格 (ISK)" if has_cjk else "Price (ISK)")
    ax.grid(True, alpha=0.3)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_price_formatter))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    # 成交量走次坐标轴
    ax2 = ax.twinx()
    ax2.bar(dates, volume, width=0.8, color="#64748b", alpha=0.22, label=label_vol)
    ax2.set_ylabel(label_vol)
    ax2.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(_volume_formatter(has_cjk))
    )

    handles1, labels1 = ax.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(handles1 + handles2, labels1 + labels2, loc="upper left", fontsize=9)

    fig.autofmt_xdate(rotation=30)
    fig.savefig(output_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return output_path


# ---------------------------------------------------------------- 对外接口

def query_item(name, db=None, output_path=None, user_agent=DEFAULT_UA,
               days=DEFAULT_DAYS, quantity=None, with_history=True):
    """查询物品行情（quantity 非空时消息中会附上总价）。

    with_history=False 时跳过分时历史请求（批量查价只需挂单价，可省一半请求）。

    返回字典：
    - 成功: {"ok": True, "name", "type_id", "prices", "history", "chart", "quantity"}
    - 失败: {"ok": False, "reason": "not_found"|"no_data", "query"/"name"}
    """
    client = ESIClient(None, user_agent)  # 市场端点为公开端点，无需 token
    hit = resolve_item(client, name, db)
    if not hit:
        return {"ok": False, "reason": "not_found", "query": name}

    type_id = hit["type_id"]
    display_name = hit["name"]
    orders = client.get_region_orders(FORGE_REGION_ID, type_id)
    prices = _calc_prices(orders)
    history = fetch_history(client, type_id, days) if with_history else []

    has_price = prices["buy"] is not None or prices["sell"] is not None
    if not has_price and not history:
        # 无挂单也无历史（如 PLEX）：退回 ESI 全局参考均价
        avg = fetch_average_price(client, type_id)
        if avg is None:
            return {"ok": False, "reason": "no_data",
                    "name": display_name, "type_id": type_id}
        prices = {"buy": None, "sell": None, "mid": None, "average": avg,
                  "scope": "ESI 全局均价", "buy_count": 0, "sell_count": 0}

    chart = None
    if history and output_path:
        chart = make_price_chart(display_name, history, output_path)

    return {
        "ok": True,
        "name": display_name,
        "type_id": type_id,
        "prices": prices,
        "history": history,
        "chart": chart,
        "quantity": quantity,
        "fuzzy": hit["fuzzy"],
        "query": hit["query"],
        "candidates": hit["candidates"],
    }


def format_price_message(result):
    """把查询结果格式化为推送文本（带数量时给出单价×数量=总价）。"""
    prices = result["prices"]
    history = result["history"]
    quantity = result.get("quantity")

    def price_line(icon, label, unit_price):
        if unit_price is None:
            return f"{icon} {label}：-"
        if quantity:
            return (f"{icon} {label}：{unit_price:,.2f} × {quantity:,}"
                    f" = {unit_price * quantity:,.2f} ISK")
        return f"{icon} {label}：{unit_price:,.2f} ISK"

    title = f"💰 {result['name']}"
    if quantity:
        title += f" × {quantity:,}"

    lines = [title]
    if result.get("fuzzy"):
        lines.append(
            f"🔍 由「{result['query']}」模糊匹配（相近物品 {result['candidates']} 个）"
        )
    lines.append(
        f"🆔 type_id {result['type_id']} ｜ 范围：{prices['scope']}"
        + ("" if prices.get("average") else
           f"（买 {prices['buy_count']} / 卖 {prices['sell_count']} 单）")
    )
    lines.append("────────────────")

    if prices.get("average"):
        # 无星域挂单/历史的物品（如 PLEX）：只提供 ESI 全局均价
        avg = prices["average"]
        if quantity:
            lines.append(f"📊 参考均价：{avg:,.2f} × {quantity:,}"
                         f" = {avg * quantity:,.2f} ISK")
        else:
            lines.append(f"📊 参考均价：{avg:,.2f} ISK")
        lines.append("⚠️ 该物品无星域挂单与历史行情（如 PLEX），"
                     "以上为 ESI 每日更新的全局均价")
        return "\n".join(lines)

    lines += [
        price_line("🟢", "收购价", prices["buy"]),
        price_line("🔴", "出售价", prices["sell"]),
        price_line("⚖️", "中间价", prices["mid"]),
    ]
    if history:
        avgs = [float(r.get("average") or 0) for r in history]
        highs = [float(r.get("highest") or 0) for r in history]
        lows = [float(r.get("lowest") or 0) for r in history]

        def isk(value):
            return f"{value:,.2f} ISK"

        lines.append("────────────────")
        lines.append(
            f"📈 近 {len(history)} 天：均价 {isk(sum(avgs) / len(avgs))}"
            f" ｜ 最高 {isk(max(highs))} ｜ 最低 {isk(min(lows))}"
        )
    return "\n".join(lines)


def query_batch(items, db=None, user_agent=DEFAULT_UA, workers=BATCH_WORKERS):
    """并发查询多个物品（不取历史也不出图），返回顺序与 items 一致的结果列表。"""
    items = list(items)
    if not items:
        return []

    def work(item):
        name, quantity = item
        try:
            return query_item(name, db=db, output_path=None, user_agent=user_agent,
                              quantity=quantity, with_history=False)
        except Exception as exc:  # 单项失败不影响其它物品
            return {"ok": False, "reason": "error", "query": name, "error": str(exc)}

    pool_size = max(1, min(workers, len(items)))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        return list(pool.map(work, items))


def format_batch_message(results, errors=None):
    """把批量结果格式化为文本：逐项小计 + 总收购/总出售/总中间价。"""
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]

    totals = {"buy": 0.0, "sell": 0.0, "mid": 0.0}
    counted = {"buy": 0, "sell": 0, "mid": 0}
    estimated = 0
    fuzzy = 0
    lines = [f"🧮 批量查价（{len(results)} 项）", "────────────────"]

    for r in ok:
        quantity = r.get("quantity") or 1
        prices = r["prices"]
        if prices.get("average"):
            # 无星域挂单（如 PLEX）：三项均按 ESI 全局均价估算
            unit = {"buy": prices["average"], "sell": prices["average"],
                    "mid": prices["average"]}
            estimated += 1
            mark = "≈"
        else:
            unit = {k: prices.get(k) for k in ("buy", "sell", "mid")}
            mark = ""
        if r.get("fuzzy"):
            fuzzy += 1
            mark += "🔍"
        for key in ("buy", "sell", "mid"):
            if unit[key] is not None:
                totals[key] += unit[key] * quantity
                counted[key] += 1
        buy_txt = f"{unit['buy'] * quantity:,.2f}" if unit["buy"] is not None else "-"
        sell_txt = f"{unit['sell'] * quantity:,.2f}" if unit["sell"] is not None else "-"
        lines.append(f"{mark}{r['name']} ×{quantity:,}  买 {buy_txt} ｜ 卖 {sell_txt} ISK")

    lines.append("────────────────")
    for key, icon, label in (("buy", "🟢", "总收购价"),
                             ("sell", "🔴", "总出售价"),
                             ("mid", "⚖️", "总中间价")):
        note = "" if counted[key] == len(ok) else f"（{counted[key]}/{len(ok)} 项有价）"
        lines.append(f"{icon} {label}：{totals[key]:,.2f} ISK{note}")

    if estimated:
        lines.append(f"≈ {estimated} 项无挂单，按 ESI 全局均价估算")
    if fuzzy:
        lines.append(f"🔍 {fuzzy} 项为模糊匹配结果")
    if failed:
        names = "、".join(str(r.get("query") or r.get("name") or "?") for r in failed)
        lines.append(f"⚠️ 未计入 {len(failed)} 项：{names}")
    if errors:
        detail = "；".join(f"{line}（{reason}）" for line, reason in errors[:3])
        lines.append(f"⚠️ 格式错误 {len(errors)} 行：{detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="查询 Jita 市场价格并生成走势图")
    parser.add_argument("name", nargs="?", help="物品名称，可带数量（中文或英文），如：三钛合金*1000")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"走势图天数（默认 {DEFAULT_DAYS}）")
    parser.add_argument("-o", "--output", default=None, help="走势图输出路径")
    parser.add_argument("--refresh-prices", action="store_true",
                        help="只刷新 ESI 全局参考价缓存后退出")
    args = parser.parse_args()

    if args.refresh_prices:
        refresh_price_cache()
        return

    if not args.name:
        parser.error("请提供物品名称，或使用 --refresh-prices 刷新参考价缓存")

    try:
        name, quantity = parse_item_query(args.name)
    except ValueError as exc:
        print(f"参数格式有误：{exc}。用法：market_price.py 物品名称*数量")
        sys.exit(1)

    output = args.output or os.path.join(BASE_DIR, "price_chart.png")
    # 本地物品索引用于模糊匹配；加载失败时退化为仅精确匹配
    db = None
    try:
        from main import get_db, load_config
        db = get_db(load_config())
    except Exception as exc:
        print(f"（本地物品索引不可用，已跳过模糊匹配：{exc}）", file=sys.stderr)

    result = query_item(name, db=db, output_path=output, days=args.days,
                        quantity=quantity)

    if not result["ok"]:
        if result["reason"] == "not_found":
            print("查无此物")
        else:
            print(f"暂无行情数据：{result['name']} (type_id {result['type_id']})")
        sys.exit(1)

    print(format_price_message(result))
    if result["chart"]:
        print(f"\n✅ 走势图已保存: {result['chart']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
