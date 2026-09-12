"""
高级指导员模块

职责：在 agent 主循环的关键时机，以完全独立的上下文和视角，
审视当前执行状态并给出战略性指导意见。

触发时机：
  1. 定期触发（每 ADVISOR_INTERVAL 轮，默认 15）
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

from .types_def import AgentState
from .llm import LLMBackend
from ..i18n import t


# ── 触发开关与频次 ────────────────────────────────────────────────────────────
# 四个触发源各有一个使能开关，看板「设置 → 通用设置 → 指导员」写进 .env：
#   ADVISOR_ENABLED      总开关，关掉则四个触发源全部失效
#   ADVISOR_ON_PERIODIC  定期触发（每 ADVISOR_INTERVAL 轮）
#   ADVISOR_ON_REQUEST   Agent 主动请求（request_advisor 工具）
#   ADVISOR_ON_LOOP      循环检测触发
#   ADVISOR_ON_STALL     执行图停滞触发
# 全部默认开启——没配过的老环境行为不变。
#
# 关掉 loop/stall 只摘掉升级梯上「advisor 介入」这一级，折叠吸引子与最终的
# ask_user 求助照常，循环保护不会跟着一起没了。

ADVISOR_DEFAULT_INTERVAL = 15
# 定期触发的硬下限。advisor 每次介入要先跑一遍进展日志自压缩、再做一次独立上下文
# 调用，比 15 轮更密既烧 token 又会把主线淹在指导意见里。看板输入框和手改 .env
# 都绕不过这条线——所以 clamp 放在这里，而不是只做前端校验。
ADVISOR_MIN_INTERVAL = 15

# 触发源 → 使能开关环境变量名
_TRIGGER_ENV = {
    "periodic":        "ADVISOR_ON_PERIODIC",
    "agent_requested": "ADVISOR_ON_REQUEST",
    "loop_detected":   "ADVISOR_ON_LOOP",
    "graph_stall":     "ADVISOR_ON_STALL",
}


def _env_flag(name: str, default: bool = True) -> bool:
    """读一个布尔环境变量。留空/未设 = default（全部开关默认开）。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no")


def advisor_interval() -> int:
    """定期触发间隔（轮），已按 ADVISOR_MIN_INTERVAL 取下限。"""
    try:
        val = int(float(os.environ.get("ADVISOR_INTERVAL") or ADVISOR_DEFAULT_INTERVAL))
    except (TypeError, ValueError):
        val = ADVISOR_DEFAULT_INTERVAL
    return max(ADVISOR_MIN_INTERVAL, val)


def advisor_trigger_enabled(kind: str) -> bool:
    """某个触发源当前是否启用。kind 取 _TRIGGER_ENV 的键。"""
    if not _env_flag("ADVISOR_ENABLED"):
        return False
    env_name = _TRIGGER_ENV.get(kind)
    return _env_flag(env_name) if env_name else True


# ── 上下文构建 ────────────────────────────────────────────────────────────────

# 推送进入 dashboard 的最近一次 advisor user 上下文，由 run_advisor 写入；
# loop.py / persistence 不强依赖此字段。
ADVISOR_LAST_CONTEXT_META_KEY = "_advisor_last_context"


# 用户中途说的话落进 short_term 时会带的前缀。三条输入路径各有各的写法：
#   /inject（agent 正在跑）            → [用户干预注入] / [User Injection]
#   ask_user 回答（agent 停在提问上）   → [用户补充信息] / [User input]   ← marker.user_info
#   看板带图消息的 raw 模式            → [Web用户]:
# 漏掉任何一个，这条路上的用户指令在 advisor 眼里就等于没说过。
_INJECTION_PREFIXES = (
    "[用户干预注入]", "[User Injection]",
    "[用户补充信息]", "[User input]",
    "[Web用户]", "[Web看板]",
)


def _strip_injection_prefix(content: str) -> str:
    """剥掉消息前缀，返回用户原话。

    不能用"砍掉第一行"——[Web用户]: 是行内前缀，砍第一行会把用户的话整句丢掉，
    只剩后面附带的图片提示。
    """
    for p in _INJECTION_PREFIXES:
        if content.startswith(p):
            return content[len(p):].lstrip(":：\n \t")
    return content


