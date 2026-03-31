"""
智能体主循环
这是整个系统最核心的文件。
LOOP: 感知 → 思考 → 行动 → 反思 → 重复
"""

import json
import re
from dataclasses import dataclass
from typing import Optional, Callable

from .types import Action, ActionType, AgentState, ToolSpec, ToolResult
from .llm import LLMBackend, build_system_prompt, build_context_messages, parse_response
from .executor import execute
from ..runtime.persistence import RunPersistence


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


def _get_persistence(state: AgentState) -> Optional[RunPersistence]:
    persistence = getattr(state, "persistence", None)
    if persistence is not None:
        return persistence
    try:
        import os

        run_dir = os.environ.get("RUN_DIR")
        if not run_dir:
            return None
        persistence = RunPersistence(run_dir)
        state.persistence = persistence
        return persistence
    except Exception:
        return None


def _append_short_term(state: AgentState, record: dict) -> None:
    state.short_term.append(record)
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.append_short_term(record)


def _checkpoint_state(state: AgentState, status: str = "running", error: Optional[str] = None) -> None:
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.checkpoint(state, status=status, error=error)


def _trim_short_term(state: AgentState, keep_last: int = 8):
    """Trim short_term history to reduce prompt size.

    Strategy:
    - Always keep short_term[0] (the original user goal — must never be lost).
    - Always keep the last `keep_last` messages (recent execution context).
    - Replace the dropped middle with a single bridge message that explicitly
      points the model to the scratchpad, which already contains the distilled
      summary of earlier work.  This turns scratchpad into the primary carrier
      of compressed history rather than silently discarding it.
    """
    if not state.short_term:
        return

    # Nothing to drop: head + tail already covers everything.
    if len(state.short_term) <= keep_last + 1:
        _compact_short_term_messages(state, per_message_chars=2000)
        return

    head = state.short_term[:1]                                         # goal — never drop
    tail = state.short_term[-keep_last:]                                # recent context
    dropped = len(state.short_term) - 1 - keep_last

    scratchpad = (state.meta.get("scratchpad") or "").strip()
    if scratchpad:
        bridge_content = (
            f"[系统] 早期对话记录（共 {dropped} 条）已压缩以节省上下文空间。"
            f"执行过程的关键发现与进度已归纳在 system prompt 的草稿本中，请以草稿本内容作为早期历史的参考依据。"
            f"以下为最近 {keep_last} 条执行记录。"
        )
    else:
        bridge_content = (
            f"[系统] 早期对话记录（共 {dropped} 条）已压缩以节省上下文空间。"
            f"以下为最近 {keep_last} 条执行记录。"
        )

    bridge = {"role": "user", "content": bridge_content}
    state.short_term = head + [bridge] + tail
    _compact_short_term_messages(state, per_message_chars=2000)


def _extract_claimed_artifact_paths(text: str, run_dir: Optional[str] = None) -> list[str]:
    """Extract artifact-like paths from acceptance text without splitting human prose."""
    if not text:
        return []

    def _clean_path(value: str) -> str:
        s = value.strip().strip("`\"'")
        s = s.rstrip("`.,;:!?)\"'】）》》，。；：！？’”）")
        if s.startswith("./"):
            s = s[2:]
        if s.startswith("/runs/") or s.startswith("/artifacts/"):
            s = s[1:]
        return s

    def _append_unique(out: list[str], seen: set[str], candidate: str):
        cleaned = _clean_path(candidate)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)

    def _extract_from_blob(blob: str, out: list[str], seen: set[str]):
        if not blob:
            return

        for match in re.findall(
            r"((?:\./)?(?:runs/\d{8}-\d{6}|artifacts)/[^\s`\)\]\}<>\"'，。；：！？]+)",
            blob,
        ):
            _append_unique(out, seen, match)

        for match in re.findall(
            r"(?:(?<=^)|(?<=[\s`\"'\(\[\{<]))((?:/[^\s`<>\"]+)*?/runs/\d{8}-\d{6}/[^\s`<>\"]+|(?:/[^\s`<>\"]+)*?/artifacts/[^\s`<>\"]+)",
            blob,
        ):
            _append_unique(out, seen, match)

        if run_dir:
            for match in re.findall(r"\$RUN_DIR/([^\s`\)\]\}<>\"'，。；：！？]+)", blob):
                _append_unique(out, seen, f"{run_dir.rstrip('/')}/{match}")

    ordered: list[str] = []
    seen: set[str] = set()

    for line in text.splitlines():
        match = re.search(r"\bevidence\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            continue
        rhs = match.group(1).strip()

        if rhs.startswith("[") and rhs.endswith("]"):
            try:
                payload = json.loads(rhs)
            except Exception:
                payload = None
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, str):
                        _append_unique(ordered, seen, item)
                continue

        _extract_from_blob(rhs, ordered, seen)

    _extract_from_blob(text, ordered, seen)
    return ordered


