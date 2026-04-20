"""错误反馈模块 - 为 LLM 提供详细的 JSON 格式错误反馈"""

import re
from typing import Tuple


def generate_error_feedback(raw: str, exc: Exception) -> Tuple[str, str]:
    """生成详细的错误反馈信息。返回 (thought, error_type)。"""
    exc_str = str(exc).lower()
    
    # 检测各种错误类型
    has_bare_newline = bool(re.search(r'"[^"]*\n[^"]*"', raw))
    has_split_structure = bool(re.search(r'"\s*\}\s*,\s*"action"', raw))
    has_single_quote_key = bool(re.search(r"\{\s*'[^']+'", raw))
    has_unescaped_backslash = bool(re.search(r'\\[^"\\/bfnrtu]', raw))
    looks_like_prose = ('"action"' not in raw and '"thought"' not in raw
                        and "'action'" not in raw and "'thought'" not in raw)
    has_unquoted_string_value = bool(re.search(
        r'"(?:thought|action|tool|final_answer|args)"\s*:\s*[^\s",\[\{0-9\-ntf\r\n\\]',
        raw,
    ))
    
    # 根据错误类型生成反馈
    if looks_like_prose:
        return _build_prose_feedback(raw), "prose_with_json"
    elif has_unescaped_backslash and not has_bare_newline:
        return _build_backslash_feedback(raw), "invalid_escape"
    elif has_bare_newline:
        return _build_newline_feedback(raw), "unescaped_newline"
    elif has_single_quote_key:
        return _build_single_quote_feedback(raw), "single_quote_key"
    elif has_unquoted_string_value:
        return _build_unquoted_value_feedback(raw), "unquoted_string_value"
    elif has_split_structure:
        return _build_split_structure_feedback(raw), "split_structure"
    else:
        return _build_generic_feedback(raw, exc), "json_parse_error"


def _build_prose_feedback(raw: str) -> str:
    """构建纯文本错误的反馈信息。"""
    return (
        "【JSON 格式错误】你的上一条输出是纯文本（其中虽含有 '{' 字符，但没有合法的 JSON 结构）。\n"
        "错误类型：prose_with_json - 纯文本误判为 JSON\n"
        "问题描述：输出中包含了 '{' 字符，但没有形成合法的 JSON 对象结构。\n\n"
        "正确格式示例：\n"
        "1. 完成任务时：{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"done\\\", \\\"final_answer\\\": \\\"最终答案...\\\"}\n"
        "2. 调用工具时：{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"tool_call\\\", \\\"tool\\\": \\\"工具名\\\", \\\"args\\\": {...}}\n\n"
        "请严格按照上述 JSON 格式重新输出，确保：\n"
        "- 使用双引号（\\\"）包裹所有键名和字符串值\n"
        "- 所有字符串内的换行符转义为\\\\n\n"
        "- 所有字符串内的反斜杠转义为\\\\\\\\\n"
        "- 不要输出任何 Markdown 代码块标记（```json ... ```）\n\n"
        f"你的原始输出（前 200 字符）：{raw[:200]}..."
    )


def _build_backslash_feedback(raw: str) -> str:
    """构建未转义反斜杠错误的反馈信息。"""
    return (
        "【JSON 格式错误】字符串内包含未转义的反斜杠。\n"
        "错误类型：invalid_escape - 无效的转义字符\n"
        "问题描述：Windows 路径（如 C:\\\\Users\\\foo 或 runs\\\20260413）中的 \\\\ 在 JSON 字符串里\n"
        "            必须写成 \\\\\\，否则解析器会把 \\U、\\2 等当成非法的转义序列。\n"
        "错误修复示例：\n"
        "  错误：{\\\"path\\\": \\\"runs\\\20260413\\\file.txt\\\"}\n"
        "  正确：{\\\"path\\\": \\\"runs\\\\\\\20260413\\\\\\\file.txt\\\"}\n\n"
        "建议：在 thought / final_answer 中引用路径时，可以改用正斜杠（/）来避免此问题，\n"
        "例如 runs/20260413-140101 或 C:/Users/92680。\n"
        f"原始输出 (截断): {raw[:300]}"
    )


