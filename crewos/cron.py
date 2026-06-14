"""定时任务 — config/crontab.yaml,标准 5 段 cron 表达式,零外部依赖。

调度线程随 crewos start 启动(Mission Control 在,日程就在)。
每分钟热加载 crontab.yaml:改完即生效,不用重启。
任务本体就是一次派单,全程入台账,受预算熔断/DLP 保护。

crontab.yaml 格式:
jobs:
  - name: daily-briefing
    schedule: "30 8 * * *"        # 分 时 日 月 周(本地时间)
    agent: researcher
    instruction: "搜集今天 AI agent 领域热点,输出 5 条带来源的简报"
    enabled: true
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml

from .ledger import Ledger
from .router import Router


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """单个 cron 字段 → 命中值集合。支持 * , - / 组合。"""
    out: set[int] = set()
    for part in str(field).split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            lo_p, hi_p = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            lo_p, hi_p = int(a), int(b)
        else:
            lo_p = hi_p = int(part)
        if not (lo <= lo_p <= hi_p <= hi):
            raise ValueError(f"cron 字段越界: {field} (允许 {lo}-{hi})")
        out.update(range(lo_p, hi_p + 1, step))
    return out


def due(expr: str, t: time.struct_time) -> bool:
    """该分钟是否命中 cron 表达式。周日 = 0 或 7 都认。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式必须 5 段: {expr!r}")
    minute, hour, dom, month, dow = fields
    wday = (t.tm_wday + 1) % 7  # struct_time 周一=0 → cron 周日=0
    dom_match = t.tm_mday in _parse_field(dom, 1, 31)
    dow_match = (wday in _parse_field(dow, 0, 7)
                 or (wday == 0 and 7 in _parse_field(dow, 0, 7)))
    # 标准 cron 语义:dom 和 dow 都非 * 时取「或」(任一命中即可);否则各字段「与」
    if dom.strip() != "*" and dow.strip() != "*":
        day_match = dom_match or dow_match
    else:
        day_match = dom_match and dow_match
    return (t.tm_min in _parse_field(minute, 0, 59)
            and t.tm_hour in _parse_field(hour, 0, 23)
            and t.tm_mon in _parse_field(month, 1, 12)
            and day_match)


def load_jobs(root: Path) -> list[dict]:
    f = root / "config" / "crontab.yaml"
    if not f.exists():
        return []
    doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return [j for j in (doc.get("jobs") or []) if isinstance(j, dict)]


class CronScheduler:
    """随 Web 服务运行的调度线程。同一任务同一分钟只触发一次。"""

    def __init__(self, root: Path, router_factory):
        self.root = Path(root)
        self.router_factory = router_factory  # 每次触发新建 Router,拿到最新配置
        self._fired: dict[str, int] = {}      # job name → 上次触发的分钟戳
        self._stop = threading.Event()

    def _fire(self, job: dict):
        router: Router = self.router_factory()
        ledger: Ledger = router.ledger
        tid = ledger.new_task(f"[CRON] {job['name']}", created_by="cron")
        try:
            router.dispatch(job["agent"], job["instruction"], task_id=tid)
        except Exception as e:
            ledger.log(tid, "escalation", "cron", "user", payload={
                "reason": f"定时任务 {job['name']} 执行失败", "error": str(e)[:300]})

    def tick(self, now: float | None = None) -> list[str]:
        """检查并触发到点的任务。返回本次触发的任务名(供测试)。"""
        now = now or time.time()
        minute_stamp = int(now // 60)
        t = time.localtime(now)
        fired = []
        for job in load_jobs(self.root):
            name = job.get("name", "")
            if not name or not job.get("enabled", True):
                continue
            if self._fired.get(name) == minute_stamp:
                continue
            try:
                if not due(str(job.get("schedule", "")), t):
                    continue
            except ValueError:
                continue
            self._fired[name] = minute_stamp
            fired.append(name)
            threading.Thread(target=self._fire, args=(job,), daemon=True).start()
        return fired

    def run_forever(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(20)

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()

    def stop(self):
        self._stop.set()
