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

    # scratchpad persistence disabled by default (often low-signal / stale).
    # If you want it back for debugging, re-enable writing run_dir/scratchpad.md here.

    # Always persist final answer (per-run)
    try:
        final_answer = state.meta.get("final_answer") or ""
        (run_dir / "final_answer.md").write_text(final_answer, encoding="utf-8")
    except Exception:
        pass

    # Always persist raw session data (per-run)
    # - short_term is the full linear transcript used for prompting (tool calls/results)
    # - meta captures run diagnostics (token estimates, pauses, etc.)
    try:
        import json as _json
        st_path = run_dir / "short_term.jsonl"
        with st_path.open("w", encoding="utf-8") as f:
            for rec in state.short_term:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        (run_dir / "meta.json").write_text(_json.dumps(state.meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Always persist an execution summary (process + issues + reflections) for future reuse.
    try:
        import json as _json
        import re as _re

        # Extract tool usage + failures from short_term.
        used_tools: list[str] = []
        failures: list[str] = []
        issues: list[dict] = []
        json_parse_errors = 0
        self_heal_notes: list[str] = []

        for idx, m in enumerate(state.short_term):
            c = m.get("content", "")
            if not isinstance(c, str):
                continue

            # Tool usage (best-effort: inferred from assistant JSON)
            if '"tool"' in c:
                mt = _re.search(r'"tool"\s*:\s*"([^"]+)"', c)
                if mt:
                    used_tools.append(mt.group(1))

            # Failure snippets
            if "执行失败" in c or "[TOOL ERROR]" in c:
                failures.append(c[:800])
                issues.append({
                    "kind": "tool_failure",
                    "short_term_index": idx,
                    "snippet": c[:2000],
                })

            # JSON parse errors
            if "JSON 解析失败" in c:
                json_parse_errors += 1
                issues.append({
                    "kind": "json_parse_error",
                    "short_term_index": idx,
                    "snippet": c[:2000],
                })

        # Long-term notes often contain self-heal events.
        for s in state.long_term:
            if isinstance(s, str) and ("[自我修复]" in s or "[RUN_OK]" in s):
                self_heal_notes.append(s)

        # De-dup tool list while keeping order.
        seen = set()
        used_tools2 = []
        for t in used_tools:
            if t in seen:
                continue
            seen.add(t)
            used_tools2.append(t)

        prompt_est = state.meta.get("prompt_tokens_est")
        ctx = state.meta.get("context_window")
        timeout = bool(state.meta.get("timeout"))

        summary_lines: list[str] = []
        summary_lines.append(f"# Execution Summary\n")
        summary_lines.append(f"## Goal\n{state.goal}\n")
        summary_lines.append("## Outcome (final_answer)\n")
        summary_lines.append((state.meta.get("final_answer") or "(no final_answer)") + "\n")

        summary_lines.append("## Run Artifacts\n")
        summary_lines.append(
            "- final_answer.md\n"
            "- execution_summary.md\n"
            "- reflection.md\n"
            "- issues.json\n"
            "- short_term.jsonl\n"
            "- meta.json\n"
            "- scratchpad.md (disabled)\n"
        )

        summary_lines.append("## What actually happened (high-level steps)\n")
        if used_tools2:
            summary_lines.append("Tools used (inferred from assistant JSON):\n")
            summary_lines.extend([f"- {t}" for t in used_tools2])
            summary_lines.append("")

        summary_lines.append("## Issues observed\n")
        summary_lines.append(f"- JSON parse errors: {json_parse_errors}\n")
        summary_lines.append(f"- Timeout hit: {timeout}\n")
        if failures:
            summary_lines.append("### Tool/Execution failures (snippets)\n")
            for i, snip in enumerate(failures[:10], 1):
                summary_lines.append(f"{i}. {snip.replace('```','')}")
            summary_lines.append("")

        summary_lines.append("## Self-healing / reflections captured during run\n")
        if self_heal_notes:
            summary_lines.extend([f"- {x}" for x in self_heal_notes[-20:]])
        else:
            summary_lines.append("- (none)\n")

        summary_lines.append("\n## Recommendations for next time\n")
        summary_lines.append("- Prefer shorter tool args to avoid JSON truncation/parse issues (split long code into multiple tool calls).\n")
        summary_lines.append("- If you need durable learning, enable AUTO_SAVE_SNAPSHOT_ON_EXIT=1 to persist evolved_tools + long_term.\n")
        if prompt_est and ctx:
            summary_lines.append(f"- Prompt size: est {prompt_est}/{ctx}; if near limit, consider trimming large tool outputs earlier.\n")

        (run_dir / "execution_summary.md").write_text("\n".join(summary_lines).strip() + "\n", encoding="utf-8")

        # Structured issues export (machine-friendly)
        (run_dir / "issues.json").write_text(
            _json.dumps(
                {
                    "goal": state.goal,
                    "timeout": timeout,
                    "json_parse_errors": json_parse_errors,
                    "used_tools": used_tools2,
                    "issues": issues,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # A separate reflection file focused on process + mistakes + improvements.
        # We attempt an LLM-based reflection; if it fails, fall back to a heuristic reflection.
        reflection_md = ""
        try:
            from agent.core.llm import OpenAIBackend  # local import
            base_url = os.environ.get("OPENAI_BASE_URL")
            model = os.environ.get("OPENAI_MODEL") or "gpt-4o"
            api_key = os.environ.get("OPENAI_API_KEY")
            rb = OpenAIBackend(model=model, api_key=api_key, base_url=base_url, max_tokens=int(os.environ.get("REFLECTION_MAX_TOKENS", "2048")))

            prompt = (
                "你是一个负责做 run postmortem 的工程师。\n"
                "请基于给定的运行事实，写一份‘对执行过程的总结’（不是对结果的总结）。\n\n"
                "要求：\n"
                "- 结构：1) 实际执行链路(按时间) 2) 发生的问题/异常 3) 根因分析 4) 当场如何修复 5) 下次行动清单(可执行)\n"
                "- 聚焦未来可复用的经验；避免空话。\n"
                "- 用中文，200~600字。\n\n"
                f"[GOAL]\n{state.goal}\n\n"
                f"[USED_TOOLS]\n{used_tools2}\n\n"
                f"[FINAL_ANSWER]\n{state.meta.get('final_answer') or ''}\n\n"
                f"[ISSUES_JSON]\n{_json.dumps(issues[:20], ensure_ascii=False)}\n\n"
                f"[SELF_HEAL_NOTES]\n" + "\n".join(self_heal_notes[-10:])
            )

            resp = rb.complete(messages=[{"role": "user", "content": prompt}], system="You write concise engineering postmortems.")
            reflection_md = resp.strip() if isinstance(resp, str) else str(resp)
        except Exception:
            # heuristic fallback
            reflection_md = (
                "# Reflection\n\n"
                "## 实际执行链路（概览）\n"
                + ("- " + "\n- ".join(used_tools2) + "\n\n" if used_tools2 else "- (unknown)\n\n")
                + "## 发生的问题/异常\n"
                + (f"- JSON 解析失败次数：{json_parse_errors}\n" if json_parse_errors else "- 未观察到 JSON 解析失败\n")
                + ("- 观察到工具执行失败片段（见 issues.json）\n\n" if failures else "\n")
                + "## 下次行动清单\n"
                "- 把长代码/长参数拆成多步工具调用，避免 JSON 截断\n"
                "- 开启 AUTO_SAVE_SNAPSHOT_ON_EXIT=1 持久化长期记忆与进化工具\n"
            )

        (run_dir / "reflection.md").write_text(reflection_md + ("\n" if not reflection_md.endswith("\n") else ""), encoding="utf-8")

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
