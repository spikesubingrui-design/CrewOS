"""供应商目录 + 每个 agent 的推荐模型 —— Hermes / OpenClaw 式配置体验。

用户不用懂 endpoint、不用手填 model id:在看板「供应商」面板里,选一个供应商、
粘贴一个 API key 就能用;每个 agent 旁边给出推荐模型 + 为什么推荐 + 哪些供应商
能供给它,点「用推荐」一键绑定。还支持「自定义供应商」(任何 OpenAI 兼容端点)。

- 内置供应商目录写在代码里(BUILTIN_PROVIDERS),稳定不变。
- 用户新增的自定义供应商持久化到 <工作区>/config/providers.yaml 的 custom: 段。
- key 仍然只存 ~/.crewos/.env(环境变量),永不进 agent 上下文 —— 安全铁律不变,
  变的只是「key 可以从本地看板粘贴录入」(和 Hermes/OpenClaw 一样)。
"""
from __future__ import annotations

from pathlib import Path

import yaml

# 内置供应商(全部走 OpenAI 兼容 /v1)。endpoint 已填好,用户只需粘 key。
BUILTIN_PROVIDERS = [
    # ——— 聚合 / 网关(一个 key 覆盖多家模型)———
    {"id": "openrouter", "name": "OpenRouter", "group": "聚合网关", "endpoint": "https://openrouter.ai/api/v1",
     "key_env": "OPENROUTER_KEY", "signup": "https://openrouter.ai/keys",
     "note": "一个 key 覆盖 400+ 模型,最省事。新手强烈建议先配这一个。"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow", "group": "聚合网关", "endpoint": "https://api.siliconflow.cn/v1",
     "key_env": "SILICONFLOW_KEY", "signup": "https://cloud.siliconflow.cn/account/ak",
     "note": "国内聚合,DeepSeek/Qwen/GLM/Kimi 等一站式,国内访问快。"},
    {"id": "together", "name": "Together AI", "group": "聚合网关", "endpoint": "https://api.together.xyz/v1",
     "key_env": "TOGETHER_KEY", "signup": "https://api.together.xyz/settings/api-keys",
     "note": "开源模型聚合,Llama/Qwen/DeepSeek 等。"},
    {"id": "fireworks", "name": "Fireworks AI", "group": "聚合网关", "endpoint": "https://api.fireworks.ai/inference/v1",
     "key_env": "FIREWORKS_KEY", "signup": "https://fireworks.ai/account/api-keys",
     "note": "高速开源模型推理。"},
    {"id": "deepinfra", "name": "DeepInfra", "group": "聚合网关", "endpoint": "https://api.deepinfra.com/v1/openai",
     "key_env": "DEEPINFRA_KEY", "signup": "https://deepinfra.com/dash/api_keys",
     "note": "便宜的开源模型托管。"},
    {"id": "novita", "name": "Novita AI", "group": "聚合网关", "endpoint": "https://api.novita.ai/v3/openai",
     "key_env": "NOVITA_KEY", "signup": "https://novita.ai/settings/key-management",
     "note": "开源模型聚合,性价比高。"},
    {"id": "groq", "name": "Groq", "group": "聚合网关", "endpoint": "https://api.groq.com/openai/v1",
     "key_env": "GROQ_KEY", "signup": "https://console.groq.com/keys",
     "note": "LPU 超低延迟,Llama/Qwen 等开源模型飞快。"},
    {"id": "cerebras", "name": "Cerebras", "group": "聚合网关", "endpoint": "https://api.cerebras.ai/v1",
     "key_env": "CEREBRAS_KEY", "signup": "https://cloud.cerebras.ai/",
     "note": "晶圆级芯片,极快推理。"},
    {"id": "sambanova", "name": "SambaNova", "group": "聚合网关", "endpoint": "https://api.sambanova.ai/v1",
     "key_env": "SAMBANOVA_KEY", "signup": "https://cloud.sambanova.ai/apis",
     "note": "高速开源模型推理。"},
    {"id": "nvidia", "name": "Nvidia NIM", "group": "聚合网关", "endpoint": "https://integrate.api.nvidia.com/v1",
     "key_env": "NVIDIA_KEY", "signup": "https://build.nvidia.com/",
     "note": "Nvidia 托管的开源模型。"},
    {"id": "github", "name": "GitHub Models", "group": "聚合网关", "endpoint": "https://models.inference.ai.azure.com",
     "key_env": "GITHUB_TOKEN", "signup": "https://github.com/marketplace/models",
     "note": "用 GitHub token 免费试多家模型(有额度限制)。"},

    # ——— 国际直连 ———
    {"id": "openai", "name": "OpenAI", "group": "国际直连", "endpoint": "https://api.openai.com/v1",
     "key_env": "OPENAI_KEY", "signup": "https://platform.openai.com/api-keys",
     "note": "GPT / Codex 系列直连。"},
    {"id": "anthropic", "name": "Anthropic", "group": "国际直连", "endpoint": "https://api.anthropic.com/v1",
     "key_env": "ANTHROPIC_KEY", "signup": "https://console.anthropic.com/settings/keys",
     "note": "Claude 系列直连(CEO 一般用 Claude Code,不必在此配)。"},
    {"id": "gemini", "name": "Google Gemini", "group": "国际直连", "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_env": "GEMINI_KEY", "signup": "https://aistudio.google.com/apikey",
     "note": "Gemini 系列,OpenAI 兼容端点。"},
    {"id": "xai", "name": "xAI (Grok)", "group": "国际直连", "endpoint": "https://api.x.ai/v1",
     "key_env": "XAI_KEY", "signup": "https://console.x.ai/",
     "note": "Grok 系列。"},
    {"id": "mistral", "name": "Mistral AI", "group": "国际直连", "endpoint": "https://api.mistral.ai/v1",
     "key_env": "MISTRAL_KEY", "signup": "https://console.mistral.ai/api-keys/",
     "note": "Mistral / Codestral 系列。"},
    {"id": "cohere", "name": "Cohere", "group": "国际直连", "endpoint": "https://api.cohere.ai/compatibility/v1",
     "key_env": "COHERE_KEY", "signup": "https://dashboard.cohere.com/api-keys",
     "note": "Command 系列,OpenAI 兼容端点。"},
    {"id": "perplexity", "name": "Perplexity", "group": "国际直连", "endpoint": "https://api.perplexity.ai",
     "key_env": "PERPLEXITY_KEY", "signup": "https://www.perplexity.ai/settings/api",
     "note": "Sonar 系列,自带联网搜索。"},

    # ——— 国内直连 ———
    {"id": "deepseek", "name": "DeepSeek 深度求索", "group": "国内直连", "endpoint": "https://api.deepseek.com/v1",
     "key_env": "DEEPSEEK_KEY", "signup": "https://platform.deepseek.com/api_keys",
     "note": "DeepSeek V4 直连,推理/数学/代码极便宜。"},
    {"id": "moonshot", "name": "月之暗面 Kimi", "group": "国内直连", "endpoint": "https://api.moonshot.cn/v1",
     "key_env": "MOONSHOT_KEY", "signup": "https://platform.moonshot.cn/console/api-keys",
     "note": "Kimi 长上下文 + 搜索,适合深度研究。"},
    {"id": "zhipu", "name": "智谱 GLM", "group": "国内直连", "endpoint": "https://open.bigmodel.cn/api/paas/v4",
     "key_env": "ZHIPU_KEY", "signup": "https://open.bigmodel.cn/usercenter/apikeys",
     "note": "GLM 中文模板/行政类性价比高。"},
    {"id": "dashscope", "name": "阿里百炼 Qwen", "group": "国内直连", "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "key_env": "DASHSCOPE_KEY", "signup": "https://bailian.console.aliyun.com/?apiKey=1",
     "note": "Qwen 中文创作 + 1M 上下文。"},
    {"id": "ark", "name": "火山方舟 Doubao 豆包", "group": "国内直连", "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
     "key_env": "ARK_KEY", "signup": "https://console.volcengine.com/ark",
     "note": "豆包多模态,唯一能做视频/图像理解、提抖音文案的供应商。"},
    {"id": "minimax", "name": "MiniMax 海螺", "group": "国内直连", "endpoint": "https://api.minimax.chat/v1",
     "key_env": "MINIMAX_KEY", "signup": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
     "note": "MiniMax 系列,长上下文 + 语音多模态。"},
    {"id": "baichuan", "name": "百川 Baichuan", "group": "国内直连", "endpoint": "https://api.baichuan-ai.com/v1",
     "key_env": "BAICHUAN_KEY", "signup": "https://platform.baichuan-ai.com/console/apikey",
     "note": "百川大模型。"},
    {"id": "lingyi", "name": "零一万物 Yi", "group": "国内直连", "endpoint": "https://api.lingyiwanwu.com/v1",
     "key_env": "LINGYI_KEY", "signup": "https://platform.lingyiwanwu.com/apikeys",
     "note": "Yi 系列。"},
    {"id": "stepfun", "name": "阶跃星辰 Step", "group": "国内直连", "endpoint": "https://api.stepfun.com/v1",
     "key_env": "STEPFUN_KEY", "signup": "https://platform.stepfun.com/interface-key",
     "note": "Step 系列,多模态强。"},
    {"id": "spark", "name": "讯飞星火 Spark", "group": "国内直连", "endpoint": "https://spark-api-open.xf-yun.com/v1",
     "key_env": "SPARK_KEY", "signup": "https://console.xfyun.cn/services/cbm",
     "note": "讯飞星火。"},
    {"id": "hunyuan", "name": "腾讯混元 Hunyuan", "group": "国内直连", "endpoint": "https://api.hunyuan.cloud.tencent.com/v1",
     "key_env": "HUNYUAN_KEY", "signup": "https://console.cloud.tencent.com/hunyuan/api-key",
     "note": "腾讯混元。"},
    {"id": "qianfan", "name": "百度千帆 文心", "group": "国内直连", "endpoint": "https://qianfan.baidubce.com/v2",
     "key_env": "QIANFAN_KEY", "signup": "https://console.bce.baidu.com/iam/#/iam/apikey/list",
     "note": "文心 ERNIE 系列(千帆 v2 OpenAI 兼容)。"},

    # ——— 本地 / 自托管 ———
    {"id": "ollama", "name": "Ollama (本地)", "group": "本地自托管", "endpoint": "http://localhost:11434/v1",
     "key_env": "OLLAMA_KEY", "signup": "https://ollama.com/",
     "note": "本地跑开源模型,key 随便填(如 ollama)。先 ollama serve。"},
    {"id": "lmstudio", "name": "LM Studio (本地)", "group": "本地自托管", "endpoint": "http://localhost:1234/v1",
     "key_env": "LMSTUDIO_KEY", "signup": "https://lmstudio.ai/",
     "note": "本地图形化跑模型,key 随便填。先在 LM Studio 开本地服务器。"},
]

