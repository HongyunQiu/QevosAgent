"""
LLM 接口层
职责：把 AgentState 转换成 LLM 请求，把 LLM 响应解析成 Action。
与具体 LLM 提供商（OpenAI/Anthropic/本地模型）解耦——只需实现 LLMBackend 接口即可。
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Optional

from .types import Action, ActionType, AgentState, ToolSpec


# ── 抽象后端接口 ──────────────────────────────────────────────────────────────

class LLMBackend(ABC):
    """
    只需实现一个方法：给定消息列表，返回文本响应。
    这使得切换 OpenAI / Anthropic / Ollama 只需替换这一个类。
    """
    @abstractmethod
    def complete(self, messages: list[dict], system: str) -> str:
        ...


# ── OpenAI 后端实现 ────────────────────────────────────────────────────────────

class OpenAIBackend(LLMBackend):
    def __init__(self, model: str = "gpt-4o", api_key: Optional[str] = None):
        import openai
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model

    def complete(self, messages: list[dict], system: str) -> str:
        full_messages = [{"role": "system", "content": system}] + messages
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        return resp.choices[0].message.content


# ── Anthropic 后端实现 ─────────────────────────────────────────────────────────

class AnthropicBackend(LLMBackend):
    def __init__(self, model: str = "claude-opus-4-6", api_key: Optional[str] = None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(self, messages: list[dict], system: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            messages=messages,
        )
        return resp.content[0].text


# ── System Prompt 构建器 ───────────────────────────────────────────────────────

def build_system_prompt(tools: dict[str, ToolSpec], long_term: list[str]) -> str:
    """
    动态构建 system prompt。
    工具集变化（进化后）时，prompt 会自动更新——这是工具进化能生效的关键。
    """
    tool_docs = []
    for name, spec in tools.items():
        args_desc = "\n".join(
            f"    - {k}: {v}" for k, v in spec.args_schema.items()
        )
        tag = " [进化工具]" if spec.is_evolve_tool else ""
        tool_docs.append(
            f"• {name}{tag}: {spec.description}\n  参数:\n{args_desc}"
        )

    tools_section = "\n".join(tool_docs) if tool_docs else "（暂无可用工具）"

    memory_section = ""
    if long_term:
        memory_section = "\n\n## 你的长期记忆（经验积累）\n" + "\n".join(
            f"- {m}" for m in long_term
        )

    return f"""你是一个通用自主智能体。你通过循环调用工具来完成任意目标。

## 输出格式（严格遵守，必须是合法 JSON）
{{
  "thought": "你当前的推理过程，分析情况、决定下一步",
  "action": "tool_call" | "done",
  "tool": "工具名（action=tool_call 时必填）",
  "args": {{...}},
  "final_answer": "最终结论（action=done 时填写，其他时候省略）"
}}

## 可用工具
{tools_section}
{memory_section}

## 行为准则
1. 每次只做一个动作（一次工具调用）
2. 用 thought 展示完整推理，不要跳过
3. 遇到错误，分析原因后换一种方式重试
4. 目标完成后，用 action=done 退出并给出 final_answer
5. 优先利用长期记忆中的经验，避免重复犯错"""


# ── 响应解析器 ────────────────────────────────────────────────────────────────

def parse_response(raw: str) -> Action:
    """
    把 LLM 的原始文本解析成 Action。
    做了防御性处理：模型有时会在 JSON 外面包裹 markdown 代码块。
    """
    # 尝试提取 ```json ... ``` 块
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    text = match.group(1).strip() if match else raw.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return Action(
            type=ActionType.ERROR,
            thought=f"JSON 解析失败: {e}\n原始输出: {raw[:300]}"
        )

    thought = data.get("thought", "")
    action_str = data.get("action", "tool_call")

    if action_str == "done":
        return Action(
            type=ActionType.DONE,
            thought=thought,
            final_answer=data.get("final_answer", ""),
        )

    tool = data.get("tool", "")
    args = data.get("args", {})

    if not tool:
        return Action(
            type=ActionType.ERROR,
            thought=f"action=tool_call 但未指定 tool 字段。thought: {thought}"
        )

    return Action(
        type=ActionType.TOOL_CALL,
        thought=thought,
        tool=tool,
        args=args if isinstance(args, dict) else {},
    )


# ── 上下文构建器 ──────────────────────────────────────────────────────────────

def build_context_messages(state: AgentState) -> list[dict]:
    """
    把 AgentState.short_term 转换成 LLM 的 messages 列表。
    短期记忆直接作为对话历史传入。
    """
    return state.short_term.copy()
