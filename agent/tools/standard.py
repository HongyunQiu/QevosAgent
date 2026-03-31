"""
内置标准工具集
这些工具构成通用智能体的"标准装备"。
所有工具遵循统一签名：fn(state: AgentState, **kwargs) -> ToolResult
"""

import os
import ast
import json
import subprocess
import textwrap
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
        is_evolve_tool=False,
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


def tool_run_python(state: AgentState, code: str) -> ToolResult:
    """在隔离子进程中执行 Python 代码并返回输出。"""
    try:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
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
        return ToolResult(success=False, output=None, error="执行超时（>15s）")
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


def tool_shell(state: AgentState, command: str) -> ToolResult:
    """执行 shell 命令并返回输出（危险工具，生产环境应加白名单）。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        combined = output
        if stderr:
            combined += f"\n[STDERR]: {stderr}"
        return ToolResult(
            success=(result.returncode == 0),
            output=combined or "（无输出）",
            error=stderr if result.returncode != 0 else None,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, output=None, error="命令超时（>30s）")
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


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


# ── 工具集构建器 ──────────────────────────────────────────────────────────────

def tool_save_snapshot_meta(state: AgentState, path: str) -> ToolResult:
    """Persist long_term + evolved_tools recipes to a JSON snapshot."""
    try:
        evolved_tools = state.meta.get("evolved_tools", {})
        valid_evolved_tools = {}
        invalid_evolved_tools = {}
        for name, rec in evolved_tools.items():
            python_code = rec.get("python_code", "") if isinstance(rec, dict) else ""
            errors = _validate_evolved_tool_python_code(python_code) if python_code else ["缺少 python_code"]
            if errors:
                invalid_evolved_tools[name] = {
                    "errors": errors,
                    "recipe": rec,
                }
                continue
            valid_evolved_tools[name] = rec

        if invalid_evolved_tools:
            state.meta["invalid_evolved_tools"] = invalid_evolved_tools

        payload = {
            "long_term": list(state.long_term),
            "evolved_tools": valid_evolved_tools,
            "tool_repair_candidates": state.meta.get("tool_repair_candidates", {}),
            "tool_repair_failures": state.meta.get("tool_repair_failures", []),
            "tool_repair_history": state.meta.get("tool_repair_history", []),
            "scratchpad": state.meta.get("scratchpad", ""),
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(
            success=True,
            output={
                "path": str(p.resolve()),
                "saved_tools": len(valid_evolved_tools),
                "skipped_invalid_tools": len(invalid_evolved_tools),
                "saved_repair_candidates": len(payload["tool_repair_candidates"]),
            },
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_load_snapshot_meta(state: AgentState, path: str, overwrite: bool = False) -> ToolResult:
    """Load snapshot and re-register evolved tools into state.tools.

    This is an offline restore mechanism that does NOT call the LLM.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ToolResult(success=False, output=None, error="snapshot must be a JSON object")

        long_term = payload.get("long_term", [])
        evolved = payload.get("evolved_tools", {})
        repair_candidates = payload.get("tool_repair_candidates", {})
        # scratchpad is intentionally NOT restored by default to avoid stale/noisy runs.
        # If you want scratchpad persistence, re-enable restoration here.
        if not isinstance(long_term, list) or not all(isinstance(x, str) for x in long_term):
            return ToolResult(success=False, output=None, error="snapshot.long_term must be list[str]")
        if not isinstance(evolved, dict):
            return ToolResult(success=False, output=None, error="snapshot.evolved_tools must be dict")
        if not isinstance(repair_candidates, dict):
            return ToolResult(success=False, output=None, error="snapshot.tool_repair_candidates must be dict")

        state.long_term = list(long_term)
        state.meta["evolved_tools"] = evolved
        state.meta.setdefault("invalid_evolved_tools", {})
        restored_candidates = {}
        restored_candidate_failures = list(payload.get("tool_repair_failures", []))
        for cand_name, cand in repair_candidates.items():
            if not isinstance(cand, dict):
                restored_candidate_failures.append({
                    "name": cand_name,
                    "errors": ["候选修复元数据不是对象"],
                })
                continue
            spec, errors = _materialize_tool_recipe(
                cand.get("name", cand_name),
                cand.get("description", ""),
                cand.get("args_schema", {}),
                cand.get("python_code", ""),
            )
            if spec is None:
                restored_candidate_failures.append({
                    "name": cand_name,
                    "errors": errors,
                })
                continue
            restored = dict(cand)
            restored["validation"] = {"ok": True, "errors": []}
            restored_candidates[cand_name] = restored

        state.meta["tool_repair_candidates"] = restored_candidates
        state.meta["tool_repair_failures"] = restored_candidate_failures
        state.meta["tool_repair_history"] = payload.get("tool_repair_history", [])

        restored = 0
        skipped = 0
        invalid = 0
        for name, rec in evolved.items():
            if not overwrite and name in state.tools:
                skipped += 1
                continue
            # rec: {name, description, args_schema, python_code}
            python_code = rec.get("python_code", "")
            description = rec.get("description", "")
            args_schema = rec.get("args_schema", {})
            if not isinstance(python_code, str) or not python_code.strip():
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {
                    "errors": ["缺少 python_code"],
                    "recipe": rec,
                }
                continue

            validation_errors = _validate_evolved_tool_python_code(python_code)
            if validation_errors:
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {
                    "errors": validation_errors,
                    "recipe": rec,
                }
                continue

            spec, errors = _materialize_tool_recipe(name, description, args_schema, python_code)
            if spec is None:
                invalid += 1
                state.meta["invalid_evolved_tools"][name] = {
                    "errors": errors,
                    "recipe": rec,
                }
                continue

            state.tools[name] = spec
            restored += 1

        return ToolResult(
            success=True,
            output={
                "restored": restored,
                "skipped": skipped,
                "invalid": invalid,
                "repair_candidates": len(repair_candidates),
                "long_term": len(state.long_term),
            },
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


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
            description="执行 shell 命令，适合文件操作、系统查询、调用外部程序等",
            args_schema={"command": "shell 命令字符串"},
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
            description="读取文件内容并返回",
            args_schema={"path": "文件路径（字符串）"},
            fn=tool_read_file,
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
            name="ask_user",
            description="当缺少关键信息时，向人类提问并暂停本次运行，等待命令行输入后继续",
            args_schema={"question": "要向人类询问的问题（字符串）"},
            fn=lambda state, question: ToolResult(success=True, output={"question": question}),
        ),
        ToolSpec(
            name="save_snapshot_meta",
            description="保存长期记忆(state.long_term)和进化工具配方(state.meta['evolved_tools'])到一个 JSON 快照文件",
            args_schema={"path": "快照文件路径（如 ./agent_snapshot_meta.json）"},
            fn=tool_save_snapshot_meta,
        ),
        ToolSpec(
            name="load_snapshot_meta",
            description="从 JSON 快照文件恢复长期记忆，并按配方把进化工具恢复到 state.tools 里（离线恢复，不依赖 LLM）",
            args_schema={
                "path": "快照文件路径",
                "overwrite": "是否覆盖同名已有工具（bool，默认 false）",
            },
            fn=tool_load_snapshot_meta,
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
    ]
    return {s.name: s for s in specs}
