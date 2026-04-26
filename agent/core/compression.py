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
from datetime import datetime, timezone
from typing import Optional

from .types_def import Action, AgentHooks, AgentState, ToolResult
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


# ── LLM 全文压缩（基于完整上下文输出新草稿本）────────────────────────────────

def _llm_compress_full_history(messages: list[dict], state: AgentState, llm: LLMBackend) -> str:
    """基于已组装好的完整消息列表，调用模型输出结构化压缩摘要。

    与切片摘要的区别：模型看到完整执行历史，能做全局判断，
    不会因为只看"被丢弃的切片"而丢失上下文关联。
    输出直接作为新草稿本内容，替代原始的消息序列。
    """
    if not messages:
        return ""

    goal_text = (getattr(state, "goal", "") or "")[:400]

    compress_system = (
        "你是一个智能体执行历史的压缩专家。\n"
        "你将收到一段智能体与工具交互的完整消息历史。\n"
        "请将其压缩为简洁、结构化的执行摘要，作为后续步骤的工作记忆。\n\n"
        "输出格式（直接输出纯文本，不要 JSON，不要标题装饰）：\n"
        "• 已完成：逐条列出已完成的步骤及其关键结果\n"
        "• 关键发现：执行中发现的重要事实、数据或结论\n"
        "• 遇到的问题：障碍及采取的应对方式（若有）\n"
        "• 当前状态：目前进展到哪一步，下一步计划是什么\n\n"
        "压缩原则：\n"
        "- 保留：步骤结果、关键数据、重要决策、有效的解决路径\n"
        "- 丢弃：工具原始输出的冗长内容、重复失败的重试、无结论的中间思考\n"
        "- 总长控制在 500 字以内，语言简洁直接"
    )

    # 在现有消息末尾追加压缩请求，让模型基于完整上下文输出
    compress_request = {
        "role": "user",
        "content": (
            f"[系统指令] 请将以上执行历史压缩为结构化摘要。\n"
            f"任务目标参考：{goal_text}"
        ),
    }

    try:
        summary = llm.complete_text(
            messages=messages + [compress_request],
            system=compress_system,
            max_tokens=500,
        ).strip()
        return summary[:1000]  # 硬上限
    except Exception:
        return ""


def _write_summary_to_scratchpad(state: AgentState, summary: str, tag: str = "compress") -> None:
    """将摘要追加写入草稿本并落盘。超限时从中部裁剪，保留头部（任务描述）和最新内容。"""
    if not summary or not summary.strip():
        return
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    cur = state.meta.get("scratchpad", "") or ""
    new_sp = cur + f"\n[{tag}] {summary.strip()}"
    if len(new_sp) > max_chars:
        lines = new_sp.splitlines(keepends=True)
        head = "".join(lines[:3])
        body = new_sp[len(head):]
        overflow = len(head) + len(body) - max_chars
        body = body[overflow:]
        new_sp = head + body
    state.meta["scratchpad"] = new_sp
    persistence = _get_persistence(state)
    if persistence is not None:
        try:
            persistence.save_scratchpad(new_sp)
        except Exception:
            pass


def _overwrite_scratchpad(state: AgentState, new_content: str) -> None:
    """用 LLM 压缩输出覆盖草稿本（保留任务描述头部 + 新的全量摘要）。"""
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    task_desc = (state.meta.get("_task_desc") or "").strip()
    if task_desc:
        header = f"任务描述:\n{task_desc}\n\n"
        body = new_content.strip()
        new_sp = header + body
    else:
        new_sp = new_content.strip()
    # 超限时截尾（头部任务描述优先保留）
    if len(new_sp) > max_chars:
        new_sp = new_sp[:max_chars]
    state.meta["scratchpad"] = new_sp
    persistence = _get_persistence(state)
    if persistence is not None:
        try:
            persistence.save_scratchpad(new_sp)
        except Exception:
            pass


