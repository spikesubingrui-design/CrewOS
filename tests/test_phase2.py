"""Phase 2 测试:cron 解析与触发 / 通知过滤 / Memory Tree / 多模态派单 / 评估报表。

运行: python -m pytest tests/ -v
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.cron import CronScheduler, due, load_jobs
from crewos.ledger import Ledger
from crewos.memory import vault_read, vault_search, vault_write
from crewos.notify import compose
from crewos.router import Router


# ---------- cron ----------

def _t(minute, hour, mday, mon, wday_cron):
    """构造 struct_time。wday_cron: cron 习惯(周日=0)。"""
    return time.struct_time((2026, mon, mday, hour, minute, 0,
                             (wday_cron - 1) % 7, 1, -1))


def test_cron_due():
    assert due("* * * * *", _t(5, 3, 12, 6, 5))
    assert due("30 8 * * *", _t(30, 8, 1, 1, 0))
    assert not due("30 8 * * *", _t(31, 8, 1, 1, 0))
    assert due("*/15 * * * *", _t(45, 0, 1, 1, 0))
    assert not due("*/15 * * * *", _t(46, 0, 1, 1, 0))
    assert due("0 9 * * 1", _t(0, 9, 1, 6, 1))      # 周一
    assert not due("0 9 * * 1", _t(0, 9, 1, 6, 2))
    assert due("0 0 1-7 * *", _t(0, 0, 3, 6, 0))
    assert due("0 8,18 * * *", _t(0, 18, 1, 1, 0))
    assert due("0 9 * * 7", _t(0, 9, 1, 6, 0))      # 7 也是周日
    try:
        due("bad expr", _t(0, 0, 1, 1, 0))
        assert False
    except ValueError:
        pass


def test_cron_tick_fires_once_per_minute():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "config").mkdir()
        (root / "config" / "crontab.yaml").write_text("""
jobs:
  - {name: always, schedule: "* * * * *", agent: tester, instruction: "ping", enabled: true}
  - {name: off, schedule: "* * * * *", agent: tester, instruction: "x", enabled: false}
