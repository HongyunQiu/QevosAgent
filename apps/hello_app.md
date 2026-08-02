---
name: 普通 App 示例
icon: 🧪
description: 点「运行」看看 QevosAgent 的「普通 App」是什么 —— 一段直接执行的脚本，输出打在控制台（不调用 LLM、不联网）
runtime: python
timeout: 30
enabled: true
---
# 这是一个「普通 App」示例：一段直接执行的脚本。
# 它只读取本机的几个目录做统计并打印，不改动任何文件、不联网、不调用 LLM。

import json
import os
import platform
import sys
import unicodedata
from pathlib import Path


def line(k, v):
    # CJK 是双宽字符，按显示宽度补空格，列才对得齐。
    width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in k)
    print(f"  {k}{' ' * max(1, 14 - width)}{v}")


print("=" * 56)
print("🧪 普通 App 示例")
print("=" * 56)
print()
print("QevosAgent 的 App 分两种，都在「Apps」页里以卡片出现：")
print()
print("  • 普通 App（就是本卡）— runtime 写 python / shell / powershell。")
print("    点「运行」就直接跑这段脚本，stdout/stderr 实时打到控制台。")
print("    适合：定时统计、批量整理、调外部命令这类一次性任务。")
print()
print("  • UI App — runtime 写 web，正文是一整页 HTML。")
print("    点卡片会在窗口里打开一个面板，能读写自己的数据目录。")
print("    见「📦 内置 App 示例」那张卡。")
print()
print("两者都是 apps/<id>.md 一个文件：YAML frontmatter 声明元信息，")
print("正文就是脚本或页面。在「Apps」页里能直接编辑。")
print()

print("-" * 56)
print("本机实况（脚本真的在你的电脑上跑起来了）：")
workdir = Path.cwd()
line("平台", f"{platform.system()} {platform.release()}")
line("Python", sys.version.split()[0])
line("工作目录", workdir)

for name, pattern in (("apps", "*.md"), ("SKILLS", "*.md"), ("crons", "*.md")):
    d = Path(os.environ.get(f"{name.upper()}_DIR") or workdir / name)
    n = len(list(d.glob(pattern))) if d.is_dir() else 0
    line(name, f"{n} 个" if d.is_dir() else "（未创建）")

runs = Path(os.environ.get("RUNS_DIR") or workdir / "runs")
line("runs", f"{len(list(runs.iterdir()))} 条记录" if runs.is_dir() else "（还没有运行记录）")

# 调用方可以传结构化参数，平台经 QEVOS_RUN_ARGS 环境变量交给脚本。
raw = os.environ.get("QEVOS_RUN_ARGS")
if raw:
    try:
        line("传入参数", json.dumps(json.loads(raw), ensure_ascii=False))
    except ValueError:
        line("传入参数", raw)

print("-" * 56)
print("✓ 运行结束。要写自己的 App，复制这张卡改改就行。")
