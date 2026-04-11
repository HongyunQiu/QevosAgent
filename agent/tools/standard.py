"""
内置标准工具集
这些工具构成通用智能体的"标准装备"。
所有工具遵循统一签名：fn(state: AgentState, **kwargs) -> ToolResult
"""

import os
import sys
import ast
import json
import subprocess
import textwrap
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

from ..core.types import AgentState, ToolSpec, ToolResult


# ── 工具函数实现 ──────────────────────────────────────────────────────────────


def _validate_evolved_tool_python_code(python_code: str) -> list[str]:
    """Best-effort static validation for persisted tool recipes."""
    errors: list[str] = []
    try:
        tree = ast.parse(textwrap.dedent(python_code))
    except SyntaxError as e:
        return [f"代码语法错误: {e}"]

    tool_result_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "ToolResult":
            continue

        tool_result_calls += 1
        kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
        unknown = [name for name in kw_names if name not in {"success", "output", "error"}]
        if unknown:
            errors.append(f"ToolResult 使用了未知关键字: {unknown}")
        if "success" not in kw_names and len(node.args) < 1:
            errors.append("ToolResult 缺少 success 参数")
        if "output" not in kw_names and len(node.args) < 2:
            errors.append("ToolResult 缺少 output 参数")

    if tool_result_calls == 0:
        errors.append("python_code 中未找到 ToolResult(...) 返回")

    return list(dict.fromkeys(errors))


def _make_tool_wrapper(fn, tool_name: str):
    def wrapper(state: AgentState, **kwargs) -> ToolResult:
        return fn(state, **kwargs)

    wrapper.__name__ = tool_name
    return wrapper


def _materialize_tool_recipe(
    name: str,
    description: str,
    args_schema: dict,
    python_code: str,
) -> Tuple[Optional[ToolSpec], list[str]]:
    validation_errors = _validate_evolved_tool_python_code(python_code)
    if validation_errors:
        return None, validation_errors

    namespace: dict[str, Any] = {"ToolResult": ToolResult}
    try:
        exec(textwrap.dedent(python_code), namespace)
    except SyntaxError as e:
        return None, [f"代码语法错误: {e}"]
    except Exception as e:
        return None, [f"代码定义错误: {e}"]

    run_fn = namespace.get("run")
    if run_fn is None or not callable(run_fn):
        return None, ["python_code 必须定义一个名为 `run` 的函数。"]

    spec = ToolSpec(
        name=name,
        description=description,
        args_schema=args_schema if isinstance(args_schema, dict) else {},
        fn=_make_tool_wrapper(run_fn, name),
        is_evolve_tool=True,
    )
    return spec, []


def _build_tool_recipe(name: str, description: str, args_schema: dict, python_code: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "args_schema": args_schema if isinstance(args_schema, dict) else {},
        "python_code": textwrap.dedent(python_code).strip(),
    }

def tool_remember(state: AgentState, content: str) -> ToolResult:
    """把重要结论写入长期记忆。"""
    if not content or not content.strip():
        return ToolResult(success=False, output=None, error="content 不能为空")
    state.long_term.append(content.strip())
    return ToolResult(
        success=True,
        output=f"已记录（当前长期记忆共 {len(state.long_term)} 条）"
    )


def tool_raw_append(state: AgentState, content: str, path: str = "") -> ToolResult:
    """Append raw memory (full-fidelity notes / transcript fragments) to an NDJSON file.

    This is the '原始记忆' channel: never summarize here, just append.

    Notes:
    - If `path` is empty, we default to env RAW_MEMORY_PATH (set by run_goal.py per-run),
      falling back to ./raw_memory.ndjson.
    """
    try:
        if not content:
            return ToolResult(success=False, output=None, error="content 不能为空")
        if not path:
            path = os.environ.get("RAW_MEMORY_PATH", "./raw_memory.ndjson")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        import json as _json, time
        rec = {"ts": int(time.time()), "content": content}
        with p.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        return ToolResult(success=True, output=f"raw appended: {p.resolve()}")
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def _scratchpad_trim(text: str, max_chars: int, task_desc: str = "") -> str:
    """裁剪草稿本到 max_chars 以内。

    策略：保留任务描述头（前3行）+ 最新内容，从中部裁掉最老的记录。
    这与 _auto_scratchpad_note 的溢出策略保持一致，确保新追加的内容不被丢弃。
    """
    if len(text) <= max_chars:
        return text

    lines = text.splitlines(keepends=True)
    # 保留任务描述头（前3行，通常是 "任务描述:\n{desc}\n"）
    head_lines = lines[:3]
    head = "".join(head_lines)
    body = text[len(head):]

    overflow = len(head) + len(body) - max_chars
    if overflow >= len(body):
        # 极端情况：head 本身就超了，直接硬截取
        return text[:max_chars]
    body = body[overflow:]
    return head + body


def tool_scratchpad_get(state: AgentState) -> ToolResult:
    """Get current scratchpad."""
    return ToolResult(success=True, output=state.meta.get("scratchpad", ""))


def _scratchpad_persist_to_disk(state: AgentState) -> None:
    """Best-effort persist current scratchpad to $RUN_DIR/scratchpad.md.

    This enables *during-run* visibility and recovery.
    """
    try:
        persistence = getattr(state, "persistence", None)
        if persistence is not None:
            persistence.save_scratchpad(state.meta.get("scratchpad", "") or "")
            return
        run_dir = os.environ.get("RUN_DIR")
        if not run_dir:
            return
        p = Path(run_dir) / "scratchpad.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(state.meta.get("scratchpad", "") or "", encoding="utf-8")
    except Exception:
        return


def tool_scratchpad_set(state: AgentState, content: str) -> ToolResult:
    """Overwrite scratchpad (editable short-term working memory).

    Note: We preserve the task description header (seeded at run start) so the
    scratchpad remains self-contained even if the model overwrites it.
    """
    import os
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    text = (content or "").strip()

    task_desc = state.meta.get("_task_desc")
    if isinstance(task_desc, str) and task_desc.strip():
        # If caller didn't explicitly include task description, prepend it.
        if "任务描述" not in text:
            text = f"任务描述:\n{task_desc.strip()}\n\n" + text

    text = _scratchpad_trim(text, max_chars)
    state.meta["scratchpad"] = text

    # Persist immediately (during-run)
    _scratchpad_persist_to_disk(state)

    return ToolResult(success=True, output=f"scratchpad set ({len(state.meta['scratchpad'])} chars)")


def tool_scratchpad_append(state: AgentState, content: str) -> ToolResult:
    """Append to scratchpad."""
    import os
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    cur = state.meta.get("scratchpad", "")
    add = (content or "").strip()
    if not add:
        return ToolResult(success=False, output=None, error="content 不能为空")
    if cur:
        cur = cur.rstrip() + "\n" + add
    else:
        cur = add
    cur = _scratchpad_trim(cur, max_chars)
    state.meta["scratchpad"] = cur

    # Persist immediately (during-run)
    _scratchpad_persist_to_disk(state)

    return ToolResult(success=True, output=f"scratchpad appended ({len(cur)} chars)")


def _find_python_executable() -> str:
    """找到当前可用的 Python 解释器路径。

    优先级：
    1. 运行本框架的解释器（sys.executable）— 最可靠
    2. 环境变量 PYTHON_EXEC（用户显式指定）
    3. 常见 Windows 路径（Anaconda、py launcher、python）
    4. 系统 PATH 上的 python / python3
    """
    import sys as _sys
    import shutil as _shutil

    # 1. 当前框架自身的解释器
    if _sys.executable:
        return _sys.executable

    # 2. 用户显式指定
    import os as _os
    env_exec = _os.environ.get("PYTHON_EXEC")
    if env_exec and _os.path.isfile(env_exec):
        return env_exec

    # 3. Windows 常见路径（按优先级）
    candidates = [
        r"C:\Users\92680\Anaconda3\envs\nanoGPT\python.exe",
        r"C:\Users\92680\Anaconda3\python.exe",
        r"C:\Python311\python.exe",
        r"C:\Python310\python.exe",
    ]
    for c in candidates:
        if _os.path.isfile(c):
            return c

    # 4. PATH 中查找
    for name in ("python", "python3"):
        found = _shutil.which(name)
        if found:
            return found

    return "python"   # 最后兜底，让调用失败时有清晰报错


