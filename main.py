"""EVE 钱包余额与变动查询工具（主入口，MySQL 多角色版）。

用法：
    python main.py --init-db              # 初始化数据库表结构
    python main.py --add-account          # 新增一个角色的授权
    python main.py --list                 # 列出所有已授权角色
    python main.py                        # 查询所有角色的余额与流水
    python main.py --char chuxins1        # 查询指定角色
    python main.py --balance --char 123   # 仅查询余额
    python main.py --journal 100          # 查询最近 100 条变动流水
    python main.py --market-transactions  # 同步并查看市场交易详情
    python main.py --remove chuxins1      # 删除角色及其数据
    python main.py --reset-token chuxins1 # 清除指定角色 token 并重新授权
    python main.py --migrate              # 从旧版 token.json 迁移单角色
"""

import argparse
import json
import os
import sys

from auth import (
    OAuthError,
    authorize_flow,
    is_token_expired,
    refresh_access_token,
    verify,
)
from db import Database
from esi_client import ESIClient, translate_description

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE_PATH = os.path.join(BASE_DIR, "token.json")  # 旧版单角色文件，仅用于迁移


# ---------------------------------------------------------------- 配置加载

def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            f"未找到配置文件 {CONFIG_PATH}\n"
            f"请复制 config.example.json 为 config.json 并填写你的 EVE 应用凭证。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    required = ("client_id", "client_secret", "callback_url")
    for key in required:
        if not config.get(key) or "在此填写" in str(config.get(key, "")):
            sys.exit(f"配置文件缺少有效字段：{key}，请检查 config.json。")

    if "db" not in config:
        sys.exit("配置文件缺少 db 数据库配置，请参考 config.example.json 补充。")

    scopes = config.get("scopes") or ["esi-wallet.read_character_wallet.v1"]
    config["scope"] = " ".join(scopes)
    config.setdefault("user_agent", "eve-wallet-tracker/1.0")
    config.setdefault("journal_limit", 50)
    return config


def get_db(config):
    return Database(config["db"])


# ---------------------------------------------------------------- 授权与 token

def authorize_account(config, db):
    """执行一次 OAuth 授权，将新角色与 token 保存到数据库。"""
    print("开始 OAuth 授权流程...")
    token = authorize_flow(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        callback_url=config["callback_url"],
        scope=config["scope"],
        user_agent=config["user_agent"],
    )
    char = verify(token["access_token"], config["user_agent"])
    cid = int(char["CharacterID"])
    name = char.get("CharacterName", str(cid))

    db.upsert_character(cid, name, config["scope"])
    db.upsert_token(cid, token)
    print(f"✅ 已授权并保存角色：{name} (ID: {cid})")
    return cid


def get_access_token(config, db, character_id):
    """获取指定角色的有效 access_token（必要时自动刷新）。"""
    row = db.get_token(character_id)
    if not row:
        return None
    if not is_token_expired(row):
        return row["access_token"]
    if row.get("refresh_token"):
        print(f"  角色 {character_id} token 已过期，正在刷新...")
        try:
            new_token = refresh_access_token(
                row["refresh_token"],
                config["client_id"],
                config["client_secret"],
                config["user_agent"],
            )
            db.upsert_token(character_id, new_token)
            return new_token["access_token"]
        except OAuthError as exc:
            print(f"  刷新失败：{exc}")
    return None


def resolve_characters(db, char_arg=None):
    """解析目标角色列表。char_arg 为 None 时返回全部角色。"""
    chars = db.list_characters()
    if not chars:
        sys.exit("数据库中没有已授权角色，请先运行：python main.py --add-account")
    if not char_arg:
        return chars
    matched = [
        c for c in chars
        if str(c["character_id"]) == str(char_arg) or c["character_name"] == char_arg
    ]
    if not matched:
        sys.exit(f"未找到角色 {char_arg}，可用 python main.py --list 查看已授权角色。")
    return matched


# ---------------------------------------------------------------- 格式化与输出

def fmt_isk(value):
    """将 ISK 数值格式化为带千分位。"""
    return f"{value:,.2f}"


def show_balance(db, character, access_token, config):
    client = ESIClient(access_token, config["user_agent"])
    balance = client.get_wallet_balance(character["character_id"])
    db.record_balance(character["character_id"], balance)
    print(f"当前钱包余额：{fmt_isk(balance)} ISK")
    print("-" * 60)


def show_journal(db, character, access_token, config, limit):
    client = ESIClient(access_token, config["user_agent"])
    entries = client.get_wallet_journal(character["character_id"], limit=limit)
    if entries:
        db.upsert_journal(character["character_id"], entries)

    # 优先展示数据库中持久化的流水（含历史积累）
    stored = db.get_journal(character["character_id"], limit=limit)
    rows = stored if stored else entries

    if not rows:
        print("没有找到钱包变动流水。")
        return

    print(f"最近 {len(rows)} 条钱包变动流水（已持久化到 MySQL）：")
    print(f"{'时间':<20}{'变动(ISK)':>14}{'变动后余额(ISK)':>20}  描述")
    print("-" * 60)
    for e in rows:
        date = str(e.get("journal_date") or e.get("date") or "")[:19]
        amount = float(e.get("amount", 0.0) or 0.0)
        balance = e.get("balance", None)
        desc = translate_description(e.get("description"))
        balance_str = fmt_isk(balance) if balance is not None else "-"
        print(f"{date:<20}{fmt_isk(amount):>14}{balance_str:>20}  {desc}")

    # 汇总统计
    summary = ESIClient.summarize(rows)
    print("-" * 60)
    print("收支汇总（基于上述记录）：")
    print(f"  记录条数：{summary['count']}")
    print(f"  总收入  ：{fmt_isk(summary['total_income'])} ISK")
    print(f"  总支出  ：{fmt_isk(summary['total_expense'])} ISK")
    print(f"  税费合计：{fmt_isk(summary['tax_total'])} ISK")
    print(f"  净变动  ：{fmt_isk(summary['net'])} ISK")
    print("-" * 60)


