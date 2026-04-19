"""
LLM 接口层
职责：把 AgentState 转换成 LLM 请求，把 LLM 响应解析成 Action。
与具体 LLM 提供商（OpenAI/Anthropic/本地模型）解耦——只需实现 LLMBackend 接口即可。
"""

import json
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse
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
        self.base_url = base_url
        self._is_official_openai = self._detect_official_openai_endpoint(base_url)
        self._use_response_format = self._is_official_openai
        # vLLM/OpenAI-compatible servers may compute a negative default max_tokens when
        # the prompt is long; set an explicit positive value.
        if max_tokens is None:
            import os
            # Default higher because tool_call JSON (esp. long code strings) is easy to truncate.
            max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
        self.max_tokens = max(1, int(max_tokens))

    @staticmethod
    def _detect_official_openai_endpoint(base_url: Optional[str]) -> bool:
        """Return True for the default client endpoint or api.openai.com-style URLs."""
        if not base_url:
            return True
        try:
            hostname = (urlparse(base_url).hostname or "").lower()
        except Exception:
            return False
        return hostname in {"api.openai.com", "openai.com"}

    def _call_api(self, messages: list[dict], system: str, max_tokens: int, use_json_format: bool) -> str:
        """Internal helper: raw API call with explicit format and token controls."""
        full_messages = [{"role": "system", "content": system}] + messages
        kwargs = {
            "model": self.model,
            "messages": full_messages,
            "temperature": 0.3,
        }
        if self._is_official_openai:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        if use_json_format and self._use_response_format:
            kwargs["response_format"] = {"type": "json_object"}

        if not self._is_official_openai:
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

    def __init__(
        self,
        model: str = "claude-opus-4-6",
        api_key: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ):
        import anthropic
        import os
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        # Extended thinking budget (tokens). Set to 0 to disable.
        # Env: ANTHROPIC_THINKING_BUDGET (default 8000)
        if thinking_budget is None:
            thinking_budget = int(os.environ.get("ANTHROPIC_THINKING_BUDGET", "8000"))
        self.thinking_budget = max(0, thinking_budget)
        # Output token limit. Must exceed thinking_budget when thinking is enabled.
        # Env: ANTHROPIC_MAX_TOKENS or LLM_MAX_TOKENS (default 16000)
        if max_tokens is None:
            max_tokens = int(os.environ.get(
                "ANTHROPIC_MAX_TOKENS",
                os.environ.get("LLM_MAX_TOKENS", "16000"),
            ))
        if self.thinking_budget > 0:
            # Anthropic requires max_tokens > budget_tokens
            max_tokens = max(max_tokens, self.thinking_budget + 2048)
        self.max_tokens = max_tokens

    @staticmethod
    def _extract_text(content) -> str:
        """Return the first text block from a Messages response content list."""
        for block in content:
            if getattr(block, "type", None) == "text":
                return block.text
        # Fallback for unexpected shapes
        first = content[0] if content else None
        return getattr(first, "text", str(first)) if first is not None else ""

    def complete(self, messages: list[dict], system: str) -> str:
        """Main agent loop call: full max_tokens, extended thinking enabled."""
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
        )
        if self.thinking_budget > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
        resp = self.client.messages.create(**kwargs)
        return self._extract_text(resp.content)

    def complete_text(self, messages: list[dict], system: str, max_tokens: int = 200) -> str:
        """Lightweight plain-text call for summarisation / note extraction.

        No extended thinking — keeps latency and cost low.
        """
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        return self._extract_text(resp.content)


# ── System Prompt 构建器 ───────────────────────────────────────────────────────