def tool_run_python(state: AgentState, code: str) -> ToolResult:
    """在隔离子进程中执行 Python 代码并返回输出。"""
    import os as _os
    timeout = int(_os.environ.get("PYTHON_TIMEOUT", "30"))
    python_exec = _find_python_executable()
    try:
        result = subprocess.run(
            [python_exec, "-c", code],
            capture_output=True,
            stdin=subprocess.DEVNULL,  # prevent inheriting parent's stdin pipe (dashboard mode)
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            return ToolResult(
                success=False,
                output=None,
                error=f"执行失败 (exit {result.returncode})\nSTDERR:\n{stderr}"
            )

        return ToolResult(
            success=True,
            output=output or "（代码执行完毕，无输出）"
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output=None, error=f"执行超时（>{timeout}s）")
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_write_file(state: AgentState, path: str, content: str) -> ToolResult:
    """把内容写入文件（自动创建父目录）。

    Supports env-var expansion in path, e.g. "$RUN_DIR/artifacts/x.json".
    """
    try:
        import os
        # Expand $VARS and ~
        path2 = os.path.expandvars(os.path.expanduser(path))
        p = Path(path2)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"已写入 {p.resolve()}（{len(content)} 字符）"
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_file(state: AgentState, path: str) -> ToolResult:
    """读取文件内容。

    Supports env-var expansion in path, e.g. "$RUN_DIR/artifacts/x.json".
    """
    try:
        import os
        path2 = os.path.expandvars(os.path.expanduser(path))
        content = Path(path2).read_text(encoding="utf-8")
        return ToolResult(success=True, output=content)
    except FileNotFoundError:
        return ToolResult(success=False, output=None, error=f"文件不存在: {path}")
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_file_lines(
    state: AgentState,
    path: str,
    start_line: int = 1,
    end_line: int = 0,
) -> ToolResult:
    """读取文件的指定行范围（行号从 1 开始），避免全量加载大文件。

    - start_line: 起始行（含，默认 1）
    - end_line:   结束行（含，默认 0 = 读到文件末尾）

    返回带行号前缀的内容，格式与 cat -n 一致，方便后续调用 edit_file 时精确定位。
    """
    try:
        import os as _os
        path2 = _os.path.expandvars(_os.path.expanduser(path))
        p = Path(path2)
        if not p.exists():
            return ToolResult(success=False, output=None, error=f"文件不存在: {path}")

        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        total = len(lines)

        s = max(1, int(start_line))
        e = int(end_line) if int(end_line) > 0 else total
        e = min(e, total)

        if s > total:
            return ToolResult(
                success=False, output=None,
                error=f"start_line={s} 超出文件总行数 {total}"
            )

        selected = lines[s - 1: e]
        numbered = "".join(f"{s + i:>6}\t{line}" for i, line in enumerate(selected))
        return ToolResult(
            success=True,
            output=f"[{p.name}  行 {s}–{e} / 共 {total} 行]\n{numbered}",
        )
    except Exception as ex:
        return ToolResult(success=False, output=None, error=str(ex))


def tool_file_outline(state: AgentState, path: str) -> ToolResult:
    """提取文件的结构概要（类、函数、方法及其行号），无需读取完整内容。

    - Python 文件：使用 AST 精确解析，输出缩进树形结构
    - 其他文件：用正则识别常见函数/类声明（JS/TS/Go/Java/C 等）

    结合 read_file_lines 使用：先用本工具定位目标代码块的行号，
    再用 read_file_lines 只读那一段，节省大量 token。
    """
    try:
        import os as _os, ast as _ast, re as _re
        path2 = _os.path.expandvars(_os.path.expanduser(path))
        p = Path(path2)
        if not p.exists():
            return ToolResult(success=False, output=None, error=f"文件不存在: {path}")

        content = p.read_text(encoding="utf-8")
        lines = content.splitlines()
        total = len(lines)

        # ── Python: AST 解析 ────────────────────────────────────────────────
        if p.suffix.lower() == ".py":
            try:
                tree = _ast.parse(content)
            except SyntaxError as e:
                return ToolResult(success=False, output=None, error=f"Python 语法错误: {e}")

            entries: list[str] = []

            def _walk(nodes, indent=0):
                for node in nodes:
                    if isinstance(node, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
                        kind = "class" if isinstance(node, _ast.ClassDef) else (
                            "async def" if isinstance(node, _ast.AsyncFunctionDef) else "def"
                        )
                        end = getattr(node, "end_lineno", "?")
                        entries.append(f"{'  ' * indent}{kind} {node.name}  [{node.lineno}–{end}]")
                        if isinstance(node, _ast.ClassDef):
                            _walk(node.body, indent + 1)

            _walk(_ast.walk(tree) if False else tree.body)  # top-level only, recurse into classes
            if not entries:
                return ToolResult(success=True, output=f"[{p.name}]  共 {total} 行，未找到类/函数定义")
            return ToolResult(
                success=True,
                output=f"[{p.name}]  共 {total} 行\n" + "\n".join(entries),
            )

        # ── 通用：正则启发式 ────────────────────────────────────────────────
        patterns = [
            # JS/TS
            (r"^(\s*)(export\s+)?(default\s+)?(async\s+)?function\s+(\w+)", "function"),
            (r"^(\s*)(export\s+)?(abstract\s+)?class\s+(\w+)", "class"),
            (r"^(\s*)(\w+)\s*[:=]\s*(async\s+)?\(.*\)\s*=>", "arrow"),
            # Go
            (r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "func"),
            # Java/C#/C++
            (r"^(\s*)(public|private|protected|static|virtual|override).*\s(\w+)\s*\([^)]*\)\s*\{", "method"),
            # C/C++ loose
            (r"^(\w[\w\s\*]+)\s+(\w+)\s*\([^)]*\)\s*\{", "func"),
        ]
        entries2: list[str] = []
        for i, line in enumerate(lines, 1):
            for pat, kind in patterns:
                m = _re.match(pat, line)
                if m:
                    entries2.append(f"  {kind:<8} {line.strip()[:80]}  [行 {i}]")
                    break

        if not entries2:
            return ToolResult(
                success=True,
                output=f"[{p.name}]  共 {total} 行（未识别到函数/类声明，可直接用 read_file_lines 分段读取）",
            )
        return ToolResult(
            success=True,
            output=f"[{p.name}]  共 {total} 行\n" + "\n".join(entries2),
        )
    except Exception as ex:
        return ToolResult(success=False, output=None, error=str(ex))


def tool_grep_files(
    state: AgentState,
    pattern: str,
    path: str = ".",
    glob: str = "",
    context: int = 0,
    max_results: int = 50,
    ignore_case: bool = False,
) -> ToolResult:
    """在文件中搜索正则表达式，返回匹配行及上下文，用于追踪跨文件的符号引用。

    参数：
    - pattern:      正则表达式（如 "def load_model" 或 "import torch"）
    - path:         搜索目录或单个文件（默认当前目录）
    - glob:         文件名过滤（如 "*.py"、"*.{ts,js}"，空 = 所有文本文件）
    - context:      每个匹配项前后各保留几行（默认 0）
    - max_results:  最多返回几条匹配（默认 50，防止结果爆炸）
    - ignore_case:  是否忽略大小写（默认 false）

    典型用法：
      grep_files("class Trainer", path="src/", glob="*.py")  → 找类定义位置
      grep_files("from .utils import", path=".", glob="*.py") → 找所有导入点
    """
    try:
        import os as _os, re as _re, fnmatch as _fnmatch
        path2 = _os.path.expandvars(_os.path.expanduser(path))
        base = Path(path2)

        flags = _re.IGNORECASE if ignore_case else 0
        try:
            rx = _re.compile(pattern, flags)
        except _re.error as e:
            return ToolResult(success=False, output=None, error=f"无效正则: {e}")

        # 收集候选文件
        if base.is_file():
            candidates = [base]
        else:
            candidates = [f for f in base.rglob("*") if f.is_file()]
            # glob 过滤
            if glob:
                # 支持 "*.{py,ts}" 展开
                globs = []
                m = _re.match(r"^(.*)\{(.+)\}(.*)$", glob)
                if m:
                    for ext in m.group(2).split(","):
                        globs.append(m.group(1) + ext.strip() + m.group(3))
                else:
                    globs = [glob]
                candidates = [
                    f for f in candidates
                    if any(_fnmatch.fnmatch(f.name, g) for g in globs)
                ]
            # 跳过二进制/隐藏/大文件
            candidates = [
                f for f in candidates
                if not any(part.startswith(".") for part in f.parts)
                and f.stat().st_size < 2 * 1024 * 1024  # 2MB
            ]
            candidates.sort()

        results: list[str] = []
        total_matches = 0
        truncated = False

        for fpath in candidates:
            try:
                file_lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue

            rel = str(fpath.relative_to(base)) if base.is_dir() else fpath.name

            for lineno, line in enumerate(file_lines, 1):
                if not rx.search(line):
                    continue

                if total_matches >= max_results:
                    truncated = True
                    break

                total_matches += 1
                if context > 0:
                    lo = max(0, lineno - 1 - context)
                    hi = min(len(file_lines), lineno + context)
                    block_lines = []
                    for i in range(lo, hi):
                        marker = ">" if i == lineno - 1 else " "
                        block_lines.append(f"{rel}:{i+1}{marker} {file_lines[i]}")
                    results.append("\n".join(block_lines))
                else:
                    results.append(f"{rel}:{lineno}: {line}")

            if truncated:
                break

        if not results:
            return ToolResult(success=True, output=f"未找到匹配 {pattern!r} 的内容")

        header = f"找到 {total_matches} 处匹配（pattern={pattern!r}）"
        if truncated:
            header += f"  [已截断，最多显示 {max_results} 条]"
        return ToolResult(success=True, output=header + "\n\n" + "\n".join(results))
    except Exception as ex:
        return ToolResult(success=False, output=None, error=str(ex))


def tool_analyze_content(
    state: AgentState,
    sources: list,
    question: str,
    model: str = "",
    max_tokens: int = 4000,
) -> ToolResult:
    """将多个文件/文本合并为一个大上下文，发起一次独立的模型调用进行深度分析。

    原始内容不会进入主对话上下文，只有分析结果返回给主 agent，
    从而在处理大型代码库或多文件关联分析时节省主上下文空间。

    sources 格式（列表，支持混合）：
      - 字符串路径：      "src/model.py"  → 自动读取文件内容
      - {"path": "..."}  → 同上
      - {"text": "...", "label": "描述"}  → 直接传入文本片段

    question: 希望模型回答的问题或分析任务，例如：
      "梳理这些文件中数据流向，找出可能的性能瓶颈"
      "列出所有公开 API 接口及其参数"

    model:      覆盖模型名（空 = 沿用主 agent 的模型）
    max_tokens: 分析调用的最大输出 token（默认 4000）

    返回：模型的分析文本（不含原始文件内容）。
    """
    import os as _os

    # ── 1. 加载所有来源 ───────────────────────────────────────────────────────
    sections: list[str] = []
    load_errors: list[str] = []
    total_chars = 0
    char_limit = int(_os.environ.get("ANALYZE_CONTENT_CHAR_LIMIT", str(400_000)))  # ~100k tokens

    for src in (sources or []):
        if isinstance(src, str):
            src = {"path": src}
        if not isinstance(src, dict):
            load_errors.append(f"无效来源格式：{src!r}（需字符串路径或字典）")
            continue

        if "path" in src:
            raw_path = _os.path.expandvars(_os.path.expanduser(src["path"]))
            p = Path(raw_path)
            label = src.get("label") or p.name
            if not p.exists():
                load_errors.append(f"文件不存在：{src['path']}")
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                load_errors.append(f"读取失败 {src['path']}：{e}")
                continue
        elif "text" in src:
            text = str(src["text"])
            label = src.get("label", "文本片段")
        else:
            load_errors.append(f"来源缺少 'path' 或 'text' 字段：{src!r}")
            continue

        if total_chars + len(text) > char_limit:
            remaining = char_limit - total_chars
            if remaining <= 0:
                load_errors.append(f"已达字符上限 {char_limit}，跳过：{label}")
                continue
            text = text[:remaining]
            load_errors.append(f"警告：{label} 被截断至 {remaining} 字符（总上限 {char_limit}）")

        total_chars += len(text)
        sections.append(f"=== {label} ===\n{text}")

    if not sections:
        err = "没有可分析的内容。" + ("错误：" + "；".join(load_errors) if load_errors else "")
        return ToolResult(success=False, output=None, error=err)

    # ── 2. 构建分析 prompt ────────────────────────────────────────────────────
    combined = "\n\n".join(sections)
    warn_block = ("\n\n[加载警告]\n" + "\n".join(load_errors)) if load_errors else ""
    user_msg = (
        f"以下是需要分析的内容（共 {len(sections)} 个来源，{total_chars:,} 字符）：\n\n"
        f"{combined}"
        f"{warn_block}\n\n"
        f"---\n请回答以下问题/完成以下任务：\n{question}"
    )

    system_msg = (
        "你是一个专注的代码与文档分析助手。"
        "请仔细阅读所有提供的内容，然后给出精准、结构化的分析。"
        "分析应直接回答问题，不要重复原始内容，不要废话。"
    )

    # ── 3. 获取 LLM 后端 ──────────────────────────────────────────────────────
    llm = state.meta.get("_llm")

    if llm is None:
        # 兜底：用环境变量重建一个 OpenAI 兼容客户端
        try:
            from ..core.llm import OpenAIBackend
            llm = OpenAIBackend(
                api_key=_os.environ.get("OPENAI_API_KEY", ""),
                base_url=_os.environ.get("OPENAI_BASE_URL") or None,
                model=model or _os.environ.get("LLM_MODEL", ""),
                max_tokens=max_tokens,
            )
        except Exception as e:
            return ToolResult(success=False, output=None, error=f"无法初始化 LLM 客户端：{e}")

    # ── 4. 独立模型调用（不污染主对话） ─────────────────────────────────────
    try:
        # 若需要覆盖模型或 max_tokens，临时 patch
        orig_model = getattr(llm, "model", None)
        orig_max = getattr(llm, "max_tokens", None)
        if model:
            llm.model = model
        llm.max_tokens = max_tokens

        messages = [{"role": "user", "content": user_msg}]
        result_text = llm.complete_text(messages, system=system_msg, max_tokens=max_tokens)

        # 还原
        if model and orig_model is not None:
            llm.model = orig_model
        if orig_max is not None:
            llm.max_tokens = orig_max
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"模型调用失败：{e}")

    header = (
        f"[analyze_content] 分析了 {len(sections)} 个来源，"
        f"{total_chars:,} 字符，独立调用（不占主上下文）\n"
    )
    if load_errors:
        header += "[加载警告] " + "；".join(load_errors) + "\n"
    header += "─" * 40 + "\n"

    return ToolResult(success=True, output=header + result_text)


def tool_edit_file(
    state: AgentState,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    """对文件进行精确的字符串替换，无需重写整个文件。

    精确匹配 old_string 并替换为 new_string：
    - old_string 在文件中不存在 → 报错（检查空格、换行、缩进是否完全一致）
    - old_string 出现多次且 replace_all=False → 报错，提示在 old_string 中加入更多上下文
    - replace_all=True → 替换所有匹配项

    适用场景：修改已有函数/代码块、替换配置值、重命名变量等，
    相比 write_file 节省大量 token，且不会意外覆盖文件其他部分。
    """
    try:
        import os as _os
        path2 = _os.path.expandvars(_os.path.expanduser(path))
        p = Path(path2)

        if not p.exists():
            return ToolResult(success=False, output=None, error=f"文件不存在: {path}")

        content = p.read_text(encoding="utf-8")

        if old_string not in content:
            # Give a diagnostic hint: show nearby lines if old_string is a single line
            hint = ""
            if "\n" not in old_string.strip():
                keyword = old_string.strip()[:30]
                for i, line in enumerate(content.splitlines(), 1):
                    if keyword and keyword.lower() in line.lower():
                        hint = f"\n提示：第 {i} 行含有类似内容：{line!r}"
                        break
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "old_string 在文件中未找到。\n"
                    "请确认内容（空格、换行、缩进）与文件完全一致。"
                    + hint
                ),
            )

        count = content.count(old_string)
        if count > 1 and not replace_all:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"old_string 在文件中出现了 {count} 次，无法唯一定位。\n"
                    f"请在 old_string 中加入更多上下文使其唯一，或传入 replace_all=true 替换全部。"
                ),
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        p.write_text(new_content, encoding="utf-8")

        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        return ToolResult(
            success=True,
            output=(
                f"已修改 {p.resolve()}\n"
                f"替换了 {replaced} 处：{old_lines} 行 → {new_lines} 行"
            ),
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_shell(state: AgentState, command: str, timeout: int = 0) -> ToolResult:
    """执行 shell 命令并返回输出（危险工具，生产环境应加白名单）。

    timeout 参数（秒）：
      - 0 或不传：使用环境变量 SHELL_TIMEOUT（默认 30s）
      - 正整数：使用指定超时，允许模型为耗时操作（下载、编译等）显式延长
    """
    default_timeout = int(os.environ.get("SHELL_TIMEOUT", "30"))
    timeout = int(timeout) if timeout and int(timeout) > 0 else default_timeout

    # Use Popen directly so we can kill the full process tree on timeout without
    # blocking on pipe-drain.  subprocess.run(timeout=...) internally calls
    # communicate() *after* kill(), which hangs on Windows when shell=True spawns
    # child processes (e.g. winget) that survive the parent cmd.exe kill and keep
    # the stdout/stderr pipe open indefinitely.
    # PYTHONUNBUFFERED=1 forces any Python child process invoked by the shell
    # command to use line-buffered (unbuffered) stdout/stderr instead of the
    # default block-buffered mode that applies when stdout is a pipe.
    # Without this, Python subprocesses accumulate output in an 8 KB internal
    # buffer and only flush when the buffer fills or the process exits, making
    # real-time streaming in the dashboard Console tab impossible.
    # Non-Python programs are unaffected by this env var.
    #
    # Prepend the directory of sys.executable to PATH so that "python" in shell
    # commands always resolves to the same interpreter that is running this agent,
    # regardless of whether the process was started from an activated conda env or
    # launched by the dashboard server (which may not have conda on its PATH).
    import sys as _sys
    _python_dir = str(Path(_sys.executable).parent)
    _path = os.environ.get("PATH", "")
    _patched_path = _python_dir + os.pathsep + _path if _python_dir not in _path.split(os.pathsep) else _path
    child_env = {**os.environ, "PYTHONUNBUFFERED": "1", "PATH": _patched_path}

    kwargs: dict = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": child_env,
    }
    if os.name == "nt":
        # Give the shell its own process group so taskkill /T can reach all children.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(command, **kwargs)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))

    # ── Streaming reader threads ───────────────────────────────────────────
    # Read stdout and stderr concurrently in background threads.
    # Each line is printed to sys.stdout immediately (→ Node.js broadcastConsole
    # → dashboard Console tab in real time), while also being collected for the
    # final ToolResult.  Using threads avoids the pipe-deadlock that arises when
    # reading stdout and stderr sequentially.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _drain(pipe, lines, prefix: str) -> None:
        try:
            for raw in pipe:
                lines.append(raw)
                sys.stdout.write(prefix + raw)
                sys.stdout.flush()
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_lines, ""), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_lines, "[stderr] "), daemon=True)
    t_out.start()
    t_err.start()

    def _kill_tree() -> None:
        """Kill the full process tree (same logic as before)."""
        if os.name == "nt":
            subprocess.run(
                f"taskkill /F /T /PID {proc.pid}",
                shell=True, capture_output=True,
            )
        else:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree()
        # Give reader threads a moment to drain whatever was buffered before kill
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        return ToolResult(success=False, output=None, error=f"命令超时（>{timeout}s）")
    except Exception as e:
        _kill_tree()
        return ToolResult(success=False, output=None, error=str(e))

    # Wait for both readers to finish flushing (process is already done)
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    output     = "".join(stdout_lines).strip()
    stderr_text = "".join(stderr_lines).strip()
    combined   = output
    if stderr_text:
        combined += f"\n[STDERR]: {stderr_text}"
    return ToolResult(
        success=(proc.returncode == 0),
        output=combined or "（无输出）",
        error=stderr_text if proc.returncode != 0 else None,
    )


