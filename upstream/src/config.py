"""集中配置。

凭据一律从环境变量读取，默认值留空 —— 避免 ``os.getenv(k, "真实值")`` 这种泄漏点。
用 stdlib 实现的 .env 加载器，不引 python-dotenv。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器：真实环境变量优先级更高。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _num(name: str, default: float, cast=float) -> float:
    """读数值型 env；坏值回退默认值，**不在 import 期抛异常**。

    ``.env`` 是手写的，写错一个 ``DEVIN_CODE_TIMEOUT=3m`` 不该让
    ``--help`` 都跑不起来 —— 这类错应该在 ``--doctor`` 里以可读信息出现。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return cast(default)


def _num_int(name: str, default: int) -> int:
    """整数型 env（``range()`` 之类要求的真是 int，不是 float）。"""
    try:
        return int(_num(name, float(default)))
    except (TypeError, ValueError, OverflowError):
        return default


_load_dotenv(ROOT / ".env")

# ── 收信后端（默认 CF Temp Email Worker）────────────────────────────────
# 🔴 三个都不留真实默认值：Worker 子域 + 自有邮箱域名单独看不致命，
#    合起来足以被针对性打击，且一旦公开就永久公开。缺项由 `validate()` 显式报出。
MAIL_BACKEND = os.getenv("DEVIN_MAIL_BACKEND", "cf")
TEMPMAIL_BASE = os.getenv("TEMPMAIL_BASE", "")
TEMPMAIL_ADMIN_KEY = os.getenv("TEMPMAIL_ADMIN_KEY", "")
TEMPMAIL_DOMAIN = os.getenv("TEMPMAIL_DOMAIN", "")

# ── Devin 站点 ──────────────────────────────────────────────────────────
# 契约来自 devin.ai.har（详见 docs/architecture.md）：
#   1. POST /api/auth1/email/start      {email, mode:"signup"}
#   2. 收信取 6 位码（Subject: Your Devin Login Code）
#   3. POST /api/auth1/email/complete    {email, email_verification_token, code, mode:"signup"}
#   4. POST /api/users/post-auth         -> org_id / org_name
#   5. GET  /api/users/current-membership / /api/users/info
#   6. POST /api/{org_id}/{user_id}/api-key -> apk_user_...
DEVIN_ORIGIN = "https://app.devin.ai"
DEVIN_API_ORIGIN = "https://api.devin.ai"
DEVIN_SIGNUP_MODE = os.getenv("DEVIN_SIGNUP_MODE", "signup")

# ── 网络 ────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = _num("DEVIN_HTTP_TIMEOUT", 30)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# ── 流程节拍（都是实测值，不是拍脑袋）──────────────────────────────────
#: 等验证码邮件的总时长。实测投递 2~10s；给 3 分钟是为了覆盖站点排队，
#: 而不是让我们多等 —— 超时后的处置是"重发"（有 per-email 冷却，见下）。
CODE_TIMEOUT = _num("DEVIN_CODE_TIMEOUT", 180)
#: 收件箱轮询间隔。Worker 的 retention 是"全表 100 行"，
#: 拉得勤一点是为了**尽快读走**，不是为了"更早发现"。
#: 2026-10-31 压测：从 2s 降到 1s，wait_code 均值 2.77s → ~1.8s；
#: 5 并发 = 5 req/s，对 Worker 毫无压力。
CODE_POLL_INTERVAL = _num("DEVIN_CODE_POLL_INTERVAL", 1)
#: 同一地址重发验证码的 429 冷却退避（秒）。
#: 🔴 实测 429 是 **per-email** 的（换一个新地址立刻 200），
#:    所以退避上限不必很长；但也不要 1s 一次地打，否则只会续上冷却。
START_RETRIES = _num_int("DEVIN_START_RETRIES", 3)
START_BACKOFF = _num("DEVIN_START_BACKOFF", 25)

