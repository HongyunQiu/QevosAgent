"""
核心类型定义
这里定义了整个智能体系统的数据契约。
所有模块都依赖这里的定义，但这里不依赖任何其他模块。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from enum import Enum


# ── 动作类型 ─────────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    TOOL_CALL = "tool_call"   # 调用一个工具
    DONE      = "done"        # 目标已完成，退出循环
    ERROR     = "error"       # LLM 输出解析失败


# ── LLM 返回的动作 ────────────────────────────────────────────────────────────

@dataclass
class Action:
    type: ActionType
    thought: str                    # LLM 的推理过程（透明化）
    tool: Optional[str] = None      # 工具名
    args: dict = field(default_factory=dict)
    final_answer: Optional[str] = None  # type=DONE 时的最终输出


# ── 工具执行结果 ──────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    success: bool
    output: Any          # 工具的实际输出
    error: Optional[str] = None

    def to_str(self) -> str:
        if self.success:
            return str(self.output)
        return f"[TOOL ERROR] {self.error}"


# ── 工具描述（用于构建 system prompt）────────────────────────────────────────

@dataclass
class ToolSpec:
    name: str
    description: str                        # 告诉 LLM 这个工具干什么
    args_schema: dict                       # 参数说明 {arg_name: "描述"}
    fn: Callable[..., ToolResult]           # 实际执行函数
    is_evolve_tool: bool = False            # 标记是否是"进化类"工具（影响自身）


# ── 智能体状态（完整的运行时上下文）─────────────────────────────────────────

@dataclass
class AgentState:
    goal: str
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    # 短期记忆：本轮所有 (action, result) 的线性历史，直接拼入 LLM 上下文
    short_term: list[dict] = field(default_factory=list)

    # 长期记忆：跨轮次保留的经验/结论，以字符串列表存储（极简方案）
    long_term: list[str] = field(default_factory=list)

    # 迭代计数
    iteration: int = 0

    # 元数据：可存任意键值，供工具和进化机制使用
    meta: dict = field(default_factory=dict)

    # 运行期持久化器（可选），用于把 short_term/meta/final_answer 等实时落盘
    persistence: Any = None
