"""全宇宙「高价值舰船击毁」监控：估价 ≥ 阈值的 km 自动推送链接到 QQ。

数据源说明（2026-09-20 实测确认）
- ESI 没有「全宇宙 km 流」端点，单条 km 详情需要 hash，而 hash 只能从第三方拿；
- zKillboard 的 ``/api/kills/`` **必须带实体过滤**（不带过滤返回
  ``{"error":"Please provide an entity filter first."}``），所以只能按**星域**轮询：
  ``GET https://zkillboard.com/api/kills/regionID/{region_id}/``
- 单次请求最多返回 200 条（``limit`` 参数已被官方撤销），返回体本身就是 ESI 格式的 km
  加上 ``zkb`` 统计块（含 ``totalValue`` 估价与 ``hash``），不需要再调 ESI 取详情；
- **不要用 ``pastSeconds``**：实测其索引有 ~15 分钟延迟（``pastSeconds/1800`` 会返回 0 条，
  ``pastSeconds/86400`` 的最新一条比无窗口查询旧），只有不带窗口的「最新 200 条」才是新鲜数据；
  所以增量轮询一律取无窗口结果，靠 ``killmail_id`` 去重；
- 官方要求 User-Agent 可联系、请求频率 ≤ 1 次/秒，因此星域之间 sleep（默认 1 秒）。
- zKillboard 端有「冷缓存」现象：某星域久未查询时首次请求可能耗时 30～60 秒，
  故单请求读取超时取 45 秒，超时的星域本轮跳过（下一轮补上）。

工作流程
1. **首次运行（初始化）**：先按「当前时间 − lookback_hours」（默认 24 小时）用 ``pastSeconds``
   拉一遍历史（该窗口数据较旧、且受 200 条上限影响只覆盖部分），再按无窗口拉一遍最新数据；
   ``push_backfill`` 决定这批历史是否补推（默认只入库、不推送）；
2. 之后每 ``interval_seconds`` 秒扫一遍全部星域（无窗口，每星域最新 200 条），按
   ``killmail_id`` 去重，进程重启不会重复推送；
3. 把 ``isk_value ≥ min_isk`` 且未推送的 km 组装成消息推送，每轮最多 ``max_push_per_cycle`` 条。

用法
    python3 kill_monitor.py                                  # 常驻（supervisor 托管）
    python3 kill_monitor.py --once                            # 只跑一轮
    python3 kill_monitor.py --once --dry-run                  # 只打印，不写库不推送
    python3 kill_monitor.py --once --region-limit 3 --dry-run --verbose   # 快速自测
    python3 kill_monitor.py --reset                           # 清空初始化状态（下次重新初始化）
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from db import _to_mysql_datetime
from esi_client import ESIClient
from eve_push import send_message
from main import get_db, load_config

# 行缓冲：日志重定向到文件时也能实时看到
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "kill_monitor_state.json")
REGIONS_PATH = os.path.join(BASE_DIR, "kill_monitor_regions.json")

BEIJING_TZ = timezone(timedelta(hours=8))
ZKILL_BASE = "https://zkillboard.com/api/kills"
ZKILL_MAX_ROWS = 200          # zKillboard 单次返回上限，达到即视为可能截断

REGIONS_TTL_SECONDS = 24 * 3600
NAME_CHUNK = 1000             # /universe/names/ 单次最多 1000 个 id
PUSH_INTERVAL = 0.5           # 连续推送之间的间隔，避免刷屏太快
ALERT_COOLDOWN = 1800         # 大面积拉取失败告警的最小间隔

DEFAULT_MIN_ISK = 1_000_000_000      # 10 亿
DEFAULT_INTERVAL = 600
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_MAX_PUSH = 30
DEFAULT_REQUEST_INTERVAL = 1.0
REQUEST_TIMEOUT = (10, 45)    # (连接, 读取) 秒；冷缓存星域可能要 30~60 秒


class ZkillError(RuntimeError):
    """zKillboard 请求失败。"""


# ---------------------------------------------------------------- 小工具

def now_str():
    """当前北京时间字符串（与数据库里的时间口径一致）。"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fmt_isk(value):
    """把 ISK 数值格式化成「亿 / 万」可读形式。"""
    v = float(value or 0)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f} 亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f} 万"
    return f"{v:,.0f}"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 配置

