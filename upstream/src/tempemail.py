"""CF Temp Email Worker 客户端。

关键设计（与兄弟项目一致，都是踩过坑换来的）：

- 收信走 **按收件人索引的端点** ``/api/inbox?email=``，不走 ``/admin/all``
  （拉最新 N 条再自己筛）。该 Worker 被同机其它项目共用，
  retention 是"全表 100 行"的窗口，拉列表会把我们的邮件挤出窗口。
- **5xx 要重试**：那是"服务端现在读不出来"，不是"邮件没到"。4xx 才立刻失败。
- 每次轮询打了几次接口、其中几次 5xx 都要计数并上报 —— 否则配额类故障只能靠猜。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from . import config


class TempMailError(RuntimeError):
    pass


@dataclass
class Stats:
    """只留**真的会被读**的计数器。

    ``polls`` / ``http_5xx`` 会出现在 stages 的超时信息里
    （``邮箱接口轮询 N 次，5xx M 次``）—— 那是"服务端读不出来"与
    "邮件没到"的唯一分界，而这两件事的处置恰好相反。
    """

    polls: int = 0
    http_5xx: int = 0


@dataclass
class Mail:
    id: str
    to: str
    sender: str
    subject: str
    body: str
    received_at: int

    @property
    def recipient(self) -> str:
        """收件人。``to`` 是内置名，用 ``recipient`` 读起来不歧义。"""
        return self.to


def _as_int(value: object) -> int:
    """收件时间戳宽容转换。

    Worker 正常返回毫秒整数（``received_at``），但那条 SQL 曾把历史行回填成
    ISO 字符串 ⇒ 这里读失败就归 0（排序靠前），**不抛异常**：
    一封时间戳坏掉的邮件不该让整批收信失败。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class TempMailClient:
    """CF Worker 后端。``create_mailbox()`` 免费，随便建。"""

    def __init__(self, base: str | None = None, admin_key: str | None = None,
                 domain: str | None = None, session: requests.Session | None = None):
        self.base = (base or config.TEMPMAIL_BASE).rstrip("/")
        self.admin_key = admin_key if admin_key is not None else config.TEMPMAIL_ADMIN_KEY
        self.domain = domain if domain is not None else config.TEMPMAIL_DOMAIN
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
        self.stats = Stats()

    # ── 底层 ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, retries: int = 4,
                 backoff: float = 0.8, **kw) -> requests.Response:
        """5xx 重试、4xx 立刻失败。"""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, f"{self.base}{path}",
                                   timeout=config.HTTP_TIMEOUT, **kw)
            except requests.RequestException as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(f"网络失败 {path}: {exc}") from exc

            if 500 <= r.status_code < 600:
                self.stats.http_5xx += 1
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(
                    f"邮箱服务持续 5xx（{self.stats.http_5xx} 次，最近 HTTP {r.status_code}）"
                    f"—— 不是邮件没到，是读不出来"
                )
            if 400 <= r.status_code < 500:
                raise TempMailError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
            return r
        raise TempMailError(f"重试耗尽: {path} ({last})")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    # ── 建邮箱 ────────────────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None) -> str:
        body: dict[str, Any] = {}
        target = domain or self.domain
        if target:
            body["domain"] = target
        r = self._request(
            "POST", "/api/mailboxes",
            headers={"Authorization": f"Bearer {self.admin_key}",
                     "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        data = r.json() or {}
        emails = data.get("emails") or []
        if not emails:
            raise TempMailError(f"建邮箱未返回地址: {data}")
        return str(emails[0])

    # ── 收信 ──────────────────────────────────────────────────────────
    def list_mails(self, email: str | None = None, limit: int | None = None) -> list[Mail]:
        """给了 email 走索引端点（读 0~2 行）；不给则退回 ``/admin/all``。"""
        if email:
            self.stats.polls += 1
            msgs = (self._request("GET", "/api/inbox",
                                  params={"email": email}).json() or {}).get("messages") or []
        else:
            self.stats.polls += 1
            params = {"limit": limit or 20}
            r = self._request("GET", "/admin/all", params=params,
                              headers={"Authorization": f"Bearer {self.admin_key}"})
            body = r.json() or {}
            msgs = body.get("messages") if isinstance(body, dict) else body
            msgs = msgs or []

        out: list[Mail] = []
        for m in msgs:
            out.append(Mail(
                id=str(m.get("id", "")),
                to=m.get("to_address") or m.get("to") or "",
                sender=m.get("from_address") or m.get("from") or "",
                subject=m.get("subject") or "",
                body=m.get("body") or m.get("text") or m.get("raw_text") or "",
                received_at=_as_int(m.get("received_at")),
            ))
        out.sort(key=lambda x: x.received_at)
        return out

    def wait_for_mail(self, email: str, match: Callable[[Mail], bool], *,
                      timeout: float = 300.0, interval: float | None = None,
                      since_ms: int | None = None,
                      first_interval: float = 0.3, first_polls: int = 3) -> Mail | None:
        """轮询等一封满足条件的邮件。

        ``since_ms`` 用于跳过历史邮件 —— 同一个地址可能已经收过旧邮件
        （补跑场景），不跳过会拿着上一轮的码去 complete。

        ⚡ 首轮快探：邮件实测 2~7s 到达，固定 1s 粒度会白等最多 1s。
        前 ``first_polls`` 次间隔 0.3s，之后回到 ``interval``（默认 1s）。
        """
        interval = config.CODE_POLL_INTERVAL if interval is None else interval
        deadline = time.time() + timeout
        polls = 0
        while time.time() < deadline:
            for m in self.list_mails(email=email):
                if since_ms is not None and m.received_at < since_ms:
                    continue
                if match(m):
                    return m
            polls += 1
            time.sleep(first_interval if polls <= first_polls else interval)
        return None

    def scan_all(self, limit: int = 100) -> list[Mail]:
        """扫整个窗口（不按收件人过滤）。只用于 ``--mode scan`` 诊断。

        ⚠️ 这条通路会占满窗口读取额度，**不要**用它做常规收信。
        """
        return self.list_mails(email=None, limit=limit)


def build_mail_client(backend: str | None = None, *,
                      session: requests.Session | None = None) -> TempMailClient:
    """按 ``--mail-backend`` 构造收信客户端。

    ⚠️ 目前 Devin 链路只实现了 ``cf``：Remail 侧没有 Devin 项目号
    （见 ``config.validate_remail`` 的实测记录），选了会显式报错，
    而不是跑到发码阶段才失败。
    """
    name = (backend or config.MAIL_BACKEND).lower()
    if name == "cf":
        return TempMailClient(session=session)
    if name == "remail":
        raise TempMailError(
            "Remail 后端暂不可用于 Devin：Remail 的 /v1/open/projects 里没有 "
            "Devin 项目（下单必须带 projectId）。请用 --mail-backend cf。"
        )
    raise TempMailError(f"未知邮箱后端: {name!r}（支持 cf）")
