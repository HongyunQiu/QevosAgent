"""
上下文管理模块
职责：管理 LLM 上下文窗口的空间，防止 prompt 超限。

包含三类功能：

【压缩/裁剪】在 loop 每轮迭代开始前调用，削减 short_term 体积：
  - _get_persistence              持久化器懒加载（辅助）
  - _summarize_large_text         JSON 感知的文本截断（辅助）
  - _compact_short_term_messages  单条消息体限长
  - _trim_short_term              删除中间历史 + 插入桥接消息
  - _maybe_compress_for_context   Token 监控，超限时自动触发上面两个

【上下文重建】工具反复失败时调用，打破模型的重复循环：
  - _rebuild_context_on_hard_block  清空吸引子上下文，注入新起点指令

【笔记提炼】每次工具执行完成后调用，非压缩，而是向草稿本增量写入：
  - _auto_scratchpad_note  mini LLM call，从工具输出提炼关键发现

依赖方向：types ← llm ← compression ← loop（无循环）
"""

import json
import os
from typing import Optional

from .types import Action, AgentHooks, AgentState, ToolResult
from .llm import LLMBackend, build_system_prompt, build_context_messages, _extract_json
from ..runtime.persistence import RunPersistence


# 这些工具的成功 ACK 对模型无信息价值（结果已通过 system prompt 中的 scratchpad 反映）
_ACK_ONLY_TOOLS = frozenset({"scratchpad_set", "scratchpad_append", "raw_append"})


# ── 持久化器 ──────────────────────────────────────────────────────────────────

def _get_persistence(state: AgentState) -> Optional[RunPersistence]:
    persistence = getattr(state, "persistence", None)
    if persistence is not None:
        return persistence
    try:
        run_dir = os.environ.get("RUN_DIR")
        if not run_dir:
            return None
        persistence = RunPersistence(run_dir)
        state.persistence = persistence
        return persistence
    except Exception:
        return None


# ── 文本截断 ──────────────────────────────────────────────────────────────────

def _summarize_large_text(text: str, limit: int) -> str:
    """Summarize/truncate large tool outputs for prompt safety."""
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s

    # Best-effort JSON summary
    try:
        obj = json.loads(s)
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


# ── 消息体限长 ────────────────────────────────────────────────────────────────

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


# ── 历史修剪 ──────────────────────────────────────────────────────────────────

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


# ── 自动触发压缩 ──────────────────────────────────────────────────────────────

def _maybe_compress_for_context(state: AgentState, llm: LLMBackend, system: str, messages: list[dict]) -> dict:
    """Estimate prompt tokens and auto-trim when close to context limit."""
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
            system2 = build_system_prompt(state.tools, state.long_term, concept_memory=state.meta.get("concept_memory", ""))
            messages2 = build_context_messages(state)
            est2 = int(llm.estimate_tokens(messages2, system2))
            state.meta["prompt_tokens_est"] = est2
            return {"system": system2, "messages": messages2}
        except Exception:
            return {"system": system, "messages": messages}

    return {"system": system, "messages": messages}


# ── 自动笔记提炼 ──────────────────────────────────────────────────────────────

def _auto_scratchpad_note(
    action: Action,
    result: ToolResult,
    state: AgentState,
    llm: LLMBackend,
    hooks: Optional[AgentHooks] = None,
) -> None:
    """在工具执行成功后，用超短 LLM 对话自动提取关键发现并追加到草稿本。

    设计原则：
    - mini call 携带 goal + 当前草稿摘要 + 工具结果，做到目标感知而不只是局部摘要
    - 直接写 state.meta["scratchpad"]，绕过工具系统，不产生 ACK 噪声
    - 超限时从头部裁剪（保留任务描述行），优先保留最新记录
    - 任何异常静默跳过，不影响主流程
    """
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

        # 触发控制台钩子（品红色笔记提示）
        if hooks is not None and hooks.on_note:
            hooks.on_note(action.tool, note)

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


# ── 上下文重建 ────────────────────────────────────────────────────────────────