def _parse_acceptance_evidence(text: str, run_dir: Optional[str] = None) -> dict:
    """Parse acceptance evidence metadata and decide whether artifact checks apply."""
    evidence_type = "artifact"
    evidence_values: list[str] = []

    for line in text.splitlines():
        match_type = re.search(r"\bevidence_type\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if match_type:
            evidence_type = (match_type.group(1).strip().lower() or "artifact")
            continue

        match_evidence = re.search(r"\bevidence\s*:\s*(.+)$", line, flags=re.IGNORECASE)
        if match_evidence:
            evidence_values.append(match_evidence.group(1).strip())

    if evidence_type not in {"artifact", "tool_result", "observation", "none"}:
        evidence_type = "artifact"

    if evidence_type != "artifact":
        return {
            "evidence_type": evidence_type,
            "evidence_values": evidence_values,
            "paths": [],
        }

    artifact_text = "\n".join(f"evidence: {value}" for value in evidence_values) if evidence_values else text
    return {
        "evidence_type": evidence_type,
        "evidence_values": evidence_values,
        "paths": _extract_claimed_artifact_paths(artifact_text, run_dir=run_dir),
    }


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
        # 截断过长输出（可配置，避免刷屏）
        import os
        max_len = int(os.environ.get("TOOL_RESULT_PRINT_MAX_CHARS", "5000"))
        if len(text) > max_len:
            text = text[:max_len] + "...[截断]"
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
        try:
            import os

            raw_goal = (os.environ.get("USER_GOAL") or goal).strip()
        except Exception:
            raw_goal = goal.strip()
        state.meta["_task_desc"] = raw_goal
        state.meta["scratchpad"] = f"任务描述:\n{raw_goal}\n"
        _append_short_term(
            state,
            {
                "role": "user",
                "content": f"请完成以下目标：\n\n{goal}",
            },
        )
        persistence = _get_persistence(state)
        if persistence is not None:
            persistence.start(state)
    else:
        state.goal = goal
        for k, v in tools.items():
            state.tools.setdefault(k, v)
        if long_term:
            for item in long_term:
                if item not in state.long_term:
                    state.long_term.append(item)
        state.meta.pop("paused", None)
        state.meta.pop("awaiting_input", None)
        _checkpoint_state(state)

    try:
        while state.iteration < max_iterations:
            if hooks.on_iteration_start:
                hooks.on_iteration_start(state.iteration, state)

            system = build_system_prompt(state.tools, state.long_term, scratchpad=state.meta.get("scratchpad", ""))
            messages = build_context_messages(state)

            pack = _maybe_compress_for_context(state, llm, system, messages)
            system = pack["system"]
            messages = pack["messages"]
            if hooks.on_thought and state.meta.get("prompt_tokens_est"):
                est = state.meta.get("prompt_tokens_est")
                ctx = state.meta.get("context_window")
                hooks.on_thought(
                    f"[token] prompt≈{est} / context={ctx} (est), max_tokens={getattr(llm, 'max_tokens', 'n/a')}"
                )

            import os

            if os.environ.get("DEBUG_LLM_IO", "0") == "1":
                DEEP_GREEN = "\033[32m"
                DARK_RED = "\033[31m"
                RESET = "\033[0m"
                max_chars = int(os.environ.get("DEBUG_LLM_IO_MAX_CHARS", "200000"))

                try:
                    payload = {
                        "system": system,
                        "messages": messages,
                        "max_tokens": getattr(llm, "max_tokens", None),
                    }
                    s = json.dumps(payload, ensure_ascii=False, indent=2)
                except Exception:
                    s = f"(failed to serialize payload) system_len={len(system)} messages={len(messages)}"

                if len(s) > max_chars:
                    s = s[:max_chars] + "\n...[TRUNCATED]"
                print(f"{DEEP_GREEN}\n[DEBUG_LLM_IO] >>> request{RESET}\n{s}\n")

            try:
                raw_response = llm.complete(messages, system)
            except Exception as e:
                error_msg = f"LLM 调用失败: {e}"
                if hooks.on_error:
                    hooks.on_error(error_msg)

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

                _append_short_term(
                    state,
                    {
                        "role": "user",
                        "content": f"[系统] LLM调用异常: {e}，请重试或换一种方式。",
                    },
                )
                _checkpoint_state(state)
                state.iteration += 1
                continue

            if os.environ.get("DEBUG_LLM_IO", "0") == "1":
                DARK_RED = "\033[31m"
                RESET = "\033[0m"
                max_chars = int(os.environ.get("DEBUG_LLM_IO_MAX_CHARS", "200000"))
                s2 = raw_response if isinstance(raw_response, str) else str(raw_response)
                if len(s2) > max_chars:
                    s2 = s2[:max_chars] + "\n...[TRUNCATED]"
                print(f"{DARK_RED}[DEBUG_LLM_IO] <<< response{RESET}\n{s2}\n")

            action = parse_response(raw_response)
            if action.type != ActionType.ERROR:
                state.meta.pop("json_parse_retry", None)

            if hooks.on_thought and action.thought:
                hooks.on_thought(action.thought)

            _append_short_term(
                state,
                {
                    "role": "assistant",
                    "content": raw_response,
                },
            )

            if action.type == ActionType.DONE:
                def _acceptance_gate(state: AgentState, final_answer: Optional[str]):
                    import os
                    from pathlib import Path

                    sp = state.meta.get("scratchpad", "")
                    if not isinstance(sp, str):
                        sp = ""

                    if "ACCEPTANCE" not in sp.upper():
                        return False, [
                            {
                                "code": "acceptance_missing",
                                "message": "缺少验收自评。请在草稿本追加一个 ACCEPTANCE 区块：包含验收标准(criteria)、证据(evidence 路径/片段)与结论(verdict)。",
                            }
                        ]

                    text = (sp or "") + "\n" + (final_answer or "")
                    run_dir = os.environ.get("RUN_DIR")
                    if run_dir:
                        repo_root = Path(run_dir).resolve().parent.parent
                    else:
                        repo_root = Path.cwd().resolve()

                    acceptance_evidence = _parse_acceptance_evidence(text, run_dir=run_dir)
                    paths = acceptance_evidence["paths"]

                    norm_paths: list[Path] = []
                    for p in paths:
                        pp = Path(p)
                        if not pp.is_absolute():
                            pp = repo_root / pp
                        norm_paths.append(pp)

                    failures = []
                    for pp in sorted({p.resolve() for p in norm_paths}):
                        if not pp.exists():
                            failures.append(
                                {
                                    "code": "artifact_missing",
                                    "message": f"宣称/引用的产物不存在: {pp}。若应生成该文件，请先 write_file 落盘后再 done。",
                                }
                            )

                    if failures:
                        return False, failures
                    return True, []

                passed, failures = _acceptance_gate(state, action.final_answer)
                if not passed:
                    state.meta.setdefault("acceptance_failures", []).append(
                        {
                            "iteration": state.iteration,
                            "failures": failures,
                        }
                    )
                    if hooks.on_error:
                        hooks.on_error("[验收失败] 未通过验收，继续 loop 进行补救")
                        try:
                            hooks.on_error("[验收失败详情] " + json.dumps(failures, ensure_ascii=False))
                        except Exception:
                            pass

                    _append_short_term(
                        state,
                        {
                            "role": "user",
                            "content": (
                                "[系统][验收失败] 你刚才尝试 done，但未通过验收，因此不会退出。\n"
                                "你必须先补救并满足验收，再次 done。\n\n"
                                f"失败原因: {json.dumps(failures, ensure_ascii=False, indent=2)}\n\n"
                                "补救建议: 若缺少产物文件，请调用 write_file 生成；若缺少验收自评，请用 scratchpad_append 追加 ACCEPTANCE 区块(标准+证据+结论)。"
                            ),
                        },
                    )
                    _checkpoint_state(state)
                    state.iteration += 1
                    continue

                if hooks.on_done:
                    hooks.on_done(action.final_answer or "（无最终输出）")
                state.meta["final_answer"] = action.final_answer
                persistence = _get_persistence(state)
                if persistence is not None:
                    persistence.save_final_answer(action.final_answer or "")
                _checkpoint_state(state)

                if os.environ.get("AUTO_REMEMBER_ON_DONE", "0") == "1":
                    try:
                        used_tools = []
                        for m in state.short_term:
                            if m.get("role") != "assistant":
                                continue
                            c = m.get("content", "")
                            if isinstance(c, str) and '"tool"' in c:
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

            if action.type == ActionType.ERROR:
                error_msg = action.thought
                if hooks.on_error:
                    hooks.on_error(error_msg)

                is_json_parse_error = isinstance(error_msg, str) and "JSON 解析失败" in error_msg
                retry_max = int(os.environ.get("JSON_PARSE_RETRY_MAX", "3"))

                if is_json_parse_error and hasattr(llm, "max_tokens"):
                    retry_n = int(state.meta.get("json_parse_retry", 0))
                    cap = int(os.environ.get("LLM_MAX_TOKENS_CAP", "8192"))
                    old = int(getattr(llm, "max_tokens", 0) or 0)
                    if retry_n < retry_max and old > 0 and old < cap:
                        new = min(cap, max(old + 1, old * 2))
                        try:
                            setattr(llm, "max_tokens", new)
                        except Exception:
                            pass
                        state.meta["json_parse_retry"] = retry_n + 1
                        state.long_term.append(
                            f"[自我修复] JSON 解析失败，疑似输出被截断：max_tokens {old}→{new} 后重试。"
                        )
                        if hooks.on_error:
                            hooks.on_error(f"[自我修复] 提升 max_tokens {old}→{new} 并重试")

                # 连续 JSON 解析失败计数（独立于 json_parse_retry）
                if is_json_parse_error:
                    streak = int(state.meta.get("_json_fail_streak", 0)) + 1
                    state.meta["_json_fail_streak"] = streak
                else:
                    streak = 0
                    state.meta["_json_fail_streak"] = 0

                # 超出重试上限后注入强提示，打破截断死循环
                if is_json_parse_error and streak > retry_max:
                    overload_hint = (
                        f"\n\n⛔ 循环检测：JSON 解析已连续失败 {streak} 次，输出持续被截断。"
                        "根本原因极可能是 args（尤其是 content/code 字段）过长，超出模型单次输出上限。"
                        "请立刻改变策略：① 用 run_python 分块写入文件；② 或大幅缩短内容后重试。"
                        "禁止继续原样重试相同的长内容。"
                    )
                else:
                    overload_hint = ""

                _append_short_term(
                    state,
                    {
                        "role": "user",
                        "content": (
                            "[系统] 输出格式错误，请严格按照 JSON 格式重新回复。"
                            "只输出 JSON，不要额外文本；如需调用工具，请尽量让 args 简短（例如把长代码放在多行字符串中或拆步）。"
                            f"错误详情: {error_msg}"
                            + overload_hint
                        ),
                    },
                )
                _checkpoint_state(state)
                state.iteration += 1
                continue

            if action.type == ActionType.TOOL_CALL:
                # 成功解析说明 JSON 截断已恢复，重置连续失败计数
                state.meta["_json_fail_streak"] = 0

                if hooks.on_tool_call:
                    hooks.on_tool_call(action.tool, action.args)

                result = execute(action, state)

                if hooks.on_tool_result:
                    hooks.on_tool_result(result)

                # 自动提取关键发现追加草稿本（目标感知的 mini LLM call）
                if os.environ.get("AUTO_SCRATCHPAD_NOTE", "1") != "0":
                    _auto_scratchpad_note(action, result, state, llm)

                if action.tool == "ask_user":
                    q = action.args.get("question") or (result.output or {}).get("question")
                    state.meta["awaiting_input"] = q or "(no question provided)"
                    state.meta["paused"] = True
                    if hooks.on_error:
                        hooks.on_error(f"暂停等待用户输入: {state.meta['awaiting_input']}")
                    _checkpoint_state(state, status="paused")
                    break

                feedback = _build_feedback(action, result, state=state)
                if feedback is not None:
                    _append_short_term(
                        state,
                        {
                            "role": "user",
                            "content": feedback,
                        },
                    )

                if os.environ.get("AUTO_RAW_LOG", "0") == "1":
                    try:
                        path = os.environ.get("RAW_MEMORY_PATH", "./raw_memory.ndjson")
                        state.tools.get("raw_append").fn(
                            state=state,
                            content=f"ITER={state.iteration} TOOL={action.tool} ARGS={action.args} RESULT={result.to_str()}",
                            path=path,
                        )
                    except Exception:
                        pass

                _checkpoint_state(state)
                state.iteration += 1
                continue

            state.iteration += 1
    except Exception as e:
        persistence = _get_persistence(state)
        if persistence is not None:
            persistence.finish(state, outcome="failed", error=f"{type(e).__name__}: {e}")
        raise
    else:
        if state.iteration >= max_iterations and not state.meta.get("paused") and "final_answer" not in state.meta:
            if hooks.on_error:
                hooks.on_error(f"达到最大迭代次数 {max_iterations}，强制退出。")
            state.meta["timeout"] = True
            _checkpoint_state(state)

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


# 这些工具的成功 ACK 对模型无信息价值（结果已通过 system prompt 中的 scratchpad 反映）
_ACK_ONLY_TOOLS = frozenset({"scratchpad_set", "scratchpad_append", "raw_append"})


def _auto_scratchpad_note(action: "Action", result: "ToolResult", state: "AgentState", llm: "LLMBackend") -> None:
    """在工具执行成功后，用超短 LLM 对话自动提取关键发现并追加到草稿本。

    设计原则：
    - mini call 携带 goal + 当前草稿摘要 + 工具结果，做到目标感知而不只是局部摘要
    - 直接写 state.meta["scratchpad"]，绕过工具系统，不产生 ACK 噪声
    - 超限时从头部裁剪（保留任务描述行），优先保留最新记录
    - 任何异常静默跳过，不影响主流程
    """
    import os

    if not result.success:
        return
    if action.tool in _ACK_ONLY_TOOLS:
        return

    # 短输出已足够简洁，不需要再提炼
    out = result.to_str()
    min_chars = int(os.environ.get("AUTO_NOTE_MIN_CHARS", "200"))
    if len(out) < min_chars:
        return

    # 处于 JSON 截断循环中时跳过，避免雪上加霜
    if int(state.meta.get("_json_fail_streak", 0)) > 0:
        return

    try:
        goal_text   = (getattr(state, "goal", "") or "")[:300]
        sp_text     = (state.meta.get("scratchpad") or "")[:300]
        args_text   = json.dumps(action.args, ensure_ascii=False)[:120]
        result_text = out[:1000]

        mini_system = (
            "你是一个简洁的信息提取助手。"
            "根据任务目标，从工具结果中提取1-2条最关键的新发现。"
            "要求：每条一行，不超过40字，直接输出文字，不要JSON，不要编号，不要重复草稿中已有的内容。"
        )
        mini_user = (
            f"任务目标: {goal_text}\n"
            f"当前草稿摘要: {sp_text}\n"
            f"工具: {action.tool}  参数: {args_text}\n"
            f"工具结果:\n{result_text}"
        )

        note = llm.complete_text(
            messages=[{"role": "user", "content": mini_user}],
            system=mini_system,
            max_tokens=120,
        ).strip()

        if not note:
            return

        note = note[:200]   # 防止模型输出过长
        iter_n = getattr(state, "iteration", 0)
        entry = f"\n[iter{iter_n}|{action.tool}] {note}"

        cur = state.meta.get("scratchpad", "")
        max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
        new_sp = cur + entry

        # 超限时从头部裁剪，保留任务描述（前3行）+ 最新内容
        if len(new_sp) > max_chars:
            lines = new_sp.splitlines(keepends=True)
            head = "".join(lines[:3])
            body = new_sp[len(head):]
            overflow = len(head) + len(body) - max_chars
            body = body[overflow:]
            new_sp = head + body

        state.meta["scratchpad"] = new_sp

        # 立即落盘
        persistence = _get_persistence(state)
        if persistence is not None:
            persistence.save_scratchpad(new_sp)

    except Exception:
        pass  # 自动追加失败不影响主流程


def _spill_large_output_to_disk(tool_name: str, content: str, state: "AgentState") -> Optional[str]:
    """将超限的工具输出完整写入 artifacts 目录，返回可用的相对路径；失败返回 None。

    保证大型工具输出在被截断送入上下文之前先落盘，避免信息永久丢失。
    模型可凭路径用 shell / run_python 分段读取完整内容。
    """
    from pathlib import Path

    persistence = _get_persistence(state)
    if persistence is not None:
        run_dir = Path(persistence.run_dir)
    else:
        rd = os.environ.get("RUN_DIR")
        if not rd:
            return None
        run_dir = Path(rd)

    try:
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        iter_n = getattr(state, "iteration", 0)
        safe_name = tool_name.replace("/", "_").replace("\\", "_")
        filepath = artifacts_dir / f"tool_raw_{safe_name}_iter{iter_n}.txt"
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)
    except Exception:
        return None


