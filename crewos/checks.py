"""自动检查层 — CEO 审阅前的免 token 硬验收。

CC 派单时附 checks(验收规格),产出回来先过机器检查;
CC 只看标红项,不用通读全文逐条核对。检查不通过不会拦截交付——
报告随产出一起给 CC,打回与否仍由 CEO 判断(机器管硬指标,CEO 管质量)。

规格格式(JSON 数组,每项一个检查):
  {"type": "word_count", "min": 300, "max": 1000}     # 字数(去空白字符数)
  {"type": "must_include", "values": ["hook", "CTA"]}  # 全部出现
  {"type": "forbid", "values": ["敬请期待"]}            # 全部不许出现
  {"type": "must_match", "pattern": "^#\\s"}           # 正则(MULTILINE)
  {"type": "json_parseable"}                            # 产出可被 json.loads
  {"type": "min_links", "count": 3}                     # 至少 N 个 http(s) 链接
"""
from __future__ import annotations

import json
import re

KNOWN_TYPES = {"word_count", "must_include", "forbid", "must_match",
               "json_parseable", "min_links"}


def _word_count(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def _run_one(text: str, spec: dict) -> tuple[bool, str]:
    t = spec.get("type", "")
    if t == "word_count":
        n = _word_count(text)
        lo, hi = spec.get("min", 0), spec.get("max", 10 ** 9)
        return lo <= n <= hi, f"字数 {n}(要求 {lo}-{hi if hi < 10**9 else '∞'})"
    if t == "must_include":
        missing = [v for v in spec.get("values", []) if v not in text]
        return not missing, "缺少: " + "、".join(missing) if missing else "必含项齐全"
    if t == "forbid":
        found = [v for v in spec.get("values", []) if v in text]
        return not found, "出现违禁词: " + "、".join(found) if found else "无违禁词"
    if t == "must_match":
        ok = bool(re.search(spec.get("pattern", ""), text, re.MULTILINE))
        return ok, f"正则 {spec.get('pattern', '')!r} " + ("命中" if ok else "未命中")
    if t == "json_parseable":
        # 容忍 ```json 代码块包裹
        body = text.strip()
        m = re.search(r"```(?:json)?\s*\n(.*?)```", body, re.DOTALL)
        if m:
            body = m.group(1)
        try:
            json.loads(body)
            return True, "JSON 可解析"
        except (json.JSONDecodeError, ValueError) as e:
            return False, f"JSON 解析失败: {str(e)[:80]}"
    if t == "min_links":
        n = len(re.findall(r"https?://", text))
        need = int(spec.get("count", 1))
        return n >= need, f"链接 {n} 个(要求 ≥{need})"
    return False, f"未知检查类型: {t}"


def run_checks(text: str, specs: list[dict]) -> dict:
    """跑全部检查。返回 {passed, failures, results}。specs 为空 → 视为通过。"""
    results = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            results.append({"check": str(spec), "ok": False, "detail": "规格必须是对象"})
            continue
        ok, detail = _run_one(text, spec)
        results.append({"check": spec.get("type", "?"), "ok": ok, "detail": detail})
    failures = [r for r in results if not r["ok"]]
    return {"passed": not failures, "results": results,
            "failures": [f"{r['check']}: {r['detail']}" for r in failures]}


def parse_specs(checks_json: str) -> list[dict]:
    """解析 CC 传来的 checks JSON。非法输入返回带说明的单条失败规格,绝不抛异常。"""
    if not checks_json or not checks_json.strip():
        return []
    try:
        specs = json.loads(checks_json)
    except (json.JSONDecodeError, ValueError):
        return [{"type": f"checks_json 不是合法 JSON: {checks_json[:60]}"}]
    if isinstance(specs, dict):
        specs = [specs]
    return specs if isinstance(specs, list) else []