def _build_newline_feedback(raw: str) -> str:
    """构建未转义换行符错误的反馈信息。"""
    return (
        "【JSON 格式错误】字符串内包含未转义的换行符。\n"
        "错误类型：unescaped_newline - 未转义的换行符\n"
        "问题描述：JSON 字符串值内不能直接包含换行符，必须转义为\\n。\n"
        "错误修复示例：\n"
        "  错误：{\\\"thought\\\": \\\"这是第一行\n这是第二行\\\"}\n"
        "  正确：{\\\"thought\\\": \\\"这是第一行\\n这是第二行\\\"}\n\n"
        "请检查所有字符串值内的换行是否都转义成了\\n。\n"
        f"原始输出 (截断): {raw[:300]}"
    )


def _build_single_quote_feedback(raw: str) -> str:
    """构建单引号键名错误的反馈信息。"""
    return (
        "【JSON 格式错误】使用了单引号而不是双引号。\n"
        "错误类型：single_quote_key - 单引号键名\n"
        "问题描述：JSON 标准要求使用双引号（\\\"）包裹键名和字符串值，不能使用单引号（'）。\n"
        "错误修复示例：\n"
        "  错误：{'thought': '测试', 'action': 'done'}\n"
        "  正确：{\\\"thought\\\": \\\"测试\\\", \\\"action\\\": \\\"done\\\"}\n\n"
        "请将所有单引号替换为双引号。\n"
        f"原始输出 (截断): {raw[:300]}"
    )


def _build_unquoted_value_feedback(raw: str) -> str:
    """构建未引用字符串值错误的反馈信息。"""
    return (
        "【JSON 格式错误】字符串值缺少双引号。\n"
        "错误类型：unquoted_string_value - 未引用的字符串值\n"
        "问题描述：JSON 要求所有字符串值都必须用双引号包裹。\n"
        "错误修复示例：\n"
        "  错误：{\\\"thought\\\": 用户要求测试，\\\"action\\\": done}\n"
        "  正确：{\\\"thought\\\": \\\"用户要求测试\\\", \\\"action\\\": \\\"done\\\"}\n\n"
        "请检查 thought、action、tool、final_answer 等所有字段的字符串值是否都用双引号包裹。\n"
        f"原始输出 (截断): {raw[:300]}"
    )


def _build_split_structure_feedback(raw: str) -> str:
    """构建分割 JSON 结构错误的反馈信息。"""
    return (
        "【JSON 格式错误】JSON 结构被分割。\n"
        "错误类型：split_structure - 分割的 JSON 结构\n"
        "问题描述：JSON 对象被提前闭合，导致后续字段悬空。\n"
        "错误修复示例：\n"
        "  错误：{\\\"thought\\\": \\\"测试\\\"}, \\\"action\\\": \\\"done\\\"}\n"
        "  正确：{\\\"thought\\\": \\\"测试\\\", \\\"action\\\": \\\"done\\\"}\n\n"
        "请确保所有字段都在同一个 JSON 对象内，不要在中间闭合花括号。\n"
        f"原始输出 (截断): {raw[:300]}"
    )


def _build_generic_feedback(raw: str, exc: Exception) -> str:
    """构建通用错误反馈信息。"""
    return (
        f"【JSON 格式错误】无法解析你的输出。\n"
        f"错误信息：{exc}\n\n"
        "请检查你的输出是否符合以下 JSON 格式：\n"
        "1. 完成任务时：{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"done\\\", \\\"final_answer\\\": \\\"最终答案...\\\"}\n"
        "2. 调用工具时：{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"tool_call\\\", \\\"tool\\\": \\\"工具名\\\", \\\"args\\\": {...}}\n\n"
        "常见错误及修复：\n"
        "- 使用双引号（\\\"）而不是单引号（'）\n"
        "- 字符串内的换行符转义为\\n\n"
        "- 字符串内的反斜杠转义为\\\\\n"
        "- 不要在字符串值中直接包含未转义的特殊字符\n\n"
        f"原始输出 (截断): {raw[:300]}"
    )