def tool_think(state: AgentState, thought: str) -> ToolResult:
    """
    专用思考工具：不执行任何外部操作，仅让 LLM 有机会进行深度推理。
    对复杂问题特别有用，相当于 Chain-of-Thought 的显式版本。
    """
    return ToolResult(
        success=True,
        output=f"思考完毕。你的分析: {thought}"
    )


def tool_set_goal(state: AgentState, new_goal: str, reason: str) -> ToolResult:
    """
    动态修改当前目标（子目标分解）。
    智能体可以把复杂目标拆分为当前要处理的子目标。
    """
    old_goal = state.goal
    state.goal = new_goal
    state.meta.setdefault("goal_history", []).append(old_goal)
    return ToolResult(
        success=True,
        output=f"目标已更新。\n原目标: {old_goal}\n新目标: {new_goal}\n理由: {reason}"
    )


# ── 进化工具：动态注册新工具 ──────────────────────────────────────────────────

def tool_register_tool(
    state: AgentState,
    name: str,
    description: str,
    args_schema: dict,
    python_code: str,
) -> ToolResult:
    """
    【进化工具】在运行时定义并注册新工具。
    LLM 可以自主生成工具代码并添加到自己的工具集中。

    python_code 必须定义一个名为 `run` 的函数，签名为：
        def run(state, **kwargs) -> ToolResult

    示例:
        def run(state, url):
            import urllib.request
            content = urllib.request.urlopen(url).read().decode()
            return ToolResult(success=True, output=content[:2000])
    """
    if name in state.tools:
        return ToolResult(
            success=False,
            output=None,
            error=f"工具 '{name}' 已存在。如需覆盖，请先确认。"
        )

    spec, validation_errors = _materialize_tool_recipe(name, description, args_schema, python_code)
    if spec is None:
        return ToolResult(
            success=False,
            output=None,
            error="；".join(validation_errors),
        )

    state.tools[name] = spec

    # Record evolved tool recipe for persistence/snapshots.
    # We keep this out of ToolSpec to stay minimal and avoid breaking call sites.
    state.meta.setdefault("evolved_tools", {})[name] = _build_tool_recipe(name, description, args_schema, python_code)

    # 把这次进化记录到长期记忆
    state.long_term.append(
        f"[工具进化] 注册了新工具 '{name}': {description}"
    )

    return ToolResult(
        success=True,
        output=f"工具 '{name}' 注册成功！现在你可以使用它了。"
    )


