"""EVE Online SSO 授权模块（授权码 + PKCE 模式）。

负责：
- 生成 PKCE 挑战对（code_verifier / code_challenge）
- 启动本地回调 HTTP 服务器接收授权码
- 使用授权码换取 access_token / refresh_token
- 使用 refresh_token 刷新 access_token
- 通过 /oauth/verify 获取角色信息

说明：token 的持久化交由 db.py（MySQL）管理，本模块保持纯 OAuth 逻辑。
"""

import base64
import hashlib
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
VERIFY_URL = "https://login.eveonline.com/oauth/verify"

DEFAULT_SCOPE = "esi-wallet.read_character_wallet.v1"


class OAuthError(RuntimeError):
    """OAuth 流程相关的错误。"""


# ---------------------------------------------------------------- PKCE

def _pkce_pair():
    """生成 (code_verifier, code_challenge) 对。"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _basic_auth(client_id, client_secret):
    """构造 EVE SSO 要求的 Basic Auth 头（client_id:secret）。"""
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.urlsafe_b64encode(raw).decode("ascii")


# ---------------------------------------------------------------- 回调服务器

class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    error = None

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            _CallbackHandler.auth_code = query["code"][0]
            body = "<h1>登录成功！可以关闭此窗口并返回终端。</h1>".encode("utf-8")
            self.send_response(200)
        elif "error" in query:
            _CallbackHandler.error = query["error"][0]
            body = ("<h1>授权失败：" + query["error"][0] + "</h1>").encode("utf-8")
            self.send_response(400)
        else:
            body = "<h1>未找到授权码，请重新运行脚本。</h1>".encode("utf-8")
            self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # 静默访问日志
        pass


# ---------------------------------------------------------------- 授权流程

def authorize_flow(client_id, client_secret, callback_url, scope, user_agent):
    """启动本地回调服务器，完成 OAuth 授权，返回 token 字典。

    返回的 token 字典包含：access_token、expires_in、token_type、
    refresh_token，以及我们附加的 expires_at（epoch 秒）。
    """
    parsed = urlparse(callback_url)
    if parsed.hostname is None or parsed.port is None:
        raise OAuthError("callback_url 必须包含主机名和端口，例如 http://localhost:8080/callback/")

    verifier, challenge = _pkce_pair()
    # 绑定到 0.0.0.0 以监听所有接口。EVE 回调通过 NAT 转发到公网 IP:port，
    # 若绑定具体公网 IP 会报 "Cannot assign requested address"。
    server = HTTPServer(("0.0.0.0", parsed.port), _CallbackHandler)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "scope": scope,
        "state": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"
    print("=" * 60)
    print("请在浏览器中完成 EVE 账号登录与角色授权：")
    print(auth_url)
    print("=" * 60)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # 等待浏览器回调（回调服务器单线程轮询）
    while _CallbackHandler.auth_code is None and _CallbackHandler.error is None:
        server.handle_request()
    server.server_close()

    if _CallbackHandler.error:
        raise OAuthError(f"授权失败：{_CallbackHandler.error}")

    headers = {
        "Authorization": _basic_auth(client_id, client_secret),
        "User-Agent": user_agent,
    }
    data = {
        "grant_type": "authorization_code",
        "code": _CallbackHandler.auth_code,
        "redirect_uri": callback_url,
        "code_verifier": verifier,
    }
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise OAuthError(f"换取 token 失败：HTTP {resp.status_code} - {resp.text}")

    token = resp.json()
    token["expires_at"] = _expires_at(token)
    return token


def refresh_access_token(refresh_token, client_id, client_secret, user_agent):
    """使用 refresh_token 换取新的 access_token，返回新 token 字典。"""
    headers = {
        "Authorization": _basic_auth(client_id, client_secret),
        "User-Agent": user_agent,
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise OAuthError(f"刷新 token 失败：HTTP {resp.status_code} - {resp.text}")

    token = resp.json()
    token["expires_at"] = _expires_at(token)
    # 刷新响应通常不再返回 refresh_token，若返回则覆盖
    if "refresh_token" in token and token["refresh_token"]:
        token["refresh_token"] = token["refresh_token"]
    else:
        # 保留旧 refresh_token（EVE 的 refresh token 长期有效）
        token["refresh_token"] = refresh_token
    return token


def _expires_at(token):
    """根据 expires_in 计算过期时间（epoch 秒）。"""
    import time
    return int(time.time()) + int(token.get("expires_in", 1200))


# ---------------------------------------------------------------- 授权链接（供机器人/网页使用）

def build_authorization_url(client_id, callback_url, scope):
    """生成 ESI 授权链接，返回 (auth_url, state, code_verifier)。

    用于机器人等场景：先把链接发给用户，待用户在浏览器完成授权后，
    回调会携带 code 与 state 回来，再用 exchange_code 换取 token。
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"
    return auth_url, state, verifier


def exchange_code(code, verifier, client_id, client_secret, callback_url, user_agent):
    """用授权码 code 换取 token 字典（含 expires_at）。

    授权码单次有效：网络类错误（连接/读超时）会重试（此时码可能尚未被消费），
    4xx（码已失效/被消费）不重试、直接报错，5xx 服务端错误可重试。
    """
    import time as _time
    headers = {
        "Authorization": _basic_auth(client_id, client_secret),
        "User-Agent": user_agent,
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "code_verifier": verifier,
    }
    for attempt in range(3):
        try:
            resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=60)
        except requests.RequestException as exc:
            if attempt < 2:
                _time.sleep(2 * (attempt + 1))
                continue
            raise OAuthError(f"换取 token 失败（网络错误）：{exc}")
        if resp.status_code == 200:
            token = resp.json()
            token["expires_at"] = _expires_at(token)
            return token
        if 500 <= resp.status_code < 600 and attempt < 2:
            _time.sleep(2 * (attempt + 1))
            continue
        raise OAuthError(f"换取 token 失败：HTTP {resp.status_code} - {resp.text}")
    raise OAuthError("换取 token 失败：多次重试后仍未成功")


def is_token_expired(token):
    """判断 access_token 是否已过期。"""
    import time
    return int(token.get("expires_at", 0)) <= int(time.time())


def verify(access_token, user_agent):
    """调用 /oauth/verify 获取角色信息。"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": user_agent,
    }
    resp = requests.get(VERIFY_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()
