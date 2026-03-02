#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from agent import Agent

DEFAULT_SNAPSHOT = "./agent_snapshot_meta.json"
DEFAULT_RUNS_DIR = "./runs"


def ensure_env_defaults():
    # Defaults for your local OpenAI-compatible vLLM (gpt-oss-120b)
    os.environ.setdefault("OPENAI_BASE_URL", "http://172.24.168.225:8389/v1")
    os.environ.setdefault("OPENAI_API_KEY", "local")
    os.environ.setdefault("OPENAI_MODEL", "openai/gpt-oss-120b")


def main():
    ensure_env_defaults()

    # Per-run workspace (raw memory, scratchpad copies, etc.)
    from datetime import datetime
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_dir = Path(os.environ.get("RUNS_DIR", DEFAULT_RUNS_DIR))
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Default raw memory path per run
    os.environ.setdefault("RAW_MEMORY_PATH", str(run_dir / "raw_memory.ndjson"))

    goal = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    if not goal:
        print("Enter your goal/task, then press Ctrl-D (EOF) to run:\n")
        goal = sys.stdin.read().strip()

    if not goal:
        print("No goal provided.")
        sys.exit(2)

    snapshot_path = os.environ.get("AGENT_SNAPSHOT", DEFAULT_SNAPSHOT)
    snapshot_exists = Path(snapshot_path).exists()

    # Print scratchpad preview (if any) BEFORE LLM run
    if snapshot_exists:
        try:
            import json
            snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
            sp = snap.get("scratchpad", "") if isinstance(snap, dict) else ""
            if isinstance(sp, str) and sp.strip():
                print("\n=== SCRATCHPAD (loaded from snapshot) ===\n")
                print(sp.strip())
                print("\n=== END SCRATCHPAD ===\n")
        except Exception:
            pass

    # Prefix instruction: load snapshot when available; otherwise proceed without it.
    if snapshot_exists:
        prefix = (
            f"请先调用 load_snapshot_meta(path='{snapshot_path}') 加载快照，恢复长期记忆与工具。\n\n"
        )
    else:
        prefix = (
            f"提示：快照文件不存在({snapshot_path})。请直接继续完成任务；"
            f"如确实需要跨次记忆/工具，可在结束时保存快照。\n\n"
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

    # Always persist a copy of scratchpad for analysis (per-run)
    try:
        sp = state.meta.get("scratchpad", "")
        (run_dir / "scratchpad.md").write_text(sp or "", encoding="utf-8")
    except Exception:
        pass

    # Optional: persist snapshot after run (so long_term is not lost between processes)
    if os.environ.get("AUTO_SAVE_SNAPSHOT_ON_EXIT", "0") == "1":
        snap = os.environ.get("AGENT_SNAPSHOT", DEFAULT_SNAPSHOT)
        try:
            if "save_snapshot_meta" in state.tools:
                # call tool directly (offline) to persist long_term + evolved_tools + scratchpad
                state.tools["save_snapshot_meta"].fn(state=state, path=snap)
                print(f"\n[run_goal] snapshot saved: {snap}")
            else:
                print("\n[run_goal] save_snapshot_meta tool not available; snapshot not saved")
        except Exception as e:
            print(f"\n[run_goal] snapshot save failed: {e}")

    print("\n=== RUN_GOAL RESULT ===")
    print(state.meta.get("final_answer") or "(no final_answer)")

    print(f"\n[run_goal] run_dir: {run_dir}")
    print(f"[run_goal] raw_memory: {os.environ.get('RAW_MEMORY_PATH')}")
    print(f"[run_goal] scratchpad_copy: {run_dir / 'scratchpad.md'}")


if __name__ == "__main__":
    main()
