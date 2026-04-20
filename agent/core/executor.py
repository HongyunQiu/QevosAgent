"""
工具执行器
职责：安全地执行工具函数，捕获异常，返回标准化的 ToolResult。
不关心工具的具体逻辑——那是各工具自己的事。
"""

from .types_def import Action, AgentState, ToolResult


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
            error=f"工具 '{tool_name}' 不存在。当前可用工具: {available}"
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
            error=f"工具参数错误: {e}{hint}"
        )
    except Exception as e:
        return ToolResult(
            success=False,
            output=None,
            error=f"工具执行异常: {type(e).__name__}: {e}"
        )
