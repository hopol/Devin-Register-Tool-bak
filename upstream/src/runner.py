"""批量调度：并发 / 台账 / 汇总。

分层与兄弟项目一致：

    runner.py   调度：批量 / 并发 / 汇总（不碰 HTTP 细节）
    stages.py   单账号：注册→发码→账号信息→建 Key→入库

并发要点
────────
每个 worker 一个独立的 ``Pipeline``（= 独立的 ``requests.Session`` +
独立的收件箱索引端点），只共享带锁的台账。这点很关键：Session 的 cookie jar
和 Devin 的 ``Bearer`` 头都是**按账号**的，复用会串号。

⚠️ 并发会放大**站点侧的 per-email 限流**：同一地址重复发码仍然 429。
   并发提高的是"同时处理多少个不同地址"，不是"同一地址能发多快"。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from . import config, ledger
from .pacer import StartPacer
from .proxy import Lease, ProxyPool
from .stages import Pipeline
from .tempemail import TempMailError, build_mail_client


@dataclass
class BatchSummary:
    total: int = 0
    keyed: int = 0
    registered: int = 0
    failed: int = 0
    elapsed: float = 0.0
    #: 开了 ``--cli-token`` 时：本批成功换到 CLI 会话的数量 / 期望数量。
    #: CLI 失败不算账号失败（key 已交付），但**验收口径要单独报**。
    cli_ok: int = 0
    cli_expected: int = 0
    #: 发码节流统计：命中 429 的次数、本批收敛到的放行速率、排队总时长。
    rate_limited: int = 0
    start_rate: float = 0.0
    pace_wait_seconds: float = 0.0
    #: 代理池统计（未启用时为空 dict）。
    proxy_pool: dict[str, int] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "keyed": self.keyed,
            "registered": self.registered,
            "failed": self.failed,
            "cli_ok": self.cli_ok,
            "cli_expected": self.cli_expected,
            "rate_limited": self.rate_limited,
            "start_rate": self.start_rate,
            "pace_wait_seconds": self.pace_wait_seconds,
            "proxy_pool": self.proxy_pool,
            "elapsed": round(self.elapsed, 2),
            # 速率必须与"本批失败数"一起报：只报速率的两批数字不可比。
            "success_rate": round(self.keyed / self.total, 3) if self.total else 0.0,
            "avg_seconds": round(self.elapsed / self.total, 2) if self.total else 0.0,
        }


class _Logger:
    """给并发日志加锁：不加锁时两行会交错，读起来像数据错乱。"""

    def __init__(self, sink: Callable[[str], None], enabled: bool = True):
        self._sink = sink
        self._lock = threading.Lock()
        self.enabled = enabled

    def __call__(self, msg: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._sink(msg)


class MailboxPrefetcher:
    """后台预建临时邮箱（流水线化）。

    实测 mailbox 阶段 1.1~1.4s。预建把这段从关键路径挪走；
    用独立直连 Session —— 收件箱只是 Worker 上的一条记录，与账号出口 IP 无关。
    """

    def __init__(self, backend: str | None, depth: int, log: Callable[[str], None]):
        self.q: queue.Queue[str] = queue.Queue(maxsize=max(1, depth))
        self._stop = threading.Event()
        self._log = log
        self.errors = 0
        self._backend = backend
        self._thread = threading.Thread(target=self._run, name="mailbox-prefetch",
                                        daemon=True)

    def start(self) -> MailboxPrefetcher:
        self._thread.start()
        return self

    def _run(self) -> None:
        client = None
        while not self._stop.is_set():
            try:
                if client is None:
                    client = build_mail_client(self._backend)
                email = client.create_mailbox()
            except (TempMailError, OSError, ValueError) as exc:
                self.errors += 1
                self._log(f"预建邮箱失败（{self.errors}）: {exc}")
                time.sleep(2)
                continue
            while not self._stop.is_set():
                try:
                    self.q.put(email, timeout=0.5)
                    break
                except queue.Full:
                    continue

    def take(self, timeout: float = 15.0) -> str | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def _is_network_failure(rec: Any) -> bool:
    """只有网络类失败才摘代理；业务错（429/无权限）不该怪代理。"""
    if getattr(rec, "status", "") != "failed":
        return False
    err = getattr(rec, "error", "") or ""
    return any(k in err for k in ("网络失败", "SSL", "ProxyError", "ConnectTimeout",
                                  "ReadTimeout", "ConnectionError"))


def run_batch(*, count: int = 1, concurrency: int = 1,
              mail_backend: str | None = None, onboarding: bool = True,
              cli_auth: bool = False,
              start_rate: float | None = None,
              pool: ProxyPool | None = None,
              prefetch: bool = True,
              cli_overlap: bool = True,
              emails: list[str] | None = None,
              log: Callable[[str], None] = print,
              pipeline_factory: Callable[..., Pipeline] | None = None,
              verbose: bool = True) -> BatchSummary:
    """跑 ``count`` 个新账号，或对 ``emails`` 里的地址补跑。

    ``cli_auth`` 打开后每个账号多跑一段 ``/auth/cli/authorize`` → ``/auth/cli/token``。
    CLI 失败**不**把账号判失败（key 已经交付），但会单独计数（``cli_ok``）。

    ``start_rate``（个/分钟）打开**整批共享**的发码节流（AIMD）。
    实测：不节流时 19/50 个新地址也吃 429，均值被拖到 29.5s；
    节流后服务耗时不受影响，排队时间记账到 ``pace_wait``。
    """
    targets: list[str | None] = list(emails) if emails else [None] * max(1, count)
    total = len(targets)
    concurrency = max(1, min(concurrency, total))
    logf = _Logger(log, enabled=verbose)
    if pool is not None:
        concurrency = min(concurrency, len(pool))
        logf(f"代理池：{len(pool)}/{len(pool.all)} 条可用，并发上限 {concurrency}")
    # 没显式给速率时按代理数放大（每条出口 ~8/min）。
    if start_rate is None:
        start_rate = config.START_RATE * (len(pool) if pool else 1)
    pacer = StartPacer(start_rate) if start_rate and start_rate > 0 else None
    if pacer is not None:
        logf(f"发码节流：起始 {pacer.rate:.0f} 个/分钟（AIMD "
             f"{pacer.min_rate:.0f}~{pacer.max_rate:.0f}）")

    prefetcher = None
    if prefetch and pipeline_factory is None and not emails:
        prefetcher = MailboxPrefetcher(mail_backend, concurrency * 2, logf).start()
        logf(f"邮箱预建：队列深度 {concurrency * 2}")

    built: list[Any] = []

    def _run_one(lease: Lease | None, email: str | None, ready: bool):
        factory = pipeline_factory or Pipeline
        pipe = factory(mail_backend=mail_backend, onboarding=onboarding,
                       cli_auth=cli_auth, pacer=pacer, log=logf,
                       session=lease.session if lease else None,
                       cli_overlap=cli_overlap)
        return pipe.run(email, mailbox_ready=ready)

    def one(email: str | None):
        pre = prefetcher.take() if prefetcher is not None else None
        target = email or pre
        ready = bool(email is None and pre)
        lease_cm = pool.lease() if pool is not None else nullcontext(None)
        try:
            with lease_cm as lease:
                rec = _run_one(lease, target, ready)
                if pool is not None and lease is not None:
                    if _is_network_failure(rec):
                        pool.mark_failure(lease.url)
                    else:
                        pool.mark_success(lease.url)
                return rec
        except Exception as exc:  # noqa: BLE001
            # 🔴 单账号的意外异常不能带走整批：
            # 一个 worker 挂了（例如后端配置写错），其余账号仍应跑完并落库。
            logf(f"[{target or 'new'}] ✗ worker 异常: {exc}")
            from .stages import AccountRecord
            return AccountRecord(email=target or "", status="failed",
                                 stage="worker", error=str(exc))

    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as workers:
            futures = {workers.submit(one, e): e for e in targets}
            for fut in as_completed(futures):
                built.append(fut.result())
    finally:
        if prefetcher is not None:
            prefetcher.close()
    elapsed = time.time() - started

    recs = [r.to_dict() if hasattr(r, "to_dict") else r for r in built]
    snap = pacer.snapshot() if pacer is not None else None
    summary = BatchSummary(
        total=total,
        keyed=sum(1 for r in recs if r.get("api_key")),
        registered=sum(1 for r in recs if r.get("user_id") and not r.get("api_key")),
        failed=sum(1 for r in recs if r.get("status") == "failed"),
        cli_ok=sum(1 for r in recs if r.get("cli_token")),
        cli_expected=total if cli_auth else 0,
        rate_limited=snap.penalties if snap else 0,
        start_rate=snap.rate_per_minute if snap else 0.0,
        pace_wait_seconds=snap.waited_seconds if snap else 0.0,
        proxy_pool=pool.snapshot() if pool is not None else {},
        elapsed=elapsed,
        records=recs,
    )
    if prefetcher is not None and prefetcher.errors:
        logf(f"预建邮箱失败 {prefetcher.errors} 次（已回退为账号内建箱）")
    return summary


def run_scan(*, limit: int = 50, mail_backend: str | None = None,
             log: Callable[[str], None] = print) -> list[str]:
    """``--mode scan``：列出 Worker 窗口内的邮件 + 未被规则命中的主题。

    只对"当前窗口"负责：CF Worker 的 retention 是全表 100 行，
    跑批期间每分钟几百封时，窗口只有十几分钟 ⇒ 扫不到更早的邮件是正常的。
    """
    from . import mailrules
    from .tempemail import build_mail_client

    client = build_mail_client(mail_backend)
    mails = client.scan_all(limit=limit)
    log(f"窗口内邮件 {len(mails)} 封")
    for m in mails:
        hit = any(r.matches(m) for r in mailrules.RULES.values())
        log(f"  {'✓' if hit else '✗'} {m.received_at} | {m.sender} | {m.subject}")
    return mailrules.diagnose(mails)


def ledger_rows() -> list[dict[str, Any]]:
    """按状态等级排序后的台账行（高等级在前，便于肉眼核对）。"""
    rows = list(ledger.load().values())
    rows.sort(key=lambda r: (-ledger.rank(r.get("status")), r.get("email") or ""))
    return rows


def summary_line(summary: BatchSummary) -> str:
    data = summary.to_dict()
    line = (
        f"共 {data['total']} 个：成功 {data['keyed']} / 已注册未出 key "
        f"{data['registered']} / 失败 {data['failed']}，"
        f"耗时 {data['elapsed']}s（平均 {data['avg_seconds']}s/个，"
        f"成功率 {data['success_rate']:.1%}）"
    )
    if data["cli_expected"]:
        line += f"；CLI 会话 {data['cli_ok']}/{data['cli_expected']}"
    if data["rate_limited"] or data["start_rate"]:
        line += (f"；429 重试 {data['rate_limited']} 次，"
                 f"收敛速率 {data['start_rate']} 个/分钟")
    return line
