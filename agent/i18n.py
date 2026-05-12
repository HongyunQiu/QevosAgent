"""
i18n for QevosAgent — terminal/UI strings AND LLM-facing protocol strings.

Language is detected from the system locale; set QEVOS_LANG=zh or QEVOS_LANG=en
to override.

String categories:
  loop.*        ConsoleHooks terminal output
  interrupt.*   User interrupt terminal output
  status.*      /status display
  log.*         /log display
  marker.*      Internal protocol markers (produced by Python, parsed by
                persistence.py and server.js — both sides use t() so they
                always agree; server.js consumer uses OR-logic for old logs)
  compress.*    Compression system prompts and bridge messages
  note.*        Auto scratchpad note mini-LLM prompts
  advisor.*     Advisor context and injection strings
  sys.*         Agent system prompt sections (build_system_prompt)
  err.*         JSON error-feedback strings (generate_error_feedback)
  parse.*       Inline error thoughts in parse_response
"""

import locale
import os

# ── Language detection ────────────────────────────────────────────────────────

def _detect_lang() -> str:
    override = os.environ.get("QEVOS_LANG", "")
    if override:
        return "zh" if override.lower().startswith("zh") else "en"
    try:
        sys_locale = locale.getlocale()[0] or ""
    except Exception:
        sys_locale = ""
    return "zh" if sys_locale.lower().startswith("zh") else "en"

LANG: str = _detect_lang()

