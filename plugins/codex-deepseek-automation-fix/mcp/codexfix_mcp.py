#!/usr/bin/env python3
"""Stdio MCP server exposing codexfix's diagnostics as agent tools.

Zero dependencies. Speaks newline-delimited JSON-RPC 2.0 over stdio, which is the
transport Codex uses for plugin-provided MCP servers.

Everything here is read-only on purpose: it tells the agent *why* a Codex
automation is failing and whether an upstream is strict. Installing the
middleware, changing ``base_url`` and starting a service stay in the user's
hands (or in the standalone ``codexfix`` CLI), so a tool call can never
silently rewrite someone's provider configuration.

stdout is reserved for protocol messages; diagnostics go to stderr.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.dont_write_bytecode = True  # keep __pycache__ out of the plugin directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import codexfix  # noqa: E402  (vendored copy, synced from the repo)

SERVER_NAME = "codexfix"
SERVER_VERSION = "0.1.0"
DEFAULT_PORT = 18317
DEFAULT_MODEL = "deepseek-flash"


def log(message):
    sys.stderr.write("[codexfix-mcp] %s\n" % message)
    sys.stderr.flush()


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


def tool_doctor(args):
    home = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
    since = int(args.get("since_days") or 0)
    limit = int(args.get("limit") or 0)
    report = codexfix.scan_sessions(home, since, limit)

    lines = []
    lines.append("扫描会话数：%d" % report["sessions_scanned"])
    lines.append("孤立工具输出项：%d 条，分布在 %d 个会话" % (
        len(report["orphans"]), len({o[0] for o in report["orphans"]})))
    for name, count in sorted(report["by_name"].items(), key=lambda kv: -kv[1]):
        lines.append("  - %s ×%d" % (name, count))
    for reason, count in sorted(report["by_reason"].items(), key=lambda kv: -kv[1]):
        label = "无 call_id（任何上游都无法解析）" if reason == "unidentified" \
            else "call_id 悬空（有状态上游可能自行解析）"
        lines.append("  - %s ×%d" % (label, count))
    lines.append("因缺 call_id 而真实失败的会话：%d" % len(report["error_sessions"]))
    for path, line_no in report["error_sessions"][:10]:
        lines.append("  - %s:%d" % (os.path.relpath(path, home), line_no))

    if report["error_sessions"] and report["orphans"]:
        verdict = ("判定：命中 Codex 注入项缺 call_id 的问题——自动化/heartbeat/跨线程消息"
                   "在严格 Responses 上游上会在模型运行前被拒。")
        next_step = ("下一步：确认上游严格性（codexfix_probe），然后用 codexfix CLI "
                     "安装本地中间件并把 model_providers.<name>.base_url 指向它。")
    elif report["orphans"]:
        verdict = "判定：本地存在孤立工具输出项，但没有观察到它导致失败，当前上游可能较宽松。"
        next_step = "下一步：换用严格上游时再启用中间件。"
    elif report["error_sessions"]:
        verdict = "判定：有请求因缺 call_id 失败，但本地记录里没找到孤立输出项，可能是其它构造路径。"
        next_step = "下一步：把上面列出的会话文件附到上游 issue 里进一步排查。"
    else:
        verdict = "判定：没有发现该问题。"
        next_step = "无需处理。"
    return "\n".join(lines + ["", verdict, next_step]), False


def tool_status(args):
    port = int(args.get("port") or DEFAULT_PORT)
    url = "http://127.0.0.1:%d/healthz" % port
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.load(response)
    except Exception as exc:
        return ("中间件未在 127.0.0.1:%d 响应（%r）。\n"
                "说明：本机的 codexfix serve 没有运行；自动化请求会直接打到原网关。"
                % (port, exc)), False
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return "中间件正在运行：%s\n%s" % (url, text), False


def _token_from_config(upstream_host):
    """Find a bearer token for this upstream in ~/.codex/config.toml.

    Keeps credentials out of the conversation: the agent never has to be told a
    key just to test whether an upstream is strict.
    """
    path = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
    path = os.path.join(path, "config.toml")
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    for block in re.findall(r"\[model_providers\.[^\]]+\]([\s\S]*?)(?=\n\[|\Z)", text):
        base = re.search(r'base_url\s*=\s*"([^"]+)"', block)
        token = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', block)
        if base and token and upstream_host in base.group(1):
            return token.group(1)
    return ""


def tool_probe(args):
    url = (args.get("url") or "").rstrip("/")
    if not url:
        return "缺少 url 参数（例如 http://gateway.example:8080）。", True
    model = args.get("model") or DEFAULT_MODEL
    token = args.get("token") or os.environ.get("CODEXFIX_TOKEN") or ""
    if not token:
        token = _token_from_config(url)
    endpoint = url if url.endswith("/responses") else url + (
        "/responses" if url.endswith("/v1") else "/v1/responses")

    payload = {
        "model": model,
        "max_output_tokens": 8,
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "id": "fco_probe",
             "name": "automation_update", "namespace": "codex_app", "output": "probe"},
        ],
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return ("HTTP %d — 上游接受了这条畸形注入项（宽松实现），不需要中间件。"
                    % response.status), False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code >= 500:
            return "上游暂时不可用（HTTP %d），这次结果不构成判断，稍后重试。" % exc.code, False
        if "call_id" in body:
            return ("HTTP %d — 严格实现，会被 Codex 的注入项卡住。\n%s\n\n"
                    "判定：需要 codexfix 中间件。" % (exc.code, body[:400])), False
        return "HTTP %d — %s" % (exc.code, body[:400]), False
    except Exception as exc:
        return "请求失败：%r（地址写错了？token 无效？）" % (exc,), True


TOOLS = [
    {
        "name": "codexfix_doctor",
        "description": (
            "只读扫描本地 Codex 会话日志，判断定时任务/heartbeat/跨线程消息失败是否由"
            "「注入项缺 call_id」引起。返回孤立工具输出项统计、真实失败的会话列表和结论。"
            "在用户抱怨 Codex 定时任务在 DeepSeek 等严格上游上报 400 时先调用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_days": {"type": "integer",
                               "description": "只看最近 N 天修改过的会话，默认全部"},
                "limit": {"type": "integer",
                          "description": "最多扫描最近 N 个会话，默认全部"},
            },
            "required": [],
        },
    },
    {
        "name": "codexfix_status",
        "description": (
            "查询本机 codexfix 中间件是否在运行（读 /healthz），返回版本、上游地址、"
            "改写计数。用于确认修复是否已生效或服务是否掉线。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"port": {"type": "integer", "description": "默认 18317"}},
            "required": [],
        },
    },
    {
        "name": "codexfix_probe",
        "description": (
            "向指定的 Responses 上游发一条最小化的畸形注入项，判断它是否严格（会因为缺 "
            "call_id 拒绝请求）。token 会自动从 ~/.codex/config.toml 里匹配该上游的 "
            "experimental_bearer_token，不要把密钥写进对话。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "上游根地址，如 http://host:port 或 .../v1"},
                "model": {"type": "string", "description": "默认 deepseek-flash"},
                "token": {"type": "string", "description": "一般留空，自动从 config.toml 读取"},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {
    "codexfix_doctor": tool_doctor,
    "codexfix_status": tool_status,
    "codexfix_probe": tool_probe,
}


# ---------------------------------------------------------------------------
# protocol loop
# ---------------------------------------------------------------------------


def handle(request):
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "codexfix 诊断工具：先用 codexfix_doctor 判断失败原因，"
                    "再用 codexfix_probe 确认上游是否严格，最后用 codexfix_status 检查中间件状态。"
                ),
            },
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return

    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            text, is_error = "未知工具：%s" % name, True
        else:
            try:
                text, is_error = handler(arguments)
            except Exception as exc:  # never crash the server on a tool error
                log("tool %s failed: %r" % (name, exc))
                text, is_error = "工具执行失败：%r" % (exc,), True
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        })
        return

    if request_id is not None:
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method},
        })


def main():
    log("started (pid=%d)" % os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            log("ignoring non-JSON input")
            continue
        if not isinstance(request, dict):
            continue
        try:
            handle(request)
        except Exception as exc:
            log("handler error: %r" % (exc,))
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
