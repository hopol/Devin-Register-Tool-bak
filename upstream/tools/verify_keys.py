#!/usr/bin/env python
"""验收 + 导出：真打推理接口验证每把 key，并写出 result/ 下的交付物。

判据（与兄弟项目同一口径）：**key 能不能真用** ——
``GET https://api.devin.ai/v1/sessions`` 返回 200 即通过。
这是只读接口，不会产生会话、不消耗额度。

产物（覆盖写，但内容来自台账，可重复生成）：

    result/keys.txt        email----api_key----user_id----org_id
    result/apikeys.txt     纯 api_key，一行一个（直接灌其它系统用）
    result/accounts.csv    账号信息明细（Excel 可直接打开）
    result/summary.json    本批统计 + 每把 key 的验收结果
    result/success.jsonl   账号级成功台账（由 run_e2e 追加，这里不重写）

退出码：0 = 全部有效；1 = 有无效 key（可当门禁用）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, ledger  # noqa: E402
from src.devin import verify_api_key  # noqa: E402

CSV_COLUMNS = [
    "email", "status", "user_id", "org_id", "org_name", "account_id",
    "plan_slug", "available_credits", "onboarding_status", "site_created_at",
    "api_key", "elapsed", "api_key_ok", "api_key_status", "api_key_error",
]


def _verify_one(rec: dict) -> dict:
    result = verify_api_key(rec["api_key"])
    out = dict(rec)
    out["api_key_ok"] = bool(result.get("ok"))
    out["api_key_status"] = result.get("status")
    out["api_key_error"] = result.get("error", "")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验收 API Key 并导出交付物")
    ap.add_argument("--limit", type=int, default=None, help="只验收前 N 条")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)

    records = list(ledger.keyed_records().values())
    records.sort(key=lambda r: r.get("last_attempt") or "")
    if args.limit:
        records = records[: args.limit]
    if not records:
        print("台账里没有带 api_key 的记录 —— 先跑 tools/run_e2e.py")
        return 1

    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        verified = list(pool.map(_verify_one, records))
    elapsed = time.time() - started

    ok = [r for r in verified if r["api_key_ok"]]
    bad = [r for r in verified if not r["api_key_ok"]]

    config.RESULT_DIR.mkdir(parents=True, exist_ok=True)
    config.KEYS_TXT_PATH.write_text(
        "".join(f"{r['email']}----{r['api_key']}----{r.get('user_id','')}----{r.get('org_id','')}\n"
                for r in ok),
        encoding="utf-8")
    config.APIKEYS_TXT_PATH.write_text(
        "".join(f"{r['api_key']}\n" for r in ok), encoding="utf-8")

    with config.ACCOUNTS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in verified:
            writer.writerow(r)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verified": len(verified),
        "ok": len(ok),
        "invalid": len(bad),
        "elapsed": round(elapsed, 2),
        "invalid_records": [
            {"email": r["email"], "status": r.get("api_key_status"),
             "error": r.get("api_key_error")} for r in bad
        ],
    }
    config.VERIFY_SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"验收 {len(verified)} 把：有效 {len(ok)} / 无效 {len(bad)}，{elapsed:.1f}s")
    for r in bad:
        print(f"  ✗ {r['email']}: HTTP {r.get('api_key_status')} {r.get('api_key_error')}")
    print(f"\n交付物：\n  {config.KEYS_TXT_PATH}\n  {config.APIKEYS_TXT_PATH}"
          f"\n  {config.ACCOUNTS_CSV_PATH}\n  {config.SUMMARY_JSON_PATH}")
    if bad:
        print("\n⚠️ 有无效 key：不要把这些行交付。")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
