#!/usr/bin/env python3
"""codexfix — make Codex Desktop automations work on DeepSeek (and other strict Responses providers).

Background
----------
Codex (Desktop and CLI) delivers injected input — automations (heartbeat/cron),
subagent reports, cross-thread messages — as a standalone ``function_call_output``
item that carries ``id``, ``name``, ``namespace`` and ``output`` but **no
``call_id``**, and no matching ``function_call``.

The OpenAI Responses schema requires ``call_id`` on every tool-output item, so
strict implementations reject the whole request before inference:

    Failed to deserialize the JSON body into the target type:
    input: missing field `call_id` at line 1 column N
    Invalid Value: 'input.call_id'. Function call output requires call_id.
    No tool call found for tool output with call_id ...

DeepSeek's ``/responses`` is one such strict implementation, so scheduled tasks
die instantly there while manual chats keep working. Lenient gateways accept the
malformed item, which is why the bug only shows up on some providers.

Note that the intuitive repair — pairing the orphan with a synthetic
``function_call`` — does **not** work on DeepSeek: its thinking mode also requires
the original ``reasoning_text`` (``The `reasoning_text` in the thinking mode must
be passed back to the API.``). Rewriting the item into a plain message is the
approach that works.

What this tool does
-------------------
``codexfix serve`` runs a local middleware in front of your existing provider
gateway. It rewrites *structurally unresolvable* tool-output items into ordinary
messages so the request validates, and passes everything else through
byte-for-byte (including SSE streaming). Point ``model_providers.<x>.base_url``
at it; nothing else about your provider setup changes.

``codexfix doctor`` scans local Codex session logs and tells you whether this
bug is what broke your automations, without touching anything.

Zero dependencies. Python 3.9+.
"""

from __future__ import annotations

import argparse
import glob
import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.1.0"

TOOL_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output")
CALL_TYPES = ("function_call", "local_shell_call", "custom_tool_call")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18317
DEFAULT_MAX_BODY = 64 * 1024 * 1024
DEFAULT_LOG_MAX_MB = 5
DEFAULT_LOG_KEEP = 3

DROP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# ---------------------------------------------------------------------------
# core rewrite logic
# ---------------------------------------------------------------------------


def _text_of(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _content_parts(output, marker: str):
    """Turn a tool-output payload into message content parts without losing structure."""
    parts = []
    if marker:
        parts.append({"type": "input_text", "text": marker})
    if isinstance(output, str):
        if output:
            parts.append({"type": "input_text", "text": output})
    elif isinstance(output, list):
        for part in output:
            if not isinstance(part, dict):
                parts.append({"type": "input_text", "text": _text_of(part)})
                continue
            kind = part.get("type")
            if kind in ("input_text", "output_text", "text") and isinstance(part.get("text"), str):
                parts.append({"type": "input_text", "text": part["text"]})
            elif kind == "refusal" and isinstance(part.get("refusal"), str):
                parts.append({"type": "input_text", "text": part["refusal"]})
            elif kind == "input_image":
                parts.append(part)
            elif kind == "encrypted_content":
                parts.append({"type": "input_text", "text": "[encrypted content omitted]"})
            else:
                parts.append({"type": "input_text", "text": _text_of(part)})
    elif output is not None:
        parts.append({"type": "input_text", "text": _text_of(output)})
    if not parts:
        parts.append({"type": "input_text", "text": ""})
    return parts


def rewrite_payload(doc, *, role="user", repair_unpaired=False, marker=True, models=None):
    """Return (new_doc, stats). stats = {"unidentified": n, "unpaired": n}."""
    stats = {"unidentified": 0, "unpaired": 0}
    if not isinstance(doc, dict):
        return doc, stats
    items = doc.get("input")
    if not isinstance(items, list) or not items:
        return doc, stats

    model = str(doc.get("model") or "")
    if models and not any(m and m in model for m in models):
        return doc, stats

    call_ids = set()
    for item in items:
        if isinstance(item, dict) and item.get("type") in CALL_TYPES and item.get("call_id"):
            call_ids.add(item["call_id"])

    out = []
    changed = False
    for item in items:
        if isinstance(item, dict) and item.get("type") in TOOL_OUTPUT_TYPES:
            call_id = item.get("call_id")
            reason = None
            if not call_id:
                reason = "unidentified"
            elif repair_unpaired and call_id not in call_ids:
                reason = "unpaired"
            if reason:
                name = item.get("name") or "unknown tool"
                prefix = "[tool output: %s]" % name
                out.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": _content_parts(item.get("output"), prefix if marker else ""),
                    }
                )
                stats[reason] += 1
                changed = True
                continue
        out.append(item)

    if not changed:
        return doc, stats
    new_doc = dict(doc)
    new_doc["input"] = out
    return new_doc, stats


