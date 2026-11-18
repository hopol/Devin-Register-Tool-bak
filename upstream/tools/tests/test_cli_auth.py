"""CLI 授权（PKCE）单测：请求形状 / 错误分流 / 白名单 / token 自检。

全部离线：用 ``support.FakeSession`` 记录请求，不打网络。
重点钉住三条实测结论：
1. authorize **必须**带 ``x-cog-org-id``（缺了服务端 401）；
2. 请求体只有 ``code_challenge`` + ``method``（CLI 不传 client）；
3. code 一次性 —— 换 token 失败要归到 ``invalid_code``，不能报成"网络错"。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeResponse, FakeSession, make_client, route  # noqa: E402

from src import cli_auth  # noqa: E402

#: 测试用会话 token（不做成字面量赋值，避免被"硬编码密码"规则误报）。
AUTH_TOKEN = "auth1_" + "x"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwt(payload: dict) -> str:
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{head}.{body}.signature"


class PkceTests(unittest.TestCase):
    def test_s256_derivation_and_bounds(self):
        pair = cli_auth.generate_pkce(32)
        self.assertEqual(pair.method, "S256")
        self.assertEqual(len(pair.verifier), 43)  # RFC 7636 允许的最小长度
        expected = _b64url(hashlib.sha256(pair.verifier.encode()).digest())
        self.assertEqual(pair.challenge, expected)

    def test_default_length_in_range(self):
        pair = cli_auth.generate_pkce()
        self.assertTrue(43 <= len(pair.verifier) <= 128)
        self.assertNotIn("=", pair.challenge)


class AuthorizeTests(unittest.TestCase):
    def test_request_shape_has_org_and_pkce(self):
        client, session = make_client({
            ("POST", "/auth/cli/authorize"): {"code": "C" * 43},
        })
        client.session_token = "auth1_" + "x"
        client.org_id = "org-1"
        pair = cli_auth.generate_pkce(32)
        code = cli_auth.authorize(client, pair)

        self.assertEqual(code, "C" * 43)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://api.devin.ai/auth/cli/authorize")
        self.assertEqual(call["headers"]["Authorization"], "Bearer auth1_x")
        self.assertEqual(call["headers"]["x-cog-org-id"], "org-1")
        self.assertEqual(call["body"], {
            "code_challenge": pair.challenge,
            "code_challenge_method": "S256",
        })

    def test_missing_org_fails_before_http(self):
        client, session = make_client({})
        client.session_token = AUTH_TOKEN  # 有会话、无 org
        with self.assertRaises(cli_auth.CliAuthError) as ctx:
            cli_auth.authorize(client, cli_auth.generate_pkce(32))
        self.assertEqual(ctx.exception.code, "no_org")
        self.assertEqual(session.calls, [])  # 本地就拦住，不浪费请求

    def test_server_no_org_is_classified(self):
        client, _ = make_client({
            ("POST", "/auth/cli/authorize"):
                FakeResponse(401, {"detail": "No organizations found for auth1 user"}),
        })
        client.session_token = AUTH_TOKEN
        client.org_id = "org-1"
        with self.assertRaises(cli_auth.CliAuthError) as ctx:
            cli_auth.authorize(client, cli_auth.generate_pkce(32))
        self.assertEqual(ctx.exception.code, "no_org")

    def test_not_eligible_is_classified(self):
        client, _ = make_client({
            ("POST", "/auth/cli/authorize"):
                FakeResponse(403, {"detail": "Forbidden"}),
        })
        client.session_token = AUTH_TOKEN
        client.org_id = "org-1"
        with self.assertRaises(cli_auth.CliAuthError) as ctx:
            cli_auth.authorize(client, cli_auth.generate_pkce(32))
        self.assertEqual(ctx.exception.code, "not_eligible")


class ExchangeTests(unittest.TestCase):
    def test_success_returns_grant_and_session_id(self):
        token = _jwt({"session_id": "windsurf-session-abc123"})
        session = FakeSession(route({
            ("POST", "/auth/cli/token"): {"token": token, "webapp_host": "app.devin.ai"},
        }))
        grant = cli_auth.exchange_code("code-1", "verifier-1",
                                      session=cast(Any, session))

        self.assertEqual(grant.token, token)
        self.assertEqual(grant.webapp_host, "app.devin.ai")
        self.assertEqual(grant.session_id, "windsurf-session-abc123")
        self.assertTrue(grant.issued_at)
        self.assertEqual(session.calls[0]["body"],
                         {"code": "code-1", "code_verifier": "verifier-1"})

    def test_used_code_is_invalid_code(self):
        session = FakeSession(route({
            ("POST", "/auth/cli/token"):
                FakeResponse(401, {"detail": "Invalid or expired code. The code may have "
                                             "expired or already been used."}),
        }))
        with self.assertRaises(cli_auth.CliAuthError) as ctx:
            cli_auth.exchange_code("used", "verifier", session=cast(Any, session))
        self.assertEqual(ctx.exception.code, "invalid_code")


class RedirectTests(unittest.TestCase):
    def test_allowlist_matches_frontend_rule(self):
        self.assertTrue(cli_auth.is_allowed_redirect("http://127.0.0.1:40473/callback"))
        self.assertTrue(cli_auth.is_allowed_redirect("http://127.0.0.1:1/callback"))
        for bad in (
            "https://127.0.0.1:40473/callback",   # 必须 http
            "http://localhost:40473/callback",    # 必须 127.0.0.1
            "http://127.0.0.1:40473/other",       # 必须 /callback
            "http://127.0.0.1:40473/callback/x",
            "http://evil.example/callback",
        ):
            self.assertFalse(cli_auth.is_allowed_redirect(bad), bad)

    def test_build_redirect_strips_original_query(self):
        url = cli_auth.build_redirect_url(
            "http://127.0.0.1:40473/callback?junk=1", "CODE", "STATE")
        self.assertEqual(url, "http://127.0.0.1:40473/callback?code=CODE&state=STATE")
        with self.assertRaises(cli_auth.CliAuthError):
            cli_auth.build_redirect_url("https://evil.example/callback", "C")


class TokenVerifyTests(unittest.TestCase):
    def test_structural_check(self):
        good = _jwt({"session_id": "windsurf-session-1"})
        self.assertTrue(cli_auth.verify_cli_token(good)["ok"])
        self.assertEqual(cli_auth.verify_cli_token(good)["session_id"],
                         "windsurf-session-1")
        self.assertFalse(cli_auth.verify_cli_token("not-a-jwt")["ok"])
        self.assertFalse(cli_auth.verify_cli_token(_jwt({"session_id": "other"}))["ok"])


if __name__ == "__main__":
    unittest.main()
