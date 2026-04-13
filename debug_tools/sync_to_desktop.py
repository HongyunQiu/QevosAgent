#!/usr/bin/env python3
"""
sync_to_desktop.py
──────────────────
将 simpleAgent 源码同步到已安装的桌面应用的 vendor/app/ 目录，
用于在不重新打包的情况下快速测试代码改动。

用法：
    python debug_tools/sync_to_desktop.py
    python debug_tools/sync_to_desktop.py --target "D:\\other\\path\\vendor\\app"
    python debug_tools/sync_to_desktop.py --dry-run   # 只显示将要做什么，不实际复制
"""

import argparse
import shutil
import sys
from pathlib import Path

# ── 默认路径 ───────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = Path(r"D:\simpleagent-desktop\resources\app\vendor\app")

# 需要同步的条目：(源相对路径, 目标相对路径)
SYNC_MAP = [
    ("agent",       "agent"),
    ("dashboard",   "dashboard"),
    ("run_goal.py", "run_goal.py"),
]

# ── 工具函数 ───────────────────────────────────────────────────────────────

def fmt(path: Path) -> str:
    """短路径显示（去掉公共前缀让输出更易读）"""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def sync_item(src: Path, dest: Path, dry_run: bool) -> int:
    """同步单个文件或目录，返回复制的文件数。"""
    if not src.exists():
        print(f"  [跳过] 源不存在: {src}")
        return 0

    count = 0
    if src.is_file():
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        count = 1
        print(f"  {'[dry]' if dry_run else '     '} {fmt(src)}  →  {fmt(dest)}")

    else:  # directory
        files = list(src.rglob("*"))
        file_list = [f for f in files if f.is_file()]
        count = len(file_list)
        print(f"  {'[dry]' if dry_run else '     '} {fmt(src)}/  →  {fmt(dest)}/  ({count} 个文件)")
        if not dry_run:
            shutil.copytree(src, dest, dirs_exist_ok=True)

    return count


# ── 主逻辑 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="同步 simpleAgent 源码到桌面应用 vendor/app/")
    parser.add_argument(
        "--target", default=str(DEFAULT_TARGET),
        help=f"目标 vendor/app 目录（默认：{DEFAULT_TARGET}）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅预览，不实际复制",
    )
    args = parser.parse_args()

    target = Path(args.target)
    dry_run: bool = args.dry_run

    print()
    print("simpleAgent → Desktop 同步工具")
    print("═" * 40)
    print(f"  源目录  : {REPO_ROOT}")
    print(f"  目标目录: {target}")
    if dry_run:
        print("  模式    : dry-run（只预览，不复制）")
    print()

    if not target.exists():
        print(f"错误：目标目录不存在\n  {target}")
        print("\n请确认桌面应用已安装，或通过 --target 指定正确路径。")
        sys.exit(1)

    total = 0
    for src_rel, dest_rel in SYNC_MAP:
        src  = REPO_ROOT / src_rel
        dest = target   / dest_rel
        total += sync_item(src, dest, dry_run)

    print()
    if dry_run:
        print(f"预览完成，共 {total} 个文件将被复制（未实际执行）。")
    else:
        print(f"✓ 同步完成，共复制 {total} 个文件。")
        print("  重启桌面应用后生效。")
    print()


if __name__ == "__main__":
    main()
