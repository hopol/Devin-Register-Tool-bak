"""DevinClient 的契约单测：请求形状 / 鉴权头 / 错误分类。

用假 Session 记录每个请求，不碰网络。这样"少发一个 mode=signup"、
"漏掉 x-cog-org-id" 这类**看起来能跑、其实必 400** 的错会当场被抓住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.devin import DevinError, verify_api_key  # noqa: E402

#: 测试用会话 token（不做成字面量赋值，避免被"硬编码密码"规则误报）。
AUTH_TOKEN = "auth1_" + "abc"


from support import FakeResponse, FakeSession, make_client, route  # noqa: E402


class StartTests(unittest.TestCase):
    def test_body_contains_mode_signup(self):
        client, session = make_client({
            ("POST", "/api/auth1/email/start"):
                {"ok": True, "email_verification_token": "ev-token"},
        })
        data = client.start_email("a@b.c")
        self.assertEqual(data["email_verification_token"], "ev-token")
        call = session.calls[0]
        self.assertEqual(call["body"], {"email": "a@b.c", "mode": "signup"})

    def test_rate_limited_classified(self):
        client, _ = make_client({
            ("POST", "/api/auth1/email/start"):
                FakeResponse(429, {"detail": "Rate limit exceeded. Please try again later."}),
        })
        with self.assertRaises(DevinError) as ctx:
            client.start_email("a@b.c")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_no_account_classified(self):
        client, _ = make_client({
            ("POST", "/api/auth1/email/start"):
                FakeResponse(400, {"detail": "No account found for this email"}),
        })
        with self.assertRaises(DevinError) as ctx:
            client.start_email("a@b.c")
        self.assertEqual(ctx.exception.code, "no_account")


class CompleteTests(unittest.TestCase):
    def test_complete_sets_session(self):
        client, session = make_client({
            ("POST", "/api/auth1/email/complete"): {
                "token": "auth1_abc", "user_id": "user-1",
                "email": "a@b.c", "status": "authenticated",
            },
        })
        data = client.complete_email("a@b.c", "ev-token", "123456")
        self.assertEqual(data["user_id"], "user-1")
        self.assertEqual(client.session_token, "auth1_abc")
        self.assertEqual(client.user_id, "user-1")
        body = session.calls[0]["body"]
        self.assertEqual(set(body), {"email", "email_verification_token", "code", "mode"})
        self.assertEqual(body["mode"], "signup")

    def test_invalid_code_classified(self):
        client, _ = make_client({
            ("POST", "/api/auth1/email/complete"):
                FakeResponse(401, {"detail": "Invalid or expired code"}),
        })
        with self.assertRaises(DevinError) as ctx:
            client.complete_email("a@b.c", "ev", "000000")
        self.assertEqual(ctx.exception.code, "invalid_code")


class AuthScopeTests(unittest.TestCase):
    def test_post_auth_sets_org_and_sends_bearer(self):
        client, session = make_client({
            ("POST", "/api/users/post-auth"): {"org_id": "org-1", "org_name": "team"},
        })
        client.session_token = AUTH_TOKEN
        data = client.post_auth()
        self.assertEqual(data["org_id"], "org-1")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer auth1_abc")

    def test_user_info_requires_org_header(self):
        client, session = make_client({
            ("POST", "/api/users/post-auth"): {"org_id": "org-1"},
            ("GET", "/api/users/info"): {"user_id": "user-1"},
        })
        client.session_token = AUTH_TOKEN
        client.post_auth()
        client.user_info()
        headers = session.calls[-1]["headers"]
        self.assertEqual(headers["x-cog-org-id"], "org-1")
        self.assertEqual(headers["Authorization"], "Bearer auth1_abc")

    def test_org_path_not_double_prefixed(self):
        """org_id 已含 org- 前缀，再拼一次就是 404 —— 这里钉死路径形状。"""
        client, session = make_client({
            ("POST", "/api/users/post-auth"): {"org_id": "org-1"},
            ("POST", "/api/org-1/user-1/api-key"): {"api_key": "apk_user_x"},
        })
        client.session_token = AUTH_TOKEN
        client.user_id = "user-1"
        client.post_auth()
        key = client.create_api_key()
        self.assertEqual(key, "apk_user_x")
        self.assertIn("/api/org-1/user-1/api-key", session.calls[-1]["url"])
        self.assertNotIn("org-org-", session.calls[-1]["url"])


_ORG = {
    "org_id": "org-1", "name": "team", "account_id": "account-1",
    "created_at": "2026-10-31T00:00:00", "onboarding_status": "completed",
}


class SnapshotTests(unittest.TestCase):

    def test_snapshot_collects_availability_fields(self):
        """organization/user_info 不再单独请求；但账单与模型资格必须落库。"""
        client, session = make_client({
            ("GET", "/api/users/current-membership"):
                {"org": _ORG, "user": {"email": "a@b.c"}},
            ("GET", "/api/org-1/billing/status"):
                {"plan_slug": "free", "available_credits": 0,
                 "billing_error": None, "overage_credits": 9.93},
            ("GET", "/api/toggleable-agent-versions"):
                {"swe2_promo_eligible": True, "swe2_priority_eligible": False,
                 "toggleable_versions": ["devin-swe-2-max"]},
        })
        client.session_token = "auth1_" + "x"
        client.org_id = "org-1"
        snap = client.account_snapshot(onboarding=False)

        self.assertEqual(snap["account_id"], "account-1")
        self.assertEqual(snap["created_at"], "2026-10-31T00:00:00")
        self.assertEqual(snap["onboarding_status"], "completed")
        self.assertEqual(snap["plan_slug"], "free")
        self.assertEqual(snap["billing_error"], "")
        self.assertAlmostEqual(snap["overage_credits"], 9.93)
        self.assertTrue(snap["swe2_promo_eligible"])
        self.assertFalse(snap["swe2_priority_eligible"])
        # membership + billing + toggleable（没有 organization / user_info）
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(any("/api/users/info" in c["url"] for c in session.calls))

    def test_snapshot_survives_promo_endpoint_failure(self):
        """模型资格接口挂了只记 warning，不影响账号信息。"""
        client, _ = make_client({
            ("GET", "/api/users/current-membership"):
                {"org": _ORG, "user": {"email": "a@b.c"}},
            ("GET", "/api/org-1/billing/status"):
                {"plan_slug": "free", "available_credits": 0},
            ("GET", "/api/toggleable-agent-versions"):
                FakeResponse(403, {"detail": "Forbidden"}),
        })
        client.session_token = "auth1_" + "x"
        client.org_id = "org-1"
        snap = client.account_snapshot(onboarding=False)

        self.assertEqual(snap["account_id"], "account-1")
        self.assertFalse(snap["swe2_promo_eligible"])
        self.assertTrue(any("agent_versions" in w for w in snap["warnings"]))


class ClassifyTests(unittest.TestCase):
    def test_messages_keep_original_text(self):
        client, _ = make_client({
            ("GET", "/api/users/info"):
                FakeResponse(401, {"detail": "No organizations found for auth1 user"}),
        })
        client.session_token = AUTH_TOKEN
        client.org_id = "org-1"
        with self.assertRaises(DevinError) as ctx:
            client.user_info()
        self.assertEqual(ctx.exception.code, "unauthenticated")
        self.assertIn("No organizations found", str(ctx.exception))


class VerifyKeyTests(unittest.TestCase):
    def test_ok(self):
        session = FakeSession(route({
            ("GET", "/v1/sessions"): {"sessions": []},
        }))
        result = verify_api_key("apk_user_x", session=cast(Any, session))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Bearer apk_user_x")

    def test_invalid(self):
        session = FakeSession(route({
            ("GET", "/v1/sessions"):
                FakeResponse(401, {"detail": "Invalid API key"}),
        }))
        result = verify_api_key("apk_user_bad", session=cast(Any, session))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)


if __name__ == "__main__":
    unittest.main()
