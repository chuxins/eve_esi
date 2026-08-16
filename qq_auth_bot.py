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

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from auth import OAuthError, build_authorization_url, exchange_code, verify
from esi_client import ESIClient
from eve_push import send_message
from main import get_access_token, get_db, load_config

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
    elif text == "查询" or text.startswith("查询 "):
        _on_query(user_id, text)


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


def _on_query(user_id, text):
    """处理「查询 [角色名]」指令。角色名为空则列出角色，否则查询当前 ISK 余额。"""
    name = text[len("查询"):].strip()
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
            send_message(f"未找到角色「{name}」。发送「查询」可查看已授权角色列表。", target_user=user_id)
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


# ---------------------------------------------------------------- OAuth 回调

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        error = query.get("error", [None])[0]
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]

        if error:
            self._respond(400, f"<h1>授权失败</h1><p>{error}</p>")
            return
        if not code or not state:
            self._respond(400, "<h1>缺少授权码或 state</h1>")
            return

        with _lock:
            entry = _pending.pop(state, None)
        if entry is None:
            self._respond(400, "<h1>授权会话已过期或无效</h1>")
            return
        verifier, created_at = entry
        if time.time() - created_at > STATE_TTL_SECONDS:
            self._respond(400, "<h1>授权会话已过期，请重新在 QQ 发送「添加账号」</h1>")
            return

        # 异步换取 token，避免阻塞回调响应
        threading.Thread(target=_finish_authorize, args=(code, verifier), daemon=True).start()
        self._respond(200, "<h1>授权成功！正在保存角色，可关闭此窗口。</h1>")

    def _respond(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


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

        push_cfg = config.get("push", {})
        target = push_cfg.get("target_user")
        if target:
            try:
                send_message(f"✅ 新账号已授权：{name} (ID:{cid})", target_user=target)
            except Exception as exc:
                print(f"[{_now()}] 授权成功但通知失败: {exc}")
    except OAuthError as exc:
        print(f"[{_now()}] 授权换取 token 失败: {exc}")
    except Exception as exc:
        print(f"[{_now()}] 授权处理失败: {exc}")


# ---------------------------------------------------------------- 主流程

def main():
    config = load_config()
    parsed = urlparse(config["callback_url"])
    callback_port = parsed.port or 8000
    callback_path = parsed.path or "/"

    event_server = HTTPServer((EVENT_HOST, EVENT_PORT), _EventHandler)
    callback_server = HTTPServer((CALLBACK_HOST, callback_port), _CallbackHandler)

    print(f"[{_now()}] 授权机器人已启动")
    print(f"[{_now()}] OneBot 事件接收: http://{EVENT_HOST}:{EVENT_PORT}")
    print(f"[{_now()}] OAuth 回调: http://{CALLBACK_HOST}:{callback_port}{callback_path}")

    threads = [
        threading.Thread(target=event_server.serve_forever, daemon=True),
        threading.Thread(target=callback_server.serve_forever, daemon=True),
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
