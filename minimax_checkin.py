#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minimax_checkin.py — MiniMax M Plan 每日自动签到领积分

纯 Python 标准库实现，零第三方依赖，跨平台（Linux / macOS / Windows）。

M Plan（MiniMax Code）每天签到送积分，一轮 7 天共 4000 分（400/400/400/1000/400/400/1000），
每笔积分自到账起 30 天有效。官方客户端不会自动领，本脚本替你按天领取。

子命令：
    login      通过 OAuth 2.0 设备码流登录（浏览器里授权一次即可，脚本不接触密码）
    checkin    签到领积分（默认命令）
    status     只查今日签到状态与本轮进度
    balance    查询积分余额
    doctor     体检：检查凭据、网络、签名是否可用
    logout     清除本机保存的凭据

作者声明：非 MiniMax 官方工具，仅供个人学习与自用。是否违反服务条款由使用者自行判断与承担。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import random
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone as dt_timezone

__version__ = "1.0.0"

# ==========================================================================
# 常量
# ==========================================================================

# 区域 -> (账号服务 / 业务网关)
REGIONS = {
    "cn": {
        "account": "https://account.minimax.cn",
        "agent": "https://agent.minimax.cn",
        "default_tz": "Asia/Shanghai",
    },
    "io": {
        "account": "https://account.minimax.io",
        "agent": "https://agent.minimax.io",
        "default_tz": "UTC",
    },
    "com": {
        "account": "https://account.minimaxi.com",
        "agent": "https://agent.minimaxi.com",
        "default_tz": "UTC",
    },
}

# 桌面端公开 OAuth 客户端（与 MiniMax Code 客户端一致）
OAUTH_CLIENT_ID = "mcode-public"
OAUTH_SCOPES = ["agent.default"]
OAUTH_AUDIENCE = "agent-backend"

DEVICE_CODE_PATH = "/oauth2/device/code"
TOKEN_PATH = "/oauth2/token"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# 签到接口
SIGNIN_STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
SIGNIN_CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
MEMBERSHIP_PATH = "/matrix/api/v1/commerce/get_membership_info"
CREDIT_DETAILS_PATH = "/minimax-cloud/api/v1/credit/details"
RENEWAL_PATH = "/v1/api/user/renewal"

# 签名盐（逆向自 MiniMax Code 客户端）
SIGN_SALT = "I*7Cf%WZ#S&%1RlZJ&C2"
SIGN_SUFFIX = "ooui"

# 桌面端签名的设备参数，顺序必须与客户端一致（参与 yy 计算）
DEVICE_PARAM_ORDER = [
    "device_platform", "biz_id", "app_id", "version_code", "unix",
    "timezone_offset", "is_desktop", "desktop_version", "sys_language",
    "lang", "uuid", "device_id", "os_name", "browser_name", "device_memory",
    "cpu_core_num", "browser_language", "browser_platform", "user_id",
    "op_ticket", "screen_width", "screen_height",
]

# 稳定的默认设备指纹：避免每次运行随机导致设备身份漂移
DEFAULT_UUID = "3548c8fa-9ac2-4a28-8f6b-71ecd88bc048"
DEFAULT_DEVICE_ID = "1790426211"

# 本机凭据存放位置
HOME_DIR = os.path.join(os.path.expanduser("~"), ".minimax", "checkin")
CRED_FILE = os.path.join(HOME_DIR, "credentials.json")

# 签到日状态码
DAY_UPCOMING, DAY_CLAIMABLE, DAY_CLAIMED, DAY_DISABLED = 1, 2, 3, 4
# 领取结果码
CLAIM_CREDITED, CLAIM_ALREADY = 1, 2

# 退出码
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_CREDENTIAL = 2
EXIT_RATE_LIMITED = 3
EXIT_NETWORK = 4

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MiniMaxAgent/desktop"
)


# ==========================================================================
# 基础工具
# ==========================================================================

def log(msg: str) -> None:
    print(msg, flush=True)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def encode_uri_component(text: str) -> str:
    """复刻 JavaScript 的 encodeURIComponent。

    JS 放行 A-Z a-z 0-9 - _ . ! ~ * ' ( )，其余按 UTF-8 逐字节 %XX（大写十六进制）编码。
    注意非 ASCII 要按 UTF-8 字节编码，而不是 Unicode 码位。
    """
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_.!~*'()"):
            out.append(ch)
        else:
            out.extend("%%%02X" % byte for byte in ch.encode("utf-8"))
    return "".join(out)


def urlencode_js(params: dict) -> str:
    """复刻 JavaScript URLSearchParams 的序列化。

    规则同 application/x-www-form-urlencoded：
      * 保留 A-Z a-z 0-9 * - . _ ，空格变 '+'，其余 %XX（UTF-8 字节，大写十六进制）。
      * 与 encodeURIComponent 的差异：'~' 会被编码成 %7E，'*' 反而保留原样。
    """
    unreserved = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._")

    def enc(value: str) -> str:
        out = []
        for ch in value:
            if ch in unreserved:
                out.append(ch)
            elif ch == " ":
                out.append("+")
            else:
                out.extend("%%%02X" % byte for byte in ch.encode("utf-8"))
        return "".join(out)

    return "&".join("%s=%s" % (enc(str(k)), enc("" if v is None else str(v)))
                    for k, v in params.items())



def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def region_hosts(region: str) -> tuple:
    """返回 (账号服务, 业务网关)。可用环境变量覆盖，便于自建代理或联调。"""
    info = REGIONS[region]
    account = clean(os.environ.get("MINIMAX_ACCOUNT_HOST")) or info["account"]
    agent = clean(os.environ.get("MINIMAX_AGENT_HOST")) or info["agent"]
    return account, agent


def detect_timezone(region: str) -> str:
    """尽量用系统时区；取不到就用该区域的默认值。"""
    try:
        import zoneinfo  # Python 3.9+

        tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        return str(datetime.now(tz).tzinfo)
    except Exception:
        pass
    try:
        name = os.popen("date +%Z").read().strip()
        if name in ("CST", "Asia/Shanghai"):
            return "Asia/Shanghai"
    except Exception:
        pass
    return REGIONS[region]["default_tz"]


def now_ms() -> int:
    return int(time.time() * 1000)


