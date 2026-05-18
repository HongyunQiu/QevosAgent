"""
团队协作工具集
==============
主管角色工具：查询队员状态、分配任务、获取/回答队员问题。
队员角色工具：向主管汇报进度和完成情况。

设计原则：
- ask_user 在队员模式下由 run_goal.py 透明路由到主管，无需队员 LLM 感知。
- 本模块只提供"主动发起"的通信工具；被动响应（等待主管回答）由 api.py 处理。
- 所有 HTTP 调用使用纯 stdlib urllib，无额外依赖。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from agent.core.types_def import ToolResult, ToolSpec

if TYPE_CHECKING:
    from agent.core.types_def import AgentState


# ── 底层 HTTP 工具函数 ────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP GET {url} 失败: {e}") from e


def _http_post(url: str, data: dict, timeout: int = 10) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP POST {url} 失败: {e}") from e


# ── 主管侧工具函数 ────────────────────────────────────────────────────────────

def tool_get_worker_status(state: "AgentState", worker_url: str) -> ToolResult:
    try:
        data = _http_get(f"{worker_url.rstrip('/')}/agent/status")
        return ToolResult(success=True, output=data)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_get_worker_snapshot(state: "AgentState", worker_url: str) -> ToolResult:
    try:
        data = _http_get(f"{worker_url.rstrip('/')}/agent/snapshot")
        return ToolResult(success=True, output=data)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_get_pending_questions(state: "AgentState") -> ToolResult:
    """获取所有队员提交的待回答问题（从本地 team API 缓存读取，无网络调用）。"""
    team_api = state.meta.get("_team_api")
    if team_api is None:
        return ToolResult(success=False, output=None, error="Team API 未启动（非组网模式）")
    questions = team_api.get_questions()
    return ToolResult(
        success=True,
        output={"questions": questions, "count": len(questions)},
    )


def tool_answer_worker(
    state: "AgentState",
    worker_id: str,
    question_id: str,
    answer: str,
) -> ToolResult:
    """向指定队员回答一个问题，通过 question_id 配对确保准确投递。"""
    workers: dict = state.meta.get("_team_workers", {})
    worker_url = workers.get(worker_id)
    if not worker_url:
        return ToolResult(
            success=False, output=None,
            error=f"未知队员 ID: {worker_id}。已知队员: {list(workers.keys())}",
        )
    try:
        _http_post(
            f"{worker_url.rstrip('/')}/agent/answer",
            {"question_id": question_id, "answer": answer},
        )
        # 本地移除已回答的问题
        team_api = state.meta.get("_team_api")
        if team_api is not None:
            team_api.remove_question(question_id)
        return ToolResult(
            success=True,
            output={"ok": True, "worker_id": worker_id, "question_id": question_id},
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_ask_worker(
    state: "AgentState",
    worker_url: str,
    message: str,
) -> ToolResult:
    """向指定队员注入一条消息（主管主动询问进度、补充指令等）。"""
    try:
        _http_post(
            f"{worker_url.rstrip('/')}/agent/inject",
            {"message": message},
        )
        return ToolResult(success=True, output={"ok": True})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_delegate_task(
    state: "AgentState",
    worker_url: str,
    task: str,
    context: str = "",
) -> ToolResult:
    """向指定队员分配一个子任务，附带可选背景说明。"""
    try:
        _http_post(
            f"{worker_url.rstrip('/')}/agent/task",
            {"task": task, "context": context},
        )
        return ToolResult(
            success=True,
            output={"ok": True, "worker_url": worker_url, "task_preview": task[:100]},
        )
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 队员侧工具函数 ────────────────────────────────────────────────────────────

def _get_team_config(state: "AgentState") -> tuple[str, str]:
    """返回 (supervisor_url, worker_id)，若未配置则 supervisor_url 为空字符串。"""
    cfg: dict = state.meta.get("_team_mode") or {}
    return cfg.get("supervisor_url", ""), cfg.get("worker_id", "unknown")


def tool_report_progress(state: "AgentState", summary: str) -> ToolResult:
    """向主管汇报当前工作进度。"""
    supervisor_url, worker_id = _get_team_config(state)
    if not supervisor_url:
        return ToolResult(success=False, output=None, error="未配置主管 URL（非队员模式）")
    try:
        _http_post(
            f"{supervisor_url.rstrip('/')}/agent/inject",
            {"message": f"[队员 {worker_id} 进度汇报] {summary}"},
        )
        return ToolResult(success=True, output={"ok": True})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_report_done(state: "AgentState", result: str) -> ToolResult:
    """向主管汇报子任务已完成，并提供结果摘要。"""
    supervisor_url, worker_id = _get_team_config(state)
    if not supervisor_url:
        return ToolResult(success=False, output=None, error="未配置主管 URL（非队员模式）")
    try:
        _http_post(
            f"{supervisor_url.rstrip('/')}/agent/inject",
            {"message": f"[队员 {worker_id} 任务完成] {result}"},
        )
        return ToolResult(success=True, output={"ok": True})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 工具规格工厂 ──────────────────────────────────────────────────────────────

def get_supervisor_tools() -> dict[str, ToolSpec]:
    """返回主管角色的团队工具字典。"""
    specs = [
        ToolSpec(
            name="get_worker_status",
            description=(
                "查询指定队员 Agent 的当前状态（迭代数、运行状态、是否暂停等）。"
                "轻量读取，不消耗队员的推理资源。"
            ),
            args_schema={"worker_url": "队员 Agent 的 HTTP 地址（如 http://192.168.1.2:9101）"},
            fn=tool_get_worker_status,
        ),
        ToolSpec(
            name="get_worker_snapshot",
            description=(
                "获取队员 Agent 的详细快照：meta 状态 + scratchpad 工作笔记 + 最近 5 条行动记录。"
                "适合深入了解队员当前工作内容，无需队员消耗推理。"
            ),
            args_schema={"worker_url": "队员 Agent 的 HTTP 地址"},
            fn=tool_get_worker_snapshot,
        ),
        ToolSpec(
            name="get_pending_questions",
            description=(
                "获取所有队员提交的待回答问题列表。"
                "每条包含：question_id、worker_id、问题内容、提交时间。"
                "使用 answer_worker 工具进行配对回答。"
            ),
            args_schema={},
            fn=tool_get_pending_questions,
        ),
        ToolSpec(
            name="answer_worker",
            description=(
                "回答某个队员提出的问题。"
                "必须提供 question_id（从 get_pending_questions 获取），确保配对准确。"
                "回答后队员将自动恢复执行。"
            ),
            args_schema={
                "worker_id": "队员 ID（如 worker-A）",
                "question_id": "问题 ID（从 get_pending_questions 获取）",
                "answer": "回答内容",
            },
            fn=tool_answer_worker,
        ),
        ToolSpec(
            name="ask_worker",
            description=(
                "向指定队员注入一条消息（主动询问进度、发送补充指令等）。"
                "队员的 LLM 将在下次迭代时处理此消息。"
            ),
            args_schema={
                "worker_url": "队员 Agent 的 HTTP 地址",
                "message": "要发送给队员的消息",
            },
            fn=tool_ask_worker,
        ),
        ToolSpec(
            name="delegate_task",
            description=(
                "向指定队员分配一个子任务，附带可选背景信息。"
                "适合任务分解后将子任务派发给相应队员执行。"
            ),
            args_schema={
                "worker_url": "队员 Agent 的 HTTP 地址",
                "task": "子任务描述",
                "context": "背景信息（可选）",
            },
            fn=tool_delegate_task,
        ),
    ]
    return {s.name: s for s in specs}


def get_worker_tools() -> dict[str, ToolSpec]:
    """返回队员角色的团队工具字典。"""
    specs = [
        ToolSpec(
            name="report_progress",
            description=(
                "向主管汇报当前工作进度。"
                "建议在完成重要阶段或遇到关键发现时调用。"
            ),
            args_schema={"summary": "进度摘要（已完成的内容和下一步计划）"},
            fn=tool_report_progress,
        ),
        ToolSpec(
            name="report_done",
            description=(
                "向主管汇报子任务已完成，提供结果摘要。"
                "调用后主管将知晓本队员的子任务已结束。"
            ),
            args_schema={"result": "任务完成结果摘要"},
            fn=tool_report_done,
        ),
    ]
    return {s.name: s for s in specs}
