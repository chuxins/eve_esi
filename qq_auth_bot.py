"""EVE 授权机器人。

功能：
1. 接收 SnowLuma OneBot HTTP 上报的 QQ 消息事件（默认 127.0.0.1:8888）。
2. 收到「添加账号」指令时，生成 EVE ESI 授权链接并私聊回复。
3. 常驻 OAuth 回调服务器（从 config.callback_url 解析端口），处理 EVE 授权回调，
   自动换取 token 并将新角色写入数据库（支持多账号）。

运行：
    nohup python3 qq_auth_bot.py > qq_auth_bot.log 2>&1 &

停止：
    pkill -f qq_auth_bot.py
"""

import base64
import http.client
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from auth import OAuthError, build_authorization_url, exchange_code, verify
from esi_client import ESIClient, translate_description
from eve_push import send_image_message, send_message
from fittings import (FittingError, FittingScopeError, collect_type_ids,
                      fetch_fittings, format_candidates, format_detail,
                      format_list, resolve_fitting)
from main import get_access_token, get_db, load_config
from market_price import (MAX_BATCH_ITEMS, PRICE_CACHE_TTL, format_batch_message,
                          format_price_message, get_price_table,
                          parse_batch_query, parse_item_query, price_cache_age,
                          query_batch, query_item, refresh_price_cache)
from plot_balance import plot_balance

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENT_HOST = "127.0.0.1"
EVENT_PORT = 8888
CALLBACK_HOST = "0.0.0.0"
STATE_TTL_SECONDS = 600  # 授权链接 10 分钟内有效

# state → (code_verifier, created_at)
_pending = {}
_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_text(event):
    """从 OneBot 事件中提取纯文本消息。兼容 string / array 两种格式。"""
    raw = event.get("raw_message")
    if raw is not None:
        return str(raw).strip()
    msg = event.get("message")
    if isinstance(msg, list):
        parts = [
            str(seg.get("data", {}).get("text", ""))
            for seg in msg
            if seg.get("type") == "text"
        ]
        return "".join(parts).strip()
    return str(msg or "").strip()


# ---------------------------------------------------------------- OneBot 事件

class _EventHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self.send_response(204)
        self.end_headers()
        if not body:
            return
        try:
            event = json.loads(body)
        except Exception:
            return
        threading.Thread(target=handle_event, args=(event,), daemon=True).start()

    def log_message(self, *args):
        pass


# 指令与参数之间允许的分隔符（用户常打「查价：三钛合金」「流水：chuxins1」）
_CMD_SEPARATORS = " \t：:，,、;；。\r\n\u3000"


def _is_command(text, command):
    """判断 text 是否为 command 指令（允许其后直接跟参数或任意分隔符）。

    避免「余额宝」这类同前缀词被误判为指令。
    """
    if text == command:
        return True
    if not text.startswith(command):
        return False
    return text[len(command)] in _CMD_SEPARATORS


def _strip_arg(text):
    """取出指令后的参数，去掉前置分隔符与空白。"""
    return str(text or "").lstrip(_CMD_SEPARATORS).strip()


def handle_event(event):
    if event.get("post_type") != "message":
        return
    if event.get("message_type") != "private":
        return  # 仅处理私聊
    user_id = event.get("user_id")
    text = _extract_text(event)
    print(f"[{_now()}] 收到私聊 {user_id}: {text!r}")

    if text == "添加账号":
        _on_add_account(user_id)
    elif text in ("账号列表", "查看账号", "账号"):
        _on_list_accounts(user_id)
    elif text.isdigit():
        # 纯数字：按上次「装配」列表的序号看详情（无上下文则静默忽略）
        _on_fitting_index(user_id, text, quiet=True)
    elif _is_command(text, "批量查价"):
        _on_batch_price(user_id, text)
    elif _is_command(text, "查价"):
        _on_price(user_id, text)
    elif _is_command(text, "余额"):
        _on_balance(user_id, text)
    elif _is_command(text, "流水"):
        _on_journal(user_id, text)
    elif _is_command(text, "装配"):
        _on_fittings(user_id, text)
    elif _is_command(text, "图表"):
        _on_chart(user_id, text)
    elif _is_command(text, "菜单"):
        _on_menu(user_id)


