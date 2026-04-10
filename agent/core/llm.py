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

    def complete_text(self, messages: list[dict], system: str, max_tokens: int = 200) -> str:
        """Plain-text lightweight call. Default falls back to complete(); subclasses may override."""
        return self.complete(messages, system)


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
            max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
        self.max_tokens = max(1, int(max_tokens))

    def _call_api(self, messages: list[dict], system: str, max_tokens: int, use_json_format: bool) -> str:
        """Internal helper: raw API call with explicit format and token controls."""
        full_messages = [{"role": "system", "content": system}] + messages
        kwargs = {
            "model": self.model,
            "messages": full_messages,
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
        if use_json_format and self._use_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        elif not use_json_format and self._use_response_format:
            kwargs["response_format"] = {"type": "text"}

        if not self._use_response_format:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if content is None:
            content = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)

    def complete(self, messages: list[dict], system: str) -> str:
        """Main agent loop call: JSON-formatted, full max_tokens."""
        return self._call_api(messages, system, max_tokens=self.max_tokens, use_json_format=True)

    def complete_text(self, messages: list[dict], system: str, max_tokens: int = 200) -> str:
        """Lightweight plain-text call for summarisation / note extraction.

        Bypasses JSON response_format so the model can output free-form text.
        Uses a small max_tokens cap to keep latency low.
        """
        return self._call_api(messages, system, max_tokens=max_tokens, use_json_format=False)


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

    def complete_text(self, messages: list[dict], system: str, max_tokens: int = 200) -> str:
        """Lightweight plain-text call for summarisation / note extraction."""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
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
6. 如果已有进化工具出现定义/契约错误，优先使用 `validate_tool_recipe`、`repair_tool_candidate`、`promote_tool_candidate` 修复旧工具；不要仅仅换名字继续注册同义新工具

## 草稿本（scratchpad）使用规则（强制）
- 草稿本用于“执行过程中的中间记录与分析”，是你在多步任务中的工作台。
- 当任务需要多步执行时：
  1) 在开始执行前，先用 scratchpad_set 写出一个简短计划/分解（3-8 条即可）。
  2) 每次工具调用得到关键新信息后，用 scratchpad_append 追加“关键发现/结论/下一步”。
- 在准备结束(action=done)之前，必须在草稿本追加一个 **ACCEPTANCE** 区块（验收自评）：
  - criteria: 本次任务的验收标准
  - evidence_type: `artifact` | `tool_result` | `observation` | `none`
  - evidence: 证据。只有当 `evidence_type=artifact` 时才填写真实文件路径；其他类型写简短文字说明即可
  - verdict: PASS/FAIL
- 默认优先根据任务选择合适的 `evidence_type`：只有真正生成了文件产物时才使用 `artifact`
- 草稿本必须：简短、结构化、可随时重写；禁止粘贴大段原文（原文应写入 artifacts 文件并在草稿本引用路径）。
- 长度限制：<= 2000 字符（系统会截断）。
"""


# ── 响应解析器 ────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> tuple:
    """Extract the first JSON *object* from *text* using three strategies.

    Returns ``(dict, None)`` on success or ``(None, exception)`` on failure.

    Strategy order matters:
      1. Direct parse of the stripped text — cheapest path.
      2. Locate an explicit ``\\`\\`\\`json`` marker and raw_decode from there.
         This step is intentionally placed BEFORE the generic brace-scan (step 3)
         so that prose containing stray ``{…}`` fragments before the fence does
         not shadow the real JSON payload.  raw_decode is used instead of a
         regex-to-closing-fence so nested fences inside string values don't
         truncate the match.
      3. Scan successive ``{`` positions via raw_decode — handles plain fences,
         prose prefixes, and other wrapping patterns.

    Fence extraction (step 2) is attempted only after direct parsing fails so
    that code fences embedded inside JSON string values (e.g. a ```python block
    inside ``final_answer``) do not fool a fence regex into extracting non-JSON
    content as the payload.
    """
    dec = json.JSONDecoder()
    stripped = text.strip()

    # 1) Direct parse.
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError:
        pass

    # 2) Explicit ```json marker → raw_decode.
    m = re.search(r"```json\s*", text, re.IGNORECASE)
    if m:
        after = text[m.end():]
        brace = after.find("{")
        if brace != -1:
            try:
                obj, _ = dec.raw_decode(after[brace:])
                return obj, None
            except Exception:
                pass

    # 3) Generic brace scan — tries each ``{`` until one yields a complete object.
    parse_error: Exception = json.JSONDecodeError("No JSON object found", stripped, 0)
    search_from = 0
    while True:
        idx = stripped.find("{", search_from)
        if idx == -1:
            break
        try:
            obj, _ = dec.raw_decode(stripped[idx:])
            return obj, None
        except Exception as e:
            parse_error = e
            search_from = idx + 1

    return None, parse_error


def parse_response(raw: str) -> Action:
    """Parse the LLM raw response into an Action.

    Delegates JSON extraction to :func:`_extract_json` so the parsing strategy
    is defined in exactly one place.
    """
    data, exc = _extract_json(raw)
    if data is None:
        # Keep error message compact; the loop will feed it back.
        return Action(
            type=ActionType.ERROR,
            thought=(
                f"JSON 解析失败: {exc}\n"
                f"原始输出(截断): {raw[:300]}"
            ),
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

    # 检测 LLM 把工具名写成了 action 值（如 action="ask_user"）
    if action_str not in ("tool_call",):
        # 尝试将 action 值本身作为 tool 名自动修复
        guessed_tool = action_str
        guessed_args = {k: v for k, v in data.items()
                        if k not in ("thought", "action", "tool", "args")}
        if not guessed_args:
            guessed_args = data.get("args", {})
        return Action(
            type=ActionType.ERROR,
            thought=(
                f"action='{action_str}' 不合法，action 只能是 'tool_call' 或 'done'。\n"
                f"如需调用工具，请严格使用以下格式：\n"
                f'{{"thought":"...","action":"tool_call","tool":"{guessed_tool}","args":{{...}}}}\n'
                f"例如调用 ask_user：\n"
                f'{{"thought":"...","action":"tool_call","tool":"ask_user","args":{{"question":"你的问题"}}}}'
            )
        )

    tool = data.get("tool", "")
    args = data.get("args", {})

    if not tool:
        return Action(
            type=ActionType.ERROR,
            thought=(
                f"action=tool_call 但未指定 tool 字段。\n"
                f"正确格式：{{\"action\":\"tool_call\",\"tool\":\"工具名\",\"args\":{{...}}}}\n"
                f"thought: {thought}"
            )
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