def monitor_config(config):
    """合并 config.json 的 kill_monitor 段与 push 段，返回生效配置。"""
    m = config.get("kill_monitor") or {}
    push = config.get("push") or {}
    interval = int(m.get("interval_seconds", DEFAULT_INTERVAL))
    return {
        "min_isk": float(m.get("min_isk", DEFAULT_MIN_ISK)),
        "interval_seconds": max(120, interval),
        "lookback_hours": float(m.get("lookback_hours", DEFAULT_LOOKBACK_HOURS)),
        "push_backfill": bool(m.get("push_backfill", False)),
        "max_push_per_cycle": max(1, int(m.get("max_push_per_cycle", DEFAULT_MAX_PUSH))),
        "request_interval_seconds": max(0.5, float(m.get("request_interval_seconds", DEFAULT_REQUEST_INTERVAL))),
        "target_user": m.get("target_user") or push.get("target_user"),
        "target_group": m.get("target_group", push.get("target_group")),
        "user_agent": m.get("user_agent") or config.get("user_agent") or "eve-wallet-tracker/1.0",
    }


# ---------------------------------------------------------------- 星域列表

def load_regions(client, log, force=False, limit=None):
    """获取全部星域 ID（ESI /universe/regions/，本地缓存 24 小时）。"""
    cache = load_json(REGIONS_PATH, {})
    fetched_at = float(cache.get("fetched_at") or 0)
    regions = cache.get("regions") or []
    if not force and regions and (time.time() - fetched_at) < REGIONS_TTL_SECONDS:
        log(f"星域列表：使用缓存（{len(regions)} 个星域，{int((time.time() - fetched_at) / 60)} 分钟前获取）")
    else:
        regions = [int(r) for r in client.get_region_ids()]
        save_json(REGIONS_PATH, {"fetched_at": time.time(), "regions": regions})
        log(f"星域列表：已刷新（{len(regions)} 个星域）")
    if limit:
        regions = regions[: int(limit)]
    return regions


# ---------------------------------------------------------------- 采集

def fetch_region_kills(session, region_id, past_seconds, user_agent, retries=2):
    """拉取单个星域的 km 列表（zKillboard 公开 API）。

    past_seconds 为空/0 时使用无窗口查询（最新 200 条，数据最新鲜）；
    传入秒数则用 ``pastSeconds`` 过滤（仅初始化回填历史时用，索引延迟较大）。
    """
    url = f"{ZKILL_BASE}/regionID/{int(region_id)}/"
    if past_seconds:
        url += f"pastSeconds/{int(past_seconds)}/"
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"网络异常：{exc.__class__.__name__}"
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    last_error = f"响应不是 JSON：{exc}"
                else:
                    # 实测 zKillboard 在无数据时会返回 [null]，非 dict 元素一律丢弃
                    if not isinstance(data, list):
                        return []
                    return [k for k in data if isinstance(k, dict)]
            elif resp.status_code == 404:
                return []                      # 该星域窗口内无数据
            elif resp.status_code in (420, 429) or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}（限流/服务端错误）"
            else:
                raise ZkillError(f"HTTP {resp.status_code} - {resp.text[:120]}")
        if attempt < retries:
            time.sleep(2 * attempt)
    raise ZkillError(str(last_error))


def kill_row(km, region_id):
    """把 zKillboard 返回的一条 km 归一化成数据库行（dict）。"""
    if not isinstance(km, dict):
        return None
    try:
        killmail_id = int(km.get("killmail_id"))
    except (TypeError, ValueError):
        return None
    zkb = km.get("zkb") or {}
    victim = km.get("victim") or {}
    return {
        "killmail_id": killmail_id,
        "killmail_time": _to_mysql_datetime(km.get("killmail_time")),
        "solar_system_id": km.get("solar_system_id"),
        "region_id": region_id,
        "victim_character_id": victim.get("character_id"),
        "victim_corporation_id": victim.get("corporation_id"),
        "victim_alliance_id": victim.get("alliance_id"),
        "ship_type_id": victim.get("ship_type_id"),
        "attacker_count": len(km.get("attackers") or []),
        "isk_value": round(float(zkb.get("totalValue") or 0), 2),
        "dropped_value": round(float(zkb.get("droppedValue") or 0), 2),
        "zkb_hash": zkb.get("hash"),
    }