def tool_validate_tool_recipe(
    state: AgentState,
    name: str,
    description: str,
    args_schema: dict,
    python_code: str,
) -> ToolResult:
    """Validate a candidate tool recipe without registering it."""
    spec, errors = _materialize_tool_recipe(name, description, args_schema, python_code)
    recipe = _build_tool_recipe(name, description, args_schema, python_code)
    return ToolResult(
        success=True,
        output={
            "ok": spec is not None,
            "errors": errors,
            "recipe": recipe,
        },
    )


def tool_repair_tool_candidate(
    state: AgentState,
    name: str,
    description: str,
    args_schema: dict,
    python_code: str,
) -> ToolResult:
    """Store a validated candidate repair for an existing tool."""
    if name not in state.tools and name not in state.meta.get("evolved_tools", {}):
        return ToolResult(success=False, output=None, error=f"工具 '{name}' 不存在，无法修复。")

    validation = tool_validate_tool_recipe(
        state=state,
        name=name,
        description=description,
        args_schema=args_schema,
        python_code=python_code,
    ).output
    if not validation["ok"]:
        state.meta.setdefault("tool_repair_failures", []).append({
            "name": name,
            "errors": list(validation["errors"]),
        })
        return ToolResult(
            success=False,
            output=validation,
            error="；".join(validation["errors"]) or "候选修复未通过校验",
        )

    candidate = dict(validation["recipe"])
    candidate["validation"] = {
        "ok": True,
        "errors": [],
    }
    state.meta.setdefault("tool_repair_candidates", {})[name] = candidate
    state.long_term.append(f"[工具修复候选] 为工具 '{name}' 生成了待晋升修复版本。")
    return ToolResult(
        success=True,
        output={
            "name": name,
            "candidate_stored": True,
            "validation": candidate["validation"],
        },
    )


