#!/usr/bin/env python3
"""
压缩功能测试脚本

从 runs/ 目录中加载历史运行数据，模拟到指定迭代轮次后触发压缩，
对比压缩前后的状态变化。

用法：
    python test/test_compress_replay.py --compress-at 80
    python test/test_compress_replay.py --run runs/20260409-234244 --compress-at 50
    python test/test_compress_replay.py --list-runs
"""

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

# 将项目根目录加入 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agent.core.compression import (
    _compact_short_term_messages,
    _maybe_compress_for_context,
    _trim_short_term,
)
from agent.core.llm import (
    LLMBackend,
    _estimate_tokens_heuristic,
    build_context_messages,
    build_system_prompt,
)
from agent.core.types_def import AgentState, ToolSpec, ToolResult


# ── Mock LLM（仅用于 token 估算，不发起真实 API 调用）─────────────────────────

class MockLLM(LLMBackend):
    """纯启发式 token 估算，不调用任何外部 API。"""

    def complete(self, messages: list[dict], system: str) -> str:
        raise NotImplementedError("MockLLM 不支持真实补全")

    def estimate_tokens(self, messages: list[dict], system: str) -> int:
        return _estimate_tokens_heuristic(
            [system] + [m.get("content", "") for m in messages]
        )


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def list_runs(runs_dir: Path) -> list[Path]:
    """返回按时间戳排序（最新在前）的 runs 目录列表。"""
    entries = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and (d / "short_term.jsonl").exists()],
        reverse=True,
    )
    return entries


def load_short_term(run_dir: Path) -> list[dict]:
    """加载 short_term.jsonl，过滤掉 __token__ 元数据行。"""
    msgs = []
    path = run_dir / "short_term.jsonl"
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("role") == "__token__":
                continue
            msgs.append(obj)
    return msgs


def load_meta(run_dir: Path) -> dict:
    """加载 meta.json，容忍 JSON 解析错误（大文件可能被截断）。"""
    path = run_dir / "meta.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # 文件损坏时尝试部分解析
        try:
            text = path.read_text(encoding="utf-8")
            # 只截取前 50KB 防止超大文件
            return json.loads(text[:51200])
        except Exception:
            return {}


def split_into_iterations(messages: list[dict]) -> list[list[dict]]:
    """
    将消息列表按迭代轮次分组。

    约定：每轮 = 1 条 assistant 消息 + 0~N 条随后的 user 消息（工具结果）。
    首条 user 消息（目标）单独作为第 0 轮。
    """
    if not messages:
        return []

    iterations: list[list[dict]] = []
    current: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            # 新的 assistant 消息代表新的一轮开始
            if current:
                iterations.append(current)
            current = [msg]
        else:
            current.append(msg)

    if current:
        iterations.append(current)

    return iterations


# ── 格式化输出 ────────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
YELLOW = "\033[33m"
RED   = "\033[31m"
RESET = "\033[0m"

def header(text: str) -> str:
    line = "─" * 60
    return f"\n{BOLD}{line}\n  {text}\n{line}{RESET}"


def truncate(s: str, max_chars: int = 200) -> str:
    s = s.replace("\n", " ")
    return s[:max_chars] + "…" if len(s) > max_chars else s


def print_state_summary(label: str, state: AgentState, llm: MockLLM, system: str):
    messages = build_context_messages(state)
    token_est = llm.estimate_tokens(messages, system)
    ctx = int(os.environ.get("LLM_CONTEXT_WINDOW", "131072"))

    print(f"\n{CYAN}[{label}]{RESET}")
    print(f"  消息数量    : {len(state.short_term)}")
    print(f"  Token 估算  : {token_est:,} / {ctx:,}  ({token_est/ctx*100:.1f}%)")
    scratchpad = (state.meta.get("scratchpad") or "").strip()
    print(f"  草稿本长度  : {len(scratchpad)} 字符")

    if state.short_term:
        print(f"  消息预览：")
        for i, m in enumerate(state.short_term[:3]):
            role = m.get("role", "?")
            content = truncate(m.get("content", ""), 120)
            print(f"    [{i}] {role}: {content}")
        if len(state.short_term) > 6:
            print(f"    ... ({len(state.short_term) - 6} 条省略) ...")
        for i, m in enumerate(state.short_term[-3:]):
            idx = len(state.short_term) - 3 + i
            role = m.get("role", "?")
            content = truncate(m.get("content", ""), 120)
            print(f"    [{idx}] {role}: {content}")


def print_diff(before: AgentState, after: AgentState, llm: MockLLM, system: str):
    b_msgs = len(before.short_term)
    a_msgs = len(after.short_term)
    b_tok = llm.estimate_tokens(build_context_messages(before), system)
    a_tok = llm.estimate_tokens(build_context_messages(after), system)

    dropped = b_msgs - a_msgs
    tok_saved = b_tok - a_tok

    print(f"\n{GREEN}── 压缩效果 ──{RESET}")
    print(f"  消息数  : {b_msgs} → {a_msgs}  (减少 {dropped} 条)")
    print(f"  Token   : {b_tok:,} → {a_tok:,}  (节省 {tok_saved:,}，{tok_saved/max(b_tok,1)*100:.1f}%)")

    # 查找桥接消息
    for m in after.short_term:
        if "[系统]" in m.get("content", "") and "压缩" in m.get("content", ""):
            print(f"\n  {YELLOW}桥接消息内容：{RESET}")
            print(f"  {textwrap.fill(m['content'], width=70, initial_indent='  ', subsequent_indent='  ')}")
            break


# ── 主逻辑 ───────────────────────────────────────────────────────────────────

