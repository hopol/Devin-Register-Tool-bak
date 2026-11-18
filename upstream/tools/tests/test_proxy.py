"""代理池单测：解析 / 脱敏 / 排他租约 / 失败摘除。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.proxy import ProxyPool, load_proxies, mask  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_mask_hides_credentials(self):
        masked = mask("http://user:secret@host.example:8080")
        self.assertEqual(masked, "http://host.example:8080")
        self.assertNotIn("secret", masked)

    def test_mask_hides_subdomain_token(self):
        """凭据在子域里的代理（如 provider）不能把子域原样打进日志。"""
        token = "deadbeef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
        masked = mask(f"https://{token}.proxy.example.com:7173")
        self.assertNotIn(token, masked)
        self.assertEqual(masked, "https://deadbe….proxy.example.com:7173")

    def test_load_from_file_dedup_and_normalize(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("http://u:p@a.example:1\n")
            fh.write("b.example:2\n")
            fh.write("http://u:p@a.example:1\n")  # 重复
            fh.write("# comment\n")
            path = fh.name
        proxies = load_proxies(path)
        self.assertEqual(proxies, ["http://u:p@a.example:1", "http://b.example:2"])
        Path(path).unlink()

    def test_rejects_bad_entries(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("ftp://x:1\nno-port\n\n")
            path = fh.name
        self.assertEqual(load_proxies(path), [])
        Path(path).unlink()


def _pool(n: int = 2) -> ProxyPool:
    urls = [f"http://u:p@h{i}.example:1" for i in range(n)]
    return ProxyPool(urls, log=lambda _m: None)


class LeaseTests(unittest.TestCase):
    def pool(self, n=2):
        return _pool(n)

    def test_lease_is_exclusive_and_released(self):
        pool = self.pool(1)
        with pool.lease(timeout=1) as lease:
            self.assertEqual(lease.masked, "http://h0.example:1")
            # 唯一一条被占用时，第二个租约应超时
            with self.assertRaises(TimeoutError), pool.lease(timeout=0.2):
                pass
        with pool.lease(timeout=1):
            pass  # 释放后可再租
        self.assertEqual(pool.snapshot()["leases"], 2)

    def test_round_robin_distributes(self):
        pool = self.pool(2)
        seen = set()
        for _ in range(2):
            with pool.lease(timeout=1) as lease:
                seen.add(lease.masked)
        self.assertEqual(seen, {"http://h0.example:1", "http://h1.example:1"})

    def test_repeated_failures_remove_proxy(self):
        pool = self.pool(1)
        url = pool.all[0]
        for _ in range(3):
            pool.mark_failure(url)
        self.assertEqual(len(pool), 0)
        self.assertEqual(pool.snapshot()["dead"], 1)
        with self.assertRaises(TimeoutError), pool.lease(timeout=0.2):
            pass

    def test_success_resets_failure_counter(self):
        pool = self.pool(1)
        url = pool.all[0]
        pool.mark_failure(url)
        pool.mark_success(url)
        pool.mark_failure(url)
        self.assertEqual(len(pool), 1)  # 未连续 3 次，不摘除

    def test_session_reused_per_proxy(self):
        pool = self.pool(1)
        with pool.lease() as first:
            sid = id(first.session)
        with pool.lease() as second:
            self.assertEqual(id(second.session), sid)  # 同一 Session（TLS 复用）
        self.assertFalse(first.session.trust_env)


class ConcurrencyTests(unittest.TestCase):
    def test_two_leases_run_in_parallel(self):
        pool = _pool(2)
        active = []
        lock = threading.Lock()

        def worker():
            with pool.lease(timeout=1):
                with lock:
                    active.append(1)
                time.sleep(0.05)
                with lock:
                    active.pop()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(pool.snapshot()["leases"], 2)


if __name__ == "__main__":
    unittest.main()