# 每个 agent 的推荐模型 + 理由 + 能供给它的供应商(按优先级)。
# CEO(指挥舱)是 Claude Code 本体,不在此配,故不列。
RECOMMENDATIONS = {
    "coder": {"model": "codex-5.5", "reason": "代码能力顶尖、英文代码场景碾压;价格约 Opus 的 1/6,做编码主力最划算。",
              "providers": ["openai", "openrouter"]},
    "writer": {"model": "qwen3.7-max", "reason": "中文创作质量高,1M 上下文可一次吃下所有参考资料;做小红书/公众号/脚本首选。",
               "providers": ["dashscope", "openrouter"]},
    "researcher": {"model": "kimi-k2.6", "reason": "256K 上下文能吞下整个网页/论文,搜索能力强;深度研究/竞品分析最稳。",
                   "providers": ["moonshot", "openrouter"]},
    "analyst": {"model": "deepseek-v4-pro", "reason": "推理与数学最强、价格极低;数据分析/财务/策略推演性价比之王。",
                "providers": ["deepseek", "openrouter"]},
    "builder": {"model": "glm-5.1", "reason": "中文理解好、模板类任务便宜;合同/邮件/会议纪要够用且省钱。",
                "providers": ["zhipu", "openrouter"]},
    "runner": {"model": "deepseek-v4-flash", "reason": "几乎免费 + 1M 上下文;批量搬运/格式转换/日志处理用它不心疼。",
               "providers": ["deepseek", "openrouter"]},
    "perceiver": {"model": "doubao-seed-2.0", "reason": "多模态视觉理解;CC 再强也提不了抖音视频文案,这活只能它干。",
                  "providers": ["ark"]},
}