def _collapse_to_bridge(state: AgentState) -> None:
    """LLM 全文压缩完成后，将 short_term 缩减为 [goal, bridge]。

    历史摘要已写入草稿本（体现在 system prompt 中），short_term 只需保留
    原始目标和一条指向草稿本的桥接消息。
    """
    goal_msg = state.short_term[0] if state.short_term else None
    bridge = {
        "role": "user",
        "content": (
            "[系统] 执行历史已通过模型压缩，关键信息已整理至 system prompt 的草稿本中。"
            "请以草稿本内容作为历史执行记录的完整参考，继续后续任务。"
        ),
    }
    state.short_term = ([goal_msg] if goal_msg else []) + [bridge]


# ── 压缩核心（可被工具调用或兜底程序调用）────────────────────────────────────

def compress_context(
    state: AgentState,
    summary: str = "",
    messages: Optional[list[dict]] = None,
    use_llm_summary: bool = True,
) -> str:
    """压缩 short_term 上下文，为后续对话腾出 token 空间。

    可被模型主动调用（通过 tool_compress_context），也可被自动兜底触发。

    压缩策略优先级：
    1. summary 非空 → 直接使用 agent 手动提供的摘要（质量最高，无额外 LLM 开销）
    2. use_llm_summary=True 且 llm 可用 → 将完整 messages 发给模型，让其基于全文输出
       结构化摘要；压缩后 short_term 缩减为 [goal, bridge]
    3. 兜底 → 机械裁剪（keep_last=6），无摘要

    Args:
        summary:         agent 手动提供的摘要，写入草稿本后再裁剪。
        messages:        已组装好的完整消息列表（来自 build_context_messages），
                         供 LLM 全文压缩使用；为 None 时自动从 state 重建。
        use_llm_summary: 是否允许调用 LLM 生成摘要（默认 True）。
    """
    before = len(state.short_term)

    # ── 路径 1：agent 手动提供摘要 ────────────────────────────────────────────
    if summary and summary.strip():
        _write_summary_to_scratchpad(state, summary.strip(), tag="compress:manual")
        _trim_short_term(state, keep_last=6)
        _compact_short_term_messages(state, per_message_chars=1500)
        method = "手动摘要+机械裁剪"

    # ── 路径 2：LLM 全文压缩 ──────────────────────────────────────────────────
    elif use_llm_summary:
        llm = state.meta.get("_llm")
        if llm is not None and state.short_term:
            # 若调用方未传入已组装的 messages，则从 state 重建
            if messages is None:
                messages = build_context_messages(state)
            llm_summary = _llm_compress_full_history(messages, state, llm)
            if llm_summary:
                # 用新摘要覆盖整个草稿本（它已是全量压缩，不需要追加）
                _overwrite_scratchpad(state, llm_summary)
                # short_term 缩减为 [goal, bridge]，历史已在草稿本中
                _collapse_to_bridge(state)
                method = "llm全文压缩"
            else:
                # LLM 调用失败，降级到机械裁剪
                _trim_short_term(state, keep_last=6)
                _compact_short_term_messages(state, per_message_chars=1500)
                method = "机械裁剪(llm降级)"
        else:
            _trim_short_term(state, keep_last=6)
            _compact_short_term_messages(state, per_message_chars=1500)
            method = "机械裁剪(无llm)"

    # ── 路径 3：纯机械裁剪兜底 ────────────────────────────────────────────────
    else:
        _trim_short_term(state, keep_last=6)
        _compact_short_term_messages(state, per_message_chars=1500)
        method = "机械裁剪"

    after = len(state.short_term)
    msg = (
        f"上下文已压缩：short_term {before}→{after} 条"
        f"，丢弃 {max(0, before - after)} 条"
        f"，压缩方式={method}"
    )
    state.long_term.append(f"[compress_context] {msg}")
    return msg


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

    # If we're near the limit, compress and keep going.
    if est and est > int(ctx * warn_ratio):
        # 把已组装好的 messages 直接传入，避免重建；LLM 可基于全文输出高质量摘要
        compress_context(state, messages=messages, use_llm_summary=True)
        state.long_term.append(
            f"[自我修复] prompt≈{est} tokens 接近 context={ctx}，已自动触发 compress_context。"
        )
        # After trimming, recompute once (best-effort)
        try:
            system2 = build_system_prompt(state.tools, state.long_term, scratchpad=state.meta.get("scratchpad", ""), concept_memory=state.meta.get("concept_memory", ""), runtime_patches=state.meta.get("runtime_patches"))
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


