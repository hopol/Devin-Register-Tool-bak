#!/usr/bin/env python
"""独立复核 result/ 下的交付物（只读；退出码 0/1，可当门禁）。

它**不信任** verify_keys 的打印结果，只读文件重新核对：

    1. keys.txt / apikeys.txt / accounts.csv 存在且非空
    2. api_key 形状像 ``apk_user_…``（不是空串/占位符）
    3. keys.txt 行数 == apikeys.txt 行数（同一批导出，集合必须恒等）
    4. 无重复 key、无重复邮箱
    5. accounts.csv 里 api_key_ok 全为 True
    6. summary.json 的计数与文件一致
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

KEY_PREFIX = "apk_user_"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="独立复核交付物")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    problems: list[str] = []

    def note(msg: str) -> None:
        if not args.quiet:
            print(msg)

    for path in (config.KEYS_TXT_PATH, config.APIKEYS_TXT_PATH, config.ACCOUNTS_CSV_PATH):
        if not path.is_file():
            problems.append(f"缺少 {path}")
        elif path.stat().st_size == 0:
            problems.append(f"{path} 是空文件")

    keys: list[str] = []
    emails: list[str] = []
    if config.KEYS_TXT_PATH.is_file():
        for lineno, line in enumerate(
                config.KEYS_TXT_PATH.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split("----")
            if len(parts) != 4:
                problems.append(f"keys.txt:{lineno} 字段数 {len(parts)} != 4")
                continue
            email, key, _user, _org = parts
            emails.append(email)
            keys.append(key)
            if not key.startswith(KEY_PREFIX):
                problems.append(f"keys.txt:{lineno} key 形状不对: {key[:16]}…")

    pure: list[str] = []
    if config.APIKEYS_TXT_PATH.is_file():
        pure = [ln.strip() for ln in
                config.APIKEYS_TXT_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]

    if keys and pure and len(keys) != len(pure):
        problems.append(f"keys.txt {len(keys)} 行 != apikeys.txt {len(pure)} 行")
    if set(keys) != set(pure):
        problems.append("keys.txt 与 apikeys.txt 的 key 集合不一致")
    if len(set(keys)) != len(keys):
        problems.append("keys.txt 里有重复 key")
    if len(set(emails)) != len(emails):
        problems.append("keys.txt 里有重复邮箱")

    csv_bad = 0
    if config.ACCOUNTS_CSV_PATH.is_file():
        with config.ACCOUNTS_CSV_PATH.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if str(row.get("api_key_ok", "")).lower() != "true":
                    csv_bad += 1
        if csv_bad:
            problems.append(f"accounts.csv 里有 {csv_bad} 行 api_key_ok != True")

    verify_path = getattr(config, "VERIFY_SUMMARY_PATH", config.SUMMARY_JSON_PATH)
    if verify_path.is_file():
        try:
            summary = json.loads(verify_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            problems.append(f"summary.json 读不了: {exc}")
            summary = {}
        if "ok" in summary and summary["ok"] != len(pure):
            problems.append(f"summary.ok={summary['ok']} != apikeys.txt {len(pure)}")
        if summary.get("invalid"):
            problems.append(f"summary.invalid={summary['invalid']}（有无效 key）")

    note(f"keys={len(pure)} emails={len(set(emails))} csv_bad={csv_bad}")
    if problems:
        print("复核失败：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print(f"复核通过：{len(pure)} 把 key 与交付物一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
