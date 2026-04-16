import os
from agent import Agent

# OpenAI-compatible local model (gpt-oss-120b)
os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
os.environ.setdefault("OPENAI_API_KEY", "local")
os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")

GOAL = (
    "请自由发挥，在一次运行中完成并验收：\n"
    "1) 通过 register_tool 创建 save_snapshot(path) 与 load_snapshot(path)。\n"
    "   - save_snapshot: 将 state.long_term 以及你通过 register_tool 创建的新工具(至少包含 name/description/args_schema/python_code)保存到 JSON 文件。\n"
    "   - load_snapshot: 从该 JSON 文件恢复 state.long_term，并自动逐个恢复工具（必要时调用 register_tool）。\n"
    "2) 为了可验收，请先创建一个可用的 http_get2 工具（或类似名字）用于 GET 一个 URL，并遵守 ToolResult(success, output, error) 契约（即返回 (bool, str, str)）。\n"
    "3) 用 remember 写入一条测试记忆：SNAPSHOT_TEST TIMESTAMP=2026-03-02T23:12:00+08:00。\n"
    "4) 调用 save_snapshot 把快照写到 ./agent_snapshot.json。\n"
    "5) 模拟重启：创建一个全新的 Agent 实例 new_agent（不要复用旧实例）。\n"
    "6) 让 new_agent 调用 load_snapshot('./agent_snapshot.json') 恢复记忆与工具。\n"
    "7) 验收：new_agent 必须直接调用恢复出来的 http_get2（或你保存的 GET 工具）去抓 https://httpbin.org/json，然后总结。\n"
    "8) 最后输出：new_agent 的长期记忆条数、是否包含 SNAPSHOT_TEST、以及本次验收是否通过。\n"
)

agent = Agent(backend="openai", api_key=os.environ["OPENAI_API_KEY"], max_iterations=60, verbose=True)
state = agent.run(GOAL)

print("\n=== EXP5 RESULT ===")
print("FINAL:", state.meta.get("final_answer"))
print("TOOLS:", list(state.tools.keys()))
print("LONG_TERM_LAST10:", state.long_term[-10:])
