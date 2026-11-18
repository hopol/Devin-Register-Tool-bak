"""发码节流器（AIMD）的单测。

用假时钟，不真 sleep —— 否则一个测试要跑几十秒。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.pacer import StartPacer  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class PacerTests(unittest.TestCase):
    def make(self, rate: float, **kw) -> tuple[StartPacer, FakeClock]:
        clock = FakeClock()
        pacer = StartPacer(rate, clock=clock, sleep=clock.sleep, **kw)
        return pacer, clock

    def test_slots_are_spaced(self):
        pacer, clock = self.make(60)  # 1 个/秒
        self.assertEqual(pacer.acquire(), 0.0)
        self.assertEqual(clock.t, 0.0)
        self.assertAlmostEqual(pacer.acquire(), 1.0)
        self.assertAlmostEqual(clock.t, 1.0)
        self.assertAlmostEqual(pacer.acquire(), 1.0)
        self.assertAlmostEqual(clock.t, 2.0)

    def test_penalize_multiplies_down_and_cools_down(self):
        pacer, _ = self.make(20, min_per_minute=4, penalty_factor=0.8)
        pacer.acquire()
        pacer.penalize()
        self.assertAlmostEqual(pacer.rate, 16.0)
        self.assertEqual(pacer.snapshot().penalties, 1)
        for _ in range(10):
            pacer.penalize()
        self.assertAlmostEqual(pacer.rate, 4.0)  # 不低于下限

    def test_reward_adds_after_streak(self):
        pacer, _ = self.make(10, max_per_minute=40, reward_every=3, reward_step=1)
        pacer.reward()
        pacer.reward()
        self.assertAlmostEqual(pacer.rate, 10.0)  # 未到奖励点
        pacer.reward()
        self.assertAlmostEqual(pacer.rate, 11.0)  # 加性 +1，不是 ×1.1
        for _ in range(200):
            pacer.reward()
        self.assertAlmostEqual(pacer.rate, 40.0)  # 上限生效

    def test_requested_rate_above_max_raises_ceiling(self):
        pacer, _ = self.make(80, max_per_minute=30, reward_every=1)
        self.assertAlmostEqual(pacer.rate, 80.0)
        pacer.reward()
        self.assertAlmostEqual(pacer.rate, 80.0)  # 不会被夹回 30
        self.assertAlmostEqual(pacer.max_rate, 80.0)

    def test_rejects_nonpositive_rate(self):
        with self.assertRaises(ValueError):
            StartPacer(0)

    def test_snapshot_reports_wait_and_rate(self):
        pacer, _ = self.make(60)
        pacer.acquire()
        pacer.acquire()
        snap = pacer.snapshot()
        self.assertAlmostEqual(snap.waited_seconds, 1.0)
        self.assertAlmostEqual(snap.rate_per_minute, 60.0)
        self.assertEqual(snap.penalties, 0)


if __name__ == "__main__":
    unittest.main()
