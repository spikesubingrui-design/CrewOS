# paperclip 生态 -> CrewOS 升级图与差异化定位

> 对 `paperclipai/paperclip`(70227 star / 13037 fork,开源 AI agent 公司管理平台,TS/MIT)及其生态
> (clipmart 市场 / companies 模板 / paperclip-surfers 自改进分支 / plugin-acp / hermes 适配器 / companies-tool)
> 的 7 路并行调研,每条借鉴逐条对照 CrewOS v0.5.0 源码核实。共 31 条值得落地,15 条 CrewOS 已有等价能力。

## 战略判断:继续做,但差异化

paperclip 的护城河是「企业级编排基础设施的工程严谨度 + org chart 隐喻的传播力」(70k star 的根因)。CrewOS 不去抢「管理 AI 员工」这个已被占住的心智,而是守住自己的差异化:强 CEO 智能派单 + 成本套利(贵模型决策、便宜模型执行)+ 连续风险分级 + 学习闭环 + 多模态。

最该学的 3 件事:adapter contract / execution hygiene / distribution as code(适配器契约、执行卫生与容错、分发即代码)

最该坚持不学的 2 件事:no multi tenant platform / no expensive model per role(不做多租户平台、不走每角色配贵模型)

生态联动:CrewOS ships a paperclip adapter and Hermes is a shared cheap executor —— 即 CrewOS 出一个 Paperclip 适配器,让一整支 CrewOS 乘组被「招聘」进 Paperclip 公司当一个部门;Hermes 作为共享的廉价执行体。三者不互斥,叠成一条链。

## CrewOS 已有等价能力(无需重做)

- 把 CrewOS 编排引擎暴露成 MCP server(薄包装现有 API),让外部 agent/IDE/Claude Code 直接 create_task/checkout/comment/approve/查台账
- Make comments the only communication channel; all context hangs on the task (Tasks are the communication channel). Unify CEO↔executor round-trips in the review loop onto the task object rather than scattered, for replay and audit.
- Refactor the 7 cheap-model backends into a unified Executor/Adapter interface (execute→{exit,usage,session}, test_environment, session_codec), so adding/replacing a model = implement an adapter class + register, not if model==X branches in the dispatcher. Borrow paperclip's ServerAdapterModule: especially testEnvironment self-check (validate each model's CLI/API key/quota at boot/before dispatch) and unified UsageSummary feeding the budget engine.
- 把 CEO 对 agent 的驱动收敛成一组语义化工具(acp_spawn/send/status/cancel/close/result 类比),让 CC CEO 通过稳定工具契约调度执行层。
- 用事件总线 + 命名空间事件解耦各子系统,约定 at-least-once + 幂等,把'审阅驳回→写错题本→推通知→看板回放'变成订阅链。
- Secret-scrubbing export + 幂等 replay install(对标 paperclip companies.sh):export 时剥离 secret,import 时重建 model-map/cron/MCP/board。
- Visualize org chart before install: pre-import render blueprint as roles + dispatch chain + governance + projects, reusing the SpaceX-style board render layer.
- Marketplace-ships-packages, install-runs-against-local-engine-API boundary: server never holds runtime/secrets; import pushes scrubbed blueprint to user's own CrewOS API. Preserves self-hosted/data-private edge.
- 秘密需求声明与角色灵魂解耦:极薄 manifest 只声明每个 agent 需要哪些 env secret(required/optional),角色指令放 markdown,导入时据此提示补齐缺失 secret。
- assignment 不唤醒、comment 才唤醒的显式语义:分配任务与唤醒 agent 干活拆成两动作,先布依赖图再统一唤醒。
- anti-patterns 角色专属、随故障增长的结构化错题本:每角色维护自己的表,审阅拒绝/生产故障即追加,下次同类任务强制注入。
- 评估集补 KPI 定义:per-agent/per-model 设 target_value+direction+绑定指标键,算滚动均值/趋势/异常告警,而非只出胜任度快照
- 做 `crewos onboard --yes` 真正零交互一键起步,并把 onboard/检查/启动折叠进一个 `crewos start`(无配置则自动 init→自检自修→启服务→开看板),对标 paperclip 的 run 统一 bootstrap;再补 pipx/uvx 式一行分发零 clone 试用。
- adapter 三件套契约(server.execute(context) + UI transcript + CLI 输出)作为接入新模型/运行时的统一接口,把'加一个便宜模型'降到'写一个 adapter'
- 审批门收敛到'高杠杆动作'(只对 Hire Agent / CEO Strategy 强制审批)+ 预算硬停作为第二条风险线;补 overspend 自动暂停


## 高影响(优先落地)

### 1. Silent Watchdog:独立监控'仍在跑但长时间无输出'的执行,分级 ok/suspicious/critical 自动建恢复工单,operator 可 snooze/continue/dismiss

**来源**:paperclip/core   **落点**:crewos/web_server.py(新增 _watchdog_loop,与 _heartbeat_loop line 149 / _monthly_watch_loop line 161 并列)+ crewos/checks.py 或新模块

**写法**:现有后台只有通道 ping(_heartbeat_loop)和月度成本(_monthly_watch_loop),没有任何对'已派单但迟迟无 task_result'的监控;dispatch 是同步 urlopen(router.py line 144,timeout=120),dispatch_async 的卡死线程无人回收。落地写法:加 _watchdog_loop,每 60s 扫 ledger——对有 task_assign 但 N 分钟内无对应 task_result 的 task_id,按时长分 suspicious/critical 写一条 escalation 事件(已是看板可见的上报类型),并在 settings.yaml 加 watchdog_suspicious_minutes/critical_minutes 阈值。snooze/continue/dismiss 可复用现有 approvals 表或新建 watchdog 事件。

**核实备注**:高度契合:CrewOS 明确派单给 7 个便宜模型,便宜模型跑飞/静默概率高,且 router 只有单次 urlopen timeout 而无'整任务级'停滞检测。这是当前架构最实打实缺的一块,纯增量、零侵入现有逻辑。

### 2. Build a lightweight behavioral eval set with promptfoo: deterministic assertions for take-work / report-progress / report-blocker / no-work-exit / checkout / 409 conflict / approval compliance / cross-company boundary, compared across models via OpenRouter. Run 'each cheap model's compliance rate per task class'.

**来源**:paperclip/core   **落点**:new evals/ dir (promptfoo config) + crewos/checks.py (reuse assertion primitives) + crewos/ledger.py eval_report (telemetry side)

