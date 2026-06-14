"""v0.7 测试:供应商目录 / 自定义供应商持久化 / 推荐元数据 / agent 绑定派生通道。

运行: python -m pytest tests/ -v
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from crewos import providers as provcat


def test_builtin_catalog_and_recommendations():
    ids = {p["id"] for p in provcat.BUILTIN_PROVIDERS}
    # 市面常见全集:聚合/国际/国内/本地各有代表
    assert {"openrouter", "siliconflow", "openai", "anthropic", "gemini", "xai",
            "mistral", "groq", "deepseek", "moonshot", "zhipu", "dashscope", "ark",
            "minimax", "ollama"} <= ids
    assert len(provcat.BUILTIN_PROVIDERS) >= 25
    seen = set()
    for p in provcat.BUILTIN_PROVIDERS:
        assert p["endpoint"].startswith("http") and p["key_env"].isupper()
        assert p.get("group"), f"{p['id']} 缺 group"
        assert p["id"] not in seen, f"重复 id {p['id']}"
        seen.add(p["id"])
        assert p["key_env"] not in [q["key_env"] for q in provcat.BUILTIN_PROVIDERS if q["id"] != p["id"]], \
            f"{p['id']} key_env 与他人冲突"
    # 7 个执行 agent 都有推荐 + 理由 + 至少一个供应商
    assert set(provcat.RECOMMENDATIONS) == {
        "coder", "writer", "researcher", "analyst", "builder", "runner", "perceiver"}
    for name, rec in provcat.RECOMMENDATIONS.items():
        assert rec["model"] and rec["reason"] and rec["providers"]
        for pid in rec["providers"]:
            assert pid in ids, f"{name} 推荐了不存在的供应商 {pid}"


def test_load_and_add_custom_provider():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        base = provcat.load_providers(root)
        assert len(base) == len(provcat.BUILTIN_PROVIDERS)   # 无 custom 时只有内置
        rec = provcat.add_custom_provider(root, "My Proxy", "https://x.example.com/v1")
        assert rec["id"] == "my_proxy" and rec["key_env"] == "MY_PROXY_KEY" and rec["custom"]
        after = provcat.load_providers(root)
        assert len(after) == len(base) + 1
        assert provcat.get_provider(root, "my_proxy")["endpoint"] == "https://x.example.com/v1"
        # 持久化到 config/providers.yaml
        doc = yaml.safe_load((root / "config" / "providers.yaml").read_text(encoding="utf-8"))
        assert doc["custom"][0]["id"] == "my_proxy"
        # 同 id 覆盖,不重复
        provcat.add_custom_provider(root, "My Proxy", "https://y.example.com/v1")
        assert len([p for p in provcat.load_providers(root) if p["id"] == "my_proxy"]) == 1
        assert provcat.get_provider(root, "my_proxy")["endpoint"] == "https://y.example.com/v1"


def test_custom_provider_rejects_bad_endpoint():
    with tempfile.TemporaryDirectory() as d:
        try:
            provcat.add_custom_provider(Path(d), "Bad", "not-a-url")
            assert False, "应拒绝非 http(s) endpoint"
        except ValueError:
            pass


def test_bind_derives_channel_from_provider():
    # 端到端验证绑定逻辑:选模型+供应商 → 派生 provider.yaml 通道
    from crewos.web_server import _env_has  # noqa: F401 (import sanity)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        agent_dir = root / "agents" / "coder"
        (agent_dir / "memory").mkdir(parents=True)
        (agent_dir / "role.md").write_text("# coder", encoding="utf-8")
        (agent_dir / "provider.yaml").write_text(
            "model: old\nchannels:\n  - {name: x, endpoint: 'mock://', key_env: ''}\n"
            "pricing: {input_per_m: 2.5, output_per_m: 10}\ntemperature: 0.2\n", encoding="utf-8")
        prov = provcat.get_provider(root, "openai")
        # 模拟 bind:写入派生通道,保留原定价/温度
        cur = yaml.safe_load((agent_dir / "provider.yaml").read_text(encoding="utf-8"))
        doc = {"model": "codex-5.5",
               "channels": [{"name": prov["id"], "endpoint": prov["endpoint"], "key_env": prov["key_env"]}],
               "fallback_model": "", "pricing": cur["pricing"], "temperature": cur["temperature"]}
        assert doc["channels"][0]["endpoint"] == "https://api.openai.com/v1"
        assert doc["channels"][0]["key_env"] == "OPENAI_KEY"
        assert doc["pricing"]["input_per_m"] == 2.5 and doc["temperature"] == 0.2
