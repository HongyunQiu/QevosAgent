#!/usr/bin/env python3
"""
扫描 runs/ 目录下所有 short_term.jsonl，
提取 "[系统] 输出格式错误" 条目及其前一条（触发错误的 assistant 输出），
输出到 stdout 或指定文件，便于分析。

使用 -h 或 --help 查看完整帮助。
"""

import argparse
import json
import sys
from pathlib import Path

MARKER = "[系统] 输出格式错误"
TRUNCATE_LEN = 800  # 每段内容最大显示字符数


def iter_errors(runs_dir: Path, run_filter: str | None, last_n_runs: int = 0):
    """逐个 run 目录扫描，yield (run_id, line_no, prev_entry, error_entry)。
    last_n_runs > 0 时只扫描最近 N 个 run（按目录名倒序取前 N，再正序输出）。
    """
    pattern = f"{run_filter}/short_term.jsonl" if run_filter else "*/short_term.jsonl"
    all_paths = sorted(runs_dir.glob(pattern))  # 旧→新
    if last_n_runs > 0:
        all_paths = all_paths[-last_n_runs:]     # 取最近 N 个，保持旧→新顺序
    for jsonl_path in all_paths:
        run_id = jsonl_path.parent.name
        try:
            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            print(f"[WARN] 无法读取 {jsonl_path}: {e}", file=sys.stderr)
            continue

        for i, raw in enumerate(lines):
            if MARKER not in raw:
                continue
            try:
                error_entry = json.loads(raw)
            except json.JSONDecodeError:
                error_entry = {"role": "?", "content": raw}

            # Only real system-injected format errors have role=="user".
            # Skip assistant messages and tool results that happen to contain
            # the marker string (e.g. a tool result returning this file's source code).
            if error_entry.get("role") not in ("user", "?"):
                continue

            prev_entry = None
            if i > 0:
                try:
                    prev_entry = json.loads(lines[i - 1])
                except json.JSONDecodeError:
                    prev_entry = {"role": "?", "content": lines[i - 1]}

            yield run_id, i + 1, prev_entry, error_entry


def extract_content(entry: dict) -> str:
    """把 content（字符串或列表）统一转成字符串"""
    c = entry.get("content", "")
    if isinstance(c, list):
        parts = []
        for item in c:
            if isinstance(item, dict):
                t = item.get("type", "")
                if t == "text":
                    parts.append(item.get("text", ""))
                elif t == "tool_use":
                    parts.append(f"[tool_use] {item.get('name','')} args={json.dumps(item.get('input',''), ensure_ascii=False)[:200]}")
                elif t == "tool_result":
                    parts.append(f"[tool_result] {str(item.get('content',''))[:200]}")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False)[:200])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(c)


def extract_error_detail(content: str) -> dict:
    """从错误消息中解析错误类型和原始输出片段"""
    detail = {}
    if "错误详情:" in content:
        after = content.split("错误详情:", 1)[1]
        detail["error_detail"] = after.split("原始输出")[0].strip()
    if "原始输出" in content:
        after = content.split("原始输出", 1)[1]
        # 去掉 "(截断):" 或 ":"
        after = after.lstrip("(截断):").lstrip(":").strip()
        detail["raw_output_snippet"] = after[:400]
    return detail


def format_entry(entry: dict | None, label: str, truncate: int) -> str:
    if entry is None:
        return f"### {label}\n_(无前置消息)_\n"
    role = entry.get("role", "?")
    content = extract_content(entry)
    snippet = content[:truncate]
    if len(content) > truncate:
        snippet += f"\n... [共 {len(content)} 字，已截断]"
    return f"### {label}（role={role}）\n```\n{snippet}\n```\n"