# ── inline 模式：直接应用 LLM 同步输出的草稿笔记 ─────────────────────────────

def _apply_inline_scratchpad_note(
    action: Action,
    state: AgentState,
    hooks: Optional[AgentHooks] = None,
) -> None:
    """inline 模式下，将 action.scratchpad_note 追加到草稿本。

    与 _auto_scratchpad_note 共享相同的截断策略，但无需额外 LLM 调用。
    """
    note = getattr(action, "scratchpad_note", None)
    if not note:
        return
    note = note.strip()[:200]
    if not note:
        return

    try:
        iter_n = getattr(state, "iteration", 0)
        entry = f"\n[iter{iter_n}|inline] {note}"

        if hooks is not None and hooks.on_note:
            hooks.on_note(action.tool or "inline", note)

        cur = state.meta.get("scratchpad", "")
        max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
        new_sp = cur + entry

        if len(new_sp) > max_chars:
            lines = new_sp.splitlines(keepends=True)
            head = "".join(lines[:3])
            body = new_sp[len(head):]
            overflow = len(head) + len(body) - max_chars
            body = body[overflow:]
            new_sp = head + body

        state.meta["scratchpad"] = new_sp

        persistence = _get_persistence(state)
        if persistence is not None:
            persistence.save_scratchpad(new_sp)

    except Exception:
        pass


# ── 运行时补丁 ────────────────────────────────────────────────────────────────

# 已知 JSON 错误类型 → 补丁规则（静态映射）
_JSON_ERROR_PATCH_RULES: dict[str, str] = {
    "bare_newline":        "JSON字符串内的换行必须转义为\\n，禁止直接回车换行",
    "unescaped_backslash": "Windows路径的反斜杠\\必须写成\\\\，或改用正斜杠/",
    "unterminated_string": "超长内容先用write_file写入文件，args/final_answer只引用路径，避免截断",
    "split_structure":     "thought/action/tool/args必须全部在同一个顶层{}内，thought不能单独成对象",
    "single_quote_key":    "JSON的key必须用双引号\"\"，不能用单引号''",
    "prose_with_json":        "禁止用```json```代码围栏包裹输出，必须直接输出裸JSON对象，不加任何前缀或围栏",
    "unquoted_string_value":   "JSON字符串值必须用双引号括起来，例如 \"thought\": \"你的思考内容\" 而不是 \"thought\": 你的思考内容",
    "unescaped_string_quote":  "JSON字符串值内的双引号必须转义为\\\"，例如 \"thought\": \"描述为\\\"引用文字\\\"\" 而非 \"thought\": \"描述为\"引用文字\"\"",
}

# 每次运行最多触发多少次 mini LLM 诊断（针对未知类型）
_PATCH_UNKNOWN_MAX = int(os.environ.get("RUNTIME_PATCH_UNKNOWN_MAX", "2"))
# 候选规则出现多少次后升级为正式 patch
_PATCH_CANDIDATE_THRESHOLD = 2