def _custom_path(root: str | Path) -> Path:
    return Path(root) / "config" / "providers.yaml"


def load_providers(root: str | Path) -> list[dict]:
    """内置供应商 + 用户自定义供应商(config/providers.yaml 的 custom: 段)。"""
    out = [dict(p) for p in BUILTIN_PROVIDERS]
    f = _custom_path(root)
    if f.exists():
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for c in doc.get("custom") or []:
            if isinstance(c, dict) and c.get("id") and c.get("endpoint") and c.get("key_env"):
                out.append({**c, "custom": True, "group": "自定义",
                            "note": c.get("note", "自定义供应商")})
    return out


def get_provider(root: str | Path, pid: str) -> dict | None:
    return next((p for p in load_providers(root) if p["id"] == pid), None)


def add_custom_provider(root: str | Path, name: str, endpoint: str,
                        key_env: str = "", note: str = "") -> dict:
    """新增自定义供应商,持久化到 config/providers.yaml。返回新供应商记录。"""
    import re
    pid = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_")) or "custom"
    if not re.match(r"^https?://", endpoint):
        raise ValueError("endpoint 必须是 http(s):// 开头的 OpenAI 兼容地址")
    key_env = (key_env or f"{pid.upper()}_KEY").strip()
    f = _custom_path(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    doc = (yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None) or {}
    customs = doc.get("custom") or []
    # 同 id 覆盖
    customs = [c for c in customs if c.get("id") != pid]
    rec = {"id": pid, "name": name.strip(), "endpoint": endpoint.strip(),
           "key_env": key_env, "note": note.strip() or "自定义供应商"}
    customs.append(rec)
    doc["custom"] = customs
    f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {**rec, "custom": True}
