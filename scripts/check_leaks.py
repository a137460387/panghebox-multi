#!/usr/bin/env python3
"""提交前自检:扫描仓库里是否混入了真实凭据。

用法::

    python scripts/check_leaks.py            # 扫描仓库
    python scripts/check_leaks.py --staged   # 只扫 git 暂存区

检出项:
* JWT(含胖盒 token 的特征:``acceessKey`` 字段)
* UUID 形态的 accessKey / machine_id
* 中国大陆手机号
* 疑似账号档案 JSON(含 flutter.token / flutter.uid)

退出码:0 = 干净,1 = 发现可疑内容。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 扫描时跳过的目录名
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", "build", "dist", ".idea", ".vscode",
    "accounts",  # 账号档案目录本身就是凭据,不该被扫描也不该入库
}

# 允许出现的文本文件后缀
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".sh", ".bat", ".ps1", ".html", ".js", ".ts", ".css", "",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        # 真实 token 一定是三段式 base64url,且头段是 eyJ 开始的 {"alg":...
        "JWT token (三段式)",
        re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    ),
    (
        # 胖盒 token 的 payload 含服务端拼错的 acceessKey 字段,
        # 但必须是放在 json 值里的形态,而不是文档中的反引号引用
        "JWT payload 含 acceessKey 值",
        re.compile(r'["\']acceessKey["\']\s*:\s*["\'][0-9a-fA-F-]{8,}'),
    ),
    (
        "prefs 账号字段(带真实值)",
        re.compile(
            r'"flutter\.(?:token|last_login_access_key)"\s*:\s*"'
            r"(?!FAKE|fake|xxx|test)[A-Za-z0-9_.-]{20,}\""
        ),
    ),
    (
        # 测试里用的 13800000001 这类连号是明显的占位值,排除掉
        "中国大陆手机号",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    (
        # 只标记「真实」UUID:全同字符的显然是占位
        "accessKey/machine_id (UUID)",
        re.compile(
            r"(?<![0-9a-fA-F-])(?!([0-9a-f])\1{7}-)"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
            r"(?![0-9a-fA-F-])"
        ),
    ),
    (
        "内网/公网 IP 带端口",
        re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{4,5}\b"),
    ),
]

# 这些文件/内容属于文档说明,允许出现示例词
ALLOWLIST_LINE_HINTS = (
    "示例", "example", "FAKE", "fake", "xxxx", "***", "占位",
)

# 占位手机号特征:后 6 位以上全是同一数字或连号(如 13800000001)
_PLACEHOLDER_PHONE = re.compile(r"1[3-9](\d)\1{5,}\d?$|1[3-9]0{6,}")


def _is_placeholder_phone(line: str) -> bool:
    """判断该行里的手机号是不是明显的占位值。"""
    for m in re.finditer(r"(?<!\d)(1[3-9]\d{9})(?!\d)", line):
        num = m.group(1)
        # 中段全 0(13800000001 这类)视为占位
        if num[3:9].strip("0") == "":
            return True
    return False


def _git_tracked_files() -> list[Path] | None:
    """返回 git 跟踪的文件列表;不是 git 仓库则返回 None。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


def _git_staged_files() -> list[Path] | None:
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [Path(line) for line in out.stdout.splitlines() if line.strip()]


def walk_files(root: Path) -> list[Path]:
    """遍历目录下所有可扫描的文本文件。"""
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        out.append(p)
    return out


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描单个文件,返回 [(行号, 规则名, 该行内容)]。"""
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hits

    for lineno, line in enumerate(text.splitlines(), start=1):
        if any(hint in line for hint in ALLOWLIST_LINE_HINTS):
            continue
        for name, pat in PATTERNS:
            if not pat.search(line):
                continue
            if name == "中国大陆手机号" and _is_placeholder_phone(line):
                continue
            hits.append((lineno, name, line.strip()[:160]))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描仓库中的凭据泄露")
    ap.add_argument("--staged", action="store_true", help="只扫描 git 暂存区")
    ap.add_argument("--root", default=".", help="仓库根目录")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()

    if args.staged:
        files = _git_staged_files()
        if files is None:
            print("不是 git 仓库或 git 不可用,改为全目录扫描。", file=sys.stderr)
            files = walk_files(root)
    else:
        files = _git_tracked_files()
        if files is None:
            files = walk_files(root)

    total = 0
    for f in files:
        p = f if f.is_absolute() else root / f
        if not p.exists() or any(part in SKIP_DIRS for part in p.parts):
            continue
        hits = scan_file(p)
        if hits:
            rel = p.relative_to(root) if p.is_relative_to(root) else p
            print(f"\n{p if False else rel}")
            for lineno, name, line in hits:
                print(f"  {lineno}: [{name}] {line}")
                total += 1

    print()
    if total:
        print(f"发现 {total} 处可疑内容,请确认全部是示例/占位值后再提交。")
        print("提示:真实凭据绝不应入库;账号档案请放 accounts/(已在 .gitignore)。")
        return 1

    print(f"扫描完成:{len(files)} 个文件,未发现凭据泄露。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
