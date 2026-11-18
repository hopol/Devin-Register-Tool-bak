"""Devin 站点客户端（纯 HTTP，无浏览器）。

契约全部来自 ``devin.ai.har`` + 2026-10-31 的真实复现，逐条如下：

    1. POST /api/auth1/email/start      {"email": …, "mode": "signup"}
       → {"ok": true, "message": "Code sent.", "email_verification_token": "eyJ…"}
       ⚠️ 少了 ``"mode":"signup"`` 会变成"登录"语义：新地址直接 400
          ``No account found for this email``，且**不发信**。
       ⚠️ 同一地址重复 start 会 429（per-email 冷却，换新地址立刻 200）。

    2. 邮件：发件人 ``*@*.cognition.ai``，主题 ``Your Devin Login Code``，正文 6 位码。

    3. POST /api/auth1/email/complete
       {"email": …, "email_verification_token": …, "code": "123456", "mode": "signup"}
       → {"token": "auth1_…", "user_id": "user-…", "email": …, "status": "authenticated"}
       🔴 响应**不 Set-Cookie**：会话就是响应里的 ``token``，
          后续所有请求走 ``Authorization: Bearer <token>``。

    4. POST /api/users/post-auth  {}
       → {"org_id": "org-…", "org_name": …}
       ⚠️ 这一步不可省：不调它，``/api/users/info`` 会 401
          ``No organizations found for auth1 user``。

    5. 组织作用域：``/api/users/info`` 必须再带 ``x-cog-org-id: org-…``。
       路径是 ``/api/{org_id}/...``（org_id 本身已含 ``org-`` 前缀，
       不要再拼一个 ``org-`` —— 拼错会得到 404 ``Organization not found``，
       而 404 很容易被误读成"接口下线"）。

    6. POST /api/{org_id}/{user_id}/api-key   （空 body）
       → {"api_key": "apk_user_…"}
       ⚠️ key **只在创建那一刻返回**。已存在时 ``/api-key/exists`` 为 true，
          但取不回旧 key ⇒ 只能标记 ``partial``，不能假装成功。
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from . import config

#: 已存在的 key 无法再取回 —— 这是站点设计，不是我们能绕的。
ERR_EXISTS = "api_key_exists"


class DevinError(RuntimeError):
    """带分类的失败：``code`` 供 stages 决定"重试 / 换路 / 放弃"。"""

    def __init__(self, message: str, *, code: str = "http",
                 status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽容转 float（账单字段可能是 null / 字符串 / 缺失）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _detail_of(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or body)[:300]
    return str(body)[:300]


class DevinClient:
    """一个实例 = 一条会话。批量跑时**每个 worker 一个实例**。"""

    def __init__(self, *, origin: str | None = None,
                 session: requests.Session | None = None):
        self.origin = (origin or config.DEVIN_ORIGIN).rstrip("/")
        self.session_token: str = ""
        self.org_id: str = ""
        self.user_id: str = ""
        self.email: str = ""
        self.s = session or requests.Session()
        self.s.headers.update({
            "User-Agent": config.UA,
            "Accept": "application/json",
            "Origin": self.origin,
            "Referer": f"{self.origin}/auth/signup?redirect=/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })

    # ── 底层 ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, json_body: Any = None,
                 base: str | None = None, auth: bool = False,
                 org_scope: bool = False, retries: int = 3,
                 backoff: float = 1.2) -> requests.Response:
        url = f"{(base or self.origin).rstrip('/')}{path}"
        headers: dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.session_token:
                raise DevinError("缺少会话 token", code="unauthenticated")
            headers["Authorization"] = f"Bearer {self.session_token}"
        if org_scope:
            if not self.org_id:
                raise DevinError("缺少 org_id", code="unauthenticated")
            headers["x-cog-org-id"] = self.org_id
            headers["x-cog-client"] = "web"

        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, url, headers=headers,
                                   data=None if json_body is None else json.dumps(json_body),
                                   timeout=config.HTTP_TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise DevinError(f"网络失败 {path}: {exc}", code="network") from exc

            if 500 <= r.status_code < 600:
                last = DevinError(f"服务端 {r.status_code}", code="http_5xx", status=r.status_code)
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise last
            return r
        raise DevinError(f"重试耗尽: {path} ({last})", code="http_5xx")

    def _json(self, method: str, path: str, **kw) -> dict[str, Any] | list[Any]:
        r = self._request(method, path, **kw)
        if r.status_code >= 400:
            detail = _detail_of(r)
            raise DevinError(_classify_message(r.status_code, detail),
                             code=_classify_code(r.status_code, detail),
                             status=r.status_code, detail=detail)
        try:
            return r.json()
        except ValueError as exc:
            raise DevinError(f"响应不是 JSON: {r.text[:200]}", code="bad_json") from exc

    # ── 0. 站点能力（诊断）────────────────────────────────────────
    def connections(self) -> dict[str, Any]:
        """站点开启了哪些登录方式（只读；``--doctor`` 用来确认 email 通道可用）。"""
        data = self._json("POST", "/api/auth1/connections",
                          json_body={"product": "devin"})
        if not isinstance(data, dict):
            raise DevinError(f"connections 响应形状异常: {data}", code="bad_response")
        return data

    # ── 1. 注册 + 发码 ────────────────────────────────────────────────
    def start_email(self, email: str) -> dict[str, Any]:
        """请求发送验证码。返回 ``{"email_verification_token": …}``。

        429 不在这里重试 —— 冷却退避策略属于 stages（要记账、要能中断）。
        """
        self.email = email
        body = {"email": email, "mode": config.DEVIN_SIGNUP_MODE}
        return self._json("POST", "/api/auth1/email/start", json_body=body)  # type: ignore[return-value]

    # ── 2. 用码换会话 ─────────────────────────────────────────────────
    def complete_email(self, email: str, ev_token: str, code: str) -> dict[str, Any]:
        body = {
            "email": email,
            "email_verification_token": ev_token,
            "code": code,
            "mode": config.DEVIN_SIGNUP_MODE,
        }
        data = self._json("POST", "/api/auth1/email/complete", json_body=body)
        assert isinstance(data, dict)
        token = str(data.get("token") or "")
        if not token:
            raise DevinError(f"complete 未返回 token: {data}", code="bad_response")
        self.session_token = token
        self.user_id = str(data.get("user_id") or "")
        self.email = str(data.get("email") or email)
        # 换一个"已登录"语义的 Referer，避免站点把后续请求当成注册页流量。
        self.s.headers["Referer"] = f"{self.origin}/"
        return data

    # ── 3. 建立组织上下文 ─────────────────────────────────────────────
    def api_request(self, method: str, path: str, *, json_body: Any = None,
                    base: str | None = None) -> dict[str, Any] | list[Any]:
        """通用**已鉴权 + org 作用域**请求。

        给扩展模块（如 ``cli_auth``）复用同一套重试/错误分类，避免再造一个
        HTTP 栈 —— 两套栈的 UA/重试策略一旦分叉，排查时很难发现。
        """
        return self._json(method, path, json_body=json_body, base=base,
                          auth=True, org_scope=True)

    def post_auth(self) -> dict[str, Any]:
        """会话 → 组织。返回 ``{"org_id","org_name",…}``。"""
        data = self._json("POST", "/api/users/post-auth", json_body={},
                          auth=True)
        assert isinstance(data, dict)
        self.org_id = str(data.get("org_id") or "")
        if not self.org_id:
            raise DevinError(f"post-auth 未返回 org_id: {data}", code="bad_response")
        return data

    # ── 4. 账号信息 ───────────────────────────────────────────────────
    def user_info(self) -> dict[str, Any]:
        return self._json("GET", "/api/users/info", auth=True, org_scope=True)  # type: ignore[return-value]

    def membership(self) -> dict[str, Any]:
        return self._json("GET", "/api/users/current-membership",
                          auth=True, org_scope=True)  # type: ignore[return-value]

    def toggleable_agent_versions(self, locale: str = "en") -> dict[str, Any]:
        """可切换的模型/agent 版本，以及 SWE-2 促销资格。

        实测（2026-10-31）：``swe2_promo_eligible`` 为 false 时，SWE-2 的
        75% 促销/角标不生效；但**能不能跑**取决于 ``billing_error``
        （零额度时起会话会 403 ``billing_restricted``，与模型无关）。
        """
        data = self._json("GET", f"/api/toggleable-agent-versions?locale={locale}",
                          auth=True, org_scope=True)
        if not isinstance(data, dict):
            raise DevinError(f"toggleable-agent-versions 响应形状异常: {data}",
                             code="bad_response")
        return data

    def organization(self) -> dict[str, Any]:
        return self._json("GET", f"/api/organizations/{self.org_id}",
                          auth=True, org_scope=True)  # type: ignore[return-value]

    def billing_status(self) -> dict[str, Any]:
        return self._json("GET", f"/api/{self.org_id}/billing/status",
                          auth=True, org_scope=True)  # type: ignore[return-value]

    def quota_usage(self) -> dict[str, Any]:
        return self._json("GET", f"/api/{self.org_id}/{self.user_id}/quota/usage",
                          auth=True, org_scope=True)  # type: ignore[return-value]

    def complete_onboarding(self) -> dict[str, Any]:
        """把组织从 ``in_progress`` 推到 ``completed``（一次空 POST）。

        不做也能用 API Key 调 ``/v1/sessions``（已实测），但 Web UI 会卡在
        onboarding 向导里 ⇒ 默认做，``--no-onboarding`` 可关。
        """
        data = self._json("POST", f"/api/organizations/{self.org_id}/onboarding/complete",
                          json_body={}, auth=True, org_scope=True)
        return data  # type: ignore[return-value]

    # ── 5. 建 API Key ─────────────────────────────────────────────────
    def api_key_exists(self) -> bool:
        data = self._json("GET", f"/api/{self.org_id}/{self.user_id}/api-key/exists",
                          auth=True, org_scope=True)
        return bool(isinstance(data, dict) and data.get("exists"))

    def create_api_key(self) -> str:
        data = self._json("POST", f"/api/{self.org_id}/{self.user_id}/api-key",
                          json_body=None, auth=True, org_scope=True)
        key = str((data or {}).get("api_key") or "") if isinstance(data, dict) else ""
        if not key:
            raise DevinError(f"建 key 未返回 api_key: {data}", code="bad_response")
        return key

    # ── 6. 账号信息汇总 ───────────────────────────────────────────────
    def account_snapshot(self, *, onboarding: bool = True,
                         include_user_info: bool = False) -> dict[str, Any]:
        """抓一份完整账号快照（"获取账号信息"阶段）。

        任何**可选**子接口失败都不应让整条链路失败：核心判据是
        ``user_id / org_id / account_id`` 有没有拿到，账单只是附赠信息。

        ⚡ 并行化（2026-10-31 压测优化）：``membership`` / ``billing`` 互不依赖，
        串行要 ~3.5s，并行后 ~1.3s（单号总耗时降 ~2s）。
        ``organization`` 不再单独请求 —— ``membership.org`` 已经包含
        ``account_id / created_at / onboarding_status / name``，多打一次纯属浪费。
        ``user_info`` 默认不取：它的字段（``product_onboarding_state`` 等）本工具不用。
        """
        snap: dict[str, Any] = {
            "user_id": self.user_id,
            "org_id": self.org_id,
            "email": self.email,
            "user_info": {},
            "warnings": [],
        }
        tasks = {
            "membership": self.membership,
            "billing": self.billing_status,
            # 模型可见性 / SWE-2 促销资格：决定"能不能选 SWE-2"，
            # 与 billing_error（能不能跑）是两件事，都要落库。
            "agent_versions": self.toggleable_agent_versions,
        }
        if include_user_info:
            tasks["user_info"] = self.user_info

        # 🔴 串行，不并行（2026-10-31 实测）：空闲时 membership+billing 并行要
        # 1499ms，串行只要 673ms —— 本机出口（代理/VPN）对同域并发连接会排队，
        # 并行只会把两个请求都拖慢。批次里更是 5 个 worker × 2 路 = 10 条连接。
        for name, fn in tasks.items():
            try:
                snap[name] = fn()
            except DevinError as exc:
                snap[name] = {}
                snap["warnings"].append(f"{name}: {exc}")

        memb = snap.get("membership") or {}
        org = (memb.get("org") or {}) if isinstance(memb, dict) else {}
        # 保持 ``organization`` 字段形状：下游（台账）读的是 ``account_id`` 等。
        snap["organization"] = org
        if isinstance(memb, dict):
            user = memb.get("user") or {}
            snap["user_email"] = user.get("email") or self.email
            snap["org_name"] = org.get("name") or snap.get("org_name", "")
        snap["account_id"] = org.get("account_id", "")
        snap["created_at"] = org.get("created_at") or ""
        billing = snap.get("billing") or {}
        snap["plan_slug"] = billing.get("plan_slug", "")
        snap["available_credits"] = _as_float(billing.get("available_credits"))
        # 🔴 三个"能不能用"的判据（2026-10-31 排查 SWE-2 不可用时补上）：
        #   billing_error       —— out_of_quota 时起会话直接 403 billing_restricted
        #   overage_credits     —— 免费试用/连接奖励到账的钱（HAR 账号有 ~9.93）
        #   swe2_promo_eligible —— SWE-2 促销资格（影响 75% off，不影响模型存在）
        snap["billing_error"] = str(billing.get("billing_error") or "")
        snap["overage_credits"] = _as_float(billing.get("overage_credits"))
        promo = snap.get("agent_versions") or {}
        snap["swe2_promo_eligible"] = bool(promo.get("swe2_promo_eligible"))
        snap["swe2_priority_eligible"] = bool(promo.get("swe2_priority_eligible"))
        snap["onboarding_status"] = org.get("onboarding_status", "")

        if onboarding and snap.get("onboarding_status") != "completed":
            try:
                self.complete_onboarding()
                snap["onboarding_status"] = "completed"
            except DevinError as exc:
                snap["warnings"].append(f"onboarding: {exc}")
        return snap


def _classify_code(status: int, detail: str) -> str:
    low = (detail or "").lower()
    if status == 429:
        return "rate_limited"
    if "no account found" in low:
        return "no_account"
    if "invalid or expired code" in low:
        return "invalid_code"
    if status == 401:
        return "unauthenticated"
    if status == 409:
        return "conflict"
    if status == 404:
        return "not_found"
    return "http"


def _classify_message(status: int, detail: str) -> str:
    """把站点原话翻成**能指导下一步**的一句话（不要吞掉原文）。"""
    code = _classify_code(status, detail)
    hints = {
        "rate_limited": "该地址发码冷却中（per-email，换地址立即生效）",
        "no_account": "该地址没有账号：start 缺少 mode=signup，或用了登录语义",
        "invalid_code": "验证码无效/过期（可能读到了旧邮件的码）",
        "unauthenticated": "会话无效：缺 Bearer token 或还没调 post-auth",
        "conflict": "资源已存在",
        "not_found": "接口 404：检查 org_id 是否被重复拼了 org- 前缀",
    }
    prefix = hints.get(code, f"HTTP {status}")
    return f"{prefix} | 原文: {detail}"


def verify_api_key(api_key: str, *, session: requests.Session | None = None) -> dict[str, Any]:
    """真打一次推理侧接口验证 key（``GET /v1/sessions``，只读、不产生会话）。

    兄弟项目用"真打推理接口"当验收判据；Devin 侧最便宜、最不会留下副作用的
    就是列会话：拿到 200 即证明 key 有效。
    """
    s = session or requests.Session()
    s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
    # 🔴 网络错误/5xx 重试：一次 SSL EOF 不该把有效 key 判成"无效"。
    #    2026-10-31 实测：91 把里 1 把因 SSL EOF（status=0）被误判，
    #    而 check_deliverables 会因此整批失败。
    last: requests.RequestException | None = None
    r: requests.Response | None = None
    for attempt in range(3):
        try:
            r = s.get(f"{config.DEVIN_API_ORIGIN}/v1/sessions",
                      params={"limit": 1},
                      headers={"Authorization": f"Bearer {api_key}"},
                      timeout=config.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"ok": False, "status": 0, "error": f"网络失败: {exc}"}
        if 500 <= r.status_code < 600 and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    if r is None:  # 理论不可达：循环要么 break 要么 return
        return {"ok": False, "status": 0, "error": f"网络失败: {last}"}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "error": _detail_of(r)}
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "status": r.status_code, "error": "响应不是 JSON"}
    return {
        "ok": True,
        "status": r.status_code,
        "sessions": len(body.get("sessions") or []) if isinstance(body, dict) else None,
    }