def _extract_user_injections(state: AgentState) -> list[dict]:
    """汇总用户中途注入的指令。

    主来源：state.meta["_user_injections"]（record_user_injection 写入，跨压缩存活，
    覆盖 /inject、ask_user 回答、上游节点回复三条路径）。
    补充来源：扫描 short_term 中带 _INJECTION_PREFIXES 前缀的 user 消息。

    两者是**合并**关系而非二选一。早期实现是"主来源非空就 return"，结果只要
    用户 /inject 过一次，后面所有 ask_user 回答都不再被扫描兜住——而 ask_user
    回答恰恰是最常见的中途输入。

    补充项按内容去重后追加在末尾并标 source="scanned"（渲染成 [iter=?]）：
    short_term 是近端窗口，能被扫到而没记账的多半就是近期消息，顺序上足够近似。
    返回按时间顺序的列表，每项 {iter, ts, content, source}。
    """
    items: list[dict] = []
    seen: set[str] = set()

    explicit = state.meta.get("_user_injections")
    if isinstance(explicit, list):
        for it in explicit:
            if not isinstance(it, dict):
                continue
            body = str(it.get("content") or "").strip()
            if not body:
                continue
            items.append({
                "iter":    int(it.get("iter") or 0),
                "ts":      it.get("ts") or "",
                "content": body,
                "source":  it.get("source") or "explicit",
            })
            seen.add(body)

    for m in state.short_term[1:]:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if not any(content.startswith(p) for p in _INJECTION_PREFIXES):
            continue
        # 记账存的就是剥完前缀的原话，所以精确比对足以去重；用子串比对会把
        # "继续" 这类短指令误判成某条长指令的一部分而整条吞掉。
        body = _strip_injection_prefix(content).strip()
        if not body or body in seen:
            continue
        items.append({"iter": 0, "ts": "", "content": body, "source": "scanned"})
        seen.add(body)

    return items


def _build_tools_catalog(state: AgentState) -> str:
    """构造一行一个的工具/能力清单，仅含名称和首行简介。"""
    tools = getattr(state, "tools", {}) or {}
    lines: list[str] = []
    for name in sorted(tools.keys()):
        spec = tools.get(name)
        desc = (getattr(spec, "description", "") or "").strip()
        # 只取首行，避免膨胀
        first_line = desc.split("\n", 1)[0].strip()
        if len(first_line) > 140:
            first_line = first_line[:140] + "…"
        lines.append(f"- {name} — {first_line}" if first_line else f"- {name}")

    # SKILL 清单给全量（名称+简介+已加载标注），advisor 才能主动建议「这任务该读
    # kicad_mcp」；只给已激活名称的话，它对没勾选的技能同样无从推荐。
    catalog = state.meta.get("_skills_catalog")
    if isinstance(catalog, str) and catalog.strip():
        lines.append("")
        lines.append(t("advisor.ctx.skills_header"))
        lines.append(catalog.strip())
    else:
        # 无 catalog（如恢复自旧 run 的 state）时退回仅列已激活名称。
        skills = state.meta.get("_active_skills")
        if isinstance(skills, list) and skills:
            lines.append("")
            lines.append(f"(已激活 SKILL：{', '.join(skills)})")
    return "\n".join(lines)


