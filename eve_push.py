"""推送 EVE 最新钱包流水到 QQ（通过 SnowLuma OneBot v11 HTTP API）。

前置条件：
- SnowLuma 已运行且 OneBot HTTP 服务器已开启（默认 127.0.0.1:3000）
- config.json 的 push 段已配置 onebot_http 与 access_token

用法：
    python eve_push.py                     # 推送到 config.json 配置的目标
    python eve_push.py --user 1234567      # 私聊推送到指定 QQ
    python eve_push.py --group 987654      # 群聊推送
    python eve_push.py --from-db           # 从数据库读取最新流水（默认从 ESI）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from esi_client import ESIClient, translate_description
from main import get_access_token, get_db, load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_ONEBOT_BASE = "http://127.0.0.1:3000"
BEIJING_TZ = timezone(timedelta(hours=8))


def _onebot_client():
    """从 config.json 读取 OneBot HTTP 地址与 token，返回 (base_url, headers)。"""
    config = load_config()
    push_cfg = config.get("push", {})
    base = push_cfg.get("onebot_http", DEFAULT_ONEBOT_BASE)
    token = push_cfg.get("access_token", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base, headers


def send_message(message, target_user=None, target_group=None):
    """通过 OneBot 发送消息，返回响应 JSON。"""
    base, headers = _onebot_client()
    if target_group:
        url = f"{base}/send_group_msg"
        payload = {"group_id": int(target_group), "message": message}
    else:
        url = f"{base}/send_private_msg"
        payload = {"user_id": int(target_user), "message": message}
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_image_message(image_path, target_user=None, target_group=None, text=""):
    """通过 OneBot 发送本地图片（可选附带文本），返回响应 JSON。"""
    image_uri = Path(image_path).resolve().as_uri()
    segments = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    segments.append({"type": "image", "data": {"file": image_uri}})

    base, headers = _onebot_client()
    if target_group:
        url = f"{base}/send_group_msg"
        payload = {"group_id": int(target_group), "message": segments}
    else:
        url = f"{base}/send_private_msg"
        payload = {"user_id": int(target_user), "message": segments}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_latest_from_esi(config, db):
    """从 ESI 获取最新一条流水及当前余额。返回 (entry, balance, character)。"""
    chars = db.list_characters()
    if not chars:
        print("没有已授权角色。")
        return None, None, None
    c = chars[0]
    cid = c["character_id"]
    token = get_access_token(config, db, cid)
    if not token:
        print(f"角色 {c['character_name']} 无有效 token。")
        return None, None, None
    client = ESIClient(token, config["user_agent"])
    batch = client._get(f"/v4/characters/{cid}/wallet/journal/", params={"page": 1})
    balance = client.get_wallet_balance(cid)
    if not batch:
        return None, None, c
    return batch[0], balance, c


def get_latest_from_db(db):
    """从数据库读取最新一条流水及最新余额快照。"""
    chars = db.list_characters()
    if not chars:
        return None, None, None
    c = chars[0]
    rows = db.get_journal(c["character_id"], limit=1)
    if not rows:
        return None, None, c
    entry = {
        "id": rows[0]["ref_id"],
        "date": str(rows[0]["journal_date"]),
        "amount": float(rows[0]["amount"]),
        "balance": float(rows[0]["balance"]),
        "description": rows[0]["description"],
    }
    hist = db.get_balance_history(c["character_id"], limit=1)
    balance = float(hist[0]["balance"]) if hist else None
    return entry, balance, c


def market_escrow_transactions(db, character_id, ref_ids):
    """按 journal_ref_id 取关联的市场交易详情，返回 [(名称, 方向, 数量, 单价, 总额)]。

    供 eve_push 与 auto_query 共用，避免两处重复实现「市场托管释放 → 交易详情」。
    """
    out = []
    for txn in db.get_wallet_transactions_by_journal_refs(character_id, ref_ids):
        name = txn.get("type_name") or (
            f"物品#{txn.get('type_id')}" if txn.get("type_id") else "未知物品"
        )
        action = "买入" if txn.get("is_buy") else "卖出"
        qty = int(txn.get("quantity") or 0)
        unit = float(txn.get("unit_price") or 0)
        total = float(txn.get("total_price") or unit * qty)
        out.append((name, action, qty, unit, total))
    return out


def format_message(entry, balance, character, db=None):
    """将流水格式化为中文推送消息。

    当 db 可用且流水为“市场托管释放”时，将说明替换为市场交易详情。
    """
    amount = float(entry.get("amount", 0) or 0)
    desc_cn = translate_description(entry.get("description"))
    date_raw = entry.get("date") or ""
    try:
        dt = datetime.fromisoformat(str(date_raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # 数据库中的时间已统一存为北京时间（UTC+8）
            date_cn = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_cn = dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        date_cn = str(date_raw)[:19]

    flow_type = "📥 收入" if amount >= 0 else "📤 支出"
    lines = [
        "💰 EVE 钱包最新流水",
        "────────────────",
        f"{flow_type}：{amount:+,.2f} ISK",
    ]
    if balance is not None:
        lines.append(f"🏦 当前余额：{float(balance):,.2f} ISK")

    market_lines = []
    if db is not None and desc_cn == "市场托管释放":
        journal_ref_id = entry.get("id") or entry.get("ref_id")
        for name, action, qty, unit, total in market_escrow_transactions(
            db, character["character_id"], [journal_ref_id]
        ):
            market_lines.append(
                f"📦 {name} {action} x{qty} 单价 {unit:,.2f} ISK，总额 {total:+,.2f} ISK"
            )

    if market_lines:
        lines.extend(market_lines)
    else:
        lines.append(f"📝 说明：{desc_cn}")

    lines.append(f"🕐 时间：{date_cn}（北京时间）")
    lines.append(f"👤 角色：{character['character_name']}")
    lines.append(f"🆔 流水ID：{entry.get('id') or entry.get('ref_id')}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="推送 EVE 最新钱包流水到 QQ")
    parser.add_argument("--user", type=int, help="私聊推送的目标 QQ 号")
    parser.add_argument("--group", type=int, help="群聊推送的目标群号")
    parser.add_argument("--from-db", action="store_true", help="从数据库读取最新流水（默认从 ESI）")
    parser.add_argument("--dry-run", action="store_true", help="只生成消息不发送")
    args = parser.parse_args()

    config = load_config()
    db = get_db(config)

    # 确定目标：命令行参数优先，否则使用配置
    push_cfg = config.get("push", {})
    target_group = args.group or push_cfg.get("target_group")
    target_user = args.user or push_cfg.get("target_user")
    if not target_group and not target_user:
        sys.exit("未指定推送目标：请用 --user QQ号 / --group 群号，或在 config.json 配置 push.target_user / push.target_group")

    # 获取最新流水
    if args.from_db:
        entry, balance, character = get_latest_from_db(db)
    else:
        entry, balance, character = get_latest_from_esi(config, db)
    if entry is None:
        print("未能获取到最新流水。")
        sys.exit(1)

    message = format_message(entry, balance, character, db)
    print("=== 推送内容 ===")
    print(message)

    if args.dry_run:
        print("\n（dry-run 模式，未发送）")
        return

    resp = send_message(message, target_user=target_user, target_group=target_group)
    if resp.get("status") == "ok":
        print(f"\n✅ 已推送，消息ID: {resp['data'].get('message_id')}")
    else:
        print(f"\n❌ 推送失败: {resp}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
