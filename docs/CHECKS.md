# 自动检查层(Checks)

机器先验收硬指标,CEO 只看标红项。派单时传 `checks`(MCP 工具 `dispatch` 的 `checks_json` 参数,
或 Router.dispatch 的 `checks=[...]`),产出会先过这些检查,报告随结果返回(`task_result` 事件的
`checks_passed` / `check_failures`)。CEO 验收阶段据此判定 ✅/⚠️/❌。

源码:`crewos/checks.py`。

## 六种检查

| type | 作用 | 示例 |
|------|------|------|
| `word_count` | 字数在区间内(按去空白字符数) | `{"type":"word_count","min":300,"max":1000}` |
| `must_include` | 列表里的词**全部**出现 | `{"type":"must_include","values":["hook","CTA"]}` |
| `forbid` | 列表里的词**全部不**出现 | `{"type":"forbid","values":["敬请期待","点击链接"]}` |
| `must_match` | 匹配正则(MULTILINE) | `{"type":"must_match","pattern":"^#\\s"}` |
| `json_parseable` | 产出能被 `json.loads` 解析 | `{"type":"json_parseable"}` |
| `min_links` | 至少 N 个 http(s) 链接 | `{"type":"min_links","count":3}` |

## 什么时候用

- **写作(writer)**:`word_count`(平台字数)+ `must_include`(hook/CTA)+ `forbid`(违禁词)。
- **结构化抽取(analyst/runner)**:`json_parseable` 保证下游能直接吃。
- **研究(researcher)**:`min_links` 逼出有来源、`must_include` 关键问题都答到。
- **代码(coder)**:`must_match`(如 `<!DOCTYPE html`)+ `forbid`(`TODO`、`省略`)防半成品。

## 怎么传

CEO 自动编排时可在 instruction 的【验收标准】里写明,由 CEO 决定;
手动/MCP 派单则显式传 `checks_json`,例如:

```bash
# MCP 工具调用(由 CC 发起)
dispatch(agent="writer", instruction="写小红书文案",
         checks_json='[{"type":"word_count","min":80,"max":300},{"type":"must_include","values":["#"]}]')
```

检查不过不会自动重试,但会标红进 `check_failures`,CEO 验收时必须降级结论并指出返工点
(见 `ceo.py` 的 `REVIEW_SYSTEM`)。
