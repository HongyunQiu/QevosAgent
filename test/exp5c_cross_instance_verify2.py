import os, sys
from agent import Agent

os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
os.environ.setdefault("OPENAI_API_KEY", "local")
os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")

print("[exp5c] START", flush=True)
print("[exp5c] base_url=", os.environ.get("OPENAI_BASE_URL"), flush=True)
print("[exp5c] model=", os.environ.get("OPENAI_MODEL"), flush=True)

GOAL = (
    "严格验收：\n"
    "1) 你必须从 ./agent_snapshot_meta.json 恢复工具与长期记忆。\n"
    "2) 你可以 register_tool 创建一个 load_snapshot 工具来加载快照（如果缺）。\n"
    "3) 但你【禁止】用 register_tool 创建/重建 http_get2（也禁止用 shell/curl 绕过）。\n"
    "4) 加载快照后，必须直接调用从快照恢复出来的 http_get2 去抓 https://httpbin.org/json，然后总结其中 slideshow 的 title/author/slides 数量。\n"
    "5) 最后输出：load_snapshot 是否成功、http_get2 是否来自快照（说明理由）、以及验收 PASS/FAIL。\n"
)

new_agent = Agent(backend="openai", api_key=os.environ["OPENAI_API_KEY"], max_iterations=60, verbose=True)
state = new_agent.run(GOAL)

print("\n=== EXP5C RESULT ===")
print("FINAL:", state.meta.get("final_answer"))
print("TOOLS:", list(state.tools.keys()))
print("META_EVOLVED_TOOLS_KEYS:", list(state.meta.get("evolved_tools", {}).keys()))
print("LONG_TERM_HAS_TEST:", any("SNAPSHOT_TEST_META" in x for x in state.long_term))
print("[exp5c] END", flush=True)
