"""
内置标准工具集
这些工具构成通用智能体的"标准装备"。
所有工具遵循统一签名：fn(state: AgentState, **kwargs) -> ToolResult
"""

import os
import re
import sys
import ast
import json
import time
import subprocess
import textwrap
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

from ..core.types_def import AgentState, ToolSpec, ToolResult


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


def tool_compress_context(state: AgentState, summary: str = "", use_llm_summary: bool = True) -> ToolResult:
    """主动压缩上下文，腾出 token 空间。

    压缩前会先生成摘要写入草稿本，保留关键信息再丢弃历史：
    - summary 非空：直接使用你提供的摘要（最高优先级）
    - summary 为空且 use_llm_summary=true：自动调用模型对即将丢弃的历史生成结构化摘要
    - 兜底：纯机械裁剪（信息损失最大，不推荐）
    """
    try:
        from ..core.compression import compress_context
        msg = compress_context(state, summary=summary, use_llm_summary=use_llm_summary)
        return ToolResult(success=True, output=msg)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_recall_history(state: AgentState, last_n: int = 12, query: str = "", seg: int = -1) -> ToolResult:
    """回查落盘的原始执行记录 short_term.jsonl（压缩丢失细节时的兜底）。

    记录按 __compaction__ 封段标记切成多段；默认只看**当前段**（最后一次压缩
    之后的原始记录），避免一次糊一脸全量历史。

    参数：
      last_n: 返回最近 N 条原始记录（默认 12）。
      query:  关键词过滤（非空时在选定范围内做子串匹配，忽略 last_n 上限）。
      seg:    指定段号（0 起）。-1=当前段；传 -2 表示跨所有段检索（一般配合 query）。
    """
    import os as _os
    import json as _json
    from pathlib import Path as _Path

    run_dir = None
    persistence = getattr(state, "persistence", None)
    if persistence is not None:
        run_dir = str(getattr(persistence, "run_dir", "") or "")
    if not run_dir:
        run_dir = _os.environ.get("RUN_DIR", "")
    if not run_dir:
        return ToolResult(success=False, output=None, error="无法定位 run 目录，无原始记录可查")

    path = _Path(run_dir) / "short_term.jsonl"
    if not path.exists():
        return ToolResult(success=True, output="(尚无原始记录 short_term.jsonl)")

    # 解析并按 __compaction__ 切段；跳过纯元数据行
    segments: list[list[dict]] = [[]]
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except Exception:
            continue
        role = rec.get("role")
        if role == "__token__" or role == "__handoff__":
            continue
        if role == "__compaction__":
            segments.append([])
            continue
        segments[-1].append(rec)

    total_segs = len(segments)
    # 选定检索范围
    if seg == -2:
        scope = [r for s in segments for r in s]
        scope_desc = f"全部 {total_segs} 段"
    else:
        idx = (total_segs - 1) if seg < 0 else min(seg, total_segs - 1)
        scope = segments[idx]
        scope_desc = f"第 {idx} 段（共 {total_segs} 段）"

    def _fmt(rec: dict) -> str:
        role = rec.get("role", "?")
        content = rec.get("content", "")
        if not isinstance(content, str):
            content = _json.dumps(content, ensure_ascii=False)
        return f"[{role}] {content.strip()}"

    if query:
        hits = [r for r in scope if query in _fmt(r)]
        picked = hits[-last_n:] if last_n and last_n > 0 else hits
        header = f"recall_history｜{scope_desc}｜query='{query}'｜命中 {len(hits)} 条，显示 {len(picked)} 条"
    else:
        picked = scope[-last_n:] if last_n and last_n > 0 else scope
        header = f"recall_history｜{scope_desc}｜显示最近 {len(picked)} / {len(scope)} 条"

    body = "\n\n".join(_fmt(r) for r in picked)
    # 输出整体限长，防止回查反而撑爆上下文
    max_out = int(_os.environ.get("RECALL_HISTORY_MAX_CHARS", "6000"))
    if len(body) > max_out:
        body = body[-max_out:]
        header += "（已尾部截断）"
    return ToolResult(success=True, output=f"{header}\n\n{body}")


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

    # 3. Windows 常见路径（动态检测，不硬编码个人路径）
    user_home = _os.path.expanduser("~")
    candidates = []
    # Anaconda / Miniconda（当前用户下的常见位置）
    for conda_dir in ("Anaconda3", "Miniconda3"):
        conda_base = _os.path.join(user_home, conda_dir)
        if _os.path.isdir(conda_base):
            candidates.append(_os.path.join(conda_base, "python.exe"))
            # 也检查 envs 下的子环境
            envs_dir = _os.path.join(conda_base, "envs")
            if _os.path.isdir(envs_dir):
                try:
                    for env_name in sorted(_os.listdir(envs_dir)):
                        candidates.append(_os.path.join(envs_dir, env_name, "python.exe"))
                except OSError:
                    pass
    # 系统级安装
    for ver in ("313", "312", "311", "310", "39"):
        candidates.append(f"C:\\Python{ver}\\python.exe")
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
    import tempfile
    timeout = int(_os.environ.get("PYTHON_TIMEOUT", "30"))
    python_exec = _find_python_executable()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name
        result = subprocess.run(
            [python_exec, tmp_path],
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
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


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
    else:
        # Give the shell its own session/process group so that os.killpg() on
        # timeout only kills the subprocess tree — NOT the parent node server.js
        # (which would share the same process group if we don't do this).
        kwargs["start_new_session"] = True

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


def normalize_tags(tags) -> list[str]:
    """把原始 tags 归一化为干净的关键词列表。

    - 接受 list 或逗号分隔字符串。
    - 全/半角逗号都切开（模型偶尔把多个词用 `，` 挤进一个元素）。
    - trim、去空、按出现顺序去重（保留原始大小写用于展示）。
    """
    import re as _re
    raw: list[str]
    if isinstance(tags, str):
        raw = [tags]
    elif tags:
        raw = [str(t) for t in tags]
    else:
        raw = []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        for part in _re.split(r"[,，]", item):
            p = part.strip()
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def _run_dir_of(state: AgentState):
    """当前运行的 run 目录（Path）。优先取 persistence，回退 RUN_DIR 环境变量。"""
    pers = getattr(state, "persistence", None)
    if pers is not None and getattr(pers, "run_dir", None) is not None:
        return Path(pers.run_dir)
    env = os.environ.get("RUN_DIR")
    return Path(env) if env else None


def _merge_run_tags(state: AgentState, new_tags: list[str]) -> None:
    """把本次 episodic 的关键词并进 <run_dir>/tags.json（dashboard chip 过滤用）。

    tags.json 是一个小文件：{"tags": [...]}。与已有 tags 做顺序保留的并集，
    以便同一个 run 多次 append_episodic 时关键词累积而不重复。失败不影响主流程。
    """
    try:
        run_dir = _run_dir_of(state)
        if run_dir is None or not new_tags:
            return
        fp = run_dir / "tags.json"
        existing: list[str] = []
        if fp.exists():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = data.get("tags") or []
                elif isinstance(data, list):
                    existing = data
            except Exception:
                existing = []
        merged = normalize_tags(list(existing) + list(new_tags))
        run_dir.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"tags": merged}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # tags.json 只是 dashboard 的便利数据，写失败绝不能中断任务收尾
        pass


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

        # 把关键词并进本次 run 的 tags.json，供 dashboard 侧边栏 chip 过滤使用。
        _merge_run_tags(state, normalize_tags(tag_list))

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


# ── 宏观工作记忆（macro Markdown） ───────────────────────────────────────────