def show_market_transactions(db, character, access_token, config, limit=20):
    """从 ESI 同步并展示市场交易详情。"""
    from market import sync_market_transactions

    client = ESIClient(access_token, config["user_agent"])
    entries = sync_market_transactions(db, client, character["character_id"])
    print(f"已同步 {len(entries)} 条市场交易记录")

    rows = db.get_wallet_transactions(character["character_id"], limit=limit)
    if not rows:
        print("暂无市场交易记录。")
        return

    print(f"最近 {len(rows)} 条市场交易详情（已持久化到 MySQL）：")
    print(f"{'时间':<20}{'方向':<6}{'物品':<40}{'数量':>8}{'单价(ISK)':>16}{'总额(ISK)':>18}")
    print("-" * 110)
    for r in rows:
        date = str(r.get("date") or "")[:19]
        action = "买入" if r.get("is_buy") else "卖出"
        name = r.get("type_name") or (
            f"物品#{r.get('type_id')}" if r.get("type_id") else "未知物品"
        )
        qty = int(r.get("quantity") or 0)
        unit = float(r.get("unit_price") or 0)
        total = float(r.get("total_price") or unit * qty)
        print(f"{date:<20}{action:<6}{name:<40}{qty:>8}{unit:>16,.2f}{total:>18,.2f}")
    print("-" * 110)


def migrate_legacy_token(config, db):
    """将旧版 token.json 中的单角色数据迁移到数据库。"""
    if not os.path.exists(TOKEN_FILE_PATH):
        print("未找到 token.json，无需迁移。")
        return
    with open(TOKEN_FILE_PATH, "r", encoding="utf-8") as f:
        token = json.load(f)
    char = verify(token.get("access_token", ""), config["user_agent"])
    cid = int(char["CharacterID"])
    name = char.get("CharacterName", str(cid))
    db.upsert_character(cid, name, config["scope"])
    db.upsert_token(cid, token)
    os.remove(TOKEN_FILE_PATH)
    print(f"✅ 已从 token.json 迁移角色：{name} (ID: {cid})，旧文件已删除。")


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="EVE 钱包查询工具（MySQL 多角色版）")
    parser.add_argument("--init-db", action="store_true", help="初始化数据库表结构")
    parser.add_argument("--add-account", action="store_true", help="新增角色授权")
    parser.add_argument("--list", action="store_true", help="列出所有已授权角色")
    parser.add_argument("--char", metavar="ID或名称", help="指定要查询的角色（默认查询所有）")
    parser.add_argument("--remove", metavar="ID或名称", help="删除指定角色及其数据")
    parser.add_argument("--reset-token", metavar="ID或名称", help="清除指定角色 token 并重新授权")
    parser.add_argument("--migrate", action="store_true", help="从旧版 token.json 迁移单角色")
    parser.add_argument("--balance", action="store_true", help="仅查询余额")
    parser.add_argument("--journal", type=int, metavar="N", help="查询最近 N 条变动流水")
    parser.add_argument("--market-transactions", action="store_true", help="同步并查看市场交易详情")
    args = parser.parse_args()

    config = load_config()

    if args.init_db:
        Database(config["db"])
        print("✅ 数据库表已初始化。")
        return

    db = get_db(config)

    if args.add_account:
        authorize_account(config, db)
        return

    if args.list:
        chars = db.list_characters()
        if not chars:
            print("暂无已授权角色，请运行 python main.py --add-account 添加。")
            return
        print(f"{'角色ID':<14}{'角色名':<24}{'token状态':<14}{'授权时间'}")
        print("-" * 66)
        for c in chars:
            state = "✅ 有 token" if c.get("has_token") else "❌ 无 token"
            print(f"{c['character_id']:<14}{c['character_name']:<24}{state:<14}{c.get('created_at')}")
        return

    if args.remove:
        targets = resolve_characters(db, args.remove)
        for c in targets:
            db.delete_character(c["character_id"])
            print(f"已删除角色 {c['character_name']} (ID: {c['character_id']})")
        return

    if args.reset_token:
        targets = resolve_characters(db, args.reset_token)
        for c in targets:
            db.delete_token(c["character_id"])
            print(f"已清除角色 {c['character_name']} 的 token，重新运行 --add-account 可再次授权。")
        return

    if args.migrate:
        migrate_legacy_token(config, db)
        return

    if args.market_transactions:
        targets = resolve_characters(db, args.char)
        for c in targets:
            print("=" * 60)
            print(f"角色：{c['character_name']} (ID: {c['character_id']})")
            access_token = get_access_token(config, db, c["character_id"])
            if not access_token:
                print("  该角色无有效 token，请重新授权：python main.py --add-account")
                continue
            show_market_transactions(db, c, access_token, config)
        return

    # 正常查询流程
    targets = resolve_characters(db, args.char)
    do_balance = args.balance or (not args.journal)
    do_journal = bool(args.journal) or (not args.balance)
    limit = args.journal if args.journal else config["journal_limit"]

    for c in targets:
        print("=" * 60)
        print(f"角色：{c['character_name']} (ID: {c['character_id']})")
        access_token = get_access_token(config, db, c["character_id"])
        if not access_token:
            print("  该角色无有效 token，请重新授权：python main.py --add-account")
            continue
        if do_balance:
            show_balance(db, c, access_token, config)
        if do_journal:
            show_journal(db, c, access_token, config, limit)


if __name__ == "__main__":
    try:
        main()
    except OAuthError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # 网络、数据库等其它错误
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
