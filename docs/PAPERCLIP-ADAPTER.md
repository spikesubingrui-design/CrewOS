# CrewOS × Paperclip 适配器

把**一整支 CrewOS 乘组**接进 [Paperclip](https://github.com/paperclipai/paperclip)
(70k★ 的 AI agent 公司管理平台)当一名"员工/部门"。Paperclip 当公司外壳与组织视图,
CrewOS 当智能派单 + 风控大脑,底下的便宜模型(乃至 Hermes)当执行手。

## 为什么能直接接,不用写 TypeScript

Paperclip 内置一个**通用 `http` adapter** —— 不需要写适配包,只要把某个 Paperclip
agent 的 `adapterType` 设成 `http`、`adapterConfig.url` 指向 CrewOS 的
`/paperclip/execute`。Paperclip 每次按心跳/评论/派单唤醒这名员工,就 POST 一份
run-context 过来;CrewOS 翻译成一次派单,跑完按 Paperclip 的 `AdapterExecutionResult`
形状返回。

## 接法(3 步)

1. 启动 CrewOS 看板(它同时提供适配器端点),建议设一个 access token:
   ```bash
   crewos start            # 端点在 http://127.0.0.1:8466/paperclip/execute
   ```
   想让 Paperclip(可能在另一台机/容器)访问,在 CONFIG 设 `dashboard_token`,
   或用隧道暴露 127.0.0.1。

2. 看 CrewOS 能派给哪些乘组成员:
   ```bash
   curl http://127.0.0.1:8466/paperclip/manifest
   # → {adapter, execute_url, crew:[{name,status}...]}
   ```

3. 在 Paperclip 里新建/编辑一个 agent:
   - `adapterType: http`
   - `adapterConfig.url: http://<crewos-host>:8466/paperclip/execute?token=<dashboard_token>`
   - `adapterConfig.crew_agent: writer`(派给哪个乘组成员;缺省 researcher)
   - 可选 `adapterConfig.budget_usd`、`adapterConfig.checks`(验收规格,见 checks.py)

之后这名 Paperclip"员工"的每个任务都会落到 CrewOS,由对应乘组成员执行,成本与产出
回写 Paperclip,同时也进 CrewOS 自己的台账(双向可见)。

## 跨心跳续作(sessionParams 往返)

CrewOS 把 `task_id` 作为不透明 resume token 放进返回的 `sessionParams`。Paperclip
原样保存,下次就同一任务唤醒时回传;CrewOS 用同一 `task_id` 续派(round+1)——
于是 CrewOS 的审阅循环能跨 Paperclip 心跳延续,而记忆/状态的所有权留在编排方。

## 返回形状(AdapterExecutionResult)

```json
{
  "exitCode": 0,
  "provider": "crewos",
  "model": "qwen3.7-max",
  "costUsd": 0.0027,
  "usage": {"inputTokens": 3100, "outputTokens": 900},
  "summary": "文案 v1 …",
  "resultJson": {"content": "...", "crew_agent": "writer", "checks": {...}},
  "sessionParams": {"crewos_task_id": "task_ab12", "round": 0}
}
```

被拒/挂起(预算熔断、agent 暂停、全通道宕机、入站 DLP)返回 `exitCode: 1` +
summary,Paperclip 按其治理流程(org chart 上报/审批)处理。

## 三者关系:Hermes × CrewOS × Paperclip

你同时有这三样,它们不互斥,各归其位:

| 层 | 角色 | 谁 |
|---|---|---|
| 公司外壳 / 组织视图 / 多公司治理 | 老板的"管理后台" | Paperclip |
| 智能派单 + 风险分级 + 学习闭环 | "经理大脑" | CrewOS(CC 当 CEO) |
| 廉价执行 / 个人助理手 | "员工的手" | 便宜模型 / Hermes |

Paperclip 官方已有 `hermes-paperclip-adapter`(把 Hermes 当受管员工);CrewOS 这个
适配器是同一范式的另一条接入 —— 区别是接进去的不是单个 agent,而是一支带 CEO 审阅
和风控的完整乘组。

> 当前为 headless MVP:`/paperclip/execute` 做一次"派给指定乘组成员"的派单并返回。
> 完整的「CC 当 CEO 自主拆解→多轮审阅」模式需要 Claude Code 接 crewos MCP 驱动,
> 属于编排方那一侧的运行形态,不在本 HTTP 端点内。