def build_system_prompt(
    tools: dict[str, ToolSpec],
    long_term: list[str],
    scratchpad: str = "",
    concept_memory: str = "",
    runtime_patches: Optional[list[str]] = None,
) -> str:
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

    concept_section = ""
    if concept_memory and concept_memory.strip():
        concept_section = (
            "\n\n## 宏观工作记忆\n"
            + concept_memory.strip()
        )

    memory_section = ""
    if long_term:
        memory_section = "\n\n## 细粒度记忆（近期任务经验）\n" + "\n".join(
            f"- {m}" for m in long_term
        )

    patches_section = ""
    if runtime_patches:
        patches_section = (
            "\n\n## 运行时格式规范（自动生成，必须严格遵守）\n"
            + "\n".join(f"- {p}" for p in runtime_patches)
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
{concept_section}
{memory_section}
{patches_section}
{scratchpad_section}

## 完成任务前的必要步骤（重要！）

在调用 action='done' 之前，你必须完成以下两个步骤：

1. **提交完成报告**：调用 submit_completion_report 工具，提供详细的完成报告，包括：
   - goal_understanding: 你对任务目标的理解
   - completed_work: 已完成的工作列表
   - remaining_gaps: 未完成的工作列表（如果有）
   - evidence_type: 证据类型（artifact/tool_result/observation/none）
   - evidence: 证据列表（根据 evidence_type 提供）
   - outcome: 完成状态（done/done_partial/done_blocked）
   - confidence: 完成信心（low/medium/high）

2. **记录情景记忆**：调用 append_episodic 工具，记录本次执行的关键信息，包括：
   - path: 记忆文件路径（默认 ./memory_episodic.jsonl）
   - summary: 一段话概括（100-300 字），包含关键操作、重要发现、最终结果
   - tags: 逗号分隔的关键词，便于日后检索

**重要提示**：仅仅在 final_answer 中声称"已提交完成报告并记录情景记忆"是无效的。你必须真正调用相应的工具，否则验收会失败，任务会继续循环直到你正确提交。
    
**强烈建议**：在每次任务结束时，按以下顺序操作：
    1. 先调用 submit_completion_report 提交完成报告
    2. 再调用 append_episodic 记录情景记忆
    3. 最后才调用 action='done' 结束任务
    
**记住**：系统会严格检查这两个步骤，缺一不可！

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

def _strip_thinking_tags(text: str) -> str:
    """Remove inline thinking blocks emitted by reasoning models.

    Handles:
    - DeepSeek R1 / Qwen QwQ style: ``<think>...</think>``
    - Variant spelling: ``<thinking>...</thinking>``
    - Unclosed tags (model output cut off mid-think): strip from tag to end-of-block
      or to the first ``{`` that looks like the real JSON payload.

    Anthropic extended-thinking blocks are already stripped at the API layer
    (``_extract_text`` keeps only ``type=="text"`` content blocks), so they
    never reach this function.
    """
    # Remove fully closed blocks (DOTALL so thinking can span multiple lines).
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove unclosed opening tag and everything up to the first '{' of the JSON payload.
    # Pattern: <think> ... { → keep from '{' onward.
    text = re.sub(r"<think(?:ing)?>[^{]*(?=\{)", "", text, flags=re.DOTALL | re.IGNORECASE)
    # If there's still a dangling <think> with no following '{', drop it entirely.
    text = re.sub(r"<think(?:ing)?>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text


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
    # Pre-process: strip thinking-model inline reasoning blocks before any JSON extraction.
    # This handles DeepSeek R1 / Qwen QwQ <think>...</think> style output.
    text = _strip_thinking_tags(text)

    dec = json.JSONDecoder()
    stripped = text.strip()

    # 1) Direct parse.
    try:
        return json.loads(stripped), None
    except json.JSONDecodeError as e:
        # 如果解析失败且错误信息包含 newline，尝试转义换行符
        error_msg = str(e).lower()
        if 'newline' in error_msg:
            # 尝试将字符串中的换行符转义为\n
            # 这是一个启发式修复，用于处理 LLM 输出的未转义换行符
            try:
                # 将字符串中的换行符替换为\n
                escaped_text = stripped.replace('\n', '\\n')
                return json.loads(escaped_text), None
            except json.JSONDecodeError:
                pass
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

    # 3) Generic brace scan — tries each ``{`` until one yields a complete agent response.
    #
    # IMPORTANT: only return immediately when the parsed object looks like an agent
    # response (has "thought" or "action").  If we return an inner dict that happens
    # to be valid JSON (e.g. the ``args`` sub-object), the caller will mis-classify
    # the output as "prose without thought/action" and never retry with json_repair.
    parse_error: Exception = json.JSONDecodeError("No JSON object found", stripped, 0)
    _brace_fallback = None  # best non-agent dict found, used only as last resort
    search_from = 0
    while True:
        idx = stripped.find("{", search_from)
        if idx == -1:
            break
        try:
            obj, _ = dec.raw_decode(stripped[idx:])
            if isinstance(obj, dict) and ("thought" in obj or "action" in obj):
                return obj, None
            if _brace_fallback is None:
                _brace_fallback = obj  # save but keep scanning
        except Exception as e:
            parse_error = e
        search_from = idx + 1

    # 4) json_repair — handles malformed JSON (e.g. missing opening quote on a value).
    #    Placed BEFORE returning the brace-scan fallback so that a mis-parsed inner
    #    sub-object does not shadow a repairable outer response object.
    try:
        from json_repair import repair_json  # type: ignore
        repaired = repair_json(stripped, return_objects=True)
        if isinstance(repaired, dict):
            return repaired, None
    except Exception:
        pass

    # 5) Last resort: return whatever the brace scan found, even if not an agent dict.
    if _brace_fallback is not None:
        return _brace_fallback, None

    return None, parse_error


def parse_response(raw: str) -> Action:
    """Parse the LLM raw response into an Action.

    Delegates JSON extraction to :func:`_extract_json` so the parsing strategy
    is defined in exactly one place.
    """
    data, exc = _extract_json(raw)
    if data is None:
        if "{" not in raw:
            # Pure text output — model forgot the JSON protocol entirely.
            # Treat as a implicit done: the text itself is the final answer.
            stripped_raw = raw.strip()
            if stripped_raw:
                return Action(
                    type=ActionType.DONE,
                    thought="(auto-wrapped plain text as final answer)",
                    final_answer=stripped_raw,
                )
            thought = (
                "你的上一条输出是纯文本，没有任何 JSON 结构。\n"
                "无论任务是否完成，都必须通过 JSON 格式输出，不能直接输出纯文本。\n"
                "如果任务已完成，请使用：\n"
                '{"thought": "...", "action": "done", "final_answer": "..."}\n'
                "如果需要继续调用工具，请使用：\n"
                '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
            )
            return Action(type=ActionType.ERROR, thought=thought, error_type="prose_no_json")
        else:
            # JSON-like content found but failed to parse — diagnose the root cause.
            exc_str = str(exc)
            # Detect literal (unescaped) newlines inside a JSON string value.
            _has_bare_newline = bool(re.search(r'"[^"]*\n[^"]*"', raw))
            # Detect split-structure: thought closed early, remaining fields dangle outside.
            _has_split_structure = bool(re.search(r'"\s*\}\s*,\s*"action"', raw))
            # Detect single-quoted keys: {'key': ...}
            _has_single_quote_key = bool(re.search(r"\{\s*'[^']+'", raw))
            # Detect unescaped Windows backslash paths, e.g. runs\20260413 or C:\Users.
            # Valid JSON escape chars after '\': " \ / b f n r t u
            # Anything else (digits, uppercase letters, etc.) is illegal.
            _has_unescaped_backslash = bool(re.search(r'\\[^"\\/bfnrtu]', raw))
            # Detect pure prose that happens to contain incidental '{' (code/URL/dict snippets).
            # Heuristic: no '"action"' or '"thought"' key found anywhere in the raw text.
            _looks_like_prose = (
                '"action"' not in raw and '"thought"' not in raw
                and "'action'" not in raw and "'thought'" not in raw
            )
            # Detect unquoted string value: e.g. "thought": 用户要求... (missing opening ")
            # Matches a known agent key followed by a colon and a non-JSON-value-start character.
            _has_unquoted_string_value = bool(re.search(
                r'"(?:thought|action|tool|final_answer|args)"\s*:\s*[^\s",\[\{0-9\-ntf\r\n\\]',
                raw,
            ))

            if _looks_like_prose:
                # The '{' is incidental (e.g. inside a code snippet or URL) — treat as pure text.
                thought = (
                    "你的上一条输出是纯文本（其中虽含有 '{' 字符，但没有合法的 JSON 结构）。\n"
                    "无论任务是否完成，都必须通过 JSON 格式输出，不能直接输出纯文本。\n"
                    "如果任务已完成，请使用：\n"
                    '{"thought": "...", "action": "done", "final_answer": "..."}\n'
                    "如果需要继续调用工具，请使用：\n"
                    '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
                )
                _error_type = "prose_with_json"
            elif _has_unescaped_backslash and not _has_bare_newline:
                thought = (
                    "JSON 格式错误：字符串内包含未转义的反斜杠。\n"
                    "原因：Windows 路径（如 C:\\Users\\foo 或 runs\\20260413）中的 \\ 在 JSON 字符串里"
                    "必须写成 \\\\，否则解析器会把 \\U、\\2 等当成非法的转义序列。\n"
                    "错误修复示例：\n"
                    '  错误: {"thought": "路径是 C:\\Users\\92680"}\n'
                    '  正确: {"thought": "路径是 C:\\\\Users\\\\92680"}\n'
                    "提示：在 thought / final_answer 中引用路径时，可以改用正斜杠（/）来避免此问题，"
                    "例如 runs/20260413-140101 或 C:/Users/92680。\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "unescaped_backslash"
            elif "Invalid control character" in exc_str or _has_bare_newline:
                thought = (
                    "JSON 格式错误：字符串内包含未转义的换行符。\n"
                    "原因：thought / final_answer / args 等字段的值中，多行文本必须把换行写成 \\n，"
                    "不能直接按回车换行。\n"
                    "错误修复示例：\n"
                    '  错误: {"thought": "第一行\n第二行"}\n'
                    '  正确: {"thought": "第一行\\n第二行"}\n'
                    "特别提示：如果 args.command 或 args.content 中包含超长内容（如 base64 编码、代码脚本），\n"
                    "不要在字符串中间折行——建议先用 write_file 工具将内容写入临时文件，\n"
                    "再在命令中引用该文件路径（如 python3 /tmp/script.py），可彻底避免此类问题。\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "bare_newline"
            elif "Unterminated string" in exc_str:
                thought = (
                    "JSON 格式错误：字符串未闭合，输出很可能被截断。\n"
                    "原因：final_answer 或 args 中的内容过长，超出了单次输出上限，导致 JSON 在中途被切断。\n"
                    "解决方法：① 大幅缩短 final_answer / args 的内容；"
                    "② 将长内容先用工具写入文件，final_answer 只写摘要和文件路径。\n"
                    f"截断位置: {exc_str}\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "unterminated_string"
            elif _has_split_structure:
                thought = (
                    "JSON 结构错误：thought 字段提前闭合，导致 action/tool/args 等字段脱落在顶层对象之外。\n"
                    "原因：输出中出现了 {...}, \"action\": ... 的结构，"
                    "即 thought 自己构成了一个独立的 {} 对象，后续字段无法被解析。\n"
                    "所有字段必须在同一个顶层 {} 内，正确格式：\n"
                    '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}\n'
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "split_structure"
            elif _has_single_quote_key or "Expecting property name enclosed in double quotes" in exc_str:
                thought = (
                    "JSON 格式错误：key 必须用双引号，不能用单引号。\n"
                    '  错误: {\'thought\': "..."}\n'
                    '  正确: {"thought": "..."}\n'
                    "另一种可能：JSON 字符串中包含未转义的换行符，导致解析器在错误位置尝试读取 key。\n"
                    "请同时检查所有字符串值内的换行是否都转义成了 \\n。\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "single_quote_key"
            elif _has_unquoted_string_value:
                thought = (
                    "JSON 格式错误：字符串值缺少开头的双引号。\n"
                    '原因：某字段的值直接写了内容，而没有先写开头的 "。\n'
                    "错误示例：\n"
                    '  错误: {"thought": 用户要求做一个游戏, "action": "tool_call"}\n'
                    '  正确: {"thought": "用户要求做一个游戏", "action": "tool_call"}\n'
                    "请确保每个字符串值都用双引号包裹，包括 thought、final_answer 等所有字段。\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "unquoted_string_value"
            else:
                thought = (
                    f"JSON 解析失败: {exc_str}\n"
                    f"原始输出(截断): {raw[:300]}"
                )
                _error_type = "unknown"
        return Action(type=ActionType.ERROR, thought=thought, error_type=_error_type)

    # Defensive: some servers/models may emit `null` or a non-object JSON.
    if not isinstance(data, dict):
        pass  # handled below
    # Heuristic: if the parsed dict has neither 'thought' nor 'action', it was likely
    # spuriously extracted from incidental JSON inside plain prose (e.g. a code snippet),
    # OR the real JSON was mis-parsed due to unescaped backslashes stripping key fields.
    elif "thought" not in data and "action" not in data:
        # Handle LLM wrapping the agent response in a {"role":..., "content":"..."} envelope.
        # The outer JSON is valid, but the real agent response is nested inside "content".
        if "content" in data and isinstance(data["content"], str):
            inner_data, _ = _extract_json(data["content"])
            if (
                isinstance(inner_data, dict)
                and ("thought" in inner_data or "action" in inner_data)
            ):
                return parse_response(data["content"])
        _has_unescaped_backslash = bool(re.search(r'\\[^"\\/bfnrtu\n]', raw))
        _has_unquoted_string_value2 = bool(re.search(
            r'"(?:thought|action|tool|final_answer|args)"\s*:\s*[^\s",\[\{0-9\-ntf\r\n\\]',
            raw,
        ))
        if _has_unescaped_backslash:
            _prose_thought = (
                "JSON 格式错误：字符串内包含未转义的反斜杠。\n"
                "原因：Windows 路径（如 C:\\Users\\foo 或 runs\\20260413）中的 \\ 在 JSON 字符串里"
                "必须写成 \\\\，否则解析器会把 \\U、\\2 等当成非法的转义序列并丢失字段。\n"
                "错误修复示例：\n"
                '  错误: {"thought": "路径是 C:\\Users\\92680"}\n'
                '  正确: {"thought": "路径是 C:\\\\Users\\\\92680"}\n'
                "提示：在 thought / final_answer 中引用路径时，可以改用正斜杠（/）来避免此问题，"
                "例如 runs/20260413-140101 或 C:/Users/92680。\n"
                f"原始输出(截断): {raw[:300]}"
            )
            _prose_error_type = "unescaped_backslash"
        elif _has_unquoted_string_value2:
            _prose_thought = (
                "JSON 格式错误：字符串值缺少开头的双引号。\n"
                '原因：某字段的值直接写了内容，而没有先写开头的 "。\n'
                "错误示例：\n"
                '  错误: {"thought": 用户要求做一个游戏, "action": "tool_call"}\n'
                '  正确: {"thought": "用户要求做一个游戏", "action": "tool_call"}\n'
                "请确保每个字符串值都用双引号包裹，包括 thought、final_answer 等所有字段。\n"
                f"原始输出(截断): {raw[:300]}"
            )
            _prose_error_type = "unquoted_string_value"
        else:
            _prose_thought = (
                "你的上一条输出是纯文本（其中虽包含 JSON 片段，但不包含 thought / action 字段）。\n"
                "无论任务是否完成，都必须通过 JSON 格式输出，不能直接输出纯文本。\n"
                "如果任务已完成，请使用：\n"
                '{"thought": "...", "action": "done", "final_answer": "..."}\n'
                "如果需要继续调用工具，请使用：\n"
                '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
            )
            _prose_error_type = "prose_with_json"
        return Action(
            type=ActionType.ERROR,
            thought=_prose_thought,
            error_type=_prose_error_type,
        )
    if not isinstance(data, dict):
        return Action(
            type=ActionType.ERROR,
            thought=f"JSON 顶层必须是 object，但得到: {type(data).__name__}={data!r}. 原始输出: {raw[:300]}",
            error_type="unknown",
        )

    thought = data.get("thought", "")
    action_str = data.get("action", "tool_call")

    if action_str == "done":
        return Action(
            type=ActionType.DONE,
            thought=thought,
            final_answer=data.get("final_answer", ""),
        )

    # 检测 LLM 把工具名写成了 action 值（如 action="shell"）
    if action_str not in ("tool_call",):
        existing_tool = data.get("tool", "")
        guessed_tool = existing_tool or action_str
        # If the tool field is already present (or can be inferred from action), silently fix
        # action → "tool_call" rather than burning a retry round.
        if guessed_tool:
            data = dict(data)
            data["action"] = "tool_call"
            if not existing_tool:
                data["tool"] = guessed_tool
            if "args" not in data:
                data["args"] = {k: v for k, v in data.items()
                                if k not in ("thought", "action", "tool", "args")}
            action_str = "tool_call"
        else:
            return Action(
                type=ActionType.ERROR,
                thought=(
                    f"action='{action_str}' 不合法，action 只能是 'tool_call' 或 'done'。\n"
                    f"如需调用工具，请严格使用以下格式：\n"
                    f'{{"thought":"...","action":"tool_call","tool":"工具名","args":{{...}}}}\n'
                    f"例如调用 ask_user：\n"
                    f'{{"thought":"...","action":"tool_call","tool":"ask_user","args":{{"question":"你的问题"}}}}'
                ),
                error_type="unknown",
            )

    tool = data.get("tool", "")
    args = data.get("args", {})

    if not tool:
        # Case A: tool field was present in raw but lost during parse (split-structure).
        if re.search(r'"tool"\s*:\s*"[^"]+"', raw):
            hint = (
                "注意：原始输出中包含 \"tool\" 字段，但解析后丢失了——"
                "这通常是因为 thought 提前闭合（即 thought 自己构成了独立的 {}，"
                "导致 tool/args 等字段脱落在外）。\n"
                "请将所有字段写在同一个顶层 {} 内：\n"
                '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
            )
        # Case B: JSON only contains 'thought', and the raw output has prose text after the
        # JSON block — model tried to ask the user by writing the question as plain text.
        elif '"action"' not in raw and re.search(r'[？?]', raw):
            hint = (
                "检测到你在 JSON 外面用纯文本向用户提问。\n"
                "正确做法：使用 ask_user 工具，将问题放在 args.question 里：\n"
                '{"thought": "...", "action": "tool_call", "tool": "ask_user", '
                '"args": {"question": "你的问题"}}'
            )
        else:
            hint = (
                '正确格式：{"action":"tool_call","tool":"工具名","args":{...}}'
            )
        return Action(
            type=ActionType.ERROR,
            thought=(
                f"action=tool_call 但解析结果中缺少 tool 字段。\n"
                f"{hint}\n"
                f"thought: {thought}"
            ),
            error_type="unknown",
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
