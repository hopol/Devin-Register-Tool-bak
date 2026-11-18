"""stages 全链路单测（离线）：注册 → 发码 → 账号信息 → 建 key → 入库。

用假的收信端 + 假的 Devin 端跑完整 ``Pipeline.run()``，验证：

- 账号记录的落库字段与状态机
- 429 发码冷却重试
- 验证码失效后的"重发 + 重收"恢复路径
- key 已存在时必须是 ``partial`` 语义（不能假装成功）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config, ledger  # noqa: E402
from src.devin import DevinError  # noqa: E402
from src.stages import Pipeline  # noqa: E402
from src.tempemail import Mail, Stats  # noqa: E402


class FakeMail:
    def __init__(self, code: str = "123456", boxes: int = 1):
        self.code = code
        self.boxes = 0
        self.max_boxes = boxes
        self.stats = Stats()
        self.waits = 0

    def create_mailbox(self) -> str:
        self.boxes += 1
        return f"oai-{self.boxes}@mail.test"

    def wait_for_mail(self, email, match, *, timeout=0, interval=0, since_ms=None):
        self.waits += 1
        self.stats.polls += 1
        mail = Mail("1", email, "bounces+x@em3779.cognition.ai",
                    "Your Devin Login Code",
                    f"verification code below\n{self.code}", 2_000)
        return mail if match(mail) else None


class FakeDevin:
    def __init__(self, *, rate_limits: int = 0, bad_codes: int = 0,
                 key_exists: bool = False, key_error: bool = False):
        self.rate_limits = rate_limits
        self.bad_codes = bad_codes
        self.key_exists = key_exists
        self.key_error = key_error
        self.starts = 0
        self.completes = 0
        self.session_token = ""
        self.user_id = ""
        self.org_id = ""
        self.email = ""
        #: 与 DevinClient 对齐：真实客户端有 Session；测试里 exchange 被 patch，不需要真对象。
        self.s = None

    def start_email(self, email):
        self.starts += 1
        self.email = email
        if self.rate_limits > 0:
            self.rate_limits -= 1
            raise DevinError("限流", code="rate_limited", status=429)
        return {"ok": True, "email_verification_token": "ev"}

    def complete_email(self, email, ev_token, code):
        self.completes += 1
        self.email = email
        if self.bad_codes > 0:
            self.bad_codes -= 1
            raise DevinError("验证码失效", code="invalid_code", status=401)
        self.session_token = "auth1_" + "x"
        self.user_id = "user-1"
        return {"token": "auth1_x", "user_id": "user-1", "email": email}

    def post_auth(self):
        self.org_id = "org-1"
        return {"org_id": "org-1", "org_name": "team"}

    def account_snapshot(self, *, onboarding=True):
        return {
            "account_id": "account-1", "plan_slug": "free",
            "available_credits": 0, "onboarding_status": "completed" if onboarding else "in_progress",
            "created_at": "2026-10-31T00:00:00", "warnings": [],
            "billing_error": "", "overage_credits": 9.93,
            "swe2_promo_eligible": True, "swe2_priority_eligible": False,
        }

    def api_key_exists(self):
        return self.key_exists

    def create_api_key(self):
        if self.key_error:
            raise DevinError("建 key 失败", code="http")
        return "apk_user_test"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(config, "LEDGER_PATH", root / "ledger.jsonl"),
            mock.patch.object(config, "SUCCESS_PATH", root / "success.jsonl"),
            mock.patch.object(config, "RESULT_DIR", root),
            mock.patch.object(config, "START_BACKOFF", 0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def pipe(self, *, cli_auth: bool = False, cli_overlap: bool = True,
             **kw) -> tuple[Pipeline, FakeMail, FakeDevin]:
        mail = FakeMail()
        devin = FakeDevin(**kw)
        pipe = Pipeline(mail=cast(Any, mail), devin=cast(Any, devin),
                        cli_auth=cli_auth, cli_overlap=cli_overlap,
                        log=lambda _m: None)
        return pipe, mail, devin

    def test_happy_path_keyed_and_persisted(self):
        pipe, mail, devin = self.pipe()
        rec = pipe.run()
        self.assertEqual(rec.status, "keyed")
        self.assertEqual(rec.email, "oai-1@mail.test")
        self.assertEqual(rec.user_id, "user-1")
        self.assertEqual(rec.org_id, "org-1")
        self.assertEqual(rec.account_id, "account-1")
        self.assertEqual(rec.billing_error, "")
        self.assertAlmostEqual(rec.overage_credits, 9.93)
        self.assertTrue(rec.swe2_promo_eligible)
        self.assertFalse(rec.swe2_priority_eligible)
        self.assertEqual(rec.api_key, "apk_user_test")
        self.assertTrue(rec.timings)
        row = ledger.load()[rec.email]
        self.assertEqual(row["api_key"], "apk_user_test")
        self.assertEqual(row["status"], "keyed")
        self.assertEqual(row["billing_error"], "")
        self.assertAlmostEqual(row["overage_credits"], 9.93)
        self.assertTrue(row["swe2_promo_eligible"])
        self.assertEqual(devin.starts, 1)
        self.assertEqual(mail.boxes, 1)

    def test_rate_limit_retried(self):
        pipe, _mail, devin = self.pipe(rate_limits=2)
        rec = pipe.run()
        self.assertEqual(rec.status, "keyed")
        self.assertEqual(devin.starts, 3)  # 2 次 429 + 1 次成功

    def test_invalid_code_triggers_one_resend(self):
        pipe, mail, devin = self.pipe(bad_codes=1)
        rec = pipe.run()
        self.assertEqual(rec.status, "keyed")
        self.assertEqual(devin.completes, 2)
        self.assertEqual(devin.starts, 2)   # 首次 + 重发
        self.assertEqual(mail.waits, 2)     # 首次 + 重收

    def test_existing_key_is_not_success(self):
        pipe, _mail, _devin = self.pipe(key_exists=True)
        rec = pipe.run()
        self.assertEqual(rec.status, "failed")
        self.assertEqual(rec.api_key, "")
        self.assertIn("已有 API Key", rec.error)
        # 不可恢复：补跑只会再烧一次发码配额。
        self.assertFalse(rec.retryable)
        self.assertFalse(ledger.load()[rec.email]["retryable"])

    def test_key_creation_failure_keeps_registered_info(self):
        pipe, _mail, _devin = self.pipe(key_error=True)
        rec = pipe.run()
        self.assertEqual(rec.status, "failed")
        self.assertEqual(rec.user_id, "user-1")
        self.assertEqual(rec.org_id, "org-1")
        self.assertEqual(rec.api_key, "")

    def test_resume_reuses_given_email(self):
        pipe, mail, _devin = self.pipe()
        rec = pipe.run("known@mail.test")
        self.assertEqual(rec.email, "known@mail.test")
        self.assertEqual(mail.boxes, 0)  # 不重复建邮箱


class CliStageTests(PipelineTests):
    """CLI 会话阶段：成功要落库；失败**不能**拖垮已交付的 key。"""

    TOKEN = "eyJ" + ".payload.signature"

    def _grant(self):
        from src.cli_auth import CliGrant
        return CliGrant(token=self.TOKEN, webapp_host="app.devin.ai",
                        session_id="windsurf-session-test",
                        issued_at="2026-10-31T00:00:00+00:00")

    def test_cli_token_stored(self):
        pipe, _mail, _devin = self.pipe(cli_auth=True, cli_overlap=False)
        with mock.patch("src.stages.cli_auth.cli_login", return_value=self._grant()):
            rec = pipe.run()
        self.assertEqual(rec.status, "keyed")
        self.assertEqual(rec.cli_token, self.TOKEN)
        self.assertEqual(rec.cli_session_id, "windsurf-session-test")
        self.assertEqual(rec.cli_webapp_host, "app.devin.ai")
        row = ledger.load()[rec.email]
        self.assertEqual(row["cli_token"], self.TOKEN)

    def test_cli_failure_keeps_key(self):
        from src.cli_auth import CliAuthError

        pipe, _mail, _devin = self.pipe(cli_auth=True, cli_overlap=False)
        with mock.patch("src.stages.cli_auth.cli_login",
                        side_effect=CliAuthError("缺少 org", code="no_org")):
            rec = pipe.run()
        self.assertEqual(rec.status, "keyed")       # 账号仍然成功
        self.assertTrue(rec.api_key)
        self.assertEqual(rec.cli_token, "")
        self.assertIn("no_org", rec.cli_error)
        self.assertIn("no_org", ledger.load()[rec.email]["cli_error"])


class PacerIntegrationTests(PipelineTests):
    """节流器接入：每次发码前 acquire；429 时 penalize；等待单独记账。"""

    def test_pacer_acquire_penalize_and_waits(self):
        class FakePacer:
            def __init__(self):
                self.acquires = 0
                self.penalties = 0
                self.rewards = 0

            def acquire(self):
                self.acquires += 1
                return 0.5

            def penalize(self):
                self.penalties += 1

            def reward(self):
                self.rewards += 1

        pacer = FakePacer()
        mail = FakeMail()
        devin = FakeDevin(rate_limits=1)
        pipe = Pipeline(mail=cast(Any, mail), devin=cast(Any, devin),
                        pacer=cast(Any, pacer), log=lambda _m: None)
        rec = pipe.run()

        self.assertEqual(rec.status, "keyed")
        self.assertEqual(pacer.acquires, 2)   # 1 次 429 + 1 次成功
        self.assertEqual(pacer.penalties, 1)
        self.assertEqual(pacer.rewards, 1)
        self.assertAlmostEqual(rec.timings["pace_wait"], 1.0)


class CliOverlapTests(PipelineTests):
    """authorize 预热 + exchange 复用：code 必须来自预热，不能重新 authorize。"""

    def test_overlap_uses_preauthorized_code(self):
        from src.cli_auth import CliGrant

        grant = CliGrant(token="eyJ" + ".x.y", webapp_host="app.devin.ai",
                         session_id="windsurf-session-x", issued_at="2026-10-31T00:00:00+00:00")
        pipe, _mail, _devin = self.pipe(cli_auth=True, cli_overlap=True)
        with mock.patch("src.stages.cli_auth.authorize", return_value="CODE") as auth,              mock.patch("src.stages.cli_auth.exchange_code", return_value=grant) as exch:
            rec = pipe.run()
        self.assertEqual(rec.status, "keyed")
        self.assertEqual(rec.cli_token, "eyJ.x.y")
        auth.assert_called_once()
        exch.assert_called_once()
        self.assertEqual(exch.call_args.args[0], "CODE")


class RunnerIsolationTests(unittest.TestCase):
    def test_worker_crash_does_not_kill_batch(self):
        """一个 worker 构造失败（例如后端配置写错）不能带走整批。"""
        from src import runner

        class Boom:
            def __init__(self, **_kw):
                raise RuntimeError("bad backend")

        summary = runner.run_batch(emails=["x@y.z"],
                                   pipeline_factory=cast(Any, Boom),
                                   verbose=False)
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.failed, 1)
        self.assertIn("bad backend", summary.records[0]["error"])


if __name__ == "__main__":
    unittest.main()