# ── 发码节流（AIMD）────────────────────────────────────────────────────
# 2026-10-31 三轮实测（同样含 CLI 阶段）：
#   50 号 / 并发5 / 不节流：19 次 429，服务耗时 15.26s，吞吐 9.96/min
#   20 号 / 并发5 / 起手20 ： 5 次 429，服务耗时 13.28s，吞吐 7.06/min（回退过猛）
#   15 号 / 并发4 / 起手8  ： **0 次 429**，服务耗时 12.87s，吞吐 8.27/min（收敛到 11/min）
# ⇒ 默认起手 8、回退 ×0.8、每 5 次成功 +1；让它自己爬升到站点阈值。
#   设 ``DEVIN_START_RATE=0`` 可关闭节流（回到硬并发，吞吐略高但 429 长尾严重）。
START_RATE = _num("DEVIN_START_RATE", 8)
PACER_MIN = _num("DEVIN_PACER_MIN", 3)
PACER_MAX = _num("DEVIN_PACER_MAX", 30)

# ── 输出 ────────────────────────────────────────────────────────────────
# 两类产物分开放（与兄弟项目同一约定）：
#   exports/  运行台账 —— 含全部尝试（失败也留），便于复盘
#   result/   成功数据 —— 交付物
EXPORT_DIR = ROOT / "exports"
LEDGER_PATH = EXPORT_DIR / "ledger.jsonl"

RESULT_DIR = ROOT / "result"
SUCCESS_PATH = RESULT_DIR / "success.jsonl"      # 成功账号（账号级，按 email 去重）
KEYS_TXT_PATH = RESULT_DIR / "keys.txt"          # email----api_key----user_id----org_id
APIKEYS_TXT_PATH = RESULT_DIR / "apikeys.txt"    # 纯 api_key，一行一个
ACCOUNTS_CSV_PATH = RESULT_DIR / "accounts.csv"  # 账号信息明细
#: CLI 会话台账（``--cli-token`` / ``tools/cli_login.py`` 写）。
#: 🔴 里面是**会话凭据**（能冒充账号），比 api_key 更敏感 ——
#:    与 ``result/`` 一同被 .gitignore 覆盖，不要外发。
CLI_SESSIONS_PATH = RESULT_DIR / "cli_sessions.jsonl"
SUMMARY_JSON_PATH = RESULT_DIR / "summary.json"  # 本批统计（run_e2e 写）
#: 验收摘要。**不能**和批次摘要共用一个文件 —— 以前两者都写 summary.json，
#: 一次 verify 就把"本批 50 个账号的速率"覆盖成了"56 把 key 的验收"。
VERIFY_SUMMARY_PATH = RESULT_DIR / "verify_summary.json"


def validate(*, backend: str | None = None) -> list[str]:
    """返回缺失的必需配置项。刻意不在 import 时抛错 —— 否则连 --help 都跑不起来。"""
    backend = (backend or MAIL_BACKEND).lower()
    missing: list[str] = []
    if backend == "cf":
        for key, val in (("TEMPMAIL_BASE", TEMPMAIL_BASE),
                         ("TEMPMAIL_ADMIN_KEY", TEMPMAIL_ADMIN_KEY),
                         ("TEMPMAIL_DOMAIN", TEMPMAIL_DOMAIN)):
            if not val:
                missing.append(key)
    elif backend != "remail":
        missing.append(f"DEVIN_MAIL_BACKEND={backend!r}（只支持 cf / remail）")
    return missing


def validate_remail() -> list[str]:
    """Remail 后端配置。

    ⚠️ 2026-10-31 实测：Remail 的 `GET /v1/open/projects` 里**没有 Devin 项目**
    （只有 OpenXLab / gitlab / Kaggle / chatgpt 等），而 `POST /v1/open/orders`
    必须带 `projectId` ⇒ 目前 Devin 链路**只能走 CF Worker**。
    这里保留校验函数是为了让"选了 remail"时给出可读的错误，
    而不是让 stages 层跑一半才发现没有项目号。
    """
    return [k for k, v in (("REMAIL_BASE", os.getenv("REMAIL_BASE", "")),
                           ("REMAIL_API_KEY", os.getenv("REMAIL_API_KEY", "")),
                           ("REMAIL_PROJECT_ID", os.getenv("REMAIL_PROJECT_ID", ""))) if not v]
