"""
智能体主循环
这是整个系统最核心的文件。
LOOP: 感知 → 思考 → 行动 → 反思 → 重复
"""

import json
from dataclasses import dataclass
from typing import Optional, Callable

from .types import Action, ActionType, AgentState, ToolSpec, ToolResult
from .llm import LLMBackend, build_system_prompt, build_context_messages, parse_response
from .executor import execute


# ── 回调钩子（用于观测/调试，不影响核心逻辑）────────────────────────────────

@dataclass
class AgentHooks:
    on_iteration_start: Optional[Callable[[int, AgentState], None]] = None
    on_thought:         Optional[Callable[[str], None]] = None
    on_tool_call:       Optional[Callable[[str, dict], None]] = None
    on_tool_result:     Optional[Callable[[ToolResult], None]] = None
    on_done:            Optional[Callable[[str], None]] = None
    on_error:           Optional[Callable[[str], None]] = None


# ── 默认钩子：打印到控制台 ────────────────────────────────────────────────────

def console_hooks() -> AgentHooks:
    """开箱即用的控制台输出钩子，开发调试用。"""
    CYAN   = "\033[96m"
    YELLOW = "\033[93m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    GRAY   = "\033[90m"
    RESET  = "\033[0m"

    def on_iter(i, state):
        print(f"\n{GRAY}{'─'*60}{RESET}")
        print(f"{GRAY}[迭代 {i}]  工具数: {len(state.tools)}  长期记忆: {len(state.long_term)} 条{RESET}")

    def on_thought(t):
        print(f"{CYAN}💭 思考: {t}{RESET}")

    def on_tool(name, args):
        args_str = json.dumps(args, ensure_ascii=False)
        print(f"{YELLOW}🔧 调用工具: {name}({args_str}){RESET}")

    def on_result(r):
        icon = "✅" if r.success else "❌"
        text = r.to_str()
        # 截断过长输出
        if len(text) > 500:
            text = text[:500] + "...[截断]"
        print(f"{icon} 结果: {text}")

    def on_done(ans):
        print(f"\n{GREEN}{'='*60}{RESET}")
        print(f"{GREEN}✨ 完成！{RESET}")
        print(f"{GREEN}{ans}{RESET}")
        print(f"{GREEN}{'='*60}{RESET}")

    def on_error(msg):
        print(f"{RED}⚠️  错误: {msg}{RED}")

    return AgentHooks(
        on_iteration_start=on_iter,
        on_thought=on_thought,
        on_tool_call=on_tool,
        on_tool_result=on_result,
        on_done=on_done,
        on_error=on_error,
    )


# ── 主循环 ────────────────────────────────────────────────────────────────────

def run(
    goal: str,
    llm: LLMBackend,
    tools: dict[str, ToolSpec],
    long_term: Optional[list[str]] = None,
    max_iterations: int = 30,
    hooks: Optional[AgentHooks] = None,
) -> AgentState:
    """
    启动智能体主循环。

    参数:
        goal          - 自然语言描述的目标
        llm           - LLM 后端实例
        tools         - 初始工具集 {name: ToolSpec}
        long_term     - 预置的长期记忆（可选，用于跨次运行恢复经验）
        max_iterations - 安全阀，防止无限循环
        hooks         - 观测回调（不影响核心逻辑）

    返回:
        最终的 AgentState（包含完整历史，可用于持久化）
    """
    if hooks is None:
        hooks = AgentHooks()  # 静默模式（无输出）

    # 初始化状态
    state = AgentState(
        goal=goal,
        tools=dict(tools),  # 复制一份，允许运行时修改（进化）
        long_term=list(long_term or []),
    )

    # 初始用户消息
    state.short_term.append({
        "role": "user",
        "content": f"请完成以下目标：\n\n{goal}"
    })

    # ── LOOP ─────────────────────────────────────────────────────────────────
    while state.iteration < max_iterations:

        # 回调：迭代开始
        if hooks.on_iteration_start:
            hooks.on_iteration_start(state.iteration, state)

        # 1. 构建当前 system prompt（工具集可能已进化，每次重新生成）
        system = build_system_prompt(state.tools, state.long_term)

        # 2. 构建消息上下文
        messages = build_context_messages(state)

        # 3. 调用 LLM
        try:
            raw_response = llm.complete(messages, system)
        except Exception as e:
            error_msg = f"LLM 调用失败: {e}"
            if hooks.on_error:
                hooks.on_error(error_msg)
            # 写入错误历史，让 LLM 下次知情
            state.short_term.append({
                "role": "user",
                "content": f"[系统] LLM调用异常: {e}，请重试或换一种方式。"
            })
            state.iteration += 1
            continue

        # 4. 解析动作
        action = parse_response(raw_response)

        # 回调：输出思考
        if hooks.on_thought and action.thought:
            hooks.on_thought(action.thought)

        # 5. 把 LLM 响应加入短期记忆
        state.short_term.append({
            "role": "assistant",
            "content": raw_response,
        })

        # 6. 根据动作类型分支处理
        if action.type == ActionType.DONE:
            if hooks.on_done:
                hooks.on_done(action.final_answer or "（无最终输出）")
            state.meta["final_answer"] = action.final_answer
            break

        elif action.type == ActionType.ERROR:
            error_msg = action.thought
            if hooks.on_error:
                hooks.on_error(error_msg)
            # 把错误反馈给 LLM，让它自我修正
            state.short_term.append({
                "role": "user",
                "content": f"[系统] 输出格式错误，请严格按照 JSON 格式重新回复。错误详情: {error_msg}"
            })
            state.iteration += 1
            continue

        elif action.type == ActionType.TOOL_CALL:
            if hooks.on_tool_call:
                hooks.on_tool_call(action.tool, action.args)

            # 执行工具
            result = execute(action, state)

            if hooks.on_tool_result:
                hooks.on_tool_result(result)

            # 把执行结果反馈给 LLM（作为下一轮的 user 消息）
            feedback = _build_feedback(action, result)
            state.short_term.append({
                "role": "user",
                "content": feedback,
            })

        state.iteration += 1

    else:
        # 超出最大迭代次数
        if hooks.on_error:
            hooks.on_error(f"达到最大迭代次数 {max_iterations}，强制退出。")
        state.meta["timeout"] = True

    return state


# ── 内部辅助 ──────────────────────────────────────────────────────────────────

def _build_feedback(action: Action, result: ToolResult) -> str:
    """构建工具执行结果的反馈消息。"""
    if result.success:
        return (
            f"[工具: {action.tool}] 执行成功\n"
            f"输出:\n{result.to_str()}"
        )
    else:
        return (
            f"[工具: {action.tool}] 执行失败\n"
            f"错误: {result.error}\n"
            f"请分析原因，调整策略后重试（可换用其他工具或修改参数）。"
        )