def sweep(db, client, cfg, window_seconds, log, dry_run=False, region_limit=None,
          verbose=False, since=None):
    """扫一遍全部星域，返回统计信息。

    window_seconds 为空时用无窗口查询（每星域最新 200 条）；
    since（北京时间字符串）非空时丢弃 killmail_time 早于它的 km —— 无窗口查询对
    冷门星域会返回几个月甚至几年前的数据，必须靠起点过滤。
    """
    regions = load_regions(client, log, limit=region_limit)
    session = requests.Session()
    stats = {
        "window_seconds": int(window_seconds) if window_seconds else None,
        "regions": len(regions),
        "failed": 0,
        "saturated": 0,
        "seen": 0,
        "inserted": 0,
        "high_value": 0,
        "skipped_old": 0,
        "failures": [],
    }
    scope = f"pastSeconds={int(window_seconds)}" if window_seconds else "无窗口（每星域最新 200 条）"
    log(f"开始扫描：{len(regions)} 个星域 ｜ {scope}")
    if since:
        log(f"  时间起点：{since}（早于此时间的 km 一律丢弃）")
    for idx, region_id in enumerate(regions, 1):
        try:
            kills = fetch_region_kills(
                session, region_id, window_seconds, cfg["user_agent"]
            )
        except ZkillError as exc:
            stats["failed"] += 1
            stats["failures"].append((region_id, str(exc)))
            log(f"  [{idx}/{len(regions)}] 星域 {region_id} 拉取失败（本轮跳过）：{exc}")
            continue

        if len(kills) >= ZKILL_MAX_ROWS:
            stats["saturated"] += 1
            if verbose:
                log(f"  [{idx}/{len(regions)}] 星域 {region_id} 返回 {len(kills)} 条（已达 200 上限）")
        rows = []
        for km in kills:
            row = kill_row(km, region_id)
            if not row:
                continue
            if since and row["killmail_time"] and row["killmail_time"] < since:
                stats["skipped_old"] += 1
                continue
            rows.append(row)
            if (row["isk_value"] or 0) >= cfg["min_isk"]:
                stats["high_value"] += 1
        stats["seen"] += len(rows)

        if dry_run:
            if rows:
                log(f"  [{idx}/{len(regions)}] 星域 {region_id}: {len(rows)} 条"
                    f"（≥阈值 {sum(1 for r in rows if (r['isk_value'] or 0) >= cfg['min_isk'])} 条）")
        else:
            high = sum(1 for r in rows if (r["isk_value"] or 0) >= cfg["min_isk"])
            try:
                added = db.insert_killmails([
                    (r["killmail_id"], r["killmail_time"], r["solar_system_id"], r["region_id"],
                     r["victim_character_id"], r["victim_corporation_id"], r["victim_alliance_id"],
                     r["ship_type_id"], r["attacker_count"], r["isk_value"], r["dropped_value"],
                     r["zkb_hash"])
                    for r in rows
                ])
                stats["inserted"] += added
                if verbose:
                    log(f"  [{idx}/{len(regions)}] 星域 {region_id}: 返回 {len(rows)} 条，"
                        f"新增 {added} 条（≥阈值 {high} 条）")
            except Exception as exc:      # noqa: BLE001 单个星域写库失败不影响整轮
                log(f"      ⚠️ 星域 {region_id} 写库失败：{exc}")

        if idx < len(regions):
            time.sleep(cfg["request_interval_seconds"])

    log(f"扫描完成：星域 {stats['regions'] - stats['failed']}/{stats['regions']} 成功，"
        f"返回 {stats['seen']} 条，新增入库 {stats['inserted']} 条，"
        f"其中 ≥{fmt_isk(cfg['min_isk'])} 的 {stats['high_value']} 条"
        + (f"，丢弃超起点旧数据 {stats['skipped_old']} 条" if stats["skipped_old"] else "")
        + (f"，{stats['saturated']} 个星域返回达 200 上限（正常，仍有更早数据未取）"
           if stats["saturated"] else ""))
    return stats

