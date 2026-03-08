"""
LLM 接口层
职责：把 AgentState 转换成 LLM 请求，把 LLM 响应解析成 Action。
与具体 LLM 提供商（OpenAI/Anthropic/本地模型）解耦——只需实现 LLMBackend 接口即可。
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Iterable

from .types import Action, ActionType, AgentState, ToolSpec


def _estimate_tokens_heuristic(texts: Iterable[str]) -> int:
    """Very rough token estimator.

    - For mostly-ASCII text: ~4 chars/token
    - For CJK-heavy text: ~2 chars/token

    This is only for guarding against context overflow.
    """
    total = 0
    for t in texts:
        if not t:
            continue
        s = str(t)
        # If lots of non-ascii (likely CJK), assume denser tokenization.
        non_ascii = sum(1 for ch in s if ord(ch) > 127)
        ratio = non_ascii / max(1, len(s))
        if ratio > 0.3:
            total += int(len(s) / 2) + 1
        else:
            total += int(len(s) / 4) + 1
    return total


# ── 抽象后端接口 ──────────────────────────────────────────────────────────────

class LLMBackend(ABC):
    """Backend interface.

    Minimal contract:
    - complete(messages, system) -> str

    Optional:
    - estimate_tokens(messages, system) -> int (best-effort)
    """

    @abstractmethod
    def complete(self, messages: list[dict], system: str) -> str:
        ...

    def estimate_tokens(self, messages: list[dict], system: str) -> int:
        # Default: heuristic; subclasses can override.
        return _estimate_tokens_heuristic([system] + [m.get("content", "") for m in messages])


# ── OpenAI 后端实现 ────────────────────────────────────────────────────────────

class OpenAIBackend(LLMBackend):
    def estimate_tokens(self, messages: list[dict], system: str) -> int:
        # Try tiktoken when available, else fall back to heuristic.
        try:
            import tiktoken  # type: ignore
            # Best-effort: use o200k_base; works reasonably for many models.
            enc = tiktoken.get_encoding("o200k_base")
            parts = [system] + [m.get("content", "") for m in messages]
            return sum(len(enc.encode(str(p))) for p in parts if p)
        except Exception:
            return _estimate_tokens_heuristic([system] + [m.get("content", "") for m in messages])

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ):
        """OpenAI-compatible backend.

        Works with:
        - OpenAI official API (default)
        - Local OpenAI-compatible servers (e.g. vLLM) via base_url
        """
        import openai
        # openai>=1.x uses `base_url` for OpenAI-compatible endpoints.
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self._use_response_format = not bool(base_url)
        # vLLM/OpenAI-compatible servers may compute a negative default max_tokens when
        # the prompt is long; set an explicit positive value.
        if max_tokens is None:
            import os
            # Default higher because tool_call JSON (esp. long code strings) is easy to truncate.
            max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
        self.max_tokens = max(1, int(max_tokens))

    def complete(self, messages: list[dict], system: str) -> str:
        full_messages = [{"role": "system", "content": system}] + messages
        kwargs = {
            "model": self.model,
            "messages": full_messages,
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
        }
        # Some OpenAI-compatible servers (e.g. certain vLLM builds) may not fully
        # support `response_format`. When using a custom base_url, rely on the
        # system prompt's strict-JSON instruction instead.
        if self._use_response_format:
            kwargs["response_format"] = {"type": "json_object"}

        # For vLLM reasoning-enabled builds: disable thinking so answers land in
        # message.content (instead of only in reasoning fields).
        if not self._use_response_format:
            # OpenAI Python SDK doesn't accept arbitrary kwargs; vLLM supports this via extra_body.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if content is None:
            content = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)

        # Some OpenAI-compatible servers / SDK versions may return non-str content.
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)


# ── Anthropic 后端实现 ─────────────────────────────────────────────────────────

class AnthropicBackend(LLMBackend):
    def estimate_tokens(self, messages: list[dict], system: str) -> int:
        # Anthropic token counting is model-specific; keep heuristic.
        return _estimate_tokens_heuristic([system] + [m.get("content", "") for m in messages])

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

def build_system_prompt(tools: dict[str, ToolSpec], long_term: list[str], scratchpad: str = "") -> str:
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

    scratchpad_section = ""
    if scratchpad and scratchpad.strip():
        scratchpad_section = (
            "\n\n## 草稿本（可编辑的工作短期记忆，去噪后的关键信息/计划）\n"
            "- 要求：简短、结构化、可随时重写；不要粘贴原始大段内容（原文应写入 raw_memory 或文件并引用路径）。\n"
            "- 建议长度：<= 2000 字符。\n\n"
            + scratchpad.strip()
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
{scratchpad_section}

## 行为准则
1. 每次只做一个动作（一次工具调用）
2. 用 thought 展示完整推理，不要跳过
3. 遇到错误，分析原因后换一种方式重试
4. 目标完成后，用 action=done 退出并给出 final_answer
5. 优先利用长期记忆中的经验，避免重复犯错

## 草稿本（scratchpad）使用规则（强制）
- 草稿本用于“执行过程中的中间记录与分析”，是你在多步任务中的工作台。
- 当任务需要多步执行时：
  1) 在开始执行前，先用 scratchpad_set 写出一个简短计划/分解（3-8 条即可）。
  2) 每次工具调用得到关键新信息后，用 scratchpad_append 追加“关键发现/结论/下一步”。
- 草稿本必须：简短、结构化、可随时重写；禁止粘贴大段原文（原文应写入 artifacts 文件并在草稿本引用路径）。
- 长度限制：<= 2000 字符（系统会截断）。
"""


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

    # Defensive: some servers/models may emit `null` or a non-object JSON.
    if not isinstance(data, dict):
        return Action(
            type=ActionType.ERROR,
            thought=f"JSON 顶层必须是 object，但得到: {type(data).__name__}={data!r}. 原始输出: {raw[:300]}"
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
