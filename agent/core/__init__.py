"""Core runtime for the minimal self-evolving agent.

This subpackage exposes the low-level building blocks so you can assemble your
own loop/LLM/tools without using the high-level Agent wrapper.
"""

from .types_def import Action, ActionType, AgentState, ToolSpec, ToolResult
from .llm import LLMBackend, OpenAIBackend, AnthropicBackend
from .loop import run, console_hooks, AgentHooks
