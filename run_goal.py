#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

from agent import Agent
from agent.runtime.persistence import RunPersistence

DEFAULT_SNAPSHOT = "./agent_snapshot_meta.json"
DEFAULT_RUNS_DIR = "./runs"


def load_dotenv_if_present(path: str = ".env"):
    """Load simple KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables win over .env values.
    This keeps the runtime lightweight and avoids an extra dependency.
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def ensure_env_defaults():
    load_dotenv_if_present()

    # Model profile switch (optional):
    #   OPENAI_PROFILE=oss120b      -> env OPENAI_PROFILE_OSS120B_BASE_URL / openai/gpt-oss-120b
    #   OPENAI_PROFILE=qwen3527dgx  -> env OPENAI_PROFILE_QWEN3527DGX_BASE_URL / qwen3527dgx
    profile = (os.environ.get("OPENAI_PROFILE") or "oss120b").strip().lower()

    profile_defaults = {
        "oss120b": {
            "base_url_env": "OPENAI_PROFILE_OSS120B_BASE_URL",
            "model": "openai/gpt-oss-120b",
        },
        "qwen3527dgx": {
            "base_url_env": "OPENAI_PROFILE_QWEN3527DGX_BASE_URL",
            "model": "qwen3527dgx",
        },
    }
    profile_config = profile_defaults.get(profile)
    if profile_config is None:
        raise ValueError(f"未知 OPENAI_PROFILE: {profile}")

    if "OPENAI_BASE_URL" not in os.environ:
        profile_base_url = os.environ.get(profile_config["base_url_env"])
        if profile_base_url:
            os.environ["OPENAI_BASE_URL"] = profile_base_url
        else:
            raise ValueError(
                f"缺少 OPENAI_BASE_URL。当前 profile={profile}，"
                f"请设置 OPENAI_BASE_URL 或 {profile_config['base_url_env']}。"
            )

    if profile == "qwen3527dgx":
        os.environ.setdefault("OPENAI_API_KEY", "local")
        os.environ.setdefault("OPENAI_MODEL", profile_config["model"])
    else:
        os.environ.setdefault("OPENAI_API_KEY", "local")
        os.environ.setdefault("OPENAI_MODEL", profile_config["model"])

    # Persist useful experience by default unless the caller explicitly disables it.
    os.environ.setdefault("AUTO_REMEMBER_ON_DONE", "1")
    os.environ.setdefault("AUTO_SAVE_SNAPSHOT_ON_EXIT", "1")


def probe_openai_configuration(list_models=None):
    """Verify the configured OpenAI-compatible endpoint before starting the agent.

    Returns a small dict describing the resolved model. If the configured model
    is missing but the server exposes exactly one model, auto-switch to it to
    reduce manual config churn.
    """
    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    api_key = os.environ.get("OPENAI_API_KEY")
    model = (os.environ.get("OPENAI_MODEL") or "").strip()

    if not base_url:
        raise ValueError("LLM 服务探测失败: 缺少 OPENAI_BASE_URL。")
    if not model:
        raise ValueError("LLM 服务探测失败: 缺少 OPENAI_MODEL。")

    if list_models is None:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        list_models = client.models.list

    try:
        resp = list_models()
    except Exception as e:
        raise RuntimeError(
            f"LLM 服务探测失败: 无法连接 {base_url}。"
            f"请检查 OPENAI_BASE_URL / 网络 / 服务状态。原始错误: {e}"
        ) from e

    model_ids = []
    for item in getattr(resp, "data", []) or []:
        model_id = getattr(item, "id", None)
        if model_id:
            model_ids.append(str(model_id))

    if model in model_ids:
        return {
            "base_url": base_url,
            "configured_model": model,
            "resolved_model": model,
            "available_models": model_ids,
            "auto_selected": False,
        }

    if len(model_ids) == 1:
        resolved = model_ids[0]
        os.environ["OPENAI_MODEL"] = resolved
        return {
            "base_url": base_url,
            "configured_model": model,
            "resolved_model": resolved,
            "available_models": model_ids,
            "auto_selected": True,
        }

    shown = ", ".join(model_ids[:5]) if model_ids else "(空列表)"
    raise ValueError(
        f"LLM 服务探测失败: 配置的模型 `{model}` 不在 {base_url} 返回的模型列表中。"
        f"可用模型: {shown}"
    )


def format_probe_summary(probe: dict) -> str:
    base_url = probe["base_url"]
    configured = probe["configured_model"]
    resolved = probe["resolved_model"]
    if probe.get("auto_selected"):
        return (
            "[run_goal] probe: endpoint ok; "
            f"configured={configured!r}; resolved={resolved!r}; "
            f"auto-selected the only available model from {base_url}"
        )
    return (
        "[run_goal] probe: endpoint ok; "
        f"model={resolved!r}; base_url={base_url}"
    )


def main():
    ensure_env_defaults()
    probe = probe_openai_configuration()
    print(format_probe_summary(probe))

    # Per-run workspace (raw memory, scratchpad copies, etc.)
    from datetime import datetime
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_dir = Path(os.environ.get("RUNS_DIR", DEFAULT_RUNS_DIR))
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write PID file so the dashboard can verify this process is still alive.
    # Deleted in the finally block below; the dashboard treats absence as "dead".
    _pid_file = run_dir / "agent.pid"
    try:
        _pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        _pid_file = None  # non-fatal

    # Per-run dirs exposed to the agent
    os.environ.setdefault("RUN_DIR", str(run_dir))

    # Default raw memory path per run
    os.environ.setdefault("RAW_MEMORY_PATH", str(run_dir / "raw_memory.ndjson"))

    goal = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    # Expose the raw user goal (without injected prefixes) for scratchpad seeding.
    os.environ["USER_GOAL"] = goal
    if not goal:
        print("Enter your goal/task, then press Ctrl-D (EOF) to run:\n")
        goal = sys.stdin.read().strip()

    if not goal:
        print("No goal provided.")
        sys.exit(2)

    snapshot_path = os.environ.get("AGENT_SNAPSHOT", DEFAULT_SNAPSHOT)
    snapshot_exists = Path(snapshot_path).exists()

    # Pre-load long_term from snapshot so history survives even if LLM skips load_snapshot_meta.
    preloaded_long_term: list[str] = []
    if snapshot_exists:
        try:
            snap_data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
            if isinstance(snap_data, dict) and isinstance(snap_data.get("long_term"), list):
                preloaded_long_term = [x for x in snap_data["long_term"] if isinstance(x, str)]
        except Exception:
            pass

    # Scratchpad preview printing disabled: scratchpad is often stale/low-signal and noisy in logs.

    # Prefix instruction: load snapshot when available; otherwise proceed without it.
    if snapshot_exists:
        prefix = (
            f"你必须先调用 load_snapshot_meta(path='{snapshot_path}') 加载快照，恢复长期记忆与工具。\n"
            f"注意：加载快照只是准备步骤；完成后必须继续完成下面的用户目标，绝不能在此提前 done。\n\n"
        )
    else:
        prefix = (
            f"提示：快照文件不存在({snapshot_path})。请直接继续完成下面的用户目标；"
            f"如确实需要跨次记忆/工具，可在结束时保存快照。\n\n"
        )

    # Load repo conventions (OpenClaw-style) if present.
    # Keep it short; it's a hard constraint but should not bloat prompts.
    conventions = ""
    try:
        p = Path("./AGENTS.md")
        if p.exists():
            conventions = p.read_text(encoding="utf-8").strip()
    except Exception:
        conventions = ""

    if conventions:
        prefix = (
            prefix
            + "【总规范】你必须遵守仓库根目录的 AGENTS.md（运行规范）。\n"
            + f"本次运行 RUN_DIR={run_dir}；所有临时/中间产物必须写入 {run_dir}/artifacts/。\n\n"
            + conventions
            + "\n\n"
        )
    else:
        prefix = prefix + f"提示：本次运行 RUN_DIR={run_dir}。建议将临时/中间产物写入 {run_dir}/artifacts/。\n\n"

    full_goal = prefix + goal

    agent = Agent(
        backend="openai",
        api_key=os.environ.get("OPENAI_API_KEY"),
        max_iterations=int(os.environ.get("MAX_ITERS", "100")),
        verbose=True,
        long_term=preloaded_long_term,
    )

    # ── 用户干预处理器：后台线程读取 stdin，"/" 开头为命令，其余为 ask_user 回答 ──
    from agent.runtime.user_interrupt import UserInterruptHandler
    interrupt_handler = UserInterruptHandler()
    agent.hooks.interrupt_handler = interrupt_handler  # 挂载到 hooks，loop.py 会检查

    BLUE  = "\033[94m"
    RESET = "\033[0m"
    print(
        f"{BLUE}[提示] Agent 运行期间可随时输入干预命令（以 / 开头）：\n"
        f"  /help   显示所有命令    /stop   停止当前工具\n"
        f"  /exit   退出程序        /inject <消息>  注入上下文\n"
        f"  /status 查看当前状态   /+N  增加 N 次迭代{RESET}"
    )

    persistence = RunPersistence(run_dir)
    state = None
    run_error = None

    interrupt_handler.start()
    try:
        state = agent.run(full_goal)

        # If the agent paused for input, prompt the user and resume.
        while state.meta.get("paused") and state.meta.get("awaiting_input"):

            if state.meta.get("user_stopped"):
                # /exit：不再询问，直接退出循环
                break

            if state.meta.get("user_typing_pause"):
                # 用户按下 / 触发的暂停：等待完整命令或纯文本（自动包装为 /inject）
                print(
                    f"\n{BLUE}─── 干预模式 ────────────────────────────────────────{RESET}\n"
                    f"{BLUE}输入 /命令（如 /stop /exit /inject <消息>）\n"
                    f"或直接输入文字，将自动注入到 Agent 上下文（效果等同 /inject）：{RESET}"
                )
                cmd = None
                import time as _time
                _deadline = _time.time() + 120.0  # 最多等 2 分钟
                while _time.time() < _deadline:
                    # 优先检查命令队列（/ 开头）
                    cmd = interrupt_handler.wait_command(timeout=0.2)
                    if cmd is not None:
                        # /__pause__ 是"用户按了/"的哨兵，干预模式下已无意义，丢弃继续等待
                        if cmd == "/__pause__":
                            cmd = None
                            continue
                        break
                    # 再检查纯文本队列（用户直接输入文字，自动包装为 /inject）
                    try:
                        text = interrupt_handler._input_queue.get_nowait()
                        if text is not None and text.strip():
                            cmd = f"/inject {text.strip()}"
                        break
                    except Exception:
                        pass

                if cmd is None:
                    # 超时，没有任何输入 → 恢复执行
                    print(f"{BLUE}[干预] 未收到输入，恢复执行。{RESET}")
                    state.meta.pop("paused", None)
                    state.meta.pop("awaiting_input", None)
                    state.meta.pop("user_typing_pause", None)
                    state = agent.run(goal, state=state)
                    continue

                result = interrupt_handler.process_command(cmd, state)
                if result == "pause":
                    # 不应出现（/__pause__ 已过滤），若出现则继续等待
                    continue
                state.meta.pop("paused", None)
                state.meta.pop("awaiting_input", None)
                state.meta.pop("user_typing_pause", None)
                if result == "stop":
                    state.meta["user_stopped"] = True
                    break
                # /inject 已注入 or /status 已显示 → 恢复执行
                if state.persistence is not None:
                    state.persistence.checkpoint(state)
                state = agent.run(goal, state=state)
                continue

            # 普通 ask_user 暂停：Agent 提问，等待用户文字回答
            q = state.meta.get("awaiting_input")
            print("\n=== NEED INPUT ===")
            print(q)
            # 通过 interrupt_handler 读取，与后台线程共享 stdin 不冲突
            user_input = interrupt_handler.get_user_input("\nYour answer> ")
            if user_input is None:  # EOF
                print("\n[run_goal] No interactive stdin available (EOF). Please rerun in a real terminal to answer.")
                break
            user_input = user_input.strip()
            if not user_input:
                print("No input provided; exiting.")
                break

            state.short_term.append({
                "role": "user",
                "content": f"[用户补充信息]\n{user_input}",
            })
            if state.persistence is not None:
                state.persistence.append_short_term(state.short_term[-1])
                state.persistence.checkpoint(state)
            state = agent.run(goal, state=state)

    except Exception as e:
        run_error = e
        if state is not None and state.persistence is not None:
            state.persistence.finish(state, outcome="failed", error=f"{type(e).__name__}: {e}")
    finally:
        interrupt_handler.stop()
        # Remove PID file so the dashboard immediately sees process is gone
        if _pid_file is not None:
            try:
                _pid_file.unlink(missing_ok=True)
            except OSError:
                pass

    if state is not None:
        if state.persistence is None:
            state.persistence = persistence
            state.persistence.start(state)

        if state.meta.get("paused"):
            outcome = "paused"
        elif state.meta.get("timeout"):
            outcome = "failed"
        else:
            outcome = "done"
        state.persistence.finish(state, outcome=outcome, error=None if outcome != "failed" else "timeout")

    # Optional: persist snapshot after run (so long_term is not lost between processes)
    if state is not None and os.environ.get("AUTO_SAVE_SNAPSHOT_ON_EXIT", "0") == "1":
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
    if state is not None:
        print(state.meta.get("final_answer") or "(no final_answer)")
    else:
        print("(no final_answer)")

    print(f"\n[run_goal] run_dir: {run_dir}")
    print(f"[run_goal] raw_memory: {os.environ.get('RAW_MEMORY_PATH')}")
    print(f"[run_goal] scratchpad_copy: {run_dir / 'scratchpad.md'}")

    if run_error is not None:
        raise run_error


if __name__ == "__main__":
    main()
