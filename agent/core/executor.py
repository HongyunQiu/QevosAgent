"""
工具执行器
职责：安全地执行工具函数，捕获异常，返回标准化的 ToolResult。
不关心工具的具体逻辑——那是各工具自己的事。
"""

import difflib
import re

from .types_def import Action, AgentState, ToolResult
from ..i18n import t

# action 字段的合法取值。模型常把它们当成工具名调用——尤其是 done。
_ACTION_TYPES = {"done", "tool_call", "error"}

# 工具描述里声明子动作的写法，例如 web_interact 的：
#   "  - screenshot：截图并直接注入视觉上下文…"
# 全角/半角冒号都算。
_SUBACTION_RE = r"^\s*[-*]\s*{name}\s*[：:]"


def _unknown_tool_hint(tool_name: str, state: AgentState) -> str:
    """
    「工具不存在」有三种成因，每种都有唯一正确的写法。直接说出来，
    而不是只丢一串可用工具让模型再猜一轮——每猜一轮就是一次完整的
    LLM 往返。

    命中不了就返回空串，退回原来的报错。
    """
    # ① 把 action 类型当成了工具名（done 是最常见的一个）
    if tool_name in _ACTION_TYPES:
        return t("exec.hint_action", name=tool_name) + " "

    # ② 把某个工具的子动作当成了顶层工具名（screenshot / click / navigate …）
    pattern = re.compile(_SUBACTION_RE.format(name=re.escape(tool_name)), re.MULTILINE)
    owners = [
        name for name, spec in state.tools.items()
        if getattr(spec, "description", None) and pattern.search(spec.description)
    ]
    if len(owners) == 1:
        return t("exec.hint_subact", name=tool_name, tool=owners[0]) + " "

    # ③ 拼错了
    close = difflib.get_close_matches(tool_name, list(state.tools.keys()), n=3, cutoff=0.7)
    if close:
        return t("exec.hint_close", candidates=" / ".join(close)) + " "

    return ""


def execute(action: Action, state: AgentState) -> ToolResult:
    """
    执行一个 tool_call 动作。
    所有工具函数签名统一为：fn(state: AgentState, **kwargs) -> ToolResult
    这样工具可以读写 state（实现记忆写入、工具注册等进化行为）。
    """
    tool_name = action.tool
    spec = state.tools.get(tool_name)

    if spec is None:
        available = list(state.tools.keys())
        return ToolResult(
            success=False,
            output=None,
            error=t(
                "exec.not_found",
                name=tool_name,
                hint=_unknown_tool_hint(tool_name, state),
                available=available,
            )
        )

    try:
        filtered_args = dict(action.args or {})
        if spec.args_schema:
            allowed = set(spec.args_schema.keys())
            ignored = sorted(k for k in filtered_args.keys() if k not in allowed)
            if ignored:
                filtered_args = {k: v for k, v in filtered_args.items() if k in allowed}
                state.meta.setdefault("ignored_tool_args", []).append({
                    "tool": tool_name,
                    "ignored_args": ignored,
                })

        result = spec.fn(state=state, **filtered_args)
        # 工具函数应返回 ToolResult，但做一层兼容处理
        if isinstance(result, ToolResult):
            return result
        return ToolResult(success=True, output=result)
    except TypeError as e:
        allowed_args = sorted(spec.args_schema.keys()) if getattr(spec, "args_schema", None) else []
        hint = f"；允许参数: {allowed_args}" if allowed_args else ""
        return ToolResult(
            success=False,
            output=None,
            error=t("exec.arg_error", e=e, hint=hint)
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=t("exec.exec_error", etype=type(e).__name__, e=e)
        )
