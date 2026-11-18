"""代理池：每个账号固定一条出口，会话在代理上复用。

为什么需要"租约"而不是"轮询取一个 URL"
──────────────────────────────────────
1. **身份一致**：一个账号的 start / complete / post-auth / api-key / CLI 必须走
   同一条出口 —— 中途换 IP 会把"同一个人"拆成两段，风控更容易命中。
2. **并发上限 = 池大小**：``lease()`` 是排他的（同一条代理同一时刻只租给一个
   账号），否则 N+1 个 worker 共用 N 条代理，出口仍是 N 个，只是把队列堆在
   代理上，还会让"每 IP 速率"失控。
3. **Session 复用**：一条代理绑一个 ``requests.Session``，跨账号复用到
   app.devin.ai / api.devin.ai / Worker 的 TLS 连接（省握手，~0.3-0.6s/号）。
   租出前清 cookie，避免账号间串状态。

安全：``mask()`` 只保留 ``host:port``，密码永远不进日志/台账。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config


def _normalize(raw: str) -> str | None:
    """接受 ``host:port`` / ``http://user:pass@host:port``；非法返回 None。"""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or not parts.port:
        return None
    return raw


def mask(url: str) -> str:
    """脱敏代理地址。

    两种凭据形态都要盖住：
    - ``http://user:pass@host:port`` → 去掉 userinfo；
    - ``https://<长随机串>.provider.com:port`` → 凭据在**最左子域**里，
      只保留前 6 位 + ``…``（否则日志等于把账号密码打出来）。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<bad-proxy>"
    host = parts.hostname or "?"
    if parts.username is None:
        labels = host.split(".")
        if len(labels) > 2 and len(labels[0]) > 8:
            host = f"{labels[0][:6]}…{'.' + '.'.join(labels[1:])}"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def load_proxies(path: str | Path | None = None) -> list[str]:
    """从文件或 ``DEVIN_PROXIES`` 读代理列表（去重、保序）。"""
    path = Path(path or os.getenv("DEVIN_PROXY_FILE") or (config.ROOT / ".proxies"))
    raw: list[str] = []
    if path.is_file():
        raw = path.read_text(encoding="utf-8").splitlines()
    elif os.getenv("DEVIN_PROXIES"):
        raw = os.environ["DEVIN_PROXIES"].replace("\n", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for line in raw:
        norm = _normalize(line)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


@dataclass(frozen=True)
class Lease:
    url: str
    masked: str
    session: requests.Session


class ProxyPool:
    """排他租约 + 失败摘除 + Session 复用。"""

    def __init__(self, proxies: list[str], *,
                 max_consecutive_failures: int = 3,
                 log=print):
        if not proxies:
            raise ValueError("代理池为空")
        self.all = list(proxies)
        self.max_failures = max(1, int(max_consecutive_failures))
        self.log = log
        self._lock = threading.Lock()
        self._free: queue.Queue[str] = queue.Queue()
        self._sessions: dict[str, requests.Session] = {}
        for url in self.all:
            self._free.put(url)
            self._sessions[url] = self._new_session(url)
        self._failures: dict[str, int] = dict.fromkeys(self.all, 0)
        self._dead: set[str] = set()
        self.leases = 0
        self.failures = 0

    @staticmethod
    def _new_session(url: str) -> requests.Session:
        s = requests.Session()
        # 🔴 trust_env=False：避免环境变量里的 HTTP(S)_PROXY 再套一层
        # （本机有系统代理时，会把"代理→代理"叠起来，表现为随机超时）。
        s.trust_env = False
        s.proxies = {"http": url, "https": url}
        s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
        return s

    def __len__(self) -> int:
        return len(self.all) - len(self._dead)

    @contextmanager
    def lease(self, timeout: float = 600.0) -> Iterator[Lease]:
        """排他租一条代理。全部被占/被摘除时阻塞到 ``timeout``。

        ⚠️ 已摘除的代理可能**还留在队列里**（摘除发生在租出期间）⇒
        取到后要跳过，否则"摘除"只是记账、实际仍在用。
        """
        deadline = time.monotonic() + timeout
        url = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("代理池租约超时（全部代理被占用或已摘除）")
            try:
                candidate = self._free.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("代理池租约超时（全部代理被占用或已摘除）") from exc
            if candidate in self._dead:
                continue
            url = candidate
            break
        session = self._sessions[url]
        with self._lock:
            self.leases += 1
        session.cookies.clear()  # 账号之间不串 cookie
        try:
            yield Lease(url=url, masked=mask(url), session=session)
        finally:
            if url not in self._dead:
                self._free.put(url)

    def mark_success(self, url: str) -> None:
        with self._lock:
            self._failures[url] = 0

    def mark_failure(self, url: str) -> None:
        """连续失败 N 次 → 摘除，不参与后续轮转。"""
        with self._lock:
            self.failures += 1
            n = self._failures.get(url, 0) + 1
            self._failures[url] = n
            if n >= self.max_failures and url not in self._dead:
                self._dead.add(url)
                self.log(f"代理摘除：{mask(url)}（连续失败 {n} 次）")

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "proxies": len(self.all),
                "alive": len(self.all) - len(self._dead),
                "dead": len(self._dead),
                "leases": self.leases,
                "failures": self.failures,
            }

    # ── 体检（可选；跑批前用）──────────────────────────────────────────
    def health_check(self, url: str = "https://api.devin.ai/",
                     timeout: float = 20.0) -> dict[str, object]:
        """在**已租出**的会话上探活；返回状态码/耗时/出口 IP（不打印凭据）。"""
        import time

        with self.lease(timeout=10) as lease:
            started = time.time()
            try:
                r = lease.session.get(url, timeout=timeout)
                status = r.status_code
            except requests.RequestException as exc:
                self.mark_failure(lease.url)
                return {"proxy": lease.masked, "ok": False, "error": str(exc)[:120]}
            elapsed = round(time.time() - started, 2)
            ip = ""
            with suppress(requests.RequestException, ValueError):
                ip = lease.session.get("https://api.ipify.org?format=json",
                                       timeout=timeout).json().get("ip", "")
            if status >= 500:
                self.mark_failure(lease.url)
            else:
                self.mark_success(lease.url)
            return {"proxy": lease.masked, "ok": status < 500,
                    "status": status, "seconds": elapsed, "egress_ip": ip}
