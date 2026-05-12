"""
Minimal i18n for QevosAgent — terminal/UI strings only.

Language is detected from the system locale; set QEVOS_LANG=zh or QEVOS_LANG=en
to override.  Only strings that never reach the LLM are translated here.
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
    },
}

# ── Public API ────────────────────────────────────────────────────────────────

def t(key: str, **kwargs) -> str:
    """Return the localised string for *key*, interpolating any *kwargs*."""
    table = _STRINGS.get(LANG, _STRINGS["zh"])
    s = table.get(key) or _STRINGS["zh"].get(key, key)
    return s.format(**kwargs) if kwargs else s