def rewrite_body_bytes(body: bytes, *, role, repair_unpaired, marker, models):
    """Return (body, stats). Preserves the original bytes unless a rewrite happened."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        return body, {"unidentified": 0, "unpaired": 0, "parsed": False}
    new_doc, stats = rewrite_payload(
        doc, role=role, repair_unpaired=repair_unpaired, marker=marker, models=models
    )
    stats["parsed"] = True
    if stats["unidentified"] or stats["unpaired"]:
        return json.dumps(new_doc, ensure_ascii=False).encode("utf-8"), stats
    return body, stats


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


class Logger:
    def __init__(self, path, max_mb=DEFAULT_LOG_MAX_MB, keep=DEFAULT_LOG_KEEP, echo=True):
        self.path = path
        self.max_bytes = max_mb * 1024 * 1024
        self.keep = keep
        self.echo = echo

    def _rotate(self):
        try:
            if self.max_bytes <= 0 or not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < self.max_bytes:
                return
            for index in range(self.keep - 1, 0, -1):
                src = "%s.%d" % (self.path, index)
                dst = "%s.%d" % (self.path, index + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(self.path, self.path + ".1")
        except Exception:
            pass

    def __call__(self, message):
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._rotate()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
        if self.echo:
            sys.stderr.write(line)
            sys.stderr.flush()


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self):
        self.started = time.time()
        self.requests = 0
        self.rewritten_items = 0
        self.rewritten_requests = 0
        self.upstream_errors = 0


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn()`` just to fill in
    ``server_name``. On machines where reverse DNS stalls (or when run from
    launchd/systemd with a different resolver context) that call can hang for
    tens of seconds before the socket ever starts listening, which looks like a
    dead service. We do not need the FQDN, so bind directly.
    """

    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codexfix/%s" % __version__

    # injected by the server
    options = None
    upstream_host = None
    upstream_port = None
    upstream_tls = False
    logger = None
    stats = None

    def log_message(self, fmt, *args):
        return

    # -- helpers ----------------------------------------------------------

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length:
            try:
                size = int(length)
            except ValueError:
                return b""
            if size > self.options.max_body:
                raise ValueError("request body too large: %d" % size)
            return self.rfile.read(size)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                total += size
                if total > self.options.max_body:
                    raise ValueError("request body too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _health(self):
        body = json.dumps(
            {
                "ok": True,
                "version": __version__,
                "pid": os.getpid(),
                "uptime_s": round(time.time() - self.stats.started, 1),
                "upstream": "%s://%s:%d" % (
                    "https" if self.upstream_tls else "http",
                    self.upstream_host,
                    self.upstream_port,
                ),
                "requests": self.stats.requests,
                "rewritten_requests": self.stats.rewritten_requests,
                "rewritten_items": self.stats.rewritten_items,
                "upstream_errors": self.stats.upstream_errors,
                "role": self.options.role,
                "repair_unpaired": self.options.repair_unpaired,
                "models": self.options.models,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        if self.path.rstrip("/").endswith("/healthz") or self.path == "/healthz":
            self._health()
            return

        started = time.time()
        self.stats.requests += 1
        try:
            body = self._read_body()
        except ValueError as exc:
            self.send_error(413, str(exc))
            return

        model = ""
        stats = {"unidentified": 0, "unpaired": 0}
        content_type = (self.headers.get("Content-Type") or "").lower()
        if body and "json" in content_type:
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    model = str(parsed.get("model") or "")
            except Exception:
                model = ""
            body, stats = rewrite_body_bytes(
                body,
                role=self.options.role,
                repair_unpaired=self.options.repair_unpaired,
                marker=not self.options.no_marker,
                models=self.options.models,
            )
            if not model:
                try:
                    model = str((json.loads(body.decode("utf-8")) or {}).get("model") or "")
                except Exception:
                    model = ""

        rewritten = stats.get("unidentified", 0) + stats.get("unpaired", 0)
        if rewritten:
            self.stats.rewritten_requests += 1
            self.stats.rewritten_items += rewritten

        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_HEADERS}
        send_body = None
        if body or self.command not in ("GET", "HEAD", "DELETE", "OPTIONS"):
            send_body = body

        if self.upstream_tls:
            connection = http.client.HTTPSConnection(
                self.upstream_host, self.upstream_port, timeout=self.options.timeout
            )
        else:
            connection = http.client.HTTPConnection(
                self.upstream_host, self.upstream_port, timeout=self.options.timeout
            )

        try:
            connection.request(self.command, self.path, body=send_body, headers=headers)
            response = connection.getresponse()
        except Exception as exc:
            self.stats.upstream_errors += 1
            self.logger("ERROR %s %s model=%s upstream failed: %r" % (self.command, self.path, model or "-", exc))
            try:
                self.send_error(502, "upstream unreachable")
            except Exception:
                pass
            connection.close()
            return

        upstream_length = response.getheader("Content-Length")
        try:
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in DROP_HEADERS:
                    continue
                self.send_header(key, value)
            if upstream_length is not None:
                self.send_header("Content-Length", upstream_length)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            sent = 0
            if self.command != "HEAD":
                if upstream_length is not None:
                    remaining = int(upstream_length)
                    while remaining > 0:
                        chunk = response.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        sent += len(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        sent += len(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

        if not self.options.quiet:
            self.logger(
                "%s %s model=%s rewritten=%d status=%s bytes=%d %.0fms"
                % (
                    self.command,
                    self.path,
                    model or "-",
                    rewritten,
                    response.status,
                    sent,
                    (time.time() - started) * 1000,
                )
            )

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_HEAD = _proxy
    do_OPTIONS = _proxy


def parse_upstream(url):
    value = url.strip().rstrip("/")
    tls = value.startswith("https://")
    rest = re.sub(r"^https?://", "", value)
    hostport = rest.split("/")[0]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        return host, int(port), tls
    return hostport, 443 if tls else 80, tls


def cmd_serve(options):
    host, port, tls = parse_upstream(options.upstream)
    logger = Logger(options.log, options.log_max_mb, options.log_keep, echo=not options.quiet)

    class BoundHandler(Handler):
        pass

    BoundHandler.options = options
    BoundHandler.upstream_host = host
    BoundHandler.upstream_port = port
    BoundHandler.upstream_tls = tls
    BoundHandler.logger = logger
    BoundHandler.stats = Stats()

    server = Server((options.host, options.port), BoundHandler)

    logger(
        "serve %s:%d -> %s://%s:%d role=%s models=%s repair_unpaired=%s (pid=%d)"
        % (
            options.host,
            options.port,
            "https" if tls else "http",
            host,
            port,
            options.role,
            ",".join(options.models) if options.models else "all",
            options.repair_unpaired,
            os.getpid(),
        )
    )

    def shutdown(_signum, _frame):
        logger("received signal, shutting down")
        # serve_forever() runs on the main thread, and shutdown() must be called
        # from a different thread or it deadlocks and the process never exits.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except Exception:
            pass

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _session_files(home, since_days=0):
    roots = [
        os.path.join(home, "sessions"),
        os.path.join(home, "archived_sessions"),
    ]
    files = []
    for root in roots:
        files.extend(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
    if since_days:
        cutoff = time.time() - since_days * 86400
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
    return sorted(set(files))


def scan_sessions(home, since_days=0, limit=None):
    files = _session_files(home, since_days)
    if limit:
        files = files[-limit:]

    report = {
        "sessions_scanned": len(files),
        "orphans": [],           # (file, line_no, type, name, reason)
        "error_sessions": [],    # (file, first timestamp)
        "by_name": {},
        "by_reason": {},
    }
    for path in files:
        items = []
        raw_records = []
        errored = None
        try:
            with open(path, "r", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    # A genuine failure is a turn that ended with the upstream error,
                    # not text that merely quotes the error (tool output, transcripts).
                    if errored is None and (
                        "missing field `call_id`" in line
                        or "requires call_id" in line
                        or "No tool call found for tool output" in line
                    ):
                        if '"event_msg"' in line and '"task_complete"' in line:
                            try:
                                record = json.loads(line)
                            except Exception:
                                record = {}
                            payload = record.get("payload") or {}
                            if payload.get("type") == "task_complete" and payload.get("error"):
                                errored = line_no
                    if (
                        '"function_call_output"' not in line
                        and '"custom_tool_call_output"' not in line
                        and '"function_call"' not in line
                        and '"custom_tool_call"' not in line
                        and '"local_shell_call"' not in line
                    ):
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    payload = record.get("payload") or {}
                    kind = payload.get("type")
                    if kind in TOOL_OUTPUT_TYPES or kind == "function_call" or kind in CALL_TYPES:
                        items.append((line_no, payload))
                        raw_records.append((line_no, record))
        except Exception:
            continue

        call_ids = {p.get("call_id") for _, p in items if p.get("call_id")}
        for line_no, payload in items:
            if payload.get("type") not in TOOL_OUTPUT_TYPES:
                continue
            call_id = payload.get("call_id")
            if call_id and call_id in call_ids:
                continue
            reason = "unidentified" if not call_id else "unpaired"
            name = payload.get("name") or "-"
            report["orphans"].append((path, line_no, payload.get("type"), name, reason))
            report["by_name"][name] = report["by_name"].get(name, 0) + 1
            report["by_reason"][reason] = report["by_reason"].get(reason, 0) + 1
        if errored is not None:
            report["error_sessions"].append((path, errored))
    return report


def cmd_doctor(options):
    home = os.path.expanduser(options.home)
    report = scan_sessions(home, options.since, options.limit)

    if options.json:
        print(
            json.dumps(
                {
                    "sessions_scanned": report["sessions_scanned"],
                    "orphan_count": len(report["orphans"]),
                    "by_name": report["by_name"],
                    "by_reason": report["by_reason"],
                    "error_sessions": [
                        {"file": f, "line": n} for f, n in report["error_sessions"]
                    ],
                    "orphans": [
                        {"file": f, "line": n, "type": t, "name": name, "reason": r}
                        for f, n, t, name, r in report["orphans"][:200]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("codexfix doctor")
    print("  CODEX_HOME : %s" % home)
    print("  扫描会话数 : %d" % report["sessions_scanned"])
    print()

    orphans = report["orphans"]
    errors = report["error_sessions"]
    print("  孤立工具输出项 : %d 条，分布在 %d 个会话" % (len(orphans), len({o[0] for o in orphans})))
    if report["by_name"]:
        for name, count in sorted(report["by_name"].items(), key=lambda kv: -kv[1]):
            print("      %-32s %d" % (name, count))
    if report["by_reason"]:
        for reason, count in sorted(report["by_reason"].items(), key=lambda kv: -kv[1]):
            label = "无 call_id（任何上游都无法解析）" if reason == "unidentified" else "call_id 悬空（有状态上游可能自行解析）"
            print("      %-32s %d" % (label, count))
    print()
    print("  因缺 call_id 而失败的轮次 : %d 个会话" % len(errors))
    for path, line_no in errors[:10]:
        print("      %s:%d" % (os.path.relpath(path, home), line_no))
    if len(errors) > 10:
        print("      … 其余 %d 个" % (len(errors) - 10))
    print()

    if errors and orphans:
        print("  结论：命中本工具要修的问题（自动化/注入请求被严格 Responses 上游拒绝）。")
        print("  下一步：codexfix serve 起本地中间件，并把 model_providers.<名称>.base_url 指过去。")
    elif orphans and not errors:
        print("  结论：本地存在孤立工具输出项，但没有观察到它导致失败——当前上游可能较宽松。")
        print("  下一步：换用严格上游时再启用中间件。")
    elif errors and not orphans:
        print("  结论：有请求因缺 call_id 失败，但本地记录里没找到孤立输出项，可能是其它构造路径。")
        print("  下一步：把该会话文件路径附在 issue 里进一步排查。")
    else:
        print("  结论：没有发现该问题。")
    return 0


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def cmd_probe(options):
    payload = {
        "model": options.model,
        "max_output_tokens": 8,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "function_call_output",
                "id": "fco_probe",
                "name": "automation_update",
                "namespace": "codex_app",
                "output": "probe",
            },
        ],
    }
    url = options.url.rstrip("/")
    if not url.endswith("/responses"):
        url = url + ("/responses" if url.endswith("/v1") else "/v1/responses")
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % options.token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=options.timeout) as response:
            print("HTTP %d — 上游接受这条畸形注入项（宽松实现）" % response.status)
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print("HTTP %d — %s" % (exc.code, body[:400]))
        if exc.code == 404:
            print("提示：404 通常表示地址写错了。请给到 base 地址（例如 http://host:8317 或 http://host:8317/v1），")
            print("      本工具会自动补成 <base>/v1/responses 或 <base>/responses。")
            return 2
        if exc.code >= 500:
            print("上游暂时不可用（HTTP %d），这次结果不构成对严格性的判断，稍后重试即可。" % exc.code)
            return 2
        if "call_id" in body:
            print()
            print("判定：严格实现，会被 Codex 的注入项卡住。用 codexfix serve + base_url 指向它。")
        return 1
    except Exception as exc:
        print("请求失败：%r" % (exc,))
        return 2


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------


def _script_path():
    return os.path.abspath(__file__)


def cmd_install(options):
    label = "com.codexfix.shim"
    # macOS TCC protects ~/Documents, ~/Desktop and ~/Downloads: a launchd agent
    # that tries to read its own script from there can block forever on open().
    # Always install a copy under a non-protected path and run that.
    install_dir = os.path.expanduser(options.install_dir)
    os.makedirs(install_dir, exist_ok=True)
    installed_script = os.path.join(install_dir, os.path.basename(_script_path()))
    shutil.copy2(_script_path(), installed_script)
    print("已安装脚本副本：%s" % installed_script)

    def serve_args():
        args = [
            sys.executable,
            installed_script,
            "serve",
            "--host",
            options.host,
            "--port",
            str(options.port),
            "--upstream",
            options.upstream,
            "--role",
            options.role,
        ]
        if options.models:
            args += ["--models", ",".join(options.models)]
        if options.repair_unpaired:
            args.append("--repair-unpaired")
        return args

    if platform.system() == "Darwin":
        plist_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(plist_dir, exist_ok=True)
        plist_path = os.path.join(plist_dir, label + ".plist")
        args = serve_args()
        program = "\n".join("        <string>%s</string>" % a for a in args)
        log = os.path.expanduser(options.log)
        plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <key>ProgramArguments</key>
    <array>
%s
    </array>
    <key>WorkingDirectory</key>
    <string>%s</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>%s</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
</dict>
</plist>
""" % (label, program, install_dir, os.path.expanduser("~"), log + ".out", log + ".err")
        with open(plist_path, "w", encoding="utf-8") as handle:
            handle.write(plist)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
        print("已写入 %s" % plist_path)

        # bootout is asynchronous: bootstrapping too early fails with a launchd
        # error, so wait until the old instance is really gone.
        deadline = time.time() + 15
        while time.time() < deadline:
            probe = subprocess.run(
                ["launchctl", "print", "gui/%d/%s" % (uid, label)], capture_output=True
            )
            if probe.returncode != 0:
                break
            time.sleep(0.5)

        last_error = ""
        for attempt in range(3):
            result = subprocess.run(
                ["launchctl", "bootstrap", "gui/%d" % uid, plist_path],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            last_error = (result.stderr or result.stdout or "").strip()
            time.sleep(1.5)
        else:
            print("bootstrap 失败：%s" % last_error)
            return 1

        # Verify the service actually answers, not just that launchd accepted it.
        healthy = False
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                connection = http.client.HTTPConnection(options.host, options.port, timeout=2)
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        print("检查状态：launchctl print gui/%d/%s" % (uid, label))
        print("日志：%s（轮转文件 %s.1 …）" % (log, log))
        if healthy:
            print("服务已启动并通过 /healthz 自检：http://%s:%d/healthz" % (options.host, options.port))
            print("下一步：把 model_providers.<名称>.base_url 改为 http://%s:%d/v1" % (options.host, options.port))
            return 0
        print("服务已注册，但 /healthz 未在 15 秒内响应，请检查上面两个日志文件。")
        return 1

    if platform.system() == "Linux":
        unit_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(unit_dir, exist_ok=True)
        unit_path = os.path.join(unit_dir, "codexfix.service")
        unit = "[Unit]\nDescription=codexfix strict Responses middleware\nAfter=network.target\n\n[Service]\nWorkingDirectory=%s\nExecStart=%s\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n" % (
            install_dir,
            " ".join(serve_args()),
        )
        with open(unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "codexfix.service"], capture_output=True)
        print("已写入 %s 并启动。" % unit_path)
        return 0

    print("未识别的平台：%s。请手动运行：%s" % (platform.system(), " ".join(serve_args())))
    return 1


def cmd_uninstall(_options):
    label = "com.codexfix.shim"
    if platform.system() == "Darwin":
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
        path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
        if os.path.exists(path):
            trash = os.path.expanduser("~/.Trash")
            os.makedirs(trash, exist_ok=True)
            shutil.move(path, os.path.join(trash, os.path.basename(path)))
            print("已卸载，plist 已移到废纸篓：%s" % path)
        return 0
    if platform.system() == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", "codexfix.service"], capture_output=True)
        print("已停止 codexfix.service")
        return 0
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_serve_arguments(parser):
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    # No default on purpose: the gateway address is deployment-specific, and a
    # hardcoded fallback would (a) leak a real host if this file is published and
    # (b) silently send traffic somewhere unexpected. Pass it explicitly or via
    # CODEXFIX_UPSTREAM.
    env_upstream = os.environ.get("CODEXFIX_UPSTREAM")
    parser.add_argument(
        "--upstream",
        required=env_upstream is None,
        default=env_upstream,
        help="你的供应商网关根地址，例如 http://gateway.example:8080（或设置 CODEXFIX_UPSTREAM）",
    )
    parser.add_argument("--role", choices=("user", "developer"), default=os.environ.get("CODEXFIX_ROLE", "user"))
    parser.add_argument("--models", default=None, help="逗号分隔的子串过滤；默认全部模型")
    parser.add_argument("--repair-unpaired", action="store_true")
    parser.add_argument("--no-marker", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    parser.add_argument("--log", default=os.environ.get("CODEXFIX_LOG", "~/.codexfix/codexfix.log"))
    parser.add_argument("--install-dir", default=os.environ.get("CODEXFIX_INSTALL_DIR", "~/.codexfix"))
    parser.add_argument("--log-max-mb", type=int, default=DEFAULT_LOG_MAX_MB)
    parser.add_argument("--log-keep", type=int, default=DEFAULT_LOG_KEEP)
    parser.add_argument("--quiet", action="store_true")


def normalize(options):
    if hasattr(options, "log"):
        options.log = os.path.expanduser(options.log)
    if hasattr(options, "install_dir"):
        options.install_dir = os.path.expanduser(options.install_dir)
    if hasattr(options, "models"):
        options.models = (
            [m.strip() for m in options.models.split(",") if m.strip()] if options.models else None
        )
    return options


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codexfix", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version="codexfix %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="运行本地中间件")
    add_serve_arguments(serve)
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="扫描本地会话，判断是否命中缺 call_id 的问题")
    doctor.add_argument("--home", default=os.environ.get("CODEX_HOME", "~/.codex"))
    doctor.add_argument("--since", type=int, default=0, help="只看最近 N 天修改过的会话")
    doctor.add_argument("--limit", type=int, default=0, help="最多扫描最近 N 个会话")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser("probe", help="向上游发一条畸形注入项，判断它是否严格")
    probe.add_argument("--url", required=True)
    probe.add_argument("--token", default=os.environ.get("CODEXFIX_TOKEN", ""))
    probe.add_argument("--model", default="deepseek-flash")
    probe.add_argument("--timeout", type=float, default=30)
    probe.set_defaults(func=cmd_probe)

    install = sub.add_parser("install", help="安装为常驻服务（launchd / systemd user unit）")
    add_serve_arguments(install)
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="卸载常驻服务")
    uninstall.set_defaults(func=cmd_uninstall)

    options = normalize(parser.parse_args(argv))
    return options.func(options)


if __name__ == "__main__":
    sys.exit(main())