def _on_menu(user_id):
    """处理「菜单」指令：列出全部可用指令与使用格式。"""
    lines = [
        "📖 可用指令",
        "────────────────",
        "余额 <角色名>  查询当前 ISK 余额",
        "流水 <角色名>  查询最近 10 条钱包流水",
        "装配 <角色名>  查询该角色已保存的装配方案",
        "查价 <物品名>  查询 Jita 行情并推送走势图",
        "批量查价       每行一个物品，输出三项总计",
        "图表 <角色名>  生成并推送余额图表",
        "账号列表       列出所有已授权角色",
        "添加账号       获取 EVE 账号授权链接",
        "菜单           显示本菜单",
        "────────────────",
        "• 角色名可填「角色名」或「角色 ID」",
        "• 「余额」不带角色名时会列出全部角色",
        "• 「查价」可带数量：查价 三钛合金*1000 → 同时给出总价",
        "• 「查价」名称支持模糊：查价 三钛 → 自动匹配到 三钛合金",
        "• 「装配」查角色已保存的装配：装配 chuxins1 [序号/舰船名]",
        "• 多项查价：批量查价（每行一个 物品名称*数量，输出三项总计）",
        "• 「查价」物品名支持中文或英文（如：三钛合金 / Tritanium）",
    ]
    try:
        chars = get_db(load_config()).list_characters()
        names = "、".join(c["character_name"] for c in chars)
        if names:
            lines.append(f"• 当前可用角色：{names}")
    except Exception as exc:
        print(f"[{_now()}] 菜单读取角色列表失败: {exc}")
    lines.append("示例：余额 chuxins1 ｜ 流水 chuxins1 ｜ 图表 chuxins1")

    try:
        send_message("\n".join(lines), target_user=user_id)
        print(f"[{_now()}] 已发送菜单给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送菜单失败: {exc}")


def _on_add_account(user_id):
    config = load_config()
    auth_url, state, verifier = build_authorization_url(
        client_id=config["client_id"],
        callback_url=config["callback_url"],
        scope=config["scope"],
    )
    with _lock:
        _pending[state] = (verifier, time.time())
    msg = (
        "🔗 EVE 账号授权链接（10 分钟内有效）：\n"
        f"{auth_url}\n\n"
        "请用浏览器打开，登录 EVE 账号并选择要授权的角色，完成后会自动通知。"
    )
    try:
        send_message(msg, target_user=user_id)
        print(f"[{_now()}] 已发送授权链接给 {user_id}，state={state[:8]}...")
    except Exception as exc:
        print(f"[{_now()}] 发送授权链接失败: {exc}")


def _on_list_accounts(user_id):
    db = get_db(load_config())
    chars = db.list_characters()
    if not chars:
        send_message("暂无已授权角色。", target_user=user_id)
        return
    lines = ["📋 已授权角色："]
    for c in chars:
        state = "✅" if c.get("has_token") else "❌ 无 token"
        lines.append(f"{state} {c['character_name']} (ID:{c['character_id']})")
    try:
        send_message("\n".join(lines), target_user=user_id)
    except Exception as exc:
        print(f"[{_now()}] 发送账号列表失败: {exc}")