def tool_promote_tool_candidate(state: AgentState, name: str) -> ToolResult:
    """Promote a validated repair candidate into the formal tool registry."""
    candidates = state.meta.get("tool_repair_candidates", {})
    candidate = candidates.get(name)
    if not candidate:
        return ToolResult(success=False, output=None, error=f"工具 '{name}' 没有待晋升候选版本。")

    validation = candidate.get("validation", {})
    if not validation.get("ok"):
        errs = validation.get("errors") or ["候选版本未通过校验"]
        return ToolResult(success=False, output=None, error="；".join(errs))

    spec, errors = _materialize_tool_recipe(
        candidate.get("name", name),
        candidate.get("description", ""),
        candidate.get("args_schema", {}),
        candidate.get("python_code", ""),
    )
    if spec is None:
        state.meta.setdefault("tool_repair_failures", []).append({
            "name": name,
            "errors": list(errors),
        })
        return ToolResult(success=False, output=None, error="；".join(errors))

    previous_recipe = state.meta.get("evolved_tools", {}).get(name)
    state.tools[name] = spec
    state.meta.setdefault("evolved_tools", {})[name] = _build_tool_recipe(
        name,
        candidate.get("description", ""),
        candidate.get("args_schema", {}),
        candidate.get("python_code", ""),
    )
    state.meta.setdefault("tool_repair_history", []).append({
        "name": name,
        "previous_recipe": previous_recipe,
        "promoted_recipe": state.meta["evolved_tools"][name],
    })
    state.meta.get("tool_repair_candidates", {}).pop(name, None)
    state.meta.get("invalid_evolved_tools", {}).pop(name, None)
    state.long_term.append(f"[工具修复] 工具 '{name}' 的候选版本已晋升为正式版本。")
    return ToolResult(
        success=True,
        output={
            "name": name,
            "promoted": True,
        },
    )


def tool_delete_tool(state: AgentState, name: str, confirm: bool = False) -> ToolResult:
    """
    【进化工具管理】删除一个已降级/废弃的进化工具。

    只能删除进化工具（通过 register_tool 注册的），不能删除内置工具。

    操作流程（必须遵守）：
      1. 先以 confirm=False 调用本工具，查看待删除工具的详细信息
      2. 再用 ask_user 向用户确认是否真的删除，把工具信息告知用户
      3. 仅当用户明确同意后，才以 confirm=True 调用本工具执行删除

    confirm=False（默认）：仅预览信息，不做任何修改
    confirm=True：执行删除（从 state.tools、evolved_tools 及相关候选/无效记录中清除）
    """
    evolved_tools = state.meta.get("evolved_tools", {})

    if name not in evolved_tools:
        if name in state.tools:
            return ToolResult(
                success=False,
                output=None,
                error=f"工具 '{name}' 是内置标准工具，不允许删除。",
            )
        return ToolResult(
            success=False,
            output=None,
            error=f"工具 '{name}' 不存在。",
        )

    recipe = evolved_tools[name]
    description = recipe.get("description", "（无描述）") if isinstance(recipe, dict) else "（无描述）"

    if not confirm:
        has_candidate = name in state.meta.get("tool_repair_candidates", {})
        # 记录本次 preview，confirm=True 时需要验证此标记存在
        state.meta.setdefault("_delete_tool_previewed", set()).add(name)
        return ToolResult(
            success=True,
            output={
                "preview": True,
                "name": name,
                "description": description,
                "has_repair_candidate": has_candidate,
                "tip": (
                    f"以上是将被删除的工具信息。请先用 ask_user 向用户确认，"
                    f"用户明确同意后再以 confirm=True 调用 delete_tool 执行删除。"
                ),
            },
        )

    # 强制要求：必须先经过 confirm=False 的预览步骤
    previewed = state.meta.get("_delete_tool_previewed", set())
    if name not in previewed:
        return ToolResult(
            success=False,
            output=None,
            error=(
                f"删除工具 '{name}' 前必须先以 confirm=False 调用 delete_tool 预览信息，"
                f"并通过 ask_user 向用户确认后才能执行删除。"
            ),
        )

    # 执行删除，同时清理 preview 标记
    previewed.discard(name)
    state.tools.pop(name, None)
    state.meta.get("evolved_tools", {}).pop(name, None)
    state.meta.get("tool_repair_candidates", {}).pop(name, None)
    state.meta.get("invalid_evolved_tools", {}).pop(name, None)

    state.long_term.append(f"[工具删除] 进化工具 '{name}' 已被删除。原描述：{description}")

    return ToolResult(
        success=True,
        output={
            "deleted": name,
            "tip": "工具已删除。如需持久化此变更，请调用 save_tools。",
        },
    )


# ── 工具集构建器 ──────────────────────────────────────────────────────────────

# ── 工具文件（独立持久化） ────────────────────────────────────────────────────