def build_report(errors: list, verbose: bool) -> str:
    lines = ["# 输出格式错误扫描报告\n"]
    lines.append(f"共发现 **{len(errors)}** 处格式错误\n")
    lines.append("---\n")

    for idx, (run_id, line_no, prev_entry, error_entry) in enumerate(errors, 1):
        lines.append(f"## [{idx}] run: `{run_id}`  (short_term.jsonl 第 {line_no} 行)\n")

        # 解析错误详情
        err_content = extract_content(error_entry)
        parsed = extract_error_detail(err_content)
        if parsed.get("error_detail"):
            lines.append(f"**错误类型**: {parsed['error_detail']}\n")

        # 前置消息（触发错误的 assistant 输出）
        if prev_entry:
            prev_content = extract_content(prev_entry)
            lines.append(format_entry(prev_entry, "触发错误的上一条消息", TRUNCATE_LEN if not verbose else len(prev_content) + 1))

        # 系统错误消息（只显示解析后的原始输出片段）
        if verbose:
            lines.append(format_entry(error_entry, "系统错误消息（完整）", len(err_content) + 1))
        elif parsed.get("raw_output_snippet"):
            lines.append(f"### 系统错误消息（原始输出片段）\n```\n{parsed['raw_output_snippet']}\n```\n")

        lines.append("---\n")

    return "\n".join(lines)


EPILOG = """
示例：
  # 扫描所有 runs，输出 Markdown 报告到终端
  python debug_tools/scan_format_errors.py

  # 只扫描某次 run
  python debug_tools/scan_format_errors.py --run 20260330-234703

  # 只扫描最近 20 个 runs，保存到文件
  python debug_tools/scan_format_errors.py --limit 20 --output report.md

  # 指定 runs 根目录（默认为当前目录下的 runs/）
  python debug_tools/scan_format_errors.py --runs-dir /path/to/runs

  # 不截断，显示触发错误的完整 assistant 输出
  python debug_tools/scan_format_errors.py --verbose

  # JSON Lines 格式输出，便于管道/程序处理
  python debug_tools/scan_format_errors.py --json

输出字段（--json 模式）：
  run_id            run 目录名，如 20260330-234703
  line_no           错误条目在 short_term.jsonl 中的行号
  error_detail      解析出的错误类型描述
  raw_output_snippet  agent 原始输出片段（前 400 字）
  prev_role         触发错误的上一条消息的 role
  prev_content      触发错误的上一条消息内容（前 800 字）
"""


def main():
    parser = argparse.ArgumentParser(
        description="扫描 runs/ 下所有 short_term.jsonl，提取输出格式错误及其触发消息",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--runs-dir", default="runs", help="runs 根目录（默认: runs）")
    parser.add_argument("--run", default=None, help="只扫描指定 run ID（如 20260330-234703）")
    parser.add_argument("--output", default=None, help="输出到文件（默认输出到 stdout）")
    parser.add_argument("--limit", type=int, default=0, help="只扫描最近 N 个 runs（0=全部）")
    parser.add_argument("--verbose", action="store_true", help="显示完整内容（不截断）")
    parser.add_argument("--json", action="store_true", dest="json_out", help="以 JSON Lines 格式输出，便于程序处理")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.exists():
        print(f"错误：runs 目录不存在: {runs_dir}", file=sys.stderr)
        sys.exit(1)

    errors = list(iter_errors(runs_dir, args.run, last_n_runs=args.limit))

    if args.json_out:
        output_lines = []
        for run_id, line_no, prev_entry, error_entry in errors:
            err_content = extract_content(error_entry)
            parsed = extract_error_detail(err_content)
            record = {
                "run_id": run_id,
                "line_no": line_no,
                "error_detail": parsed.get("error_detail", ""),
                "raw_output_snippet": parsed.get("raw_output_snippet", ""),
                "prev_role": prev_entry.get("role", "") if prev_entry else "",
                "prev_content": extract_content(prev_entry)[:TRUNCATE_LEN] if prev_entry else "",
            }
            output_lines.append(json.dumps(record, ensure_ascii=False))
        result = "\n".join(output_lines)
    else:
        result = build_report(errors, verbose=args.verbose)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"已写入: {args.output}（共 {len(errors)} 条错误）")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(result)


if __name__ == "__main__":
    main()
