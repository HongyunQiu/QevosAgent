import os
from agent import Agent

os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
os.environ.setdefault("OPENAI_API_KEY", "local")
os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")

GOAL = (
    "严格验收（跨实例）：\n"
    "1) 你必须调用 load_snapshot_meta('./agent_snapshot_meta.json') 恢复长期记忆和工具。\n"
    "2) 你【禁止】用 register_tool 创建/重建 http_get2，也禁止用 shell/curl 绕过。\n"
    "3) load_snapshot_meta 完成后，必须直接调用恢复出来的 http_get2 去抓 https://httpbin.org/json。\n"
    "4) 用 run_python 解析返回的 JSON，输出 slideshow 的 title、author、slides 数量。\n"
    "5) 最后输出 PASS/FAIL（如果任何一步没做到就 FAIL）。\n"
)

new_agent = Agent(backend="openai", api_key=os.environ["OPENAI_API_KEY"], max_iterations=40, verbose=True)
state = new_agent.run(GOAL)

print("\n=== EXP5C3 RESULT ===")
print("FINAL:", state.meta.get("final_answer"))
