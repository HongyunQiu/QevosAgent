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


def _compact_short_term_messages(state: AgentState, per_message_chars: int = 2000):
    """In-place compacting of overly large messages to prevent context blow-up."""
    if not state.short_term:
        return
    for m in state.short_term:
        c = m.get("content")
        if not isinstance(c, str):
            continue
        if len(c) > per_message_chars:
            m["content"] = _summarize_large_text(c, per_message_chars)


def _trim_short_term(state: AgentState, keep_last: int = 8):
    """Trim short_term history to reduce prompt size.

    Keep the initial user goal message (index 0) plus the last `keep_last` messages.
    """
    if not state.short_term:
        return
    head = state.short_term[:1]
    tail = state.short_term[-keep_last:] if len(state.short_term) > 1 else []
    state.short_term = head + tail
    _compact_short_term_messages(state, per_message_chars=2000)


def _maybe_compress_for_context(state: AgentState, llm: LLMBackend, system: str, messages: list[dict]) -> dict:
    """Estimate prompt tokens and auto-trim when close to context limit."""
    import os

    ctx = int(os.environ.get("LLM_CONTEXT_WINDOW", "131072"))  # oss120b is 128K; vLLM reports 131072
    warn_ratio = float(os.environ.get("LLM_CONTEXT_WARN_RATIO", "0.90"))

    est = 0
    try:
        est = int(llm.estimate_tokens(messages, system))
    except Exception:
        est = 0

    # Save stats for debugging/printing
    state.meta["prompt_tokens_est"] = est
    state.meta["context_window"] = ctx

    # If we're near the limit, trim short_term and keep going.
    if est and est > int(ctx * warn_ratio):
        # Aggressive compaction: trim history AND compact large message bodies.
        _trim_short_term(state, keep_last=6)
        _compact_short_term_messages(state, per_message_chars=1500)
        state.long_term.append(
            f"[自我修复] prompt≈{est} tokens 接近 context={ctx}，已自动裁剪/压缩 short_term（大输出已截断）。"
        )
        # After trimming, recompute once (best-effort)
        try:
            system2 = build_system_prompt(state.tools, state.long_term)
            messages2 = build_context_messages(state)
            est2 = int(llm.estimate_tokens(messages2, system2))
            state.meta["prompt_tokens_est"] = est2
            return {"system": system2, "messages": messages2}
        except Exception:
            return {"system": system, "messages": messages}

    return {"system": system, "messages": messages}



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
    state: Optional[AgentState] = None,
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

    # 初始化/恢复状态
    if state is None:
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
    else:
        # Resume: update goal + merge tools (keep evolved state.tools by default)
        state.goal = goal
        # If caller provides tools, merge any missing ones.
        for k, v in tools.items():
            state.tools.setdefault(k, v)
        # Allow caller to inject long_term additions.
        if long_term:
            for item in long_term:
                if item not in state.long_term:
                    state.long_term.append(item)
        state.meta.pop("paused", None)
        state.meta.pop("awaiting_input", None)

    # ── LOOP ─────────────────────────────────────────────────────────────────
    while state.iteration < max_iterations:

        # 回调：迭代开始
        if hooks.on_iteration_start:
            hooks.on_iteration_start(state.iteration, state)

        # 1. 构建当前 system prompt（工具集可能已进化，每次重新生成）
        system = build_system_prompt(state.tools, state.long_term, scratchpad=state.meta.get('scratchpad',''))

        # 2. 构建消息上下文
        messages = build_context_messages(state)

        # 2.5 估算 token 并在接近最大上下文时自动压缩（裁剪历史）
        pack = _maybe_compress_for_context(state, llm, system, messages)
        system = pack["system"]
        messages = pack["messages"]
        if hooks.on_thought and state.meta.get("prompt_tokens_est"):
            est = state.meta.get("prompt_tokens_est")
            ctx = state.meta.get("context_window")
            hooks.on_thought(f"[token] prompt≈{est} / context={ctx} (est), max_tokens={getattr(llm, 'max_tokens', 'n/a')}")

        # 3. 调用 LLM
        try:
            raw_response = llm.complete(messages, system)
        except Exception as e:
            error_msg = f"LLM 调用失败: {e}"
            if hooks.on_error:
                hooks.on_error(error_msg)

            # Self-heal: if prompt is too large (vLLM may compute negative max_tokens)
            # or context is exceeded, trim history to shrink the next prompt.
            es = str(e)
            if (
                "max_tokens must be at least 1" in es
                or "context_length" in es
                or "context length" in es
                or "maximum context" in es
            ):
                _trim_short_term(state, keep_last=6)
                _compact_short_term_messages(state, per_message_chars=1200)
                state.long_term.append("[自我修复] 遇到上下文/输出长度错误，已自动裁剪+压缩 short_term 以缩短 prompt。")

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

            # Auto-remember on success (opt-in)
            import os
            if os.environ.get("AUTO_REMEMBER_ON_DONE", "0") == "1":
                try:
                    used_tools = []
                    for m in state.short_term:
                        if m.get("role") != "assistant":
                            continue
                        # best-effort: tool name appears as JSON field "tool"
                        c = m.get("content", "")
                        if isinstance(c, str) and '"tool"' in c:
                            # naive parse: avoid json errors
                            import re
                            mt = re.search(r'"tool"\s*:\s*"([^"]+)"', c)
                            if mt:
                                used_tools.append(mt.group(1))
                    used_tools = list(dict.fromkeys(used_tools))

                    est = state.meta.get("prompt_tokens_est")
                    ctx = state.meta.get("context_window")
                    summary = (
                        f"[RUN_OK] goal={state.goal[:120]!r} tools={used_tools} "
                        f"prompt_est={est}/{ctx} final={str(action.final_answer)[:200]!r}"
                    )
                    state.long_term.append(summary)
                except Exception:
                    pass

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

            # Special: pause-for-input tool
            if action.tool == "ask_user":
                # Expect args: {question: str}
                q = action.args.get("question") or (result.output or {}).get("question")
                state.meta["awaiting_input"] = q or "(no question provided)"
                state.meta["paused"] = True
                if hooks.on_error:
                    hooks.on_error(f"暂停等待用户输入: {state.meta['awaiting_input']}")
                break

            # 把执行结果反馈给 LLM（作为下一轮的 user 消息）
            feedback = _build_feedback(action, result)
            state.short_term.append({
                "role": "user",
                "content": feedback,
            })

            # Optional: auto-append raw memory log (full transcript fragments)
            import os
            if os.environ.get("AUTO_RAW_LOG", "0") == "1":
                try:
                    path = os.environ.get("RAW_MEMORY_PATH", "./raw_memory.ndjson")
                    # record minimal structured info
                    state.tools.get("raw_append").fn(
                        state=state,
                        content=f"ITER={state.iteration} TOOL={action.tool} ARGS={action.args} RESULT={result.to_str()}",
                        path=path,
                    )
                except Exception:
                    pass

        state.iteration += 1

    else:
        # 超出最大迭代次数
        if hooks.on_error:
            hooks.on_error(f"达到最大迭代次数 {max_iterations}，强制退出。")
        state.meta["timeout"] = True

    return state


