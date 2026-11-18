#!/usr/bin/env python
"""给 Devin CLI 发一个可用会话（PKCE 授权，纯 HTTP）。

用法
────
    # 复用台账里已有的账号：重新登录 → 换 CLI 会话
    python tools/cli_login.py --email a@b.com

    # 新注册一个账号并直接换 CLI 会话（= run_e2e --count 1 --cli-token）
    python tools/cli_login.py --new --count 1

    # 离线自检一个 token（只看结构，不打网络）
    python tools/cli_login.py --verify "eyJ..."

产物
────
``result/cli_sessions.jsonl``（一行一条：email + token + org/account + session_id）。
🔴 里面是**会话凭据**，能冒充账号 —— 默认落盘，``--no-save`` 可关；
   打印时 ``--mask`` 只显示前缀。

为什么 ``--email`` 要重新登录一次
────────────────────────────────
CLI 授权只认"活着"的 auth1 会话（``Authorization: Bearer auth1_…``）。
台账里只存 api_key（推理用），不存网页会话 —— 所以这里重发一次码、换一条新会话，
再调 ``/auth/cli/authorize``。这也是 429 唯一可能出现的环节。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import cli_auth, config, ledger, mailrules  # noqa: E402
from src.devin import DevinClient, DevinError  # noqa: E402
from src.tempemail import TempMailError, build_mail_client  # noqa: E402


def _mask(token: str) -> str:
    return f"{token[:18]}…（{len(token)} 字符，--mask 已隐藏）" if token else ""


def _save(entry: dict) -> None:
    config.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with config.CLI_SESSIONS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _reauth(email: str, backend: str | None) -> DevinClient:
    """重发码 → 收码 → complete → post-auth。返回已带 org 上下文的客户端。"""
    mail = build_mail_client(backend)
    devin = DevinClient()
    ev = devin.start_email(email)["email_verification_token"]
    print(f"[{email}] 验证码已发送，等邮件…", file=sys.stderr)
    msg = mail.wait_for_mail(
        email, mailrules.get("login_code").matches,
        timeout=config.CODE_TIMEOUT,
        since_ms=time.time_ns() // 1_000_000 - 5_000)
    if msg is None:
        raise TempMailError(
            f"{config.CODE_TIMEOUT:.0f}s 内没收到验证码"
            f"（轮询 {mail.stats.polls} 次，5xx {mail.stats.http_5xx} 次）")
    code, how = mailrules.extract_otp(msg.subject, msg.body)
    if not code:
        raise TempMailError(f"邮件里没找到 6 位码（subject={msg.subject!r}）")
    print(f"[{email}] 收到验证码（{how}），换会话…", file=sys.stderr)
    devin.complete_email(email, ev, code)
    devin.post_auth()
    return devin


def _for_email(email: str, *, backend: str | None, save: bool, mask: bool) -> int:
    devin = _reauth(email, backend)
    grant = cli_auth.cli_login(devin)
    row = ledger.load().get(email, {})
    entry = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "email": email,
        "user_id": devin.user_id or row.get("user_id", ""),
        "org_id": devin.org_id,
        "account_id": row.get("account_id", ""),
        "webapp_host": grant.webapp_host,
        "session_id": grant.session_id,
        "token": grant.token,
    }
    if save:
        _save(entry)
        # 同步回台账：只补 cli_* 字段（不覆盖 key/status，见 ledger._merge）。
        ledger.upsert_many([{
            "email": email,
            "cli_token": grant.token,
            "cli_webapp_host": grant.webapp_host,
            "cli_session_id": grant.session_id,
            "cli_token_at": grant.issued_at,
        }])
    print(json.dumps({**entry, "token": _mask(grant.token) if mask else grant.token},
                     ensure_ascii=False, indent=1))
    if save:
        print(f"\n已追加: {config.CLI_SESSIONS_PATH}", file=sys.stderr)
    return 0


def _for_new(count: int, *, backend: str | None, onboarding: bool,
             save: bool, mask: bool) -> int:
    from src import runner

    summary = runner.run_batch(
        count=count, concurrency=1, mail_backend=backend,
        onboarding=onboarding, cli_auth=True, verbose=True)
    ok = [r for r in summary.records if r.get("cli_token")]
    for rec in summary.records:
        if not rec.get("cli_token"):
            print(f"✗ {rec.get('email')}: {rec.get('cli_error') or rec.get('error')}",
                  file=sys.stderr)
            continue
        entry = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "email": rec["email"],
            "user_id": rec.get("user_id", ""),
            "org_id": rec.get("org_id", ""),
            "account_id": rec.get("account_id", ""),
            "webapp_host": rec.get("cli_webapp_host", ""),
            "session_id": rec.get("cli_session_id", ""),
            "token": rec["cli_token"],
        }
        if save:
            _save(entry)
        print(json.dumps({**entry,
                          "token": _mask(rec["cli_token"]) if mask else rec["cli_token"]},
                         ensure_ascii=False, indent=1))
    if save and ok:
        print(f"\n已追加 {len(ok)} 条: {config.CLI_SESSIONS_PATH}", file=sys.stderr)
    print(runner.summary_line(summary), file=sys.stderr)
    return 0 if ok and len(ok) == len(summary.records) else 1


def _verify(token: str) -> int:
    info = cli_auth.verify_cli_token(token)
    payload = cli_auth.jwt_payload(token)
    print(json.dumps({"ok": info["ok"], "session_id": info["session_id"],
                      "claims": payload}, ensure_ascii=False, indent=1))
    if not info["ok"]:
        print("✗ 结构不对：需要三段 JWT，且 payload.session_id 以 "
              f"{cli_auth.CLI_SESSION_PREFIX!r} 开头", file=sys.stderr)
        return 1
    print("✓ 结构有效（注意：这只是本地结构校验，服务端可用性由 CLI 侧决定）",
          file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给 Devin CLI 发一个可用会话")
    ap.add_argument("--email", help="复用已有账号（重新登录后授权）")
    ap.add_argument("--new", action="store_true", help="新注册账号并授权")
    ap.add_argument("--count", type=int, default=1, help="--new 时注册几个")
    ap.add_argument("--verify", metavar="TOKEN", help="离线结构自检")
    ap.add_argument("--mail-backend", default=config.MAIL_BACKEND, choices=["cf", "remail"])
    ap.add_argument("--no-onboarding", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="不写 result/cli_sessions.jsonl")
    ap.add_argument("--mask", action="store_true", help="打印时隐藏 token")
    args = ap.parse_args(argv)

    try:
        if args.verify:
            return _verify(args.verify)
        if args.email:
            return _for_email(args.email, backend=args.mail_backend,
                              save=not args.no_save, mask=args.mask)
        if args.new:
            missing = config.validate(backend=args.mail_backend)
            if missing:
                print(f"缺配置: {', '.join(missing)} —— 先跑 --doctor", file=sys.stderr)
                return 1
            return _for_new(args.count, backend=args.mail_backend,
                            onboarding=not args.no_onboarding,
                            save=not args.no_save, mask=args.mask)
        ap.print_help()
        return 2
    except cli_auth.CliAuthError as exc:
        print(f"✗ CLI 授权失败 [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    except (DevinError, TempMailError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