def tool_save_concept(state: AgentState, path: str, content: str) -> ToolResult:
    """将宏观工作记忆写入 Markdown 文件，并同步到当前 state（立即注入 system prompt）。

    content 按工作方向分章节，每条精简一句话、提及关键词，不写具体流程，例如：
        ## 联网搜索
        集成 web_search、DDGS，了解 agent-reach（exa/reddit/bilibili）。

        ## 远程运维
        通过 ssh 连接了 xxx、yyy 等远程主机，实现自动化部署。

    每次更新前先用 read_concept 读取旧内容，修改后整体覆盖写入。
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


def tool_persist_runtime_patches(state: AgentState, path: str = "./AGENTS.md") -> ToolResult:
    """将本次运行积累的运行时格式规范写入 AGENTS.md，供后续运行持久使用。

    只写入 meta['runtime_patches'] 中的规则；若无规则则什么也不做。
    写入位置：AGENTS.md 末尾的 '## 运行时经验' 节（已有则替换）。
    """
    import re as _re
    try:
        patches: list[str] = state.meta.get("runtime_patches", [])
        if not patches:
            return ToolResult(success=True, output="无运行时补丁，跳过写入")
        p = Path(path)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        # 替换已有的自动生成节，或追加
        section = "\n\n## 运行时经验（自动生成）\n" + "\n".join(f"- {rule}" for rule in patches) + "\n"
        existing_stripped = _re.sub(
            r"\n\n## 运行时经验（自动生成）\n.*",
            "",
            existing,
            flags=_re.DOTALL,
        )
        new_content = existing_stripped.rstrip() + section
        p.write_text(new_content, encoding="utf-8")
        return ToolResult(success=True, output={"path": str(p.resolve()), "patches_written": len(patches), "rules": patches})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_concept(state: AgentState, path: str) -> ToolResult:
    """读取宏观工作记忆文件并加载到 state，使其注入后续的 system prompt。"""
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
    from pathlib import Path
    from ..core.async_manager import AsyncJobManager
    mgr = state.meta.get("_async_manager")
    if mgr is None:
        jobs_dir = None
        persistence = getattr(state, "persistence", None)
        if persistence is not None and hasattr(persistence, "run_dir"):
            jobs_dir = Path(persistence.run_dir) / "jobs"
        else:
            import os as _os
            rd = _os.environ.get("RUN_DIR")
            if rd:
                jobs_dir = Path(rd) / "jobs"
        mgr = AsyncJobManager(jobs_dir=jobs_dir)
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


def tool_wait_for_job(state: AgentState, job_id: str, check_interval: int = 15) -> ToolResult:
    """进入轻量等待模式，直到指定后台任务完成后再继续。

    调用后框架将跳过 LLM 调用，每隔 check_interval 秒检查一次任务状态，
    任务完成时自动将结果注入上下文并恢复正常执行。
    适用于"启动任务后暂时没有其他工作可做"的场景，完全避免轮询循环。

    check_interval: 检查间隔秒数（默认 15s，建议范围 10~60s）。
    """
    mgr = _get_async_manager(state)
    jobs = {j["job_id"] for j in mgr.list_jobs()}
    if job_id not in jobs:
        return ToolResult(success=False, error=f"找不到任务 {job_id}，请先用 shell_bg 启动")
    state.meta["_yield_waiting_job"] = {
        "job_id": job_id,
        "interval": max(5, int(check_interval)),
    }
    return ToolResult(
        success=True,
        output={
            "message": f"已进入等待模式，将每 {check_interval}s 检查一次 {job_id}，完成后自动通知。",
            "job_id": job_id,
        },
    )


# ── 环境观察器(Watchers)─────────────────────────────────────────────────────


def _get_watcher_manager(state: AgentState):
    """懒加载 WatcherManager 单例(绑定在 state.meta['_watcher_manager'])。"""
    from pathlib import Path
    from ..core.watcher import WatcherManager
    mgr = state.meta.get("_watcher_manager")
    if mgr is None:
        # artifacts 目录:优先 RUN_DIR/artifacts,否则 cwd/artifacts
        artifacts_dir = None
        persistence = getattr(state, "persistence", None)
        if persistence is not None and hasattr(persistence, "run_dir"):
            artifacts_dir = Path(persistence.run_dir) / "artifacts"
        else:
            rd = os.environ.get("RUN_DIR")
            if rd:
                artifacts_dir = Path(rd) / "artifacts"
        mgr = WatcherManager(artifacts_dir=artifacts_dir)
        state.meta["_watcher_manager"] = mgr
    return mgr


def tool_watch_register(
    state: AgentState,
    name: str,
    path: str,
    interval: int = 10,
    emit: str = "event",
    params: Optional[dict] = None,
    enabled: bool = True,
    desc: str = "",
) -> ToolResult:
    """注册一个环境观察器(watcher)。

    name: 唯一标识(同名会覆盖)
    path: 代码文件的绝对路径(.py 或 .sh)
    interval: 触发间隔秒数(下界,实际由迭代节奏决定)
    emit: event(写 short_term 永久) | live(实时面板可刷新,暂未启用)
    params: 注入给代码的参数(代码通过 store['params'] 读取)
    enabled: 是否启用
    desc: 描述

    .py 文件需定义 run(prev, store, iter_n)->Optional[dict],返回:
      None / {"type":"text","content":"..."} / {"type":"image","image_block":{...}}
      / {"type":"path","path":"..."}
    .sh 文件 stdout 当 text content;环境变量 WATCHER_PARAMS_JSON/WATCHER_ITER/
      WATCHER_STORE_FILE 可读写持久状态。
    框架强制 500 字符注入硬顶,超限自动落 artifacts/ 降级为路径。
    """
    if not name or not path:
        return ToolResult(success=False, output=None, error="name 和 path 必填")
    mgr = _get_watcher_manager(state)
    result = mgr.register(
        name=name, path=path, interval=int(interval), emit=emit,
        params=params or {}, enabled=bool(enabled), desc=desc,
    )
    if not result.get("ok"):
        return ToolResult(success=False, output=None, error=result.get("error"))
    return ToolResult(success=True, output=result)


def tool_watch_unregister(state: AgentState, name: str) -> ToolResult:
    """注销一个 watcher(代码文件不会被删除)。"""
    mgr = _get_watcher_manager(state)
    result = mgr.unregister(name)
    if not result.get("ok"):
        return ToolResult(success=False, output=None, error=result.get("error"))
    return ToolResult(success=True, output=result)


def tool_watch_enable(state: AgentState, name: str) -> ToolResult:
    """启用一个已注册的 watcher。"""
    mgr = _get_watcher_manager(state)
    result = mgr.set_enabled(name, True)
    if not result.get("ok"):
        return ToolResult(success=False, output=None, error=result.get("error"))
    return ToolResult(success=True, output=result)


def tool_watch_disable(state: AgentState, name: str) -> ToolResult:
    """禁用一个 watcher(注册项保留,只是不再被调度)。"""
    mgr = _get_watcher_manager(state)
    result = mgr.set_enabled(name, False)
    if not result.get("ok"):
        return ToolResult(success=False, output=None, error=result.get("error"))
    return ToolResult(success=True, output=result)


def tool_watch_update(
    state: AgentState,
    name: str,
    interval: Optional[int] = None,
    emit: Optional[str] = None,
    params: Optional[dict] = None,
    enabled: Optional[bool] = None,
    desc: Optional[str] = None,
    path: Optional[str] = None,
) -> ToolResult:
    """更新一个 watcher 的字段(只传需要改的)。"""
    mgr = _get_watcher_manager(state)
    fields = {
        "interval": interval, "emit": emit, "params": params,
        "enabled": enabled, "desc": desc, "path": path,
    }
    result = mgr.update(name, **fields)
    if not result.get("ok"):
        return ToolResult(success=False, output=None, error=result.get("error"))
    return ToolResult(success=True, output=result)


def tool_watch_list(state: AgentState) -> ToolResult:
    """列出当前所有已注册的 watcher 及其状态。"""
    mgr = _get_watcher_manager(state)
    entries = mgr.list_entries()
    if not entries:
        return ToolResult(success=True, output="(无已注册的 watcher)")
    return ToolResult(success=True, output=entries)


# ── 网络搜索 ────────────────────────────────────────────────────────────────


def tool_web_search(state: AgentState, query: str, max_results: int = 5) -> ToolResult:
    """使用 DuckDuckGo 搜索引擎进行网络搜索并返回结果摘要。"""
    if not query or not query.strip():
        return ToolResult(success=False, output=None, error="query 不能为空")
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5
    max_results = max(1, min(max_results, 20))

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ToolResult(
                success=False, output=None,
                error="ddgs 未安装。请运行: pip install ddgs",
            )

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return ToolResult(success=True, output={"query": query, "results": [], "count": 0})
        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })
        return ToolResult(success=True, output={"query": query, "results": formatted, "count": len(formatted)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"搜索失败: {e}")


def tool_request_advisor(state: AgentState, reason: str = "") -> ToolResult:
    """主动请求高级指导员在本轮结束后立即介入。"""
    state.meta["_advisor_requested"] = True
    state.meta["_advisor_request_reason"] = reason or "agent_requested"
    return ToolResult(
        success=True,
        output={
            "status": "advisor_scheduled",
            "note": "高级指导员将在下一轮开始前介入，提供独立视角的战略性审视。",
        },
    )


def _resolve_advisor_config(advisor) -> Tuple[str, str, str, str]:
    """解析顾问模型配置，返回 (n, base_url, api_key, model)。

    优先读 os.environ（启动时已从 .env 载入），缺失时再直接解析 .env 文件，
    以便看板保存后无需重启 agent 也能取到最新值。
    """
    n = str(advisor).strip() or "1"
    if n not in ("1", "2"):
        n = "1"
    prefix = f"ADVISOR{n}_OPENAI_"

    base_url = (os.environ.get(prefix + "BASE_URL") or "").strip()
    api_key = (os.environ.get(prefix + "API_KEY") or "").strip()
    model = (os.environ.get(prefix + "MODEL") or "").strip()
    if not (base_url and model):
        env_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip()
                        if k == prefix + "BASE_URL" and not base_url:
                            base_url = v
                        elif k == prefix + "API_KEY" and not api_key:
                            api_key = v
                        elif k == prefix + "MODEL" and not model:
                            model = v
            except Exception:
                pass
    return n, base_url, api_key, model


def tool_consult_advisor(
    state: AgentState,
    question: str,
    advisor: int = 1,
    model: Optional[str] = None,
    max_tokens: int = 4096,
) -> ToolResult:
    """向「顾问模型」咨询，获取来自更强模型的独立专业意见。

    顾问 1/2 在看板「设置 → LLM 服务 → 顾问模型1/2」配置，存于 .env 的
    ADVISOR1_OPENAI_* / ADVISOR2_OPENAI_*，仅供本工具按需调用，不参与主备 fallback。
    兼容 OpenAI / 本地模型 / OpenAI 兼容代理；命中 anthropic.com 时自动改用原生
    Anthropic SDK（功能最全，非兼容层）。
    """
    n, base_url, api_key, cfg_model = _resolve_advisor_config(advisor)
    if not base_url or not cfg_model:
        return ToolResult(
            success=False,
            output=None,
            error=f"顾问模型{n} 未配置：请在看板「设置 → LLM 服务 → 顾问模型{n}」填写服务地址和模型名称",
        )

    use_model = model or cfg_model
    host = re.sub(r"^https?://", "", base_url).split("/")[0].lower()

    # Anthropic 官方 endpoint：仅当域名命中 anthropic.com 才走原生 SDK；OpenAI 兼容代理
    # （含转发 Claude 的网关）仍走 OpenAI SDK。原生失败则回退到 OpenAI 兼容层。
    if host.endswith("anthropic.com"):
        try:
            import anthropic

            native_base = re.sub(r"/v1/?$", "", base_url.rstrip("/")) or "https://api.anthropic.com"
            client = anthropic.Anthropic(api_key=(api_key or ""), base_url=native_base)
            resp = client.messages.create(
                model=use_model,
                max_tokens=int(max_tokens),
                messages=[{"role": "user", "content": question}],
            )
            text = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
            )
            return ToolResult(success=True, output=text)
        except Exception:
            pass  # 回退到 OpenAI 兼容层

    try:
        from openai import OpenAI

        client = OpenAI(api_key=(api_key or "local"), base_url=base_url)
        resp = client.chat.completions.create(
            model=use_model,
            messages=[{"role": "user", "content": question}],
            max_tokens=int(max_tokens),
        )
        return ToolResult(success=True, output=resp.choices[0].message.content)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def _get_run_dir(state: AgentState) -> Optional[str]:
    """Return the current run directory path, or None if unavailable."""
    persistence = getattr(state, "persistence", None)
    if persistence is not None and hasattr(persistence, "run_dir"):
        return str(persistence.run_dir)
    return os.environ.get("RUN_DIR")


def tool_set_thinking_budget(state: AgentState, budget: int = 0) -> ToolResult:
    """动态调整当前 LLM 实例的 extended thinking token 预算。

    budget=0 关闭 thinking；budget>0 开启并设置 token 上限。
    对 AnthropicBackend（claude 系列）和支持 thinking 的 OpenAI 兼容后端均有效。
    修改立即生效，影响本次 run 后续的所有 llm.complete() 调用。
    """
    llm = state.meta.get("_llm")
    if llm is None:
        return ToolResult(success=False, output=None, error="LLM 实例未挂载到 state.meta['_llm']")
    if not hasattr(llm, "thinking_budget"):
        return ToolResult(success=False, output=None,
                          error=f"{type(llm).__name__} 不支持 thinking_budget 属性")
    old = getattr(llm, "thinking_budget", 0)
    llm.thinking_budget = max(0, int(budget))
    # Anthropic 要求 max_tokens > thinking_budget
    if llm.thinking_budget > 0 and hasattr(llm, "max_tokens"):
        llm.max_tokens = max(llm.max_tokens, llm.thinking_budget + 2048)
    action = "开启" if llm.thinking_budget > 0 else "关闭"
    return ToolResult(
        success=True,
        output=f"thinking_budget {old} → {llm.thinking_budget}（{action}）",
    )


def _normalise_image(raw: bytes) -> tuple[str, str]:
    """Convert raw image bytes to a base64 string + MIME type suitable for LLMs.

    Tries Pillow first so any format (BMP, TIFF, ICO, WebP, …) gets re-encoded
    as PNG (lossless, universally supported).  JPEG input is kept as JPEG to
    avoid quality loss from double compression.  Falls back to a straight
    base64 encode when Pillow is unavailable.

    Returns (base64_str, mime_type).
    """
    import base64, io
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        if fmt in ("JPEG", "JPG"):
            if img.mode in ("RGBA", "P", "LA"):
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=95)
            else:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
            return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
        else:
            if img.mode not in ("RGB", "RGBA", "L", "LA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode(), "image/png"
    except ImportError:
        mime = "image/jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        return base64.b64encode(raw).decode(), mime


def tool_load_image(state: AgentState, path: str, caption: str = "") -> ToolResult:
    """加载本地图片或远程图片 URL，将其注入下一次 LLM 调用的上下文（多模态）。

    支持：本地文件路径（jpg/png/gif/webp/bmp/tiff/ico 等，自动转换为 PNG/JPEG）、http/https URL。
    图片会作为 image content block 附加到对话历史，LLM 在下一轮可直接"看到"该图片。
    caption 会作为图片前的文字说明一并注入。
    """
    # 若后端已确认不支持视觉，提前返回明确错误，避免注入图片块导致死循环
    if state.meta.get("_vision_supported") is False:
        return ToolResult(
            success=False,
            output=None,
            error=(
                "当前 LLM 后端不支持多模态（视觉），无法加载图片。\n"
                "如需分析图片内容，请改用支持视觉的模型，"
                "或通过其他方式（如 OCR、图片描述文本）提供图片信息。"
            ),
        )

    import base64, mimetypes, urllib.request

    from ..core.llm import image_block, image_url_block

    p = path.strip()

    # ── 远程 URL：直接用 image_url_block，不下载 ──────────────────────────────
    if p.startswith("http://") or p.startswith("https://"):
        blocks = []
        if caption:
            blocks.append({"type": "text", "text": caption})
        blocks.append(image_url_block(p))
        return ToolResult(
            success=True,
            output=f"已加载远程图片：{p}",
            content_blocks=blocks,
        )

    # ── 本地文件：base64 编码 ─────────────────────────────────────────────────
    fp = Path(p)
    if not fp.is_absolute():
        fp = Path(os.getcwd()) / fp
    if not fp.exists():
        return ToolResult(success=False, output=None, error=f"文件不存在：{fp}")

    try:
        raw = fp.read_bytes()
        # Artifact files (e.g. web_interact screenshots saved as .txt) may contain a
        # Python dict wrapper: {'ok': True, 'data': '<base64_image>'}. Extract the
        # actual base64 image data instead of re-encoding the wrapper text.
        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".ico", ".svg"}
        if fp.suffix.lower() not in _IMAGE_EXTS:
            try:
                import ast
                parsed = ast.literal_eval(raw.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict) and parsed.get("ok") and isinstance(parsed.get("data"), str):
                    raw = base64.b64decode(parsed["data"])
            except Exception:
                pass

        data, mime = _normalise_image(raw)
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"读取图片失败：{e}")

    blocks = []
    if caption:
        blocks.append({"type": "text", "text": caption})
    blocks.append(image_block(data, mime))
    return ToolResult(
        success=True,
        output=f"已加载本地图片：{fp.name}（{mime}，{len(data)//1024}KB base64）",
        content_blocks=blocks,
    )


def tool_load_video(
    state: AgentState,
    path: str,
    interval: float = 2.0,
    max_frames: int = 16,
    start_time: float = 0.0,
    end_time: float = -1.0,
    caption: str = "",
) -> ToolResult:
    """从本地视频文件中均匀抽取关键帧，注入多模态上下文供 LLM 分析。

    依赖 opencv-python（pip install opencv-python）。

    抽帧策略：
    - 在 [start_time, end_time] 时间范围内，每 interval 秒取一帧
    - 若候选帧数超过 max_frames，自动稀疏采样以覆盖整个时间段
    - end_time=-1 表示视频结尾

    对长视频建议先用大 interval 概览全片，再用 start_time/end_time 锁定感兴趣的段落精细分析。
    """
    if state.meta.get("_vision_supported") is False:
        return ToolResult(
            success=False,
            output=None,
            error="当前 LLM 后端不支持多模态（视觉），无法加载视频帧。",
        )

    import base64, io

    from ..core.llm import image_block

    try:
        import cv2
    except ImportError:
        return ToolResult(
            success=False,
            output=None,
            error=(
                "缺少依赖 opencv-python，请执行：pip install opencv-python\n"
                "安装后重试。"
            ),
        )

    fp = Path(path.strip())
    if not fp.is_absolute():
        fp = Path(os.getcwd()) / fp
    if not fp.exists():
        return ToolResult(success=False, output=None, error=f"文件不存在：{fp}")

    cap = cv2.VideoCapture(str(fp))
    if not cap.isOpened():
        return ToolResult(
            success=False, output=None,
            error=f"无法打开视频文件：{fp.name}（格式不支持或文件损坏）",
        )

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps

        # 时间范围 → 帧号范围
        start_frame = max(0, int(start_time * fps))
        end_frame = total_frames if end_time < 0 else min(total_frames, int(end_time * fps))
        if start_frame >= end_frame:
            return ToolResult(
                success=False, output=None,
                error=f"时间范围无效：start_time={start_time}s >= end_time（视频时长 {duration:.1f}s）",
            )

        # 在范围内按 interval 生成候选帧号，超出 max_frames 时均匀稀疏
        step_frames = max(1, int(fps * interval))
        candidates = list(range(start_frame, end_frame, step_frames))
        if len(candidates) > max_frames:
            step = len(candidates) / max_frames
            candidates = [candidates[int(i * step)] for i in range(max_frames)]

        blocks = []
        if caption:
            blocks.append({"type": "text", "text": caption})

        extracted = 0
        for frame_idx in candidates:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            ts = frame_idx / fps
            ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok2:
                continue
            data, mime = _normalise_image(buf.tobytes())
            blocks.append({"type": "text", "text": f"[帧 {extracted + 1}/{len(candidates)}  时间 {ts:.1f}s]"})
            blocks.append(image_block(data, mime))
            extracted += 1
    finally:
        cap.release()

    if extracted == 0:
        return ToolResult(success=False, output=None, error="未能从视频中提取到任何帧。")

    range_desc = f"{start_time:.1f}s–{duration:.1f}s" if end_time < 0 else f"{start_time:.1f}s–{end_time:.1f}s"
    summary = (
        f"已从视频 {fp.name} 提取 {extracted} 帧"
        f"（总时长 {duration:.1f}s，分析范围 {range_desc}，fps={fps:.1f}，采样间隔 {interval}s）"
    )
    return ToolResult(success=True, output=summary, content_blocks=blocks)


def tool_get_env_info(state: AgentState) -> ToolResult:
    """返回当前运行环境的基本信息：日期时间、工作目录。"""
    import datetime
    now = datetime.datetime.now()
    cwd = os.getcwd()
    info = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.strftime("%A"),
        "cwd": cwd,
    }
    lines = [
        f"当前时间：{info['datetime']}（{info['weekday']}）",
        f"当前目录：{info['cwd']}",
    ]
    return ToolResult(success=True, output="\n".join(lines))


def tool_file_tab(
    state: AgentState,
    action: str,
    path: str = "",
    label: str = "",
) -> ToolResult:
    """管理 Dashboard Files 面板的目录 Tab。

    action:
      "open"  — 打开或激活指定路径的目录 Tab（path 必填，label 可选）。
                若该路径已存在 Tab，只切换激活，不重复添加。
      "close" — 关闭指定路径的 Tab（path 必填）。
      "list"  — 列出当前所有 Tab 及其路径。
    """
    import urllib.request as _ur
    import urllib.error as _ue

    run_dir = _get_run_dir(state)
    if not run_dir:
        return ToolResult(success=False, output="", error="无法获取 run_dir")

    port   = os.environ.get("DASHBOARD_PORT", "8765")
    run_id = Path(run_dir).name

    if action not in ("open", "close", "list"):
        return ToolResult(success=False, output="", error=f"未知 action: {action}。可选: open / close / list")

    if action in ("open", "close") and not path:
        return ToolResult(success=False, output="", error=f"action={action} 需要提供 path")

    payload = json.dumps(
        {"action": action, "path": path, "label": label, "runId": run_id},
        ensure_ascii=False,
    ).encode()

    try:
        req = _ur.Request(
            f"http://localhost:{port}/api/file-tab",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except _ue.HTTPError as e:
        err = e.read().decode(errors="replace")
        try:
            err = json.loads(err).get("error", err)
        except Exception:
            pass
        return ToolResult(success=False, output="", error=err)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e) + _port_hint())

    if action == "list":
        tabs = result.get("tabs", [])
        lines = [f"  [{t['id']}] {t['label']}  path={t.get('path','(run)')}" for t in tabs]
        return ToolResult(success=True, output="\n".join(lines) or "(无 Tab)")

    return ToolResult(success=True, output={
        "action": action, "path": path,
        "tabs": [{"id": t["id"], "label": t["label"], "path": t.get("path", "")} for t in result.get("tabs", {}).get("tabs", [])],
    })


def _looks_like_path(s: str) -> bool:
    """判断字符串更像"文件路径"而非"真实内容"。

    用于纠错：LLM 经常把 `runs/xxx/foo.html` 这样的路径直接当 content 传进来，
    本意是想展示该文件的内容。
    """
    if not isinstance(s, str):
        return False
    s2 = s.strip()
    if not s2 or "\n" in s2 or len(s2) > 1024:
        return False
    # 含有 HTML/标签结构 → 是真实内容而非路径
    if "<" in s2 and ">" in s2:
        return False
    # 看起来要像个带扩展名的路径
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", s2))


def _resolve_web_content(run_dir: str, content: str, content_type: str):
    """若 content 实际是一个文件路径，则读出文件内容并返回 (content, base_path)。

    base_path 是该文件所在目录相对 run_dir 的 posix 路径（用于让 HTML/Markdown 里
    的相对图片/资源路径在前端正确解析）。若 content 本就是真实内容，原样返回。
    """
    base_path = ""
    if content_type not in ("html", "markdown", "text", "chart", "image"):
        return content, base_path
    if not _looks_like_path(content):
        return content, base_path

    cand = content.strip().strip('"').strip("'")
    p = Path(cand)
    if not p.is_absolute():
        p = Path(run_dir) / cand
    try:
        if not p.is_file():
            return content, base_path
    except OSError:
        return content, base_path

    run_root = Path(run_dir).resolve()
    try:
        p_res = p.resolve()
        under_run = True
        try:
            rel_to_run = p_res.relative_to(run_root)
        except ValueError:
            under_run = False
            rel_to_run = None
    except OSError:
        return content, base_path

    if content_type == "image":
        # run_dir 内的图片：存相对路径，前端经 /api/run-file-raw 解析；
        # run_dir 外的图片：内联为 data URI，保证一定能显示。
        if under_run:
            return rel_to_run.as_posix(), ""
        try:
            import base64 as _b64
            import mimetypes as _mt
            mime = _mt.guess_type(str(p_res))[0] or "image/png"
            data = p_res.read_bytes()
            return f"data:{mime};base64," + _b64.b64encode(data).decode(), ""
        except Exception:
            return content, base_path

    # 文本类（html / markdown / text / chart）→ 读出文本内容
    try:
        text = p_res.read_text(encoding="utf-8")
    except Exception:
        return content, base_path

    if under_run and rel_to_run is not None:
        parent = rel_to_run.parent.as_posix()
        base_path = "" if parent in (".", "") else parent
    return text, base_path


def tool_web_show(
    state: AgentState,
    content: str,
    content_type: str = "html",
    display_id: str = "default",
    title: str = "",
    mode: str = "replace",
) -> ToolResult:
    """将内容写入 web_display_{display_id}.json，dashboard 监听后实时推送到浏览器。

    content_type: html | markdown | table | chart | text | image
    mode: replace（覆盖）| append（追加）

    纠错：若 content 实际是一个文件路径（如误传 runs/xxx/foo.html），自动读出
    文件内容再展示，并据其所在目录解析相对资源路径。
    """
    import time as _time

    run_dir = _get_run_dir(state)
    if not run_dir:
        return ToolResult(success=False, output="", error="无法获取 run_dir，请确保 agent 通过持久化模式运行")

    content, base_path = _resolve_web_content(run_dir, content, content_type)

    fp = Path(run_dir) / f"web_display_{display_id}.json"

    if mode == "append" and fp.exists():
        try:
            existing = json.loads(fp.read_text(encoding="utf-8"))
            existing["content"] = existing.get("content", "") + "\n" + content
            existing["updated_at"] = _time.time()
            fp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        except Exception:
            mode = "replace"
    else:
        mode = "replace"

    if mode != "append":
        data = {
            "display_id": display_id,
            "content_type": content_type,
            "title": title,
            "content": content,
            "base_path": base_path,
            "created_at": _time.time(),
            "updated_at": _time.time(),
        }
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    port, is_default = _dashboard_port()
    run_id = Path(run_dir).name
    url = f"http://localhost:{port}/view/{run_id}/{display_id}"

    # 每个 display_id 只在首次创建时自动打开，append 不重复触发。
    # opened: None=本次未尝试（append / 已打开过）；True/False=本次尝试的结果。
    opened = None
    open_error = None
    opened_key = f"_web_show_opened_{display_id}"
    if mode != "append" and not state.meta.get(opened_key):
        # Always notify via dashboard API: Electron uses serverEvents to open a native
        # tab; browser clients receive a WebSocket broadcast so remote browsers (e.g.
        # computer A accessing the server on computer B) can open the view themselves.
        #
        # web 视图【只在这一次 open-view 广播/serverEvents 时】弹出，dashboard 没有持久的
        # "视图列表"可事后点开。所以打开结果必须如实上报——POST 失败（服务重启 / DASHBOARD_PORT
        # 指向了非当前浏览器所连的实例）时不能再像以前那样 except:pass 静默吞掉、盲报成功。
        import urllib.request as _ur
        try:
            _payload = json.dumps(
                {"url": url, "title": title or display_id, "display_id": display_id}
            ).encode()
            _req = _ur.Request(
                f"http://localhost:{port}/api/open-view",
                data=_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _ur.urlopen(_req, timeout=2)
            opened = True
        except Exception as _e:
            opened = False
            open_error = f"{type(_e).__name__}: {_e}"
        state.meta[opened_key] = True

    out = {
        "url": url,
        "display_id": display_id,
        "content_type": content_type,
        "port": port,
        "opened": opened,
    }
    if is_default:
        out["warning"] = (
            f"DASHBOARD_PORT 未设置，已回退默认 :{port}；同机多实例时可能不是你浏览器所连的 dashboard。"
        )

    if opened is False:
        # 内容已写盘、URL 有效，但自动弹窗失败——不再盲报成功。附上可手动打开的 URL 以便补救。
        out["open_error"] = open_error
        return ToolResult(
            success=False,
            output=out,
            error=(
                f"视图内容已生成，但未能在 dashboard 自动弹出（{open_error}）。"
                f"目标 dashboard 可能已重启，或 DASHBOARD_PORT 指向了非当前浏览器所连的实例。"
                f"请手动打开 {url}，或确认 agent 与浏览器连的是同一个 dashboard。"
                + _port_hint()
            ),
        )

    # 实例归属自校验（仅在本次确实尝试了自动打开时做）。同机多 agent 错路由时，POST 会打到
    # "活着但不是目标"的实例——它 200 受理、opened=True，却把 Tab 弹在【错误的窗口】里，你正看的
    # 实例什么都没有（terminal_open 的"会话在不在册"是同一思路）。判据：目标 dashboard 的运行
    # 列表 runs 里必须包含本 run；不包含 → 打错了实例，如实降级为失败。
    if opened is True:
        _st = _term_api("GET", "/api/state")
        _runs = _st.get("runs") if isinstance(_st, dict) else None
        if isinstance(_runs, list) and _runs and run_id not in _runs:
            out["registered"] = False
            return ToolResult(
                success=False,
                output=out,
                error=(
                    f"页面已推送到 :{port}，但该 dashboard 的运行列表里没有本 run（{run_id}）——"
                    f"很可能打到了错误的实例（同机多 agent），弹窗出现在别的窗口、你这边看不到。"
                    f"请确认 agent 与浏览器连的是同一个 dashboard。" + _port_hint()
                ),
            )
        if isinstance(_runs, list) and run_id in _runs:
            out["registered"] = True
    return ToolResult(success=True, output=out)


def tool_web_notify(
    state: AgentState,
    message: str,
    display_id: str = "*",
) -> ToolResult:
    """向 WEB 页面的悬浮聊天框推送一条消息（agent → 用户）。

    display_id: 目标展示页面 ID，"*" 表示推送到所有页面（默认）。
    """
    import time as _time

    run_dir = _get_run_dir(state)
    if not run_dir:
        return ToolResult(success=False, output="", error="无法获取 run_dir")

    fp = Path(run_dir) / "web_chat.jsonl"
    record = json.dumps(
        {"role": "agent", "message": message, "display_id": display_id, "ts": _time.time()},
        ensure_ascii=False,
    )
    with open(fp, "a", encoding="utf-8") as f:
        f.write(record + "\n")

    return ToolResult(success=True, output={"message": message, "display_id": display_id})


def tool_web_interact(
    state: AgentState,
    action: str,
    display_id: str = "default",
    payload: dict | None = None,
    inject: bool = True,
) -> ToolResult:
    """在浏览器视图中执行自动化操作。

    Electron 模式：直接控制内嵌 WebContentsView，无需额外配置。
    普通浏览器模式：通过 CDP 控制 Chrome/Edge，需以 --remote-debugging-port=9222 启动浏览器。

    inject 参数仅对 action="screenshot" 有效：
      True（默认）：截图结果直接作为图像块注入对话上下文，LLM 下一轮即可直接"看到"图像，
                    无需再调用 load_image。
      False：只返回 base64 数据字符串，不注入视觉上下文（用于纯数据处理场景）。
    """
    import urllib.request as _ur
    import urllib.error as _ue

    port = os.environ.get("DASHBOARD_PORT", "8765")
    req_data = json.dumps(
        {"display_id": display_id, "action": action, "payload": payload or {}},
        ensure_ascii=False,
    ).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{port}/api/browser-action",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
    except _ue.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body).get("error", body)
        except Exception:
            err = body
        return ToolResult(success=False, output="", error=err)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e) + _port_hint())

    if "error" in result:
        return ToolResult(success=False, output="", error=result["error"])

    # screenshot + inject=True：将图像直接附加为 content_blocks，
    # loop.py 会把它注入 short_term，LLM 下一轮直接看到图像，无需额外 load_image。
    if action == "screenshot" and inject and result.get("data"):
        from ..core.llm import image_block as _image_block
        summary = f"截图完成（display_id={display_id}）"
        cursor = result.get("cursor")
        if cursor:
            summary += f"  光标标识码：#{cursor['code']} 位于 ({cursor['x']}, {cursor['y']})"
        return ToolResult(
            success=True,
            output=summary,
            content_blocks=[_image_block(result["data"], "image/png")],
        )

    return ToolResult(success=True, output=result)


# ── SKILL 工具 ────────────────────────────────────────────────────────────────

_SKILLS_DIR = Path(__file__).parent.parent.parent / "SKILLS"


def tool_list_skills(state: AgentState) -> ToolResult:
    """列出 SKILLS/ 目录中所有可用的技能文件。"""
    skills_dir = Path(os.environ.get("SKILLS_DIR", str(_SKILLS_DIR)))
    if not skills_dir.exists():
        return ToolResult(success=True, output={"skills": [], "skills_dir": str(skills_dir)})
    try:
        skills = []
        for p in sorted(skills_dir.glob("*.md")):
            size = p.stat().st_size
            skills.append({"name": p.stem, "filename": p.name, "size_bytes": size})
        return ToolResult(success=True, output={"skills": skills, "skills_dir": str(skills_dir)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_skill(state: AgentState, name: str) -> ToolResult:
    """读取指定技能文件的内容。name 为文件名（不含 .md 后缀）。"""
    skills_dir = Path(os.environ.get("SKILLS_DIR", str(_SKILLS_DIR)))
    # 支持带或不带 .md 后缀
    target = name if name.endswith(".md") else f"{name}.md"
    fp = skills_dir / target
    if not fp.exists():
        available = [p.stem for p in skills_dir.glob("*.md")] if skills_dir.exists() else []
        return ToolResult(
            success=False, output=None,
            error=f"技能文件 '{target}' 不存在。可用技能: {available}"
        )
    try:
        content = fp.read_text(encoding="utf-8")
        return ToolResult(success=True, output={"name": fp.stem, "content": content})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── UI App 面板事件（runtime:web 的结构化事件旁路）────────────────────────────
# 面板经 qevos.emit(...) 把结构化事件追加到 app-data/<app>/.qevos/panel_events.jsonl。
# Agent 用 panel_poll 读取这些事件来处理"需要智能"的操作，改写项目文件后由面板重渲染。
# 详见 SKILLS/ui_app.md。
_APP_DATA_DIR = Path(__file__).parent.parent.parent / "app-data"


def _app_data_dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR", str(_APP_DATA_DIR)))


def tool_panel_poll(state: AgentState, app: str, since: float = None, consume: bool = False,
                    root: str = None) -> ToolResult:
    """读取某 UI App 面板发来的结构化事件（panel_events.jsonl）。

    app:     App id（apps/<id>.md 的 id，也是 app-data/<id>/ 目录名）。
    since:   （可选）只返回 ts 大于该毫秒时间戳的事件，用于增量轮询。
    consume: （可选）读取后清空事件日志；增量轮询请改用 since，不要 consume。
    root:    （可选）项目文件夹绝对路径；给了则读 <root>/.qevos/，否则 app-data/<id>/。
    """
    app_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(app or ""))
    if not app_id:
        return ToolResult(success=False, output=None, error="app 必填")
    if root and os.path.isabs(str(root)):
        base = Path(str(root))
    else:
        base = _app_data_dir() / app_id
    fp = base / ".qevos" / "panel_events.jsonl"
    if not fp.exists():
        return ToolResult(success=True, output={"app": app_id, "events": [], "count": 0})
    try:
        events = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if since is not None and ev.get("ts", 0) <= since:
                continue
            events.append(ev)
        if consume:
            fp.write_text("", encoding="utf-8")
        return ToolResult(success=True, output={"app": app_id, "events": events, "count": len(events)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_panel_control(state: AgentState, app: str, action: str, selector: str = None,
                       value: str = None, code: str = None, root: str = None,
                       timeout: int = None, inject: bool = True) -> ToolResult:
    """操控/检查一个已打开的 UI App 面板（第一方通道，无需 CDP/调试浏览器）。

    通过面板自带的 qevos 桥下发指令，Electron 与普通浏览器模式完全一致；
    要求该 App 的面板当前已打开（有 SSE 连接），否则返回错误。

    action:
      click    — 点击 selector 匹配的元素
      fill     — 给 selector（input/textarea）填 value 并派发 input/change
      value    — 读取 selector 的 value
      getText  — 读取 selector 的 textContent
      getHtml  — 读取 selector（或整页）的 outerHTML
      exists   — selector 是否存在（bool）
      count    — selector 匹配数量
      waitFor  — 等待 selector 出现（timeout 毫秒）
      screenshot — 把面板（或 selector 元素）DOM 渲染成 PNG。注意是"重绘非抓屏"，
                   布局够用、精细样式可能有偏差；跨域资源会失败。inject=True 时图像直接注入上下文。
      eval     — 在面板内求值 code（表达式，返回可 JSON 序列化的值）
    root: （可选）项目根绝对路径，同 openProject 的 root。
    """
    import urllib.request as _ur
    import urllib.error as _ue

    # screenshot 走 html2canvas，可能较慢；给更宽的默认超时。
    eff_timeout = timeout if timeout is not None else (25000 if action == "screenshot" else None)

    args = {}
    if selector is not None:     args["selector"] = selector
    if value is not None:        args["value"] = value
    if code is not None:         args["code"] = code
    if eff_timeout is not None:  args["timeout"] = eff_timeout
    body = {"app": str(app or ""), "action": str(action or ""), "args": args}
    if root:        body["root"] = root
    if eff_timeout: body["timeout"] = eff_timeout

    port = os.environ.get("DASHBOARD_PORT", "8765")
    try:
        req = _ur.Request(
            f"http://localhost:{port}/api/panel-control",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=(int(eff_timeout) / 1000 + 5) if eff_timeout else 35) as resp:
            result = json.loads(resp.read())
    except _ue.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        try: err = json.loads(body_txt).get("error", body_txt)
        except Exception: err = body_txt
        return ToolResult(success=False, output=None, error=err)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e) + _port_hint())

    if result.get("ok") is False:
        return ToolResult(success=False, output=None, error=result.get("error") or "面板控制失败")

    res = result.get("result")
    # screenshot + inject=True：把面板渲染图直接注入上下文，LLM 下一轮即可"看到"。
    if action == "screenshot" and isinstance(res, dict) and res.get("image"):
        if inject:
            from ..core.llm import image_block as _image_block
            return ToolResult(
                success=True,
                output=f"面板截图完成（app={app}，DOM 渲染，非像素抓屏）",
                content_blocks=[_image_block(res["image"], res.get("mime") or "image/png")],
            )
        return ToolResult(success=True, output={"image_len": len(res["image"]), "mime": res.get("mime")})
    return ToolResult(success=True, output={"result": res})


# ── APP 工具（用户态可执行程序，陈列在 Dashboard 的 Apps Tab）─────────────────
# 一个 app = apps/<id>.md：YAML frontmatter（name/icon/description/runtime/enabled）
# + 脚本正文。点击即跑、不启动 Agent。Dashboard 后端用同样的格式解析执行。
_APPS_DIR = Path(__file__).parent.parent.parent / "apps"
_APP_RUNTIMES = ("python", "powershell", "shell")


def _apps_dir() -> Path:
    return Path(os.environ.get("APPS_DIR", str(_APPS_DIR)))


def tool_register_app(
    state: AgentState,
    name: str,
    description: str,
    runtime: str,
    script: str,
    icon: str = "📦",
) -> ToolResult:
    """
    【产出可执行程序】把一段脚本注册成 Dashboard "Apps" Tab 里的一个可点击程序。

    用于把已经收敛、确定性强的重复任务固化成"一键运行"的小程序——用户（或你自己）
    点一下就直接执行，无需再启动 Agent / 调模型。

    参数:
      name        程序名（也用于派生文件名 id）
      description 一句话说明用途
      runtime     'python' | 'powershell' | 'shell'
      script      脚本正文（纯代码，不要带 ``` 围栏）
      icon        一个 emoji 图标，默认 📦
    """
    if runtime not in _APP_RUNTIMES:
        return ToolResult(success=False, output=None,
                          error=f"runtime 必须是 {_APP_RUNTIMES} 之一")
    app_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", name).strip("_") or f"app_{int(time.time())}"
    apps_dir = _apps_dir()
    try:
        apps_dir.mkdir(parents=True, exist_ok=True)

        def _q(s: str) -> str:
            return json.dumps(str(s), ensure_ascii=False)

        front = (
            "---\n"
            f"name: {_q(name)}\n"
            f"icon: {_q(icon or '📦')}\n"
            f"description: {_q(description or '')}\n"
            f"runtime: {runtime}\n"
            "enabled: true\n"
            "---\n\n"
        )
        (apps_dir / f"{app_id}.md").write_text(front + (script or ""), encoding="utf-8")
        state.long_term.append(f"[程序产出] 注册了可执行程序 '{name}' (id={app_id}, runtime={runtime})")
        return ToolResult(success=True, output={
            "id": app_id, "name": name, "runtime": runtime,
            "message": f"程序 '{name}' 已加入 Dashboard 的 Apps Tab，用户可一键运行。",
        })
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_list_apps(state: AgentState) -> ToolResult:
    """列出 Apps Tab 里所有已注册的可执行程序。"""
    apps_dir = _apps_dir()
    if not apps_dir.exists():
        return ToolResult(success=True, output={"apps": [], "apps_dir": str(apps_dir)})
    try:
        apps = []
        for p in sorted(apps_dir.glob("*.md")):
            meta, _ = _parse_app_file(p.read_text(encoding="utf-8"))
            apps.append({"id": p.stem, "name": meta.get("name") or p.stem,
                         "runtime": meta.get("runtime"), "description": meta.get("description", "")})
        return ToolResult(success=True, output={"apps": apps, "apps_dir": str(apps_dir)})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def _parse_app_file(content: str):
    """解析 app 文件 -> (meta dict, script body)。与 dashboard/server.js:parseAppFile 对齐。"""
    meta = {"name": "", "icon": "📦", "description": "", "runtime": "shell", "enabled": True}
    body = content
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", content)
    if m:
        body = m.group(2) or ""
        for raw in m.group(1).splitlines():
            line = re.sub(r"#.*$", "", raw).strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and ((v[0] == '"' and v.endswith('"')) or (v[0] == "'" and v.endswith("'"))):
                v = v[1:-1]
            if k == "enabled":
                meta["enabled"] = v.lower() not in ("false", "no", "0", "off")
            elif k in meta:
                meta[k] = v
    fence = re.match(r"^\s*```([a-zA-Z0-9_+-]*)\r?\n([\s\S]*?)\r?\n```\s*$", body)
    if fence:
        body = fence.group(2)
    if meta["runtime"] not in _APP_RUNTIMES:
        meta["runtime"] = "shell"
    return meta, body.lstrip()


def tool_run_app(state: AgentState, name: str) -> ToolResult:
    """运行一个已注册的可执行程序（按 id 或名称）。复用 run_python / shell 执行路径。"""
    apps_dir = _apps_dir()
    target = name[:-3] if name.endswith(".md") else name
    fp = apps_dir / f"{target}.md"
    if not fp.exists():
        available = [p.stem for p in apps_dir.glob("*.md")] if apps_dir.exists() else []
        return ToolResult(success=False, output=None,
                          error=f"程序 '{target}' 不存在。可用: {available}")
    meta, script = _parse_app_file(fp.read_text(encoding="utf-8"))
    runtime = meta["runtime"]
    if runtime == "python":
        return tool_run_python(state, code=script)
    if runtime == "powershell":
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"qevos_app_{int(time.time())}.ps1"
        tmp.write_text(script, encoding="utf-8")
        try:
            return tool_shell(state, command=f'powershell -NoProfile -ExecutionPolicy Bypass -File "{tmp}"')
        finally:
            try: tmp.unlink()
            except Exception: pass
    return tool_shell(state, command=script)


def tool_ssh_execute(state: AgentState, **kwargs) -> ToolResult:
    """通过 SSH 在远程服务器执行命令。支持密码/密钥认证、sudo 密码注入、严格 timeout 和 /stop 中断。"""
    host = kwargs.get("host", "")
    port = int(kwargs.get("port", 22))
    username = kwargs.get("username", "")
    password = kwargs.get("password", None)
    command = kwargs.get("command", "")
    timeout = float(kwargs.get("timeout", 30))
    key_file = kwargs.get("key_file", None)
    sudo_password = kwargs.get("sudo_password", None)

    if not host or not username or not command:
        return ToolResult(success=False, output="", error="缺少必要参数: host, username, command")
    if not password and not key_file:
        return ToolResult(success=False, output="", error="必须提供 password 或 key_file 之一")

    if sudo_password:
        if "sudo" in command:
            command = command.replace("sudo", "sudo -S", 1)
        else:
            command = "sudo -S " + command
        # 用 printf 避免密码含单引号时 shell 解析错误
        escaped = sudo_password.replace("'", "'\\''")
        command = f"printf '%s\\n' '{escaped}' | " + command

    try:
        import paramiko
    except ImportError:
        return ToolResult(
            success=False, output="",
            error="缺少依赖 paramiko，请执行：pip install paramiko",
        )

    try:
        import time as _time

        _ih = state.meta.get("_interrupt_handler")

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": min(timeout, 15),
        }
        if key_file:
            connect_kwargs["key_filename"] = key_file
        else:
            connect_kwargs["password"] = password

        ssh.connect(**connect_kwargs)

        try:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            channel = stdout.channel

            start = _time.time()
            stdout_data = b""
            stderr_data = b""

            while True:
                if _ih and getattr(_ih, "force_stop", False):
                    _ih.force_stop = False
                    channel.close()
                    return ToolResult(success=False, output="", error="SSH 命令被用户中断 (/stop)")

                if _time.time() - start > timeout:
                    channel.close()
                    return ToolResult(success=False, output="", error=f"SSH 命令超时（{timeout}s）")

                if channel.exit_status_ready():
                    break

                if channel.recv_ready():
                    stdout_data += channel.recv(4096)
                if channel.recv_stderr_ready():
                    stderr_data += channel.recv_stderr(4096)

                _time.sleep(0.1)

            stdout_data += stdout.read()
            stderr_data += stderr.read()
            returncode = channel.recv_exit_status()

            stdout_str = stdout_data.decode("utf-8", errors="replace")
            stderr_str = stderr_data.decode("utf-8", errors="replace")

            parts = []
            if stdout_str:
                parts.append("[stdout]\n" + stdout_str)
            if stderr_str:
                parts.append("[stderr]\n" + stderr_str)
            output = "\n".join(parts)

            if returncode == 0:
                return ToolResult(success=True, output=output)
            else:
                return ToolResult(success=False, output=output, error=f"命令退出码: {returncode}")

        finally:
            ssh.close()

    except paramiko.AuthenticationException:
        return ToolResult(success=False, output="", error="SSH 认证失败：用户名或密码错误")
    except paramiko.SSHException as e:
        return ToolResult(success=False, output="", error=f"SSH 错误: {e}")
    except Exception as e:
        return ToolResult(success=False, output="", error=f"{type(e).__name__}: {e}")


# ── Shared built-in terminal (人机共用) ─────────────────────────────────────
# These drive the dashboard's named PTY sessions over HTTP (/api/term/*), the
# SAME sessions the user sees and types into. So the agent and the user share one
# shell: the agent's commands run live in the user's visible terminal, and while
# the agent is "holding the mic" that terminal tints + shows "🤖 Agent 操作中".
#
# Difference from `shell`: `shell` is one-shot and private (a fresh hidden
# subprocess each call, no session state, no human visibility). Use the terminal
# tools when state must persist across commands (cd / activated env / an ssh
# session) or when the user should watch / be able to take over.

_ANSI_RE = None


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences so the model reads clean text."""
    global _ANSI_RE
    if _ANSI_RE is None:
        import re as _re
        # CSI sequences, OSC sequences, and stray single-char escapes.
        _ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
    return _ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "")


def _dashboard_port():
    """返回 (port, is_default)。

    is_default=True 表示环境里没有 DASHBOARD_PORT、回退到了默认 8765——这在同机跑多个
    实例时往往意味着 agent 打到了错误的 dashboard（会话建在别的服务进程里，当前浏览器看不到）。
    """
    v = os.environ.get("DASHBOARD_PORT")
    return (v, False) if v else ("8765", True)


def _port_hint() -> str:
    """DASHBOARD_PORT 用了默认兜底时，返回一句可拼到错误信息后的诊断提示；否则空串。

    用于请求-响应式的 dashboard 工具：连接失败/找不到目标时，把含糊的"连接被拒"变成
    "可能打到了错误的 dashboard 实例"这种可诊断信息。
    """
    port, is_default = _dashboard_port()
    if is_default:
        return (
            f" [提示] DASHBOARD_PORT 未设置，本次用了默认 :{port}；"
            "同机多实例时连接失败/找不到目标，往往是打到了错误的 dashboard 进程。"
        )
    return ""


def _term_api(method: str, path: str, body=None, timeout: float = 10):
    """Call the dashboard terminal API at http://localhost:DASHBOARD_PORT."""
    import urllib.request as _ur
    import urllib.error as _ue
    port, _is_default = _dashboard_port()
    url = f"http://localhost:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = _ur.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with _ur.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except _ue.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def tool_terminal_list(state: AgentState) -> ToolResult:
    """列出所有内置终端会话（id / 标题 / cwd / owner / 是否存活）。

    用于发现用户已经打开的终端，以便用 terminal_run/send 在其中操作。
    """
    r = _term_api("GET", "/api/term")
    if "error" in r:
        return ToolResult(success=False, output=None, error=r["error"])
    return ToolResult(success=True, output=r.get("sessions", []))


def tool_terminal_open(state: AgentState, title: str = "Agent") -> ToolResult:
    """新建一个共享终端会话，用户界面会自动出现对应 Tab。

    返回 {id, title, port, registered}：port 是本次实际命中的 dashboard 端口，
    registered 表示新建的会话确实登记在了这台服务的会话列表里。终端会话是各服务进程
    的内存态，只对连着【同一台 live 进程】的浏览器可见——同机多实例或服务重启后，即便
    POST 成功、会话也可能落在你正看的浏览器所连之外的进程里。此工具因此建完会二次核对，
    发现没登记在册就不再盲报成功。
    """
    port, is_default = _dashboard_port()
    r = _term_api("POST", "/api/term", {"title": title})
    if "error" in r:
        return ToolResult(success=False, output=None, error=r["error"])
    sid = r.get("id")
    out = {"id": sid, "title": r.get("title"), "port": port}
    if is_default:
        out["warning"] = (
            "DASHBOARD_PORT 未设置，已回退默认 8765；同机多实例时可能落到错误的 dashboard，"
            "当前浏览器未必看得到该终端。"
        )

    # 自校验：确认会话确实登记在这台服务上（多实例 / 服务重启场景下能一眼看出错配）。
    check = _term_api("GET", "/api/term")
    if "error" in check:
        # 无法二次核对——不阻断，但标注未验证，让上层知道成功是"未经确认"的。
        out["registered"] = None
        out["verify_error"] = check["error"]
        return ToolResult(success=True, output=out)
    registered = any(s.get("id") == sid for s in (check.get("sessions") or []))
    out["registered"] = registered
    if not registered:
        return ToolResult(
            success=False, output=out,
            error=(
                f"终端会话已创建 (id={sid}) 但未出现在 :{port} 的会话列表中——"
                "目标 dashboard 可能已重启，或 DASHBOARD_PORT 指向了非当前浏览器所连的实例。"
                "请用 terminal_list 核对，并确认 agent 与浏览器连的是同一个 dashboard。"
            ),
        )
    return ToolResult(success=True, output=out)


def tool_terminal_send(state: AgentState, id: str, text: str, submit: bool = True) -> ToolResult:
    """向指定终端会话输入文本（submit=True 时自动追加回车提交），不等待输出立即返回。

    适合交互式场景：应答提示符、向 REPL/ssh 会话喂输入。需要看结果时配合 terminal_read，
    或直接用 terminal_run 一步到位。
    """
    data = text + ("\r" if submit else "")
    r = _term_api("POST", f"/api/term/{id}/input", {"data": data})
    if "error" in r:
        return ToolResult(success=False, output=None, error=r["error"])
    return ToolResult(success=True, output={"sent": True, "seq": r.get("seq")})


def tool_terminal_read(state: AgentState, id: str, since: int = 0) -> ToolResult:
    """读取终端会话自 since 偏移以来的输出（已去 ANSI）。返回 {output, seq, owner, alive}。

    返回的 seq 可作为下次调用的 since，实现增量读取（只取新输出）。
    """
    r = _term_api("GET", f"/api/term/{id}/output?since={int(since)}")
    if "error" in r:
        return ToolResult(success=False, output=None, error=r["error"])
    return ToolResult(success=True, output={
        "output": _strip_ansi(r.get("data", "")),
        "seq": r.get("seq"), "owner": r.get("owner"), "alive": r.get("alive"),
    })


def tool_terminal_run(state: AgentState, id: str, command: str, timeout: int = 60) -> ToolResult:
    """在共享终端会话里执行一条命令并等待结束，返回干净输出与退出码 {output, exit_code}。

    执行过程实时显示在用户可见的终端里；期间该终端背景变色并标注“🤖 Agent 操作中”，
    结束后恢复。与 shell 不同：会话状态（cd / 激活的环境 / ssh 连接）在多次调用间保留。
    底层用哨兵标记判定命令结束。timeout 为最长等待秒数（默认 60）。
    """
    import time as _time
    import re as _re
    import uuid as _uuid

    timeout = int(timeout) if timeout and int(timeout) > 0 else 60
    token = _uuid.uuid4().hex[:12]

    base = _term_api("GET", f"/api/term/{id}/output?since=0")
    if "error" in base:
        return ToolResult(success=False, output=None, error=base["error"])
    since = base.get("seq", 0)

    _term_api("POST", f"/api/term/{id}/owner", {"who": "agent"})
    try:
        # Token comes BEFORE the exit code: in PowerShell `$LASTEXITCODE:foo`
        # parses as the $scope:var syntax and swallows both, so the exit-code
        # variable must not be immediately followed by `:literal`. Braces on
        # ${LASTEXITCODE} keep it parsing cleanly.
        if os.name == "nt":
            wrapped = f'{command}; Write-Output "<<QEVOS_DONE:{token}:${{LASTEXITCODE}}>>"\r'
        else:
            wrapped = f'{command}; echo "<<QEVOS_DONE:{token}:$?>>"\r'
        snd = _term_api("POST", f"/api/term/{id}/input", {"data": wrapped})
        if "error" in snd:
            return ToolResult(success=False, output=None, error=snd["error"])

        marker = _re.compile(r"<<QEVOS_DONE:" + token + r":(-?\d*)>>")
        deadline = _time.time() + timeout
        acc = ""
        code = None
        done = False
        while _time.time() < deadline:
            r = _term_api("GET", f"/api/term/{id}/output?since={since}")
            if "error" in r:
                return ToolResult(success=False, output=None, error=r["error"])
            if r.get("data"):
                acc += r["data"]
                since = r.get("seq", since)
            m = marker.search(acc)
            if m:
                code = int(m.group(1)) if m.group(1) not in (None, "") else None
                acc = acc[:m.start()]
                done = True
                break
            if not r.get("alive", True):
                break
            _time.sleep(0.4)

        clean = _strip_ansi(acc)
        # Drop the echoed wrapped-command line (it contains the unique token).
        clean = "\n".join(ln for ln in clean.split("\n") if token not in ln).strip("\n")
        if not done:
            return ToolResult(
                success=False,
                output={"output": clean, "exit_code": None, "timed_out": True},
                error="命令超时未完成（终端仍在运行，可用 terminal_read 继续观察）",
            )
        return ToolResult(success=True, output={"output": clean, "exit_code": code})
    finally:
        _term_api("POST", f"/api/term/{id}/owner", {"who": "user"})


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
            name="compress_context",
            description=(
                "主动压缩上下文历史，腾出 token 空间。"
                "执行长任务、切换阶段、或感知到对话历史很长时使用。"
                "压缩前会自动调用模型对即将丢弃的历史生成结构化摘要并写入草稿本，"
                "保留关键发现/进度/决策，丢弃低价值的执行噪声，"
                "比系统自动兜底压缩（纯机械裁剪）保留更多有效信息。"
                "也可通过 summary 参数自行提供摘要。"
            ),
            args_schema={
                "summary": "（可选）自行提供的摘要，优先于自动 LLM 摘要，写入草稿本作为替代记录",
                "use_llm_summary": "（可选，默认 true）是否允许自动调用模型生成摘要；设为 false 则退化为纯机械裁剪",
            },
            fn=tool_compress_context,
        ),
        ToolSpec(
            name="recall_history",
            description=(
                "回查被压缩封存的原始执行记录。"
                "当交接文档/草稿本里某处细节不够、需要核对早先究竟发生了什么时使用。"
                "默认只返回当前段（最后一次压缩之后）最近若干条原始记录；"
                "可用 query 关键词检索，或用 seg 指定历史段号（seg=-2 跨全部段检索）。"
            ),
            args_schema={
                "last_n": "（可选，默认12）返回最近 N 条原始记录",
                "query": "（可选）关键词，在选定范围内子串匹配",
                "seg": "（可选，默认-1=当前段）指定段号(0起)；-2=跨所有段检索",
            },
            fn=tool_recall_history,
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
            name="terminal_run",
            description=(
                "在【用户可见、人机共用】的内置终端会话里执行一条命令并等待结束，返回 {output, exit_code}。"
                "与 shell 的区别：会话状态（cd / 激活的虚拟环境 / ssh 连接等）在多次调用间保留，"
                "且执行过程用户能实时看到、随时接管；期间该终端会变色标注“Agent 操作中”。"
                "需要持久会话或希望用户旁观/协作时用它；一次性私有命令用 shell。"
                "没有可用会话时先用 terminal_open 新建（用户界面会自动出现该终端）。"
            ),
            args_schema={
                "id": "终端会话 id（来自 terminal_list / terminal_open）",
                "command": "要执行的命令字符串",
                "timeout": "（可选）最长等待秒数，默认 60",
            },
            fn=tool_terminal_run,
        ),
        ToolSpec(
            name="terminal_open",
            description=(
                "新建一个共享终端会话，用户界面会自动出现对应 Tab。"
                "返回 {id, title, port, registered}：port 为实际命中的 dashboard 端口，"
                "registered 表示会话已登记在册。建完会二次核对，未登记（服务重启/端口错配）时不报成功。"
            ),
            args_schema={"title": "（可选）终端标题，默认 'Agent'"},
            fn=tool_terminal_open,
        ),
        ToolSpec(
            name="terminal_list",
            description="列出当前所有内置终端会话（id/标题/cwd/owner/是否存活），用于发现用户已打开的终端。",
            args_schema={},
            fn=tool_terminal_list,
        ),
        ToolSpec(
            name="terminal_send",
            description=(
                "向共享终端会话输入文本（submit=True 自动追加回车），不等待输出立即返回。"
                "适合交互式应答：提示符、密码、REPL/ssh 会话喂输入。需要看结果配合 terminal_read。"
            ),
            args_schema={
                "id": "终端会话 id",
                "text": "要输入的文本",
                "submit": "（可选）是否自动回车提交，默认 true",
            },
            fn=tool_terminal_send,
        ),
        ToolSpec(
            name="terminal_read",
            description="读取共享终端会话自 since 偏移以来的输出（已去 ANSI）。返回 {output, seq, owner, alive}；seq 可作下次 since 实现增量读。",
            args_schema={
                "id": "终端会话 id",
                "since": "（可选）起始偏移，默认 0（从头读）；传上次返回的 seq 可只取新输出",
            },
            fn=tool_terminal_read,
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
            name="web_search",
            description=(
                "使用 DuckDuckGo 搜索引擎进行网络搜索。"
                "返回标题、URL 和摘要。适合查找技术文档、解决方案、最新资讯等。"
            ),
            args_schema={
                "query": "搜索关键词（字符串）",
                "max_results": "（可选）最多返回结果数，默认 5，最大 20",
            },
            fn=tool_web_search,
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
            name="request_advisor",
            description=(
                "主动请求高级指导员在下一轮立即介入，提供独立视角的战略性审视意见。"
                "当你感到迷失方向、陷入局部思维、或需要外部视角时使用。"
            ),
            args_schema={
                "reason": "请求指导的原因（可选）：例如 '完成了阶段一，想确认方向' 或 '对下一步没有把握'",
            },
            fn=tool_request_advisor,
        ),
        ToolSpec(
            name="consult_advisor",
            description=(
                "向「顾问模型」咨询复杂问题，获取来自更强模型的独立专业意见。"
                "顾问 1/2 在看板「设置 → LLM 服务 → 顾问模型1/2」配置（存 .env 的 ADVISOR1/2_OPENAI_*），"
                "仅供本工具按需调用，不参与主备 fallback。兼容 OpenAI / 本地模型 / OpenAI 兼容代理；"
                "Anthropic 填 https://api.anthropic.com/v1/ + Anthropic key + claude-* 即可"
                "（命中 anthropic.com 时自动改用原生 Anthropic SDK）。"
                "与 request_advisor 不同：本工具是带着具体问题去问外部更强模型，而非触发内部高级指导员。"
            ),
            args_schema={
                "question": "要咨询的问题（字符串，必填）",
                "advisor": "顾问编号 1 或 2（可选，默认 1）",
                "model": "模型名称（可选，覆盖该顾问在 .env 中配置的模型）",
                "max_tokens": "最大输出 token 数（可选，默认 4096）",
            },
            fn=tool_consult_advisor,
        ),
        ToolSpec(
            name="ask_user",
            description=(
                "向人类提问并暂停本次运行，等待用户回复后继续。"
                "支持两种输入渠道：命令行直接输入，或 WEB 页面聊天框（用户发消息后会自动注入）。"
                "在 WEB 展示场景下，调用本工具是让 agent 进入等待交互状态的标准方式。"
            ),
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
                "将宏观工作记忆写入 Markdown 文件，并同步注入当前 system prompt。"
                "内容按工作方向分章节，每条精简一句话、提及关键词，不写具体流程。"
                "更新时先用 read_concept 读取旧内容，修改后整体覆盖写入。"
            ),
            args_schema={
                "path": "宏观工作记忆文件路径（如 ./memory_macro.md）",
                "content": "完整 Markdown 内容，按工作方向分 ## 章节，每条一句话精简叙述",
            },
            fn=tool_save_concept,
        ),
        ToolSpec(
            name="read_concept",
            description=(
                "读取宏观工作记忆文件并加载到 state，使其注入后续 system prompt。"
                "在任务开始时调用，获取当前的工作方向全景。"
            ),
            args_schema={"path": "宏观工作记忆文件路径（如 ./memory_macro.md）"},
            fn=tool_read_concept,
        ),
        ToolSpec(
            name="persist_runtime_patches",
            description=(
                "将本次运行中自动积累的 JSON 格式规范写入 AGENTS.md，持久化供后续运行使用。"
                "通常在任务结束前调用一次即可；无规则时自动跳过。"
            ),
            args_schema={
                "path": "（可选）AGENTS.md 的路径，默认 ./AGENTS.md",
            },
            fn=tool_persist_runtime_patches,
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
        # ── 环境观察器(Watchers)──────────────────────────────────────────────
        ToolSpec(
            name="watch_register",
            description=(
                "【环境感知】注册一个观察器(watcher),由框架在每轮迭代开头按 interval 触发执行,"
                "输出自动注入 LLM 上下文(单条硬上限 500 字符,超限自动落 artifacts/ 降级为路径)。\n"
                "代码文件(.py 或 .sh,绝对路径,任意位置)是纯逻辑;interval/params/enabled 等配置由注册表管理。\n"
                ".py 需定义 run(prev, store, iter_n)->Optional[dict],返回 None / "
                "{'type':'text','content':...} / {'type':'image','image_block':{...}} / {'type':'path','path':...}。\n"
                ".sh 的 stdout 当 text 内容;非零退出码视为失败(注入错误提示)。"
            ),
            args_schema={
                "name":     "唯一标识(同名覆盖)",
                "path":     "代码文件绝对路径(.py 或 .sh)",
                "interval": "(可选)触发间隔秒数,默认 10",
                "emit":     "(可选)event(默认,写 short_term 永久) | live(实时面板,暂未启用)",
                "params":   "(可选)注入给代码的参数 dict;代码通过 store['params'] 读取",
                "enabled":  "(可选)是否启用,默认 true",
                "desc":     "(可选)描述",
            },
            fn=tool_watch_register,
        ),
        ToolSpec(
            name="watch_unregister",
            description="注销一个 watcher(代码文件不会被删除,如需保留可改用 watch_disable)",
            args_schema={"name": "要注销的 watcher 名称"},
            fn=tool_watch_unregister,
        ),
        ToolSpec(
            name="watch_enable",
            description="启用一个已注册的 watcher",
            args_schema={"name": "watcher 名称"},
            fn=tool_watch_enable,
        ),
        ToolSpec(
            name="watch_disable",
            description="禁用一个 watcher(注册项保留,只是不再被调度;便于复用)",
            args_schema={"name": "watcher 名称"},
            fn=tool_watch_disable,
        ),
        ToolSpec(
            name="watch_update",
            description="更新一个 watcher 的字段(只传需要改的)",
            args_schema={
                "name":     "watcher 名称",
                "interval": "(可选)新的触发间隔秒数",
                "emit":     "(可选)event | live",
                "params":   "(可选)新的参数 dict",
                "enabled":  "(可选)是否启用",
                "desc":     "(可选)新的描述",
                "path":     "(可选)新的代码文件绝对路径",
            },
            fn=tool_watch_update,
        ),
        ToolSpec(
            name="watch_list",
            description="列出当前所有已注册的 watcher 及其配置/状态",
            args_schema={},
            fn=tool_watch_list,
        ),
        ToolSpec(
            name="wait_for_job",
            description=(
                "进入轻量等待模式，直到指定后台任务完成后再继续执行。\n"
                "调用后框架跳过 LLM 调用、每隔 check_interval 秒检查一次，\n"
                "任务完成时自动将结果注入上下文并恢复。\n"
                "适用于启动任务后暂时没有其他工作可做的场景，避免产生轮询循环。"
            ),
            args_schema={
                "job_id":         "由 shell_bg 返回的任务 ID",
                "check_interval": "（可选）检查间隔秒数，默认 15，建议 10~60",
            },
            fn=tool_wait_for_job,
        ),
        # ── Dashboard Files Tab ───────────────────────────────────────────────
        ToolSpec(
            name="file_tab",
            description=(
                "管理 Dashboard Files 面板的目录 Tab，实现对本地任意目录的浏览。\n"
                "action 可选值：\n"
                "  open  — 在 Files 面板打开指定路径的目录 Tab（path 必填，label 可选）。"
                "若该路径已存在 Tab 则只切换激活，不重复添加。\n"
                "  close — 关闭指定路径的 Tab（path 必填）。\n"
                "  list  — 列出当前所有 Tab 及其路径，用于判断是否需要新开。\n"
                "【使用规范】打开前先调用 list 确认目标路径尚未开启，避免重复添加。"
            ),
            args_schema={
                "action": "操作类型：open / close / list",
                "path":   "（open/close 必填）目录的绝对路径，如 E:/workspace/project",
                "label":  "（open 可选）Tab 显示名称，默认取路径最后一段",
            },
            fn=tool_file_tab,
        ),
        # ── WEB 展示工具 ──────────────────────────────────────────────────────
        ToolSpec(
            name="web_show",
            description=(
                "在浏览器 WEB 页面中展示内容（图表、表格、HTML、Markdown 等）。"
                "返回可访问的 URL，用户打开后实时接收更新，无需刷新。"
                "支持多个独立展示面板（display_id），每个面板可独立更新。**必须**自动弹出以增加用户获得感。\n"
                "【重要交互规范】调用本工具后，用户会停留在 WEB 页面通过下方聊天框与你交互，"
                "因此你必须：① 紧接着调用 web_notify 向用户说明展示内容并邀请他继续对话"
                "（例如：已展示完毕，有什么问题或需要调整的吗？）；"
                "② 然后调用 ask_user 暂停任务，等待用户通过 WEB 聊天框发来下一步指令。"
                "不要在调用 web_show 后立即完成任务（submit_completion_report/done），"
                "除非用户已明确说不需要进一步交互。\n"
                "【内容必须是真实内容，不是文件路径】content 参数要直接传入要渲染的"
                "字符串本身（HTML/Markdown/JSON 文本），**绝不要**只传一个文件路径"
                "（如 runs/xxx/foo.html）；若内容已在磁盘文件里，先用 read_file 读出再传入。"
                "（兜底：即便误传了路径，工具会尝试自动读取，但不应依赖此行为。）\n"
                "【图片等资源用相对路径】HTML/Markdown 内引用的图片、CSS、JS 等资源"
                "必须使用相对 run 目录的相对路径（如 artifacts/loss.png），或 http(s)/"
                "data: URL；**绝不要**写本机绝对路径（如 E:/... 或 /home/...），否则前端无法加载。"
            ),
            args_schema={
                "content": (
                    "要展示的【真实内容字符串】（HTML/Markdown/JSON 文本等），"
                    "不是文件路径；磁盘文件请先 read_file 读出。"
                    "内部引用的图片用相对路径（artifacts/x.png）或 data: URL，勿用绝对路径。"
                ),
                "content_type": (
                    "内容类型：\n"
                    "  - html: HTML 片段（无 <html>/<body> 标签），适合简单布局；"
                    "若需完整交互页面（游戏/复杂 JS），直接传入完整 <!DOCTYPE html> 文档也支持，会在 iframe 中隔离渲染\n"
                    "  - markdown: Markdown 文本，自动渲染\n"
                    "  - table: JSON 数组（如 [{列名:值,...}]），自动渲染为表格\n"
                    "  - chart: ECharts option 的 JSON 字符串，自动渲染图表\n"
                    "  - text: 纯文本/代码，等宽字体显示\n"
                    "  - image: 图片 URL 或 base64"
                ),
                "display_id": "（可选）展示面板 ID，默认 'default'；同一 run 内可有多个独立面板",
                "title": "（可选）面板标题",
                "mode": "（可选）replace（覆盖，默认）| append（追加，适合流式输出）",
            },
            fn=tool_web_show,
        ),
        ToolSpec(
            name="web_notify",
            description=(
                "向 WEB 页面的悬浮聊天框推送一条消息，让用户在浏览器内看到 agent 的通知或问题。"
                "配合 web_show 使用：展示内容后用 web_notify 告知用户去查看或回答问题。"
            ),
            args_schema={
                "message": "要推送给用户的消息文本",
                "display_id": "（可选）目标面板 ID，'*' 表示推送到所有面板（默认）",
            },
            fn=tool_web_notify,
        ),
        ToolSpec(
            name="web_interact",
            description=(
                "在已打开的浏览器视图中执行自动化操作。\n"
                "Electron 模式：直接控制内嵌标签页，无需额外配置。\n"
                "普通浏览器模式：需以 --remote-debugging-port=9222 启动 Chrome/Edge，"
                "否则工具会返回带有具体启动命令的错误提示。\n"
                "【页面控制】\n"
                "  - new_tab：打开新标签页，payload: {url?, title?}\n"
                "  - navigate：跳转 URL 并等待加载完成，payload: {url}\n"
                "  - eval：执行 JS 并返回结果，payload: {code}\n"
                "  - get_html：获取页面完整 HTML，payload: {}\n"
                "  - screenshot：截图并直接注入视觉上下文（LLM 下一轮即可看到图像），payload: {}\n"
                "    inject=False 时改为只返回 base64 数据，不注入视觉上下文\n"
                "【元素交互（基于 CSS 选择器）】\n"
                "  - click：点击元素，payload: {selector}\n"
                "  - fill：填写普通 input/textarea，payload: {selector, value}\n"
                "【原生输入（可绕过 React/Vue 等框架）】\n"
                "  - key_type：向已聚焦元素输入文字（原生键盘注入，适合 contenteditable），payload: {text}\n"
                "  - key_press：按下特殊键，payload: {key}，支持的键名：\n"
                "      Enter / Tab / Escape / Backspace / Delete /\n"
                "      ArrowUp / ArrowDown / ArrowLeft / ArrowRight /\n"
                "      Home / End / PageUp / PageDown / Space\n"
                "【鼠标操作（基于坐标）】\n"
                "  - mouse_move：移动鼠标到坐标，payload: {x, y}\n"
                "  - mouse_click：原子点击（down+up），payload: {x, y, button?('left'/'right'/'middle'), count?(1=单击/2=双击)}\n"
                "  - mouse_down：仅按下不抬起（长按/拖拽起点），payload: {x, y, button?}\n"
                "  - mouse_up：抬起（配合 mouse_down 实现长按），payload: {x, y, button?}\n"
                "  - drag：拖拽，payload: {x1, y1, x2, y2, steps?(默认10), button?}\n"
                "  - scroll：在坐标处滚动，payload: {x, y, deltaX?, deltaY?}\n"
                "【键盘操作】\n"
                "  - key_combo：组合键，payload: {key, modifiers(['ctrl','shift','alt','meta'])}\n"
                "    示例：Ctrl+A 全选={key:'A',modifiers:['ctrl']}，Ctrl+Enter 发送\n"
                "【典型用法】\n"
                "  截图确认：screenshot → 直接看到图（inject 默认 True，无需 load_image）\n"
                "  光标确认：mouse_click → screenshot → 在图中找 #CODE 标签确认位置\n"
                "  长按：mouse_down → eval(sleep) → mouse_up\n"
                "  拖拽排序：drag {x1,y1,x2,y2,steps:20}\n"
                "  React输入框：mouse_click聚焦 → key_type输入 → key_press Enter\n"
                "  全选复制：key_combo{key:'A',modifiers:['ctrl']} → key_combo{key:'C',modifiers:['ctrl']}"
            ),
            args_schema={
                "action": "操作类型（见描述）",
                "display_id": "（可选）目标视图 ID，对应 web_show 的 display_id，默认 'default'",
                "payload": "（可选）操作参数对象，不同 action 所需字段见描述",
                "inject": "（可选，仅对 screenshot 有效）True（默认）：截图直接注入视觉上下文；False：只返回 base64 数据",
            },
            fn=tool_web_interact,
        ),
        # ── 模型能力动态控制 ─────────────────────────────────────────────────
        ToolSpec(
            name="set_thinking_budget",
            description=(
                "动态开启或关闭 extended thinking（深度推理）模式，并设置 token 预算。\n"
                "适合在遇到复杂推理、数学证明、多步规划等任务时临时开启，"
                "完成后可再次调用关闭以节省 token。\n"
                "budget=0 关闭；budget>0（建议 4000~16000）开启。"
            ),
            args_schema={
                "budget": "thinking token 预算（整数）。0=关闭，建议范围 4000~16000",
            },
            fn=tool_set_thinking_budget,
        ),
        ToolSpec(
            name="load_image",
            description=(
                "将本地图片文件或远程图片 URL 加载到对话上下文，使 LLM 在下一轮能直接分析图片内容。\n"
                "适用场景：分析截图、识别图表/表格、检查设计稿、读取扫描件等。\n"
                "支持 jpg/png/gif/webp/bmp/tiff/ico 等常见格式（自动转换为 PNG/JPEG）；本地路径支持相对路径（相对于当前工作目录）。"
            ),
            args_schema={
                "path": "图片路径（本地文件路径或 http/https URL）",
                "caption": "（可选）图片说明文字，会作为图片前的提示注入上下文",
            },
            fn=tool_load_image,
        ),
        ToolSpec(
            name="load_video",
            description=(
                "从本地视频文件中均匀抽取关键帧，注入多模态上下文供 LLM 逐帧分析。\n"
                "适用场景：分析录屏操作步骤、检查视频中的界面/图表、理解动态过程等。\n"
                "支持 mp4/avi/mov/mkv/webm 等主流格式（依赖 opencv-python）。\n"
                "对长视频建议两步走：先用大 interval 概览全片，再用 start_time/end_time 锁定片段精细分析。\n"
                "默认每 2 秒抽一帧，最多 16 帧；超出时自动稀疏以覆盖完整时间段。"
            ),
            args_schema={
                "path": "视频文件路径（本地路径，支持相对路径）",
                "interval": "（可选）抽帧间隔秒数，默认 2.0",
                "max_frames": "（可选）最大帧数上限，默认 16",
                "start_time": "（可选）分析起始时间（秒），默认 0",
                "end_time": "（可选）分析结束时间（秒），默认 -1 表示视频结尾",
                "caption": "（可选）整段视频的说明文字，会作为第一条消息注入上下文",
            },
            fn=tool_load_video,
        ),
        # ── SSH 工具 ──────────────────────────────────────────────────────────
        ToolSpec(
            name="ssh_execute",
            description="通过 SSH 在远程服务器执行命令。支持密码/密钥认证、sudo 密码注入、严格 timeout 和 /stop 中断。",
            args_schema={
                "host": "远程服务器主机地址或 IP",
                "port": "SSH 端口，默认 22",
                "username": "登录用户名",
                "password": "登录密码（用于密码认证）",
                "command": "要在远程服务器执行的命令",
                "timeout": "命令执行超时时间（秒），默认 30",
                "key_file": "SSH 私钥文件路径（用于密钥认证，与 password 二选一）",
                "sudo_password": "sudo 密码（可选）。若指定，会自动在命令前注入 echo pwd | sudo -S 前缀",
            },
            fn=tool_ssh_execute,
        ),
        # ── 环境信息工具 ──────────────────────────────────────────────────────
        ToolSpec(
            name="get_env_info",
            description="获取当前运行环境的基本信息：当前日期与时间、当前工作目录。在任务开始时调用以了解所处环境。",
            args_schema={},
            fn=tool_get_env_info,
        ),
        # ── SKILL 工具 ────────────────────────────────────────────────────────
        ToolSpec(
            name="list_skills",
            description=(
                "列出 SKILLS/ 目录中所有可用的领域技能文件。"
                "每个技能文件包含特定领域的操作规范和最佳实践。"
                "任务开始时可调用此工具了解有哪些可用技能规范。"
            ),
            args_schema={},
            fn=tool_list_skills,
        ),
        ToolSpec(
            name="read_skill",
            description=(
                "读取指定领域技能文件的完整内容。"
                "技能文件包含针对该领域的操作规范、工具偏好和输出标准。"
                "当前任务与某领域高度相关时，读取对应技能文件获取专业指导。"
            ),
            args_schema={"name": "技能名称（SKILLS/ 目录下的文件名，不含 .md 后缀，如 'coding'、'data_analysis'）"},
            fn=tool_read_skill,
        ),
        ToolSpec(
            name="register_app",
            description=(
                "把一段脚本固化成 Dashboard 'Apps' Tab 里的一个可点击程序。"
                "适用于已经收敛、确定性强的重复任务——固化后用户（或你自己）一键即可运行，"
                "无需再启动 Agent/调模型，又快又省。"
            ),
            args_schema={
                "name": "程序名（也用于派生文件名）",
                "description": "一句话说明用途",
                "runtime": "运行时：'python' | 'powershell' | 'shell'",
                "script": "脚本正文（纯代码，不要带 ``` 围栏）",
                "icon": "（可选）一个 emoji 图标，默认 📦",
            },
            fn=tool_register_app,
        ),
        ToolSpec(
            name="list_apps",
            description="列出 Apps Tab 里所有已注册的可执行程序。",
            args_schema={},
            fn=tool_list_apps,
        ),
        ToolSpec(
            name="run_app",
            description="运行一个已注册的可执行程序（按 id 或名称），直接执行其脚本并返回输出。",
            args_schema={"name": "程序 id 或名称（apps/ 目录下的文件名，不含 .md 后缀）"},
            fn=tool_run_app,
        ),
        ToolSpec(
            name="panel_poll",
            description=(
                "被动读取某 UI App（runtime:web）面板经 qevos.emit 写入的结构化事件日志。"
                "⚠️ 近期为预留能力：面板与 Agent 尚未接入（待子 Agent），"
                "**仅在用户显式要求你去查看某面板时才调用，切勿主动/循环轮询面板**——"
                "主动轮询会扰动主 Agent 运行。是只读操作，不会触发任何面板动作。详见 SKILLS/ui_app.md。"
            ),
            args_schema={
                "app": "App id（apps/<id>.md 的 id）",
                "since": "（可选）只返回 ts 大于该毫秒时间戳的事件",
                "consume": "（可选）读取后清空事件日志；增量轮询请改用 since",
                "root": "（可选）项目文件夹绝对路径；给了则读 <root>/.qevos/，否则 app-data/<id>/",
            },
            fn=tool_panel_poll,
        ),
        ToolSpec(
            name="panel_control",
            description=(
                "操控/检查一个已打开的 UI App（runtime:web）面板——第一方通道，"
                "**无需 CDP/调试浏览器**，Electron 与普通浏览器模式一致。用于给 App 写自动化测试、"
                "或按用户要求操控其正打开的面板。要求该 App 面板当前已打开。"
                "断言优先仍读文件态（panel_poll/文件工具），本工具补 DOM 交互与读取。"
                "action: click/fill/value/getText/getHtml/exists/count/waitFor/eval。"
            ),
            args_schema={
                "app": "App id（apps/<id>.md 的 id）",
                "action": "click|fill|value|getText|getHtml|exists|count|waitFor|screenshot|eval",
                "selector": "（多数 action 需要；screenshot 可选，缺省截整个面板）CSS 选择器",
                "value": "（fill 用）要填入的值",
                "code": "（eval 用）在面板内求值的表达式，返回可 JSON 序列化的值",
                "root": "（可选）项目文件夹绝对路径，同 openProject 的 root",
                "timeout": "（可选）毫秒；waitFor 等待上限 / 整体超时",
                "inject": "（screenshot 用，默认 true）把图像直接注入对话上下文供你直接查看",
            },
            fn=tool_panel_control,
        ),
    ]
    tools = {s.name: s for s in specs}
    tools.update(_load_pro_tools())
    return tools


# ── PRO extension point (tool auto-discovery) ────────────────────────────────
# Closed-source PRO builds may drop an `agent/pro/tools.py` exposing
# `get_pro_tools() -> dict[str, ToolSpec]`. When the module is absent
# (open-source build) this is a no-op and the standard tool set is unchanged.
def _load_pro_tools() -> dict[str, "ToolSpec"]:
    try:
        from agent.pro.tools import get_pro_tools  # type: ignore
    except Exception:
        return {}
    try:
        extra = get_pro_tools() or {}
        return {name: spec for name, spec in extra.items() if name}
    except Exception:
        return {}
