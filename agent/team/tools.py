"""
团队协作工具集（统一，无角色标签）
====================================
所有工具对任意节点均可用，行为由当前拓扑节点码决定，而非预设角色。

拓扑管理：
  set_node(node_code)               设置本节点的拓扑位置
  assign_node(target_url, node_code) 向任意节点分配拓扑节点码

通信（按 URL，任意方向）：
  get_agent_status(agent_url)        查询任意节点状态
  get_agent_snapshot(agent_url)      查询任意节点完整快照
  send_to_agent(agent_url, message)  向任意节点注入消息
  delegate_task(agent_url, task, context) 向任意节点分配子任务

上游感知（依赖本节点的拓扑节点码）：
  report_to_upstream(message)        向上游节点汇报（自动读取上游 URL）

下游感知（依赖下游节点通过 /agent/question 提交的问题）：
  get_pending_questions()             获取来自下游的待回答问题
  answer_downstream(agent_url, question_id, answer) 回答某下游问题
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
        raise RuntimeError(f"GET {url} 失败: {e}") from e


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
        raise RuntimeError(f"POST {url} 失败: {e}") from e


def _get_team_api(state: "AgentState"):
    api = state.meta.get("_team_api")
    if api is None:
        raise RuntimeError("Team API 未启动")
    return api


# ── 拓扑管理工具 ──────────────────────────────────────────────────────────────

def tool_set_node(state: "AgentState", node_code: str) -> ToolResult:
    """设置本节点的拓扑节点码，进入组网模式；传入 'null' 退出组网模式。"""
    try:
        api = _get_team_api(state)
        api.set_topology_node(node_code)
        return ToolResult(success=True, output={
            "ok": True,
            "topology_node": api.topology_node,
        })
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_assign_node(
    state: "AgentState",
    target_url: str,
    node_code: str,
) -> ToolResult:
    """向任意节点分配拓扑节点码（通常由顶层节点调用）。"""
    try:
        result = _http_post(
            f"{target_url.rstrip('/')}/agent/set_node",
            {"node_code": node_code},
        )
        return ToolResult(success=True, output=result)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 通信工具（任意方向，按 URL）──────────────────────────────────────────────

def tool_get_agent_status(state: "AgentState", agent_url: str) -> ToolResult:
    """查询指定节点的当前状态（轻量，无推理消耗）。"""
    try:
        return ToolResult(success=True, output=_http_get(
            f"{agent_url.rstrip('/')}/agent/status"
        ))
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_get_agent_snapshot(state: "AgentState", agent_url: str) -> ToolResult:
    """获取指定节点的完整快照：状态 + scratchpad + 最近 5 条行动记录。"""
    try:
        return ToolResult(success=True, output=_http_get(
            f"{agent_url.rstrip('/')}/agent/snapshot"
        ))
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_send_to_agent(
    state: "AgentState",
    agent_url: str,
    message: str,
) -> ToolResult:
    """向指定节点注入一条消息（对方 LLM 将在下次迭代中感知）。"""
    try:
        _http_post(f"{agent_url.rstrip('/')}/agent/inject", {"message": message})
        return ToolResult(success=True, output={"ok": True})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_delegate_task(
    state: "AgentState",
    agent_url: str,
    task: str,
    context: str = "",
) -> ToolResult:
    """向指定节点分配一个子任务，附带可选背景说明。"""
    try:
        _http_post(
            f"{agent_url.rstrip('/')}/agent/task",
            {"task": task, "context": context},
        )
        return ToolResult(success=True, output={
            "ok": True,
            "agent_url": agent_url,
            "task_preview": task[:100],
        })
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 上游感知工具（自动读取拓扑节点码中的上游 URL）────────────────────────────

def tool_report_to_upstream(state: "AgentState", message: str) -> ToolResult:
    """向上游节点发送汇报消息（自动从拓扑节点码获取上游地址）。"""
    try:
        api = _get_team_api(state)
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))

    node = api.topology_node
    if not node or not node.get("upstream_url"):
        return ToolResult(
            success=False, output=None,
            error="当前无上游节点（独立模式或顶层节点），无法汇报",
        )
    try:
        _http_post(
            f"{node['upstream_url']}/agent/inject",
            {"message": f"[节点 {node['id']} 汇报] {message}"},
        )
        return ToolResult(success=True, output={"ok": True})
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 下游感知工具（处理来自下游节点的问题）────────────────────────────────────

def tool_get_pending_questions(state: "AgentState") -> ToolResult:
    """获取来自下游节点的待回答问题列表（含 question_id 和来源节点 URL）。"""
    try:
        api = _get_team_api(state)
        questions = api.get_questions()
        return ToolResult(success=True, output={
            "questions": questions,
            "count": len(questions),
        })
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


def tool_answer_downstream(
    state: "AgentState",
    agent_url: str,
    question_id: str,
    answer: str,
) -> ToolResult:
    """
    回答来自某下游节点的问题（按 question_id 配对）。
    agent_url 从 get_pending_questions 返回的 from_node_url 字段获取。
    """
    try:
        _http_post(
            f"{agent_url.rstrip('/')}/agent/answer",
            {"question_id": question_id, "answer": answer},
        )
        try:
            api = _get_team_api(state)
            api.remove_question(question_id)
        except Exception:
            pass
        return ToolResult(success=True, output={
            "ok": True, "question_id": question_id,
        })
    except Exception as e:
        return ToolResult(success=False, output=None, error=str(e))


# ── 统一工具集工厂 ────────────────────────────────────────────────────────────

def get_team_tools() -> dict[str, ToolSpec]:
    """返回完整团队协作工具集（所有节点均可使用，行为由拓扑节点码决定）。"""
    specs = [
        ToolSpec(
            name="set_node",
            description=(
                "设置本节点的拓扑节点码，进入组网模式。"
                "格式：'nodeA ^ http://upstream:9100'（有上游）或 'nodeRoot'（顶层）。"
                "传入 'null' 退出组网模式，恢复独立运行。"
                "设置后，ask_user 将自动路由到上游节点；同时通知本地 LLM 上下文。"
            ),
            args_schema={
                "node_code": "拓扑节点码字符串，或 'null' 退出组网",
            },
            fn=tool_set_node,
        ),
        ToolSpec(
            name="assign_node",
            description=(
                "向任意节点分配拓扑节点码（通常由顶层节点在架构阶段调用）。"
                "对方收到后立即进入组网模式，并感知自身的上游关系。"
            ),
            args_schema={
                "target_url": "目标节点的 HTTP 地址（如 http://192.168.1.2:9100）",
                "node_code": "要分配给目标节点的拓扑节点码",
            },
            fn=tool_assign_node,
        ),
        ToolSpec(
            name="get_agent_status",
            description="查询指定节点的当前运行状态（轻量，不消耗对方推理资源）。",
            args_schema={"agent_url": "目标节点的 HTTP 地址"},
            fn=tool_get_agent_status,
        ),
        ToolSpec(
            name="get_agent_snapshot",
            description=(
                "获取指定节点的完整快照：拓扑信息 + meta 状态 + scratchpad + 最近 5 条行动记录。"
                "适合深入了解对方的当前工作内容，无需对方消耗推理。"
            ),
            args_schema={"agent_url": "目标节点的 HTTP 地址"},
            fn=tool_get_agent_snapshot,
        ),
        ToolSpec(
            name="send_to_agent",
            description=(
                "向指定节点注入一条消息（对方 LLM 在下次迭代时感知并处理）。"
                "适合主动询问进度、发送补充指令等场景。"
            ),
            args_schema={
                "agent_url": "目标节点的 HTTP 地址",
                "message": "要发送的消息内容",
            },
            fn=tool_send_to_agent,
        ),
        ToolSpec(
            name="delegate_task",
            description="向指定节点分配一个子任务，附带可选背景说明。",
            args_schema={
                "agent_url": "目标节点的 HTTP 地址",
                "task": "子任务描述",
                "context": "背景信息（可选）",
            },
            fn=tool_delegate_task,
        ),
        ToolSpec(
            name="report_to_upstream",
            description=(
                "向上游节点发送汇报消息（自动从本节点的拓扑节点码获取上游地址）。"
                "适合阶段性进展汇报或任务完成通知。"
            ),
            args_schema={"message": "汇报内容"},
            fn=tool_report_to_upstream,
        ),
        ToolSpec(
            name="get_pending_questions",
            description=(
                "获取来自下游节点的待回答问题列表。"
                "每条包含：question_id、from_node_id、from_node_url、问题内容、时间。"
                "使用 answer_downstream 进行配对回答。"
            ),
            args_schema={},
            fn=tool_get_pending_questions,
        ),
        ToolSpec(
            name="answer_downstream",
            description=(
                "回答来自某下游节点的问题（按 question_id 精确配对）。"
                "agent_url 从 get_pending_questions 的 from_node_url 字段获取。"
                "回答后对方将自动恢复执行。"
            ),
            args_schema={
                "agent_url": "提问节点的 HTTP 地址（from_node_url）",
                "question_id": "问题 ID（从 get_pending_questions 获取）",
                "answer": "回答内容",
            },
            fn=tool_answer_downstream,
        ),
    ]
    return {s.name: s for s in specs}
