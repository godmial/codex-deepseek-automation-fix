# Codex DeepSeek Automation Fix

> **让 Codex Desktop 的定时任务在 DeepSeek 上跑起来。**
>
> Codex 的自动化注入项没有 `call_id`，DeepSeek 严格的 Responses 接口会在模型运行前直接返回 400 ——
> 定时任务、heartbeat、跨线程消息全部失效。这是一个**零依赖本地中间件**，外加一个能从你自己的会话
> 日志里直接诊断出这个原因的 `doctor`。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)

[![CI](https://github.com/godmial/codex-deepseek-automation-fix/actions/workflows/ci.yml/badge.svg)](https://github.com/godmial/codex-deepseek-automation-fix/actions)

**中文搜索关键词：** Codex 定时任务失败、Codex Desktop DeepSeek 400、`missing field call_id`、
`function_call_output requires call_id`、automation 不能用、heartbeat 报错、定时任务模型没有运行、
DeepSeek Responses 严格校验、聚合网关、中转站、schedule 失效。

**English search terms:** Codex automation DeepSeek, missing field `call_id`,
function_call_output requires call_id, strict Responses provider, heartbeat 400,
codex desktop schedule broken, zero-dependency middleware.

---

## 30 秒判断：是不是这个问题

```bash
python3 codexfix.py doctor
```

```text
codexfix doctor
  CODEX_HOME : /Users/you/.codex
  扫描会话数 : 75

  孤立工具输出项 : 25 条，分布在 8 个会话
      automation_update                25
      无 call_id（任何上游都无法解析）             25

  因缺 call_id 而失败的轮次 : 5 个会话
      sessions/2026/09/10/rollout-2026-09-10T17-44-47-....jsonl:64

  结论：命中本工具要修的问题（自动化/注入请求被严格 Responses 上游拒绝）。
```

`doctor` 只读本地记录，不改任何东西。它能把**真的失败**（`task_complete` 带着上游错误）和
「只是引用了错误文本」区分开，并把「无 `call_id`」和「`call_id` 悬空」分开统计。

## 症状

平时手动对话一切正常，但只要**定时任务（cron / heartbeat）一触发**，回合立刻失败、模型一行都没输出：

```text
Failed to deserialize the JSON body into the target type: input: missing field `call_id` at line 1 column N
Invalid Value: 'input.call_id'. Function call output requires call_id.
No tool call found for tool output with call_id ...
```

同一条线程之后连手动发消息也会一起失败——因为那条畸形的注入项已经留在历史里，每次请求都会被重发。

## 根因

Codex 把定时任务、子代理报告、跨线程消息这类**注入输入**，写成一条独立的工具输出项：

```json
{
  "type": "function_call_output",
  "id": "fco_...",
  "name": "automation_update",
  "namespace": "codex_app",
  "output": "<heartbeat>...</heartbeat>"
}
```

它**没有 `call_id`**，也**没有配对的 `function_call`**。而 Responses 规范要求每条工具输出都必须带
`call_id`，DeepSeek 的 `/responses` 是严格实现，于是整个请求在模型推理之前就被拒绝。

宽松的中转站会照收这条畸形项，所以同样的 Codex 配置"换个中转就能跑"，问题只在严格上游暴露。

### 为什么「补一个 call_id」不行

最直觉的修法是给注入项补一条配对的 `function_call`。**这条路在 DeepSeek 上是死的**：

```text
The `reasoning_text` in the thinking mode must be passed back to the API.
```

DeepSeek 的 thinking 模式要求把 `reasoning_text` 一起回传，一个合成出来的工具调用凑不齐这个三元组。
（openai/codex issue #42067 里提议的补丁正是这个做法，对 DeepSeek 用户无效。）伪造一个 `call_id` 也不行，
会立刻变成上面第三条错误。

所以唯一可行、且已验证的做法是：**把这条结构上无法解析的项改写成一条普通消息**。

## 修复：两步

零依赖，Python 3.9+。

```bash
# 1) 起中间件（装成常驻服务，macOS 用 launchd、Linux 用 systemd --user）
python3 codexfix.py install --upstream http://YOUR-GATEWAY:PORT --models deepseek

#    只想先试一下，可以前台运行：
python3 codexfix.py serve   --upstream http://YOUR-GATEWAY:PORT --models deepseek
```

```toml
# 2) 把 Codex 指向它 —— 这是唯一需要改的配置
# ~/.codex/config.toml
[model_providers.YOUR-PROVIDER-NAME]
base_url = "http://127.0.0.1:18317/v1"
```

不用重启 Codex，也**不用动 key、模型列表、账号或网关配置**。`install` 会在装完后自己打 `/healthz`
确认服务真的起来了，起不来会明确报错。

## 它到底改了什么

| 情形 | 默认行为 | 原因 |
|---|---|---|
| 工具输出**没有 / 空的 `call_id`** | 改写成一条消息 | 任何上游都解析不了，永远安全 |
| `call_id` **在本请求里找不到配对** | **不动**（需要 `--repair-unpaired` 才开） | 有状态上游可能靠 `previous_response_id` 自行解析 |
| `call_id` 配对完整的正常工具输出 | 不动 | 合法请求 |
| 其他所有内容（消息、工具调用、别的接口、SSE 流） | 逐字节透传 | 这是中间件，不是协议翻译层 |

改写会保住内容：文本进 `input_text`，图片仍是图片，`encrypted_content` 用占位符替代而不是当明文塞回去，
工具名保留在 `[tool output: <name>]` 标记里。

**角色默认是 `user`，不是 `developer`。** 工具输出的内容可能来自不可信的地方，把它提升成 developer
等于给它加权限；实测 `user` 已经能让自动化指令正常执行。只有在你明确希望注入指令带 developer 权威时，
才用 `--role developer`。

## 验证

```bash
curl -s http://127.0.0.1:18317/healthz     # 运行状态、计数、生效参数
tail -f ~/.codexfix/codexfix.log           # 每行：model / rewritten / 上游状态码 / 耗时

# 先探一下你的上游到底严不严格（不改任何东西）
python3 codexfix.py probe --url http://YOUR-GATEWAY:PORT --token $KEY --model deepseek-flash
```

`probe` 对严格上游会明确给出判定：

```text
HTTP 400 — {"error":{"message":"Failed to deserialize the JSON body into the target type:
input: missing field `call_id` at line 1 column 252", ...}}

判定：严格实现，会被 Codex 的注入项卡住。用 codexfix serve + base_url 指向它。
```

实测记录（本机，2026-09-11）：定时任务真实触发后，运行记录里 `thread_source=automation`，
注入项仍是 `call_id=None`（这侧不变），但模型正常产出回复，`task_complete error=None`。

## 为什么不用别的修法

| 做法 | 问题 |
|---|---|
| 手工改会话记录（把畸形项改成 `agent_message`） | 能救回当次，但**会复发**——每次注入都会再写一条畸形项 |
| 给注入项补配对的 `function_call` | DeepSeek thinking 模式要求 `reasoning_text`，直接 400 |
| 伪造一个 `call_id` | 立刻变成 `No tool call found for tool output` |
| 把 DeepSeek 换成不校验的中转站 | 本质是换掉了模型来源 |
| 打补丁版 Codex CLI | 那解决的是子代理收不到任务的另一个问题，不是定时任务 |

## 其他严格供应商

同样的 bug 也发生在 Azure OpenAI（`wire_api = "responses"`）、OpenRouter、Kimi 以及自建网关。
引擎本身与供应商无关，去掉 `--models` 过滤即可对全部模型生效：

```bash
python3 codexfix.py install --upstream http://YOUR-GATEWAY:PORT
```

只有当你想限定作用范围时才加 `--models deepseek`（本机就是这样配的）。

## 安全与限制

- **不持有任何密钥**：`Authorization` 原样转发，`/healthz` 和日志里都不会出现；日志只记
  `method path model rewritten status bytes ms`。
- **不要把真实网关地址提交进仓库**：`--upstream` 故意没有默认值。`scripts/scan_sensitive.py`
  会在 CI 里拦截私网地址、绝对家目录路径和疑似密钥。
- **监听范围**默认只绑 `127.0.0.1`。
- **作用范围**：只修这个畸形工具输出项。其他供应商差异（reasoning 回传、`web_search_call` 字段方言、
  call_id 长度限制）不在范围内。
- **macOS 注意**：`install` 会把脚本复制到 `~/.codexfix/` 再运行——launchd 代理读不到 `~/Documents`、
  `~/Desktop`、`~/Downloads`（TCC 保护），脚本放在那里会卡在 `open()`。
- **服务挂掉时**：指向 `127.0.0.1:18317` 的请求会失败。请让服务受监督（`launchd KeepAlive` /
  `systemd Restart=always`），或把 `base_url` 改回原网关即可停用。

## 卸载 / 回滚

```bash
python3 codexfix.py uninstall     # 停止并移除服务
# 然后把 ~/.codex/config.toml 里的 base_url 改回原网关地址
```

## 相关项目

- [openai/codex#41690](https://github.com/openai/codex/issues/41690)（DeepSeek 原始报告）及
  [#41799](https://github.com/openai/codex/issues/41799) Azure、[#42067](https://github.com/openai/codex/issues/42067)、
  [#42088](https://github.com/openai/codex/issues/42088)、[#43515](https://github.com/openai/codex/issues/43515) Kimi、
  [#44519](https://github.com/openai/codex/issues/44519) Windows —— 上游全部仍未修复。
- [electrobum/codex-http-responses-bootstrap-compat](https://github.com/electrobum/codex-http-responses-bootstrap-compat)
  —— 同样是「把 bootstrap 转回 user input」的窄范围兼容层，Node 实现，含中英接入指南。
- [lidge-jun/opencodex](https://github.com/lidge-jun/opencodex) —— 通用 provider 代理，代码里已包含同类修复
  （`repairUnidentifiedToolOutputItems`），并额外处理多种 DeepSeek 差异。需要它来托管 provider 就用它。
- [CCanxue/codex-deepseek-subagent-fix](https://github.com/CCanxue/codex-deepseek-subagent-fix) ——
  解决的是**子代理收不到任务**（`agent_message` / `encrypted_content`）的另一个问题。
- [hairyf/codex-deepseek-proxy](https://github.com/hairyf/codex-deepseek-proxy) —— 同一类子代理问题的轻量代理。

本项目刻意保持小而专：它是插在你现有网关前面的中间件，额外提供诊断能力，而不是替代你的 provider 体系。

## English

**The problem.** Codex Desktop injects automations, subagent reports and cross-thread messages as a
standalone `function_call_output` item with **no `call_id`** and no matching `function_call`. DeepSeek's
strict `/responses` implementation rejects the whole request before inference:

```text
Failed to deserialize the JSON body into the target type: input: missing field `call_id` at line 1 column N
```

Manual chats keep working, scheduled tasks die instantly, and the poisoned thread then fails on every
later turn. Lenient gateways accept the malformed item, which is why this only shows up on strict ones.

**Why the obvious fix fails.** Pairing the orphan with a synthetic `function_call` is rejected too —
DeepSeek's thinking mode also demands the original `reasoning_text`:
``The reasoning_text in the thinking mode must be passed back to the API.`` Fabricating a `call_id`
only produces `No tool call found for tool output with call_id ...`.

**The fix.** A zero-dependency local middleware that rewrites *only* structurally unresolvable
tool-output items into ordinary messages and passes everything else through byte-for-byte (SSE included).

```bash
python3 codexfix.py doctor                                                  # diagnose first
python3 codexfix.py install --upstream http://YOUR-GATEWAY:PORT --models deepseek
# then set model_providers.<name>.base_url = "http://127.0.0.1:18317/v1"
```

Rewrites only when the item cannot be resolved: missing/empty `call_id` (always safe), or an unpaired
`call_id` (opt-in via `--repair-unpaired`). Default role is `user`, never a trust-elevating `developer`.
No credentials are stored, the `Authorization` header is forwarded untouched, and the listener binds to
`127.0.0.1` by default.

## License

MIT