def _build_advisor_context(state: AgentState) -> str:
    """为 advisor 构建输入上下文（分节结构）。

    分节顺序：
      ## 当前迭代轮次
      ## 原始任务目标
      ## 用户后续指令      ← 治问题 1：用户中途指令永远独立成节、不截断
      ## 草稿本
      ## 工作进展日志       ← 治问题 2（由批 2 真正填充；现在若缺则跳过）
      ## 可用工具与能力     ← 治问题 3：advisor 知道有哪些工具/skill 可推荐
      ## 最近原始执行片段   ← 缩减为最后 8 条，仅作对账材料
    """
    raw_goal = (state.meta.get("_task_desc") or getattr(state, "goal", "") or "").strip()
    scratchpad = (state.meta.get("scratchpad") or "").strip()
    iteration = getattr(state, "iteration", 0)

    parts: list[str] = []
    parts.append(t("advisor.ctx.iter", iter=iteration))
    parts.append(t("advisor.ctx.goal", goal=raw_goal[:800]))

    # ── 用户后续指令（治问题 1）─────────────────────────────────────────────
    injections = _extract_user_injections(state)
    if injections:
        # 不做截断；上限保护：单条最多 2000 字符，整体最多最近 20 条。
        kept = injections[-20:]
        lines: list[str] = []
        for it in kept:
            body = it["content"]
            if len(body) > 2000:
                body = body[:2000] + t("advisor.ctx.truncated")
            tag = f"[iter={it['iter']}]" if it.get("iter") else "[iter=?]"
            # 多行原文保持原样，前置标签
            lines.append(f"- {tag} {body}")
        parts.append(t("advisor.ctx.user_inj", items="\n".join(lines)))
    else:
        parts.append(t("advisor.ctx.user_inj_empty"))

    # ── 草稿本 ─────────────────────────────────────────────────────────────
    if scratchpad:
        parts.append(t("advisor.ctx.sp", sp=scratchpad[:1500]))
    else:
        parts.append(t("advisor.ctx.sp_empty"))

    # ── 执行图（有图时才有）────────────────────────────────────────────────
    # 上面几节全是散文。advisor 光读文字很难看出"结构性"停滞——比如在两个节点
    # 之间来回三次。图是唯一能让它一眼看出这件事的输入，所以排在进展日志之前。
    try:
        from . import graph as _graph

        graph_text = _graph.render(state)
        if graph_text.strip():
            parts.append(t("advisor.ctx.graph", graph=graph_text.strip()[:2500]))
    except Exception:
        pass

    # ── 工作进展日志（批 2 启用；批 1 阶段：仅在已有内容时输出）──────────────
    progress = state.meta.get("_progress_log")
    if isinstance(progress, str) and progress.strip():
        method = state.meta.get("_progress_log_method") or "unknown"
        log_iter = state.meta.get("_progress_log_iter") or 0
        parts.append(t("advisor.ctx.progress",
                       method=method, iter=log_iter, log=progress.strip()[:4000]))

    # ── 可用工具与能力（治问题 3）───────────────────────────────────────────
    tools_text = _build_tools_catalog(state)
    if tools_text:
        parts.append(t("advisor.ctx.tools", items=tools_text))
    else:
        parts.append(t("advisor.ctx.tools_empty"))

    # ── 最近原始执行片段（仅作对账）─────────────────────────────────────────
    recent_msgs: list[str] = []
    for m in state.short_term[1:]:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > 500:
            content = content[:500] + t("advisor.ctx.truncated")
        recent_msgs.append(f"[{role}] {content}")
    recent_msgs = recent_msgs[-8:]  # 由 15 缩到 8
    history_text = "\n---\n".join(recent_msgs) if recent_msgs else t("advisor.ctx.no_history")
    parts.append(t("advisor.ctx.history", n=len(recent_msgs), hist=history_text))

    return "\n\n".join(parts)


# ── 独立日志 ──────────────────────────────────────────────────────────────────