def run_test(
    run_dir: Path,
    compress_at: int,
    keep_last: int = 8,
    context_window: Optional[int] = None,
    warn_ratio: float = 0.90,
):
    print(header(f"压缩测试  run={run_dir.name}  compress_at={compress_at}"))

    # 加载数据
    all_messages = load_short_term(run_dir)
    meta = load_meta(run_dir)
    total_msgs = len(all_messages)

    # 分组到迭代
    iterations = split_into_iterations(all_messages)
    total_iters = len(iterations)
    print(f"\n  总消息数  : {total_msgs}")
    print(f"  总迭代数  : {total_iters}")

    if compress_at > total_iters:
        print(f"\n{RED}警告：指定的迭代轮次 {compress_at} 超过总迭代数 {total_iters}，将使用全部消息。{RESET}")
        compress_at = total_iters

    # 截取到第 compress_at 轮的消息
    selected_msgs: list[dict] = []
    for iters in iterations[:compress_at]:
        selected_msgs.extend(iters)

    # 构建 AgentState
    state = AgentState(
        goal=selected_msgs[0]["content"] if selected_msgs else "",
        short_term=list(selected_msgs),
        long_term=[],
        iteration=compress_at,
        meta={
            "scratchpad": meta.get("scratchpad", ""),
            "concept_memory": meta.get("concept_memory", ""),
        },
    )

    # 设置 context window（覆盖环境变量）
    if context_window:
        os.environ["LLM_CONTEXT_WINDOW"] = str(context_window)

    llm = MockLLM()
    system = build_system_prompt(state.tools, state.long_term, concept_memory=state.meta.get("concept_memory", ""))

    # 压缩前
    import copy
    state_before = copy.deepcopy(state)
    print_state_summary("压缩前", state_before, llm, system)

    # 执行压缩：先通过 _maybe_compress_for_context 尝试自动触发
    messages = build_context_messages(state)
    pack = _maybe_compress_for_context(state, llm, system, messages)
    rebuilt_system = pack["system"]

    # 若 _maybe_compress_for_context 未触发（token 未超限），直接调用 _trim_short_term
    after_auto = len(state.short_term)
    auto_triggered = after_auto != len(state_before.short_term)

    if not auto_triggered:
        print(f"\n  {YELLOW}注意：当前 Token 未达到 {warn_ratio*100:.0f}% 阈值，手动触发 _trim_short_term(keep_last={keep_last}){RESET}")
        _trim_short_term(state, keep_last=keep_last)
        rebuilt_system = build_system_prompt(state.tools, state.long_term, concept_memory=state.meta.get("concept_memory", ""))
    else:
        print(f"\n  {GREEN}_maybe_compress_for_context 自动触发了压缩{RESET}")

    # 压缩后
    print_state_summary("压缩后", state, llm, rebuilt_system)

    # 对比
    print_diff(state_before, state, llm, rebuilt_system)

    # 详细展示所有保留消息
    print(f"\n{CYAN}── 保留消息列表 ──{RESET}")
    for i, m in enumerate(state.short_term):
        role = m.get("role", "?")
        content = truncate(m.get("content", ""), 160)
        marker = ""
        if "[系统]" in m.get("content", ""):
            marker = f" {YELLOW}[桥接]{RESET}"
        print(f"  [{i:2d}] {role:9s}{marker}: {content}")


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="测试 compress 功能，从 runs/ 目录加载历史数据模拟压缩",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--run",
        type=str,
        default=None,
        help="指定 run 目录路径（默认：自动选择最新的 run）",
    )
    parser.add_argument(
        "--compress-at",
        type=int,
        default=80,
        help="在第几轮迭代后触发压缩（默认：80）",
    )
    parser.add_argument(
        "--keep-last",
        type=int,
        default=8,
        help="压缩时保留最近几条消息（默认：8）",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="模拟的 context window 大小（默认：使用环境变量 LLM_CONTEXT_WINDOW 或 131072）",
    )
    parser.add_argument(
        "--warn-ratio",
        type=float,
        default=0.90,
        help="触发自动压缩的 token 比例阈值（默认：0.90）",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="列出所有可用的 runs 目录",
    )
    args = parser.parse_args()

    runs_dir = ROOT / "runs"
    if not runs_dir.exists():
        print(f"{RED}错误：找不到 runs/ 目录 ({runs_dir}){RESET}")
        sys.exit(1)

    if args.list_runs:
        available = list_runs(runs_dir)
        print(f"找到 {len(available)} 个可用 runs：")
        for d in available[:20]:
            msgs = load_short_term(d)
            iters = split_into_iterations(msgs)
            print(f"  {d.name}  ({len(msgs)} 条消息, ~{len(iters)} 轮迭代)")
        if len(available) > 20:
            print(f"  ... 及另 {len(available)-20} 个")
        return

    if args.run:
        run_dir = Path(args.run)
        if not run_dir.is_absolute():
            run_dir = ROOT / args.run
        if not run_dir.exists():
            print(f"{RED}错误：指定的 run 目录不存在：{run_dir}{RESET}")
            sys.exit(1)
    else:
        available = list_runs(runs_dir)
        if not available:
            print(f"{RED}错误：runs/ 目录下没有可用的 run{RESET}")
            sys.exit(1)
        run_dir = available[0]
        print(f"自动选择最新 run：{run_dir.name}")

    os.environ["LLM_CONTEXT_WARN_RATIO"] = str(args.warn_ratio)

    run_test(
        run_dir=run_dir,
        compress_at=args.compress_at,
        keep_last=args.keep_last,
        context_window=args.context_window,
        warn_ratio=args.warn_ratio,
    )


if __name__ == "__main__":
    main()