# ── String tables ─────────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, str]] = {
    "zh": {
        # loop.py — ConsoleHooks
        "loop.iter_header":        "[迭代 {i}/{max_i}]  工具数: {tools}  长期记忆: {lt} 条",
        "loop.thought":            "💭 思考: {t}",
        "loop.tool_call":          "🔧 调用工具: {name}({args})",
        "loop.result":             "结果: {text}",
        "loop.truncated":          "...[截断]",
        "loop.done":               "✨ 完成！",
        "loop.error":              "⚠️  错误: {msg}",
        "loop.note":               "📓 草稿本笔记 [{tool}]: {note}",
        "loop.rebuild":            "🔄 上下文重建  ·  封锁工具: {tool}  ·  重建后消息数: {count}",
        "loop.rebuild_reason":     "   原因: 反复忽略循环警告，已清除污染上下文并注入新起点",
        "loop.patch":              "🩹 运行时补丁 [{label}|{etype}]: {rule}",
        "loop.patch.rule_added":        "新增规则",
        "loop.patch.candidate_recorded":"候选记录",
        "loop.patch.candidate_promoted":"候选晋升",
        "loop.advisor":            "[高级指导员 · {reason}]",

        # ── Internal protocol markers ─────────────────────────────────────────
        "marker.tool_prefix":       "[工具: {name}]",
        "marker.tool_success":      "执行成功",
        "marker.tool_failure":      "执行失败",
        "marker.output":            "输出:",
        "marker.output_truncated":  "输出(可能已截断):",
        "marker.error_label":       "错误:",
        "marker.retry_hint":        "请分析原因，调整策略后重试（可换用其他工具或修改参数）。",
        "marker.spill_saved":       "输出较大（{chars} 字符），已完整保存至：{path}",
        "marker.spill_hint":        "如需读取完整内容，请使用 shell 或 run_python 分段读取该文件。",
        "marker.spill_preview":     "内容预览：",
        "marker.vision_skip":       "图片已跳过：当前模型不支持多模态",
        "marker.json_error":        "JSON 解析失败",
        "marker.system_prefix":     "[系统]",
        "marker.system_cmd":        "[系统指令]",
        "marker.advisor_prefix":    "[高级指导员 · 触发: {reason}]",
        "marker.advisor_ref":       "以上是来自独立视角的战略性审视意见，供参考。请结合当前任务状态判断是否调整策略。",

        # ── compression.py ────────────────────────────────────────────────────
        "compress.bridge_sp": (
            "[系统] 早期对话记录（共 {dropped} 条）已压缩以节省上下文空间。"
            "执行过程的关键发现与进度已归纳在 system prompt 的草稿本中，请以草稿本内容作为早期历史的参考依据。"
            "以下为最近 {keep} 条执行记录。"
        ),
        "compress.bridge_no_sp": (
            "[系统] 早期对话记录（共 {dropped} 条）已压缩以节省上下文空间。"
            "以下为最近 {keep} 条执行记录。"
        ),
        "compress.request": (
            "[系统指令] 请将以上执行历史压缩为结构化摘要。\n"
            "任务目标参考：{goal}"
        ),
        "compress.system": (
            "你是一个智能体执行历史的压缩专家。\n"
            "你将收到一段智能体与工具交互的完整消息历史。\n"
            "请将其压缩为简洁、结构化的执行摘要，作为后续步骤的工作记忆。\n\n"
            "输出格式（直接输出纯文本，不要 JSON，不要标题装饰）：\n"
            "• 已完成：逐条列出已完成的步骤及其关键结果\n"
            "• 关键发现：执行中发现的重要事实、数据或结论\n"
            "• 遇到的问题：障碍及采取的应对方式（若有）\n"
            "• 当前状态：目前进展到哪一步，下一步计划是什么\n\n"
            "压缩原则：\n"
            "- 保留：步骤结果、关键数据、重要决策、有效的解决路径\n"
            "- 丢弃：工具原始输出的冗长内容、重复失败的重试、无结论的中间思考\n"
            "- 总长控制在 500 字以内，语言简洁直接"
        ),

        # ── note (auto_scratchpad_note) ───────────────────────────────────────
        "note.system": (
            "你是一个简洁的信息提取助手。"
            "根据任务目标，从工具结果中提取1-2条最关键的新发现。"
            "要求：每条一行，不超过40字，直接输出文字，不要JSON，不要编号，不要重复草稿中已有的内容。"
        ),
        "note.user": (
            "任务目标: {goal}\n"
            "当前草稿摘要: {sp}\n"
            "工具: {tool}  参数: {args}\n"
            "工具结果:\n{result}"
        ),

        # ── advisor.py ────────────────────────────────────────────────────────
        "advisor.trigger_msg": (
            "触发原因：{reason}\n\n"
            "请审视以下 Agent 的当前状态，给出战略性指导意见。\n\n"
            "---\n{context}\n---"
        ),
        "advisor.ctx.iter":       "## 当前迭代轮次\n第 {iter} 轮",
        "advisor.ctx.goal":       "## 任务目标\n{goal}",
        "advisor.ctx.sp":         "## 草稿本（Agent 当前工作状态）\n{sp}",
        "advisor.ctx.sp_empty":   "## 草稿本\n（草稿本为空）",
        "advisor.ctx.history":    "## 最近执行历史（最后 {n} 条）\n{hist}",
        "advisor.ctx.no_history": "（暂无历史记录）",
        "advisor.ctx.truncated":  "…[截断]",

        # ── sys (build_system_prompt in llm.py) ───────────────────────────────
        "sys.preamble": "你是一个通用自主智能体。你通过循环调用工具来完成任意目标。",
        "sys.format_header":  "## 输出格式（严格遵守，必须是合法 JSON）",
        "sys.thought_hint":   "你当前的推理过程，分析情况、决定下一步",
        "sys.note_field":     '  "scratchpad_note": "（可选）对上一步工具结果的1-2条关键新发现，将自动追加草稿本，每条<=40字",',
        "sys.tool_hint":      "工具名（action=tool_call 时必填）",
        "sys.answer_hint":    "最终结论（action=done 时填写，其他时候省略）",
        "sys.tools_header":   "## 可用工具",
        "sys.tools_none":     "（暂无可用工具）",
        "sys.evolved_tag":    " [进化工具]",
        "sys.params_label":   "  参数:",
        "sys.concept_header": "## 宏观工作记忆",
        "sys.memory_header":  "## 细粒度记忆（近期任务经验）",
        "sys.patches_header": "## 运行时格式规范（自动生成，必须严格遵守）",
        "sys.sp_header":      "## 草稿本（可编辑的工作短期记忆，去噪后的关键信息/计划）",
        "sys.sp_rules":       (
            "- 要求：简短、结构化、可随时重写；不要粘贴原始大段内容（原文应写入 raw_memory 或文件并引用路径）。\n"
            "- 建议长度：<= 2000 字符。"
        ),
        "sys.completion_header": "## 完成任务前的必要步骤（重要！）",
        "sys.completion_body": """\
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

**记住**：系统会严格检查这两个步骤，缺一不可！\
""",
        "sys.behavior_header": "## 行为准则",
        "sys.behavior_body": """\
1. 每次只做一个动作（一次工具调用）
2. 用 thought 展示完整推理，不要跳过
3. 遇到错误，分析原因后换一种方式重试
4. 目标完成后，用 action=done 退出并给出 final_answer
5. 优先利用长期记忆中的经验，避免重复犯错
6. 如果已有进化工具出现定义/契约错误，优先使用 `validate_tool_recipe`、`repair_tool_candidate`、`promote_tool_candidate` 修复旧工具；不要仅仅换名字继续注册同义新工具
7. **WEB 展示交互模式**：调用 `web_show` 后，用户会停留在 WEB 页面，通过页面下方聊天框继续与你交流。此时你必须先调用 `web_notify` 邀请用户互动，再调用 `ask_user` 暂停等待——不得在未收到用户明确"结束"指令前直接走完成流程（submit_completion_report → done）。\
""",
        "sys.sp_rules_header": "## 草稿本（scratchpad）使用规则（强制）",
        "sys.sp_rules_body": """\
- 草稿本用于"执行过程中的中间记录与分析"，是你在多步任务中的工作台。
- 当任务需要多步执行时：
  1) 在开始执行前，先用 scratchpad_set 写出一个简短计划/分解（3-8 条即可）。
  2) 每次工具调用得到关键新信息后，用 scratchpad_append 追加"关键发现/结论/下一步"。
- 在准备结束(action=done)之前，必须在草稿本追加一个 **ACCEPTANCE** 区块（验收自评）：
  - criteria: 本次任务的验收标准
  - evidence_type: `artifact` | `tool_result` | `observation` | `none`
  - evidence: 证据。只有当 `evidence_type=artifact` 时才填写真实文件路径；其他类型写简短文字说明即可
  - verdict: PASS/FAIL
- 默认优先根据任务选择合适的 `evidence_type`：只有真正生成了文件产物时才使用 `artifact`
- 草稿本必须：简短、结构化、可随时重写；禁止粘贴大段原文（原文应写入 artifacts 文件并在草稿本引用路径）。
- 长度限制：<= 2000 字符（系统会截断）。\
""",

        # ── err.* (generate_error_feedback) ───────────────────────────────────
        "err.prose": (
            "【JSON 格式错误】你的上一条输出是纯文本（其中虽含有 '{{' 字符，但没有合法的 JSON 结构）。\n"
            "错误类型：prose_with_json - 纯文本误判为 JSON\n"
            "问题描述：输出中包含了 '{{' 字符，但没有形成合法的 JSON 对象结构。\n\n"
            "正确格式示例：\n"
            "1. 完成任务时：{{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"done\\\", \\\"final_answer\\\": \\\"最终答案...\\\"}}\n"
            "2. 调用工具时：{{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"tool_call\\\", \\\"tool\\\": \\\"工具名\\\", \\\"args\\\": {{...}}}}\n\n"
            "请严格按照上述 JSON 格式重新输出，确保：\n"
            "- 使用双引号（\\\"）包裹所有键名和字符串值\n"
            "- 所有字符串内的换行符转义为\\\\n\n"
            "- 所有字符串内的反斜杠转义为\\\\\\\\\n"
            "- 不要输出任何 Markdown 代码块标记（```json ... ```）\n\n"
            "你的原始输出（前 200 字符）：{raw}"
        ),
        "err.backslash": (
            "【JSON 格式错误】字符串内包含未转义的反斜杠。\n"
            "错误类型：invalid_escape - 无效的转义字符\n"
            "问题描述：Windows 路径（如 C:\\\\Users\\\foo 或 runs\\\20260413）中的 \\\\ 在 JSON 字符串里\n"
            "            必须写成 \\\\\\，否则解析器会把 \\U、\\2 等当成非法的转义序列。\n"
            "错误修复示例：\n"
            "  错误：{{\\\"path\\\": \\\"runs\\\20260413\\\file.txt\\\"}}\n"
            "  正确：{{\\\"path\\\": \\\"runs\\\\\\\20260413\\\\\\\file.txt\\\"}}\n\n"
            "建议：在 thought / final_answer 中引用路径时，可以改用正斜杠（/）来避免此问题，\n"
            "例如 runs/20260413-140101 或 C:/Users/92680。\n"
            "原始输出 (截断): {raw}"
        ),
        "err.newline": (
            "【JSON 格式错误】字符串内包含未转义的换行符。\n"
            "错误类型：unescaped_newline - 未转义的换行符\n"
            "问题描述：JSON 字符串值内不能直接包含换行符，必须转义为\\n。\n"
            "错误修复示例：\n"
            "  错误：{{\\\"thought\\\": \\\"这是第一行\n这是第二行\\\"}}\n"
            "  正确：{{\\\"thought\\\": \\\"这是第一行\\n这是第二行\\\"}}\n\n"
            "请检查所有字符串值内的换行是否都转义成了\\n。\n"
            "原始输出 (截断): {raw}"
        ),
        "err.single_quote": (
            "【JSON 格式错误】使用了单引号而不是双引号。\n"
            "错误类型：single_quote_key - 单引号键名\n"
            "问题描述：JSON 标准要求使用双引号（\\\"）包裹键名和字符串值，不能使用单引号（'）。\n"
            "错误修复示例：\n"
            "  错误：{{'thought': '测试', 'action': 'done'}}\n"
            "  正确：{{\\\"thought\\\": \\\"测试\\\", \\\"action\\\": \\\"done\\\"}}\n\n"
            "请将所有单引号替换为双引号。\n"
            "原始输出 (截断): {raw}"
        ),
        "err.unquoted_value": (
            "【JSON 格式错误】字符串值缺少双引号。\n"
            "错误类型：unquoted_string_value - 未引用的字符串值\n"
            "问题描述：JSON 要求所有字符串值都必须用双引号包裹。\n"
            "错误修复示例：\n"
            "  错误：{{\\\"thought\\\": 用户要求测试，\\\"action\\\": done}}\n"
            "  正确：{{\\\"thought\\\": \\\"用户要求测试\\\", \\\"action\\\": \\\"done\\\"}}\n\n"
            "请检查 thought、action、tool、final_answer 等所有字段的字符串值是否都用双引号包裹。\n"
            "原始输出 (截断): {raw}"
        ),
        "err.split_structure": (
            "【JSON 格式错误】JSON 结构被分割。\n"
            "错误类型：split_structure - 分割的 JSON 结构\n"
            "问题描述：JSON 对象被提前闭合，导致后续字段悬空。\n"
            "错误修复示例：\n"
            "  错误：{{\\\"thought\\\": \\\"测试\\\"}}, \\\"action\\\": \\\"done\\\"}}\n"
            "  正确：{{\\\"thought\\\": \\\"测试\\\", \\\"action\\\": \\\"done\\\"}}\n\n"
            "请确保所有字段都在同一个 JSON 对象内，不要在中间闭合花括号。\n"
            "原始输出 (截断): {raw}"
        ),
        "err.generic": (
            "【JSON 格式错误】无法解析你的输出。\n"
            "错误信息：{exc}\n\n"
            "请检查你的输出是否符合以下 JSON 格式：\n"
            "1. 完成任务时：{{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"done\\\", \\\"final_answer\\\": \\\"最终答案...\\\"}}\n"
            "2. 调用工具时：{{\\\"thought\\\": \\\"思考内容...\\\", \\\"action\\\": \\\"tool_call\\\", \\\"tool\\\": \\\"工具名\\\", \\\"args\\\": {{...}}}}\n\n"
            "常见错误及修复：\n"
            "- 使用双引号（\\\"）而不是单引号（'）\n"
            "- 字符串内的换行符转义为\\n\n"
            "- 字符串内的反斜杠转义为\\\\\n"
            "- 不要在字符串值中直接包含未转义的特殊字符\n\n"
            "原始输出 (截断): {raw}"
        ),

        # ── parse.* (inline error thoughts in parse_response) ─────────────────
        "parse.prose_no_json": (
            "你的上一条输出是纯文本，没有任何 JSON 结构。\n"
            "无论任务是否完成，都必须通过 JSON 格式输出，不能直接输出纯文本。\n"
            "如果任务已完成，请使用：\n"
            '{"thought": "...", "action": "done", "final_answer": "..."}\n'
            "如果需要继续调用工具，请使用：\n"
            '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
        ),
        "parse.backslash_error": (
            "JSON 格式错误：字符串内包含未转义的反斜杠。\n"
            "原因：Windows 路径（如 C:\\Users\\foo 或 runs\\20260413）中的 \\ 在 JSON 字符串里"
            "必须写成 \\\\，否则解析器会把 \\U、\\2 等当成非法的转义序列并丢失字段。\n"
            "错误修复示例：\n"
            '  错误: {"thought": "路径是 C:\\Users\\92680"}\n'
            '  正确: {"thought": "路径是 C:\\\\Users\\\\92680"}\n'
            "提示：在 thought / final_answer 中引用路径时，可以改用正斜杠（/）来避免此问题，"
            "例如 runs/20260413-140101 或 C:/Users/92680。\n"
            "原始输出(截断): {raw}"
        ),
        "parse.unquoted_error": (
            "JSON 格式错误：字符串值缺少开头的双引号。\n"
            '原因：某字段的值直接写了内容，而没有先写开头的 "。\n'
            "错误示例：\n"
            '  错误: {"thought": 用户要求做一个游戏, "action": "tool_call"}\n'
            '  正确: {"thought": "用户要求做一个游戏", "action": "tool_call"}\n'
            "请确保每个字符串值都用双引号包裹，包括 thought、final_answer 等所有字段。\n"
            "原始输出(截断): {raw}"
        ),
        "parse.string_quote_error": (
            "JSON 格式错误：字符串值内含有未转义的双引号。\n"
            '原因：thought / final_answer 等字段的值中，如果内容本身含有 " 引号（如引用文字、英文名称），\n'
            '必须将其写成 \\"，否则 JSON 解析器会误把它当作字符串结束符，导致后续字段全部丢失。\n'
            "错误示例：\n"
            '  错误: {"thought": "描述为"the open-source code"，这是重名"}\n'
            '  正确: {"thought": "描述为\\"the open-source code\\"，这是重名"}\n'
            "原始输出(截断): {raw}"
        ),
        "parse.prose_with_json": (
            "你的上一条输出是纯文本（其中虽包含 JSON 片段，但不包含 thought / action 字段）。\n"
            "无论任务是否完成，都必须通过 JSON 格式输出，不能直接输出纯文本。\n"
            "如果任务已完成，请使用：\n"
            '{"thought": "...", "action": "done", "final_answer": "..."}\n'
            "如果需要继续调用工具，请使用：\n"
            '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
        ),
        "parse.not_object":           "JSON 顶层必须是 object，但得到: {typename}={val}. 原始输出: {raw}",
        "parse.missing_tool_split": (
            "注意：原始输出中包含 \"tool\" 字段，但解析后丢失了——"
            "这通常是因为 thought 提前闭合（即 thought 自己构成了独立的 {}，"
            "导致 tool/args 等字段脱落在外）。\n"
            "请将所有字段写在同一个顶层 {} 内：\n"
            '{"thought": "...", "action": "tool_call", "tool": "工具名", "args": {...}}'
        ),
        "parse.missing_tool_question": (
            "检测到你在 JSON 外面用纯文本向用户提问。\n"
            "正确做法：使用 ask_user 工具，将问题放在 args.question 里：\n"
            '{"thought": "...", "action": "tool_call", "tool": "ask_user", '
            '"args": {"question": "你的问题"}}'
        ),
        "parse.missing_tool_default": '{"action":"tool_call","tool":"工具名","args":{...}}',
        "parse.missing_tool_msg": (
            "action=tool_call 但解析结果中缺少 tool 字段。\n"
            "{hint}\n"
            "thought: {thought}"
        ),
        "parse.invalid_action": (
            "action='{action}' 不合法，action 只能是 'tool_call' 或 'done'。\n"
            "如需调用工具，请严格使用以下格式：\n"
            '{{"thought":"...","action":"tool_call","tool":"工具名","args":{{...}}}}\n'
            "例如调用 ask_user：\n"
            '{{"thought":"...","action":"tool_call","tool":"ask_user","args":{{"question":"你的问题"}}}}'
        ),

        # user_interrupt.py — terminal interaction
        "interrupt.pause_detected":
            "[干预] 检测到 /，Agent 将在当前操作结束后暂停。"
            "请输入命令后回车，或直接回车显示帮助：",
        "interrupt.ack":           "[用户干预] 已收到 {name}，将在当前工具调用结束后生效。",
        "interrupt.webcmd":        "[Web看板] 注入命令: {cmd}",
        "interrupt.stop":
            "[用户干预] /stop 已生效：当前工具将被终止，Agent 继续执行。"
            "（如需退出程序，请输入 /exit）",
        "interrupt.exit":          "[用户干预] /exit：Agent 即将退出。",
        "interrupt.newtask_usage": "[用户干预] 用法: /newtask <新任务目标>",
        "interrupt.newtask_done":  "[用户干预] 新目标已注入：{arg}",
        "interrupt.inject_usage":  "[用户干预] 用法: /inject <消息内容>",
        "interrupt.inject_done":   "[用户干预] 消息已注入，下轮 LLM 可感知。",
        "interrupt.compress":
            "[压缩] 已标记：下次 LLM 调用前将压缩上下文，"
            "保留最近 {keep} 条（当前共 {before} 条）。",
        "interrupt.add_iters":     "[用户干预] 已增加 {n} 次迭代，累计待增加: {total} 次。",
        "interrupt.add_iters_usage":"[用户干预] 用法: /+<正整数>，例如 /+50",
        "interrupt.unknown_cmd":   "[用户干预] 未知命令: {name}。输入 /help 查看可用命令。",

        # user_interrupt.py — /status display
        "status.header":           "[状态]  迭代: {i}  工具数: {tools}  长期记忆: {lt} 条",
        "status.current_tool":     "  当前工具: {tool}  已耗时: {elapsed}",
        "status.idle":             "  当前工具: (空闲中)",
        "status.scratchpad":       "草稿本:",
        "status.truncated":        "\n...[截断]",

        # user_interrupt.py — /log display
        "log.header":              "[执行记录] 最近 {n} / 共 {total} 条",
        "log.tool":                "🔧 [#{i}] 工具: {tool}",
        "log.done":                "✨ [#{i}] 完成",
        "log.thought":             "💭 [#{i}] 思考",
        "log.result_tag":          "📥 结果",

        # user_interrupt.py — HELP_TEXT
        "interrupt.help": """\
[用户干预命令] - 输入 / 即可触发：
  /help              立即显示此帮助（不等当前工具结束）
  /stop              终止当前正在执行的工具，Agent 继续下一步
  /exit              退出整个 Agent 程序
  /inject <消息>     将消息注入 Agent 上下文，下轮 LLM 可感知
  /newtask <目标>    注入新任务目标（nostop 模式专用，解除等待并开始新一轮）
  /compress [N]      下次 LLM 调用前压缩上下文（保留最近 N 条，默认 8）
  /status            显示当前状态：迭代号、正在执行的工具、草稿本
  /log [N]           显示最近 N 条执行记录（默认 5 条）
  /+N                增加 N 次最大迭代次数（例如 /+50）
  （/status 和 /log 在工具执行中也会立即响应）
提示: 只需输入 / 即可暂停，完整命令后按回车生效。
""",

        # ── run_goal.py — LLM-facing goal prefix strings ──────────────────────
        "rg.prefix_preloaded": "工具、细粒度记忆和概念记忆已自动预加载，请直接完成任务。\n\n",
        "rg.agents_md_rule":   "【总规范】你必须遵守仓库根目录的 AGENTS.md（运行规范）。\n",
        "rg.run_dir_with_agents": "本次运行 RUN_DIR={run_dir}；所有临时/中间产物必须写入 {run_dir}/artifacts/。\n\n",
        "rg.run_dir_hint":     "提示：本次运行 RUN_DIR={run_dir}。建议将临时/中间产物写入 {run_dir}/artifacts/。\n\n",
        "rg.skills_header":    "【领域技能】以下是本次任务激活的领域专业规范，请遵守：\n\n",
        "rg.nostop_await":     "任务完成，进入持续对话模式。请输入下一个目标，或 /exit 退出：",
        "rg.scratchpad_init":  "任务描述:\n{goal}\n",
        "rg.next_goal_msg":    "请完成以下目标：\n\n{goal}",

        # ── run_goal.py — terminal strings ────────────────────────────────────
        "rg.hint_header": (
            "[提示] Agent 运行期间可随时输入干预命令（以 / 开头）：\n"
            "  /help   显示所有命令    /stop   停止当前工具\n"
            "  /exit   退出程序        /inject <消息>  注入上下文\n"
            "  /status 查看当前状态   /+N  增加 N 次迭代\n"
        ),
        "rg.hint_nostop": (
            "  /newtask <目标>  注入新目标（nostop 模式专用）\n"
            "  [nostop 模式已启用] 任务完成后将持续等待下一个目标。\n"
        ),
        "rg.intervention_header":  "─── 干预模式 ────────────────────────────────────────",
        "rg.intervention_prompt": (
            "输入 /命令（如 /stop /exit /inject <消息>）\n"
            "或直接输入文字，将自动注入到 Agent 上下文（效果等同 /inject）："
        ),
        "rg.intervention_timeout": "[干预] 未收到输入，恢复执行。",
        "rg.user_confirmed":       "[run_goal] 用户确认完成，退出。",
        "rg.nostop_done":          "[nostop] ✅ 第 {n} 轮任务完成。",
        "rg.nostop_prompt":        "[nostop] 请输入下一个目标（/exit 退出）：",

        # ── marker: user supplementary info ──────────────────────────────────
        "marker.user_info": "[用户补充信息]\n{content}",

        # ── agent/core/executor.py — LLM-facing error messages ────────────────
        "exec.not_found":  "工具 '{name}' 不存在。当前可用工具: {available}",
        "exec.arg_error":  "工具参数错误: {e}{hint}",
        "exec.exec_error": "工具执行异常: {etype}: {e}",
    },

    "en": {
        # loop.py — ConsoleHooks
        "loop.iter_header":        "[Iter {i}/{max_i}]  Tools: {tools}  Long-term: {lt}",
        "loop.thought":            "💭 Thought: {t}",
        "loop.tool_call":          "🔧 Tool call: {name}({args})",
        "loop.result":             "Result: {text}",
        "loop.truncated":          "...[truncated]",
        "loop.done":               "✨ Done!",
        "loop.error":              "⚠️  Error: {msg}",
        "loop.note":               "📓 Scratchpad note [{tool}]: {note}",
        "loop.rebuild":            "🔄 Context rebuild  ·  Blocked: {tool}  ·  Messages after: {count}",
        "loop.rebuild_reason":     "   Reason: Repeated loop warnings ignored; poisoned context cleared and restarted",
        "loop.patch":              "🩹 Runtime patch [{label}|{etype}]: {rule}",
        "loop.patch.rule_added":        "Rule added",
        "loop.patch.candidate_recorded":"Candidate recorded",
        "loop.patch.candidate_promoted":"Candidate promoted",
        "loop.advisor":            "[Advisor · {reason}]",

        # user_interrupt.py — terminal interaction
        "interrupt.pause_detected":
            "[Interrupt] / detected — Agent will pause after the current operation. "
            "Enter a command and press Enter, or press Enter alone for help:",
        "interrupt.ack":           "[Interrupt] {name} received — will take effect after the current tool call.",
        "interrupt.webcmd":        "[Web dashboard] Injecting command: {cmd}",
        "interrupt.stop":
            "[Interrupt] /stop applied: current tool will be terminated, Agent continues. "
            "(Use /exit to quit the program)",
        "interrupt.exit":          "[Interrupt] /exit: Agent is about to quit.",
        "interrupt.newtask_usage": "[Interrupt] Usage: /newtask <new goal>",
        "interrupt.newtask_done":  "[Interrupt] New goal injected: {arg}",
        "interrupt.inject_usage":  "[Interrupt] Usage: /inject <message>",
        "interrupt.inject_done":   "[Interrupt] Message injected — LLM will see it next turn.",
        "interrupt.compress":
            "[Compress] Marked: context will be compressed before the next LLM call, "
            "keeping the latest {keep} (currently {before}).",
        "interrupt.add_iters":     "[Interrupt] Added {n} iterations — queued total: {total}.",
        "interrupt.add_iters_usage":"[Interrupt] Usage: /+<positive int>, e.g. /+50",
        "interrupt.unknown_cmd":   "[Interrupt] Unknown command: {name}. Type /help for available commands.",

        # user_interrupt.py — /status display
        "status.header":           "[Status]  Iter: {i}  Tools: {tools}  Long-term: {lt}",
        "status.current_tool":     "  Current tool: {tool}  Elapsed: {elapsed}",
        "status.idle":             "  Current tool: (idle)",
        "status.scratchpad":       "Scratchpad:",
        "status.truncated":        "\n...[truncated]",

        # user_interrupt.py — /log display
        "log.header":              "[Log] Last {n} / {total} entries",
        "log.tool":                "🔧 [#{i}] Tool: {tool}",
        "log.done":                "✨ [#{i}] Done",
        "log.thought":             "💭 [#{i}] Thought",
        "log.result_tag":          "📥 Result",

        # user_interrupt.py — HELP_TEXT
        "interrupt.help": """\
[User Commands] - type / to trigger:
  /help              Show this help immediately (without waiting for the current tool)
  /stop              Terminate the current tool; Agent continues to the next step
  /exit              Quit the Agent program
  /inject <msg>      Inject a message into Agent context; LLM sees it next turn
  /newtask <goal>    Inject a new goal (nostop mode: unblocks the wait loop)
  /compress [N]      Compress context before the next LLM call (keep latest N, default 8)
  /status            Show current state: iteration, active tool, scratchpad
  /log [N]           Show the last N execution records (default 5)
  /+N                Add N more max iterations (e.g. /+50)
  (/status and /log respond immediately even during tool execution)
Tip: just type / to pause; enter the full command then press Enter.
""",

        # ── Internal protocol markers (loop.py producer / persistence.py + server.js consumer) ──
        "marker.tool_prefix":       "[Tool: {name}]",
        "marker.tool_success":      "executed successfully",
        "marker.tool_failure":      "execution failed",
        "marker.output":            "Output:",
        "marker.output_truncated":  "Output (may be truncated):",
        "marker.error_label":       "Error:",
        "marker.retry_hint":        "Analyse the cause, adjust your strategy, and retry (try a different tool or different parameters).",
        "marker.spill_saved":       "Output is large ({chars} chars) and has been saved to: {path}",
        "marker.spill_hint":        "To read the full content, use the shell or run_python tool to read it in sections.",
        "marker.spill_preview":     "Content preview:",
        "marker.vision_skip":       "Images skipped: current model does not support multimodal",
        "marker.json_error":        "JSON parse error",
        "marker.system_prefix":     "[System]",
        "marker.system_cmd":        "[System instruction]",
        "marker.advisor_prefix":    "[Advisor · trigger: {reason}]",
        "marker.advisor_ref":       "The above is a strategic review from an independent perspective. Consider whether to adjust your strategy based on your current task state.",

        # ── compression.py ────────────────────────────────────────────────────
        "compress.bridge_sp": (
            "[System] Early conversation history ({dropped} messages) has been compressed to save context space. "
            "Key findings and progress have been summarised in the scratchpad in the system prompt — "
            "use the scratchpad as the reference for earlier history. "
            "The {keep} most recent records follow."
        ),
        "compress.bridge_no_sp": (
            "[System] Early conversation history ({dropped} messages) has been compressed to save context space. "
            "The {keep} most recent records follow."
        ),
        "compress.request": (
            "[System instruction] Please compress the execution history above into a structured summary.\n"
            "Task goal reference: {goal}"
        ),
        "compress.system": (
            "You are a compression expert for agent execution histories.\n"
            "You will receive a complete message history of an agent interacting with tools.\n"
            "Compress it into a concise, structured execution summary for use as working memory in subsequent steps.\n\n"
            "Output format (plain text only — no JSON, no decorative headers):\n"
            "• Completed: list each completed step and its key result\n"
            "• Key findings: important facts, data, or conclusions discovered during execution\n"
            "• Problems encountered: obstacles and how they were handled (if any)\n"
            "• Current state: how far along the task is, and what the next step is\n\n"
            "Compression principles:\n"
            "- Keep: step results, key data, important decisions, effective solution paths\n"
            "- Discard: verbose raw tool output, repeated failed retries, inconclusive intermediate thoughts\n"
            "- Keep the total under 500 words; be concise and direct"
        ),

        # ── note (auto_scratchpad_note) ───────────────────────────────────────
        "note.system": (
            "You are a concise information-extraction assistant. "
            "Based on the task goal, extract 1-2 of the most important new findings from the tool result. "
            "Requirements: one finding per line, no more than 40 characters each, plain text only — "
            "no JSON, no numbering, do not repeat content already in the scratchpad."
        ),
        "note.user": (
            "Task goal: {goal}\n"
            "Current scratchpad summary: {sp}\n"
            "Tool: {tool}  Args: {args}\n"
            "Tool result:\n{result}"
        ),

        # ── advisor.py ────────────────────────────────────────────────────────
        "advisor.trigger_msg": (
            "Trigger: {reason}\n\n"
            "Please review the Agent's current state below and provide strategic guidance.\n\n"
            "---\n{context}\n---"
        ),
        "advisor.ctx.iter":       "## Current Iteration\nIteration {iter}",
        "advisor.ctx.goal":       "## Task Goal\n{goal}",
        "advisor.ctx.sp":         "## Scratchpad (Agent current state)\n{sp}",
        "advisor.ctx.sp_empty":   "## Scratchpad\n(empty)",
        "advisor.ctx.history":    "## Recent Execution History (last {n})\n{hist}",
        "advisor.ctx.no_history": "(no history yet)",
        "advisor.ctx.truncated":  "…[truncated]",

        # ── sys (build_system_prompt in llm.py) ───────────────────────────────
        "sys.preamble": "You are a general-purpose autonomous agent. You complete any goal by repeatedly calling tools.",
        "sys.format_header":  "## Output format (strictly required — must be valid JSON)",
        "sys.thought_hint":   "Your current reasoning: analyse the situation and decide the next step",
        "sys.note_field":     '  "scratchpad_note": "(optional) 1-2 key findings from the last tool result, auto-appended to scratchpad, ≤40 chars each",',
        "sys.tool_hint":      "tool name (required when action=tool_call)",
        "sys.answer_hint":    "final conclusion (fill when action=done, omit otherwise)",
        "sys.tools_header":   "## Available tools",
        "sys.tools_none":     "(no tools available)",
        "sys.evolved_tag":    " [evolved tool]",
        "sys.params_label":   "  Parameters:",
        "sys.concept_header": "## Macro working memory",
        "sys.memory_header":  "## Fine-grained memory (recent task experience)",
        "sys.patches_header": "## Runtime format rules (auto-generated — must be strictly followed)",
        "sys.sp_header":      "## Scratchpad (editable short-term working memory — distilled key info and plans)",
        "sys.sp_rules":       (
            "- Keep it brief, structured, and rewritable at any time. "
            "Do not paste raw long content (write it to a file and reference the path).\n"
            "- Recommended length: ≤ 2000 characters."
        ),
        "sys.completion_header": "## Required steps before completing the task (important!)",
        "sys.completion_body": """\
Before calling action='done', you MUST complete the following two steps:

1. **Submit a completion report**: call the submit_completion_report tool with a detailed report including:
   - goal_understanding: your understanding of the task goal
   - completed_work: list of work completed
   - remaining_gaps: list of incomplete work (if any)
   - evidence_type: evidence type (artifact/tool_result/observation/none)
   - evidence: evidence list (according to evidence_type)
   - outcome: completion status (done/done_partial/done_blocked)
   - confidence: completion confidence (low/medium/high)

2. **Record episodic memory**: call the append_episodic tool to record key information from this run:
   - path: memory file path (default ./memory_episodic.jsonl)
   - summary: one-paragraph overview (100–300 words) covering key actions, important findings, and final result
   - tags: comma-separated keywords for future retrieval

**Important**: merely claiming in final_answer that you have "submitted the report and recorded episodic memory" is invalid. You must actually call the corresponding tools, or the acceptance check will fail and the task loop will continue until you do.

**Strongly recommended** order at the end of every task:
    1. Call submit_completion_report to submit the completion report
    2. Call append_episodic to record episodic memory
    3. Only then call action='done' to end the task

**Remember**: the system strictly checks for both steps — neither can be skipped!\
""",
        "sys.behavior_header": "## Behaviour rules",
        "sys.behavior_body": """\
1. Take only one action per turn (one tool call)
2. Show complete reasoning in 'thought' — do not skip steps
3. On error, analyse the cause then retry with a different approach
4. Once the goal is complete, use action=done and provide a final_answer
5. Leverage long-term memory experience to avoid repeating mistakes
6. If an evolved tool has a definition/contract error, prefer using validate_tool_recipe, repair_tool_candidate, and promote_tool_candidate to fix it; do not simply register a synonym tool with a new name
7. **WEB display interaction mode**: after calling web_show, the user stays on the web page and continues interacting via the chat box at the bottom. You must first call web_notify to invite the user to interact, then call ask_user to pause and wait — do not proceed to the completion flow (submit_completion_report → done) without an explicit "done" signal from the user.\
""",
        "sys.sp_rules_header": "## Scratchpad (scratchpad) usage rules (mandatory)",
        "sys.sp_rules_body": """\
- The scratchpad is for "intermediate records and analysis during execution" — your workbench for multi-step tasks.
- When a task requires multiple steps:
  1) Before starting, use scratchpad_set to write a brief plan/breakdown (3–8 items).
  2) After each tool call that yields important new information, use scratchpad_append to add "key findings/conclusions/next steps".
- Before finishing (action=done), you MUST append an **ACCEPTANCE** block to the scratchpad (self-evaluation):
  - criteria: the acceptance criteria for this task
  - evidence_type: `artifact` | `tool_result` | `observation` | `none`
  - evidence: evidence. Only include real file paths when `evidence_type=artifact`; for other types, write a brief text description
  - verdict: PASS/FAIL
- Default: choose the appropriate `evidence_type` based on the task — only use `artifact` when a file artifact was actually produced
- Scratchpad must be: brief, structured, and rewritable; do not paste large raw content (write it to artifacts and reference the path).
- Length limit: ≤ 2000 characters (the system will truncate).\
""",

        # ── err.* (generate_error_feedback in llm.py) ─────────────────────────
        "err.prose": (
            "[JSON FORMAT ERROR] Your last output was plain text "
            "(it contained a '{{' character but no valid JSON structure).\n"
            "Error type: prose_with_json — plain text misidentified as JSON\n"
            "Description: the output contained '{{' but did not form a valid JSON object.\n\n"
            "Correct format examples:\n"
            '1. When done: {{"thought": "reasoning...", "action": "done", "final_answer": "answer..."}}\n'
            '2. When calling a tool: {{"thought": "reasoning...", "action": "tool_call", "tool": "tool_name", "args": {{...}}}}\n\n'
            "Please re-output strictly in the JSON format above, ensuring:\n"
            '- All keys and string values are wrapped in double quotes (")\n'
            "- Newlines inside strings are escaped as \\n\n"
            "- Backslashes inside strings are escaped as \\\\\n"
            "- Do not output any Markdown code-block markers (```json ... ```)\n\n"
            "Your raw output (first 200 chars): {raw}"
        ),
        "err.backslash": (
            "[JSON FORMAT ERROR] A string contains an unescaped backslash.\n"
            "Error type: invalid_escape — invalid escape character\n"
            "Description: backslashes in Windows paths (e.g. C:\\\\Users\\\\foo or runs\\\\20260413) "
            "must be written as \\\\\\\\ inside a JSON string, "
            "otherwise the parser treats \\\\U, \\\\2, etc. as illegal escape sequences.\n"
            "Fix example:\n"
            '  Wrong:  {{"path": "runs\\\\20260413\\\\file.txt"}}\n'
            '  Correct: {{"path": "runs\\\\\\\\20260413\\\\\\\\file.txt"}}\n\n'
            "Tip: when referencing paths in thought/final_answer, use forward slashes (/) to avoid this issue, "
            "e.g. runs/20260413-140101 or C:/Users/92680.\n"
            "Raw output (truncated): {raw}"
        ),
        "err.newline": (
            "[JSON FORMAT ERROR] A string contains an unescaped newline.\n"
            "Error type: unescaped_newline — unescaped newline character\n"
            "Description: newline characters inside JSON string values must be escaped as \\n.\n"
            "Fix example:\n"
            '  Wrong:  {{"thought": "line one\\nline two"}}\n'
            '  Correct: {{"thought": "line one\\\\nline two"}}\n\n'
            "Please check that all newlines inside string values are escaped as \\n.\n"
            "Raw output (truncated): {raw}"
        ),
        "err.single_quote": (
            "[JSON FORMAT ERROR] Single quotes used instead of double quotes.\n"
            "Error type: single_quote_key — single-quote key\n"
            "Description: the JSON standard requires double quotes (\") around keys and string values; "
            "single quotes (') are not allowed.\n"
            "Fix example:\n"
            "  Wrong:  {{'thought': 'test', 'action': 'done'}}\n"
            '  Correct: {{"thought": "test", "action": "done"}}\n\n'
            "Please replace all single quotes with double quotes.\n"
            "Raw output (truncated): {raw}"
        ),
        "err.unquoted_value": (
            "[JSON FORMAT ERROR] A string value is missing its double quotes.\n"
            "Error type: unquoted_string_value — unquoted string value\n"
            "Description: all string values in JSON must be wrapped in double quotes.\n"
            "Fix example:\n"
            '  Wrong:  {{"thought": write some code, "action": done}}\n'
            '  Correct: {{"thought": "write some code", "action": "done"}}\n\n'
            "Please check that all field values for thought, action, tool, final_answer, etc. are wrapped in double quotes.\n"
            "Raw output (truncated): {raw}"
        ),
        "err.split_structure": (
            "[JSON FORMAT ERROR] The JSON structure is split.\n"
            "Error type: split_structure — split JSON structure\n"
            "Description: the JSON object was closed prematurely, leaving subsequent fields dangling.\n"
            "Fix example:\n"
            '  Wrong:  {{"thought": "test"}}, "action": "done"}}\n'
            '  Correct: {{"thought": "test", "action": "done"}}\n\n'
            "Please ensure all fields are inside a single JSON object — do not close the curly brace in the middle.\n"
            "Raw output (truncated): {raw}"
        ),
        "err.generic": (
            "[JSON FORMAT ERROR] Could not parse your output.\n"
            "Error: {exc}\n\n"
            "Please check that your output matches one of the following JSON formats:\n"
            '1. When done: {{"thought": "reasoning...", "action": "done", "final_answer": "answer..."}}\n'
            '2. When calling a tool: {{"thought": "reasoning...", "action": "tool_call", "tool": "tool_name", "args": {{...}}}}\n\n'
            "Common errors and fixes:\n"
            '- Use double quotes (") not single quotes (\')\n'
            "- Escape newlines inside strings as \\n\n"
            "- Escape backslashes inside strings as \\\\\n"
            "- Do not include unescaped special characters inside string values\n\n"
            "Raw output (truncated): {raw}"
        ),

        # ── parse.* (inline error thoughts in parse_response) ─────────────────
        "parse.prose_no_json": (
            "Your last output was plain text with no JSON structure.\n"
            "Regardless of whether the task is complete, you must output in JSON format — plain text output is not allowed.\n"
            "If the task is complete, use:\n"
            '{{"thought": "...", "action": "done", "final_answer": "..."}}\n'
            "If you need to call a tool, use:\n"
            '{{"thought": "...", "action": "tool_call", "tool": "tool_name", "args": {{...}}}}'
        ),
        "parse.backslash_error": (
            "JSON format error: string contains an unescaped backslash.\n"
            "Reason: backslashes in Windows paths (e.g. C:\\\\Users\\\\foo or runs\\\\20260413) must be written as \\\\\\\\ "
            "inside a JSON string, otherwise the parser treats \\\\U, \\\\2, etc. as illegal escape sequences and drops fields.\n"
            "Fix example:\n"
            '  Wrong:  {{"thought": "path is C:\\\\Users\\\\92680"}}\n'
            '  Correct: {{"thought": "path is C:\\\\\\\\Users\\\\\\\\92680"}}\n'
            "Tip: when referencing paths in thought/final_answer, use forward slashes (/) to avoid this issue, "
            "e.g. runs/20260413-140101 or C:/Users/92680.\n"
            "Raw output (truncated): {raw}"
        ),
        "parse.unquoted_error": (
            "JSON format error: a string value is missing its opening double quote.\n"
            "Reason: a field's value was written directly without a leading \".\n"
            "Error example:\n"
            '  Wrong:  {{"thought": build a game, "action": "tool_call"}}\n'
            '  Correct: {{"thought": "build a game", "action": "tool_call"}}\n'
            "Please ensure every string value is wrapped in double quotes, including thought, final_answer, and all other fields.\n"
            "Raw output (truncated): {raw}"
        ),
        "parse.string_quote_error": (
            "JSON format error: a string value contains an unescaped double quote.\n"
            'Reason: inside thought/final_answer, if the content itself contains " (e.g. quoting text or English names), '
            'it must be written as \\", otherwise the JSON parser treats it as the end of the string and all subsequent fields are lost.\n'
            "Error example:\n"
            '  Wrong:  {{"thought": "described as "the open-source code", which is a name conflict"}}\n'
            '  Correct: {{"thought": "described as \\"the open-source code\\", which is a name conflict"}}\n'
            "Raw output (truncated): {raw}"
        ),
        "parse.prose_with_json": (
            "Your last output was plain text (it contained JSON fragments but no thought/action fields).\n"
            "Regardless of whether the task is complete, you must output in JSON format — plain text output is not allowed.\n"
            "If the task is complete, use:\n"
            '{{"thought": "...", "action": "done", "final_answer": "..."}}\n'
            "If you need to call a tool, use:\n"
            '{{"thought": "...", "action": "tool_call", "tool": "tool_name", "args": {{...}}}}'
        ),
        "parse.not_object": "JSON top-level must be an object, but got: {typename}={val}. Raw output: {raw}",
        "parse.missing_tool_split": (
            "Note: the raw output contained a \"tool\" field, but it was lost after parsing — "
            "this usually means 'thought' was closed prematurely (i.e. thought itself formed a standalone {{}} "
            "causing tool/args and other fields to fall outside).\n"
            "Please put all fields inside a single top-level {{}}:\n"
            '{{"thought": "...", "action": "tool_call", "tool": "tool_name", "args": {{...}}}}'
        ),
        "parse.missing_tool_question": (
            "Detected: you asked the user a question in plain text outside the JSON.\n"
            "Correct approach: use the ask_user tool, with the question in args.question:\n"
            '{{"thought": "...", "action": "tool_call", "tool": "ask_user", "args": {{"question": "your question"}}}}'
        ),
        "parse.missing_tool_default": '{{"action":"tool_call","tool":"tool_name","args":{{...}}}}',
        "parse.missing_tool_msg": (
            "action=tool_call but the parsed result is missing the 'tool' field.\n"
            "{hint}\n"
            "thought: {thought}"
        ),
        "parse.invalid_action": (
            "action='{action}' is invalid — action can only be 'tool_call' or 'done'.\n"
            "To call a tool, use this exact format:\n"
            '{{"thought":"...","action":"tool_call","tool":"tool_name","args":{{...}}}}\n'
            "For example, calling ask_user:\n"
            '{{"thought":"...","action":"tool_call","tool":"ask_user","args":{{"question":"your question"}}}}'
        ),

        # ── run_goal.py — LLM-facing goal prefix strings ──────────────────────
        "rg.prefix_preloaded": "Tools, fine-grained memory, and concept memory have been pre-loaded. Please proceed directly with the task.\n\n",
        "rg.agents_md_rule":   "[RULES] You must follow AGENTS.md (repository conventions) in the root directory.\n",
        "rg.run_dir_with_agents": "This run: RUN_DIR={run_dir}; all temporary/intermediate artifacts must be written to {run_dir}/artifacts/.\n\n",
        "rg.run_dir_hint":     "Note: this run RUN_DIR={run_dir}. Recommended to write temporary/intermediate artifacts to {run_dir}/artifacts/.\n\n",
        "rg.skills_header":    "[DOMAIN SKILLS] The following domain-specific rules are active for this task. Please follow them:\n\n",
        "rg.nostop_await":     "Task complete — entering continuous dialogue mode. Enter the next goal, or /exit to quit:",
        "rg.scratchpad_init":  "Task description:\n{goal}\n",
        "rg.next_goal_msg":    "Please complete the following goal:\n\n{goal}",

        # ── run_goal.py — terminal strings ────────────────────────────────────
        "rg.hint_header": (
            "[Hint] You can send intervention commands at any time while the Agent is running (prefix with /):\n"
            "  /help   show all commands    /stop   stop current tool\n"
            "  /exit   quit program         /inject <msg>  inject context\n"
            "  /status show current state   /+N  add N iterations\n"
        ),
        "rg.hint_nostop": (
            "  /newtask <goal>  inject a new goal (nostop mode)\n"
            "  [nostop mode enabled] Agent will wait for the next goal after each task.\n"
        ),
        "rg.intervention_header":  "─── Intervention mode ───────────────────────────────",
        "rg.intervention_prompt": (
            "Enter a /command (e.g. /stop /exit /inject <msg>)\n"
            "or type plain text to inject it into the Agent context (equivalent to /inject):"
        ),
        "rg.intervention_timeout": "[Interrupt] No input received — resuming.",
        "rg.user_confirmed":       "[run_goal] User confirmed done — exiting.",
        "rg.nostop_done":          "[nostop] ✅ Round {n} complete.",
        "rg.nostop_prompt":        "[nostop] Enter the next goal (/exit to quit):",

        # ── marker: user supplementary info ──────────────────────────────────
        "marker.user_info": "[User input]\n{content}",

        # ── agent/core/executor.py — LLM-facing error messages ────────────────
        "exec.not_found":  "Tool '{name}' does not exist. Available tools: {available}",
        "exec.arg_error":  "Tool argument error: {e}{hint}",
        "exec.exec_error": "Tool execution error: {etype}: {e}",
    },
}

# ── Public API ────────────────────────────────────────────────────────────────

def t(key: str, **kwargs) -> str:
    """Return the localised string for *key*, interpolating any *kwargs*."""
    table = _STRINGS.get(LANG, _STRINGS["zh"])
    s = table.get(key) or _STRINGS["zh"].get(key, key)
    return s.format(**kwargs) if kwargs else s
