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
    ):
        if backend == "openai":
            self.llm = OpenAIBackend(
                model=model or "gpt-4o",
                api_key=api_key,
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

    def run(self, goal: str) -> AgentState:
        """运行智能体直到目标完成或超过最大迭代次数。"""
        state = run(
            goal=goal,
            llm=self.llm,
            tools=self.tools,
            long_term=self.long_term,
            max_iterations=self.max_iterations,
            hooks=self.hooks,
        )
        # 把这次运行的长期记忆持久化回 Agent 实例（跨次运行积累经验）
        self.long_term = state.long_term
        # 工具集也持久化（进化后的工具在下次运行时仍可用）
        self.tools = state.tools
        return state

    def add_tool(self, spec: ToolSpec):
        """手动添加自定义工具。"""
        self.tools[spec.name] = spec

    def remember(self, content: str):
        """手动注入长期记忆（预置领域知识）。"""
        self.long_term.append(content)
