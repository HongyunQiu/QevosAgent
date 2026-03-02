#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from agent import Agent

DEFAULT_SNAPSHOT = "./agent_snapshot_meta.json"


def ensure_env_defaults():
    # Defaults for your local OpenAI-compatible vLLM (gpt-oss-120b)
    os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
    os.environ.setdefault("OPENAI_API_KEY", "local")
    os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")


def main():
    ensure_env_defaults()

    goal = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    if not goal:
        print("Enter your goal/task, then press Ctrl-D (EOF) to run:\n")
        goal = sys.stdin.read().strip()

    if not goal:
        print("No goal provided.")
        sys.exit(2)

    snapshot_path = os.environ.get("AGENT_SNAPSHOT", DEFAULT_SNAPSHOT)
    snapshot_exists = Path(snapshot_path).exists()

    # Prefix instruction: always load snapshot first (offline restore tool)
    prefix = (
        f"请先调用 load_snapshot_meta(path='{snapshot_path}') 加载快照，恢复长期记忆与工具。"
        f"如果文件不存在（exists={snapshot_exists}），请先解释缺少什么并给出下一步。\n\n"
    )

    full_goal = prefix + goal

    agent = Agent(
        backend="openai",
        api_key=os.environ.get("OPENAI_API_KEY"),
        max_iterations=int(os.environ.get("MAX_ITERS", "40")),
        verbose=True,
    )

    state = agent.run(full_goal)

    # If the agent paused for input, prompt the user and resume.
    while state.meta.get("paused") and state.meta.get("awaiting_input"):
        q = state.meta.get("awaiting_input")
        print("\n=== NEED INPUT ===")
        print(q)
        try:
            user_input = input("\nYour answer> ").strip()
        except EOFError:
            print("\n[run_goal] No interactive stdin available (EOF). Please rerun in a real terminal to answer.")
            break
        if not user_input:
            print("No input provided; exiting.")
            break

        # Inject user input into the same conversation state and resume.
        state.short_term.append({
            "role": "user",
            "content": f"[用户补充信息]\n{user_input}",
        })
        # Resume with same goal (the new info is in short_term)
        state = agent.run(goal, state=state)

    print("\n=== RUN_GOAL RESULT ===")
    print(state.meta.get("final_answer") or "(no final_answer)")


if __name__ == "__main__":
    main()
