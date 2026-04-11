"""
通用极简自进化智能体
=====================

快速开始:

    from agent import Agent

    agent = Agent(backend="openai", api_key="sk-...")
    agent.run("帮我分析 /tmp/data.csv 并生成报告")

或手动组装:

    from agent.core import run, console_hooks, AnthropicBackend
    from agent.tools import get_standard_tools

    llm = AnthropicBackend(api_key="...")
    tools = get_standard_tools()
    state = run("你的目标", llm, tools, hooks=console_hooks())
"""

import os

from .core.types import AgentState, ToolSpec, ToolResult
from .core.loop import run, console_hooks, AgentHooks
from .core.llm import OpenAIBackend, AnthropicBackend
from .tools.standard import get_standard_tools


class Agent:
    """
    高层封装，提供最简洁的使用界面。
    底层所有模块都可以单独导入使用。
    """

    def __init__(
        self,
        backend: str = "openai",
        model: str = None,
        api_key: str = None,
        extra_tools: dict = None,
        long_term: list = None,
        max_iterations: int = 30,
        verbose: bool = True,
        concept_memory: str = "",
        initial_meta: dict = None,
    ):
        if backend == "openai":
            # Allow OpenAI-compatible local servers (vLLM etc.) via env var.
            # Example: OPENAI_BASE_URL=http://172.24.168.225:8389/v1
            base_url = os.environ.get("OPENAI_BASE_URL")
            self.llm = OpenAIBackend(
                model=model or os.environ.get("OPENAI_MODEL") or "gpt-4o",
                api_key=api_key,
                base_url=base_url,
                # Default higher because tool_call JSON (esp. long code strings) is easy to truncate.
                max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "8192")),
            )
        elif backend == "anthropic":
            self.llm = AnthropicBackend(
                model=model or "claude-opus-4-6",
                api_key=api_key,
            )
        else:
            raise ValueError(f"未知后端: {backend}。支持: openai, anthropic")

        self.tools = get_standard_tools()
        if extra_tools:
            self.tools.update(extra_tools)

        self.long_term = list(long_term or [])
        self.max_iterations = max_iterations
        self.hooks = console_hooks() if verbose else AgentHooks()
        self.concept_memory = concept_memory or ""
        self.initial_meta = dict(initial_meta or {})

    def run(self, goal: str, state: AgentState = None) -> AgentState:
        """运行智能体直到目标完成或超过最大迭代次数。

        If `state` is provided, the agent will resume from that state (used for
        pause/continue workflows).
        """
        state = run(
            goal=goal,
            llm=self.llm,
            tools=self.tools,
            long_term=self.long_term,
            max_iterations=self.max_iterations,
            hooks=self.hooks,
            state=state,
            concept_memory=self.concept_memory,
            initial_meta=self.initial_meta,
        )
        # 把这次运行的长期记忆持久化回 Agent 实例（跨次运行积累经验）
        self.long_term = state.long_term
        # 工具集也持久化（进化后的工具在下次运行时仍可用）
        self.tools = state.tools
        # 同步概念记忆（LLM 可能调用 save_concept 更新了它）
        self.concept_memory = state.meta.get("concept_memory", self.concept_memory)
        return state

    def add_tool(self, spec: ToolSpec):
        """手动添加自定义工具。"""
        self.tools[spec.name] = spec

    def remember(self, content: str):
        """手动注入长期记忆（预置领域知识）。"""
        self.long_term.append(content)
