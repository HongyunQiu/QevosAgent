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

from .types_def import Action, ActionType, AgentState, ToolSpec
from ..i18n import t


# ── JSON 错误反馈（内联，仅 parse_response 使用）─────────────────────────────

def generate_error_feedback(raw: str, exc: Exception):
    """根据错误特征生成详细的 JSON 格式错误反馈。返回 (thought, error_type)。"""
    has_bare_newline          = bool(re.search(r'"[^"]*\n[^"]*"', raw))
    has_split_structure       = bool(re.search(r'"\s*\}\s*,\s*"action"', raw))
    has_single_quote_key      = bool(re.search(r"\{\s*'[^']+'", raw))
    has_unescaped_backslash   = bool(re.search(r'\\[^"\\/bfnrtu]', raw))
    looks_like_prose          = ('"action"' not in raw and '"thought"' not in raw
                                 and "'action'" not in raw and "'thought'" not in raw)
    has_unquoted_string_value = bool(re.search(
        r'"(?:thought|action|tool|final_answer|args)"\s*:\s*[^\s",\[\{0-9\-ntf\r\n\\]',
        raw,
    ))

    raw200 = raw[:200] + "..." if len(raw) > 200 else raw
    raw300 = raw[:300]

    if looks_like_prose:
        return t("err.prose", raw=raw200), "prose_with_json"
    elif has_unescaped_backslash and not has_bare_newline:
        return t("err.backslash", raw=raw300), "invalid_escape"
    elif has_bare_newline:
        return t("err.newline", raw=raw300), "unescaped_newline"
    elif has_single_quote_key:
        return t("err.single_quote", raw=raw300), "single_quote_key"
    elif has_unquoted_string_value:
        return t("err.unquoted_value", raw=raw300), "unquoted_string_value"
    elif has_split_structure:
        return t("err.split_structure", raw=raw300), "split_structure"
    else:
        return t("err.generic", exc=exc, raw=raw300), "json_parse_error"


def _estimate_tokens_heuristic(texts: Iterable[str]) -> int:
    """Very rough token estimator.

    - For mostly-ASCII text: ~4 chars/token
    - For CJK-heavy text: ~2 chars/token

    This is only for guarding against context overflow.
    """
    total = 0
    for text_chunk in texts:
        if not text_chunk:
            continue
        s = str(text_chunk)
        # If lots of non-ascii (likely CJK), assume denser tokenization.
        non_ascii = sum(1 for ch in s if ord(ch) > 127)
        ratio = non_ascii / max(1, len(s))
        if ratio > 0.3:
            total += int(len(s) / 2) + 1
        else:
            total += int(len(s) / 4) + 1
    return total