def _log_advisor_call(
    state: AgentState,
    trigger_reason: str,
    context: str,
    advice: Optional[str],
    status: str,  # "ok" | "empty" | "failed"
    system: str = "",
) -> None:
    """将 advisor 的完整调用记录（发送的 system + context + advice）写入独立文件。

    每条记录含对应的 short_term 迭代数，便于与主执行日志交叉查阅。
    文件路径由 state.meta["_advisor_log_path"] 指定（通常为 run_dir/advisor_log.jsonl）。

    同时把"最后一次调用快照"写入 run_dir/advisor_last.json，供 dashboard 一目了然地
    展示当前 advisor 看到的完整上文（无需自行解析 jsonl）。

    写入失败时静默忽略，不影响主流程。
    """
    log_path = state.meta.get("_advisor_log_path")
    if not log_path:
        return
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    iteration = getattr(state, "iteration", 0)
    entry = {
        "ts":        ts,
        "iteration": iteration,
        "trigger":   trigger_reason,
        "status":    status,
        "system":    system,
        "context":   context,
        "advice":    advice,
    }
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 最后一次快照（dashboard 直读，无需解析 jsonl）
    try:
        snap_path = Path(log_path).parent / "advisor_last.json"
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
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
    # 总开关兜底：调用点已各自判过，这里再拦一道，免得将来新增触发源漏判。
    if not _env_flag("ADVISOR_ENABLED"):
        return None

    context: Optional[str] = None
    try:
        context = _build_advisor_context(state)
        user_msg = t("advisor.trigger_msg", reason=trigger_reason, context=context)

        max_tokens = int(os.environ.get("ADVISOR_MAX_TOKENS", "800"))
        advice = llm.complete_text(
            messages=[{"role": "user", "content": user_msg}],
            system=advisor_system,
            max_tokens=max_tokens,
        ).strip()

        advice = advice if advice else None
        _log_advisor_call(state, trigger_reason, context, advice, "ok" if advice else "empty", system=advisor_system)
        return advice

    except Exception:
        _log_advisor_call(state, trigger_reason, context or "(context build failed)", None, "failed", system=advisor_system)
        return None


# ── 主对话自压缩进展日志（批 2 核心）──────────────────────────────────────────
# 利用主 agent 的 KV 缓存做一次"自我汇报"调用，把结构化的工作进展日志写入
# state.meta["_progress_log"]。由于 system + history 都已被主对话缓存，这次
# 调用只付输出 tokens；进展日志随后被 advisor 上下文读取，让其看到 15 条窗口
# 之外的宏观进展，治"短视"。

_PROGRESS_LOG_FILENAME = "progress_log.md"


def _persist_progress_log(state: AgentState, log_text: str) -> None:
    """把进展日志落盘到 run_dir/progress_log.md，dashboard 直接 watch。"""
    log_path = state.meta.get("_advisor_log_path")
    if not log_path:
        return
    try:
        out = Path(log_path).parent / _PROGRESS_LOG_FILENAME
        header = (
            f"<!-- iter={state.meta.get('_progress_log_iter', 0)} "
            f"method={state.meta.get('_progress_log_method', 'unknown')} -->\n"
        )
        out.write_text(header + (log_text or ""), encoding="utf-8")
    except Exception:
        pass


def run_self_progress_summary(state: AgentState, llm: LLMBackend) -> Optional[str]:
    """让主 agent 在当前完整上下文上输出一份诚实的工作进展日志。

    复用主 agent 的 system + short_term，因此 KV 缓存完全命中 —— 只付输出 tokens。
    输出不写入 short_term（不污染主对话），仅写入：
      - state.meta["_progress_log"]
      - state.meta["_progress_log_iter"]
      - state.meta["_progress_log_method"] = "llm_self"
      - run_dir/progress_log.md

    失败时返回 None，保留旧 _progress_log。
    """
    # 延迟导入，避免顶层循环依赖
    from .llm import build_system_prompt, build_context_messages

    try:
        system = build_system_prompt(
            state.tools,
            state.long_term,
            concept_memory=state.meta.get("concept_memory", ""),
            skills_catalog=state.meta.get("_skills_catalog", ""),
        )
        messages = build_context_messages(
            state,
            scratchpad=state.meta.get("scratchpad", ""),
            runtime_patches=state.meta.get("runtime_patches"),
        )

        # 在末尾追加"自我汇报"指令；不进 short_term。
        # progress.system 是临时模式说明，跟随主 system 一起送，但只追加一段。
        sys_with_mode = system.rstrip() + "\n\n" + t("progress.system")

        request = {"role": "user", "content": t("progress.request")}

        max_tokens = int(os.environ.get("PROGRESS_LOG_MAX_TOKENS", "1200"))
        out = llm.complete_text(
            messages=messages + [request],
            system=sys_with_mode,
            max_tokens=max_tokens,
        ).strip()

        if not out:
            return None

        # 硬上限：截到 6000 字符
        if len(out) > 6000:
            out = out[:6000] + "\n…[截断]"

        state.meta["_progress_log"] = out
        state.meta["_progress_log_iter"] = getattr(state, "iteration", 0)
        state.meta["_progress_log_method"] = "llm_self"
        _persist_progress_log(state, out)
        return out
    except Exception:
        return None


