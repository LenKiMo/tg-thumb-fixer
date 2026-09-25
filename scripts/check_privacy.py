#!/usr/bin/env python3
"""仓库隐私门禁：扫描 git 跟踪文件，检出本机绝对路径/用户目录痕迹。

用法:  python scripts/check_privacy.py [仓库根目录]   默认当前目录
命中 → 逐条打印 文件:行号:[类型] 内容 并退出码 1；干净 → 打印 ✓ 退出码 0。
接入:  CI 加一步 `python scripts/check_privacy.py`；
      本地 pre-commit: `git config core.hooksPath scripts/hooks`。

规则刻意简单粗暴：盘符/家目录/用户目录 ANY 出现即失败；
示例占位符也得守规则 —— 写 目录A\\套图一，不写盘符路径。
"""

import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATTERNS = [
    (r"\b[A-Za-z]:[\\/]", "Windows 盘符绝对路径"),
    (r"\b/home/\S+", "Linux 家目录"),
    (r"\b/Users/\S+", "macOS 用户目录"),
    (r"%(?:USERPROFILE|APPDATA|LOCALAPPDATA|HOMEDRIVE|HOMEPATH)%", "Windows 用户环境变量"),
]


def tracked_files(root: str) -> list:
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [f for f in out.split(b"\0") if f]


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    hits = []
    for raw in tracked_files(root):
        path = raw.decode("utf-8", "replace")
        # 门禁脚本自身含有规则字面量（/home/、/Users/），跳过以免自伤
        if os.path.basename(path) == "check_privacy.py":
            continue
        try:
            with open(os.path.join(root, path), "rb") as fh:
                for lineno, line in enumerate(fh, 1):
                    text = line.decode("utf-8", "replace")
                    for pat, label in PATTERNS:
                        if re.search(pat, text):
                            hits.append(f"{path}:{lineno}  [{label}] {text.rstrip()}")
                            break
        except OSError:
            pass  # 子模块/缺失文件跳过
    if hits:
        print("隐私检查失败 — 跟踪文件中存在本机路径痕迹：")
        for h in hits:
            print("  " + h)
        return 1
    print("✓ 隐私检查通过：未发现本机绝对路径痕迹")
    return 0


if __name__ == "__main__":
    sys.exit(main())
