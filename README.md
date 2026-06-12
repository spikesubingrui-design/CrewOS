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

## 安装(与 Hermes/OpenClaw 同款体验)

```bash
git clone <repo> && cd crewos     # 或直接进入本目录
bash install.sh                   # = pip install .
crewos onboard                    # 交互式向导:工作区 + API keys(可全部回车跳过)
crewos start                      # 启动 Mission Control,自动打开浏览器
```

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

## 越用越聪明(错题本)

- CC 审阅打回 → 打回原因自动写入该 agent 的 `memory/lessons.md`
- CC 复盘 → `add_lesson` 写"错误 → 修正后的做法"
- 错题本随每次派单注入该 agent 上下文,同样的坑不栽第二次

## 验收

```bash
python3 -m pytest tests/   # 13 项:派单台账/failover/宕机上报/DLP/熔断/回放/风险分级/错题本
```

## 安全(OpenClaw 138 CVE 教训)

只绑 127.0.0.1 / key 隔离 / 档案编辑白名单防越权 / 出站全过 DLP / 预算熔断 / 模型全挂不静默换模型(挂起上报,交接需授权)

## Phase 2 路线

飞书网关(VPS)、cron 调度、Memory Tree + Obsidian vault、竞品视频自动拆解流水线、评估集自动生长、CC 归因复盘。
