"""
智能体主循环
这是整个系统最核心的文件。
LOOP: 感知 → 思考 → 行动 → 反思 → 重复
"""

import json
import os
import re
from typing import Optional, Callable

from .types import Action, ActionType, AgentHooks, AgentState, ToolSpec, ToolResult
from .llm import LLMBackend, build_system_prompt, build_context_messages, parse_response
from .executor import execute
from .compression import (
    _ACK_ONLY_TOOLS,
    _get_persistence,
    _summarize_large_text,
    _compact_short_term_messages,
    _trim_short_term,
    _maybe_compress_for_context,
    _auto_scratchpad_note,
    _rebuild_context_on_hard_block,
    _apply_runtime_patch,
)
from .advisor import run_advisor, should_trigger_advisor, inject_advisor_advice



def _append_short_term(state: AgentState, record: dict) -> None:
    state.short_term.append(record)
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.append_short_term(record)


def _log_token_stats(state: AgentState, record: dict) -> None:
    """Write a metadata record to short_term.jsonl only (not added to LLM context)."""
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.append_short_term(record)


def _checkpoint_state(state: AgentState, status: str = "running", error: Optional[str] = None) -> None:
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.checkpoint(state, status=status, error=error)



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


# ── 结构化完成报告 ────────────────────────────────────────────────────────────