def _on_balance(user_id, text):
    """处理「余额 [角色名]」指令。角色名为空则列出角色，否则查询当前 ISK 余额。"""
    name = _strip_arg(text[len("余额"):])
    if not name:
        _on_list_accounts(user_id)
        return

    config = load_config()
    db = get_db(config)
    chars = db.list_characters()
    matched = [
        c for c in chars
        if str(c["character_id"]) == name or c["character_name"] == name
    ]
    if not matched:
        try:
            send_message(f"未找到角色「{name}」。发送「余额」可查看已授权角色列表。", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送查询结果失败: {exc}")
        return

    character = matched[0]
    balance, source = _query_balance(config, db, character)
    if balance is None:
        try:
            send_message(
                f"❌ 查询 {character['character_name']} 余额失败（ESI 与本地快照均无数据）。",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送查询结果失败: {exc}")
        return

    msg = f"💰 {character['character_name']} 当前余额：\n{balance:,.2f} ISK\n（{source}）"
    try:
        send_message(msg, target_user=user_id)
    except Exception as exc:
        print(f"[{_now()}] 发送查询结果失败: {exc}")


def _query_balance(config, db, character):
    """查询角色当前 ISK 余额。优先实时 ESI，失败回退数据库快照。返回 (balance, source)。"""
    cid = character["character_id"]
    # 优先实时 ESI
    try:
        token = get_access_token(config, db, cid)
        if token:
            client = ESIClient(token, config["user_agent"])
            balance = client.get_wallet_balance(cid)
            db.record_balance(cid, balance)
            return float(balance), "ESI 实时"
    except Exception as exc:
        print(f"[{_now()}] ESI 余额查询失败（{character['character_name']}）: {exc}")
    # 回退数据库快照
    try:
        hist = db.get_balance_history(cid, limit=1)
        if hist:
            return float(hist[0]["balance"]), "数据库快照"
    except Exception as exc:
        print(f"[{_now()}] 数据库快照读取失败: {exc}")
    return None, None


JOURNAL_LIMIT = 10  # 「流水」指令展示的条数


def _on_journal(user_id, text):
    """处理「流水 <角色名>」指令：列出该角色最近 10 条钱包流水。

    数据取自数据库（auto_query 每 2 分钟同步），不实时请求 ESI，
    保证秒回且不消耗接口配额。
    """
    name = _strip_arg(text[len("流水"):])
    if not name:
        try:
            send_message("请使用格式：流水 <角色名>", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送流水格式提示失败: {exc}")
        return

    db = get_db(load_config())
    chars = db.list_characters()
    matched = [
        c for c in chars
        if str(c["character_id"]) == name or c["character_name"] == name
    ]
    if not matched:
        try:
            send_message(f"未找到角色「{name}」。发送「余额」可查看已授权角色列表。", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送未找到角色失败: {exc}")
        return

    character = matched[0]
    rows = db.get_journal(character["character_id"], limit=JOURNAL_LIMIT)
    if not rows:
        try:
            send_message(f"📒 {character['character_name']} 暂无流水记录。", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送流水失败: {exc}")
        return

    lines = [f"📒 {character['character_name']} 最新 {len(rows)} 条流水", "────────────────"]
    income = 0.0
    expense = 0.0
    for r in rows:
        raw_date = r.get("journal_date")
        date = str(raw_date)[5:16] if raw_date else "-"  # MM-DD HH:MM
        amount = float(r.get("amount") or 0)
        if amount >= 0:
            income += amount
        else:
            expense += amount
        desc = (translate_description(r.get("description")) or "无描述").replace("\n", " ")
        if len(desc) > 28:
            desc = desc[:28] + "…"
        lines.append(f"{date}  {amount:+,.0f}  {desc}")
    lines.append("────────────────")
    lines.append(f"📥 收入 {income:+,.0f} ｜ 📤 支出 {expense:+,.0f} ISK")

    try:
        send_message("\n".join(lines), target_user=user_id)
        print(f"[{_now()}] 已发送 {character['character_name']} 最新流水给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送流水失败: {exc}")


# 记录每个用户最近一次「装配」列表，便于直接用序号查看详情
_fitting_context = {}
_fitting_lock = threading.Lock()
FITTING_CONTEXT_TTL = 600  # 秒


def _reply(user_id, message):
    """发送私聊消息（失败只记日志，不抛出）。"""
    try:
        send_message(message, target_user=user_id)
        return True
    except Exception as exc:
        print(f"[{_now()}] 发送消息失败: {exc}")
        return False


def _remember_fittings(user_id, character, fittings):
    """记住本次列出的装配列表，供用户直接用序号查看详情。"""
    with _fitting_lock:
        if len(_fitting_context) > 200:  # 简单清理，避免长期占用
            _fitting_context.clear()
        _fitting_context[user_id] = {
            "character": character,
            "fittings": fittings,
            "ts": time.time(),
        }


def _load_fittings_context(user_id):
    """取出未过期的装配列表上下文；无则返回 None。"""
    with _fitting_lock:
        context = _fitting_context.get(user_id)
    if not context or time.time() - context["ts"] > FITTING_CONTEXT_TTL:
        return None
    return context


def _on_fitting_index(user_id, index_text, quiet=False):
    """按序号查看上次列出的装配详情（无需再带角色名）。

    quiet=True 时（纯数字消息、且无上下文）不作任何回复。
    """
    context = _load_fittings_context(user_id)
    if not context:
        if not quiet:
            _reply(user_id, "请先发送「装配 <角色名>」查看装配列表，再用序号查看详情。")
        return

    fittings = context["fittings"]
    character = context["character"]
    index = int(index_text)
    if not 1 <= index <= len(fittings):
        _reply(user_id, f"序号超出范围（1~{len(fittings)}）。"
                       f"发送「装配 {character['character_name']}」重新查看列表。")
        return

    fitting = fittings[index - 1]
    db = get_db(load_config())
    names = db.get_item_type_names(collect_type_ids([fitting]))
    if not _reply(user_id, format_detail(fitting, names, get_price_table())):
        return
    print(f"[{_now()}] 已发送 {character['character_name']} 第 {index} 套装配详情给 {user_id}")


def _on_fittings(user_id, text):
    """处理「装配 <角色名> [序号|关键词]」指令：查看角色已保存的装配方案。

    注意：ESI 只能读取**个人**装配；军团共享装配无接口，无法获取。
    """
    arg = _strip_arg(text[len("装配"):])
    if not arg:
        try:
            send_message(
                "请使用格式：装配 <角色名> [序号或舰船名]\n"
                "如：装配 chuxins1 ｜ 装配 chuxins1 3 ｜ 装配 chuxins1 狂暴（按舰船名）\n"
                "列表发出后，直接回复序号即可看详情",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送装配格式提示失败: {exc}")
        return

    parts = arg.split(maxsplit=1)
    char_arg = parts[0]
    selector = parts[1].strip() if len(parts) > 1 else None

    config = load_config()
    db = get_db(config)
    matched = [
        c for c in db.list_characters()
        if str(c["character_id"]) == char_arg or c["character_name"] == char_arg
    ]
    # 「装配 <序号>」（不是角色）→ 用上次列出的列表查看详情
    if not matched and selector is None and char_arg.isdigit():
        _on_fitting_index(user_id, char_arg)
        return
    if not matched:
        try:
            send_message(
                f"未找到角色「{char_arg}」。发送「余额」可查看已授权角色列表。",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送未找到角色失败: {exc}")
        return

    character = matched[0]
    cid = character["character_id"]
    cname = character["character_name"]

    try:
        fittings = fetch_fittings(db, config, cid)
    except FittingScopeError:
        try:
            send_message(
                f"🔒 {cname} 尚未授予装配权限。\n"
                "请发送「添加账号」重新授权该角色；\n"
                "若授权后仍提示此项，请先在 EVE 账号设置里撤销本应用的授权，再重新授权。",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送装配权限提示失败: {exc}")
        return
    except FittingError as exc:
        try:
            send_message(f"❌ 查询 {cname} 装配失败：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送装配失败提示失败: {send_exc}")
        return
    except Exception as exc:
        print(f"[{_now()}] 查询 {cname} 装配出错: {exc}")
        try:
            send_message(f"❌ 查询 {cname} 装配出错：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送装配出错提示失败: {send_exc}")
        return

    if not fittings:
        try:
            send_message(
                f"🛠 {cname} 没有保存过装配方案。\n"
                "（ESI 仅能读取个人装配，军团共享装配无法获取）",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送无装配提示失败: {exc}")
        return

    names = db.get_item_type_names(collect_type_ids(fittings))

    if not selector:
        message = format_list(fittings, cname, names)
        _remember_fittings(user_id, character, fittings)
    else:
        fitting, candidates, mode = resolve_fitting(fittings, selector, names)
        if fitting is not None:
            message = format_detail(fitting, names, get_price_table())
        elif candidates:
            message = format_candidates(fittings, candidates, names, mode)
            _remember_fittings(user_id, character, fittings)
        else:
            message = f"未找到匹配「{selector}」的装配（{cname} 共 {len(fittings)} 套）"

    try:
        send_message(message, target_user=user_id)
        print(f"[{_now()}] 已发送 {cname} 装配信息给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送装配信息失败: {exc}")


def _on_price(user_id, text):
    """处理「查价 <物品名称>[*数量]」指令：查询 Jita 行情并推送走势图。"""
    raw = _strip_arg(text[len("查价"):])
    if not raw:
        try:
            send_message("请使用格式：查价 <物品名称>*<数量>（数量可省略，「物品*」按 1 计算）", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送查价格式提示失败: {exc}")
        return

    try:
        name, quantity = parse_item_query(raw)
    except ValueError as exc:
        try:
            send_message(
                f"格式有误：{exc}\n用法：查价 <物品名称>*<数量>（如：查价 三钛合金*1000）",
                target_user=user_id,
            )
        except Exception as send_exc:
            print(f"[{_now()}] 发送查价格式提示失败: {send_exc}")
        return

    config = load_config()
    db = get_db(config)
    # 按物品名分文件，避免不同物品的图表相互覆盖
    safe = "".join(ch if ch.isalnum() else "_" for ch in name)[:32]
    output = os.path.join(BASE_DIR, f"price_chart_{safe}.png")

    try:
        result = query_item(
            name, db=db, output_path=output,
            user_agent=config["user_agent"], quantity=quantity,
        )
    except Exception as exc:
        print(f"[{_now()}] 查价失败（{name}）: {exc}")
        try:
            send_message(f"❌ 查询「{name}」价格失败：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送查价失败提示失败: {send_exc}")
        return

    if not result["ok"]:
        tip = (
            "查无此物"
            if result["reason"] == "not_found"
            else f"📉 {result['name']} 暂无 Jita 行情数据（无挂单也无历史）。"
        )
        try:
            send_message(tip, target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送查价结果失败: {exc}")
        return

    message = format_price_message(result)
    try:
        if result.get("chart"):
            send_image_message(result["chart"], target_user=user_id, text=message)
        else:
            send_message(message, target_user=user_id)
        print(f"[{_now()}] 已发送「{result['name']}」行情给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送行情失败（{name}）: {exc}")
        try:  # 图片发送失败时至少把文字行情发出去
            send_message(message, target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送行情文本也失败: {send_exc}")


def _on_batch_price(user_id, text):
    """处理「批量查价」指令：每行一个「物品名称*数量」，输出 Jita 4-4 三项总计。

    只有一行物品时直接复用「查价」指令（含走势图）。
    """
    body = _strip_arg(text[len("批量查价"):])
    if not body:
        try:
            send_message(
                "请使用格式：批量查价 <物品名称>*<数量>\n每行一个物品，如：\n"
                "批量查价 三钛合金*1000\n伊甸币*10\n"
                "（「物品*」数量留空时按 1 计算）",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送批量查价格式提示失败: {exc}")
        return

    items, errors = parse_batch_query(body)
    if not items and not errors:
        return

    # 只有一行且格式无误：与「查价」指令完全相同（含走势图）
    if len(items) == 1 and not errors:
        name, quantity = items[0]
        _on_price(user_id, f"查价 {name}" + (f"*{quantity}" if quantity else ""))
        return

    if len(items) + len(errors) > MAX_BATCH_ITEMS:
        try:
            send_message(
                f"一次最多查询 {MAX_BATCH_ITEMS} 项（当前 {len(items) + len(errors)} 项），请分批发送。",
                target_user=user_id,
            )
        except Exception as exc:
            print(f"[{_now()}] 发送批量查价超限提示失败: {exc}")
        return

    config = load_config()
    db = get_db(config)
    try:
        results = query_batch(items, db=db, user_agent=config["user_agent"])
    except Exception as exc:
        print(f"[{_now()}] 批量查价失败: {exc}")
        try:
            send_message(f"❌ 批量查价失败：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送批量查价失败提示失败: {send_exc}")
        return

    if not any(r.get("ok") for r in results):
        names = "、".join(str(r.get("query") or r.get("name") or "?") for r in results)
        message = f"查无此物：{names}"
        if errors:
            message += f"\n⚠️ 另有 {len(errors)} 行格式错误"
    else:
        message = format_batch_message(results, errors)

    try:
        send_message(message, target_user=user_id)
        print(f"[{_now()}] 已发送批量查价结果（{len(results)} 项）给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送批量查价结果失败: {exc}")


def _on_chart(user_id, text):
    """处理「图表 <角色名>」指令：生成对应角色的余额图表并推送图片。"""
    name = _strip_arg(text[len("图表"):])
    if not name:
        try:
            send_message("请使用格式：图表 <角色名>", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送图表格式提示失败: {exc}")
        return

    config = load_config()
    db = get_db(config)
    chars = db.list_characters()
    matched = [
        c for c in chars
        if str(c["character_id"]) == name or c["character_name"] == name
    ]
    if not matched:
        try:
            send_message(f"未找到角色「{name}」。发送「余额」可查看已授权角色列表。", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送未找到角色失败: {exc}")
        return

    character = matched[0]
    output = os.path.join(BASE_DIR, f"chart_{character['character_id']}.png")

    # 优先从 ESI 获取当前余额，失败时回退数据库快照
    current_balance, balance_source = _query_balance(config, db, character)
    try:
        plot_balance(
            config,
            char_arg=name,
            limit=200,
            output=output,
            current_balance=current_balance,
            balance_source=balance_source or "ESI",
        )
    except SystemExit:
        try:
            send_message(f"❌ 角色「{name}」暂无余额历史数据，请稍后再试。", target_user=user_id)
        except Exception as exc:
            print(f"[{_now()}] 发送无数据提示失败: {exc}")
        return
    except Exception as exc:
        print(f"[{_now()}] 图表生成失败（{name}）: {exc}")
        try:
            send_message(f"❌ 图表生成失败：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送图表失败提示失败: {send_exc}")
        return

    try:
        send_image_message(
            output,
            target_user=user_id,
            text=f"📊 {character['character_name']} 余额图表",
        )
        print(f"[{_now()}] 已发送 {character['character_name']} 图表给 {user_id}")
    except Exception as exc:
        print(f"[{_now()}] 发送图表失败（{name}）: {exc}")
        try:
            send_message(f"❌ 图表已生成但发送失败：{exc}", target_user=user_id)
        except Exception as send_exc:
            print(f"[{_now()}] 发送失败提示失败: {send_exc}")


# ---------------------------------------------------------------- OAuth 回调

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        error = query.get("error", [None])[0]
        detail = query.get("error_description", [None])[0]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]

        # 记录每一次回调，便于排查「授权后没反应」这类问题
        print(
            f"[{_now()}] OAuth 回调: path={self.path[:100]!r} "
            f"error={error} desc={detail} "
            f"code={'有' if code else '无'} "
            f"state={(state[:8] + '…') if state else '无'}"
        )

        if error:
            print(f"[{_now()}] ⚠️ 授权失败：{error} {detail or ''}")
            self._respond(400, f"<h1>授权失败</h1><p>{error}</p><p>{detail or ''}</p>")
            return
        if not code or not state:
            print(f"[{_now()}] 回调缺少 code/state（多为扫描或误访问）")
            self._respond(400, "<h1>缺少授权码或 state</h1>")
            return

        with _lock:
            entry = _pending.pop(state, None)
        if entry is None:
            # 非 QQ 机器人发起的 state：转发给 Eve-PI Web 的 SSO 回调处理
            print(f"[{_now()}] state 未匹配（非本机器人发起），转发给 Eve-PI")
            self._forward_to_evepi(code, state)
            return
        verifier, created_at = entry
        if time.time() - created_at > STATE_TTL_SECONDS:
            print(f"[{_now()}] state 已过期（超过 {STATE_TTL_SECONDS}s）")
            self._respond(400, "<h1>授权会话已过期，请重新在 QQ 发送「添加账号」</h1>")
            return

        # 异步换取 token，避免阻塞回调响应
        threading.Thread(target=_finish_authorize, args=(code, verifier), daemon=True).start()
        self._respond(200, "<h1>授权成功！正在保存角色，可关闭此窗口。</h1>")

    def _forward_to_evepi(self, code, state):
        target = f"/api/auth/callback?code={quote(code)}&state={quote(state)}"
        try:
            conn = http.client.HTTPConnection("127.0.0.1", 8001, timeout=120)
            conn.request("GET", target)
            resp = conn.getresponse()
            body = resp.read()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("location", "set-cookie", "content-type"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            conn.close()
        except Exception as exc:
            self._respond(502, f"<h1>Eve-PI 回调转发失败</h1><p>{exc}</p>")

    def _respond(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _granted_scopes(access_token):
    """从 access_token（JWT）中解析 EVE 实际授予的 scope 列表。

    ESI 的 token 响应不带 scope 字段，只有 JWT 的 scp 声明是权威的。
    """
    try:
        payload = str(access_token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return list(claims.get("scp") or [])
    except Exception:
        return []


def _finish_authorize(code, verifier):
    config = load_config()
    db = get_db(config)
    try:
        token = exchange_code(
            code=code,
            verifier=verifier,
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            callback_url=config["callback_url"],
            user_agent=config["user_agent"],
        )
        char = verify(token["access_token"], config["user_agent"])
        cid = int(char["CharacterID"])
        name = char.get("CharacterName", str(cid))
        db.upsert_character(cid, name, config["scope"])
        db.upsert_token(cid, token)
        print(f"[{_now()}] ✅ 已授权角色：{name} (ID:{cid})")

        # 校验 EVE 实际授予的权限：SSO 可能复用旧同意记录而漏发新增的 scope
        granted = set(_granted_scopes(token.get("access_token")))
        missing = sorted(set(config.get("scopes") or []) - granted)
        if missing:
            print(f"[{_now()}] ⚠️ 授权缺少 scope：{missing}")
        elif granted:
            print(f"[{_now()}] 已授予 scope：{sorted(granted)}")

        push_cfg = config.get("push", {})
        target = push_cfg.get("target_user")
        if target:
            msg = f"✅ 新账号已授权：{name} (ID:{cid})"
            if missing:
                msg += (
                    "\n⚠️ 但 EVE 未授予以下权限：" + "、".join(missing)
                    + "\n请在 EVE 账号设置里「撤销本应用的授权」后，再重新发送「添加账号」"
                    "（EVE SSO 会复用旧同意记录，导致新增权限不生效）"
                )
            try:
                send_message(msg, target_user=target)
            except Exception as exc:
                print(f"[{_now()}] 授权成功但通知失败: {exc}")
    except OAuthError as exc:
        print(f"[{_now()}] 授权换取 token 失败: {exc}")
    except Exception as exc:
        print(f"[{_now()}] 授权处理失败: {exc}")


# ---------------------------------------------------------------- 主流程

def _price_cache_worker():
    """后台线程：按需刷新 ESI 全局参考价缓存（供 PLEX 这类无挂单物品用）。

    该端点单次约 5~25 秒、约 1MB，所以只在后台做，不阻塞指令处理。
    """
    while True:
        try:
            age = price_cache_age()
            if age is None or age > PRICE_CACHE_TTL:
                refresh_price_cache(quiet=True)
                print(f"[{_now()}] 已刷新 ESI 全局参考价缓存")
        except Exception as exc:
            print(f"[{_now()}] 刷新全局参考价失败: {exc}")
        time.sleep(3600)  # 每小时检查一次，实际每 24 小时刷新


def main():
    config = load_config()
    parsed = urlparse(config["callback_url"])
    callback_port = parsed.port or 8000
    callback_path = parsed.path or "/"

    event_server = ThreadingHTTPServer((EVENT_HOST, EVENT_PORT), _EventHandler)
    callback_server = ThreadingHTTPServer((CALLBACK_HOST, callback_port), _CallbackHandler)
    event_server.daemon_threads = True
    callback_server.daemon_threads = True

    print(f"[{_now()}] 授权机器人已启动")
    print(f"[{_now()}] OneBot 事件接收: http://{EVENT_HOST}:{EVENT_PORT}")
    print(f"[{_now()}] OAuth 回调: http://{CALLBACK_HOST}:{callback_port}{callback_path}")

    threads = [
        threading.Thread(target=event_server.serve_forever, daemon=True),
        threading.Thread(target=callback_server.serve_forever, daemon=True),
        threading.Thread(target=_price_cache_worker, daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        event_server.shutdown()
        callback_server.shutdown()


if __name__ == "__main__":
    main()
