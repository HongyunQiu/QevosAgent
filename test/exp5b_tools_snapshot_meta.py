import os
from agent import Agent

os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
os.environ.setdefault("OPENAI_API_KEY", "local")
os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")

GOAL = (
    "请自由发挥，在一次运行中完成并验收：\n"
    "0) 背景提示：框架会把你用 register_tool 进化出来的工具配方记录在 state.meta['evolved_tools'] 中（包含 name/description/args_schema/python_code）。\n"
    "1) 通过 register_tool 创建 save_snapshot(path) 与 load_snapshot(path)。\n"
    "   - save_snapshot: 将 state.long_term + state.meta['evolved_tools'] 保存到 JSON 文件。\n"
    "   - load_snapshot: 从 JSON 文件恢复 state.long_term，并把 evolved_tools 逐个 register_tool 恢复为可调用工具。\n"
    "2) 创建一个可用的 http_get2 工具用于 GET 一个 URL，并遵守 ToolResult(success, output, error) 契约（返回 (bool, str, str)）。\n"
    "3) remember 写入：SNAPSHOT_TEST_META TIMESTAMP=2026-03-02T23:15:00+08:00。\n"
    "4) save_snapshot 写到 ./agent_snapshot_meta.json。\n"
    "5) 模拟重启：创建全新 Agent 实例 new_agent（不要复用旧实例）。\n"
    "6) new_agent 调用 load_snapshot('./agent_snapshot_meta.json') 后，不得再次 register_tool 定义 http_get2；必须直接调用已恢复的 http_get2 去抓 https://httpbin.org/json 并总结。\n"
    "7) 最后输出：new_agent long_term 条数、是否包含 SNAPSHOT_TEST_META、以及工具恢复是否成功。\n"
)

agent = Agent(backend="openai", api_key=os.environ["OPENAI_API_KEY"], max_iterations=80, verbose=True)
state = agent.run(GOAL)

print("\n=== EXP5B RESULT ===")
print("FINAL:", state.meta.get("final_answer"))
print("TOOLS:", list(state.tools.keys()))
print("META_EVOLVED_TOOLS:", list(state.meta.get("evolved_tools", {}).keys()))
print("LONG_TERM_LAST10:", state.long_term[-10:])
