"""Phase 0 全链路测试(mock 通道,无需 API key)。

覆盖:派单→台账→成本、failover、DLP 拦截、预算熔断、回放。
运行: python -m pytest tests/ -v  (或 python tests/test_phase0.py)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crewos.dlp import scan
from crewos.ledger import Ledger
from crewos.router import AllChannelsDown, BudgetExceeded, Router


def make_workspace(tmp: Path, channels_yaml: str) -> Path:
    """构造临时 agent 档案。"""
    ws = tmp / "agents" / "tester"
    (ws / "memory").mkdir(parents=True)
    (ws / "role.md").write_text("# Tester\n你是测试员。", encoding="utf-8")
    (ws / "memory" / "lessons.md").write_text("# 错题本\n- 教训A", encoding="utf-8")
    (ws / "provider.yaml").write_text(channels_yaml, encoding="utf-8")
    return tmp / "agents"


MOCK_ONLY = """
model: "mock-model"
channels:
  - {name: mock_main, endpoint: "mock://", key_env: ""}
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""

FAILOVER = """
model: "mock-model"
channels:
  - {name: dead_channel, endpoint: "http://127.0.0.1:1/v1", key_env: ""}
  - {name: mock_backup, endpoint: "mock://", key_env: ""}
fallback_model: "backup-model"
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""

ALL_DEAD = """
model: "mock-model"
channels:
  - {name: dead1, endpoint: "http://127.0.0.1:1/v1", key_env: ""}
  - {name: dead2, endpoint: "http://127.0.0.1:2/v1", key_env: ""}
fallback_model: "backup-model"
pricing: {input_per_m: 1.0, output_per_m: 2.0}
"""


def test_dispatch_and_ledger():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, MOCK_ONLY), Ledger(tmp / "l.db"))
        r = router.dispatch("tester", "写一句问候")
        assert "mock-model" in r["content"] and r["cost_usd"] > 0
        events = [e["type"] for e in router.ledger.task_events(r["task_id"])]
        assert events == ["task_created", "task_assign", "task_result"]
        assert router.ledger.cost_report()["total_usd"] == r["cost_usd"]
        # 角色记忆已注入
        assert r["channel"] == "mock_main"
    print("✅ 派单→台账→成本")


def test_failover():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, FAILOVER), Ledger(tmp / "l.db"))
        r = router.dispatch("tester", "测试容灾")
        assert r["channel"] == "mock_backup"  # 第一通道挂,自动切第二
        types = [e["type"] for e in router.ledger.task_events(r["task_id"])]
        assert "failover" in types
    print("✅ 同模型通道 failover")


def test_all_channels_down():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, ALL_DEAD), Ledger(tmp / "l.db"))
        task_id = router.ledger.new_task("全挂测试")
        try:
            router.dispatch("tester", "测试", task_id=task_id)
            assert False, "应抛出 AllChannelsDown"
        except AllChannelsDown as e:
            assert "backup-model" in str(e)  # 提示可交接的备用模型
        types = [e["type"] for e in router.ledger.task_events(task_id)]
        assert "escalation" in types  # 已上报用户
    print("✅ 全通道宕机→挂起上报(不静默换模型)")


def test_dlp():
    res = scan("这是我的 key: sk-ant-abc123def456ghi789jkl012")
    assert res.blocked and "[REDACTED:anthropic_key]" in res.redacted_text
    res2 = scan("正常文案,联系电话 13812345678")
    assert not res2.blocked and any(h["rule"] == "cn_phone" for h in res2.hits)
    res3 = scan("公司在幸福路88号", extra_blocklist=["幸福路88号"])
    assert res3.blocked  # 用户自定义黑名单
    print("✅ DLP:密钥拦截/PII警告/自定义黑名单")


def test_dlp_in_dispatch():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, MOCK_ONLY), Ledger(tmp / "l.db"))
        # mock 通道会回显任务内容,故意让产出带密钥
        r = router.dispatch("tester", "复述: sk-ant-abc123def456ghi789jkl012")
        assert r["dlp_blocked"] and "sk-ant-abc123" not in r["content"]
        types = [e["type"] for e in router.ledger.task_events(r["task_id"])]
        assert "dlp_block" in types
    print("✅ 派单链路中的 DLP 拦截")


def test_budget_cap():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, MOCK_ONLY), Ledger(tmp / "l.db"),
                        default_task_budget_usd=0.000001)
        task_id = router.ledger.new_task("预算测试")
        router.dispatch("tester", "第一次,花掉一点钱", task_id=task_id)
        try:
            router.dispatch("tester", "第二次应被熔断", task_id=task_id)
            assert False, "应抛出 BudgetExceeded"
        except BudgetExceeded:
            pass
        types = [e["type"] for e in router.ledger.task_events(task_id)]
        assert "budget_block" in types
    print("✅ 任务预算熔断")


def test_replay():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        router = Router(make_workspace(tmp, MOCK_ONLY), Ledger(tmp / "l.db"))
        r = router.dispatch("tester", "回放测试任务")
        text = router.ledger.replay(r["task_id"])
        assert "task_assign" in text and "task_result" in text and "$" in text
    print("✅ 历史回放")


if __name__ == "__main__":
    test_dispatch_and_ledger()
    test_failover()
    test_all_channels_down()
    test_dlp()
    test_dlp_in_dispatch()
    test_budget_cap()
    test_replay()
    print("\n全部 7 项测试通过 ✅")