def _build_feedback(action: Action, result: ToolResult, state: Optional["AgentState"] = None) -> Optional[str]:
    """构建工具执行结果的反馈消息。

    Important: never stuff huge tool outputs into the LLM context.
    """
    import os
    import json as _json
    import hashlib

    max_chars = int(os.environ.get("MAX_TOOL_FEEDBACK_CHARS", "4000"))

    # ── 重复调用检测 ──────────────────────────────────────────────────────────
    repeat_warning = ""
    if state is not None and action.type == ActionType.TOOL_CALL:
        try:
            call_sig = hashlib.md5(
                _json.dumps(
                    {"tool": action.tool, "args": action.args},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()

            history = state.meta.setdefault("_call_sig_history", [])
            consecutive = 0
            for prev in reversed(history[-5:]):
                if prev == call_sig:
                    consecutive += 1
                else:
                    break
            history.append(call_sig)

            if consecutive >= 2:
                args_preview = _json.dumps(action.args, ensure_ascii=False)[:300]
                repeat_warning = (
                    f"\n\n⛔ 循环检测：你已连续 {consecutive + 1} 次以完全相同的参数调用 `{action.tool}`，"
                    f"继续重试无意义。请立刻换用其他工具（如 run_python）或直接 done 给出已知结论。"
                    f"\n以下参数禁止再次原样使用：\n```\n{args_preview}\n```"
                )
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────────

    if result.success:
        # 纯 ACK 工具：成功时不写入 short_term，避免无意义消息占用上下文
        if action.tool in _ACK_ONLY_TOOLS:
            return None
        out = result.to_str()

        if len(out) > max_chars:
            # 输出超限：先完整落盘，再给模型路径 + 预览，确保数据不因截断而丢失
            spill_path = _spill_large_output_to_disk(action.tool, out, state) if state is not None else None
            if spill_path:
                preview = _summarize_large_text(out, 800)
                return (
                    f"[工具: {action.tool}] 执行成功\n"
                    f"输出较大（{len(out)} 字符），已完整保存至：{spill_path}\n"
                    f"如需读取完整内容，请使用 shell 或 run_python 分段读取该文件。\n"
                    f"内容预览：\n{preview}"
                )
            # RUN_DIR 不可用时降级为截断（保持原有行为）
            out2 = _summarize_large_text(out, max_chars)
            return (
                f"[工具: {action.tool}] 执行成功\n"
                f"输出(可能已截断):\n{out2}"
            )

        return (
            f"[工具: {action.tool}] 执行成功\n"
            f"输出:\n{out}"
        )
    else:
        return (
            f"[工具: {action.tool}] 执行失败\n"
            f"错误: {result.error}\n"
            f"请分析原因，调整策略后重试（可换用其他工具或修改参数）。"
            + repeat_warning
        )