def _log_patch_event(
    state: AgentState,
    event: str,
    error_type: str,
    rule: str,
    raw_snippet: str = "",
) -> None:
    """将运行时补丁事件写入独立 JSONL 日志文件。

    事件类型（event）:
      rule_added        - 静态规则首次加入 runtime_patches
      rule_skipped      - 静态规则已存在，跳过
      candidate_recorded - unknown 类型候选规则记录，尚未晋升
      candidate_promoted - 候选规则达到阈值，晋升为正式 patch
      diagnosis_skipped  - unknown 类型因频控跳过诊断

    写入失败时静默忽略，不影响主流程。
    """
    log_path = state.meta.get("_patch_log_path")
    if not log_path:
        return
    try:
        entry = {
            "ts":        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "iteration": getattr(state, "iteration", 0),
            "event":     event,
            "error_type": error_type,
            "rule":      rule,
            "raw_snippet": raw_snippet[:200] if raw_snippet else "",
        }
        from pathlib import Path as _Path
        _Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _apply_runtime_patch(
    raw: str,
    action: "Action",
    state: AgentState,
    llm: LLMBackend,
    hooks: Optional[AgentHooks] = None,
) -> None:
    """根据 JSON 错误类型，向 meta['runtime_patches'] 追加补丁规则。

    - 已知类型：静态映射，去重后直接追加。
    - 未知类型：触发 mini LLM 诊断（频控+候选晋升机制）。
    每个事件均写入独立日志文件，并触发 hooks.on_patch 控制台回调。
    """
    error_type = getattr(action, "error_type", None)
    if not error_type:
        return

    patches: list[str] = state.meta.setdefault("runtime_patches", [])
    raw_snippet = raw[:200]

    # ── 已知类型：静态规则 ────────────────────────────────────────────────────
    rule = _JSON_ERROR_PATCH_RULES.get(error_type)
    if rule:
        if rule not in patches:
            patches.append(rule)
            state.long_term.append(f"[运行时补丁] 新增格式规范: {rule}")
            _log_patch_event(state, "rule_added", error_type, rule, raw_snippet)
            if hooks is not None and hooks.on_patch:
                hooks.on_patch("rule_added", error_type, rule)
        else:
            _log_patch_event(state, "rule_skipped", error_type, rule, raw_snippet)
        return

    # ── 未知类型：mini LLM 诊断 ──────────────────────────────────────────────
    if error_type != "unknown":
        return

    diagnosed = state.meta.get("_patch_unknown_diagnosed", 0)
    if diagnosed >= _PATCH_UNKNOWN_MAX:
        _log_patch_event(state, "diagnosis_skipped", error_type, "", raw_snippet)
        return

    try:
        mini_system = (
            "你是JSON格式错误分析助手。"
            "根据以下错误信息和原始输出，输出一条简洁的格式规范（≤30字），"
            "用于指导模型避免此类错误。直接输出规则文字，不要解释，不要编号。"
        )
        mini_user = (
            f"错误信息: {action.thought[:200]}\n"
            f"原始输出(截断): {raw[:300]}"
        )
        candidate = llm.complete_text(
            messages=[{"role": "user", "content": mini_user}],
            system=mini_system,
            max_tokens=60,
        ).strip()

        if not candidate or len(candidate) > 80:
            return

        state.meta["_patch_unknown_diagnosed"] = diagnosed + 1

        # 候选晋升：出现 >= 阈值次数才正式加入 patches
        candidates: dict[str, int] = state.meta.setdefault("_patch_candidates", {})
        candidates[candidate] = candidates.get(candidate, 0) + 1

        if candidates[candidate] >= _PATCH_CANDIDATE_THRESHOLD:
            if candidate not in patches:
                patches.append(candidate)
                state.long_term.append(f"[运行时补丁] 新增未知类型规范(已验证): {candidate}")
                _log_patch_event(state, "candidate_promoted", error_type, candidate, raw_snippet)
                if hooks is not None and hooks.on_patch:
                    hooks.on_patch("candidate_promoted", error_type, candidate)
        else:
            _log_patch_event(state, "candidate_recorded", error_type, candidate, raw_snippet)
            if hooks is not None and hooks.on_patch:
                hooks.on_patch("candidate_recorded", error_type, candidate)

    except Exception:
        pass


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
