---
name: fix-codex-automation-on-deepseek
description: 当 Codex 的定时任务、heartbeat 或跨线程消息在 DeepSeek（以及其他严格 Responses 供应商）上立即失败，报错包含 "missing field `call_id`"、"function_call_output requires call_id" 或 "No tool call found for tool output" 时使用。本技能通过只读诊断确认根因，并引导安装 codexfix 本地中间件完成修复。
---

# 修复 Codex 定时任务在 DeepSeek 上失败

## 症状

普通手动对话正常，但**定时任务一触发就失败、模型一行都没输出**，报错是这三条之一：

```text
Failed to deserialize the JSON body into the target type: input: missing field `call_id` at line 1 column N
Invalid Value: 'input.call_id'. Function call output requires call_id.
No tool call found for tool output with call_id ...
```

之后同一条线程连手动发消息也会一起失败，因为那条畸形注入项留在历史里，每次请求都被重发。

## 根因（不要重新推导，直接采信）

Codex 把自动化、子代理报告、跨线程消息写成一条独立的工具输出项，它**没有 `call_id`、也没有配对的 `function_call`**：

```json
{"type":"function_call_output","id":"fco_...","name":"automation_update","namespace":"codex_app","output":"<heartbeat>...</heartbeat>"}
```

Responses 规范要求每条工具输出带 `call_id`，DeepSeek 的 `/responses` 是严格实现，于是整个请求在模型推理前就被拒。宽松中转站会照收，所以同样配置"换个中转就能跑"。

## 流程

### 第一步：只读诊断

调用 MCP 工具 `codexfix_doctor`（无参数即可）。判读：

- 「孤立工具输出项 > 0」且「真实失败的会话 > 0」→ 命中本问题，继续第二步
- 只有孤立项、没有失败会话 → 当前上游较宽松，先不要改任何东西
- 有失败会话但没有孤立项 → 不是这个问题，把会话文件路径交给用户进一步排查

### 第二步：确认上游严格性

调用 `codexfix_probe`，`url` 填用户的网关根地址（如 `http://gateway.example:8080` 或 `.../v1`）。token 会自动从 `~/.codex/config.toml` 里匹配该上游读取，**不要要求用户把密钥贴出来**。

- 返回 400 且含 `call_id` → 严格实现，需要中间件
- 返回 200 → 宽松实现，本问题不适用
- 返回 5xx → 上游暂时不可用，结论无效，稍后重试

### 第三步：安装中间件（写操作，需用户确认）

MCP 工具全部是只读的，安装必须走命令行，并且**先向用户说明会改哪一行配置**。项目地址：
`https://github.com/godmial/codex-deepseek-automation-fix`

```bash
# 起常驻服务（macOS 用 launchd，Linux 用 systemd --user），装完会自己打 /healthz 自检
python3 codexfix.py install --upstream http://YOUR-GATEWAY:PORT --models deepseek
```

然后把 Codex 指向它——**这是唯一需要改的配置**：

```toml
# ~/.codex/config.toml
[model_providers.YOUR-PROVIDER-NAME]
base_url = "http://127.0.0.1:18317/v1"
```

不需要重启 Codex，也不要动 key、模型列表或账号配置。

### 第四步：验证

1. `codexfix_status` → 应返回 `ok: true`，且 `models` 包含 deepseek
2. 让用户建一条每分钟触发的测试任务（只输出 `hi`，不要调工具）
3. 等第一次真实触发后，调用 `codexfix_doctor` 复查：真实失败的会话数不应再增长
4. 提醒用户把测试任务暂停或删除

## 不要做的事

| 做法 | 为什么不行 |
|---|---|
| 给注入项补一个 `call_id`，或补一条配对的 `function_call` | DeepSeek thinking 模式还要求回传 `reasoning_text`，直接 400（`The reasoning_text in the thinking mode must be passed back to the API.`） |
| 伪造 `call_id` | 立刻变成 `No tool call found for tool output` |
| 手工编辑会话 JSONL 把畸形项改掉 | 能救回当次，但每次注入都会新写入一条畸形项，**会复发** |
| 把 DeepSeek 换成不校验的中转站 | 本质是换掉了模型来源 |
| 装补丁版 Codex CLI | 那解决的是子代理收不到任务的另一个问题，不是定时任务 |

## 边界

本技能只处理「注入项缺 `call_id`」这一类失败。其他供应商差异（reasoning 回传、`web_search_call` 字段方言、call_id 长度上限）不在范围内，遇到时如实说明而不是硬套。

中间件进程必须在任何模型请求发生时都在监听——插件自身不能替代它。插件提供的是诊断与流程，常驻服务仍由 `codexfix install` 负责。
