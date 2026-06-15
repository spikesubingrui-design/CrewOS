# CREWOS // MISSION CONTROL

一人公司的 AI 团队操作系统。CDR·FABLE-5 (Claude) 任指挥舱,7 名专属模型乘组在轨执行。

```
            ┌──────────────────────────┐
            │   CDR · FABLE-5 指挥舱    │  只决策,不执行
            └──────────────────────────┘
   CODER-1   WRITER-1   RECON-1   ANLYT-1
  codex-5.5  qwen3.7   kimi-k2.6  ds-v4-pro
   BUILD-1   CARGO-1   OPTIC-1
   glm-5.1  ds-flash  doubao-seed(视频理解)
```

## 环境要求

- **Python ≥ 3.10**(3.10 / 3.12 已在 CI 验证,3.14 亦可)
- pip ≥ 22,或 [pipx](https://pipx.pypa.io)(推荐:隔离安装,不污染全局)
- macOS / Linux(Windows 未测)
- 浏览器(看板)。可选:`gbrain`(接 U 第二大脑,见 [docs/MEMORY.md](docs/MEMORY.md))

## 安装(与 Hermes/OpenClaw 同款体验)

```bash
# 方式一:pipx(推荐)
pipx install git+<repo-url>          # 或本地:pipx install .

# 方式二:pip
git clone <repo> && cd CrewOS
pip install .                         # 或 bash install.sh

# 然后:
crewos onboard                        # 交互式向导:工作区 + API keys(可全部回车跳过)
crewos start                          # 启动 Mission Control,自动打开浏览器(默认 http://127.0.0.1:8466)
```

**第一次跑:** `crewos start` → 浏览器开 CONFIG → 在角色卡里给「总指挥 CEO」选一个供应商+粘 API key(推荐 DeepSeek/OpenRouter)→ 回 ORBIT,在底部 TRANSMIT 输入一个目标(如「做一个马里奥小游戏」)选「CDR·CEO 自动编排」→ 看团队规划→派单→交付,产出文件在任务流里点链接即开。
排错见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

- 所有数据住在 `~/.crewos`(agent 档案/记忆/台账/key),升级 crewos 不丢任何东西
- key 写入 `~/.crewos/.env`(权限 600),只活在环境变量,永不进 agent 上下文
- **没配 key 也能玩**:打开看板点 `RUN SIMULATION`,看一次完整任务的派单→产出→审阅打回→重做→交付→复盘

## Mission Control 界面(SpaceX 风格)

- **ORBIT** — 轨道视图:指挥舱居中,7 个乘组节点沿双轨道缓慢公转;派单时光脉冲射向节点,节点引擎点火(绿色脉冲环),交付时绿色脉冲返航,指挥舱审阅时琥珀色呼吸;背景实时星场+流星;左侧 CREW MANIFEST(状态灯/模型/单兵成本),右侧 TELEMETRY 遥测流,底部 TRANSMIT 手动上行
- **MISSIONS** — IN FLIGHT / REVIEW / HOLD / LANDED 四列;点任务进 FLIGHT RECORDER 逐帧回放(谁、何时、几轮、多少 token、多少钱)
- **CONFIG** — FUEL LIMITS(单任务预算熔断+月度警戒线)、SECURITY PROTOCOLS(DLP 黑名单)、UPLINK STATUS(每个通道 key 状态)、CREW DOSSIERS(网页直接编辑每个 agent 的 role.md / provider.yaml / 错题本 / 总指挥手册)
- 顶栏:UTC 时钟、MET 任务计时、FUEL USED 实时成本
- 告警横幅:DLP INTERCEPT / FUEL CUTOFF / ESCALATION / CHANNEL SWITCH

## 让 CC 上岗当总指挥

```bash
claude mcp add crewos -- crewos mcp
```

之后在任意目录启动 `claude`,对它说"帮我研究X"。CC 按 `~/.crewos/CrewOS.md` 手册派单、审阅(≤3轮)、复盘,看板同步直播。

## CLI

```bash
crewos status / cost / replay <task_id> / dispatch <agent> "<指令>"
crewos -w /其他/工作区 start        # 多团队隔离(像 mmclaw -w)
```

## 风险分级(全自动无边界,风险越大汇报越大)

任何外部动作执行前,CC 必须先调 `request_action` 申请放行:

| 等级 | 含义 | 处理 | 例 |
|------|------|------|----|
| L0 静默 | 纯读取 | 只入台账 | 搜索/读文件 |
| L1 日志 | 内部写 | 入台账,看板可查 | 存草稿 |
| L2 通知 | 外部只读 | 放行 + 看板横幅 | 拉外部数据 |
| L3 倒计时 | 外部写(可逆) | 看板倒计时卡片,N 秒无人反对自动放行 | 建 PR / 发飞书 |
| L4 审批 | 不可逆 | 阻塞等你明确批准(看板卡片 / `crewos approve`) | 发布内容 / 花钱 |

- 每个 agent 的动作风险表在 `agents/<name>/actions.yaml`(看板 CONFIG 可直接编辑)
- 未登记的动作一律按 L3(60 秒倒计时)处理
- 审批卡片在看板右下角:GO / NO-GO 一键裁决;CLI 用 `crewos approvals / approve <id> / deny <id>`

## 越用越聪明(错题本 + Memory Tree)

- CC 审阅打回 → 打回原因自动写入该 agent 的 `memory/lessons.md`
- CC 复盘 → `add_lesson` 写"错误 → 修正后的做法";项目经验 `memory_write` 进 Memory Tree
- 错题本随每次派单注入该 agent 上下文;Memory Tree 是 Obsidian 兼容 vault(直接打开 `~/.crewos/memory`),接新任务先 `memory_search` —— 第二个项目吸取第一个项目的教训

## 通知与定时(Phase 2)

- **飞书推送**:看板 CONFIG 贴上群机器人 webhook,交付/上报/熔断/审批请求实时推送(L0/L1 静默事件绝不外推);通用 webhook 同理
- **cron 定时任务**:编辑 `config/crontab.yaml`(看板可改),标准 5 段 cron,改完即生效;每次触发就是一次派单,全程入台账受熔断保护;`crewos jobs` 查看
- **视频理解**:`dispatch(agent="perceiver", media_url="<视频/图片直链>")` 走多模态消息(火山方舟 doubao 格式)
- **评估集自动生长**:每个任务自动成为评估样本;看板 CREW PERFORMANCE / `crewos evals` / MCP `eval_report` 查各角色×模型的一次过率、平均轮次、上报数 —— 换模型前先看它

## v0.5 引擎加固

- **自动检查层**:派单附 `checks_json` 验收规格(字数/必含/违禁词/正则/链接数/JSON),产出先过机器检查,CEO 只看标红项 —— 审阅 token 再砍一半
- **并行派单**:`dispatch_async` + `wait_task`,多个长任务真并行
- **双向 DLP**:含敏感信息的指令直接拒发第三方模型(此前只扫产出)
- **心跳后台化**:看板每 30s 后台探测并缓存,名册不再每次真发请求烧钱;通道掉线/恢复写台账
- **错题本检索化**:按当前任务相关度选 top-3 注入,错题再多也不稀释上下文
- **提额续跑**:熔断任务在看板一键提额(budget_override 事件,台账可审计);任务卡可直接取消
- **月度警戒**:成本越线自动上报 + 推送(每月一次)
- **访问令牌**:CONFIG 设 Access Token 后,局域网/隧道访问需带 token(本机 127.0.0.1 默认仍免配)
- **配置校验**:看板保存 provider/actions/crontab.yaml 时先校验,写坏直接拒绝

## v0.6 容错、自治与生态(借鉴 paperclip 70k★ 工程严谨度)

- **Silent Watchdog**:后台监控"派单后久无产出"的任务(便宜模型易跑飞),分级 suspicious/critical 自动上报;`crewos doctor` 用失败分类学诊断停工原因 + 下一步建议
- **预算硬刹车**:单任务 80% 软告警 + 撞线熔断;月度硬上限自动暂停全员;`crewos pause/resume <agent>` 或看板手动暂停,暂停态拒绝派单
- **post-run-eval**:CEO 手册升级——成功任务也强制复盘,按 learning/pattern/decision/preference 四类沉淀经验(不再只在打回时学)
- **离线行为评估**:`crewos eval-behavior` 派真单前就测每个便宜模型的合规率(JSON 严格/不照搬注入/来源齐全…),换模型决策有据

## 验收

```bash
python3 -m pytest tests/   # 派单台账/failover/宕机上报/DLP双向/熔断与提额/回放/风险分级/错题本/cron/通知(飞书+企微+Discord)/记忆/多模态/评估/检查层/心跳/校验/watchdog/暂停门禁/doctor/行为评估/CEO调研
```

## 文档(docs/)

- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 常见错误 + `crewos doctor` 排错与监控
- [CHECKS.md](docs/CHECKS.md) — 自动检查层(6 种机器验收)怎么用
- [MEMORY.md](docs/MEMORY.md) — 三层记忆:错题本 / Memory Tree / U 第二大脑
- [HARNESS-UPGRADES.md](docs/HARNESS-UPGRADES.md) — 旗舰模型 harness 逐条拆解的 35 项升级图
- [PAPERCLIP-UPGRADES.md](docs/PAPERCLIP-UPGRADES.md) — paperclip 70k★ 生态对标的 31 项升级 + 差异化定位

## 宣传页

`docs/index.html`(GitHub Pages)— 含循环播放的工作流模拟动画;正式看板无模拟入口,保持纯净。

## 安全(OpenClaw 138 CVE 教训)

只绑 127.0.0.1 / key 隔离 / 档案编辑白名单防越权 / 出站全过 DLP / 预算熔断 / 模型全挂不静默换模型(挂起上报,交接需授权)
