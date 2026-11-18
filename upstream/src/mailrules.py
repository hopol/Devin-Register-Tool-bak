"""收件过滤规则表 —— Devin 侧。

格式对齐兄弟项目（``sender_contains`` 做第一道过滤，主题子串做第二道）。

实测（2026-10-31，devin.ai.har + 三次真实发码）：

    bounces+<...>=<收件人域>@em3779.cognition.ai
        Your Devin Login Code                     ← 唯一一类流量

信封发件人是 VERP 回弹地址：``bounces+44927189-54bb-oai-xxx=mail.test@em3779.cognition.ai``。
它**内嵌了收件人地址**（``=`` 后面那段），可以当一条免费的收件人一致性校验，
但**不要**把它当收件人字段的替代 —— ``to`` 才是权威字段。

主题是必需的判别位：Cognition 还会给注册用户发产品/账单类邮件，
只按域过滤会把营销邮件当成验证码邮件。

引用方式
────────
    from .mailrules import RULES, get, extract_otp

    rule = get("login_code")
    if rule.matches(mail): ...
    code, how = extract_otp(mail.subject, mail.body)   # how ∈ {"subject","anchored","loose","none"}
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

# ── 发件人域（改这里就够了，不要在规则里散落字符串）──────────────────────
SENDER_COGNITION = "cognition.ai"

#: 6 位验证码的"整词"匹配。
#:
#: 🔴 必须带边界断言。VERP 地址里有 ``44927189`` 这种 8 位数字，
#:    用裸 ``\d{6}`` 会把它的前 6 位当成验证码（"449271"）——
#:    这种错不报异常，只是 complete 永远 401，排查方向会被带偏。
OTP_TOKEN = re.compile(r"(?<![\w-])(\d{6})(?![\w-])")

#: 正文锚点模式（预编译：避免把常量模式当"动态正则"传入 re.search）。
ANCHORED_PATTERNS = (
    re.compile(r"verification code below[^\d]{0,120}(\d{6})"),
    re.compile(r"verification code[^\d]{0,80}(\d{6})"),
    re.compile(r"code[^\d]{0,40}(\d{6})"),
)


@dataclass(frozen=True)
class Rule:
    name: str
    sender_contains: str
    subject_all: tuple[str, ...] = ()
    subject_any: tuple[str, ...] = ()
    description: str = ""

    def matches(self, mail) -> bool:
        if self.sender_contains and self.sender_contains not in _norm(mail.sender):
            return False
        subject = _norm(mail.subject)
        if any(tok not in subject for tok in self.subject_all):
            return False
        if self.subject_any:
            return any(tok in subject for tok in self.subject_any)
        return True


def _norm(s: str) -> str:
    """NFKC 归一化 + 小写（把弯引号/全角字符折叠成 ASCII 形态）。"""
    return unicodedata.normalize("NFKC", s or "").lower()


RULES: dict[str, Rule] = {
    "login_code": Rule(
        name="login_code",
        sender_contains=SENDER_COGNITION,
        subject_all=("devin",),
        subject_any=("code", "verif"),
        description="Your Devin Login Code —— 唯一主路径凭据",
    ),
}
CODE_RULES = tuple(RULES)


def get(name: str) -> Rule:
    return RULES[name]


def any_of(*names: str):
    """把若干规则名合成一个 ``match(mail)`` 谓词。"""
    rules = [get(n) for n in names]
    return lambda mail: any(r.matches(mail) for r in rules)


def extract_otp(subject: str, body: str) -> tuple[str, str]:
    """从邮件里抽 6 位验证码，返回 ``(code, how)``。

    优先级（越靠前越可信）：

    1. ``subject``  —— 实测主题形如 ``Your Devin Login Code``，不带码；
       但若站点改成 ``... - 123456`` 这类，主题里的码最可信。
    2. ``anchored`` —— 正文里 ``verification code below`` / ``code`` 之后紧跟的码。
    3. ``loose``    —— 全文中出现次数最多的 6 位整词数字。

    ``loose`` 兜底是必要的：HTML 版正文会重复多遍同一段文案（实测一封邮件里
    验证码出现 4 次），而噪声数字（街道号 103、年份 2026）要么不是 6 位、
    要么不是整词。取"出现次数最多"比"取第一个"稳。
    """
    m = re.search(r"[-–—]\s*(\d{6})\s*$", subject or "")
    if m:
        return m.group(1), "subject"

    text = _norm(body)
    for pattern in (
        r"verification code below[^\d]{0,120}(\d{6})",
        r"verification code[^\d]{0,80}(\d{6})",
        r"\bcode\b[^\d]{0,40}(\d{6})",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1), "anchored"

    tokens = OTP_TOKEN.findall(body or "")
    if tokens:
        code, _ = Counter(tokens).most_common(1)[0]
        return code, "loose"
    return "", "none"


def diagnose(mails: Iterable) -> list[str]:
    """列出**没有**被任何规则命中的主题，用于 ``--mode scan`` 报漏网流量。"""
    out: list[str] = []
    for m in mails:
        if not any(r.matches(m) for r in RULES.values()):
            out.append(f"{m.sender} | {m.subject}")
    return out


@dataclass
class MatchStats:
    """收信诊断计数（只留会被读的）。"""

    polls: int = 0
    rejected_not_code: int = 0
    subjects: list[str] = field(default_factory=list)
