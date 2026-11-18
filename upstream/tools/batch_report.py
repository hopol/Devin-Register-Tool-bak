#!/usr/bin/env python
"""批次速率报告：单号端到端耗时 / 吞吐 / 分阶段均值。

数据源优先级：

1. ``result/summary.json`` —— ``run_e2e.py`` 每次跑批覆盖写，**只含本批**记录；
2. 回退到台账 —— 用 ``--since`` 过滤本批开始时间之后的行。

为什么"速率"要报三个数
──────────────────────
只报一个平均值会被长尾骗：一批 50 个里 3 个卡在等信 180s，均值立刻失真。
所以同时给 **mean / p50 / p95** 和 **吞吐（个/分钟）**，
并列出分阶段均值 —— 哪一段变慢一眼能看出来。

用法::

    python tools/batch_report.py
    python tools/batch_report.py --json result/batch_report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, ledger  # noqa: E402

STAGES = ("mailbox", "send_code", "wait_code", "complete", "account",
          "api_key", "cli_session")


def _pct(values: list[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _load_batch(path: Path) -> tuple[list[dict], float, dict]:
    """返回 (records, wall_elapsed, summary_meta)。"""
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"summary.json 读不了: {exc}", file=sys.stderr)
            return [], 0.0, {}
        recs = data.get("records") or []
        meta = data.get("summary") or {}
        return recs, float(meta.get("elapsed") or 0.0), meta
    return [], 0.0, {}


def _from_ledger(since: str | None) -> list[dict]:
    rows = [r for r in ledger.load().values()
            if (not since or (r.get("started_at") or "") >= since)]
    rows.sort(key=lambda r: r.get("started_at") or "")
    return rows


def _wall_from_records(recs: list[dict]) -> float:
    """从台账近似墙钟：max(start+elapsed) − min(start)。

    🔴 不能用 ``sum(elapsed)`` —— 那是"串行等价耗时"，并发 5 时会算出 5 倍墙钟，
       吞吐会被低估成 1/5（实测踩过：300.6s 被算成 1473s）。
    时间戳精确到秒，误差 ≤1s/条，对 5 分钟级批次可忽略。
    """
    starts: list[datetime] = []
    ends: list[datetime] = []
    for r in recs:
        try:
            start = datetime.fromisoformat(str(r.get("started_at") or ""))
            end = start + timedelta(seconds=float(r.get("elapsed") or 0))
        except (TypeError, ValueError):
            continue
        starts.append(start)
        ends.append(end)
    if not starts:
        return 0.0
    return max(0.0, (max(ends) - min(starts)).total_seconds())


def report(recs: list[dict], wall: float, meta: dict, *, source: str) -> dict:
    ok = [r for r in recs if r.get("api_key")]
    bad = [r for r in recs if not r.get("api_key")]
    elapsed = [float(r.get("elapsed") or 0) for r in ok]
    pace = [float((r.get("timings") or {}).get("pace_wait") or 0) for r in ok]
    service = [e - p for e, p in zip(elapsed, pace)]
    cli = [r for r in ok if r.get("cli_token")]

    stats = {
        "source": source,
        "total": len(recs),
        "keyed": len(ok),
        "failed": len(bad),
        "cli_ok": len(cli),
        "wall_seconds": round(wall, 2),
        "elapsed": round(wall, 2),
        "avg_seconds": round(wall / len(recs), 2) if recs and wall else 0.0,
        "success_rate": round(len(ok) / len(recs), 4) if recs else 0.0,
        "per_account_seconds": {
            "mean": round(statistics.fmean(elapsed), 2) if elapsed else 0.0,
            "p50": round(_pct(elapsed, 0.5), 2),
            "p95": round(_pct(elapsed, 0.95), 2),
            "min": round(min(elapsed), 2) if elapsed else 0.0,
            "max": round(max(elapsed), 2) if elapsed else 0.0,
        },
        #: 扣掉节流排队后的"真实服务耗时"—— 判断优化是否真的生效看这个，
        #: 而不是含排队的 e2e 均值（节流把等待放进 elapsed，均值天然变大）。
        "service_seconds": {
            "mean": round(statistics.fmean(service), 2) if service else 0.0,
            "p50": round(_pct(service, 0.5), 2),
            "p95": round(_pct(service, 0.95), 2),
        },
        "pace_wait_seconds_mean": round(statistics.fmean(pace), 2) if pace else 0.0,
        "throughput_per_minute": round(len(recs) / wall * 60, 2) if wall else 0.0,
        "stage_seconds_mean": {},
        "failures": [
            {"email": r.get("email"), "stage": r.get("stage"),
             "error": (r.get("error") or "")[:160]}
            for r in bad
        ],
    }

    for stage in (*STAGES, "pace_wait"):
        vals = [float((r.get("timings") or {}).get(stage) or 0) for r in ok
                if (r.get("timings") or {}).get(stage)]
        stats["stage_seconds_mean"][stage] = round(statistics.fmean(vals), 2) if vals else 0.0

    if meta:
        stats["summary_meta"] = meta
    return stats


def print_report(s: dict) -> None:
    pa = s["per_account_seconds"]
    print(f"\n== 批次速率（{s['source']}）==")
    print(f"账号 {s['total']}：成功 {s['keyed']} / 失败 {s['failed']}"
          f" / CLI 会话 {s['cli_ok']}，成功率 {s['success_rate']:.1%}")
    print(f"墙钟 {s['wall_seconds']}s，吞吐 {s['throughput_per_minute']} 个/分钟")
    print(f"单号端到端：平均 {pa['mean']}s / p50 {pa['p50']}s / p95 {pa['p95']}s"
          f"（min {pa['min']}s, max {pa['max']}s）")
    sv = s["service_seconds"]
    print(f"单号服务耗时（扣排队）：平均 {sv['mean']}s / p50 {sv['p50']}s"
          f" / p95 {sv['p95']}s；平均排队 {s['pace_wait_seconds_mean']}s")
    print("分阶段均值：")
    for k, v in s["stage_seconds_mean"].items():
        if v:
            print(f"  {k:<12} {v:>6.2f}s")
    if s["failures"]:
        print("失败：")
        for f in s["failures"][:10]:
            print(f"  ✗ {f['email']} [{f['stage']}] {f['error']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批次速率报告")
    ap.add_argument("--summary", default=str(config.SUMMARY_JSON_PATH))
    ap.add_argument("--since", default=None, help="从台账取数时的时间下界（ISO）")
    ap.add_argument("--json", default=None, help="把报告写到这个路径")
    ap.add_argument("--write-summary", default=None,
                    help="用台账重建批次摘要（结果写到这个路径，如 result/summary.json）")
    args = ap.parse_args(argv)

    recs, wall, meta = _load_batch(Path(args.summary))
    source = f"summary.json（{args.summary}）"
    if not recs:
        recs = _from_ledger(args.since)
        source = "台账 exports/ledger.jsonl"
        if not wall and recs:
            wall = _wall_from_records(recs)
    if not recs:
        print("没有可统计的记录")
        return 1

    stats = report(recs, wall, meta, source=source)
    print_report(stats)

    out = Path(args.json) if args.json else config.RESULT_DIR / "batch_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告: {out}")

    if args.write_summary:
        target = Path(args.write_summary)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "ledger",
            "summary": stats,
            "records": recs,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8")
        print(f"批次摘要: {target}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