**写法**:Add an evals/promptfooconfig.yaml with a handful of fixed prompts and deterministic asserts (e.g. must-call request_action before publish, must escalate on AllChannelsDown, must not leak across DLP) and run it across the 7 cheap models via OpenRouter as a CI gate / model-selection aid; reuse checks.py's _run_one assertion style for the contains/regex/json checks.

**核实备注**:Confirmed eval_report (ledger.py:127-164) is a production-telemetry competency report (first-pass rate/rounds/cost), NOT a deterministic behavioral test suite — no promptfoo, no offline assertion set over agent behaviors (tests/ are engine unit tests, none cover take-work/409/approval-compliance of actual model output). Genuinely additive and high-value: lets the operator pick which cheap model is competent at each role before burning real tasks. Fits perfectly (local, model-comparison is core to CrewOS's whole premise).

### 3. 预算从月度成本告警升级成调度门禁加自动暂停状态机:每 agent/每公司月度上限,心跳入口先查预算余额、耗尽直接 skip,撞线把 agent 置 paused 阻断后续所有心跳直到人工解除。把花钱做成硬刹车而非事后通知。可与 CrewOS 已有预算提额审批联动:paused 后走提额审批才能恢复。

**来源**:paperclip/templates   **落点**:crewos/router.py(dispatch 入口加月度门禁)+ crewos/web_server.py(_monthly_watch_loop 现仅告警,需升为刹车)+ crewos/cron.py(_fire 前查暂停态)+ crewos/ledger.py(新增 agent_paused 状态事件)

**写法**:在 Router.dispatch 的预算检查段(router.py:254-262 已有 per-task 熔断)旁加 per-agent/全公司月度上限:dispatch 与 cron._fire 入口先 ledger.cost_report(since=month_start) 比对 settings.monthly_cap,撞线写一个 budget_block + 持久 paused 标记(类似 budget_override 事件,append-only),后续 dispatch/cron 命中 paused 直接 skip,解除走现有 /api/task budget_override 同款提额事件。

**核实备注**:现状精确匹配建议前提:预算硬刹车只有 per-task(router.py:258 spent>=cap 才 raise BudgetExceeded),月度层面 _monthly_watch_loop(web_server.py:161)只是'每月 log 一次 escalation 通知',不阻断任何派单——确实是'事后通知'而非门禁。没有任何 agent paused 状态,没有 per-agent 月度上限。这条对一人公司是核心安全网(无人盯屏时 cron+async 可能持续烧钱),完全契合本地无人值守定位,且能复用现成的 append-only 事件 + budget_override 提额联动,effort 中等。impact high。唯一要注意:cost_report 现按 from_agent 聚合,可直接支撑 per-agent 月度统计。

### 4. CEO 质量门强制实测验证:审阅环节 CEO/审阅层必须真执行一次验证动作(WebFetch/跑测试/打开产物)才能放行,把验证证据做成审阅通过的必填项。

**来源**:paperclip/templates   **落点**:crewos/templates/CrewOS.md(审阅流程第 3 步)+ crewos/checks.py(扩 command 类型)+ crewos/mcp_server.py(log_event 校验)

**写法**:两层落地:(1) checks.py 增 {"type":"command","run":"pytest -q","expect_exit":0} 类型,在沙箱跑命令把 exit code/输出当硬证据(coder 产物可真跑测试);(2) mcp_server.py 的 log_event 在 type='task_done' 时要求 payload 带 verified_by(checks 报告 id 或 CEO 手动执行记录),无证据则拒绝结案并回 {error:'need_verification'};(3) CrewOS.md 审阅第 3 步改成『放行前必须有一条机器验证证据或你亲自执行 WebFetch/打开产物的记录,只读 SUMMARY 不算』。

**核实备注**:已核实是真缺口:checks.py 顶部注释明确写『检查不通过不会拦截交付……打回与否仍由 CEO 判断』,且只有 word_count/must_include/min_links 等静态检查,无执行类(无跑命令/抓 URL)。CrewOS.md 审阅第 3 步现为读式判断(『代码可否运行』靠 CEO 主观看)。强契合一人公司定位——CC 当 CEO 最大风险就是信 agent 自报『我做完了』,本地优先也能跑 pytest/打开产物。high。

### 5. onboarding 魔法复刻:一段 CrewOS 专属装配 prompt,CC 做 intake 问答→生成 workflow YAML→实例化角色→写风险分级阈值→自检交付。

**来源**:paperclip/templates   **落点**:crewos/templates/(新增 onboarding skill/prompt,如 templates/skills/new-company.md)+ crewos/cli.py(crewos onboard 末尾引导)

**写法**:写一份 templates/skills/new-company.md:5 问 intake(做什么业务/要哪些角色/对外动作有哪些/各动作可逆性与花钱否/月预算),CC 据答案用现成 MCP/文件 API 落地——调 /api/file PUT 写各 agent 的 role.md+actions.yaml(按可逆性/花钱定 L0-L4)+provider.yaml,改 config/settings.yaml 预算,最后 list_agents() 自检通道。cli.py 的 cmd_onboard 成功后加一行提示『下一步对 CC 说:用 new-company skill 开一家公司』。

**核实备注**:已核实底层引擎齐备:派单(dispatch)、风险分级(request_action+actions.yaml)、配置写入(/api/file PUT+validate_config)、名册(list_agents)全有,但 cmd_onboard(cli.py:34)只 shutil.copy 静态模板+录 key,无 intake→定制 YAML 的装配层。这正是把已有 API 串成『一句话开公司』体验的缺失环节,与 Hermes 式 onboard 定位一致。high,但需新写 skill 文档+少量引导,effort medium。

### 6. 错题本从'仅审阅拒绝时写回'升级成'每个 run 后无条件 post-run-eval':run 完自评,抽 pattern/decision/preference/learning 四类,成功任务也产正向经验

**来源**:paperclip/persistent   **落点**:router.py dispatch() 末尾新增 post-run 钩子 + mcp_server.py + memory.py

**写法**:dispatch 返回前(router.py:316 task_result 之后)发一个可选的 CEO/执行模型自评事件,把抽出的 4 类条目分别写入 agent 错题本(memory.py append_lesson)与 Memory Tree(memvault.vault_write),由 CrewOS.md 手册强制 CEO 每个任务结案前执行而非仅打回时。

**核实备注**:已核实现状:错题本只在 review_feedback 打回(mcp_server.py:137)或显式 add_lesson(mcp_server.py:167)时写,成功任务零沉淀;CrewOS.md:29 的复盘是'强制但靠手册自觉、且偏项目级'。这条真补空白且契合 CEO 复盘定位。落地注意:CrewOS 是本地一人公司,自评应由 CEO Claude(免额外 API)做,别让便宜执行模型再多跑一轮烧钱。

### 7. 把『诊断为什么停工』做成一等命令 crewos doctor --why-stopped,用失败分类学:死的 in_review、in_progress 无下一步、被不可达依赖阻塞、recovery 递归、silent pseudo-stops(等审核没被路由),并落成不变量自检。

**来源**:paperclip/tooling   **落点**:crewos/checks.py 或新模块、crewos/ledger.py(任务状态已从事件派生)、crewos/web_server.py(api_tasks 的 status 派生 + 看板卡住高亮)、crewos/cli.py(新子命令)

**写法**:新增 ledger.stalled_tasks():扫 api_tasks 已有的 per-task 状态派生逻辑(web_server.py:223-238 把事件类型映射成 done/blocked/review/working/pending),对每个非终态任务算『最后事件距今时长』与『最后事件类型』,判定:status=review 且 updated 超 N 分钟无 review_feedback → 死在审阅;status=working 且末事件是 task_result 但无下一 task_assign → 无下一步;有 escalation 未被 status_update 跟进 → silent pseudo-stop。CLI 加 cmd_doctor 输出这张表,web 看板给这些 task 卡加红边。

**核实备注**:fits 极好且是真缺口:CrewOS 的任务状态已经完全从 append-only 事件流派生(web_server.py:208-239 已有 review/working/blocked/pending 的成熟映射),这正是停工诊断需要的全部原料,但目前没有任何代码去识别『卡在 review 没人推进』『有产出但没下一步』这类伪停工——CEO 是 CC,CC 一旦漏掉一个等审阅的任务就静默卡死,没有兜底扫描。这是 CrewOS 当前最薄弱、收益最直接的一环,故定 high。


## 中影响

### 1. 双锁任务签出(checkoutRunId 所有权锁 + executionRunId 活跃执行锁分离)+ 状态机非终态写成可校验不变量

**来源**:paperclip/core   **落点**:crewos/ledger.py(台账加签出/锁字段)+ crewos/mcp_server.py(dispatch_async/wait_task,_async_tasks 字典在 line 40)

**写法**:CrewOS 当前完全没有任务锁:web_server.py 的 api_tasks(line 208-239)在读时从事件类型 derive 出 working/review/done,dispatch_async(mcp_server.py line 89)的 _async_tasks 只是进程内 handle→thread 字典,两个并发 dispatch 传同一 task_id 不会互斥。落地写法:在 ledger 加 task_lock 表(task_id PRIMARY KEY, owner_agent, checkout_run_id, execution_run_id, state),dispatch 前 INSERT OR IGNORE 抢 checkout 锁、开始调用 LLM 时 CAS 设 execution_run_id、task_result 时清空,并写一个 assert_invariants(task_id) 校验'in_progress 必有 execution_run_id、done 必无锁'。

**核实备注**:双锁全套对一人公司偏重,paperclip 的便宜模型抢单场景 CrewOS 确实存在(7 个便宜模型),但 CrewOS 是 CC 单点串行派单为主,真正的并发只在 dispatch_async。建议先做轻量版:给 dispatch_async 加一把 task_id 级所有权锁(防重复执行),不必拆完整双锁/全状态机不变量——后者 effort 偏 high 且 ROI 不匹配当前规模。

### 2. 崩溃恢复双 pass:对'已分配但 run 失败的 todo'排 assignment recovery wake,对'丢失活跃路径的 in_progress'排 continuation wake,保留原 owner,二次失败开显式 recovery action 而非无限重试

**来源**:paperclip/core   **落点**:crewos/web_server.py @app.on_event('startup')(line 466)+ crewos/mcp_server.py(_async_tasks 易失,line 40/97-110)

**写法**:崩溃恢复目前完全空白:dispatch_async 的 _async_tasks 是纯内存字典,web/MCP 进程重启后所有在途异步任务静默消失,wait_task 只能回 unknown_handle(mcp_server.py line 118),没有任何 startup 阶段的扫描补偿。落地写法:在 web startup(line 466)加一次 recovery pass——查 ledger 里 status='working'(有 task_assign 无 task_result/task_done/task_failed)的 task_id,为每个写一条 escalation('崩溃后发现 N 个在途任务,需 CEO 决定续派/弃单'),由 CC 决策而非自动重试;给 ledger 加 recovery_attempts 计数,二次失败转 task_failed。

**核实备注**:契合本地优先定位(本地进程会被关机/重启)。但 CrewOS 没有 paperclip 那种持久 run 引擎,'continuation wake 续作'语义对 CrewOS 偏重——CrewOS 的任务是一次性派单,续作其实就是 CC 重派。建议落成'重启自检 + 列出孤儿任务交给 CC',不必照搬 todo/in_progress 两条独立 pass。

### 3. PARA 记忆组织(projects/areas/resources/archives)+ summary.md 先加载/items.yaml 按需加载分层 + 事实永不删除只标 superseded + 语义检索取代 grep

**来源**:paperclip/core   **落点**:crewos/memory.py(vault_search line 91 是 grep / vault_write line 74 / select_lessons line 46)+ crewos/templates/memory/tree.md(line 8 只有 projects+knowledge)

**写法**:现状:Memory Tree 只有 projects/ + knowledge/ 两层(tree.md line 9-10),vault_search(memory.py line 91)是纯关键词 AND 命中的 grep(line 101),没有 summary 优先加载、没有 supersede。可吸收的两条最值:(1)给每个 projects/ 项目加一行 status: active|superseded 与 superseded_by 字段约定(写进 tree.md 约定 + vault_write 不覆盖只追加),(2)在 knowledge/ 顶层加 _index.md 或 summary.md 作为'先加载'摘要。注意 select_lessons(line 46-58)的 bigram 相似度检索已经是比 grep 更优的'语义化'雏形,可把同样思路移植到 vault_search 给 grep 兜底排序。

**核实备注**:选择性吸收。'supersede 而非删除'和'summary 先加载'这两条契合且低成本,值得做。但完整 PARA 四象限(areas/resources/archives)对一人公司过度工程——CrewOS 的 projects/knowledge 二分已够;items.yaml 原子事实分层属于 paperclip 大规模记忆才需要的粒度,CrewOS 不必学。'语义检索取代 grep'真上向量库违背'零外部依赖/本地优先',不建议,bigram 排序兜底即可。

### 4. Goal ancestry / context lineage: every task carries the full company→initiative→project→milestone→issue ancestor chain so cheap models always know 'why'. Add an initiative/goal root to the ledger and hang loose tasks onto a company goal tree.

**来源**:paperclip/core   **落点**:crewos/ledger.py (events table + new_task), crewos/web_server.py (kanban)

**写法**:Add nullable parent_task_id + a lightweight 'goals' notion: extend events schema with parent_task_id TEXT DEFAULT '', let new_task(title, parent=...) record it, and in router.dispatch prepend an ancestor-chain breadcrumb (walk parent links) into the system prompt so the cheap model sees company→project→task context.

**核实备注**:Verified genuinely absent: events table (ledger.py:15-33) has only task_id, no parent/initiative/goal/project_id; new_task(title) takes no parent. Low effort, real alignment win for cheap models. Keep it minimal — a parent_task_id column + a goals/ markdown root in Memory Tree, not a full PM hierarchy (would over-engineer a one-person company).

### 5. Three-tier budget (visibility / soft 80% warn / hard cap auto-pause) across agent/task/project/company dimensions, token+dollar dual metering, billing-code cost attribution. CrewOS already has monthly cost warn + budget override; add per-project/per-agent attribution + soft/hard thresholds.

**来源**:paperclip/core   **落点**:crewos/router.py (dispatch budget gate), crewos/web_server.py (_monthly_watch_loop), crewos/ledger.py (cost_report)

**写法**:In router.dispatch, between the hard cap and execution, add a soft threshold: when spent >= soft_ratio*cap (e.g. 0.8) log a budget_warn event (new EVENT_TYPE) once per task instead of only blocking at 100%; and extend ledger.cost_report to also group by a billing_code/project tag carried on each task.

**核实备注**:Partial overlap confirmed: hard per-task cap exists (router.py:254-262 BudgetExceeded), single company-wide monthly warn line exists (web_server.py:161-180), cost_report already does by_agent + by_model (ledger.py:107-125). Missing: soft 80% tier, per-project/billing-code dimension, dollar+token dual view. Small effort since aggregation infra is already there. Skip full 4-dimension matrix — per-task soft tier + a project tag covers 90% of value for a solo operator.

### 6. Introduce capability-gated lightweight plugin/extension mechanism: even without full Host-Worker process isolation, let each extension (notify gateway, DLP, Perceiver, new tool) declare via manifest the capabilities it needs (read_tasks/write_lessons/http_outbound/register_tool…), and the engine gates by declaration at load time, explicitly forbidding over-reach. Turn 'what an extension can touch' from implicit trust into an enumerable, auditable contract — reinforcing L0-L4 + inbound DLP.

**来源**:paperclip/plugins   **落点**:crewos/risk.py (actions.yaml gating precedent) + crewos/mcp_server.py (tool registration) + new extension manifest

**写法**:Extend the existing actions.yaml capability-declaration pattern to extensions: require each non-core tool/gateway to ship a manifest listing capabilities (e.g. http_outbound, write_lessons, read_ledger), and have _bootstrap refuse to load any that declares a capability not in an allowlist in settings.yaml — making notify/DLP/Perceiver reach explicit and auditable.

**核实备注**:Confirmed not present as a generic mechanism, but a strong precedent exists: actions.yaml already declares per-action risk capability gating (risk.py:53-66, validated in web_server.validate_config) and file access is already allowlisted (EDITABLE regex web_server.py:40-43, _vault_path memory.py:63-71). Today extensions (notify.py, dlp.py, the 14 MCP tools) are hardcoded and implicitly trusted. Fits the local-first/auditable ethos well. Downgraded impact to medium (not high): for a one-person, single-trusted-operator system the threat model is thin — the win is auditability/discipline, not defense against a malicious plugin author. Worth doing as it extends an existing pattern, but not urgent.

### 7. Borrow ACP subprocess lifecycle management: add a reaper loop for long-running/parallel agents — auto-reap zombie processes / stuck tasks by idle timeout + max lifetime, SIGINT for soft turn-cancel, SIGTERM for graceful shutdown, and a per-queue/per-task concurrency cap (maxSessionsPerThread). You already have dispatch_async/wait_task + backgrounded heartbeat; auto-reaping would cure cheap-model hang/leak.

**来源**:paperclip/plugins   **落点**:crewos/mcp_server.py (dispatch_async / wait_task / _async_tasks)

**写法**:Add a concurrency cap + handle TTL to dispatch_async: refuse (or queue) new async dispatches when len of live _async_tasks exceeds maxN, stamp each slot with a start time, and on each wait_task/list_agents call sweep _async_tasks dropping handles whose thread finished or whose age exceeds a max-lifetime, logging an escalation for ones that blew past the deadline.

**核实备注**:Confirmed the gap is real: dispatch_async (mcp_server.py:89-110) spawns daemon threads with NO concurrency cap, and orphaned handles only get popped on a *successful* wait_task (line 124) — fire-many-then-never-wait leaks slots. grep confirms zero reaper/semaphore/SIGTERM. BUT the SIGINT/SIGTERM/zombie-process framing is a mismatch: these are in-process Python *threads* over HTTP calls (router._call_openai_compatible has a 120s urllib timeout, so they can't truly hang forever), not OS subprocesses — you can't SIGTERM a thread. The applicable subset is concurrency cap + handle TTL sweep, which is genuinely useful and small effort. Downgraded from 'high' because the per-call HTTP timeout already bounds the worst case; this is leak-hygiene, not a hang cure.

### 8. 借 company-wizard Preset/Module 思路做'团队模板':把 CEO+N 个便宜模型+风险阈值+审阅强度+检查层开关打包成 fast/secure/research 等 preset,一键切换;Module 把 DLP/Perceiver/Memory Tree 做成可组合开关。

**来源**:paperclip/plugins   **落点**:crewos/templates/config/(新增 presets.yaml)+ crewos/cli.py(新增 crewos preset <name>)+ 宣传页 docs/index.html 模拟演示

**写法**:新增 crewos/templates/config/presets.yaml,每个 preset 声明 {default_task_budget_usd, monthly_warn_usd, max_review_rounds, unknown_action_risk, dlp_blocklist 强度, 启用哪些 agent}。加 cli 子命令 crewos preset secure,把对应值合并写进 config/settings.yaml(目前 settings 只有 5 个键:budget/warn/feishu/webhook/token,需扩字段),并在 risk.py 把硬编码的 UNKNOWN_ACTION_RISK=3 改成可被 preset 覆盖。模块开关里 DLP 已天然可空(dlp_blocklist=[] 即软化),Memory Tree 已是独立 vault,只需把开关收进 preset。

**核实备注**:确实没有等价物(grep 无 preset/module),且对'一人公司一键切场景'很对症。effort 中:要先把散落的阈值(router 默认预算、risk UNKNOWN_ACTION_RISK、CrewOS.md 写死的 3 轮)收口到 settings 才能被 preset 统一驱动。

### 9. 借 testEnvironment/plugin doctor 做开机自检与 doctor 命令:检测各模型 CLI 可用性、key/配额、MCP 连通、Memory Tree vault 路径、cron 注册状态,把环境故障从运行时前移到启动期。

**来源**:paperclip/plugins   **落点**:crewos/cli.py(新增 cmd_doctor)+ 复用 router.heartbeat / workspace.load_env / cron.load_jobs

**写法**:新增 crewos doctor 子命令,逐项检查并打勾/打叉:① 工作区结构完整(ensure_workspace 的 created 应为空)② 每个 KEY_SPECS 的 key 是否在 env(load_env 后查 os.environ)③ 每个 agent provider.yaml 能否 load_agent 解析 + channel 至少一个 key_set ④ 各通道 heartbeat(name) 实探(已有,复用)⑤ vault 路径 root/memory 存在可写 ⑥ crontab.yaml 每条 due() 可解析(validate_config 已有逻辑可抽出复用)⑦ ledger.db 可写。把现在散在 web_server 后台线程/启动期才暴露的失败前移到一条命令。

**核实备注**:无等价物。effort small:绝大多数检查器已存在(heartbeat 实探、validate_config 的 cron/provider 校验、load_agent),doctor 只是把它们串成一个不发请求烧钱前就能跑的体检入口。对'没配 key 也能玩'的定位是很自然的补强。

### 10. Two-layer config:Listing(storefront + supported_models/required_mcp/min_engine_version)+ Blueprint payload(role-map/review-loop/L0-L4/check-layer/cron/MCP/Memory Tree)= 一个声明式 export/import 包,作为 Workflow Marketplace 骨架。

**来源**:paperclip/marketplace   **落点**:crewos/(新增 blueprint.py:打包/校验)+ crewos/cli.py(crewos export/import)+ templates 现有结构即 blueprint 源

**写法**:CrewOS 的工作区已经是一个隐式 blueprint:agents/*/{role.md,provider.yaml,actions.yaml,memory/} + config/{crontab.yaml,dlp_blocklist.txt,settings.yaml} + CrewOS.md。落地:写 blueprint.py 把这些打成一个 manifest.yaml(声明 version=0.5.0 即 min_engine_version、各 agent 的 model 即 supported_models、用到的 key_env 即 required keys)+ 文件树,export 成 .crewos.zip;import 时校验 engine 版本与 provider.yaml schema(validate_config 已有)再铺进新工作区。Listing 层先做成 manifest 顶部的 name/description/tags 字段即可,Marketplace UI 是后话。

**核实备注**:无等价物,但价值真实:CrewOS 把'团队配置'做成了纯文件,export/import 几乎是免费的副产品,且与建议3的 preset 同源(preset 是内置 blueprint 的子集)。原建议标 effort large 偏高——因为不必先建 marketplace,先做 export/import 的 CLI 就有价值。判 medium 而非 high:对一人公司,分享/复用模板是 nice-to-have 而非刚需。

### 11. Compatibility-precheck field on listings (mirror compatibleAdapters/requiredModels): declare model tiers/MCP/min engine version/Memory Tree/Perceiver deps; auto-check pre-import, report gaps.

**来源**:paperclip/marketplace   **落点**:crewos/workspace.py(ensure_workspace 导入流程)+ crewos/web_server.py(validate_config)

**写法**:在 provider.yaml 旁加可选 requires: 段(min_crewos_version / needs_keys:[ARK_KEY] / mcp_tools:[memory_search]),ensure_workspace 或一个新 crewos doctor 子命令逐 agent 校验缺失 key/版本并报告——perceiver 现在硬依赖 ARK_KEY 但无人提示缺失。

**核实备注**:真实痛点:perceiver/provider.yaml 只声明 key_env: ARK_KEY,缺 key 时只在运行期失败(router heartbeat catch Exception 静默置 offline),没有导入期/onboard 期的缺失提示。validate_config(web_server.py:399)已校验 yaml 结构但不校验依赖可用性。这条很契合本地优先(纯本地静态校验,不需服务器),effort 小。不必照搬 marketplace 的 listing,落成 'crewos doctor' 体检即可。

### 12. 把 cron 调度、并行派单、心跳后台化三者收敛成单一 heartbeat 原语:一个 agent 的唤醒来源(cron 间隔/外部 webhook/新任务 comment/手动)和它的执行 cycle 用同一套 heartbeat_run 记录表达,降低概念面积,也让'为什么这个 agent 现在醒了'可审计。

**来源**:paperclip/templates   **落点**:crewos/cron.py(CronScheduler._fire)+ crewos/mcp_server.py(dispatch_async/_do_dispatch)+ crewos/router.py(heartbeat 通道探测)+ crewos/ledger.py(新增 wake-source 维度)

**写法**:在 ledger 给 task_assign 事件加 payload.wake_source 字段(cron/manual/async/webhook),并让 cron._fire、dispatch_async、web /api/dispatch 三处统一带上来源标记——不必新建调度内核,只把'谁唤醒了这次派单'统一记进台账即可审计。

**核实备注**:现状确认三套机制并存且语义割裂:cron._fire(cron.py:81)、dispatch_async 线程(mcp_server.py:90)、heartbeat(router.py:182,且 CrewOS 里 heartbeat 专指'通道健康探测'不是 agent 唤醒)。建议的'可审计为何醒来'是真价值。但把三者收敛成一个统一调度内核 = 重写 cron+async+健康探测三模块(建议方自标 effort large),对一人公司收益/风险比不划算,且 heartbeat 一词在 CrewOS 已被通道探测占用,强行复用会制造混淆。降级落法(给台账加 wake_source 维度)能拿到 80% 的可审计价值、effort 小。故 impact 中(不是 high):值得做的是审计维度,不是大一统重构。

### 13. 公司可移植性:完整编排配置导出成自包含目录、scrub 真实 secret、一行命令导入到另一台,配合社区模板 repo 把开一类公司做成可分发资产。

**来源**:paperclip/templates   **落点**:crewos/workspace.py(新增 export_company/import_company)+ crewos/cli.py(新增 crewos export/import 子命令)

**写法**:在 workspace.py 写 export_company(ws,name):用 shutil.copytree 把 agents/(role.md+provider.yaml+actions.yaml+memory/lessons.md)+config/+CrewOS.md 拷到目标目录,跳过 .env/data/,并对 provider.yaml 只保留 key_env 字段名(本来就不含真 key,无需脱敏);import_company 反向拷入 + 调 ensure_workspace 不覆盖已有,导入后扫描所有 channel.key_env 列出缺失环境变量。cli.py 加 export/import 子命令各一行。

**核实备注**:已核实:全仓库无任何 export/import/tarball/scrub 逻辑(grep 仅命中 .env 解析的 export 字样)。脱敏成本极低——真 key 从设计上只在 .env 与环境变量,provider.yaml 里全是 key_env 名称(router.py:35,Channel.api_key 从 os.environ 取),所以 export 只要排除 .env+data/ 即天然脱敏。workspace.py 已有 template_root/ensure_workspace 的拷贝范式可直接复用。模板市场属外部生态,不在代码内,按一条 CLI + README 链接即可。

### 14. Governance 审批工作流:招聘新 agent/改 workflow/调风险阈值/开高风险动作做成需人类 board 批准的一等审批流+全审计,与 L4 统一成 approvals 视图。

**来源**:paperclip/templates   **落点**:crewos/web_server.py(api_put_agent/api_put_file/api_put_settings 接入审批)+ crewos/risk.py(request 复用)

**写法**:把三个配置 mutation 端点纳入审批:api_put_agent(换 LLM 绑定)、api_put_file(改 actions.yaml/provider.yaml/role.md)、api_put_settings(改预算/token)在写盘前调 _risk().request('user', 'edit_config', summary, ...) 生成 L3/L4 审批单,pending 时返回 approval_id 不立即写,经 /api/approval 批准后再落盘;这样 governance 与现有 /api/approvals 视图、approvals 表审计天然统一。

**核实备注**:已核实 governance 缺口真实:risk.py 的 L0-L4 只覆盖 agent 对外动作(经 mcp request_action);而 web_server.py 的 api_put_agent(:373)、api_put_file(:445)、api_put_settings(:336)改 LLM 绑定/风险阈值/预算时直接 write_text 落盘,仅 validate_config 做语法校验,无审批门(只补记一条 status_update 日志)。改阈值/换脑本身是高杠杆操作却无人类确认,统一进 approvals 流是合理收口。但一人公司里改配置的本就是用户自己,价值 medium 而非 high;且需改三个端点为两阶段提交,effort medium。

### 15. 给 Memory Tree 每条记忆加 frontmatter:scope/category/source/confidence,注入时按 confidence 加权、可溯源、可被新证据降权淘汰

**来源**:paperclip/persistent   **落点**:memory.py vault_write/vault_search/select_lessons + templates/memory/tree.md

**写法**:在 vault_write 写入时落 YAML frontmatter(scope/category/source/confidence/updated),vault_search 解析 frontmatter 并在返回里带元数据;给 router 注入路径加一个按 confidence 降序+阈值过滤的选取,tree.md 补 schema 约定。

**核实备注**:已核实现状:tree.md 与 vault_write(memory.py:74)是纯自由文本 Markdown,无 frontmatter;vault_search(memory.py:91)是空格分词全命中的关键词检索,无加权无溯源无淘汰。建议 down 一档到 medium 而非原 high:一人公司早期记忆条目少,select_lessons 的二元组相似度(memory.py:46)已能防稀释,confidence 加权的边际收益要等记忆规模上来才显现;但 frontmatter 本身(尤其 source 区分 self/ceo/human)落地成本低、契合 Obsidian vault 定位,值得先上 category+source 两个字段。

### 16. per-agent 实验框架:对 recurring 且 safe-to-experiment 的任务跑 A/B 两种 prompt/模型,用评估集 KPI 判胜负后固化赢家

**来源**:paperclip/persistent   **落点**:ledger.py eval_report() + router.py 派单策略(新增 experiments 表)

**写法**:在 cron job 或派单上加 experiment_id+variant 标记,dispatch 按权重在 A/B 两个 prompt/model 间分流并记进 task_result payload,eval_report 按 experiment_id×variant 聚合一次过率/成本判胜负。

**核实备注**:数据底座已具备但框架全缺:eval_report(ledger.py:138)已天然按 (from_agent, model) 分组统计,A/B 只需再加一个 variant 维度;但当前无 experiments 表、无分流逻辑、cron(cron.py)也只是固定派单。契合'换便宜模型/改提示词从拍脑袋变数据驱动'的定位,且复用 eval_report 成本低于原估。但对一人公司:recurring+safe-to-experiment 的任务量短期可能不足以让 A/B 有统计意义,属于规模上来后的优化,故 medium 而非 high,排在 #2/#4 之后。

### 17. Multi-dimensional cost attribution (by project/goal/task/model, not just agent) + hard cascade on over-budget: auto-pause the agent and cancel its queued tasks, not just alert.

**来源**:paperclip/persistent   **落点**:crewos/ledger.py (cost_report + events schema) + crewos/router.py (BudgetExceeded path) + crewos/mcp_server.py (dispatch_async / _async_tasks)

**写法**:Add a `project` column (or payload key) to events and a by_project bucket in ledger.cost_report; on monthly/agent over-budget, write a budget_block + cancel queued _async_tasks for that agent instead of only logging escalation.

**核实备注**:Partially present, partially missing. Cost IS attributed by_agent + by_model + total (ledger.cost_report) and by agent×model (eval_report), and there is a per-task hard stop (router raises BudgetExceeded at cap, suspends that one task). MISSING: project/goal dimension (no such column; only task_id ungrouped), and any CASCADE — over-budget today blocks a single task; it does NOT pause an agent or cancel its other queued dispatch_async tasks (no pause/suspend/disable-agent code exists). The cascade half is the real net-new value; keep it soft (CrewOS philosophy is escalate-to-user, not silent auto-kill — so 'pause + ask' fits better than 'auto-cancel').

### 18. Observation log: CEO (Claude Code) receives periodic trend reports by cron (every N runs / daily / weekly) and writes structured 'observation + did I act?' notes, only changing config when a pattern is persistent and significant — avoid overfitting to noise.

**来源**:paperclip/persistent   **落点**:crewos/templates/config/crontab.yaml + crewos/templates/CrewOS.md (step 7) + crewos/templates/memory/ (new observations.md) + crewos/mcp_server.py (eval_report)

**写法**:Add a weekly cron job whose instruction is 'review eval_report + cost_report, append one structured note (observed / persistent? / acted or not) to memory/observations.md, and only propose a model swap if first_pass_rate stays low across N samples'.

**核实备注**:Closest existing pieces but genuinely not this. cron fires dispatches and there IS a 'weekly-cost-digest' job, but it just asks analyst for a one-paragraph summary — no structured observation log, no 'acted?' discipline, no anti-noise threshold. eval_report/cost_report exist (the data is there) but CrewOS.md step 7 treats model-switching as reactive ('when you consider switching, check eval_report'), not as a periodic review loop. Memory Tree has knowledge/ and projects/ but no observations/ convention. Small effort (it's mostly a cron entry + a handbook section + one new memory file), good fit for the local-first 'glass office' ethos. Highest-leverage of the persistent suggestions.

### 19. Adopt the adapter pattern internally as an Executor protocol (one execute() + optional test-environment, detect-model, session codec) so CrewOS ingests any runtime (Hermes, Codex, Ollama) and the HTTP bridge falls out for free.

**来源**:paperclip/hermes   **落点**:crewos/router.py — extract the _call_openai_compatible body behind an Executor protocol, then migrate the channel-calling path to it

**写法**:Define `class Executor(Protocol): def execute(self, model, messages, ...) -> dict` and make the current OpenAI-compatible HTTP call the default impl, so router.dispatch calls executor.execute() and new runtimes (Ollama, a local subprocess) drop in without touching dispatch/failover/DLP/ledger logic.

**核实备注**:Not present (router is hardcoded to one urllib OpenAI-compatible HTTP path; the only seam is the mock:// endpoint hack inside _call_openai_compatible). The pattern is a legitimately good refactor: it would cleanly separate transport from the dispatch/failover/budget/DLP/ledger orchestration that is CrewOS's real value, and make local runtimes (Ollama) — which DO fit local-first — first-class. But scoped to that internal benefit, not the 'HTTP bridge falls out for free' part (that's the paperclip tie-in, see prior verdict). Medium because it's architecture-improving but not user-visible; do it only when a non-OpenAI runtime is actually needed, else it's speculative generality. Note CrewOS's 'one agent = one model' design rule may conflict with multi-runtime per agent — keep executor per-channel, not per-agent.

### 20. 把公司/团队配置做成可移植 Markdown+YAML 包,用 `crewos add <github-shorthand>` 一行安装他人模板,带 collision rename|skip|replace、--dry-run、--include 子集,配 referenced/vendored/mirrored + commit sha+sha256 溯源。

**来源**:paperclip/tooling   **落点**:crewos/cli.py(新增 cmd_add 子命令)、crewos/workspace.py(ensure_workspace 已是同构的『纯文件、不覆盖已存在』拷贝器)

**写法**:新增 `crewos add <user/repo>`:git/tarball 拉到临时目录→校验是否含 agents/<name>/{role.md,provider.yaml} 结构→复用 workspace.ensure_workspace 的『dst.exists() 则 skip』逻辑(workspace.py:34-53)做 collision 处理,默认 skip、加 --force=replace;sha256 用 hashlib 对每个写入文件记一行 manifest 到 <ws>/agents/<name>/.source.yaml。

**核实备注**:CrewOS 配置已经是纯 Markdown+YAML 的可移植包形态(agents/<name>/role.md+actions.yaml+provider.yaml+memory/lessons.md,见 templates/agents/*),ensure_workspace 已经是『从模板树拷贝、绝不覆盖已存在』的 add 原型——所以分享格式天然就绪,只缺一个对外拉取的 cmd_add。fits 高:一人公司复用社区 agent 模板正对路。但完整溯源(referenced/vendored/mirrored 三态+sha 锁)对单人本地优先场景偏重,建议先做 add+skip/replace+--dry-run 这个 MVP,sha256 manifest 可选;因此从 large effort 的理想态我把可落地价值定为 medium。

### 21. 引入 atomic checkout 执行锁语义到并行派单队列:dispatch 前 agent 必须 checkout 任务(带 run-id),已占则跳过、永不重试冲突,天然防双重执行/抢单。

**来源**:paperclip/tooling   **落点**:crewos/mcp_server.py(dispatch_async/_do_dispatch、_async_tasks)、crewos/router.py(dispatch 入口)、crewos/ledger.py(append-only 事件正好做 checkout 凭据)

**写法**:在 router.dispatch 开头(router.py:241 附近,task_id 确定后)对同一 task_id 做一次原子 checkout:用 ledger 写一条 type='task_assign' 前先 `SELECT ... WHERE task_id=? AND type='task_checkout' AND round=?`,无则 INSERT 一条带 run-id 的 checkout 事件(靠 SQLite 行级写锁+busy_timeout 串行化),已存在则 raise AlreadyCheckedOut 直接跳过不重试。需在 ledger.EVENT_TYPES 加 'task_checkout'。

**核实备注**:确认缺失:dispatch_async 直接起 daemon 线程跑 _do_dispatch,_async_tasks 只按随机 handle 存(mcp_server.py:89-110),同一 task_id 被并行派两次不会被任何机制拦住;router/ledger 全文无 lock/claim/run-id(grep 证实)。fits 真:并行无上限是 CrewOS 明确卖点(CrewOS.md:53『并行无上限』),无抢单保护是真实风险。effort 确实小:ledger 已是 append-only + WAL + busy_timeout(ledger.py:50-56),加一种事件类型+一次条件插入即可。我把 impact 定 medium 而非 high,因为当前 CEO 是单一 CC 串行派单、双重执行概率不高,属防御性加固而非现痛点。

### 22. crewos doctor / doctor --repair 自检并自动修复(配置完整性/DB 连接/secrets/存储/关键文件就绪),内嵌进 start 流程。对标 paperclip run='init→doctor --repair→serve'。

**来源**:paperclip/tooling   **落点**:crewos/cli.py(新增 cmd_doctor)、crewos/workspace.py(validate_config 雏形在 web_server.py:399)、crewos/cli.py:cmd_start 内嵌调用

**写法**:新增 cmd_doctor:复用 web_server.validate_config(web_server.py:399-442,已能校验 provider/actions/crontab.yaml)对每个 agents/*/provider.yaml 跑一遍;再检查 .env 是否存在且 600、data/ledger.db 可连(Ledger 构造即建表)、每个 agent 的 channel.key_env 对应环境变量是否非空(缺则警告但不阻塞);--repair 时对缺失目录/模板调 ensure_workspace 补齐。cmd_start 开头加一行 `cmd_doctor(args, repair=True)`。

**核实备注**:部分已有但分散且未成命令:配置校验逻辑确实存在(web_server.validate_config,在保存配置时触发),但它绑在 Web PUT /api/file 上,没有独立的『启动前全量自检』入口,也没有 --repair 自动补齐;ensure_workspace 已是事实上的『补齐缺失文件、不覆盖』修复器(workspace.py:34-53)。所以这条是『把已有零件组装成一个 doctor 命令并塞进 start』,effort 小、fits 真(本地优先单人最怕写坏一个 yaml 让 agent 静默瘫痪,validate_config 注释原话)。与建议4共用 doctor 命名空间,建议合并实现。

### 23. 把上下文向上流做成显式契约:每次派单自动把 task→project→company goal 链路注入 prompt,让便宜模型知道做什么且为什么。对标 heartbeat GET /agents/me 返回身份+汇报链+预算+goal。

**来源**:paperclip/tooling   **落点**:crewos/router.py:dispatch(prompt 组装 264-272)、crewos/memory.py(Memory Tree)、crewos/templates/memory/tree.md

**写法**:在 router.dispatch 组 system prompt 处(router.py:264-267),除 role_prompt+memory_digest 外,再固定注入一个 header:读 <ws>/memory/company.md(新增的一页公司目标/汇报链)前 ~800 字,拼成『## 公司目标与你的位置』段。company.md 作为 ensure_workspace 的新模板文件,内容写 mission + 各 agent 角色定位。

**核实备注**:确认缺失:dispatch 当前只注入 role_prompt + memory_digest(lessons 检索 top-3 + 角色 memory),没有任何 task→project→company goal 的链路(router.py:264-272 全文可见);goal 现存于 memory/tree.md 的 projects/ 里,但只在 CC 主动塞进 context 参数时才进 prompt,不是固定契约。fits 真:CrewOS 的卖点正是给便宜专精模型配 CEO,让它们『知道为什么做』能减少跑偏。effort 小(加一个模板文件 + dispatch 里几行注入)。impact 定 medium:有正向价值但非阻塞性痛点,且要注意注入量别稀释错题本(CrewOS 已专门为此做了 lessons 检索化 memory.py:46)。


## 低影响

### 1. Per-agent MCP/skill assignment via deny-list/override (company default + per-agent extras + per-agent excludes), resolved at runtime so each cheap model only gets the MCP/skills its role needs.

**来源**:paperclip/persistent   **落点**:crewos/templates/agents/<name>/actions.yaml + crewos/risk.py (action_spec) + crewos/templates/CrewOS.md

**写法**:actions.yaml already lists per-agent registered actions with risk levels — add an `allowed: true/false` (default the unregistered → deny rather than today's L3) so an agent literally cannot request actions outside its role; resolve company-default + per-agent in risk.action_spec.

**核实备注**:Architecture mismatch with paperclip's model: CrewOS agents are NOT tool-holding subagents — they are single-LLM call wrappers (router._call_openai_compatible). All MCP tools live on the CEO/Claude-Code side; provider.yaml is model/channels/pricing only, actions.yaml is a pure L0-L4 risk registry. There is no per-agent MCP/skill surface to deny. The only thing that maps is tightening actions.yaml from 'unregistered → L3' to a real per-agent action whitelist. Low value because the convergence target (agent tool access) doesn't exist.


## CrewOS 反而更强的 7 个维度(差异化弹药)

CrewOS 在多个维度反而更强或更独特,适合做差异化定位:(1)真正的智能中心化 CEO:CrewOS 是 Claude Code 当 CEO 主动分析任务、择优派单给 7 个便宜模型,这是'强模型指挥弱模型'的成本套利架构;Paperclip 的 CEO 只是 org chart 顶点的一个 role,真正的硬编排在 Postgres/server/policy 层,委派逻辑本身并不'聪明'。(2)风险分级 L0-L4 + L3 倒计时 + L4 人工审批:Paperclip 只有两档信任预设(Standard / Low-Trust),且偏'输入可信度/隔离'维度;CrewOS 的 L0-L4 是按'动作危险度'连续分级并带倒计时/审批阶梯,粒度更细、更贴近实际放行决策——这是 CrewOS 的护城河,Paperclip 在这维度明显更弱。(3)错题本(lessons-learned)闭环:CrewOS 在审阅驳回时自动写回错题本并检索化复用,形成'犯错→沉淀→下次规避'的学习回路;Paperclip 的 UNTRUSTED-PR-REVIEW 只是人工导向的安全审计工具,没有任何自动驳回反馈或经验沉淀回路,记忆/知识还停在 roadmap。(4)自动检查层 + 入站 DLP:CrewOS 有专门的自动检查层与入站数据防泄露,Paperclip 的治理偏审批门/预算/审计,缺少独立的产物自动校验与 DLP 过滤层。(5)Perceiver 真实视频多模态:CrewOS 能真实调用视频多模态感知,Paperclip 文档里完全没有多模态/感知维度。(6)Python 引擎 + Memory Tree(Obsidian)+ SpaceX 风格看板回放:CrewOS 的记忆是已落地的跨项目 Obsidian vault(Paperclip 的 PARA memory 是独立 skill、主线记忆仍 roadmap),看板支持回放动画,产品观感更强。(7)评估集自动生长 + 模型胜任度报表:CrewOS 把'哪个便宜模型擅长哪类任务'做成会自动生长的胜任度报表用于派单决策;Paperclip 的 evals 还在 Phase 0(只有 8 个确定性断言用例,rubric 评分和效率指标都在未来阶段),且不反哺派单。一句话:Paperclip 的护城河是'企业级编排基础设施的工程严谨度 + org chart 隐喻的传播力',CrewOS 的护城河是'强 CEO 智能派单 + 风险分级 + 学习闭环 + 多模态'的实际智能与安全深度。