#!/usr/bin/env python3
"""Fail if deployment-specific or secret-looking values leak into the repository.

Run from the repo root:  python3 scripts/scan_sensitive.py
Exit code 1 means something must be removed before publishing.

This exists because codexfix was extracted from a real deployment: it is very easy to
commit the gateway address or a home-directory path without noticing.
"""

import os
import re
import sys

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}
SKIP_EXT = {".pyc", ".png", ".jpg", ".gif", ".pdf", ".ico", ".woff", ".woff2"}

ALLOW_PRIVATE = {
    "127.0.0.1",   # loopback, used for the middleware's own examples
    "0.0.0.0",
}

# Documented placeholders are fine; real account names are not.
ALLOW_USERNAMES = {"you", "user", "yourname", "your-user", "example", "me", "alice", "bob"}

PATTERNS = [
    (
        "私网地址 (RFC1918)",
        re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"),
        "换成 YOUR-GATEWAY / gateway.example",
    ),
    (
        "绝对家目录路径",
        re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+"),
        "换成 ~ 或 /path/to/...",
    ),
    (
        "Windows 家目录路径",
        re.compile(r"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9._-]+"),
        "换成 %USERPROFILE%",
    ),
    (
        "疑似 API key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
        "立即轮换该密钥并删除",
    ),
    (
        "内联 Bearer token",
        re.compile(r"Bearer\s+[A-Za-z0-9_\-]{16,}"),
        "改为从环境变量读取",
    ),
    (
        "私钥块",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "立即轮换并删除",
    ),
]


def scan(root):
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            path = os.path.join(dirpath, name)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for label, rx, advice in PATTERNS:
                for match in rx.finditer(text):
                    value = match.group(0)
                    if value in ALLOW_PRIVATE:
                        continue
                    if label == "绝对家目录路径" and value.rsplit("/", 1)[-1] in ALLOW_USERNAMES:
                        continue
                    if label == "Windows 家目录路径" and re.sub(r".*\\\\", "", value) in ALLOW_USERNAMES:
                        continue
                    line = text[: match.start()].count("\n") + 1
                    findings.append((path, line, label, value, advice))
    return findings


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    findings = scan(root)
    if not findings:
        print("OK：未发现敏感信息（扫描根目录 %s）" % root)
        return 0
    print("发现 %d 处需要处理的内容：" % len(findings))
    for path, line, label, value, advice in findings:
        print("  %s:%d  [%s] %s  → %s" % (path, line, label, value, advice))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
