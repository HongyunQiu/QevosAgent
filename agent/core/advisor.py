"""
高级指导员模块

职责：在 agent 主循环的关键时机，以完全独立的上下文和视角，
审视当前执行状态并给出战略性指导意见。

触发时机：
  1. 定期触发（每 ADVISOR_INTERVAL 轮，默认 10）
  2. 循环检测触发（_need_user_help 被设置时，在暂停前先尝试 advisor 介入）
  3. Agent 主动请求（调用 request_advisor 工具）

设计原则：
  - 独立上下文：advisor 调用不携带主 agent 的对话历史，避免"近视"
  - 过滤噪声：跳过含 AGENTS.md 的 short_term[0]，仅传递有信息量的内容
  - 不影响主流程：任何异常都静默降级，advisor 失败不中断执行

依赖方向：types ← llm ← compression ← advisor（不被 loop 以外的模块引用）
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from .types import AgentState
from .llm import LLMBackend


# ── 上下文构建 ────────────────────────────────────────────────────────────────

def _build_advisor_context(state: AgentState) -> str:
    """为 advisor 构建输入上下文。

    特意跳过 short_term[0]（含 AGENTS.md 和目标前缀），
    使用 state.meta["_task_desc"] 获取纯粹的用户目标。
    """
    # 纯粹的用户目标（不含 AGENTS.md 前缀）
    raw_goal = (state.meta.get("_task_desc") or getattr(state, "goal", "") or "").strip()

    # 当前草稿本
    scratchpad = (state.meta.get("scratchpad") or "").strip()

    # 最近执行历史：跳过 short_term[0]（含 AGENTS.md + 目标前缀），过滤元数据记录
    recent_msgs = []
    for m in state.short_term[1:]:  # 跳过第一条（含 AGENTS.md + 目标前缀）
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue  # 跳过 __token__ 等元数据记录
        content = m.get("content", "")
        if not content or not content.strip():
            continue
        # 截断过长内容，避免 advisor 上下文膨胀
        if len(content) > 400:
            content = content[:400] + "…[截断]"
        recent_msgs.append(f"[{role}] {content}")

    # 取最后 15 条
    recent_msgs = recent_msgs[-15:]
    history_text = "\n---\n".join(recent_msgs) if recent_msgs else "（暂无历史记录）"

    iteration = getattr(state, "iteration", 0)

    parts = [
        f"## 当前迭代轮次\n第 {iteration} 轮",
        f"## 任务目标\n{raw_goal[:800]}",
    ]

    if scratchpad:
        parts.append(f"## 草稿本（Agent 当前工作状态）\n{scratchpad[:1500]}")
    else:
        parts.append("## 草稿本\n（草稿本为空）")

    parts.append(f"## 最近执行历史（最后 {len(recent_msgs)} 条）\n{history_text}")

    return "\n\n".join(parts)


# ── 独立日志 ──────────────────────────────────────────────────────────────────

def _log_advisor_call(
    state: AgentState,
    trigger_reason: str,
    context: str,
    advice: Optional[str],
    status: str,  # "ok" | "empty" | "failed"
) -> None:
    """将 advisor 的完整调用记录（发送的 context + 收到的 advice）写入独立文件。

    每条记录含对应的 short_term 迭代数，便于与主执行日志交叉查阅。
    文件路径由 state.meta["_advisor_log_path"] 指定（通常为 run_dir/advisor_log.jsonl）。
    写入失败时静默忽略，不影响主流程。
    """
    log_path = state.meta.get("_advisor_log_path")
    if not log_path:
        return
    try:
        entry = {
            "ts":        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "iteration": getattr(state, "iteration", 0),
            "trigger":   trigger_reason,
            "status":    status,
            "context":   context,
            "advice":    advice,
        }
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── 主调用 ────────────────────────────────────────────────────────────────────

def run_advisor(
    state: AgentState,
    llm: LLMBackend,
    advisor_system: str,
    trigger_reason: str = "periodic",
) -> Optional[str]:
    """以独立上下文调用 LLM，获取高级指导员的审视意见。

    无论成功还是失败，都向 advisor_log.jsonl 写一条完整记录（context + advice）。
    失败时静默返回 None，不影响主流程。
    """
    if not advisor_system or not advisor_system.strip():
        return None

    context: Optional[str] = None
    try:
        context = _build_advisor_context(state)
        user_msg = (
            f"触发原因：{trigger_reason}\n\n"
            f"请审视以下 Agent 的当前状态，给出战略性指导意见。\n\n"
            f"---\n{context}\n---"
        )

        max_tokens = int(os.environ.get("ADVISOR_MAX_TOKENS", "800"))
        advice = llm.complete_text(
            messages=[{"role": "user", "content": user_msg}],
            system=advisor_system,
            max_tokens=max_tokens,
        ).strip()

        advice = advice if advice else None
        _log_advisor_call(state, trigger_reason, context, advice, "ok" if advice else "empty")
        return advice

    except Exception:
        _log_advisor_call(state, trigger_reason, context or "(context build failed)", None, "failed")
        return None


# ── 触发判断 ──────────────────────────────────────────────────────────────────

def should_trigger_advisor(
    state: AgentState,
    interval: int = 10,
) -> Tuple[bool, str]:
    """检查是否应触发定期/主动请求型 advisor。

    返回 (should_trigger, reason)。
    注意：loop_detected 触发由 loop.py 直接处理，不经过这里。
    """
    # Agent 主动请求（优先级最高）
    if state.meta.pop("_advisor_requested", False):
        reason = state.meta.pop("_advisor_request_reason", "agent_requested")
        return True, reason

    # 定期触发（第 0 轮跳过，那时什么都还没做）
    iteration = getattr(state, "iteration", 0)
    if iteration == 0:
        return False, ""

    last_advised = state.meta.get("_advisor_last_iter", 0)
    if iteration - last_advised >= interval:
        return True, f"periodic (iter={iteration})"

    return False, ""


# ── 注入 ──────────────────────────────────────────────────────────────────────

def inject_advisor_advice(
    state: AgentState,
    advice: str,
    reason: str,
) -> None:
    """将指导员意见注入 short_term，并落盘到 short_term.jsonl。"""
    from .compression import _get_persistence

    msg = {
        "role": "user",
        "content": (
            f"[高级指导员 · 触发: {reason}]\n\n"
            f"{advice}\n\n"
            "---\n"
            "以上是来自独立视角的战略性审视意见，供参考。"
            "请结合当前任务状态判断是否调整策略。"
        ),
    }
    state.short_term.append(msg)
    state.meta["_advisor_last_iter"] = getattr(state, "iteration", 0)

    # 落盘到 short_term.jsonl
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.append_short_term(msg)
