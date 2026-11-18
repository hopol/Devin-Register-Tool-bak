#!/usr/bin/env python
"""全链路入口：注册 → 发码 → 账号信息 → 建 Key → 入库。

用法
────
    python tools/run_e2e.py --doctor                  # 环境体检（只读，不发信）
    python tools/run_e2e.py --count 1                 # ★ 跑一个完整账号
    python tools/run_e2e.py --count 5 --concurrency 3 # 并发
    python tools/run_e2e.py --mode resume --limit 10   # 补跑台账里没出 key 的地址
    python tools/run_e2e.py --mode scan --limit 30     # 诊断：看窗口内邮件与漏网主题

退出码：0 = 全部出 key；1 = 有失败 / 体检不过。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, ledger, runner  # noqa: E402
from src.devin import DevinClient, DevinError  # noqa: E402
from src.proxy import ProxyPool, load_proxies  # noqa: E402
from src.tempemail import TempMailError, build_mail_client  # noqa: E402


def _print(msg: str) -> None:
    print(msg, flush=True)


def _build_pool(args) -> ProxyPool | None:
    """按参数构造代理池；没有代理文件/显式关闭时返回 None（直连）。"""
    if getattr(args, "no_proxy", False):
        _print("代理：已禁用（--no-proxy），全部直连")
        return None
    proxies = load_proxies(getattr(args, "proxy_file", None))
    if not proxies:
        _print("代理：未配置（用 --proxy-file 或 DEVIN_PROXIES 开启）")
        return None
    pool = ProxyPool(proxies, log=_print)
    _print(f"代理池：{len(pool)} 条（凭据已隐藏）")
    return pool


def _doctor_proxies(pool: ProxyPool | None) -> int:
    """并行探活整池；逐条打印出口 IP 与耗时（不打印账号密码）。"""
    if pool is None:
        return 0
    _print("== 代理池 ==")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(10, len(pool.all))) as ex:
        rows = list(ex.map(lambda _u: pool.health_check(), range(len(pool.all))))
    bad = 0
    for row in rows:
        ok = bool(row.get("ok"))
        bad += 0 if ok else 1
        _print(f"  {'✓' if ok else '✗'} {row.get('proxy')} "
               f"status={row.get('status')} ip={row.get('egress_ip')} "
               f"{row.get('seconds')}s {row.get('error', '')}")
    return 0 if bad == 0 else 1


# ── 体检 ──────────────────────────────────────────────────────────────
def cmd_doctor(backend: str, pool: ProxyPool | None = None) -> int:
    """只读检查：配置 + 收信后端 + 站点可达性。**不发信、不建邮箱。**"""
    _print("== 配置 ==")
    missing = config.validate(backend=backend)
    if missing:
        _print(f"  ✗ 缺项: {', '.join(missing)}（见 .env.example）")
        return 1
    _print("  ✓ 必填项齐全")
    if backend == "remail":
        _print("  ✗ Devin 链路目前只支持 cf 后端（Remail 无 Devin 项目号）")
        return 1

    _print("== 收信后端 ==")
    try:
        client = build_mail_client(backend)
        health = client.health()
        domains = health.get("domains") or []
        _print(f"  ✓ {config.TEMPMAIL_BASE}（domains={len(domains)}，storage={health.get('storage')}）")
        if config.TEMPMAIL_DOMAIN and config.TEMPMAIL_DOMAIN not in domains:
            _print(f"  ✗ TEMPMAIL_DOMAIN={config.TEMPMAIL_DOMAIN!r} 不在 Worker 的 domains 里")
            return 1
    except (TempMailError, ValueError) as exc:
        _print(f"  ✗ {exc}")
        return 1

    _print("== Devin 站点 ==")
    try:
        devin = DevinClient()
        data = devin.connections()
        connections = data.get("connections") or []
        methods = sorted(str(c.get("id")) for c in connections if isinstance(c, dict))
        _print(f"  ✓ {config.DEVIN_ORIGIN}（connections={methods}）")
        if "email" not in methods:
            _print("  ✗ 站点没有开启 email 登录方式")
            return 1
    except DevinError as exc:
        _print(f"  ✗ {exc}")
        return 1

    _print("== 推理侧（用假 key 探活，期望 401）==")
    from src.devin import verify_api_key
    probe = verify_api_key("apk_user_invalid_probe")
    if probe.get("status") in (401, 403):
        _print(f"  ✓ {config.DEVIN_API_ORIGIN} 可达（假 key → {probe['status']}）")
    else:
        _print(f"  ✗ 不可达或行为异常: {probe}")
        return 1

    if _doctor_proxies(pool):
        _print("  ✗ 有代理不可用 —— 先换掉再跑批")
        return 1

    _print("\n体检通过。跑批： python tools/run_e2e.py --count 1")
    return 0


# ── 补跑清单 ──────────────────────────────────────────────────────────
def pending_emails(limit: int | None = None) -> list[str]:
    """没有 key、且**值得重试**的地址（最久没跑的优先）。

    ``retryable=False`` 的是“API Key 已存在却取不回来”这类**不可恢复**失败 ——
    重试只会再烧一次发码配额，必须排除。
    """
    rows = [r for r in ledger.load().values()
            if not r.get("api_key") and r.get("retryable", True)]
    rows.sort(key=lambda r: r.get("last_attempt") or "")
    emails = [r["email"] for r in rows if r.get("email")]
    if limit:
        emails = emails[:limit]
    return emails


def cmd_resume(limit: int | None, cli_auth: bool = False,
               start_rate: float | None = None,
               pool: ProxyPool | None = None) -> int:
    emails = pending_emails(limit)
    if not emails:
        _print("台账里没有待补跑的地址（都有 api_key 或已判失败）")
        return 0
    _print(f"补跑 {len(emails)} 个地址（同一地址重发码有 per-email 冷却，注意 429）")
    summary = runner.run_batch(emails=emails, concurrency=1, cli_auth=cli_auth,
                               start_rate=start_rate, pool=pool)
    return _report(summary, expect_cli=cli_auth)


# ── 汇总 ──────────────────────────────────────────────────────────────
def _report(summary: runner.BatchSummary, *, expect_cli: bool = False) -> int:
    _print("\n== 本批结果 ==")
    for rec in summary.records:
        state = "✓" if rec.get("api_key") else ("~" if rec.get("user_id") else "✗")
        extra = rec.get("error") or rec.get("org_id") or ""
        _print(f"  {state} {rec.get('email')} [{rec.get('status')}] {extra}")
        if expect_cli:
            if rec.get("cli_token"):
                cli = "✓ " + (rec.get("cli_session_id") or "")[:34]
            else:
                cli = "✗ " + (rec.get("cli_error") or "未取")
            _print(f"      CLI: {cli}")
    _print(runner.summary_line(summary))
    if summary.proxy_pool:
        p = summary.proxy_pool
        _print(f"代理池：{p.get('alive')}/{p.get('proxies')} 可用，"
               f"租约 {p.get('leases')} 次，网络失败 {p.get('failures')} 次")

    config.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary.to_dict(),
        "records": summary.records,
    }
    config.SUMMARY_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    _print(f"明细: {config.SUMMARY_JSON_PATH}")
    if summary.keyed:
        _print("验收/导出: python tools/verify_keys.py")
    if summary.keyed != summary.total:
        return 1
    # CLI 会话是本批的显式目标时，缺一条就算失败（否则“成功率 100%”会掩盖它）。
    if expect_cli and summary.cli_ok < summary.cli_expected:
        _print("⚠️ 有账号没换到 CLI 会话（key 已交付）；用 tools/cli_login.py 单独重试")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Devin 注册全链路")
    ap.add_argument("--doctor", action="store_true", help="环境体检（只读）")
    ap.add_argument("--count", type=int, default=1, help="跑多少个新账号")
    ap.add_argument("--concurrency", type=int, default=1, help="并发 worker 数")
    ap.add_argument("--mail-backend", default=config.MAIL_BACKEND, choices=["cf", "remail"])
    ap.add_argument("--no-onboarding", action="store_true", help="不调 onboarding/complete")
    ap.add_argument("--mode", default="run", choices=["run", "resume", "scan"])
    ap.add_argument("--limit", type=int, default=None, help="resume/scan 的条数上限")
    ap.add_argument("--cli-token", action="store_true",
                    help="出 key 后再换一条 CLI 会话（PKCE；失败不拖垮账号）")
    ap.add_argument("--start-rate", type=float, default=None,
                    help="发码节流（个/分钟）；不填则 START_RATE × 代理数，0=关闭")
    ap.add_argument("--proxy-file", default=None,
                    help="代理列表文件（默认 .proxies 或 DEVIN_PROXIES）")
    ap.add_argument("--no-proxy", action="store_true", help="禁用代理（全部直连）")
    ap.add_argument("--no-prefetch", action="store_true", help="关闭邮箱预建")
    ap.add_argument("--no-cli-overlap", action="store_true",
                    help="关闭 CLI authorize 与账号信息的并行")
    ap.add_argument("--quiet", action="store_true", help="只打结果，不打过程")
    args = ap.parse_args(argv)

    pool = _build_pool(args)

    if args.doctor:
        return cmd_doctor(args.mail_backend, pool=pool)
    if args.mode == "scan":
        missing = runner.run_scan(limit=args.limit or 50,
                                  mail_backend=args.mail_backend,
                                  log=_print)
        if missing:
            _print("\n未被规则命中的主题（按需补 mailrules.RULES）:")
            for line in missing:
                _print(f"  - {line}")
        return 0
    if args.mode == "resume":
        return cmd_resume(args.limit, cli_auth=args.cli_token,
                          start_rate=args.start_rate, pool=pool)

    missing = config.validate(backend=args.mail_backend)
    if missing:
        _print(f"缺配置: {', '.join(missing)} —— 先跑 --doctor")
        return 1
    summary = runner.run_batch(
        count=args.count,
        concurrency=args.concurrency,
        mail_backend=args.mail_backend,
        onboarding=not args.no_onboarding,
        cli_auth=args.cli_token,
        start_rate=args.start_rate,
        pool=pool,
        prefetch=not args.no_prefetch,
        cli_overlap=not args.no_cli_overlap,
        verbose=not args.quiet,
    )
    return _report(summary, expect_cli=args.cli_token)


if __name__ == "__main__":
    raise SystemExit(main())
