"""CLI 授权（PKCE）：把一条网页会话换成"CLI 能用"的会话 token。

契约（2026-10-31 实测，两段接口都在 ``api.devin.ai``）：

    1. POST /auth/cli/authorize
       headers: Authorization: Bearer auth1_…  +  x-cog-org-id: org-…
       body:    {"code_challenge": "<S256>", "code_challenge_method": "S256"}
       → 200 {"code": "<43 字符>"}

       🔴 **必须带 x-cog-org-id**：不带 → 401 ``No organizations found for auth1 user``
          （与 /api/users/info 同一条规则）。授权绑定的是**组织**，不是用户。
       ⚠️ code 一次性、5 分钟有效；重复使用 → 401 ``Invalid or expired code…``。

    2. POST /auth/cli/token
       body: {"code": "<code>", "code_verifier": "<PKCE verifier>"}
       → 200 {"token": "eyJ…", "webapp_host": "app.devin.ai"}

       token 是 JWT，payload 只有 ``{"session_id": "windsurf-session-…"}`` ——
       账号/组织信息在服务端绑定，token 里不带。

⚠️ 这个 token **不是** apk_user_ 那种推理 API key：
   - ``app.devin.ai/api/*`` → 401 Unauthenticated
   - ``api.devin.ai/v1/sessions`` → 403 Unauthorized
   它是 CLI/Windsurf 侧的会话凭据，只能交给 CLI 用（见 ``verify_cli_token`` 的说明）。

redirect_uri 白名单（前端原文，服务端 authorize 不收这个参数）::

    protocol === "http:" && hostname === "127.0.0.1" && pathname === "/callback"

即 ``http://127.0.0.1:<任意端口>/callback``。本模块的 ``is_allowed_redirect``
与它逐条对齐 —— 自动化时我们**直接拿 code 去换 token**，不经过浏览器回跳。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

from . import config
from .devin import DevinClient, DevinError

#: PKCE 方法只支持 S256（站点侧也是这么写的默认值）。
PKCE_METHOD = "S256"
#: verifier 的字节数。``token_urlsafe(64)`` → 86 字符，落在 RFC 7636 的 43~128 区间内。
PKCE_BYTES = 64
#: CLI 会话 token 的 session_id 前缀（实测值）。
CLI_SESSION_PREFIX = "windsurf-session-"


class CliAuthError(DevinError):
    """CLI 授权的分类失败。``code`` 决定"换账号 / 重试 / 放弃"。"""


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = PKCE_METHOD


@dataclass(frozen=True)
class CliGrant:
    token: str
    webapp_host: str
    session_id: str
    issued_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "token": self.token,
            "webapp_host": self.webapp_host,
            "session_id": self.session_id,
            "issued_at": self.issued_at,
        }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_pkce(size: int = PKCE_BYTES) -> PkcePair:
    """生成 PKCE verifier/challenge（S256）。

    ``size`` 是**字节数**，不是字符数：``size=32`` → 43 字符（RFC 的最小合法长度），
    自测里用它来钉住边界。
    """
    verifier = secrets.token_urlsafe(size)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


# ── redirect_uri 白名单 ─────────────────────────────────────────────────
def is_allowed_redirect(uri: str) -> bool:
    """与前端 ``ir()`` 逐条对齐：http + 127.0.0.1 + 路径恰好 /callback。"""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname == "127.0.0.1"
        and parts.path == "/callback"
    )


def build_redirect_url(redirect_uri: str, code: str, state: str | None = None) -> str:
    """拼回跳 URL（模拟真实 CLI 的本地回调）。

    🔴 原 ``redirect_uri`` 的 query/fragment 会被**丢弃** —— 与前端行为一致
       （``new URL(protocol//host+pathname)`` 之后只 set ``code``/``state``）。
    """
    if not is_allowed_redirect(redirect_uri):
        raise CliAuthError(
            f"redirect_uri 不在白名单（只允许 http://127.0.0.1:<port>/callback）: {redirect_uri!r}",
            code="bad_redirect",
        )
    parts = urlsplit(redirect_uri)
    params = {"code": code}
    if state:
        params["state"] = state
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(params), ""))


# ── JWT（只解码，不验签）─────────────────────────────────────────────────
def jwt_payload(token: str) -> dict:
    """解出 JWT payload。**不验签** —— 只用于读 session_id 做展示/自检。"""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def verify_cli_token(token: str) -> dict:
    """结构自检：三段 JWT + ``session_id`` 前缀。

    ⚠️ 这**不是**服务端校验。截至 2026-10-31 没找到能接受这个 token 的
    whoami 接口（``app.devin.ai/api/*`` 与 ``api.devin.ai/v1/*`` 都拒绝它），
    而 ``server.codeium.com`` 的 RPC 需要专用 body ⇒ 只能做结构校验。
    真正的"可用"由 CLI 自己验证；本函数只挡"拿到 200 但 body 是空/错形状"。
    """
    payload = jwt_payload(token)
    session_id = payload.get("session_id")
    ok = (
        (token or "").count(".") == 2
        and isinstance(session_id, str)
        and session_id.startswith(CLI_SESSION_PREFIX)
    )
    return {"ok": ok, "session_id": session_id or ""}


# ── 1. 换一次性 code ────────────────────────────────────────────────────
def _classify(exc: DevinError, *, endpoint: str) -> CliAuthError:
    detail = (exc.detail or "").lower()
    if endpoint == "authorize":
        if "no organizations found" in detail:
            return CliAuthError(
                "缺少 org 上下文：CLI 授权必须带 x-cog-org-id（先调 post-auth）",
                code="no_org", status=exc.status, detail=exc.detail)
        if "terms" in detail:
            return CliAuthError(
                "账号需要先接受条款（account_terms_required），请在 Web UI 同意后重试",
                code="terms_required", status=exc.status, detail=exc.detail)
        if exc.status == 403:
            return CliAuthError(
                "该账号没有 CLI 使用权限（需要 is_admin 或 can_use_cli）",
                code="not_eligible", status=exc.status, detail=exc.detail)
    if endpoint == "token" and exc.status in (400, 401):
        return CliAuthError(
            f"code 无效/已用过/已过期（5 分钟 TTL，且一次性）| 原文: {exc.detail}",
            code="invalid_code", status=exc.status, detail=exc.detail)
    return CliAuthError(str(exc), code=exc.code, status=exc.status, detail=exc.detail)


def authorize(devin: DevinClient, pkce: PkcePair, *, client: str | None = None) -> str:
    """用网页会话换 CLI 的一次性 code。``devin`` 必须已完成 post-auth。"""
    if not devin.session_token:
        raise CliAuthError("缺少会话 token（先登录）", code="unauthenticated")
    if not devin.org_id:
        raise CliAuthError(
            "缺少 org_id：先调 /api/users/post-auth，并带 x-cog-org-id",
            code="no_org")
    body: dict[str, str] = {
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if client:
        body["client"] = client
    try:
        data = devin.api_request("POST", "/auth/cli/authorize", json_body=body,
                                 base=config.DEVIN_API_ORIGIN)
    except DevinError as exc:
        raise _classify(exc, endpoint="authorize") from exc
    code = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
    if not code:
        raise CliAuthError(f"authorize 未返回 code: {data}", code="bad_response")
    return code


# ── 2. code + verifier → CLI token ──────────────────────────────────────
def exchange_code(code: str, verifier: str, *,
                  session: requests.Session | None = None) -> CliGrant:
    """换 CLI 会话 token。这是唯一能消费 code 的接口，且 code 一次性。"""
    s = session or requests.Session()
    s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
    try:
        r = s.post(f"{config.DEVIN_API_ORIGIN}/auth/cli/token",
                   json={"code": code, "code_verifier": verifier},
                   timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise CliAuthError(f"网络失败: {exc}", code="network") from exc

    if r.status_code >= 400:
        try:
            detail = str((r.json() or {}).get("detail") or r.text[:300])
        except ValueError:
            detail = (r.text or "")[:300]
        raise _classify(DevinError(f"HTTP {r.status_code}", code="http",
                                   status=r.status_code, detail=detail),
                        endpoint="token")

    try:
        data = r.json()
    except ValueError as exc:
        raise CliAuthError(f"响应不是 JSON: {r.text[:200]}", code="bad_response") from exc
    token = str((data or {}).get("token") or "")
    if not token:
        raise CliAuthError(f"token 响应缺 token: {data}", code="bad_response")
    info = verify_cli_token(token)
    return CliGrant(
        token=token,
        webapp_host=str(data.get("webapp_host") or ""),
        session_id=info["session_id"],
        issued_at=_now(),
    )


# ── 一步到位 ────────────────────────────────────────────────────────────
def cli_login(devin: DevinClient, *, client: str | None = None,
              pkce: PkcePair | None = None,
              session: requests.Session | None = None) -> CliGrant:
    """``authorize`` + ``exchange``。**code 拿到后立刻换，别落盘。**

    ``session`` 传 ``devin.s`` 即可让 token 交换复用同一条代理/连接 ——
    否则 exchange 会新建一个直连 Session，把"出口一致"这条约束破坏掉。
    """
    pair = pkce or generate_pkce()
    code = authorize(devin, pair, client=client)
    return exchange_code(code, pair.verifier, session=session)


def time_left_note() -> str:
    """给日志用的提醒文案（TTL 是站点侧硬限制，不随本地时间变）。"""
    return "code 一次性、5 分钟有效；拿到就换，不要缓存"