# ---------------------------------------------------------------- 名称解析

def ensure_names(db, client, ids, log):
    """确保 id→名称缓存里已有这些 id，返回 {id: name}。失败不抛错。"""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    try:
        missing = db.get_missing_name_ids(ids)
        for start in range(0, len(missing), NAME_CHUNK):
            chunk = missing[start:start + NAME_CHUNK]
            try:
                data = client.resolve_ids(chunk)
            except Exception as exc:      # noqa: BLE001
                log(f"  ⚠️ 名称解析失败（{len(chunk)} 个）：{exc}")
                continue
            db.upsert_names([
                (e.get("id"), e.get("name"), e.get("category")) for e in (data or [])
            ])
    except Exception as exc:              # noqa: BLE001
        log(f"  ⚠️ 名称缓存读写失败：{exc}")
    try:
        return db.get_names(ids)
    except Exception:                     # noqa: BLE001
        return {}


def build_message(row, names, min_isk):
    """组装单条 km 推送消息。"""
    killmail_id = int(row["killmail_id"])
    isk = float(row["isk_value"] or 0)
    ship_id = int(row["ship_type_id"] or 0)
    ship = names.get(ship_id) or (f"type_id {ship_id}" if ship_id else "未知舰船")
    system_id = int(row["solar_system_id"] or 0)
    system = names.get(system_id) or f"星系 {system_id}"
    victim = names.get(int(row["victim_character_id"] or 0))
    corp = names.get(int(row["victim_corporation_id"] or 0))

    lines = [
        f"💥 高价值舰船击毁（阈值 {fmt_isk(min_isk)} ISK）",
        f"💰 估价：{isk:,.0f} ISK（{fmt_isk(isk)}）",
        f"🚀 舰船：{ship}",
    ]
    if victim:
        lines.append(f"🏴 受击方：{victim}" + (f"（{corp}）" if corp else ""))
    lines.append(
        f"📍 星系：{system}（{system_id}）｜攻击者 {int(row['attacker_count'] or 0)} 人"
    )
    lines.append(f"🕒 {row['killmail_time']}")
    lines.append(f"🔗 https://zkillboard.com/kill/{killmail_id}/")
    return "\n".join(lines)


def display_names(db, client, rows, log):
    """组装展示用名称：舰船优先用本地中文名（item_types），其余走 universe_names 缓存。"""
    ship_ids = [r["ship_type_id"] for r in rows if r["ship_type_id"]]
    other_ids = (
        [r["solar_system_id"] for r in rows]
        + [r["victim_character_id"] for r in rows]
        + [r["victim_corporation_id"] for r in rows]
    )
    names = dict(ensure_names(db, client, other_ids, log))

    zh_ships = {}
    if ship_ids:
        try:
            zh_ships = db.get_item_type_names(ship_ids)      # 本地 SDE 中文名
        except Exception as exc:                             # noqa: BLE001
            log(f"  ⚠️ 本地物品名查询失败：{exc}")
        fallback = ensure_names(db, client, ship_ids, log)
        for tid, name in fallback.items():
            zh_ships.setdefault(tid, name)
    names.update(zh_ships)
    return names


# ---------------------------------------------------------------- 推送