def clean(value) -> str:
    """清洗环境变量 / 配置值：去空白、去误粘贴的成对引号、去 BOM。"""
    if value is None:
        return ""
    text = str(value).strip().lstrip("\ufeff").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def decode_jwt_payload(token: str) -> dict:
    """本地解析 JWT payload（不验签），只为读 exp / user.id。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {}
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except Exception:
        return {}


# ==========================================================================
# HTTP
# ==========================================================================

class HttpResult:
    __slots__ = ("status", "body", "headers")

    def __init__(self, status: int, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        return self.body if isinstance(self.body, (dict, list)) else None

    def text(self) -> str:
        if isinstance(self.body, (dict, list)):
            return json.dumps(self.body, ensure_ascii=False)
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8", "replace")
        return str(self.body or "")

    def error_text(self) -> str:
        """从响应里抠出服务端给的错误文案。"""
        body = self.json()
        if isinstance(body, dict):
            for key in ("error", "error_description", "message", "msg", "detail"):
                if body.get(key):
                    return str(body[key])
            br = body.get("base_resp")
            if isinstance(br, dict):
                code = br.get("status_code")
                if code not in (0, None):
                    return "%s(code=%s)" % (br.get("status_msg") or "业务失败", code)
            si = body.get("statusInfo")
            if isinstance(si, dict) and si.get("msg"):
                return str(si["msg"])
        return self.text()[:200]


def http_request(
    method: str,
    url: str,
    headers: dict = None,
    body: str = None,
    timeout: int = 30,
    form: dict = None,
) -> HttpResult:
    """发一个 HTTP 请求。form 非空时按 x-www-form-urlencoded 发送。"""
    hdrs = {
        "Accept": "application/json",
        "User-Agent": UA_DESKTOP,
    }
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v is not None})

    data = body.encode("utf-8") if isinstance(body, str) else body
    if form is not None:
        data = urlencode_js(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    # 注意：urllib 会把 header 名做 title-case（yy -> Yy、x-signature -> X-Signature），
    # 这正好是 Go / Node 服务端看到的 canonical 形式，不影响按名字取 header。
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            status, rheaders = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:  # 4xx / 5xx 也要把 body 读回来
        raw = exc.read()
        if exc.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        status, rheaders = exc.code, dict(exc.headers or {})
    except Exception as exc:
        raise NetworkError(str(exc)) from exc

    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = text
    return HttpResult(status, parsed, rheaders)


class NetworkError(Exception):
    pass


# ==========================================================================
# 签名（桌面端 JWT 模式）
# ==========================================================================

def build_device_params(user_id: str, uuid_str: str, device_id: str, tz_offset: int = 28800) -> dict:
    """构建设备参数字典，顺序严格对齐桌面端（顺序参与 yy 签名）。"""
    ts = now_ms()
    values = {
        "device_platform": "web",
        "biz_id": "3",
        "app_id": "3001",
        "version_code": "22201",
        "unix": str(ts),
        "timezone_offset": str(tz_offset),
        "is_desktop": "1",
        "desktop_version": "",
        "sys_language": "en",
        "lang": "en",
        "uuid": uuid_str,
        "device_id": device_id,
        "os_name": "Windows",
        "browser_name": "Chrome",
        "device_memory": "16",
        "cpu_core_num": "4",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "user_id": str(user_id or ""),
        "op_ticket": "undefined",
        "screen_width": "1536",
        "screen_height": "864",
    }
    return {key: values[key] for key in DEVICE_PARAM_ORDER}


def sign_request(path: str, params: dict, method: str, body: dict, variant: int = 0):
    """计算签名头。

    返回 (查询串, 请求头, body 串)。

    variant 0：x-signature 的 body 用真实 body（GET 为 ""，POST 为 "{}"）—— 逆向抓包结论
    variant 1：x-signature 的 body 恒为 "{}"
    两种 yy 算法一致：md5(encodeURIComponent(path?query) + "_" + "{}" + md5(unix) + "ooui")
    """
    params = dict(params)
    params["unix"] = str(now_ms())
    params["client"] = "desktop"

    query = urlencode_js(params)
    signed_path = "%s?%s" % (path, query)

    body_str = "" if method.upper() == "GET" else json.dumps(body or {}, ensure_ascii=False)
    sig_body = "{}" if variant == 1 else body_str

    ts_ms = int(params["unix"])
    ts_sec = ts_ms // 1000

    x_signature = md5_hex("%d%s%s" % (ts_sec, SIGN_SALT, sig_body))
    yy = md5_hex(
        encode_uri_component(signed_path) + "_" + "{}" + md5_hex(str(ts_ms)) + SIGN_SUFFIX
    )

    headers = {
        "yy": yy,
        "x-timestamp": str(ts_sec),
        "x-signature": x_signature,
        "origin": "https://agent.minimax.cn",
        "referer": "https://agent.minimax.cn/",
    }
    return query, headers, body_str


# ==========================================================================
# 凭据
# ==========================================================================

class Credential:
    """一条可用于签到的凭据。"""

    def __init__(self, token, kind="oauth", user_id="", refresh_token="",
                 expires_at=0, source="", client_id=OAUTH_CLIENT_ID,
                 scope=None, audience=OAUTH_AUDIENCE, extra=None):
        self.token = clean(token)
        self.kind = kind                      # oauth | jwt
        self.user_id = clean(user_id)
        self.refresh_token = clean(refresh_token)
        self.expires_at = int(expires_at or 0)
        self.source = source
        self.client_id = client_id
        self.scope = list(scope or OAUTH_SCOPES)
        self.audience = audience
        self.extra = extra or {}

    # ---- 过期判断 ----
    def expires_at_from_jwt(self) -> int:
        payload = decode_jwt_payload(self.token)
        try:
            return int(payload.get("exp") or 0)
        except Exception:
            return 0

    def is_expired(self, margin: int = 120) -> bool:
        if self.expires_at:
            return self.expires_at - margin <= time.time()
        exp = self.expires_at_from_jwt()
        if exp:
            return exp - margin <= time.time()
        return False

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "kind": self.kind,
            "user_id": self.user_id,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "client_id": self.client_id,
            "scope": self.scope,
            "audience": self.audience,
            "source": self.source,
        }


def load_saved_credentials() -> list:
    """读取本机保存的凭据列表。"""
    try:
        if not os.path.isfile(CRED_FILE):
            return []
        with open(CRED_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    items = data.get("accounts") if isinstance(data, dict) else data
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("token"):
            continue
        out.append(
            Credential(
                token=item.get("token"),
                kind=item.get("kind") or "oauth",
                user_id=item.get("user_id") or "",
                refresh_token=item.get("refresh_token") or "",
                expires_at=item.get("expires_at") or 0,
                source=item.get("source") or "cache",
                client_id=item.get("client_id") or OAUTH_CLIENT_ID,
                scope=item.get("scope") or OAUTH_SCOPES,
                audience=item.get("audience") or OAUTH_AUDIENCE,
            )
        )
    return out


def save_credentials(creds: list) -> None:
    os.makedirs(HOME_DIR, exist_ok=True)
    payload = {"version": 1, "updated_at": int(time.time()),
               "accounts": [c.to_dict() for c in creds]}
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_FILE)
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass


def discover_desktop_credentials(region: str) -> list:
    """尝试从本机 MiniMax Code 客户端登录态里捞凭据。

    1) 新版客户端：~/.minimax/auth/prod/<region>/mcode-public/auth.json（mmoat_ OAuth）
    2) 旧版客户端：%APPDATA%/MiniMax Agent/minimax-agent-config.json（JWT）
    """
    found = []

    # --- 新版 OAuth 登录态 ---
    appdata = os.environ.get("APPDATA") or ""
    data_dir = os.environ.get("MINIMAX_DATA_DIR") or ""
    candidates = []
    if data_dir:
        candidates.append(os.path.join(data_dir, "auth", "prod", region, "mcode-public", "auth.json"))
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".minimax", "auth", "prod", region, "mcode-public", "auth.json"))
    if appdata:
        candidates.append(os.path.join(appdata, "MiniMax Code", "auth", "prod", region, "mcode-public", "auth.json"))
        candidates.append(os.path.join(appdata, "minimax-code", "auth", "prod", region, "mcode-public", "auth.json"))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        for record in (doc.get("records") or {}).values():
            if not isinstance(record, dict):
                continue
            token = clean(record.get("accessToken"))
            if not token:
                continue
            expires = 0
            try:
                expires = int(record.get("expiresAtMs") or 0) // 1000
            except Exception:
                expires = 0
            found.append(
                Credential(
                    token=token,
                    kind="oauth",
                    user_id=clean(record.get("userId")),
                    refresh_token=clean(record.get("refreshToken")),
                    expires_at=expires,
                    source="desktop-app",
                    client_id=clean(record.get("clientId")) or OAUTH_CLIENT_ID,
                    scope=list(record.get("scopes") or OAUTH_SCOPES),
                    audience=clean(record.get("audience")) or OAUTH_AUDIENCE,
                )
            )

    # --- 旧版 JWT 登录态 ---
    legacy = []
    if appdata:
        legacy.append(os.path.join(appdata, "MiniMax Agent", "minimax-agent-config.json"))
    legacy.append(os.path.join(home, ".minimax", "minimax-agent-config.json"))
    for path in legacy:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        token = clean(((doc.get("tokens") or {}).get("accessToken")))
        if not token:
            continue
        user = doc.get("user") or {}
        found.append(
            Credential(
                token=token,
                kind="jwt",
                user_id=clean(user.get("realUserID") or user.get("userID")),
                source="desktop-legacy",
            )
        )
    return found


def env_credentials() -> list:
    """从环境变量读取凭据（CI / 容器 / 青龙场景）。"""
    out = []
    token = clean(os.environ.get("MINIMAX_TOKEN"))
    if token:
        out.append(
            Credential(
                token=token,
                kind="oauth" if token.startswith("mmoat_") else "jwt",
                user_id=clean(os.environ.get("MINIMAX_USER_ID")),
                source="env",
            )
        )
    return out


def collect_credentials(region: str) -> list:
    """按优先级汇总所有候选凭据（去重，保留顺序）。"""
    candidates = env_credentials() + load_saved_credentials() + discover_desktop_credentials(region)
    seen, unique = set(), []
    for cred in candidates:
        if not cred.token or cred.token in seen:
            continue
        seen.add(cred.token)
        unique.append(cred)
    return unique


# ==========================================================================
# OAuth：设备码登录 / 刷新
# ==========================================================================

def pkce_pair() -> tuple:
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def start_device_login(account_host: str) -> dict:
    """申请设备码（RFC 8628 + PKCE S256）。"""
    verifier, challenge = pkce_pair()
    res = http_request(
        "POST",
        account_host + DEVICE_CODE_PATH,
        form={
            "client_id": OAUTH_CLIENT_ID,
            "scope": " ".join(OAUTH_SCOPES),
            "audience": OAUTH_AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    if res.status != 200:
        raise RuntimeError("申请设备码失败: HTTP %s %s" % (res.status, res.error_text()))
    doc = res.json() or {}
    if not doc.get("device_code"):
        raise RuntimeError("设备码响应缺少 device_code")
    doc["code_verifier"] = verifier
    return doc


def poll_device_login(account_host: str, session: dict, timeout: int = 300) -> dict:
    """轮询授权结果。"""
    deadline = time.time() + timeout
    interval = max(3, int(session.get("interval") or 3))
    while time.time() < deadline:
        res = http_request(
            "POST",
            account_host + TOKEN_PATH,
            form={
                "grant_type": DEVICE_CODE_GRANT,
                "device_code": session["device_code"],
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": session["code_verifier"],
            },
        )
        doc = res.json() if isinstance(res.json(), dict) else {}
        if res.status == 200 and doc.get("access_token"):
            return doc
        err = doc.get("error") or ""
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 3
            time.sleep(interval)
            continue
        if err == "expired_token":
            raise RuntimeError("设备码已过期，请重新执行 login")
        if err == "access_denied":
            raise RuntimeError("你拒绝了本次授权")
        raise RuntimeError("授权失败: %s" % (res.error_text() or err or res.status))
    raise RuntimeError("授权超时，请重新执行 login")


def refresh_oauth(account_host: str, cred: Credential) -> Credential:
    """用 refresh_token 换新 access_token。"""
    if not cred.refresh_token:
        raise RuntimeError("没有 refresh_token，无法续期")
    res = http_request(
        "POST",
        account_host + TOKEN_PATH,
        form={
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": cred.client_id or OAUTH_CLIENT_ID,
            "scope": " ".join(cred.scope or OAUTH_SCOPES),
            "audience": cred.audience or OAUTH_AUDIENCE,
        },
    )
    if res.status != 200:
        raise RuntimeError("续期失败: HTTP %s %s" % (res.status, res.error_text()))
    doc = res.json() or {}
    token = clean(doc.get("access_token"))
    if not token:
        raise RuntimeError("续期响应里没有 access_token")
    expires_at = 0
    if doc.get("expires_in"):
        try:
            expires_at = int(time.time()) + int(doc["expires_in"])
        except Exception:
            expires_at = 0
    new_cred = Credential(
        token=token,
        kind="oauth",
        user_id=cred.user_id,
        refresh_token=clean(doc.get("refresh_token")) or cred.refresh_token,
        expires_at=expires_at,
        source=cred.source,
        client_id=cred.client_id,
        scope=cred.scope,
        audience=cred.audience,
    )
    return new_cred


def renew_desktop_token(agent_host: str, cred: Credential, tz: str) -> Credential:
    """桌面端 JWT 续期：POST /v1/api/user/renewal 换新 JWT。"""
    params = build_device_params(cred.user_id, DEFAULT_UUID, DEFAULT_DEVICE_ID)
    query, headers, body = sign_request(RENEWAL_PATH, params, "POST", {})
    headers["token"] = cred.token
    res = http_request("POST", agent_host + RENEWAL_PATH + "?" + query,
                       headers=headers, body=body)
    doc = res.json() if isinstance(res.json(), dict) else {}
    new_token = clean(((doc.get("data") or {}).get("token")))
    if not new_token:
        raise RuntimeError("续期失败: HTTP %s %s" % (res.status, res.error_text()))
    out = Credential(
        token=new_token,
        kind="jwt",
        user_id=cred.user_id,
        source=cred.source,
    )
    exp = out.expires_at_from_jwt()
    out.expires_at = exp
    return out


# ==========================================================================
# 签到 API
# ==========================================================================

def _unwrap(doc):
    """MiniMax 统一信封：base_resp.status_code == 0 视为业务成功。"""
    if not isinstance(doc, dict):
        return False, "", {}
    br = doc.get("base_resp")
    if isinstance(br, dict) and br.get("status_code") not in (0, None):
        return False, "%s(code=%s)" % (br.get("status_msg") or "业务失败", br.get("status_code")), {}
    return True, "", doc.get("data") or {}


class SigninClient:
    """把「一次签到」拆成：查状态 -> 判断能否领 -> 领取。"""

    def __init__(self, region: str, timezone: str, cred: Credential):
        self.region = region
        self.account_host, self.agent_host = region_hosts(region)
        self.tz = timezone
        self.cred = cred
        self._sig_variant = 0   # 当前使用的签名变体，失败时自动切换
        self._use_signature = False  # OAuth 模式下是否需要补签名头

    # -- 底层请求 -------------------------------------------------------
    def _build(self, path: str, method: str, body: dict, sign: bool):
        """构造一次请求，返回 (url, headers, body 串)。

        sign=True  走桌面端风格：带 yy / x-timestamp / x-signature 三个签名头
        sign=False 走纯 OAuth 风格：只带 Authorization: Bearer
        """
        body = body if body is not None else {}
        headers = {}

        if not sign:
            # OAuth 模式：Bearer + timezone_id，服务端直接校验令牌
            headers["Authorization"] = "Bearer " + self.cred.token
            if method.upper() != "GET":
                headers["Content-Type"] = "application/json"
            url = "%s%s?timezone_id=%s" % (self.agent_host, path,
                                           urllib.parse.quote(self.tz, safe=""))
            body_str = "" if method.upper() == "GET" else json.dumps(body, ensure_ascii=False)
            return url, headers, body_str

        # 签名模式：token 头 + 签名头
        params = build_device_params(self.cred.user_id, DEFAULT_UUID, DEFAULT_DEVICE_ID)
        if self.cred.kind == "oauth":
            params["timezone_id"] = self.tz
        query, sig_headers, body_str = sign_request(path, params, method, body, self._sig_variant)
        headers.update(sig_headers)
        if self.cred.kind == "jwt":
            headers["token"] = self.cred.token
        else:
            headers["Authorization"] = "Bearer " + self.cred.token
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"
        return self.agent_host + path + "?" + query, headers, body_str

    def _request(self, path: str, method: str, body: dict = None) -> HttpResult:
        """按凭据类型选择鉴权方式发请求。"""
        sign = self.cred.kind == "jwt" or self._use_signature
        url, headers, body_str = self._build(path, method, body, sign)
        return http_request(method, url, headers=headers,
                            body=body_str if method.upper() != "GET" else None)

    def _request_with_fallback(self, path: str, method: str, body: dict = None) -> HttpResult:
        """容错：服务端说签名不对时，换一种签名形态再试。

        - 桌面端模式：换另一种 x-signature 变体（body 取 "" 还是 "{}"）
        - OAuth 模式：先试不带签名，被拒则补上签名头再来一次
        """
        res = self._request(path, method, body)
        if res.status != 400 or "signature" not in res.error_text().lower():
            return res

        if self.cred.kind == "jwt":
            self._sig_variant = 1 if self._sig_variant == 0 else 0
            return self._request(path, method, body)

        # OAuth 模式：补签名头
        url, headers, body_str = self._build(path, method, body, True)
        res2 = http_request(method, url, headers=headers,
                            body=body_str if method.upper() != "GET" else None)
        if res2.status != 400:
            self._use_signature = True
        return res2

    # -- 业务 -----------------------------------------------------------
    def _check(self, res: HttpResult, what: str):
        """统一处理响应：鉴权问题抛 AuthError（可触发续期），其它抛 ApiError。"""
        if res.status in (401, 403):
            raise AuthError("%s失败: HTTP %s %s" % (what, res.status, res.error_text()))
        if res.status != 200:
            raise ApiError("%s失败: HTTP %s %s" % (what, res.status, res.error_text()))
        ok, msg, data = _unwrap(res.json())
        if not ok:
            raise ApiError("%s失败: %s" % (what, msg))
        return data if isinstance(data, dict) else {}

    def status(self) -> dict:
        res = self._request_with_fallback(SIGNIN_STATUS_PATH, "GET")
        data = self._check(res, "状态查询")
        days = []
        for entry in (data.get("days") or []):
            if not isinstance(entry, dict):
                continue
            try:
                days.append({
                    "day_no": int(entry.get("day_no") or 0),
                    "points": int(entry.get("points") or 0),
                    "bonus_points": int(entry.get("bonus_points") or 0),
                    "status": int(entry.get("status") or 0),
                    "is_today": bool(entry.get("is_today")),
                })
            except (TypeError, ValueError):
                continue
        today = next((d for d in days if d["is_today"]), None)
        return {"scene": data.get("scene"), "days": days, "today": today}

    def claim(self) -> dict:
        res = self._request_with_fallback(SIGNIN_CLAIM_PATH, "POST", {})
        data = self._check(res, "领取")
        try:
            result = int(data.get("claim_result") or 0)
        except (TypeError, ValueError):
            result = 0
        try:
            points = int(data.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        return {
            "claim_result": result,
            "points": points,
            "day_no": data.get("day_no"),
            "expire_at_ms": data.get("expire_at_ms"),
        }

    def balance(self) -> dict:
        res = http_request("POST", self.agent_host + MEMBERSHIP_PATH,
                           headers={"Authorization": "Bearer " + self.cred.token,
                                    "Content-Type": "application/json"},
                           body="{}")
        if res.status != 200:
            raise ApiError("余额查询失败: HTTP %s %s" % (res.status, res.error_text()))
        doc = res.json() or {}
        summary = doc.get("op_credit_summary") or {}
        if not isinstance(summary, dict) or not summary:
            summary = doc.get("token_plan_credit") or {}
        return {
            "total_remaining": summary.get("total_remaining_amount"),
            "purchased_remaining": summary.get("purchased_remaining_amount"),
            "free_remaining": summary.get("free_remaining_amount"),
        }


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


# ==========================================================================
# 签到主流程
# ==========================================================================

def run_checkin(cred: Credential, region: str, timezone: str, force: bool = False) -> dict:
    """用一条凭据完成一次签到。返回结构化结果。"""
    client = SigninClient(region, timezone, cred)

    panel = client.status()
    today = panel.get("today")
    days = panel.get("days") or []

    cycle_points = sum(d.get("points", 0) for d in days)
    result = {
        "ok": True,
        "code": "ALREADY",
        "source": cred.source,
        "timezone": timezone,
        "cycle_days": len(days),
        "cycle_points": cycle_points,
        "today": today,
        "message": "",
    }

    if today and today.get("status") == DAY_CLAIMED:
        result["message"] = "今日已签到（第 %s 天）" % today.get("day_no")
        return result

    if not force and today and today.get("status") not in (DAY_CLAIMABLE,):
        result["ok"] = False
        result["code"] = "NOT_CLAIMABLE"
        result["message"] = "今日暂不可签到（status=%s）" % today.get("status")
        return result

    claimed = client.claim()
    if claimed.get("claim_result") == CLAIM_ALREADY:
        result["code"] = "ALREADY"
        result["message"] = "今日已签到（服务端返回 claim_result=2）"
        return result
    if claimed.get("claim_result") != CLAIM_CREDITED and not claimed.get("points"):
        result["ok"] = False
        result["code"] = "FAIL"
        result["message"] = "领取未成功: %s" % json.dumps(claimed, ensure_ascii=False)
        return result

    result["code"] = "SUCCESS"
    result["claimed"] = claimed
    result["message"] = "签到成功 +%s 积分（第 %s 天）" % (
        claimed.get("points", "?"), claimed.get("day_no", "?"))
    return result


def checkin_with_fallback(creds: list, region: str, timezone: str, force: bool = False) -> dict:
    """依次尝试各条凭据；鉴权失败会先用 refresh_token 自动续期再重试。"""
    if not creds:
        return {"ok": False, "code": "NO_CREDENTIAL",
                "message": "没有可用凭据。先执行 `minimax_checkin.py login`，"
                           "或设置环境变量 MINIMAX_TOKEN / MINIMAX_USER_ID。"}

    last_error = ""
    for slot, cred in enumerate(creds):
        try:
            return run_checkin(cred, region, timezone, force)
        except AuthError as exc:
            last_error = str(exc)
            log("  令牌失效，尝试自动续期…")
            refreshed = _try_refresh(cred, region, timezone)
            if refreshed is None:
                continue
            try:
                result = run_checkin(refreshed, region, timezone, force)
                result["source"] = "%s → 已自动续期" % cred.source
                _persist_refreshed(cred, refreshed, slot)
                return result
            except Exception as exc2:  # noqa: BLE001
                last_error = str(exc2)
        except ApiError as exc:
            msg = str(exc)
            last_error = msg
            if "429" in msg or "频繁" in msg:
                return {"ok": False, "code": "RATE_LIMITED", "message": msg}
        except NetworkError as exc:
            return {"ok": False, "code": "NETWORK", "message": "网络异常: %s" % exc}
        except Exception as exc:  # noqa: BLE001
            last_error = "%s: %s" % (type(exc).__name__, exc)
        log("  凭据 %s 失败：%s" % (cred.source, last_error))

    return {"ok": False, "code": "FAIL", "message": last_error or "签到失败"}


def _try_refresh(cred: Credential, region: str, timezone: str):
    """凭据过期时尝试自动续期。"""
    try:
        if cred.kind == "oauth" and cred.refresh_token:
            return refresh_oauth(region_hosts(region)[0], cred)
        if cred.kind == "jwt":
            return renew_desktop_token(region_hosts(region)[1], cred, timezone)
    except Exception as exc:  # noqa: BLE001
        log("  续期失败: %s" % exc)
    return None


def _persist_refreshed(old: Credential, new: Credential, slot: int = -1) -> None:
    """把续期后的凭据写回本地缓存，**替换**原位置而不是追加。

    追加会出事：OAuth 凭据没有 user_id，匹配不上就会每次续期都往文件里塞一条，
    越攒越多。GitHub Actions 这类场景必须原地替换，否则缓存里的陈旧条目会
    一直排在前面被优先选中。这里按三种方式依次匹配：slot 下标 → 相同
    refresh_token → 相同 token。
    """
    saved = load_saved_credentials()
    replaced = False

    if 0 <= slot < len(saved) and saved[slot].token == old.token:
        saved[slot] = new
        replaced = True
    if not replaced and old.refresh_token:
        for index, item in enumerate(saved):
            if item.refresh_token and item.refresh_token == old.refresh_token:
                saved[index] = new
                replaced = True
                break
    if not replaced:
        for index, item in enumerate(saved):
            if item.token == old.token:
                saved[index] = new
                replaced = True
                break
    if not replaced:
        saved.append(new)

    try:
        save_credentials(saved)
    except Exception as exc:  # noqa: BLE001
        log("  写回凭据失败: %s" % exc)


# ==========================================================================
# 通知
# ==========================================================================

def notify(url: str, title: str, content: str) -> bool:
    """推送签到结果。按 URL 自动识别渠道，也支持通用 webhook。"""
    if not url:
        return False
    try:
        if "oapi.dingtalk.com" in url:
            payload = {"msgtype": "text", "text": {"content": "%s\n%s" % (title, content)}}
        elif "api.day.app" in url:
            payload = {"title": title, "body": content, "group": "MiniMax"}
        elif "api.telegram.org" in url:
            payload = {"text": "%s\n%s" % (title, content)}
        elif "ftqq.com" in url or "sctapi" in url:
            payload = {"title": title, "desp": content}
        else:
            payload = {"title": title, "content": content, "message": content}
        res = http_request("POST", url, headers={"Content-Type": "application/json"},
                           body=json.dumps(payload, ensure_ascii=False), timeout=15)
        return res.status < 300
    except Exception as exc:  # noqa: BLE001
        log("通知发送失败: %s" % exc)
        return False


# ==========================================================================
# 子命令
# ==========================================================================

def cmd_login(args) -> int:
    account_host, _ = region_hosts(args.region)
    log("正在申请设备码…")
    session = start_device_login(account_host)
    url = session.get("verification_uri_complete") or session.get("verification_uri")
    code = session.get("user_code") or ""
    log("")
    log("  请在浏览器打开下面链接，用你的 MiniMax 账号登录并确认授权：")
    log("  %s" % url)
    if code:
        log("  （如果页面没自动带上，验证码是：%s）" % code)
    log("")
    log("等待授权中…（有效期 %s 秒）" % session.get("expires_in", 300))
    try:
        doc = poll_device_login(account_host, session)
    except Exception as exc:  # noqa: BLE001
        log("登录失败: %s" % exc)
        return EXIT_FAIL

    token = clean(doc.get("access_token"))
    expires_at = 0
    if doc.get("expires_in"):
        try:
            expires_at = int(time.time()) + int(doc["expires_in"])
        except Exception:
            expires_at = 0
    cred = Credential(
        token=token,
        kind="oauth",
        refresh_token=clean(doc.get("refresh_token")),
        expires_at=expires_at,
        source="oauth-login",
    )
    saved = [c for c in load_saved_credentials() if c.token != cred.token]
    saved.append(cred)
    save_credentials(saved)
    log("登录成功 ✅")
    log("凭据已保存到 %s（权限 600）" % CRED_FILE)
    log("接下来可以直接执行: python3 %s checkin" % os.path.basename(__file__))
    return EXIT_OK


# ==========================================================================
# 随机时间窗口
# ==========================================================================

def _state_path() -> str:
    return os.path.join(HOME_DIR, "state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _state_path())
    except Exception as exc:  # noqa: BLE001
        log("  状态文件写入失败: %s" % exc)


def parse_window(spec: str) -> tuple:
    """解析 '08:00-08:30' -> (480, 510)（分钟数）。"""
    text = clean(spec)
    if "-" not in text:
        raise ValueError("窗口格式应为 HH:MM-HH:MM，收到的是 %r" % spec)
    left, right = text.split("-", 1)

    def to_minutes(part):
        hour, minute = (int(x) for x in part.strip().split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
        return hour * 60 + minute

    start, end = to_minutes(left), to_minutes(right)
    if start > end:
        raise ValueError("窗口起始时间不能晚于结束时间")
    return start, end


def _tz_now(timezone: str):
    """拿到带时区的当前时间。

    优先用 zoneinfo（Python 3.9+）。Windows 上若没装 tzdata 会失败，
    这时退回该区域的固定偏移，保证功能不挂。
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(timezone))
    except Exception:
        pass
    hours = {"cn": 8, "io": 0, "com": 0}.get(timezone, 8)
    return datetime.now(dt_timezone(timedelta(hours=hours)))