def _rebuild_context_on_hard_block(
    blocked_tool: str,
    state: AgentState,
    hooks: Optional[AgentHooks] = None,
) -> None:
    """硬封锁触发时，重建 short_term 上下文以打破吸引子效应。

    上下文充满了反复失败的同类工具调用时，模型会被强烈吸引继续重复。
    重建策略：
      1. 保留原始目标（short_term[0]）
      2. 从草稿本引入历史路线（已知信息 + 已完成步骤）
      3. 将最近 N 次失败的该工具调用整理为反例，明确标注"禁止重复"
      4. 注入新起点指令：先做方法研究（web_search），再制定新方案
    """
    try:
        # ── 1. 提取原始目标 ────────────────────────────────────────────────
        goal_msg = state.short_term[0] if state.short_term else None
        raw_goal = (state.meta.get("_task_desc") or getattr(state, "goal", "")) or ""

        # ── 2. 草稿本历史 ─────────────────────────────────────────────────
        scratchpad = (state.meta.get("scratchpad") or "").strip()
        sp_section = ""
        if scratchpad:
            sp_section = f"\n\n## 已记录的执行历史（草稿本）\n{scratchpad[:1500]}"

        # ── 3. 提取最近失败的该工具调用作为反例 ──────────────────────────
        failed_examples: list[str] = []
        for m in state.short_term:
            if m.get("role") != "assistant":
                continue
            content = m.get("content", "")
            data, _ = _extract_json(content)
            if data is None:
                continue
            if data.get("tool") == blocked_tool and data.get("action") == "tool_call":
                args = data.get("args", {})
                args_str = json.dumps(args, ensure_ascii=False)
                example = f"  - {blocked_tool}({args_str[:120]})"
                if example not in failed_examples:
                    failed_examples.append(example)
        # 最多展示8条，避免反例列表过长
        failed_examples = failed_examples[-8:]
        if failed_examples:
            examples_text = "\n".join(failed_examples)
            fail_section = (
                f"\n\n## 已证明无效的方法（反例，禁止重复）\n"
                f"以下 `{blocked_tool}` 调用均以失败或无进展告终，**不要再尝试任何类似变体**：\n"
                f"{examples_text}"
            )
        else:
            fail_section = (
                f"\n\n## 注意\n`{blocked_tool}` 工具在本任务中已多次失败，请不要再依赖它。"
            )

        # ── 4. 新起点指令 ──────────────────────────────────────────────────
        # 从 goal 中提取关键词，给 web_search 一个启发性提示
        kw_hint = raw_goal[:80].replace("\n", " ")
        new_directive = (
            f"\n\n## 下一步：先做方法研究，再行动\n"
            f"当前策略已陷入死局。请按以下顺序重新出发：\n"
            f"  1. **web_search**：先搜索解决方案，例如 \"{kw_hint}\" 或官方文档/社区方案\n"
            f"  2. 根据搜索结果制定全新的执行路径（可能与之前完全不同）\n"
            f"  3. 若确实无法完成，使用 **ask_user** 向用户报告具体障碍并请求指导\n"
            f"  4. 或使用 **done** 诚实报告当前状态、已尝试方法和失败原因\n"
            f"\n`{blocked_tool}` 工具在你找到新的可行方案之前**保持封锁**，调用会被系统拒绝。"
        )

        # ── 5. 重建 short_term ────────────────────────────────────────────
        rebuild_content = (
            f"[系统][上下文重建] 检测到你陷入对 `{blocked_tool}` 的重复调用循环，\n"
            f"系统已清除重复历史并重建上下文，帮助你从新视角突破困境。\n"
            f"\n## 原始目标\n{raw_goal[:400]}"
            + sp_section
            + fail_section
            + new_directive
        )

        rebuild_msg = {"role": "user", "content": rebuild_content}

        # 用 [goal] + [rebuild] 替换整个 short_term（清除所有吸引子上下文）
        state.short_term = ([goal_msg] if goal_msg else []) + [rebuild_msg]

        # 触发控制台钩子（橙色醒目边框提示）
        if hooks is not None and hooks.on_rebuild:
            hooks.on_rebuild(blocked_tool, len(state.short_term))

    except Exception:
        pass  # 重建失败不影响主流程