def push_pending(db, client, cfg, log, dry_run=False, since=None):
    """把待推送的高价值 km 推送到 QQ，返回成功条数。"""
    rows = db.pending_high_value_kills(
        cfg["min_isk"], limit=cfg["max_push_per_cycle"], since=since
    )
    if not rows:
        return 0
    log(f"待推送 {len(rows)} 条（阈值 {fmt_isk(cfg['min_isk'])} ISK）")
    names = display_names(db, client, rows, log)
    sent = []
    for row in rows:
        message = build_message(row, names, cfg["min_isk"])
        if dry_run:
            log("  [dry-run] " + message.replace("\n", " ｜ "))
            continue
        try:
            send_message(
                message,
                target_user=cfg["target_user"],
                target_group=cfg["target_group"],
            )
        except Exception as exc:          # noqa: BLE001 推送失败留待下一轮重试
            log(f"  ⚠️ km {row['killmail_id']} 推送失败：{exc}")
            continue
        sent.append(row["killmail_id"])
        log(f"  ✅ 已推送 km {row['killmail_id']}（{fmt_isk(row['isk_value'])} ISK）")
        time.sleep(PUSH_INTERVAL)
    if sent and not dry_run:
        db.mark_killmails_pushed(sent)
    return len(sent)


def alert_failures(cfg, state, stats, log):
    """大面积拉取失败时告警（带冷却，避免刷屏）。"""
    if not stats["failed"] or not stats["regions"]:
        return
    ratio = stats["failed"] / stats["regions"]
    if ratio < 0.3:
        return
    last = float(state.get("alert_sent_at") or 0)
    if time.time() - last < ALERT_COOLDOWN:
        return
    sample = "、".join(f"{r}" for r, _ in stats["failures"][:5])
    try:
        send_message(
            f"⚠️ 全宇宙 km 监控异常：{stats['failed']}/{stats['regions']} 个星域拉取失败"
            f"（例：{sample}）\n最近一次扫描：{now_str()}",
            target_user=cfg["target_user"],
            target_group=cfg["target_group"],
        )
        state["alert_sent_at"] = time.time()
        log("已推送拉取失败告警")
    except Exception as exc:              # noqa: BLE001
        log(f"  ⚠️ 发送失败告警也失败：{exc}")


# ---------------------------------------------------------------- 主循环

