"""JSONL 台账：追加写、按唯一键并集合并、幂等、可补录。

四条硬规矩（与兄弟项目同一套，都是踩过坑换来的）：

1. **并集**合并，不"比大小/比字段数" —— 启发式总有相等或边界的死角。
2. **能读自己的输出** —— 台账很容易变成唯一副本，重跑不能缩水。
3. **补录按事件真实发生时间入账**，不是补录那一刻。
4. 🔴 **状态等级表的词汇必须与写入方实际写的词汇一致。**
   否则等级判断静默失效 —— 一次重跑失败会把 api_key 覆盖成空串。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: **可重入**锁：``upsert_many`` 的"读-改-写"必须整体原子，
#: 而 ``_rewrite`` 自己也取锁 ⇒ 必须 RLock，否则自死锁。
_LOCK = threading.RLock()

#: 记录状态优先级：只用于"升级 / 降级"判断，不用来决定"是否写入"。
#:
#: 🔴 本表的键必须覆盖 ``src/stages.py`` 实际写入的每一个 status 字面量。
#:    新增字面量而没登记 ⇒ 落 0 分（与 failed 同级）⇒ 重跑一次就把 api_key 清掉。
RANK: dict[str, int] = {
    "keyed": 6,        # 终态成功：拿到 api_key
    "registered": 5,   # 会话已建立（有 user_id/org_id）
    "code_sent": 4,    # 验证码已发出
    "mailbox": 3,      # 只建了临时邮箱
    "partial": 2,      # 有信息但没 key（例如 key 已存在却取不回来）
    "failed": 0,
    "": 0,
}


def rank(status: str | None) -> int:
    return RANK.get(status or "", 0)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            # 断电/并发可能留半行：跳过，不为半行垃圾丢掉整份台账。
            continue
        if isinstance(d, dict) and d.get("email"):
            out.append(d)
    return out


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """按 email 去重（末行胜出，但高等级记录优先保留其非空字段）。"""
    from . import config

    path = path or config.LEDGER_PATH
    with _LOCK:
        merged: dict[str, dict[str, Any]] = {}
        for rec in _read(path):
            email = rec["email"]
            prev = merged.get(email)
            merged[email] = _merge(prev, rec) if prev else rec
        return merged


def _merge(prev: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """等级高的一方为主，另一方补齐空字段。

    🔴 这样设计是为了让"重跑失败"不会抹掉已有的 api_key：
    旧记录是 keyed（6 分），新记录是 failed（0 分）⇒ 以旧为主，
    但新记录的 ``error`` / ``last_attempt`` 仍然被并入（诊断需要）。
    """
    if rank(new.get("status")) >= rank(prev.get("status")):
        primary, secondary = dict(new), prev
    else:
        primary, secondary = dict(prev), new

    for key, val in secondary.items():
        if key in ("email", "status"):
            continue
        if primary.get(key) in (None, "", 0, [], {}) and val not in (None, "", 0, [], {}):
            primary[key] = val
    # 次级记录的 error/last_attempt 永远保留：那是"最近一次尝试"的真实信息。
    # ``retryable`` 同理 —— 它描述的是**最新一次尝试**的结论（能不能再试），
    # 不是账号的长期属性。旧记录是 keyed、新记录是"key 已存在"时，
    # 必须听新的（False），否则 resume 会反复烧同一个地址的发码配额。
    for key in ("error", "last_attempt", "last_stage", "retryable"):
        if new.get(key) not in (None, ""):
            primary[key] = new[key]
    return primary


def append(rec: dict[str, Any], path: Path | None = None) -> None:
    from . import config

    path = path or config.LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    with _LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _rewrite(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _LOCK, tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def upsert_many(records: Iterable[dict[str, Any]], path: Path | None = None) -> None:
    """把若干记录并进台账（按 email 去重、等级合并）。"""
    from . import config

    path = path or config.LEDGER_PATH
    with _LOCK:
        merged = load(path)
        for rec in records:
            email = rec.get("email")
            if not email:
                continue
            prev = merged.get(email)
            merged[email] = _merge(prev, rec) if prev else rec
        _rewrite(merged.values(), path)


def persist(rec: dict[str, Any]) -> None:
    """把一条账号记录写进台账 + 成功台账（``keyed`` 才进成功台账）。"""
    from . import config

    append(rec)
    if rec.get("api_key"):
        append(rec, config.SUCCESS_PATH)


def keyed_records(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """只取拿到 api_key 的账号（交付物口径）。

    🔴 默认同时读**运行台账**和**成功台账**（``result/success.jsonl``）：
    成功台账是 append-only 的耐久副本，而运行台账（``exports/ledger.jsonl``）
    常被清理/重建。2026-10-31 实测踩过一次：清掉 exports 后跑一次 1 号批次，
    ``verify_keys`` 只看到 1 把 key，把 245 把的交付物覆盖掉了。
    ``success.jsonl`` 里 733 行 / 246 个唯一账号 —— 它才是重建的真源。
    """
    from . import config

    if path is not None:
        return {e: r for e, r in load(path).items() if r.get("api_key")}
    merged = load()
    for email, rec in load(config.SUCCESS_PATH).items():
        prev = merged.get(email)
        merged[email] = _merge(prev, rec) if prev else rec
    return {e: r for e, r in merged.items() if r.get("api_key")}
