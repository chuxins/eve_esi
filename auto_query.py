"""定时自动查询脚本。

每 2 分钟查询一次所有已授权角色的钱包余额，并持久化到 MySQL
（wallet_balance 表），同时增量保存钱包流水（wallet_journal 表），
并根据最新数据重新生成 HTML 报告。

用法：
    python auto_query.py                    # 前台运行（Ctrl+C 停止）
    python auto_query.py --interval 120     # 自定义间隔（秒）
    nohup python auto_query.py > auto_query.log 2>&1 &   # 后台运行

停止后台任务：
    pkill -f auto_query.py
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

from esi_client import ESIClient
from main import get_access_token, get_db, load_config
from report import build_report

# 行缓冲：即使重定向到日志文件也逐行实时写入
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

DEFAULT_INTERVAL = 120  # 2 分钟
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# HTML 报告输出到可写目录（根文件系统曾只读，/var/www/report 不可写；
# 改到工作目录内，与 report.py 默认输出保持一致）
REPORT_PATH = os.path.join(BASE_DIR, "report.html")
PUSH_STATE_PATH = os.path.join(BASE_DIR, "push_state.json")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_DONATION_RE = re.compile(r"^(.+?) deposited cash into (.+?)'s account$")


def _is_player_donation(description):
    """判断流水是否为玩家捐赠（仅这类流水才自动推送）。

    数据库存 ESI 原文描述：玩家捐赠形如
    "X deposited cash into Y's account"（如
    "wangshuai deposited cash into chuxins1's account"），
    另有少见的固定文本 "Player donation"。
    """
    d = (description or "").strip()
    return bool(_DONATION_RE.match(d)) or d == "Player donation"


def _build_batch_message(entries, cname, db=None):
    """构建单批推送消息（含收入摘要）。

    当 db 可用时，会把“市场托管释放”替换为关联的市场交易详情。
    """
    from esi_client import translate_description

    lines = []
    income = 0.0
    for e in entries:
        d = str(e.get("journal_date"))[:16]
        amount = float(e.get("amount") or 0)
        desc = translate_description(e.get("description"))
        cid = e.get("character_id")
        ref_id = e.get("ref_id")

        market_lines = []
        if db is not None and cid and ref_id and desc == "市场托管释放":
            txns = db.get_wallet_transactions_by_journal_refs(cid, [ref_id])
            for txn in txns:
                name = txn.get("type_name") or (
                    f"物品#{txn.get('type_id')}" if txn.get("type_id") else "未知物品"
                )
                action = "买入" if txn.get("is_buy") else "卖出"
                qty = int(txn.get("quantity") or 0)
                unit = float(txn.get("unit_price") or 0)
                total = float(txn.get("total_price") or unit * qty)
                market_lines.append(
                    f"{d}  {amount:+,.0f}  📦 {name} {action} x{qty} "
                    f"单价 {unit:,.2f} 总额 {total:+,.2f}"
                )

        if market_lines:
            lines.extend(market_lines)
        else:
            lines.append(f"{d}  {amount:+,.0f}  {desc}")

        if amount >= 0:
            income += amount

    header = (
        f"📬 {cname} 收到玩家捐赠（{len(entries)} 笔）\n"
        f"📥 收入 {income:+,.0f} ISK\n"
        f"────────────────"
    )
    return header + "\n" + "\n".join(lines)


def _load_push_state():
    """读取按角色维护的推送游标。"""
    if os.path.exists(PUSH_STATE_PATH):
        try:
            with open(PUSH_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_push_state(state):
    with open(PUSH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _latest_ref_id(db, cid):
    """获取角色当前最新流水 ref_id（用于初始化推送游标）。"""
    rows = db.get_journal(cid, limit=1)
    if rows:
        return int(rows[0]["ref_id"])
    return None


def push_new_flow(config, db):
    """推送每个角色上次处理之后的新流水到 QQ（分批发送）。

    通过 push_state.json 按 character_id 记录最后处理的流水 ID；本次取出
    ref_id 大于该 ID 的全部流水分批推送。一次最多处理 50 条，其余下轮继续。

    自动推送只覆盖「玩家捐赠」（"X deposited cash into Y's account" /
    "Player donation"）：其它类型流水直接跳过，游标照常推进，不推送也不重拉。

    重要：只有消息真正送达（status=ok）后才推进 last_push_id；
    任一批次失败立即停止，该批及后续保留在状态文件之后，下轮自动重试。
    新角色首次出现时仅初始化游标到当前最新 ref_id，不推送历史流水。
    """
    push_cfg = config.get("push", {})
    target_user = push_cfg.get("target_user")
    target_group = push_cfg.get("target_group")
    if not target_user and not target_group:
        return  # 未配置推送目标

    chars = db.list_characters()
    if not chars:
        return

    from eve_push import send_message
    report_url = push_cfg.get("report_url", "")

    state = _load_push_state()
    per_char = state.setdefault("characters", {})
    changed = False
    batch_size = 8

    for c in chars:
        cid = c["character_id"]
        cname = c["character_name"]
        key = str(cid)

        # 新角色：初始化游标到当前最新 ref_id，避免一次性推送历史流水
        if key not in per_char:
            latest = _latest_ref_id(db, cid)
            per_char[key] = {"last_push_id": latest, "pushed_at": now_str()}
            changed = True
            continue

        last_id = per_char[key].get("last_push_id")
        pending = db.get_journal_after(cid, last_id, limit=50)
        if not pending:
            continue  # 无新流水

        # 只推送玩家捐赠；其它类型流水跳过（游标推进，标记已处理）
        donate = [e for e in pending if _is_player_donation(e.get("description"))]
        if not donate:
            per_char[key] = {"last_push_id": int(pending[-1]["ref_id"]), "pushed_at": now_str()}
            changed = True
            print(f"[{now_str()}] {cname} 本轮无玩家捐赠，{len(pending)} 条其它流水已跳过")
            continue

        ok_msgs = 0
        delivered_last = last_id  # 仅推进真正送达的流水 ID
        for i in range(0, len(donate), batch_size):
            batch = donate[i:i + batch_size]
            msg = _build_batch_message(batch, cname, db)
            if report_url and i + batch_size >= len(donate):
                msg = msg + f"\n📊 图表：{report_url}"
            try:
                resp = send_message(msg, target_user=target_user, target_group=target_group)
                if resp.get("status") == "ok":
                    ok_msgs += 1
                    delivered_last = max(int(r["ref_id"]) for r in batch)
                else:
                    print(f"[{now_str()}] {cname} 推送未成功: {str(resp.get('message', resp))[:200]}")
                    break
            except Exception as exc:
                print(f"[{now_str()}] {cname} 推送失败: {exc}")
                break

        last_donate_ref = int(donate[-1]["ref_id"])
        if delivered_last >= last_donate_ref:
            # 所有捐赠流水已送达：本批非捐赠流水一并标记已处理，游标推进到本批末尾
            per_char[key] = {"last_push_id": int(pending[-1]["ref_id"]), "pushed_at": now_str()}
            changed = True
            print(f"[{now_str()}] 已推送 {cname} 的 {ok_msgs} 批玩家捐赠至 QQ")
        elif delivered_last != last_id:
            per_char[key] = {"last_push_id": delivered_last, "pushed_at": now_str()}
            changed = True
            print(f"[{now_str()}] 已推送 {cname} 的 {ok_msgs} 批玩家捐赠至 QQ（部分失败，其余保留待重试）")
        else:
            print(f"[{now_str()}] {cname} 捐赠推送未成功：{len(donate)} 条保留待重试")

    if changed:
        _save_push_state(state)


def regenerate_report(config, db, limit=200):
    """根据最新数据重新生成 HTML 报告。

    先写入本地可写目录（REPORT_PATH），再尽力复制一份到 nginx 公网目录
    （/var/www/report/report.html）。复制失败仅提示、不影响主流程：
    待平台恢复根文件系统可写后，公网报告将自动恢复更新。
    """
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        content = build_report(config, db, None, limit)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[{now_str()}] HTML 报告已自动更新：{REPORT_PATH}")
    except Exception as exc:
        print(f"[{now_str()}] 报告生成失败：{exc}")
        return

    # 复制层：同步到 nginx 公网目录（尽力而为）
    try:
        web_report = "/var/www/report/report.html"
        with open(web_report, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[{now_str()}] 已同步公网报告：{web_report}")
    except Exception as exc:
        print(f"[{now_str()}] 公网报告同步跳过（可写恢复后自动生效）：{str(exc)[:80]}")


def query_once(config, db):
    """查询所有已授权角色的余额与流水并入库。"""
    chars = db.list_characters()
    if not chars:
        print(f"[{now_str()}] 数据库中没有已授权角色，跳过。")
        return

    for c in chars:
        if not c.get("has_token"):
            print(f"[{now_str()}] {c['character_name']}: 无 token，跳过。")
            continue
        cid = c["character_id"]
        access_token = get_access_token(config, db, cid)
        if not access_token:
            print(f"[{now_str()}] {c['character_name']}: 无法获取有效 token，跳过。")
            continue

        client = ESIClient(access_token, config["user_agent"])
        try:
            # 余额
            balance = client.get_wallet_balance(cid)
            db.record_balance(cid, balance)

            # 增量流水：只向 ESI 索取比本地最大 ref_id 更新的记录
            # （首次同步 last_ref_id 为 None 时才全量翻页抓取历史）
            last_ref_id = db.get_max_journal_ref_id(cid)
            entries = client.sync_wallet_journal(
                cid, since_ref_id=last_ref_id, max_pages=20
            )
            if entries:
                db.upsert_journal(cid, entries)

            print(
                f"[{now_str()}] {c['character_name']} (ID:{cid}): "
                f"余额 {balance:,.2f} ISK，本次新增流水 {len(entries)} 条"
            )
        except Exception as exc:  # 单角色失败不影响其它角色
            print(f"[{now_str()}] 查询 {c['character_name']} 失败: {exc}")

        # 市场交易详情（单独容错，不因交易接口失败影响余额/流水）
        try:
            from market import sync_market_transactions
            tx_entries = sync_market_transactions(db, client, cid)
            print(
                f"[{now_str()}] {c['character_name']} 市场交易：本次同步 {len(tx_entries)} 条"
            )
        except Exception as exc:
            print(f"[{now_str()}] {c['character_name']} 市场交易同步失败: {exc}")


def main():
    parser = argparse.ArgumentParser(description="EVE 钱包定时自动查询（默认每 2 分钟）")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"查询间隔秒数（默认 {DEFAULT_INTERVAL}）",
    )
    args = parser.parse_args()

    if args.interval < 10:
        print("间隔过短（<10 秒），已重置为默认 120 秒。")
        args.interval = DEFAULT_INTERVAL

    config = load_config()
    db = get_db(config)

    print(f"[{now_str()}] 定时查询已启动：每 {args.interval} 秒执行一次（Ctrl+C 停止）")
    print(f"[{now_str()}] 已授权角色：{', '.join(c['character_name'] for c in db.list_characters()) or '无'}")

    while True:
        try:
            query_once(config, db)
            push_new_flow(config, db)
            regenerate_report(config, db)
        except KeyboardInterrupt:
            print(f"\n[{now_str()}] 已停止。")
            break
        except Exception as exc:
            print(f"[{now_str()}] 查询循环异常: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)