def _extract_content_texts(content) -> list[str]:
    """Extract text strings from message content (str or multimodal list)."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    # Rough estimate: images ~1000 tokens each
                    texts.append(" " * 4000)
        return texts
    return []


def image_block(data: str, media_type: str = "image/jpeg") -> dict:
    """Build a canonical base64 image block for multimodal messages.

    Usage:
        msg = {"role": "user", "content": [
            {"type": "text", "text": "What's in this image?"},
            image_block(base64_str, "image/png"),
        ]}
    """
    return {"type": "image", "media_type": media_type, "data": data}


def image_url_block(url: str) -> dict:
    """Build a canonical URL image block for multimodal messages."""
    return {"type": "image", "url": url}


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
        texts = [system]
        for m in messages:
            texts.extend(_extract_content_texts(m.get("content", "")))
        return _estimate_tokens_heuristic(texts)

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
            texts = [system]
            for m in messages:
                texts.extend(_extract_content_texts(m.get("content", "")))
            return sum(len(enc.encode(str(p))) for p in texts if p)
        except Exception:
            texts = [system]
            for m in messages:
                texts.extend(_extract_content_texts(m.get("content", "")))
            return _estimate_tokens_heuristic(texts)

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        """OpenAI-compatible backend.

        Works with:
        - OpenAI official API (default)
        - Local OpenAI-compatible servers (e.g. vLLM) via base_url

        thinking_budget: token budget for extended thinking (0 = disabled).
          Env: OPENAI_THINKING_BUDGET (default 0).
          Can be changed at runtime: llm.thinking_budget = N

        temperature: sampling temperature. Defaults to 0.3 via env LLM_TEMPERATURE.
          Pass None or set LLM_TEMPERATURE=none to omit the parameter entirely
          (required by some providers such as reasoning models that reject it).
          Auto-detected: if the provider returns 400 "Unsupported parameter: temperature",
          the parameter is dropped and all subsequent calls omit it automatically.
        """
        import openai
        import os
        # openai>=1.x uses `base_url` for OpenAI-compatible endpoints.
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.base_url = base_url
        self._is_official_openai = self._detect_official_openai_endpoint(base_url)
        self._use_response_format = self._is_official_openai
        # vLLM/OpenAI-compatible servers may compute a negative default max_tokens when
        # the prompt is long; set an explicit positive value.
        if max_tokens is None:
            # Default higher because tool_call JSON (esp. long code strings) is easy to truncate.
            max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
        self.max_tokens = max(1, int(max_tokens))
        # Extended thinking budget (tokens). Set to 0 to disable.
        # Env: OPENAI_THINKING_BUDGET (default 0)
        if thinking_budget is None:
            thinking_budget = int(os.environ.get("OPENAI_THINKING_BUDGET", "0"))
        self.thinking_budget = max(0, int(thinking_budget))
        # Sampling temperature. None = omit from request (for providers that reject it).
        # Env: LLM_TEMPERATURE (float or "none")
        if temperature is None:
            _env_temp = os.environ.get("LLM_TEMPERATURE", "0.3")
            self.temperature: Optional[float] = (
                None if _env_temp.lower() == "none"
                else float(_env_temp)
            )
        else:
            self.temperature = temperature
        # Context window: probe server first, fall back to env var / hardcoded default.
        self.context_window = self._probe_context_window()

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

    def _probe_context_window(self) -> int:
        """Fetch max_model_len from the server's /v1/models endpoint.

        Priority (highest to lowest):
          1. Server-reported max_model_len  — always preferred when reachable.
          2. LLM_CONTEXT_WINDOW env var     — fallback when probe fails.
          3. Hardcoded 131072               — last resort.

        The env var is intentionally NOT used when the server probe succeeds,
        so that a stale or conservative env var never caps a larger real limit.
        Makes a direct HTTP call (no SDK abstraction) to avoid vendor-specific
        fields being silently dropped by Pydantic parsing.
        """
        import os, json as _json, urllib.request

        if self._is_official_openai:
            # OpenAI's API does not expose max_model_len; rely on env var / default.
            return int(os.environ.get("LLM_CONTEXT_WINDOW", "131072"))

        try:
            url = (self.base_url or "").rstrip("/") + "/models"
            api_key = getattr(self.client, "api_key", None) or "local"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = _json.loads(resp.read().decode())
            for item in (payload.get("data") or []):
                if item.get("id") == self.model:
                    val = item.get("max_model_len")
                    if val and int(val) > 0:
                        # Server value wins — do not let env var override it.
                        return int(val)
        except Exception:
            pass

        # Probe failed: fall back to env var, then hardcoded default.
        return int(os.environ.get("LLM_CONTEXT_WINDOW", "131072"))

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """Convert canonical and Ollama-native image formats to OpenAI content-block format.

        Canonical:      {"type": "image", "media_type": "image/jpeg", "data": "<b64>"}
                        {"type": "image", "url": "https://..."}
        Ollama-native:  message-level "images": ["<b64-or-path>", ...]  (converted here so
                        callers using Ollama SDK-style dicts still work via /v1 endpoint)
        OpenAI output:  {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
        """
        result = []
        for msg in messages:
            # ── Ollama-native: images field at message level ──────────────────
            # e.g. {'role': 'user', 'content': 'text', 'images': ['<b64>']}
            ollama_images = msg.get("images")
            if ollama_images and isinstance(ollama_images, list):
                text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
                blocks: list[dict] = []
                if text:
                    blocks.append({"type": "text", "text": text})
                for img in ollama_images:
                    if isinstance(img, str):
                        # Treat as base64 data (Ollama REST encodes images as base64 strings)
                        blocks.append({"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{img}"
                        }})
                cleaned = {k: v for k, v in msg.items() if k not in ("content", "images")}
                cleaned["content"] = blocks
                result.append(cleaned)
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue

            # ── Canonical image blocks inside content list ────────────────────
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    if "url" in block:
                        new_blocks.append({"type": "image_url", "image_url": {"url": block["url"]}})
                    elif "data" in block:
                        mime = block.get("media_type", "image/jpeg")
                        new_blocks.append({"type": "image_url", "image_url": {
                            "url": f"data:{mime};base64,{block['data']}"
                        }})
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            result.append({**msg, "content": new_blocks})
        return result

    def _call_api(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
        use_json_format: bool,
        thinking_budget: Optional[int] = None,
    ) -> str:
        """Internal helper: raw API call with explicit format and token controls.

        thinking_budget: override self.thinking_budget for this call (None = use instance default).
        """
        budget = self.thinking_budget if thinking_budget is None else thinking_budget
        normalized = self._normalize_messages(messages)
        full_messages = [{"role": "system", "content": system}] + normalized
        kwargs: dict = {
            "model": self.model,
            "messages": full_messages,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self._is_official_openai:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        if use_json_format and self._use_response_format:
            kwargs["response_format"] = {"type": "json_object"}

        if not self._is_official_openai:
            enable = budget > 0
            extra: dict = {"chat_template_kwargs": {"enable_thinking": enable}}
            if enable:
                extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["extra_body"] = extra

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # Auto-detect providers that reject the temperature parameter and retry
            # without it. The flag is stored on the instance so future calls skip it.
            if self.temperature is not None and "temperature" in str(e).lower() and (
                "unsupported parameter" in str(e).lower() or "400" in str(e)
            ):
                self.temperature = None
                kwargs.pop("temperature", None)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if content is None:
            content = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if isinstance(content, str):
            return content
        # Fallback: if content is still None/empty, try streaming mode.
        # Some OpenAI-compatible APIs (e.g. certain proxy providers) do not return
        # content in non-streaming responses but work correctly with stream=True.
        if content is None:
            try:
                stream_kwargs = dict(kwargs)
                stream_kwargs["stream"] = True
                stream_resp = self.client.chat.completions.create(**stream_kwargs)
                parts = []
                for chunk in stream_resp:
                    delta = getattr(chunk.choices[0], "delta", None)
                    if delta is not None:
                        c = getattr(delta, "content", None)
                        if c is not None:
                            parts.append(c)
                content = "".join(parts)
                if content:
                    return content
            except Exception:
                pass  # streaming fallback failed, continue to last-resort below
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)

    def complete(self, messages: list[dict], system: str) -> str:
        """Main agent loop call: JSON-formatted, full max_tokens, thinking per instance default."""
        return self._call_api(
            messages, system,
            max_tokens=self.max_tokens,
            use_json_format=True,
            thinking_budget=self.thinking_budget,
        )

    def complete_text(self, messages: list[dict], system: str, max_tokens: int = 200) -> str:
        """Lightweight plain-text call for summarisation / note extraction.

        Bypasses JSON response_format so the model can output free-form text.
        Disables thinking to keep latency low.
        """
        return self._call_api(
            messages, system,
            max_tokens=max_tokens,
            use_json_format=False,
            thinking_budget=0,
        )


# ── Anthropic 后端实现 ─────────────────────────────────────────────────────────

class AnthropicBackend(LLMBackend):
    def estimate_tokens(self, messages: list[dict], system: str) -> int:
        # Anthropic token counting is model-specific; keep heuristic.
        texts = [system]
        for m in messages:
            texts.extend(_extract_content_texts(m.get("content", "")))
        return _estimate_tokens_heuristic(texts)

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
        # Can be changed at runtime: llm.thinking_budget = N
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

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """Convert canonical image blocks to Anthropic content format.

        Canonical:   {"type": "image", "media_type": "image/jpeg", "data": "<b64>"}
                     {"type": "image", "url": "https://..."}
        Anthropic:   {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
                     {"type": "image", "source": {"type": "url", "url": "..."}}
        """
        result = []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    if "url" in block:
                        new_blocks.append({"type": "image", "source": {
                            "type": "url", "url": block["url"]
                        }})
                    elif "data" in block:
                        new_blocks.append({"type": "image", "source": {
                            "type": "base64",
                            "media_type": block.get("media_type", "image/jpeg"),
                            "data": block["data"],
                        }})
                    else:
                        new_blocks.append(block)
                else:
                    new_blocks.append(block)
            result.append({**msg, "content": new_blocks})
        return result

    def complete(self, messages: list[dict], system: str) -> str:
        """Main agent loop call: full max_tokens, extended thinking per instance default."""
        budget = self.thinking_budget
        # When thinking is enabled, max_tokens must remain > budget_tokens.
        max_tokens = max(self.max_tokens, budget + 2048) if budget > 0 else self.max_tokens
        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=self._normalize_messages(messages),
        )
        if budget > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
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
            messages=self._normalize_messages(messages),
        )
        return self._extract_text(resp.content)


# ── System Prompt 构建器 ───────────────────────────────────────────────────────

def build_system_prompt(
    tools: dict[str, ToolSpec],
    long_term: list[str],
    concept_memory: str = "",
    scratchpad_note_mode: Optional[str] = None,
) -> str:
    """
    动态构建 system prompt（仅含静态/准静态内容）。
    scratchpad 和 runtime_patches 已移至每轮 context 末尾注入，以保持 system prompt
    前缀稳定，最大化 KV Cache 命中率。工具集变化（进化后）时 prompt 仍会自动更新。
    """
    tool_docs = []
    for name, spec in tools.items():
        args_desc = "\n".join(
            f"    - {k}: {v}" for k, v in spec.args_schema.items()
        )
        tag = t("sys.evolved_tag") if spec.is_evolve_tool else ""
        tool_docs.append(
            f"• {name}{tag}: {spec.description}\n{t('sys.params_label')}\n{args_desc}"
        )

    tools_section = "\n".join(tool_docs) if tool_docs else t("sys.tools_none")

    concept_section = ""
    if concept_memory and concept_memory.strip():
        concept_section = f"\n\n{t('sys.concept_header')}\n{concept_memory.strip()}"

    memory_section = ""
    if long_term:
        memory_section = f"\n\n{t('sys.memory_header')}\n" + "\n".join(
            f"- {m}" for m in long_term
        )

    import os as _os
    _note_mode = scratchpad_note_mode or _os.environ.get("SCRATCHPAD_NOTE_MODE", "mini_call")
    _inline_note_field = (
        f"\n{t('sys.note_field')}"
        if _note_mode == "inline" else ""
    )

    return f"""{t('sys.preamble')}

{t('sys.format_header')}
{{
  "thought": "{t('sys.thought_hint')}",{_inline_note_field}
  "action": "tool_call" | "done",
  "tool": "{t('sys.tool_hint')}",
  "args": {{...}},
  "final_answer": "{t('sys.answer_hint')}"
}}

{t('sys.tools_header')}
{tools_section}
{concept_section}
{memory_section}

{t('sys.completion_header')}

{t('sys.completion_body')}

{t('sys.behavior_header')}
{t('sys.behavior_body')}

{t('sys.sp_rules_header')}
{t('sys.sp_rules_body')}
"""


def _build_context_suffix(
    scratchpad: str = "",
    runtime_patches: Optional[list[str]] = None,
) -> str:
    """构建每轮注入到最后一条 user 消息末尾的动态内容（scratchpad + runtime_patches）。"""
    parts: list[str] = []
    if runtime_patches:
        parts.append(
            t("sys.patches_header") + "\n"
            + "\n".join(f"- {p}" for p in runtime_patches)
        )
    if scratchpad and scratchpad.strip():
        parts.append(
            t("sys.sp_header") + "\n"
            + t("sys.sp_rules") + "\n\n"
            + scratchpad.strip()
        )
    return "\n\n".join(parts)


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
    if not isinstance(raw, str):
        raw = str(raw) if raw is not None else ""
    data, exc = _extract_json(raw)
    if data is None:
        if "{" not in raw:
            stripped_raw = raw.strip()
            if stripped_raw:
                # JSON primitives (null / true / false / numbers) are valid JSON but not
                # valid agent responses.  json.loads("null") returns None (same as "no data"),
                # so we must detect them here before falling into the prose auto-wrap path.
                _is_json_primitive = re.match(
                    r'^(null|true|false|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)$',
                    stripped_raw, re.IGNORECASE,
                )
                if _is_json_primitive:
                    return Action(
                        type=ActionType.ERROR,
                        thought=t("parse.prose_no_json"),
                        error_type="null_response",
                    )
                # Pure text output — model forgot the JSON protocol entirely.
                # Treat as an implicit done: the text itself is the final answer.
                return Action(
                    type=ActionType.DONE,
                    thought="(auto-wrapped plain text as final answer)",
                    final_answer=stripped_raw,
                )
            return Action(type=ActionType.ERROR, thought=t("parse.prose_no_json"), error_type="prose_no_json")
        else:
            # JSON-like content found but failed to parse — use enhanced error feedback.
            thought, _error_type = generate_error_feedback(raw, exc)
            return Action(type=ActionType.ERROR, thought=thought, error_type=_error_type)

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
        # Detect unescaped double quote inside a string value: "thought": "text"more text
        # Both "thought" and "action" appear in raw, meaning the model did output a proper
        # JSON structure — but the extraction returned a sub-object (e.g. args dict) because
        # an embedded unescaped " broke the outer JSON.
        _has_unescaped_string_quote = (
            '"thought"' in raw and '"action"' in raw
            and not _has_unescaped_backslash
            and not _has_unquoted_string_value2
        )
        if _has_unescaped_backslash:
            _prose_thought = t("parse.backslash_error", raw=raw[:300])
            _prose_error_type = "unescaped_backslash"
        elif _has_unquoted_string_value2:
            _prose_thought = t("parse.unquoted_error", raw=raw[:300])
            _prose_error_type = "unquoted_string_value"
        elif _has_unescaped_string_quote:
            _prose_thought = t("parse.string_quote_error", raw=raw[:300])
            _prose_error_type = "unescaped_string_quote"
        else:
            _prose_thought = t("parse.prose_with_json")
            _prose_error_type = "prose_with_json"
        return Action(
            type=ActionType.ERROR,
            thought=_prose_thought,
            error_type=_prose_error_type,
        )
    if not isinstance(data, dict):
        return Action(
            type=ActionType.ERROR,
            thought=t("parse.not_object", typename=type(data).__name__, val=repr(data), raw=raw[:300]),
            error_type="unknown",
        )

    thought = data.get("thought", "")
    action_str = data.get("action", "tool_call")
    _sp_note = (data.get("scratchpad_note") or "").strip() or None

    if action_str == "done":
        return Action(
            type=ActionType.DONE,
            thought=thought,
            final_answer=data.get("final_answer", ""),
            scratchpad_note=_sp_note,
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
                thought=t("parse.invalid_action", action=action_str),
                error_type="unknown",
            )

    tool = data.get("tool", "")
    args = data.get("args", {})

    if not tool:
        # Case A: tool field was present in raw but lost during parse (split-structure).
        if re.search(r'"tool"\s*:\s*"[^"]+"', raw):
            hint = t("parse.missing_tool_split")
        # Case B: JSON only contains 'thought', and the raw output has prose text after the
        # JSON block — model tried to ask the user by writing the question as plain text.
        elif '"action"' not in raw and re.search(r'[？?]', raw):
            hint = t("parse.missing_tool_question")
        else:
            hint = t("parse.missing_tool_default")
        return Action(
            type=ActionType.ERROR,
            thought=t("parse.missing_tool_msg", hint=hint, thought=thought),
            error_type="unknown",
        )

    return Action(
        type=ActionType.TOOL_CALL,
        thought=thought,
        tool=tool,
        args=args if isinstance(args, dict) else {},
        scratchpad_note=_sp_note,
    )


# ── 上下文构建器 ──────────────────────────────────────────────────────────────

def build_context_messages(
    state: AgentState,
    scratchpad: str = "",
    runtime_patches: Optional[list[str]] = None,
) -> list[dict]:
    """
    把 AgentState.short_term 转换成 LLM 的 messages 列表。
    scratchpad 和 runtime_patches 动态拼接到最后一条 user 消息末尾，
    避免写入 system prompt 导致 KV Cache 每轮失效。
    """
    msgs = [dict(m) for m in state.short_term]
    suffix = _build_context_suffix(scratchpad, runtime_patches)
    if not suffix:
        return msgs
    # 找最后一条 user 消息追加；若不存在则新增一条
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            content = msgs[i].get("content", "")
            if isinstance(content, list):
                msgs[i] = dict(msgs[i])
                msgs[i]["content"] = list(content) + [{"type": "text", "text": "\n\n---\n\n" + suffix}]
            else:
                msgs[i] = dict(msgs[i])
                msgs[i]["content"] = content + "\n\n---\n\n" + suffix
            return msgs
    msgs.append({"role": "user", "content": suffix})
    return msgs
