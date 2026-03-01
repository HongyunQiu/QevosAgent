"""
内置标准工具集
这些工具构成通用智能体的"标准装备"。
所有工具遵循统一签名：fn(state: AgentState, **kwargs) -> ToolResult
"""

import os
import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from ..core.types import AgentState, ToolSpec, ToolResult


# ── 工具函数实现 ──────────────────────────────────────────────────────────────

def tool_remember(state: AgentState, content: str) -> ToolResult:
    """把重要结论写入长期记忆。"""
    if not content or not content.strip():
        return ToolResult(success=False, output=None, error="content 不能为空")
    state.long_term.append(content.strip())
    return ToolResult(
        success=True,
        output=f"已记录（当前长期记忆共 {len(state.long_term)} 条）"
    )


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
    """把内容写入文件（自动创建父目录）。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output=f"已写入 {p.resolve()}（{len(content)} 字符）"
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_read_file(state: AgentState, path: str) -> ToolResult:
    """读取文件内容。"""
    try:
        content = Path(path).read_text(encoding="utf-8")
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

    # 在隔离命名空间中执行代码
    namespace: dict[str, Any] = {"ToolResult": ToolResult}
    try:
        exec(textwrap.dedent(python_code), namespace)
    except SyntaxError as e:
        return ToolResult(success=False, output=None, error=f"代码语法错误: {e}")
    except Exception as e:
        return ToolResult(success=False, output=None, error=f"代码定义错误: {e}")

    run_fn = namespace.get("run")
    if run_fn is None or not callable(run_fn):
        return ToolResult(
            success=False,
            output=None,
            error="python_code 必须定义一个名为 `run` 的函数。"
        )

    # 包装成符合工具签名的函数
    def make_wrapper(fn):
        def wrapper(state: AgentState, **kwargs) -> ToolResult:
            return fn(state, **kwargs)
        wrapper.__name__ = name
        return wrapper

    state.tools[name] = ToolSpec(
        name=name,
        description=description,
        args_schema=args_schema,
        fn=make_wrapper(run_fn),
        is_evolve_tool=False,
    )

    # 把这次进化记录到长期记忆
    state.long_term.append(
        f"[工具进化] 注册了新工具 '{name}': {description}"
    )

    return ToolResult(
        success=True,
        output=f"工具 '{name}' 注册成功！现在你可以使用它了。"
    )


# ── 工具集构建器 ──────────────────────────────────────────────────────────────

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