def cutoff_time(state, cfg):
    """返回数据起点（北京时间字符串）。

    已有 state["cutoff_time"] 则直接用；否则按「初始化时间 − lookback_hours」倒推
    （保证进程重启/状态升级后口径一致）。
    """
    if state.get("cutoff_time"):
        return state["cutoff_time"]
    base = state.get("initialized_at")
    dt = None
    if base:
        try:
            dt = datetime.strptime(str(base), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.now(BEIJING_TZ).replace(tzinfo=None, microsecond=0)
    return (dt - timedelta(hours=cfg["lookback_hours"])).strftime("%Y-%m-%d %H:%M:%S")


def run_cycle(db, client, cfg, state, log, args):
    """跑一轮：初始化（首轮）或增量扫描 + 推送。"""
    since = cutoff_time(state, cfg)
    if not state.get("initialized"):
        window = max(60, int(cfg["lookback_hours"] * 3600))
        state["cutoff_time"] = since
        log(f"🚀 首次运行：初始化数据起点 = {since}"
            f"（当前时间 − {cfg['lookback_hours']:g} 小时，pastSeconds={window}）")
        stats = sweep(db, client, cfg, window, log,
                      dry_run=args.dry_run, region_limit=args.region_limit,
                      verbose=args.verbose, since=since)
        log("初始化历史拉取完成，再按无窗口拉一遍最新数据…")
        fresh = sweep(db, client, cfg, None, log,
                      dry_run=args.dry_run, region_limit=args.region_limit,
                      verbose=args.verbose, since=since)
        stats["seen"] += fresh["seen"]
        stats["inserted"] += fresh["inserted"]
        stats["high_value"] += fresh["high_value"]
        stats["failed"] += fresh["failed"]
        stats["saturated"] += fresh["saturated"]
        stats["skipped_old"] += fresh["skipped_old"]
        if not args.dry_run:
            if cfg["push_backfill"]:
                log("  初始化历史按配置补推（每轮最多 "
                    f"{cfg['max_push_per_cycle']} 条，分多轮推完）")
            else:
                skipped = db.mark_all_pending_pushed(cfg["min_isk"])
                log(f"  初始化历史入库完成：{skipped} 条 ≥ 阈值的数据标记为「不补推」"
                    f"（需要补推请把 config.json 的 kill_monitor.push_backfill 设为 true）")
            removed = db.delete_killmails_before(since)
            if removed:
                log(f"  已清理起点之前的远古 km {removed} 条")
            state["initialized"] = True
            state["initialized_at"] = now_str()
            state["initial_window_seconds"] = window
        alert_failures(cfg, state, stats, log)
    else:
        stats = sweep(db, client, cfg, None, log,
                      dry_run=args.dry_run, region_limit=args.region_limit,
                      verbose=args.verbose, since=since)
        alert_failures(cfg, state, stats, log)

    pushed = push_pending(db, client, cfg, log, dry_run=args.dry_run, since=since)
    if not args.dry_run:
        state["last_sweep_at"] = now_str()
        state["last_sweep_stats"] = {
            "regions": stats["regions"],
            "failed": stats["failed"],
            "seen": stats["seen"],
            "inserted": stats["inserted"],
            "high_value": stats["high_value"],
            "skipped_old": stats.get("skipped_old", 0),
            "pushed": pushed,
        }
        save_json(STATE_PATH, state)
    return stats


def main():
    parser = argparse.ArgumentParser(description="全宇宙高价值 km 监控（zKillboard 星域轮询）")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库不推送")
    parser.add_argument("--reset", action="store_true", help="清空初始化状态（下次重新初始化）")
    parser.add_argument("--interval", type=int, help="扫描间隔秒数（默认取 config.json）")
    parser.add_argument("--min-isk", type=float, help="估价阈值（ISK），覆盖配置")
    parser.add_argument("--lookback-hours", type=float, help="初始化回溯小时数，覆盖配置")
    parser.add_argument("--push-backfill", action="store_true",
                        help="初始化时一并补推历史（覆盖配置）")
    parser.add_argument("--region-limit", type=int, help="只扫前 N 个星域（自测用）")
    parser.add_argument("--verbose", action="store_true", help="打印每个星域的明细")
    args = parser.parse_args()

    def log(message):
        print(f"[{now_str()}] {message}")

    config = load_config()
    cfg = monitor_config(config)
    if args.interval:
        cfg["interval_seconds"] = max(60, args.interval)
    if args.min_isk:
        cfg["min_isk"] = float(args.min_isk)
    if args.lookback_hours:
        cfg["lookback_hours"] = float(args.lookback_hours)
    if args.push_backfill:
        cfg["push_backfill"] = True

    state = {} if args.reset else load_json(STATE_PATH, {})
    if args.reset:
        log("已重置初始化状态（下次运行将按 lookback_hours 重新初始化）")

    if not cfg["target_user"] and not cfg["target_group"]:
        log("⚠️ 未配置推送目标（kill_monitor.target_user / push.target_user），将只入库不推送")

    db = get_db(config)
    client = ESIClient(None, cfg["user_agent"])   # 只访问公开端点

    log("全宇宙 km 监控启动："
        f"阈值 {fmt_isk(cfg['min_isk'])} ISK ｜ 间隔 {cfg['interval_seconds']}s"
        f" ｜ 初始化回溯 {cfg['lookback_hours']:g}h"
        f" ｜ 补推历史 {'是' if cfg['push_backfill'] else '否'}"
        f" ｜ 目标 {cfg['target_group'] or cfg['target_user']}"
        + (" ｜ [dry-run]" if args.dry_run else ""))

    while True:
        started = time.time()
        try:
            run_cycle(db, client, cfg, state, log, args)
        except Exception as exc:          # noqa: BLE001 单轮失败不影响常驻
            log(f"❌ 本轮执行异常：{exc}")
        if args.once:
            break
        elapsed = time.time() - started
        wait = max(10, cfg["interval_seconds"] - elapsed)
        log(f"本轮耗时 {elapsed:.0f}s，{wait:.0f}s 后开始下一轮")
        time.sleep(wait)


if __name__ == "__main__":
    main()
