# 排错指南

先跑 **`crewos doctor`**(诊断停工/异常任务,`--why-stopped` 看细节)和看板右侧遥测流 / 告警横幅。

## 常见错误

### `全部通道不可用` / AllChannelsDown
- **原因**:该 agent 所有通道都没配 key,或模型 ID 不对,或 key 无效。
- **修**:看板 CONFIG → 对应角色卡 → 选供应商 + 粘 API key;或 `crewos status` 看通道健康。
  错误信息里会带具体原因(401=key 无效、404/400=模型 ID 不对、429=限流)。

### 任务「没有收到任何代码交付」/ 产出为空
- **原因**:推理模型(如 ds-flash)把 `max_tokens` 烧在思维链上、正文留空(`finish_reason=length`)。
- **修**:已自动诊断并上报(`empty_reason` 会点明)。CEO 编排默认给足额度;手动派单调大 `max_tokens`,
  或给该 agent 换非纯推理模型。

### 预算熔断 `budget_block` / `已花费 ≥ 上限`
- **原因**:单任务花费撞到上限(默认按 CONFIG 的「单任务燃料上限」,展示为人民币)。
- **修**:看板 MISSIONS → 该任务 → 「↥ 提额」;或 CONFIG 调高上限。提额后重派即续跑。

### `派单内容含敏感信息,已拒绝` / 入站 DLP
- **原因**:指令/上下文/媒体 URL 命中密钥或 DLP 黑名单,拒发第三方模型。
- **修**:移除敏感内容(API key 应配在供应商面板而非写进指令)后重派。

### 月度硬上限触发,全员暂停
- **原因**:本月总花费撞 `monthly_hard_usd`,自动暂停所有 agent。
- **修**:`crewos resume <agent>` 或看板恢复;调高月度上限。

### 看板白屏 / 连不上
- **原因**:`crewos start` 没在跑,或端口被占。
- **修**:确认服务在跑;看板 init 失败会显示红色提示并 10s 自动重试。换端口:`crewos start --port 8500`。

### U 第二大脑「未连接」
- **原因**:`gbrain` 不在 PATH,或 `~/.openclaw`/`~/wiki` 不存在。
- **修**:`crewos memory` 看三层状态;装好 gbrain(默认从 `~/.bun/bin` 找)或在 settings.yaml 改路径;
  不接 U 不影响主流程(召回失败只返回空,绝不阻断派单)。

## 监控

- `crewos doctor` —— 停滞/异常任务分类(Silent Watchdog 后台已在自动上报)。
- `crewos cost` —— 成本报表;看板 COST 视图(人民币)同款。
- `crewos status` —— 乘组与通道健康。
- `crewos evals` —— 各模型胜任度(一次过率/均轮/上报/花费)。