def tool_save_tools(state: AgentState, path: str) -> ToolResult:
    """将进化工具及修复元数据保存到独立 JSON 文件（与记忆文件分离）。"""
    try:
        evolved_tools = state.meta.get("evolved_tools", {})
        valid_tools: dict = {}
        invalid_tools: dict = {}
        for name, rec in evolved_tools.items():
            python_code = rec.get("python_code", "") if isinstance(rec, dict) else ""
            errors = _validate_evolved_tool_python_code(python_code) if python_code else ["缺少 python_code"]
            if errors:
                invalid_tools[name] = {"errors": errors, "recipe": rec}
            else:
                valid_tools[name] = rec

        payload = {
            "version": 1,
            "tools": valid_tools,
            "repair_candidates": state.meta.get("tool_repair_candidates", {}),
            "repair_failures": state.meta.get("tool_repair_failures", []),
            "repair_history": state.meta.get("tool_repair_history", []),
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(
            success=True,
            output={
                "path": str(p.resolve()),
                "saved": len(valid_tools),
                "skipped_invalid": len(invalid_tools),
            },
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_load_tools(state: AgentState, path: str, overwrite: bool = False) -> ToolResult:
    """从独立 JSON 工具文件加载进化工具，按配方注册到 state.tools。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ToolResult(success=False, output=None, error="工具文件必须是 JSON 对象")

        tools_dict = payload.get("tools", {})
        repair_candidates = payload.get("repair_candidates", {})
        if not isinstance(tools_dict, dict):
            return ToolResult(success=False, output=None, error="tools 字段必须是字典")

        state.meta["evolved_tools"] = tools_dict
        state.meta.setdefault("invalid_evolved_tools", {})
        state.meta["tool_repair_candidates"] = repair_candidates if isinstance(repair_candidates, dict) else {}
        state.meta["tool_repair_failures"] = list(payload.get("repair_failures", []))
        state.meta["tool_repair_history"] = list(payload.get("repair_history", []))

        restored = skipped = invalid = 0
        for name, rec in tools_dict.items():
            if not overwrite and name in state.tools:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                invalid += 1
                continue
            python_code = rec.get("python_code", "")
            description = rec.get("description", "")
            args_schema = rec.get("args_schema", {})

            if not isinstance(python_code, str) or not python_code.strip():
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {"errors": ["缺少 python_code"], "recipe": rec}
                continue

            validation_errors = _validate_evolved_tool_python_code(python_code)
            if validation_errors:
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {"errors": validation_errors, "recipe": rec}
                continue

            spec, errors = _materialize_tool_recipe(name, description, args_schema, python_code)
            if spec is None:
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {"errors": errors, "recipe": rec}
                continue

            state.tools[name] = spec
            restored += 1

        return ToolResult(
            success=True,
            output={"restored": restored, "skipped": skipped, "invalid": invalid},
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 细粒度记忆（episodic JSONL） ──────────────────────────────────────────────

def _episodic_ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tool_append_episodic(
    state: AgentState,
    path: str,
    summary: str,
    tags: str = "",
) -> ToolResult:
    """在细粒度记忆文件（JSONL）中追加一条任务执行记录。

    每次任务结束后调用，写入一段话概括：关键操作、重要发现、最终结果。
    - summary: 一段话，包含关键点和便于日后检索的信息（建议 100–300 字）。
    - tags: 逗号分隔的关键词，便于检索（如 "ssh,磁盘,linux,运维"）。
    目标（goal）和时间戳自动从 state 中取得，无需手动填写。
    """
    try:
        if not summary or not summary.strip():
            return ToolResult(success=False, output=None, error="summary 不能为空")

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if isinstance(tags, str) else list(tags)

        # _task_desc 是去掉前缀指令后的原始用户输入，优先使用
        raw_goal = (state.meta.get("_task_desc") or state.goal or "")
        entry = {
            "ts": _episodic_ts(),
            "goal": raw_goal[:200],
            "summary": summary.strip(),
            "tags": tag_list,
        }

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 验收门标记：通知 loop.py 本次运行已完成 episodic 记录
        state.meta["_episodic_appended"] = True

        return ToolResult(success=True, output={"appended": True, "path": str(p.resolve())})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_search_episodic(
    state: AgentState,
    path: str,
    keyword: str = "",
    limit: int = 20,
) -> ToolResult:
    """读取细粒度记忆文件，按关键词过滤并返回最近 N 条记录。

    - keyword: 可选，在 goal/summary/tags 三个字段中做子串匹配（空则返回最近 N 条）。
    - limit: 最多返回条数，默认 20。
    """
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=True, output={"entries": [], "total": 0})

        entries: list[dict] = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if keyword:
            kw = keyword.lower()
            entries = [
                e for e in entries
                if kw in e.get("goal", "").lower()
                or kw in e.get("summary", "").lower()
                or any(kw in t.lower() for t in e.get("tags", []))
            ]

        total = len(entries)
        result = entries[-limit:] if total > limit else entries
        return ToolResult(success=True, output={"entries": result, "total": total})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 概念记忆（concept Markdown） ──────────────────────────────────────────────

def tool_save_concept(state: AgentState, path: str, content: str) -> ToolResult:
    """将概念记忆写入 Markdown 文件，并同步到当前 state（立即注入 system prompt）。

    content 应按工作方向/研究方向分章节叙述，例如：
        ## 工具开发与维护
        （叙述性总结…）

        ## 远程系统运维
        （叙述性总结…）

    每次需要更新概念记忆时，先用 read_concept 读取旧内容，修改后整体覆盖写入。
    """
    try:
        if not content or not content.strip():
            return ToolResult(success=False, output=None, error="content 不能为空")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
        state.meta["concept_memory"] = content.strip()
        return ToolResult(success=True, output={"path": str(p.resolve()), "chars": len(content)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_concept(state: AgentState, path: str) -> ToolResult:
    """读取概念记忆文件并加载到 state，使其注入后续的 system prompt。"""
    try:
        p = Path(path)
        if not p.exists():
            return ToolResult(success=True, output={"content": "", "exists": False})
        content = p.read_text(encoding="utf-8").strip()
        state.meta["concept_memory"] = content
        return ToolResult(success=True, output={"content": content, "chars": len(content)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 完成报告工具 ──────────────────────────────────────────────────────────────


def _normalize_report_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def tool_submit_completion_report(
    state: AgentState,
    goal_understanding: str,
    completed_work,
    remaining_gaps,
    evidence_type: str,
    evidence,
    outcome: str = "done",
    confidence: str = "medium",
) -> ToolResult:
    """提交结构化完成报告，供验收门检查。

    outcome 取值:
      done         - 完整完成，无遗留
      done_partial - 主体完成但有已知缺口
      done_blocked - 外部阻塞，只完成了可做部分

    evidence_type 取值: artifact | tool_result | observation | none
    confidence 取值: low | medium | high
    """
    report = {
        "goal_understanding": (goal_understanding or "").strip(),
        "completed_work": _normalize_report_list(completed_work),
        "remaining_gaps": _normalize_report_list(remaining_gaps),
        "evidence_type": (evidence_type or "none").strip().lower(),
        "evidence": _normalize_report_list(evidence),
        "outcome": (outcome or "done").strip().lower(),
        "confidence": (confidence or "medium").strip().lower(),
    }
    state.meta["completion_report"] = report
    return ToolResult(success=True, output=report)


# ── 异步后台任务工具 ──────────────────────────────────────────────────────────


def _get_async_manager(state: AgentState):
    """懒加载 AsyncJobManager 单例（绑定在 state.meta["_async_manager"]）。"""
    from ..core.async_manager import AsyncJobManager
    mgr = state.meta.get("_async_manager")
    if mgr is None:
        mgr = AsyncJobManager()
        state.meta["_async_manager"] = mgr
    return mgr


def tool_shell_bg(state: AgentState, command: str, timeout: int = 0) -> ToolResult:
    """在后台线程启动 shell 命令，立即返回 job_id（不阻塞主循环）。

    适用场景：
      - 耗时命令（下载、编译、测试套件）不想卡住 agent
      - 需要并发启动多条命令再统一收集结果
      - 需要实时观察部分输出（边跑边 job_wait）

    后续用 job_wait 轮询结果，job_cancel 终止进程。
    timeout: 最长运行秒数（0 = 不限制）。
    """
    mgr = _get_async_manager(state)
    t = int(timeout) if timeout and int(timeout) > 0 else None
    job_id = mgr.start_shell(command, timeout=t)
    return ToolResult(
        success=True,
        output={
            "job_id": job_id,
            "tip": "命令已在后台启动。用 job_wait 查看/等待结果，job_cancel 取消。",
        },
    )


def tool_job_wait(state: AgentState, job_id: str, wait: int = 10) -> ToolResult:
    """查询后台任务状态，并最多等待 wait 秒让其完成。

    - 若任务已完成：立即返回完整输出。
    - 若仍在运行：等待最多 wait 秒后返回截至目前的部分输出。
      可多次调用以持续轮询（每次都能拿到最新累积输出）。

    返回字段：
      job_id, status (running/done/failed/cancelled),
      output (当前已捕获的全部输出), returncode, elapsed_s, command
    """
    mgr = _get_async_manager(state)
    info = mgr.peek(job_id, wait_secs=float(max(0, int(wait))))
    if "error" in info:
        return ToolResult(success=False, output=None, error=info["error"])

    status = info["status"]
    rc = info.get("returncode")
    if status == "running":
        succeeded = True          # 仍在跑，拿到部分输出，不算失败
    elif status == "done":
        succeeded = (rc == 0)
    else:                         # failed / cancelled
        succeeded = False

    return ToolResult(success=succeeded, output=info)


def tool_job_cancel(state: AgentState, job_id: str) -> ToolResult:
    """强制终止一个仍在运行的后台任务（进程树全部杀掉）。"""
    mgr = _get_async_manager(state)
    result = mgr.cancel(job_id)
    if "error" in result:
        return ToolResult(success=False, output=None, error=result["error"])
    return ToolResult(success=True, output=result)


def tool_jobs_list(state: AgentState) -> ToolResult:
    """列出所有后台任务及其当前状态（同时清理 5 分钟前结束的旧任务）。"""
    mgr = _get_async_manager(state)
    jobs = mgr.list_jobs()
    mgr.cleanup()
    if not jobs:
        return ToolResult(success=True, output="（无后台任务）")
    return ToolResult(success=True, output=jobs)


def get_standard_tools() -> dict[str, ToolSpec]:
    """返回标准工具集（直接传给 agent.run()）。"""
    specs = [
        ToolSpec(
            name="remember",
            description="把重要发现、结论或经验写入长期记忆，供未来参考",
            args_schema={"content": "要记忆的内容（字符串）"},
            fn=tool_remember,
        ),
        ToolSpec(
            name="raw_append",
            description="【原始记忆】把原始信息/完整片段追加写入 NDJSON 文件（不总结不去噪）",
            args_schema={
                "content": "要追加的原始内容",
                "path": "文件路径（可选；默认使用环境变量 RAW_MEMORY_PATH，其次 ./raw_memory.ndjson）",
            },
            fn=tool_raw_append,
        ),
        ToolSpec(
            name="scratchpad_get",
            description="读取草稿本（可编辑的工作短期记忆）",
            args_schema={},
            fn=tool_scratchpad_get,
        ),
        ToolSpec(
            name="scratchpad_set",
            description="覆盖写入草稿本（会替换原内容）",
            args_schema={"content": "草稿本内容"},
            fn=tool_scratchpad_set,
        ),
        ToolSpec(
            name="scratchpad_append",
            description="向草稿本末尾追加内容",
            args_schema={"content": "要追加的内容"},
            fn=tool_scratchpad_append,
        ),
        ToolSpec(
            name="think",
            description="用于深度分析和推理，不执行任何外部操作。遇到复杂问题时使用",
            args_schema={"thought": "你的分析内容"},
            fn=tool_think,
        ),
        ToolSpec(
            name="run_python",
            description="在子进程中执行 Python 代码并返回输出，适合计算、数据处理、验证逻辑等",
            args_schema={"code": "Python 代码字符串"},
            fn=tool_run_python,
        ),
        ToolSpec(
            name="shell",
            description=(
                "执行 shell 命令，适合文件操作、系统查询、调用外部程序等。"
                "对于耗时操作（大文件下载、编译、解压等），可通过 timeout 参数显式延长超时时间（单位：秒）。"
                "示例：下载大文件时传 timeout=300，编译大型项目时传 timeout=600。"
            ),
            args_schema={
                "command": "shell 命令字符串",
                "timeout": "（可选）超时秒数，默认 30s；耗时操作（下载/编译/解压）请显式设置，如 300 或 600",
            },
            fn=tool_shell,
        ),
        ToolSpec(
            name="write_file",
            description="把内容写入指定路径的文件（自动创建父目录）",
            args_schema={
                "path": "文件路径（字符串）",
                "content": "要写入的内容（字符串）"
            },
            fn=tool_write_file,
        ),
        ToolSpec(
            name="read_file",
            description="读取文件完整内容并返回。文件较大时优先用 file_outline + read_file_lines 组合代替",
            args_schema={"path": "文件路径（字符串）"},
            fn=tool_read_file,
        ),
        ToolSpec(
            name="read_file_lines",
            description=(
                "读取文件的指定行范围，带行号前缀。"
                "适合大文件的分段阅读：先用 file_outline 定位目标代码块的行号，再用本工具只读那一段。"
            ),
            args_schema={
                "path": "文件路径",
                "start_line": "起始行号（从 1 开始，默认 1）",
                "end_line": "结束行号（含，默认 0 = 读到末尾）",
            },
            fn=tool_read_file_lines,
        ),
        ToolSpec(
            name="file_outline",
            description=(
                "提取文件的类/函数/方法结构概要及各自的行号范围，无需读完整文件。"
                "Python 文件用 AST 精确解析；其他语言用正则识别常见声明。"
                "用法：先调本工具定位目标代码块在第几行，再用 read_file_lines 只读那段内容。"
            ),
            args_schema={"path": "文件路径（字符串）"},
            fn=tool_file_outline,
        ),
        ToolSpec(
            name="grep_files",
            description=(
                "在文件或目录中搜索正则表达式，返回匹配行及上下文，用于跨文件追踪符号引用、定位定义位置。"
                "典型用法：grep_files('class Trainer', path='src/', glob='*.py') 找类定义；"
                "grep_files('import utils', glob='*.py') 找所有导入点。"
            ),
            args_schema={
                "pattern": "正则表达式（如 'def train' 或 'import numpy'）",
                "path": "搜索目录或文件（默认当前目录 '.'）",
                "glob": "文件名过滤（如 '*.py'、'*.{ts,js}'，空 = 所有小于 2MB 的文本文件）",
                "context": "每处匹配前后各保留几行（默认 0）",
                "max_results": "最多返回条数（默认 50）",
                "ignore_case": "是否忽略大小写（默认 false）",
            },
            fn=tool_grep_files,
        ),
        ToolSpec(
            name="analyze_content",
            description=(
                "将多个文件/文本合并为大上下文，发起一次【独立】模型调用进行深度分析，"
                "分析结果返回主 agent，原始内容不占用主对话上下文。"
                "适合：多文件联合阅读、大文件完整理解、跨模块依赖梳理等需要全局视野的任务。"
            ),
            args_schema={
                "sources": (
                    "来源列表，每项可为：文件路径字符串 / {\"path\":\"...\"} / "
                    "{\"text\":\"...\",\"label\":\"描述\"}"
                ),
                "question": "要回答的问题或分析任务描述",
                "model": "（可选）覆盖模型名，空 = 沿用主 agent 模型",
                "max_tokens": "（可选）分析调用最大输出 token，默认 4000",
            },
            fn=tool_analyze_content,
        ),
        ToolSpec(
            name="edit_file",
            description=(
                "对文件进行精确的字符串替换，无需重写整个文件。"
                "先用 read_file 读取文件，找到要修改的确切内容，再调用本工具替换。"
                "比 write_file 节省大量 token，且不会误改文件其他部分。"
                "old_string 必须与文件内容完全一致（含空格、缩进、换行）。"
            ),
            args_schema={
                "path": "文件路径（字符串）",
                "old_string": "要被替换的原始字符串（必须在文件中唯一存在，或配合 replace_all 使用）",
                "new_string": "替换后的新字符串",
                "replace_all": "（可选，默认 false）true = 替换文件中所有匹配项；false = 仅替换第一处（若出现多次则报错）",
            },
            fn=tool_edit_file,
        ),
        ToolSpec(
            name="set_goal",
            description="修改当前目标（用于子目标分解或目标调整）",
            args_schema={
                "new_goal": "新的目标描述",
                "reason": "修改目标的原因"
            },
            fn=tool_set_goal,
        ),
        ToolSpec(
            name="submit_completion_report",
            description=(
                "在调用 done 之前提交结构化完成报告，用于验收门语义判定。"
                "outcome 三态: done=完整完成 / done_partial=有已知缺口 / done_blocked=外部阻塞只做了可做部分。"
                "evidence_type: artifact=文件产物 / tool_result=工具输出 / observation=观察到的结果 / none=无证据。"
                "confidence: low / medium / high。"
            ),
            args_schema={
                "goal_understanding": "你认定的任务目标（自然语言描述）",
                "completed_work": "已完成事项列表（字符串列表或单个字符串）",
                "remaining_gaps": "未完成/遗留事项列表（字符串列表或单个字符串，无则传空列表）",
                "evidence_type": "证据类型: artifact | tool_result | observation | none",
                "evidence": "证据列表（artifact 时填文件路径，其他类型填描述）",
                "outcome": "完成状态: done | done_partial | done_blocked（默认 done）",
                "confidence": "完成信心: low | medium | high（默认 medium）",
            },
            fn=tool_submit_completion_report,
        ),
        ToolSpec(
            name="ask_user",
            description="当缺少关键信息时，向人类提问并暂停本次运行，等待命令行输入后继续",
            args_schema={"question": "要向人类询问的问题（字符串）"},
            fn=lambda state, question: ToolResult(success=True, output={"question": question}),
        ),
        # ── 新拆分持久化工具 ──────────────────────────────────────────────────
        ToolSpec(
            name="save_tools",
            description=(
                "将进化工具（evolved_tools）及修复元数据保存到独立 JSON 文件。"
                "与记忆文件解耦，工具代码单独管理。"
            ),
            args_schema={"path": "工具文件路径（如 ./agent_tools.json）"},
            fn=tool_save_tools,
        ),
        ToolSpec(
            name="load_tools",
            description=(
                "从独立 JSON 工具文件加载进化工具并注册到 state.tools。"
                "不涉及记忆，仅恢复工具。"
            ),
            args_schema={
                "path": "工具文件路径（如 ./agent_tools.json）",
                "overwrite": "是否覆盖已有同名工具（bool，默认 false）",
            },
            fn=tool_load_tools,
        ),
        ToolSpec(
            name="append_episodic",
            description=(
                "向细粒度记忆文件（JSONL）追加一条任务执行记录。"
                "在任务结束时调用，写入一段话概括关键操作、重要发现和最终结果。"
                "goal 和时间戳自动填写，只需提供 summary 和 tags。"
            ),
            args_schema={
                "path": "细粒度记忆文件路径（如 ./memory_episodic.jsonl）",
                "summary": "一段话概括（100-300字），包含关键发现、操作结果、重要参数等便于检索的信息",
                "tags": "逗号分隔的关键词，便于日后检索（如 'ssh,磁盘,linux,运维'）",
            },
            fn=tool_append_episodic,
        ),
        ToolSpec(
            name="search_episodic",
            description=(
                "读取细粒度记忆文件，按关键词过滤并返回最近 N 条记录。"
                "在开始新任务前调用，检索与当前目标相关的历史经验。"
            ),
            args_schema={
                "path": "细粒度记忆文件路径",
                "keyword": "（可选）检索关键词，在 goal/summary/tags 中做子串匹配，空则返回最近 N 条",
                "limit": "（可选）最多返回条数，默认 20",
            },
            fn=tool_search_episodic,
        ),
        ToolSpec(
            name="save_concept",
            description=(
                "将概念记忆写入 Markdown 文件，并同步注入当前 system prompt。"
                "概念记忆按工作方向/研究方向分章节叙述，是对细粒度记忆的二次提炼。"
                "更新时先用 read_concept 读取旧内容，修改后整体覆盖写入。"
            ),
            args_schema={
                "path": "概念记忆文件路径（如 ./memory_concept.md）",
                "content": "完整 Markdown 内容，按工作方向分 ## 章节叙述",
            },
            fn=tool_save_concept,
        ),
        ToolSpec(
            name="read_concept",
            description=(
                "读取概念记忆文件并加载到 state，使其注入后续 system prompt。"
                "在任务开始时调用，获取当前的领域知识概览。"
            ),
            args_schema={"path": "概念记忆文件路径（如 ./memory_concept.md）"},
            fn=tool_read_concept,
        ),
        ToolSpec(
            name="validate_tool_recipe",
            description="校验一个工具候选配方是否满足 run(state, ...) 和 ToolResult(success, output, error) 契约，不会注册也不会覆盖工具",
            args_schema={
                "name": "目标工具名称",
                "description": "工具功能描述",
                "args_schema": "参数说明字典 {param_name: description}",
                "python_code": "候选 Python 代码，需定义 run(state, **kwargs)->ToolResult",
            },
            fn=tool_validate_tool_recipe,
        ),
        ToolSpec(
            name="repair_tool_candidate",
            description="为已有工具登记一个候选修复版本，先校验后写入 state.meta['tool_repair_candidates']，不会立即覆盖正式工具",
            args_schema={
                "name": "要修复的已有工具名称",
                "description": "修复后工具描述",
                "args_schema": "修复后参数说明字典",
                "python_code": "修复后的候选 Python 代码",
            },
            fn=tool_repair_tool_candidate,
        ),
        ToolSpec(
            name="promote_tool_candidate",
            description="将已通过校验的候选修复版本晋升为正式工具，原地更新 state.tools 和 state.meta['evolved_tools']",
            args_schema={
                "name": "要晋升的工具名称",
            },
            fn=tool_promote_tool_candidate,
        ),
        ToolSpec(
            name="register_tool",
            description="【进化】定义并注册一个全新的工具到自身工具集。当现有工具无法满足需求时使用",
            args_schema={
                "name": "新工具名称（英文，无空格）",
                "description": "工具功能描述",
                "args_schema": "参数说明字典 {param_name: description}",
                "python_code": "定义 run(state, **kwargs)->ToolResult 函数的 Python 代码"
            },
            fn=tool_register_tool,
            is_evolve_tool=True,
        ),
        ToolSpec(
            name="delete_tool",
            description=(
                "【进化管理】删除一个已降级/废弃的进化工具（内置工具不可删除）。"
                "必须先以 confirm=False 预览，再用 ask_user 向用户确认，最后以 confirm=True 执行删除。"
            ),
            args_schema={
                "name": "要删除的工具名称",
                "confirm": "是否确认删除（bool）。False=仅预览（默认），True=执行删除（须先经用户确认）",
            },
            fn=tool_delete_tool,
            is_evolve_tool=True,
        ),
        # ── 异步后台任务 ──────────────────────────────────────────────────────
        ToolSpec(
            name="shell_bg",
            description=(
                "【异步】在后台线程启动 shell 命令，立即返回 job_id，不阻塞主循环。"
                "适合耗时操作（下载、编译、长时间测试）或需要并发执行多条命令的场景。"
                "后续用 job_wait 查看/等待结果，job_cancel 终止进程。"
            ),
            args_schema={
                "command": "要执行的 shell 命令",
                "timeout": "（可选）最长运行秒数，0 = 不限制。超时后进程树自动终止",
            },
            fn=tool_shell_bg,
        ),
        ToolSpec(
            name="job_wait",
            description=(
                "查询后台任务状态，并最多等待 wait 秒让其完成。"
                "任务完成时返回完整输出；仍在运行时返回当前已捕获的部分输出（可多次轮询）。"
                "返回字段：job_id, status, output, returncode, elapsed_s, command。"
            ),
            args_schema={
                "job_id": "由 shell_bg 返回的任务 ID",
                "wait":   "（可选）最多等待秒数，默认 10。设为 0 则立即返回当前快照",
            },
            fn=tool_job_wait,
        ),
        ToolSpec(
            name="job_cancel",
            description="强制终止一个仍在运行的后台任务（杀掉整个进程树）",
            args_schema={
                "job_id": "由 shell_bg 返回的任务 ID",
            },
            fn=tool_job_cancel,
        ),
        ToolSpec(
            name="jobs_list",
            description="列出所有后台任务及其状态（running/done/failed/cancelled）",
            args_schema={},
            fn=tool_jobs_list,
        ),
    ]
    return {s.name: s for s in specs}