""", encoding="utf-8")
        agents = root / "agents" / "tester"
        (agents / "memory").mkdir(parents=True)
        (agents / "role.md").write_text("# t", encoding="utf-8")
        (agents / "provider.yaml").write_text(
            'model: "mock-model"\nchannels:\n  - {name: m, endpoint: "mock://", key_env: ""}\n'
            "pricing: {input_per_m: 1.0, output_per_m: 2.0}\n", encoding="utf-8")
        ledger = Ledger(root / "l.db")
        sched = CronScheduler(root, lambda: Router(root / "agents", ledger))
        assert sched.tick() == ["always"]          # enabled=false 的不触发
        assert sched.tick() == []                  # 同一分钟只触发一次
        deadline = time.time() + 5
        while time.time() < deadline:              # 等异步派单落账
            types = [e["type"] for r in ledger._conn.execute(
                "SELECT DISTINCT task_id FROM events").fetchall()
                for e in ledger.task_events(r["task_id"])]
            if "task_result" in types:
                break
            time.sleep(0.1)
        assert "task_result" in types and "task_created" in types


# ---------- notify ----------

def test_notify_compose_filters():
    silent = {"type": "task_assign", "from_agent": "ceo", "to_agent": "writer",
              "task_id": "t1", "payload": "{}"}
    assert compose(silent) is None
    l1 = {"type": "risk_action", "from_agent": "writer", "to_agent": "",
          "task_id": "t1", "payload": json.dumps({"risk": 1, "summary": "存草稿"})}
    assert compose(l1) is None
    l2 = dict(l1, payload=json.dumps({"risk": 2, "summary": "拉外部数据"}))
    assert "拉外部数据" in compose(l2)
    ap = {"type": "approval_request", "from_agent": "writer", "to_agent": "user",
          "task_id": "t1", "payload": json.dumps(
              {"approval_id": "abc123", "risk": 4, "label": "L4·审批", "summary": "发布"})}
    text = compose(ap)
    assert "crewos approve abc123" in text and "L4" in text


def test_notify_platform_bodies():
    """各平台出站载荷契约:飞书加签 / 企业微信 text / Discord content。"""
    import base64
    import hashlib
    import hmac

    from crewos.notify import (_feishu_sign, discord_body, feishu_body,
                               wecom_body)
    # 飞书:不加签只有 msg_type/content;加签追加 timestamp+sign 且签名可复算
    plain = feishu_body("hi")
    assert plain == {"msg_type": "text", "content": {"text": "hi"}}
    signed = feishu_body("hi", "SEC")
    assert signed["sign"] and signed["timestamp"]
    expect = base64.b64encode(hmac.new(
        f"{signed['timestamp']}\nSEC".encode(), b"", hashlib.sha256).digest()).decode()
    assert signed["sign"] == expect == _feishu_sign("SEC", signed["timestamp"])
    # 企业微信:msgtype=text,内容截断 2000
    wb = wecom_body("x" * 3000)
    assert wb["msgtype"] == "text" and len(wb["text"]["content"]) == 2000
    # Discord:content 截到 1900
    db = discord_body("y" * 3000)
    assert len(db["content"]) == 1900


def test_notify_push_routes_to_all_channels(monkeypatch):
    """push 把同一条事件分发到所有已配置通道(飞书/企业微信/Discord/通用);未配置的不发。"""
    from crewos import notify
    sent = []
    monkeypatch.setattr(notify, "_post_json", lambda url, body, timeout=5: sent.append((url, body)))
    ev = {"type": "task_done", "from_agent": "ceo", "to_agent": "user",
          "task_id": "t9", "payload": json.dumps({"summary": "完成"}), "cost_usd": 0.01}
    notify.push({"feishu_webhook": "https://feishu/x", "wecom_webhook": "https://qyapi/x",
                 "discord_webhook": "https://discord/x", "webhook_url": "https://generic/x"}, ev)
    urls = [u for u, _ in sent]
    assert urls == ["https://feishu/x", "https://qyapi/x", "https://discord/x", "https://generic/x"]
    # 一个都没配 → 不发
    sent.clear()
    notify.push({}, ev)
    assert sent == []


# ---------- Memory Tree ----------

def test_vault_roundtrip_and_search():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        vault_write(root, "projects/小红书账号.md", "# 小红书\n踩坑:hook 抒情开头数据差")
        vault_write(root, "projects/小红书账号.md", "下次:用反差开头")
        text = vault_read(root, "projects/小红书账号.md")
        assert "踩坑" in text and "反差开头" in text
        hits = vault_search(root, "hook 抒情")
        assert hits and hits[0]["file"] == "projects/小红书账号.md"
        assert vault_search(root, "不存在的词xyz") == []


def test_vault_path_safety():
    with tempfile.TemporaryDirectory() as d:
        for bad in ("../escape.md", "a/../../x.md", "notes.txt", "/etc/passwd.md"):
            try:
                vault_write(Path(d), bad, "x")
                assert False, f"应拒绝 {bad}"
            except ValueError:
                pass


# ---------- 多模态派单 ----------

def make_agent(tmp: Path) -> Path:
    ws = tmp / "agents" / "perceiver"
    (ws / "memory").mkdir(parents=True)
    (ws / "role.md").write_text("# 视觉", encoding="utf-8")
    (ws / "provider.yaml").write_text(
        'model: "mock-vlm"\nchannels:\n  - {name: m, endpoint: "mock://", key_env: ""}\n'
        "pricing: {input_per_m: 1.0, output_per_m: 2.0}\n", encoding="utf-8")
    return tmp / "agents"


def test_dispatch_with_media_url():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_agent(tmp), Ledger(tmp / "l.db"))
        r = router.dispatch("perceiver", "提取这条视频的文案",
                            media_url="https://example.com/v.mp4")
        assert "mock-vlm" in r["content"]
        assign = [e for e in router.ledger.task_events(r["task_id"])
                  if e["type"] == "task_assign"][0]
        assert json.loads(assign["payload"])["media_url"] == "https://example.com/v.mp4"


def test_media_part_types():
    from crewos.router import _media_part
    assert _media_part("https://x.com/a.png")["type"] == "image_url"
    assert _media_part("https://x.com/a.JPG?sig=1")["type"] == "image_url"
    assert _media_part("https://x.com/a.mp4")["type"] == "video_url"


# ---------- 评估报表 ----------

def test_eval_report_aggregation():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "l.db")
        t1 = led.new_task("一次过的任务")
        led.log(t1, "task_result", "writer", "ceo", model="qwen", cost_usd=0.01)
        led.log(t1, "task_done", "ceo", "user")
        t2 = led.new_task("打回一轮后上报")
        led.log(t2, "task_result", "writer", "ceo", model="qwen", cost_usd=0.01)
        led.log(t2, "task_result", "writer", "ceo", round=1, model="qwen", cost_usd=0.01)
        led.log(t2, "escalation", "ceo", "user")
        rep = led.eval_report()
        assert rep["samples"] == 2
        row = rep["by_agent_model"][0]
        assert row["agent"] == "writer" and row["model"] == "qwen"
        assert row["tasks"] == 2 and row["first_pass_rate"] == 0.5
        assert row["avg_rounds"] == 0.5 and row["escalations"] == 1
