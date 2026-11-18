"""发码节流器（AIMD）：把"并发抢发"改成"按站点能承受的速率放行"。

为什么需要它（2026-10-31 50 号压测的实测数据）
────────────────────────────────────────────
- 并发 5、不节流：**19/50 个全新地址**也命中 429，单号被 25/50/75s 退避拖到
  52.6s（p95 88s）；均值 29.5s / 吞吐 9.96 个/分钟。
- 其中 31 个没被 429 的账号：均值 **15.3s**、p95 18.9s。
- 起手 8 / 并发 4：**0 次 429**，服务耗时 12.87s、p95 15.5s，收敛到 11 个/分钟。
⇒ 429 不是"邮件服务慢"，而是**发码速率超过站点阈值**。盲目重试只会把
   速率继续顶在阈值上，退避时间全是净损失。

算法：AIMD（加性增、乘性减）
────────────────────────────
- 每个 slot 间隔 ``60/rate`` 秒放行一次，``acquire()`` 阻塞到轮到自己；
- 命中 429：``rate *= 0.8``（下限 ``min_per_minute``），下一次放行再推后一个间隔；
- 连续成功 ``reward_every`` 次：``rate += 1``（上限 ``max_per_minute``）。

⚠️ 第一版是"429 减半 / 每 10 次 ×1.1"，实测**回退过猛、恢复太慢**：
   20 个账号的批次从 20 掉到 6.0，只回到 6.6，吞吐被压到 7.06/min。
   改成当前参数后（起手 8 / 并发 4）：15 个账号 **0 次 429**，速率爬到 11/min。

这样速率会自己收敛到"刚好不触发 429"的位置，而不是靠人猜一个并发数。
⚠️ 放行等待**计入单号 elapsed**，所以报告必须把 ``pace_wait`` 单独拆出来
   （见 ``tools/batch_report.py`` 的 service/e2e 两个口径）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class PacerSnapshot:
    rate_per_minute: float
    min_per_minute: float
    max_per_minute: float
    penalties: int
    rewarded: int
    waited_seconds: float


class StartPacer:
    """线程安全的发码节流器。整批共享一个实例。"""

    def __init__(self, per_minute: float = 20.0, *,
                 min_per_minute: float = 3.0,
                 max_per_minute: float = 30.0,
                 reward_every: int = 5,
                 reward_step: float = 1.0,
                 penalty_factor: float = 0.8,
                 clock=time.monotonic,
                 sleep=time.sleep):
        if per_minute <= 0:
            raise ValueError("per_minute 必须 > 0（关闭节流请传 pacer=None）")
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        # 🔴 显式请求的速率就是上限：默认 max=30 是"自我保护"，
        #    但代理池场景会传 8×N（如 80）—— 若不放宽 max，第一次 reward
        #    就会把 80 夹回 30（实测：50 号批次被 30/min 卡住，吞吐 28.4/min）。
        self.rate = max(float(min_per_minute), float(per_minute))
        self.min_rate = float(min_per_minute)
        self.max_rate = max(float(max_per_minute), self.rate)
        self.reward_every = max(1, int(reward_every))
        self.reward_step = float(reward_step)
        self.penalty_factor = float(penalty_factor)
        self._next_at = 0.0
        self._streak = 0
        self.penalties = 0
        self.rewarded = 0
        self.waited = 0.0

    @property
    def interval(self) -> float:
        return 60.0 / self.rate

    # ── 放行 ──────────────────────────────────────────────────────────
    def acquire(self) -> float:
        """阻塞到下一个发码 slot，返回本次等待秒数。

        等待计算在锁内完成、sleep 在锁外 —— 否则一个 worker 睡觉会把
        其它 worker 全堵在锁上（等于串行化，和并发目的相反）。
        """
        with self._lock:
            now = self._clock()
            start_at = max(now, self._next_at)
            self._next_at = start_at + self.interval
            wait = max(0.0, start_at - now)
        if wait > 0:
            self._sleep(wait)
        with self._lock:
            self.waited += wait
        return wait

    # ── 反馈 ──────────────────────────────────────────────────────────
    def penalize(self) -> None:
        """收到 429：速率乘性回退，并把下一次放行再推后一个（新）间隔。

        🔴 从"直接减半"改成 ×0.8：50 号压测里从 20 起手、一次 429 就掉到 10，
        而站点阈值只是略微低一点 ⇒ 过度回退会把吞吐压到阈值以下（实测收敛到 6.6/min）。
        """
        with self._lock:
            self.penalties += 1
            self._streak = 0
            self.rate = max(self.min_rate, self.rate * self.penalty_factor)
            self._next_at = max(self._next_at, self._clock() + self.interval)

    def reward(self) -> None:
        """发码成功：连续 N 次后**加性**提速（每秒 +1 个/分钟）。

        🔴 从"×1.1 / 每 10 次"改成"×0.8 回退 + 每 5 次 +1"：
        20 个账号的批次里，旧算法从 6.0 只回到 6.6 —— 恢复太慢，等于把速率
        永久压在一次误判之下。
        """
        with self._lock:
            self._streak += 1
            if self._streak < self.reward_every:
                return
            self._streak = 0
            self.rewarded += 1
            self.rate = min(self.max_rate, self.rate + self.reward_step)

    def snapshot(self) -> PacerSnapshot:
        with self._lock:
            return PacerSnapshot(
                rate_per_minute=round(self.rate, 2),
                min_per_minute=self.min_rate,
                max_per_minute=self.max_rate,
                penalties=self.penalties,
                rewarded=self.rewarded,
                waited_seconds=round(self.waited, 2),
            )