def window_gate(args, timezone: str) -> tuple:
    """决定这一轮要不要真的去签到。

    返回 (should_run, reason)。
    每天在窗口内随机选一个时刻，并写进状态文件；同一天多次调用（CI 每 5 分钟跑一次）
    会得到同一个时刻，不会重复签到。

    窗口外的调用一律跳过——包括手动触发。这是关键：
    之前用 now.replace(hour=...) 算目标时刻，当当前时间已经过了窗口，
    「今天 08:xx」会被判定成「已到点」，导致窗口外也能签到。
    """
    if not getattr(args, "window", None):
        return True, ""

    start, end = parse_window(args.window)
    grace = int(getattr(args, "window_grace", 5) or 0)
    now = _tz_now(timezone)
    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute

    state = _load_state()
    days = state.setdefault("days", {})
    entry = days.get(today)
    if not isinstance(entry, dict) or "target" not in entry:
        target = random.randint(start, end)
        entry = {"target": target, "done": False}
        days[today] = entry
        # 只保留最近 30 天，避免状态文件无限膨胀
        for key in sorted([k for k in days if k < today])[:-30]:
            days.pop(key, None)
        _save_state(state)

    target = int(entry["target"])
    target_dt = now.replace(hour=target // 60, minute=target % 60, second=0, microsecond=0)
    label = "%02d:%02d" % (target // 60, target % 60)
    win_label = "%02d:%02d-%02d:%02d" % (start // 60, start % 60, end // 60, end % 60)

    if entry.get("done"):
        return False, "今天已在 %s 签过，跳过" % label

    # ── 窗口外一律不签 ──────────────────────────────────────────
    if now_min < start:
        if getattr(args, "wait", False):
            wait_seconds = (target_dt - now).total_seconds()
            log("签到窗口 %s，今天定在 %s，等待 %.0f 秒…" % (win_label, label, wait_seconds))
            time.sleep(max(0, wait_seconds))
        else:
            return False, "还没到签到窗口（%s 开始），今天定在 %s，跳过" % (win_label, label)
    elif now_min > end + grace:
        return False, "已错过签到窗口（%s 结束），今天不签" % win_label

    if now_min < target:
        if getattr(args, "wait", False):
            wait_seconds = (target_dt - now).total_seconds()
            log("今天定在 %s 签到，等待 %.0f 秒…" % (label, wait_seconds))
            time.sleep(max(0, wait_seconds))
        else:
            return False, "今天定在 %s 签到，还没到时间，跳过" % label
    return True, ""


def mark_window_done(args, timezone: str) -> None:
    """签到成功后标记今天已完成。"""
    if not getattr(args, "window", None):
        return
    today = _tz_now(timezone).strftime("%Y-%m-%d")
    state = _load_state()
    entry = (state.get("days") or {}).get(today)
    if isinstance(entry, dict):
        entry["done"] = True
        entry["done_at"] = _tz_now(timezone).isoformat()
        _save_state(state)


def cmd_checkin(args) -> int:
    timezone = args.timezone or detect_timezone(args.region)

    # 随机窗口闸门：没到点就跳过，不消耗接口调用
    try:
        should_run, reason = window_gate(args, timezone)
    except ValueError as exc:
        log("参数错误: %s" % exc)
        return EXIT_FAIL
    if not should_run:
        if args.json:
            log(json.dumps({"ok": True, "code": "SKIPPED", "message": reason},
                            ensure_ascii=False, indent=2))
        else:
            log("⏭ %s" % reason)
        return EXIT_OK

    creds = collect_credentials(args.region)
    if args.token:
        creds.insert(0, Credential(token=args.token,
                                   kind="oauth" if args.token.startswith("mmoat_") else "jwt",
                                   user_id=args.user_id or "",
                                   source="cli"))

    log("区域: %s   时区: %s" % (args.region, timezone))
    if creds:
        log("候选凭据: %d 条（%s）" % (
            len(creds), "、".join(sorted({c.source for c in creds}))))
    else:
        log("候选凭据: 0 条")

    result = checkin_with_fallback(creds, args.region, timezone, force=args.force)

    if args.json:
        log(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        icon = "✅" if result.get("ok") else "⚠️"
        log("%s %s" % (icon, result.get("message") or ""))

    if result.get("ok"):
        mark_window_done(args, timezone)
        _maybe_notify(args, "MiniMax 签到成功", result.get("message", ""))
        return EXIT_OK

    if result.get("code") == "RATE_LIMITED":
        _maybe_notify(args, "MiniMax 签到限频", result.get("message", ""))
        return EXIT_RATE_LIMITED
    if result.get("code") == "NO_CREDENTIAL":
        _maybe_notify(args, "MiniMax 签到失败", result.get("message", ""))
        return EXIT_NO_CREDENTIAL
    if result.get("code") == "NETWORK":
        _maybe_notify(args, "MiniMax 签到网络异常", result.get("message", ""))
        return EXIT_NETWORK
    _maybe_notify(args, "MiniMax 签到失败", result.get("message", ""))
    return EXIT_FAIL


# ==========================================================================
# 定时任务安装
# ==========================================================================

CRON_MARKER = "# minimax-checkin"


def _python_path() -> str:
    """当前解释器的绝对路径（写进 crontab 时要绝对路径，cron 的 PATH 很短）。"""
    return os.path.abspath(sys.executable or "python3")


def _script_path() -> str:
    return os.path.abspath(__file__)


def build_cron_line(time_str: str, notify_url: str = "") -> str:
    """生成 crontab 行。

    time_str 形如 '09:07'（固定时刻）或 '08:00-08:30'（随机窗口）。
    窗口模式下 cron 在窗口起点触发，脚本自己随机挑一个时刻并等到那会儿再签。
    """
    text = clean(time_str)
    window = ""
    if "-" in text:
        try:
            parse_window(text)
        except ValueError as exc:
            raise ValueError(str(exc))
        window = text
        start, _ = parse_window(text)
        minute, hour = start % 60, start // 60
    else:
        try:
            hour, minute = (int(x) for x in text.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except Exception:
            raise ValueError("时间格式应为 HH:MM 或 HH:MM-HH:MM，收到的是 %r" % time_str)

    command = '"%s" "%s" checkin' % (_python_path(), _script_path())
    if window:
        command += ' --window "%s" --wait' % window
    if notify_url:
        command += ' --notify "%s"' % notify_url
    return "%d %d * * * %s %s" % (minute, hour, command, CRON_MARKER)


def _read_crontab() -> list:
    """读现有 crontab（没有 crontab 命令或没有任务时返回空列表）。"""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return []
        return proc.stdout.splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _write_crontab(lines: list) -> bool:
    """写回 crontab。"""
    payload = "\n".join(lines).strip() + "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=payload, text=True, timeout=15)
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def cmd_export(args) -> int:
    """把本机凭据导出成可直接粘贴到 GitHub Secret 的文本。"""
    creds = load_saved_credentials()
    if not creds:
        log("本机没有已登录的凭据。先执行 login。")
        return EXIT_NO_CREDENTIAL
    payload = {"version": 1, "updated_at": int(time.time()),
               "accounts": [c.to_dict() for c in creds]}
    if getattr(args, "raw", False):
        # 纯 JSON 输出，便于管道给 gh secret set / 写文件
        print(json.dumps(payload, ensure_ascii=False))
        return EXIT_OK
    log(json.dumps(payload, ensure_ascii=False, indent=2))
    log("")
    log("上面这一整段（从 { 到 }）复制到 GitHub 仓库的 Secret：MINIMAX_CREDENTIALS")
    return EXIT_OK


def cmd_install(args) -> int:
    """把每日签到装进系统定时任务。"""
    is_windows = os.name == "nt"

    if args.uninstall:
        return _do_uninstall(is_windows)

    # 1. 先确保有凭据
    creds = collect_credentials(args.region)
    if not creds:
        log("没有找到登录凭据，需要先登录一次。")
        log("")
        log("  现在就登录：python3 %s login" % os.path.basename(__file__))
        log("  （会打印一个链接，浏览器里点确认即可，脚本不接触你的密码）")
        log("")
        log("登录完再跑一次 install 即可。")
        return EXIT_NO_CREDENTIAL
    log("✅ 已有凭据：%d 条（来源 %s）" % (len(creds), "、".join(sorted({c.source for c in creds}))))

    # 2. 生成计划
    try:
        line = build_cron_line(args.time, args.notify or "")
    except ValueError as exc:
        log("参数错误: %s" % exc)
        return EXIT_FAIL

    if is_windows:
        log("")
        log("Windows 用「任务计划程序」创建每日任务：")
        log("  程序    : %s" % _python_path())
        log("  参数    : \"%s\" checkin" % _script_path())
        log("  触发器  : 每天 %s" % args.time)
        log("  起始位置: %s" % os.path.dirname(_script_path()))
        log("")
        log("或者把上面这行保存成 run.cmd，双击运行即可手动签一次。")
        return EXIT_OK

    # 3. 写入 crontab（先删掉旧的同名任务，避免重复）
    if args.print_only:
        log("")
        log("crontab 条目：")
        log("  %s" % line)
        return EXIT_OK

    existing = [l for l in _read_crontab() if CRON_MARKER not in l]
    existing.append(line)
    if not _write_crontab(existing):
        log("")
        log("❌ 写入 crontab 失败（可能没有 crontab 命令或没有权限）。")
        log("   你可以手动执行 crontab -e，粘贴这一行：")
        log("")
        log("   %s" % line)
        return EXIT_FAIL

    log("")
    if "-" in clean(args.time):
        log("✅ 已安装定时任务：每天在 %s 之间随机挑一个时刻自动签到" % args.time)
        log("   （cron 在窗口起点触发，脚本会等到当天随机选中的那一刻再签）")
    else:
        log("✅ 已安装定时任务：每天 %s 自动签到" % args.time)
    log("   %s" % line)
    log("")
    log("查看：  crontab -l")
    log("卸载：  python3 %s install --uninstall" % os.path.basename(__file__))
    log("手动跑一次：python3 %s checkin" % os.path.basename(__file__))
    return EXIT_OK


def _do_uninstall(is_windows: bool) -> int:
    if is_windows:
        log("Windows 请在「任务计划程序」里删除对应的每日任务。")
        return EXIT_OK
    existing = [l for l in _read_crontab() if CRON_MARKER not in l]
    if not _write_crontab(existing):
        log("卸载失败：可能没有 crontab 命令。手动执行 crontab -e 删除带 %s 的行即可。" % CRON_MARKER)
        return EXIT_FAIL
    log("✅ 已卸载定时任务。")
    return EXIT_OK


def cmd_status(args) -> int:
    timezone = args.timezone or detect_timezone(args.region)
    creds = collect_credentials(args.region)
    if args.token:
        creds.insert(0, Credential(token=args.token,
                                   kind="oauth" if args.token.startswith("mmoat_") else "jwt",
                                   user_id=args.user_id or "",
                                   source="cli"))
    if not creds:
        msg = "没有可用凭据。先执行 login。"
        log(json.dumps({"ok": False, "message": msg}, ensure_ascii=False) if args.json else msg)
        return EXIT_NO_CREDENTIAL

    client = SigninClient(args.region, timezone, creds[0])
    try:
        panel = client.status()
    except Exception as exc:  # noqa: BLE001
        log("查询失败: %s" % exc)
        return EXIT_FAIL

    days = panel.get("days") or []
    today = panel.get("today")
    status_text = {DAY_UPCOMING: "未开始", DAY_CLAIMABLE: "可签到",
                   DAY_CLAIMED: "已签到", DAY_DISABLED: "不可用"}
    if args.json:
        log(json.dumps(panel, ensure_ascii=False, indent=2))
        return EXIT_OK

    log("时区: %s   本轮周期: %s 天 / 共 %s 积分" % (
        timezone, len(days), sum(d.get("points", 0) for d in days)))
    for day in days:
        mark = " ← 今天" if day.get("is_today") else ""
        log("  第 %s 天  %6s 分  %s%s" % (
            day.get("day_no"), day.get("points"),
            status_text.get(day.get("status"), "未知"), mark))
    if today:
        log("今日状态: %s" % status_text.get(today.get("status"), "未知"))
    return EXIT_OK


def cmd_balance(args) -> int:
    creds = collect_credentials(args.region)
    if args.token:
        creds.insert(0, Credential(token=args.token, kind="oauth", source="cli"))
    if not creds:
        log("没有可用凭据。先执行 login。")
        return EXIT_NO_CREDENTIAL
    client = SigninClient(args.region, args.timezone or detect_timezone(args.region), creds[0])
    try:
        balance = client.balance()
    except Exception as exc:  # noqa: BLE001
        log("查询失败: %s" % exc)
        return EXIT_FAIL
    if args.json:
        log(json.dumps(balance, ensure_ascii=False, indent=2))
    else:
        log("剩余积分: %s" % balance.get("total_remaining"))
        log("  其中赠送: %s   购买: %s" % (
            balance.get("free_remaining"), balance.get("purchased_remaining")))
    return EXIT_OK


def cmd_doctor(args) -> int:
    """体检：把每个环节单独验一遍，方便排错。"""
    region = args.region
    timezone = args.timezone or detect_timezone(region)
    log("=== MiniMax 签到体检 ===")
    log("版本      : %s" % __version__)
    account_host, agent_host = region_hosts(region)
    log("区域      : %s  (账号 %s / 网关 %s)" % (region, account_host, agent_host))
    log("时区      : %s" % timezone)
    log("凭据文件  : %s (%s)" % (CRED_FILE, "存在" if os.path.isfile(CRED_FILE) else "不存在"))

    creds = collect_credentials(region)
    log("候选凭据  : %d 条" % len(creds))
    for cred in creds:
        exp = cred.expires_at or cred.expires_at_from_jwt()
        if exp:
            left = (exp - time.time()) / 86400
            life = "剩余 %.1f 天" % left
            if cred.is_expired():
                life += "（已过期，将自动续期）"
        else:
            life = "有效期未知"
        log("  - 来源 %-14s 类型 %-5s %s" % (cred.source, cred.kind, life))

    # 网络连通性
    try:
        res = http_request("GET", account_host + "/", timeout=15)
        log("网络      : account 可达 (HTTP %s)" % res.status)
    except Exception as exc:  # noqa: BLE001
        log("网络      : account 不可达 (%s)" % exc)
        return EXIT_NETWORK

    # 签到接口（不带凭据，预期是 401/400，说明接口存在且需要鉴权）
    try:
        res = http_request("GET", "%s%s?timezone_id=%s" % (
            agent_host, SIGNIN_STATUS_PATH,
            urllib.parse.quote(timezone, safe="")), timeout=15)
        log("签到接口  : 存在 (HTTP %s %s)" % (res.status, res.error_text()[:60]))
    except Exception as exc:  # noqa: BLE001
        log("签到接口  : 异常 (%s)" % exc)

    if not creds:
        log("")
        log("结论: 没有凭据。执行 `python3 minimax_checkin.py login` 完成一次授权。")
        return EXIT_NO_CREDENTIAL

    log("")
    log("试跑一次状态查询…")
    client = SigninClient(region, timezone, creds[0])
    try:
        panel = client.status()
        days = panel.get("days") or []
        today = panel.get("today")
        log("  ✅ 状态查询成功，本轮 %s 天 / 共 %s 积分" % (
            len(days), sum(d.get("points", 0) for d in days)))
        if today:
            log("  今日: 第 %s 天 %s 分 (status=%s)" % (
                today.get("day_no"), today.get("points"), today.get("status")))
    except Exception as exc:  # noqa: BLE001
        log("  ⚠️ 状态查询失败: %s" % exc)
        return EXIT_FAIL
    return EXIT_OK


def cmd_logout(args) -> int:
    if not os.path.isfile(CRED_FILE):
        log("本机没有保存任何凭据。")
        return EXIT_OK
    try:
        os.remove(CRED_FILE)
        log("已删除 %s" % CRED_FILE)
    except Exception as exc:  # noqa: BLE001
        log("删除失败: %s" % exc)
        return EXIT_FAIL
    return EXIT_OK


def _maybe_notify(args, title: str, content: str) -> None:
    url = args.notify or os.environ.get("MINIMAX_NOTIFY_URL") or ""
    if url:
        notify(url, title, content)


# ==========================================================================
# 入口
# ==========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimax_checkin.py",
        description="MiniMax M Plan 每日自动签到领积分",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python3 minimax_checkin.py login              # 首次：浏览器授权登录
  python3 minimax_checkin.py checkin            # 每日签到
  python3 minimax_checkin.py checkin --json     # 机器可读输出
  python3 minimax_checkin.py status            # 只看状态不领取
  python3 minimax_checkin.py doctor            # 排错体检
""",
    )
    parser.add_argument("--version", action="version", version="minimax_checkin %s" % __version__)
    sub = parser.add_subparsers(dest="command")

    def add_common(sp, need_token=False):
        sp.add_argument("--region", choices=sorted(REGIONS.keys()), default=None,
                        help="区域，默认读环境变量 MINIMAX_REGION，否则 cn")
        sp.add_argument("--timezone", default=None,
                        help="IANA 时区名，默认取系统时区（国内为 Asia/Shanghai）")
        sp.add_argument("--json", action="store_true", help="输出 JSON")
        sp.add_argument("--notify", default=None,
                        help="通知地址（支持钉钉/Bark/Telegram/Server酱/通用 webhook）")
        if need_token:
            sp.add_argument("--token", default=None, help="手动指定 access token")
            sp.add_argument("--user-id", default=None, help="配合 --token 使用的 user id")

    sp = sub.add_parser("login", help="OAuth 设备码登录")
    add_common(sp)

    sp = sub.add_parser("checkin", help="签到领积分（默认）")
    add_common(sp, need_token=True)
    sp.add_argument("--force", action="store_true", help="即使状态显示不可签也尝试领取")
    sp.add_argument("--window", default=None,
                    help="随机时间窗口，如 08:00-08:30（按 --timezone 计时）")
    sp.add_argument("--window-grace", dest="window_grace", type=int, default=5,
                    help="窗口结束后的宽限分钟数（应对 runner 延迟，默认 5）")
    sp.add_argument("--wait", dest="wait", action="store_true", default=True,
                    help="没到随机时刻就等待（本地 cron 用，默认开）")
    sp.add_argument("--no-wait", dest="wait", action="store_false",
                    help="没到随机时刻直接跳过（CI 每几分钟跑一次时用）")

    sp = sub.add_parser("status", help="只查询签到状态")
    add_common(sp, need_token=True)

    sp = sub.add_parser("balance", help="查询积分余额")
    add_common(sp, need_token=True)

    sp = sub.add_parser("doctor", help="体检排错")
    add_common(sp)

    sp = sub.add_parser("logout", help="清除本机凭据")
    add_common(sp)

    sp = sub.add_parser("export", help="导出凭据（用于填 GitHub Secret）")
    add_common(sp)
    sp.add_argument("--raw", action="store_true", help="只输出 JSON，不带说明文字")

    sp = sub.add_parser("install", help="安装每日定时任务")
    add_common(sp)
    sp.add_argument("--time", default="09:07", help="每天几点签到，HH:MM（默认 09:07）")
    sp.add_argument("--uninstall", action="store_true", help="卸载定时任务")
    sp.add_argument("--print-only", action="store_true", help="只打印 crontab 条目，不写入")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        argv = ["checkin"] + argv

    args = parser.parse_args(argv)
    command = args.command or "checkin"

    # 区域与时区的默认值
    if getattr(args, "region", None) is None:
        args.region = clean(os.environ.get("MINIMAX_REGION")) or "cn"
    if args.region not in REGIONS:
        args.region = "cn"
    if not getattr(args, "timezone", None):
        args.timezone = clean(os.environ.get("MINIMAX_TIMEZONE")) or detect_timezone(args.region)
    if not getattr(args, "notify", None):
        args.notify = clean(os.environ.get("MINIMAX_NOTIFY_URL")) or None

    handlers = {
        "login": cmd_login,
        "checkin": cmd_checkin,
        "status": cmd_status,
        "balance": cmd_balance,
        "doctor": cmd_doctor,
        "logout": cmd_logout,
        "install": cmd_install,
        "export": cmd_export,
    }
    try:
        return handlers[command](args)
    except KeyboardInterrupt:
        log("\n已中断。")
        return EXIT_FAIL
    except NetworkError as exc:
        log("网络异常: %s" % exc)
        return EXIT_NETWORK


if __name__ == "__main__":
    sys.exit(main())