def ensure_progress_log(state: AgentState, llm: LLMBackend) -> None:
    """在 advisor 调用前确保进展日志足够新鲜。

    优先级：
      1. 主循环刚做过 LLM 全文压缩 → scratchpad 已是权威摘要 → 直接复用，标
         method="main_compress_reuse"，零额外 LLM 调用。
      2. 上次进展日志距今 < PROGRESS_LOG_REFRESH_INTERVAL 轮 → 跳过。
      3. 否则触发 run_self_progress_summary。

    任何步骤失败都静默返回，不影响后续 advisor 调用。
    """
    iteration = getattr(state, "iteration", 0)
    last_iter = int(state.meta.get("_progress_log_iter") or 0)
    refresh_interval = int(os.environ.get("PROGRESS_LOG_REFRESH_INTERVAL", "10"))

    # 路径 1：复用主循环最近一次 LLM 压缩成果
    last_compress_iter   = int(state.meta.get("_last_compression_iter") or 0)
    last_compress_method = state.meta.get("_last_compression_method") or ""
    if (
        last_compress_method == "llm_full"
        and last_compress_iter > last_iter
    ):
        # 权威全量摘要在 _last_handoff —— 压缩后草稿本只剩一条指向它的指针，
        # 直接读 scratchpad 会把"见文末交接消息"当成进展日志喂给 advisor。
        summary = (state.meta.get("_last_handoff") or "").strip()
        if not summary:
            summary = (state.meta.get("scratchpad") or "").strip()
        if summary:
            state.meta["_progress_log"] = summary
            state.meta["_progress_log_iter"] = last_compress_iter
            state.meta["_progress_log_method"] = "main_compress_reuse"
            _persist_progress_log(state, summary)
            return

    # 路径 2：新鲜度判断 — 不太旧就复用
    if last_iter > 0 and (iteration - last_iter) < refresh_interval:
        return

    # 路径 3：触发自压缩
    run_self_progress_summary(state, llm)


# ── 触发判断 ──────────────────────────────────────────────────────────────────

def should_trigger_advisor(
    state: AgentState,
    interval: Optional[int] = None,
) -> Tuple[bool, str]:
    """检查是否应触发定期/主动请求型 advisor。

    interval 留空 = 从环境变量读（推荐）。显式传入的值同样会被
    ADVISOR_MIN_INTERVAL 夹住——下限是硬的，没有绕过的入口。

    返回 (should_trigger, reason)。
    注意：loop_detected / graph_stall 触发由 loop.py 直接处理，不经过这里。
    """
    # Agent 主动请求（优先级最高）。
    # 开关关掉时仍然把标志位摘掉：留着它会在开关被重新打开的那一刻突然放炮。
    if state.meta.pop("_advisor_requested", False):
        reason = state.meta.pop("_advisor_request_reason", "agent_requested")
        if advisor_trigger_enabled("agent_requested"):
            return True, reason

    if not advisor_trigger_enabled("periodic"):
        return False, ""

    # 定期触发（第 0 轮跳过，那时什么都还没做）
    iteration = getattr(state, "iteration", 0)
    if iteration == 0:
        return False, ""

    eff_interval = advisor_interval() if interval is None else max(ADVISOR_MIN_INTERVAL, int(interval))
    last_advised = state.meta.get("_advisor_last_iter", 0)
    if iteration - last_advised >= eff_interval:
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
            f"{t('marker.advisor_prefix', reason=reason)}\n\n"
            f"{advice}\n\n"
            f"---\n{t('marker.advisor_ref')}"
        ),
    }
    state.short_term.append(msg)
    state.meta["_advisor_last_iter"] = getattr(state, "iteration", 0)

    # 落盘到 short_term.jsonl
    persistence = _get_persistence(state)
    if persistence is not None:
        persistence.append_short_term(msg)