# ── 内部辅助 ──────────────────────────────────────────────────────────────────

def _summarize_large_text(text: str, limit: int) -> str:
    """Summarize/truncate large tool outputs for prompt safety."""
    import os
    import json as _json

    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s

    # Best-effort JSON summary
    try:
        obj = _json.loads(s)
        if isinstance(obj, dict):
            keys = list(obj.keys())
            return (
                f"[TRUNCATED_JSON] len={len(s)} keys={keys[:20]}\n"
                + s[: max(0, limit - 1200)]
                + "\n...[TRUNCATED]"
            )
        if isinstance(obj, list):
            return (
                f"[TRUNCATED_JSON_LIST] len={len(s)} items={len(obj)}\n"
                + s[: max(0, limit - 1200)]
                + "\n...[TRUNCATED]"
            )
    except Exception:
        pass

    head = s[: int(limit * 0.7)]
    tail = s[-int(limit * 0.2) :]
    return f"[TRUNCATED] len={len(s)}\n{head}\n...\n{tail}"


def _build_feedback(action: Action, result: ToolResult) -> str:
    """构建工具执行结果的反馈消息。

    Important: never stuff huge tool outputs into the LLM context.
    """
    import os

    max_chars = int(os.environ.get("MAX_TOOL_FEEDBACK_CHARS", "4000"))

    if result.success:
        out = result.to_str()
        out2 = _summarize_large_text(out, max_chars)
        return (
            f"[工具: {action.tool}] 执行成功\n"
            f"输出(可能已截断):\n{out2}"
        )
    else:
        return (
            f"[工具: {action.tool}] 执行失败\n"
            f"错误: {result.error}\n"
            f"请分析原因，调整策略后重试（可换用其他工具或修改参数）。"
        )
