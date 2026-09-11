#!/usr/bin/env python3
"""Keep the plugin's vendored copy of codexfix.py in sync with the CLI.

The plugin has to be self-contained (it is installed into Codex's plugin cache and
must work without this repository), so it ships its own copy of codexfix.py. That
copy is a build artifact: ``codexfix.py`` in the repo root is the single source of
truth, and this script is what keeps them identical.

    python3 scripts/sync_plugin.py            # copy repo -> plugin
    python3 scripts/sync_plugin.py --check    # fail if they differ (used by CI)
    python3 scripts/sync_plugin.py --dev-copy # also mirror the plugin into ~/plugins
"""

import argparse
import filecmp
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = "codex-deepseek-automation-fix"
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins", PLUGIN_NAME)
SOURCE = os.path.join(REPO_ROOT, "codexfix.py")
VENDORED = os.path.join(PLUGIN_DIR, "mcp", "codexfix.py")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="只检查是否一致，不一致返回 1（CI 用）")
    parser.add_argument("--dev-copy", action="store_true",
                        help="额外把整个插件镜像到 ~/plugins，供本机安装测试")
    args = parser.parse_args(argv)

    if not os.path.exists(SOURCE):
        print("找不到 %s" % SOURCE)
        return 2
    if not os.path.exists(VENDORED):
        print("找不到插件内的副本 %s（插件目录结构不完整？）" % VENDORED)
        return 2

    same = filecmp.cmp(SOURCE, VENDORED, shallow=False)

    if args.check:
        if same:
            print("OK：插件内的 codexfix.py 与仓库主程序一致")
            return 0
        print("不一致：%s 与 %s 不同。" % (SOURCE, VENDORED))
        print("修复：python3 scripts/sync_plugin.py")
        return 1

    if same:
        print("已一致，无需同步")
    else:
        shutil.copy2(SOURCE, VENDORED)
        print("已同步 codexfix.py -> %s" % os.path.relpath(VENDORED, REPO_ROOT))

    if args.dev_copy:
        target = os.path.expanduser(os.path.join("~/plugins", PLUGIN_NAME))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copytree(PLUGIN_DIR, target, dirs_exist_ok=True)
        print("已镜像插件到 %s" % target)
        print("本地重装：codex plugin add %s@personal" % PLUGIN_NAME)

    return 0


if __name__ == "__main__":
    sys.exit(main())
