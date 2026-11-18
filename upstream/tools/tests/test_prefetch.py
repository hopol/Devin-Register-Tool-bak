"""收信轮询与邮箱预建的单测（离线）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import runner  # noqa: E402
from src.tempemail import Mail, TempMailClient  # noqa: E402


class WaitForMailTests(unittest.TestCase):
    def test_fast_first_polls_then_back_to_interval(self):
        """前 3 次等 0.3s，之后 1s；第 5 次才拿到邮件 ⇒ sleeps=[0.3,0.3,0.3,1.0]."""
        class Clock:
            def __init__(self):
                self.t = 0.0
                self.sleeps = []

            def time(self):
                return self.t

            def sleep(self, s):
                self.sleeps.append(s)
                self.t += s

        clock = Clock()
        client = TempMailClient(base="http://x", admin_key="k", domain="d")
        mail = Mail("1", "a@b.c", "s@cognition.ai", "Your Devin Login Code",
                    "code below 123456", 1)
        with mock.patch("src.tempemail.time.time", side_effect=clock.time), \
             mock.patch("src.tempemail.time.sleep", side_effect=clock.sleep), \
             mock.patch.object(client, "list_mails",
                               side_effect=[[], [], [], [], [mail]]):
            got = client.wait_for_mail("a@b.c", lambda m: True, timeout=10,
                                       interval=1.0)
        self.assertIsNotNone(got)
        self.assertEqual(clock.sleeps, [0.3, 0.3, 0.3, 1.0])


class PrefetchTests(unittest.TestCase):
    def test_prefetcher_creates_and_hands_out_mailboxes(self):
        class FakeClient:
            def __init__(self):
                self.n = 0

            def create_mailbox(self):
                self.n += 1
                return f"o{self.n}@b.c"

        with mock.patch.object(runner, "build_mail_client", return_value=FakeClient()):
            pf = runner.MailboxPrefetcher("cf", depth=2, log=lambda _m: None).start()
            try:
                got = {pf.take(timeout=2) for _ in range(3)}
            finally:
                pf.close()
        self.assertEqual(len(got), 3)          # 3 个不同地址
        self.assertEqual(pf.errors, 0)


if __name__ == "__main__":
    unittest.main()
