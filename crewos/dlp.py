"""出站 DLP 扫描 — 客户唯一绝对红线:隐私不外泄。

所有 agent 产出在返回/对外发送前必须过这一层。
命中 secret 类直接拦截;命中 pii 类标记警告(语义级泄露只能尽力检测,
重要发布保留 L3 倒计时由人复核)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# secret 类:命中即拦截
SECRET_PATTERNS: dict[str, str] = {
    "anthropic_key": r"sk-ant-[A-Za-z0-9\-_]{20,}",
    "openai_key": r"sk-(?!ant-)[A-Za-z0-9\-_]{20,}",
    "openrouter_key": r"sk-or-[A-Za-z0-9\-_]{20,}",
    "generic_api_key": r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9\-_]{16,}",
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "private_key_block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{36,}",
}

# pii 类:标记警告
PII_PATTERNS: dict[str, str] = {
    "bank_card": r"(?<!\d)\d{16,19}(?!\d)",
    "cn_id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "cn_phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email_addr": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
}


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number]
    odd, even = digits[-1::-2], digits[-2::-2]
    return (sum(odd) + sum(sum(divmod(2 * d, 10)) for d in even)) % 10 == 0


@dataclass
class ScanResult:
    blocked: bool = False
    hits: list[dict] = field(default_factory=list)
    redacted_text: str = ""

    @property
    def warnings(self) -> list[dict]:
        return [h for h in self.hits if h["severity"] == "warn"]


def scan(text: str, extra_blocklist: list[str] | None = None) -> ScanResult:
    """扫描文本。extra_blocklist: 用户自定义绝不外泄的字符串(如具体地址)。"""
    result = ScanResult(redacted_text=text)

    for name, pattern in SECRET_PATTERNS.items():
        for m in re.finditer(pattern, text):
            result.hits.append({"rule": name, "severity": "block",
                                "match": m.group()[:8] + "...["+name+"]"})
            result.blocked = True
            result.redacted_text = result.redacted_text.replace(
                m.group(), f"[REDACTED:{name}]")

    for item in (extra_blocklist or []):
        if item and item in text:
            result.hits.append({"rule": "user_blocklist", "severity": "block",
                                "match": item[:4] + "...[user_blocklist]"})
            result.blocked = True
            result.redacted_text = result.redacted_text.replace(
                item, "[REDACTED:user_blocklist]")

    for name, pattern in PII_PATTERNS.items():
        for m in re.finditer(pattern, text):
            if name == "bank_card" and not _luhn_ok(m.group()):
                continue  # 非银行卡的长数字串放行,减少误报
            result.hits.append({"rule": name, "severity": "warn",
                                "match": m.group()[:4] + "...["+name+"]"})

    return result
