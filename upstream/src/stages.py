"""阶段层：单个账号从"注册 / 发码"到"拿到 Key 并入库"的全部出网动作。

与 ``runner.py`` 的分工：

    runner.py   调度：批量 / 并发 / 汇总
    stages.py   单账号：mailbox → send_code → wait_code → complete
                       → account（账号信息）→ api_key → 入库

四个阶段与站点判据：

    1. 注册 + 发码   POST /api/auth1/email/start {email, mode:"signup"}
                     → 200 + email_verification_token；邮箱收到 6 位码
    2. 换会话        POST /api/auth1/email/complete {email, ev_token, code, mode}
                     → 200 + auth1_… token + user_id
    3. 账号信息      POST /api/users/post-auth → org_id
                     GET  /api/users/current-membership / organizations/{org}
    4. 建 Key + 入库 POST /api/{org}/{user}/api-key → apk_user_…
                     写 exports/ledger.jsonl + result/success.jsonl

⚠️ 判据只有一条：**建 key 有没有回 key**。账号信息拿到了但 key 没拿到，
   状态是 ``partial``，不是成功 —— 后者会污染交付物。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from . import cli_auth, config, ledger, mailrules
from .devin import DevinClient, DevinError
from .pacer import StartPacer
from .tempemail import TempMailClient, TempMailError, build_mail_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class AccountRecord:
    """一条账号记录。字段名即台账 schema —— 改名等于改交付物格式。"""

    email: str = ""
    status: str = "pending"      # mailbox / code_sent / registered / keyed / partial / failed
    stage: str = ""              # 最后进入的阶段
    error: str = ""
    mail_backend: str = "cf"

    # ── 站点侧身份 ──
    user_id: str = ""
    org_id: str = ""
    org_name: str = ""
    account_id: str = ""
    plan_slug: str = ""
    available_credits: float = 0.0
    #: 账单/模型可用性判据（2026-10-31 新增）：
    #: ``billing_error`` 非空（如 out_of_quota）⇒ 起任何会话都会 403。
    billing_error: str = ""
    overage_credits: float = 0.0
    swe2_promo_eligible: bool = False
    swe2_priority_eligible: bool = False
    onboarding_status: str = ""
    site_created_at: str = ""

    # ── 凭据（交付物）──
    api_key: str = ""

    # ── CLI 会话（可选，``--cli-token`` 才写）──
    # 🔴 这是会话凭据（能冒充该账号的 CLI），比 api_key 更敏感：
    #    默认不取、不落盘，只有显式开启时才进台账。
    cli_token: str = ""
    cli_webapp_host: str = ""
    cli_session_id: str = ""
    cli_token_at: str = ""
    cli_error: str = ""

    # ── 运行元信息 ──
    started_at: str = ""
    last_attempt: str = ""
    elapsed: float = 0.0
    #: 这条记录值不值得补跑。API Key 已存在时是 False ——
    #: 那是站点设计导致的**不可恢复**失败，重试只会再花一次发码配额。
    retryable: bool = True
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    """单账号流水线。**不要跨线程复用**：每个 worker 一个实例。"""

    def __init__(self, *, mail_backend: str | None = None,
                 onboarding: bool = True, cli_auth: bool = False, log=print,
                 pacer: StartPacer | None = None,
                 session: requests.Session | None = None,
                 cli_overlap: bool = True,
                 mail: TempMailClient | None = None,
                 devin: DevinClient | None = None):
        self.backend = (mail_backend or config.MAIL_BACKEND).lower()
        self.onboarding = onboarding
        #: 是否在出 key 之后再换一条 CLI 会话（``POST /auth/cli/authorize``）。
        #: 失败**不拖垮账号**：key 已经交付，CLI 只是附加能力（见 _stage_cli_session）。
        self.with_cli = cli_auth
        #: 整批共享的发码节流器。``None`` = 不节流（旧的"硬并发"行为）。
        #: 压测结论：不节流时 19/50 会命中 429，均值 29.5s；节流后见 batch_report。
        self.pacer = pacer
        self.log = log
        #: 代理池租约给的 Session（没有代理时为 None）。三个出网客户端共用它，
        #: 保证同一账号的 start/complete/post-auth/api-key/CLI 走同一条出口。
        self.session = session
        #: CLI authorize 与账号信息并行（同一个 Session，两条连接）。
        self.cli_overlap = cli_overlap
        self.mail = (mail if mail is not None
                     else build_mail_client(self.backend, session=session))
        self.devin = devin if devin is not None else DevinClient(session=session)
        #: CLI 并行的中间态（每次 run 重置）。
        self._cli_code = ""
        self._cli_pkce = None
        self._cli_error = ""
        self._t0 = 0.0
        self._last = 0.0
        #: 发码阶段返回的 ``email_verification_token``。
        #: 🔴 必须与**同一轮**的码配对：补跑路径（complete 失败后重发）也要配对，
        #:    所以由 ``_stage_send_code`` 统一落库，不在调用点手工赋值。
        self._ev_token = ""

    # ── 工具 ──────────────────────────────────────────────────────────
    def _enter(self, rec: AccountRecord, stage: str) -> None:
        now = time.time()
        if self._last:
            rec.timings[rec.stage or "start"] = round(now - self._last, 2)
        self._last = now
        rec.stage = stage
        rec.last_attempt = _now()
        self.log(f"[{rec.email or 'new'}] → {stage}")

    def _done(self, rec: AccountRecord) -> None:
        now = time.time()
        if self._last:
            rec.timings[rec.stage] = round(now - self._last, 2)
        rec.elapsed = round(now - self._t0, 2)
        rec.status = "keyed" if rec.api_key else (
            "registered" if rec.user_id else rec.status)
        ledger.persist(rec.to_dict())
        self.log(f"[{rec.email}] ✓ {rec.status} ({rec.elapsed}s)")

    def _fail(self, rec: AccountRecord, exc: Exception, *, status: str = "failed") -> AccountRecord:
        rec.status = status
        rec.error = f"{rec.stage}: {exc}"
        rec.elapsed = round(time.time() - self._t0, 2)
        if isinstance(exc, DevinError) and exc.code == "api_key_exists":
            rec.retryable = False
        ledger.persist(rec.to_dict())
        self.log(f"[{rec.email}] ✗ {rec.status} — {rec.error}")
        return rec

    # ── 主流程 ────────────────────────────────────────────────────────
    def run(self, email: str | None = None, *,
            mailbox_ready: bool = False) -> AccountRecord:
        """``email`` 为空 = 新注册；给了地址 = 补跑/使用预建收件箱。"""
        rec = AccountRecord(email=email or "", mail_backend=self.backend,
                            started_at=_now())
        self._t0 = self._last = time.time()
        self._cli_code = ""
        self._cli_pkce = None
        self._cli_error = ""

        try:
            self._stage_mailbox(rec, ready=mailbox_ready)
            self._stage_send_code(rec)
            code = self._stage_wait_code(rec)
            self._stage_complete(rec, code)
            self._stage_account(rec)
            self._stage_api_key(rec)
            if self.with_cli:
                self._stage_cli_session(rec)
        except DevinError as exc:
            return self._fail(rec, exc)
        except TempMailError as exc:
            return self._fail(rec, exc)
        except KeyboardInterrupt:
            rec.status, rec.error = "failed", "interrupted"
            ledger.persist(rec.to_dict())
            raise

        self._done(rec)
        return rec

    # ── 1. 收件箱 ─────────────────────────────────────────────────────
    def _stage_mailbox(self, rec: AccountRecord, *, ready: bool = False) -> None:
        self._enter(rec, "mailbox")
        if rec.email:
            self.log(f"[{rec.email}] {'使用预建收件箱' if ready else '复用已有收件箱（补跑）'}")
        else:
            rec.email = self.mail.create_mailbox()
        rec.status = "mailbox"
        ledger.persist(rec.to_dict())

    # ── 2. 发码 ───────────────────────────────────────────────────────
    def _stage_send_code(self, rec: AccountRecord) -> dict[str, Any]:
        self._enter(rec, "send_code")
        last: DevinError | None = None
        retries = int(config.START_RETRIES)
        for attempt in range(retries):
            if self.pacer is not None:
                # 等待计入 elapsed，但单独记 ``pace_wait`` —— 报告里要区分
                # "服务耗时"与"排队耗时"（否则节流后的均值会被误读为变慢）。
                wait = self.pacer.acquire()
                rec.timings["pace_wait"] = round(
                    rec.timings.get("pace_wait", 0.0) + wait, 2)
            try:
                data = self.devin.start_email(rec.email)
                if self.pacer is not None:
                    self.pacer.reward()
                self._ev_token = str(data.get("email_verification_token") or "")
                if not self._ev_token:
                    raise DevinError(f"start 未返回 email_verification_token: {data}",
                                     code="bad_response")
                rec.status = "code_sent"
                ledger.persist(rec.to_dict())
                return data
            except DevinError as exc:
                last = exc
                if exc.code == "rate_limited" and self.pacer is not None:
                    self.pacer.penalize()
                if exc.code != "rate_limited" or attempt == config.START_RETRIES - 1:
                    break
                wait = config.START_BACKOFF * (attempt + 1)
                self.log(f"[{rec.email}] 发码 429，{wait:.0f}s 后重试 "
                         f"（{attempt + 1}/{config.START_RETRIES - 1}）")
                time.sleep(wait)
        assert last is not None
        raise last

    # ── 3. 等码 ───────────────────────────────────────────────────────
    def _stage_wait_code(self, rec: AccountRecord) -> str:
        self._enter(rec, "wait_code")
        # 跳过本轮之前的历史邮件：补跑时旧码仍在收件箱里，用错就是 401。
        since_ms = time.time_ns() // 1_000_000 - 5_000
        rule = mailrules.get("login_code")
        mail = self.mail.wait_for_mail(
            rec.email, rule.matches, timeout=config.CODE_TIMEOUT, since_ms=since_ms)
        if mail is None:
            stats = self.mail.stats
            raise TempMailError(
                f"{config.CODE_TIMEOUT:.0f}s 内没等到验证码邮件"
                f"（轮询 {stats.polls} 次，5xx {stats.http_5xx} 次）"
            )
        code, how = mailrules.extract_otp(mail.subject, mail.body)
        if not code:
            raise TempMailError(f"邮件里没找到 6 位码（how=none，subject={mail.subject!r}）")
        self.log(f"[{rec.email}] 收到验证码（{how}）")
        return code

    # ── 4. 换会话 ─────────────────────────────────────────────────────
    def _stage_complete(self, rec: AccountRecord, code: str) -> None:
        self._enter(rec, "complete")
        try:
            data = self.devin.complete_email(rec.email, self._ev_token, code)
        except DevinError as exc:
            if exc.code != "invalid_code":
                raise
            # 码过期/被后一条覆盖：只重试一次，且必须重新发码（旧码已废）。
            # 这里不吞掉 429：重发被限流时应当带着"冷却中"直接失败，
            # 而不是再等 3 分钟让调用方以为流程卡死。
            self.log(f"[{rec.email}] 验证码失效，重发一次")
            fresh = self._stage_send_code(rec)
            code = self._stage_wait_code(rec)
            data = self.devin.complete_email(rec.email, fresh["email_verification_token"], code)
        rec.user_id = str(data.get("user_id") or "")
        rec.email = str(data.get("email") or rec.email)
        rec.status = "registered"
        ledger.persist(rec.to_dict())

    # ── 5. 账号信息 ───────────────────────────────────────────────────
    def _stage_account(self, rec: AccountRecord) -> None:
        self._enter(rec, "account")
        auth = self.devin.post_auth()
        rec.org_id = str(auth.get("org_id") or "")
        rec.org_name = str(auth.get("org_name") or "")
        snap = self._snapshot_with_cli()
        rec.account_id = str(snap.get("account_id") or "")
        rec.plan_slug = str(snap.get("plan_slug") or "")
        rec.available_credits = _as_float(snap.get("available_credits"))
        rec.billing_error = str(snap.get("billing_error") or "")
        rec.overage_credits = _as_float(snap.get("overage_credits"))
        rec.swe2_promo_eligible = bool(snap.get("swe2_promo_eligible"))
        rec.swe2_priority_eligible = bool(snap.get("swe2_priority_eligible"))
        rec.onboarding_status = str(snap.get("onboarding_status") or "")
        rec.site_created_at = str(snap.get("created_at") or "")
        rec.warnings.extend(snap.get("warnings") or [])
        ledger.persist(rec.to_dict())

    def _snapshot_with_cli(self) -> dict[str, Any]:
        """账号信息 + （可选）并行预热 CLI authorize。

        为什么能并行：``authorize`` 只依赖 org（``post_auth`` 已拿到），
        不依赖 membership/billing。等快照返回后再换 token 即可。
        失败只写 ``_cli_error``，不影响账号信息本身。
        """
        if not (self.with_cli and self.cli_overlap):
            return self.devin.account_snapshot(onboarding=self.onboarding)

        self._cli_pkce = cli_auth.generate_pkce()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(cli_auth.authorize, self.devin, self._cli_pkce)
        try:
            snap = self.devin.account_snapshot(onboarding=self.onboarding)
            try:
                self._cli_code = future.result(timeout=60)
            except Exception as exc:  # noqa: BLE001
                self._cli_error = f"{getattr(exc, 'code', 'error')}: {exc}"
            return snap
        finally:
            # 不等待：authorize 卡住时不能拖住账号信息阶段。
            pool.shutdown(wait=False)

    # ── 6. 建 Key ─────────────────────────────────────────────────────
    def _stage_api_key(self, rec: AccountRecord) -> None:
        self._enter(rec, "api_key")
        if self.devin.api_key_exists():
            # 🔴 key 只在创建那一刻返回 ⇒ 已存在时**取不回来**。
            # 不能"重新创建"（站点不会给第二把），也不能假装成功。
            raise DevinError(
                "该账号已有 API Key，站点不再返回明文 ⇒ 无法交付这把 key。"
                " 处置：换新地址重跑；或到 Web UI 手动吊销旧 key 后补跑。",
                code="api_key_exists",
            )
        rec.api_key = self.devin.create_api_key()
        rec.status = "keyed"
        ledger.persist(rec.to_dict())

    # ── 7. CLI 会话（可选）────────────────────────────────────────────
    def _stage_cli_session(self, rec: AccountRecord) -> None:
        """换一条 CLI 会话 token（PKCE）。

        🔴 失败**不让账号失败**：api_key 已经拿到并落库，CLI 只是附加能力。
           把失败写进 ``cli_error``，由 runner 汇总成 ``cli_ok`` 计数；
           ``tools/cli_login.py`` 才是“必须成功”的严格入口。
        """
        self._enter(rec, "cli_session")
        if self._cli_error and not self._cli_code:
            rec.cli_error = self._cli_error
            ledger.persist(rec.to_dict())
            self.log(f"[{rec.email}] ! CLI 预热失败（不影响 key）: {rec.cli_error}")
            return
        try:
            if self._cli_code and self._cli_pkce is not None:
                grant = cli_auth.exchange_code(
                    self._cli_code, self._cli_pkce.verifier, session=self.devin.s)
            else:
                grant = cli_auth.cli_login(self.devin, session=self.devin.s)
        except (cli_auth.CliAuthError, DevinError) as exc:
            # 带上错误分类码：``no_org`` / ``not_eligible`` / ``invalid_code``
            # 这三种的处置完全不同，只留自然语言会在补跑时丢信息。
            rec.cli_error = f"{exc.code}: {exc}"
            ledger.persist(rec.to_dict())
            self.log(f"[{rec.email}] ! CLI 授权失败（不影响 key）: {rec.cli_error}")
            return
        rec.cli_token = grant.token
        rec.cli_webapp_host = grant.webapp_host
        rec.cli_session_id = grant.session_id
        rec.cli_token_at = grant.issued_at
        ledger.persist(rec.to_dict())
        self.log(f"[{rec.email}] ✓ CLI 会话（{grant.session_id[:28]}…）")