def _normalize_completion_report(report: Optional[dict]) -> dict:
    """将 completion_report 规范化为标准结构，处理缺失/非法字段。"""
    data = dict(report or {})

    def _listify(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    evidence_type = str(data.get("evidence_type", "none")).strip().lower() or "none"
    if evidence_type not in {"artifact", "tool_result", "observation", "none"}:
        evidence_type = "none"

    outcome = str(data.get("outcome", "done")).strip().lower() or "done"
    if outcome not in {"done", "done_partial", "done_blocked"}:
        outcome = "done"

    confidence = str(data.get("confidence", "medium")).strip().lower() or "medium"
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"

    return {
        "goal_understanding": str(data.get("goal_understanding", "")).strip(),
        "completed_work": _listify(data.get("completed_work")),
        "remaining_gaps": _listify(data.get("remaining_gaps")),
        "evidence_type": evidence_type,
        "evidence": _listify(data.get("evidence")),
        "outcome": outcome,
        "confidence": confidence,
    }


def _completion_report_from_legacy_acceptance(state: AgentState, final_answer: Optional[str]) -> Optional[dict]:
    """将旧 ACCEPTANCE 草稿本格式转换为结构化完成报告（向后兼容层）。"""
    scratchpad = state.meta.get("scratchpad", "")
    if not isinstance(scratchpad, str) or "ACCEPTANCE" not in scratchpad.upper():
        return None

    parsed = _parse_acceptance_evidence(scratchpad + "\n" + (final_answer or ""))
    evidence = parsed["paths"] if parsed["evidence_type"] == "artifact" else parsed["evidence_values"]
    completed = []
    if final_answer and final_answer.strip():
        completed.append(final_answer.strip().splitlines()[0][:200])

    return _normalize_completion_report(
        {
            "goal_understanding": (state.goal or "").strip(),
            "completed_work": completed or ["已生成最终回答"],
            "remaining_gaps": [],
            "evidence_type": parsed["evidence_type"],
            "evidence": evidence,
            "outcome": "done",
            "confidence": "medium",
        }
    )


def _review_completion_report(state: AgentState, final_answer: Optional[str]) -> tuple[str, dict]:
    """
    审核完成报告，返回 (verdict, verdict_dict)。

    verdict 取值:
      "pass"            - 完整完成，可直接退出
      "weak_pass"       - 部分完成或被阻塞，退出前暂停询问用户是否继续
      "needs_more_work" - 缺少报告或产物文件，需继续循环补救
    """
    import os
    from pathlib import Path

    report = state.meta.get("completion_report")
    normalized = _normalize_completion_report(report if isinstance(report, dict) else None)

    # 兼容旧 ACCEPTANCE 草稿本格式
    if not normalized["goal_understanding"]:
        legacy = _completion_report_from_legacy_acceptance(state, final_answer)
        if legacy is not None:
            normalized = legacy

    if not normalized["goal_understanding"]:
        return "needs_more_work", {"status": "needs_more_work", "reason": "missing_completion_report"}

    if not normalized["completed_work"] and not (final_answer or "").strip():
        return "needs_more_work", {"status": "needs_more_work", "reason": "missing_completed_work"}

    # artifact 类证据：验证文件实际存在
    if normalized["evidence_type"] == "artifact":
        run_dir = os.environ.get("RUN_DIR")
        if run_dir:
            repo_root = Path(run_dir).resolve().parent.parent
        else:
            repo_root = Path.cwd().resolve()

        missing: list[str] = []
        for item in normalized["evidence"]:
            for raw_path in _extract_claimed_artifact_paths(item, run_dir=run_dir) or [item]:
                pp = Path(raw_path)
                if not pp.is_absolute():
                    pp = repo_root / pp
                if not pp.exists():
                    missing.append(str(pp.resolve()))

        if missing:
            verdict_dict = {
                "status": "needs_more_work",
                "reason": "artifact_missing",
                "missing": sorted(set(missing)),
                "report": normalized,
            }
            state.meta["completion_review"] = verdict_dict
            return "needs_more_work", verdict_dict

    # 三态结果：done_partial / done_blocked → weak_pass（弱通过，暂停询问用户）
    if normalized["outcome"] in {"done_partial", "done_blocked"}:
        reason = "partial_completion" if normalized["outcome"] == "done_partial" else "blocked_completion"
        verdict_dict = {"status": "weak_pass", "reason": reason, "report": normalized}
        state.meta["completion_review"] = verdict_dict
        return "weak_pass", verdict_dict

    verdict_dict = {"status": "pass", "reason": "completion_report_sufficient", "report": normalized}
    state.meta["completion_review"] = verdict_dict
    return "pass", verdict_dict


# ── 默认钩子：打印到控制台 ────────────────────────────────────────────────────

def console_hooks() -> AgentHooks:
    """开箱即用的控制台输出钩子，开发调试用。"""
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    GREEN   = "\033[92m"
    RED     = "\033[91m"
    GRAY    = "\033[90m"
    MAGENTA = "\033[95m"   # 草稿本自动笔记
    ORANGE  = "\033[38;5;214m"  # 上下文重建（亮橙色）
    BOLD    = "\033[1m"
    RESET   = "\033[0m"

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
        print(f"{RED}⚠️  错误: {msg}{RESET}")

    def on_note(tool_name: str, note: str):
        """草稿本自动笔记提炼成功时的提示（品红色）。"""
        print(f"{MAGENTA}📓 草稿本笔记 [{tool_name}]: {note}{RESET}")

    def on_rebuild(blocked_tool: str, msg_count: int):
        """上下文重建时的醒目提示（橙色 + 边框）。"""
        bar = "═" * 60
        print(f"\n{ORANGE}{BOLD}{bar}{RESET}")
        print(f"{ORANGE}{BOLD}🔄 上下文重建  ·  封锁工具: {blocked_tool}  ·  重建后消息数: {msg_count}{RESET}")
        print(f"{ORANGE}   原因: 反复忽略循环警告，已清除污染上下文并注入新起点{RESET}")
        print(f"{ORANGE}{BOLD}{bar}{RESET}\n")

    PURPLE = "\033[35m"

    def on_advisor(reason: str, advice: str):
        """高级指导员介入时的提示（紫色边框）。"""
        bar = "─" * 60
        preview = advice[:200].replace("\n", " ")
        if len(advice) > 200:
            preview += "…"
        print(f"\n{PURPLE}{bar}{RESET}")
        print(f"{PURPLE}[高级指导员 · {reason}]{RESET}")
        print(f"{PURPLE}{preview}{RESET}")
        print(f"{PURPLE}{bar}{RESET}\n")

    return AgentHooks(
        on_iteration_start=on_iter,
        on_thought=on_thought,
        on_tool_call=on_tool,
        on_tool_result=on_result,
        on_done=on_done,
        on_error=on_error,
        on_note=on_note,
        on_rebuild=on_rebuild,
        on_advisor=on_advisor,
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
    concept_memory: str = "",
    initial_meta: Optional[dict] = None,
) -> AgentState:
    """
    启动智能体主循环。

    参数:
        goal           - 自然语言描述的目标
        llm            - LLM 后端实例
        tools          - 初始工具集 {name: ToolSpec}
        long_term      - 预置的长期记忆（可选，用于跨次运行恢复经验）
        max_iterations - 安全阀，防止无限循环
        hooks          - 观测回调（不影响核心逻辑）
        concept_memory - 概念记忆字符串（Markdown），注入 system prompt 的概念记忆章节
        initial_meta   - 初始 meta 字典（如 evolved_tools 等），在新 state 创建后合并

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
        # 合并 initial_meta（evolved_tools 配方、repair 数据等），不覆盖已存在的键
        if initial_meta:
            for k, v in initial_meta.items():
                state.meta.setdefault(k, v)
        # 注入概念记忆（优先使用 initial_meta 中的值，其次用参数）
        if concept_memory and not state.meta.get("concept_memory"):
            state.meta["concept_memory"] = concept_memory
        try:
            import os

            raw_goal = (os.environ.get("USER_GOAL") or goal).strip()
        except Exception:
            raw_goal = goal.strip()
        state.meta["_task_desc"] = raw_goal
        state.meta["scratchpad"] = f"任务描述:\n{raw_goal}\n"
        state.meta["_llm"] = llm  # 供工具（如 analyze_content）发起独立模型调用
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
        state.meta["_llm"] = llm  # 恢复运行时也更新引用
        # 用户提供了新指导，重置循环检测状态，给模型新的起点
        state.meta.pop("_loop_warn_counts", None)
        state.meta.pop("_call_sig_history", None)
        state.meta.pop("_need_user_help", None)
        _checkpoint_state(state)

    try:
        while state.iteration < max_iterations:
            if hooks.on_iteration_start:
                hooks.on_iteration_start(state.iteration, state)

            # ── Drain queued interrupt commands at each iteration boundary ────────
            # Commands injected via dashboard (web_cmd.txt → _cmd_queue) or stdin
            # are processed here, before the LLM call, so effects are immediate:
            #   /inject  → appended to short_term, LLM sees it this iteration
            #   /+N      → max_iterations extended; checked at top of while
            #   /compress→ sets state.meta["_compress_requested"], consumed below
            #   /exit    → breaks the loop cleanly
            _ih = getattr(hooks, 'interrupt_handler', None)
            if _ih is not None:
                _stop_loop = False
                while True:
                    _cmd = _ih.poll_command()
                    if _cmd is None:
                        break
                    if _cmd == '/__pause__':
                        continue   # pause sentinel only relevant when actually paused
                    _r = _ih.process_command(_cmd, state)
                    if _r == 'stop':
                        _stop_loop = True
                        break
                # Apply any /+N extensions accumulated by process_command
                _extra = state.meta.pop('_add_iterations', 0)
                if _extra:
                    max_iterations += _extra
                if _stop_loop:
                    state.meta['user_stopped'] = True
                    break
            # ─────────────────────────────────────────────────────────────────────

            # ── 高级指导员：定期 + 主动请求触发 ─────────────────────────────────
            import os
            _advisor_sys = state.meta.get("_advisor_system", "")
            if _advisor_sys:
                _should_advise, _advise_reason = should_trigger_advisor(
                    state,
                    interval=int(os.environ.get("ADVISOR_INTERVAL", "10")),
                )
                if _should_advise:
                    _advice = run_advisor(state, llm, _advisor_sys, trigger_reason=_advise_reason)
                    if _advice:
                        inject_advisor_advice(state, _advice, _advise_reason)
                        if hooks.on_advisor:
                            hooks.on_advisor(_advise_reason, _advice)
            # ─────────────────────────────────────────────────────────────────

            system = build_system_prompt(state.tools, state.long_term, scratchpad=state.meta.get("scratchpad", ""), concept_memory=state.meta.get("concept_memory", ""), runtime_patches=state.meta.get("runtime_patches"))
            messages = build_context_messages(state)

            pack = _maybe_compress_for_context(state, llm, system, messages)
            system = pack["system"]
            messages = pack["messages"]
            if state.meta.get("prompt_tokens_est"):
                est = state.meta.get("prompt_tokens_est")
                ctx = state.meta.get("context_window")
                if hooks.on_thought:
                    hooks.on_thought(
                        f"[token] prompt≈{est} / context={ctx} (est), max_tokens={getattr(llm, 'max_tokens', 'n/a')}"
                    )
                _log_token_stats(state, {
                    "role": "__token__",
                    "prompt_est": est,
                    "context_window": ctx,
                    "max_tokens": getattr(llm, "max_tokens", None),
                })

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
                verdict, verdict_dict = _review_completion_report(state, action.final_answer)

                if verdict == "needs_more_work":
                    reason = verdict_dict.get("reason", "unknown")
                    state.meta.setdefault("acceptance_failures", []).append(
                        {"iteration": state.iteration, "failures": [verdict_dict]}
                    )
                    if hooks.on_error:
                        hooks.on_error("[验收失败] 未通过验收，继续 loop 进行补救")

                    if reason == "missing_completion_report":
                        feedback = (
                            "[系统][验收失败] 缺少完成报告。请在 done 之前调用 submit_completion_report 工具，\n"
                            "提交包含 goal_understanding、completed_work、remaining_gaps、"
                            "evidence_type、evidence、outcome、confidence 的完成报告。\n"
                            "也可在草稿本中追加 ACCEPTANCE 区块（包含验收标准 criteria、"
                            "证据 evidence 路径/片段与结论 verdict）作为兼容格式。"
                        )
                    elif reason == "missing_completed_work":
                        feedback = (
                            "[系统][验收失败] 完成报告缺少已完成事项（completed_work 为空）且无最终输出。\n"
                            "请补充 submit_completion_report 中的 completed_work 字段，"
                            "或在 done 中提供 final_answer。"
                        )
                    elif reason == "artifact_missing":
                        missing = verdict_dict.get("missing", [])
                        feedback = (
                            f"[系统][验收失败] 以下宣称的产物文件不存在: {missing}。\n"
                            "请先用 write_file 生成这些文件，再重新 done。"
                        )
                    else:
                        feedback = (
                            f"[系统][验收失败] 验收未通过，原因: {reason}。\n"
                            f"详情: {json.dumps(verdict_dict, ensure_ascii=False, indent=2)}"
                        )

                    _append_short_term(state, {"role": "user", "content": feedback})
                    _checkpoint_state(state)
                    state.iteration += 1
                    continue

                # ── episodic 记忆验收门 ───────────────────────────────────────
                if not state.meta.get("_episodic_appended"):
                    episodic_path = state.meta.get("_episodic_path", "./memory_episodic.jsonl")
                    concept_path  = state.meta.get("_concept_path",  "./memory_macro.md")
                    state.meta.setdefault("acceptance_failures", []).append(
                        {"iteration": state.iteration, "failures": [{"reason": "missing_episodic"}]}
                    )
                    if hooks.on_error:
                        hooks.on_error("[验收失败] 缺少 episodic 记忆记录，继续 loop 进行补救")
                    feedback = (
                        f"[系统][验收失败] 请在 done 之前调用 append_episodic 记录本次执行摘要。\n"
                        f"  path='{episodic_path}'\n"
                        f"  summary: 一段话概括（100–300 字），包含关键操作、重要发现、对未来有检索价值的信息\n"
                        f"  tags: 逗号分隔关键词（如 'ssh,磁盘,linux'）\n\n"
                        f"同时请判断本次任务是否带来了新的领域认知或经验规律——"
                        f"如果是，请一并调用 save_concept(path='{concept_path}', content=...) 更新宏观工作记忆（按工作方向精简叙述，提及关键词即可，不写具体流程）。"
                    )
                    _append_short_term(state, {"role": "user", "content": feedback})
                    _checkpoint_state(state)
                    state.iteration += 1
                    continue

                # weak_pass 或 pass：先保存最终答案和运行摘要
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

                # weak_pass：系统主动发起 ask_user，让用户决定是否在当前基础上继续
                if verdict == "weak_pass":
                    report = verdict_dict.get("report", {})
                    outcome = report.get("outcome", "done_partial")
                    completed = report.get("completed_work", [])
                    gaps = report.get("remaining_gaps", [])

                    completed_str = "\n".join(f"  - {item}" for item in completed) if completed else "  （无）"
                    gaps_str = "\n".join(f"  - {item}" for item in gaps) if gaps else "  （无）"

                    if outcome == "done_blocked":
                        status_label = "遇到外部阻塞，已完成可做部分"
                    else:
                        status_label = "主体工作完成，有已知遗留"

                    question = (
                        f"[{status_label}]\n\n"
                        f"已完成:\n{completed_str}\n\n"
                        f"遗留/阻塞:\n{gaps_str}\n\n"
                        "是否在此基础上继续推进？"
                        "如果是，请告诉我下一步的重点；如果不需要，直接回复「完成」即可。"
                    )

                    state.meta["awaiting_input"] = question
                    state.meta["paused"] = True
                    if hooks.on_error:
                        hooks.on_error(f"[弱通过] {status_label}，暂停等待用户决策")
                    _checkpoint_state(state, status="paused")
                    break

                # 正常 pass：直接退出
                break

            if action.type == ActionType.ERROR:
                error_msg = action.thought
                if hooks.on_error:
                    hooks.on_error(error_msg)

                is_json_parse_error = isinstance(error_msg, str) and "JSON 解析失败" in error_msg
                retry_max = int(os.environ.get("JSON_PARSE_RETRY_MAX", "3"))

                if is_json_parse_error and hasattr(llm, "max_tokens"):
                    retry_n = int(state.meta.get("json_parse_retry", 0))
                    cap = int(os.environ.get("LLM_MAX_TOKENS_CAP", "32768"))
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

                # 运行时补丁：识别错误类型并写入 runtime_patches
                _apply_runtime_patch(raw_response, action, state, llm)

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
                    _auto_scratchpad_note(action, result, state, llm, hooks=hooks)

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

                # ── 循环升级：高级指导员介入 → 用户求助 ──────────────────────
                # _build_feedback 检测到循环次数超阈值时会设置此标志。
                # 若 advisor 可用且尚未为本次循环介入过，先让 advisor 尝试打破死局；
                # advisor 介入后给 agent 一次机会继续执行，若仍循环则暂停求助用户。
                need_help = state.meta.pop("_need_user_help", None)
                if need_help:
                    _advisor_sys = state.meta.get("_advisor_system", "")
                    if _advisor_sys and not state.meta.get("_advisor_tried_for_loop"):
                        _advice = run_advisor(
                            state, llm, _advisor_sys, trigger_reason="loop_detected"
                        )
                        if _advice:
                            inject_advisor_advice(state, _advice, "loop_detected")
                            state.meta["_advisor_tried_for_loop"] = True
                            if hooks.on_advisor:
                                hooks.on_advisor("loop_detected", _advice)
                            _checkpoint_state(state)
                            state.iteration += 1
                            continue  # 给 agent 一次机会，看是否能凭建议突破循环
                    # advisor 已尝试或不可用：暂停等待用户指导
                    state.meta.pop("_advisor_tried_for_loop", None)
                    state.meta["awaiting_input"] = need_help
                    state.meta["paused"] = True
                    if hooks.on_error:
                        hooks.on_error(f"[循环检测→用户求助] 自动暂停，等待用户指导")
                    _checkpoint_state(state, status="paused")
                    break
                else:
                    # 本轮未触发循环检测，重置 advisor 介入标志
                    state.meta.pop("_advisor_tried_for_loop", None)

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

    # ── 重复调用检测（连续 + 滑动窗口频率）────────────────────────────────────
    # 两种循环模式：
    #   A. 严格连续：A A A A  → consecutive >= 2 触发
    #   B. 振荡型：  A A B A A A B A  → 偶发的 B 打断连续计数，但 A 仍主导
    #      → 滑动窗口内 A 出现频率过高时触发
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

            # ── A：连续重复检测 ──────────────────────────────────────────────
            consecutive = 0
            for prev in reversed(history[-5:]):
                if prev == call_sig:
                    consecutive += 1
                else:
                    break

            # ── B：滑动窗口频率检测 ─────────────────────────────────────────
            win_size  = int(os.environ.get("LOOP_WINDOW_SIZE", "12"))
            win_thresh = int(os.environ.get("LOOP_WINDOW_THRESH", "5"))
            window = history[-win_size:]
            freq_in_window = sum(1 for h in window if h == call_sig)

            history.append(call_sig)

            args_preview = _json.dumps(action.args, ensure_ascii=False)[:300]

            loop_triggered = False
            if consecutive >= 2:
                loop_triggered = True
                repeat_warning = (
                    f"\n\n⛔ 循环检测（连续）：你已连续 {consecutive + 1} 次以完全相同的参数调用 `{action.tool}`，"
                    f"继续重试无意义。请立刻换用其他工具或直接 done 给出已知结论。"
                    f"\n以下参数禁止再次原样使用：\n```\n{args_preview}\n```"
                )
            elif freq_in_window >= win_thresh:
                loop_triggered = True
                repeat_warning = (
                    f"\n\n⛔ 循环检测（振荡）：在最近 {len(window)} 次工具调用中，"
                    f"你以完全相同的参数调用 `{action.tool}` 已达 {freq_in_window} 次，"
                    f"偶尔换词后又反复回到同一查询，说明你陷入了振荡循环。"
                    f"该查询已无法提供新信息，请立刻换用不同工具或策略，或直接 done 给出已知结论。"
                    f"\n以下参数禁止再次原样使用：\n```\n{args_preview}\n```"
                )

            # ── C：循环警告升级（向用户求助） ───────────────────────────────────
            # 当某工具连续触发循环警告 LOOP_HARD_BLOCK_AFTER 次仍不改变策略，
            # 设置 _need_user_help 标志，主循环将自动暂停并向用户请求指导。
            hard_block_after = int(os.environ.get("LOOP_HARD_BLOCK_AFTER", "3"))
            if loop_triggered:
                warn_counts = state.meta.setdefault("_loop_warn_counts", {})
                warn_counts[action.tool] = warn_counts.get(action.tool, 0) + 1
                if warn_counts[action.tool] >= hard_block_after:
                    raw_goal = (state.meta.get("_task_desc") or "").strip()
                    goal_hint = f"\n当前目标：{raw_goal[:200]}" if raw_goal else ""
                    scratchpad = (state.meta.get("scratchpad") or "").strip()
                    sp_hint = f"\n\n当前草稿本摘要：\n{scratchpad[:400]}" if scratchpad else ""
                    state.meta["_need_user_help"] = (
                        f"我在执行任务时陷入了循环：已连续 {warn_counts[action.tool]} 次调用 `{action.tool}` "
                        f"（参数：{args_preview[:200]}）但未能取得进展。"
                        f"{goal_hint}{sp_hint}\n\n"
                        f"请问您有什么建议？例如：提供新的解决思路、指出绕过方式，或告知是否可以跳过此步骤。"
                    )
                    repeat_warning += (
                        f"\n\n⛔⛔ 循环升级：你已经收到 {warn_counts[action.tool]} 次循环警告但仍重复调用 `{action.tool}`。"
                        f"\n系统将在本次调用完成后自动暂停并向用户请求指导。"
                    )
            else:
                # 非循环调用：重置该工具的警告计数，清除待求助标志
                warn_counts = state.meta.get("_loop_warn_counts", {})
                if action.tool in warn_counts:
                    warn_counts[action.tool] = 0
                state.meta.pop("_need_user_help", None)

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
                    + repeat_warning
                )
            # RUN_DIR 不可用时降级为截断（保持原有行为）
            out2 = _summarize_large_text(out, max_chars)
            return (
                f"[工具: {action.tool}] 执行成功\n"
                f"输出(可能已截断):\n{out2}"
                + repeat_warning
            )

        return (
            f"[工具: {action.tool}] 执行成功\n"
            f"输出:\n{out}"
            + repeat_warning
        )
    else:
        return (
            f"[工具: {action.tool}] 执行失败\n"
            f"错误: {result.error}\n"
            f"请分析原因，调整策略后重试（可换用其他工具或修改参数）。"
            + repeat_warning
        )
