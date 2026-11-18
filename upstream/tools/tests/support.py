"""测试用的假 HTTP 层（不碰网络）。

放在单独模块里而不是各测试文件各写一份：``FakeSession`` 的行为一旦分叉，
"A 测试过了、B 测试假过"就很难发现。
"""

from __future__ import annotations

import json
from typing import Any

from src.devin import DevinClient


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str | None = None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """按 (method, path 子串) 路由的假会话，同时记录全部请求。"""

    def __init__(self, handler):
        self.handler = handler
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        call = {
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "body": json.loads(data) if data else None,
        }
        self.calls.append(call)
        return self.handler(method, url, call)

    def get(self, url, params=None, headers=None, timeout=None):
        call = {"method": "GET", "url": url, "headers": dict(headers or {}),
                "body": None, "params": params}
        self.calls.append(call)
        return self.handler("GET", url, call)

    def post(self, url, json=None, headers=None, timeout=None):
        """给 ``exchange_code`` 这类直接调 ``session.post`` 的代码用。"""
        call = {"method": "POST", "url": url, "headers": dict(headers or {}),
                "body": json}
        self.calls.append(call)
        return self.handler("POST", url, call)


def route(table):
    """``{(method, path 片段): payload 或 FakeResponse}`` → 处理函数。"""
    def handler(method, url, call):
        for (m, frag), resp in table.items():
            if m == method and frag in url:
                return resp if isinstance(resp, FakeResponse) else FakeResponse(200, resp)
        raise AssertionError(f"未路由的请求: {method} {url}")
    return handler


def make_client(table) -> tuple[DevinClient, FakeSession]:
    session = FakeSession(route(table))
    return DevinClient(session=session), session  # type: ignore[arg-type]
