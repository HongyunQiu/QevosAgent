#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

from agent import Agent
from agent.runtime.persistence import RunPersistence

DEFAULT_TOOLS    = "./agent_tools.json"
DEFAULT_EPISODIC = "./memory_episodic.jsonl"
DEFAULT_CONCEPT  = "./memory_macro.md"
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

    import argparse as _ap
    _parser = _ap.ArgumentParser(description="Run QevosAgent with a goal.")
    _parser.add_argument("goal", nargs="*", help="Goal/task for the agent")
    _parser.add_argument(
        "--nostop", action="store_true",
        help="完成任务后不退出，持续等待下一个目标（持续对话模式）",
    )
    _parser.add_argument(
        "--skills", default="",
        help="逗号分隔的技能名列表（如 coding,data_analysis），或 'all' 加载全部技能",
    )
    _pargs = _parser.parse_args()
    goal   = " ".join(_pargs.goal).strip()
    nostop = _pargs.nostop
    # --skills 优先于 AGENT_SKILLS 环境变量
    _skills_arg = _pargs.skills.strip() or os.environ.get("AGENT_SKILLS", "").strip()
    if _skills_arg:
        os.environ["AGENT_SKILLS"] = _skills_arg
    # Expose the raw user goal (without injected prefixes) for scratchpad seeding.
    os.environ["USER_GOAL"] = goal
    if not goal:
        print("Enter your goal/task, then press Ctrl-D (EOF) to run:\n")
        goal = sys.stdin.read().strip()

    if not goal:
        print("No goal provided.")
        sys.exit(2)

    tools_path    = Path(os.environ.get("AGENT_TOOLS",    DEFAULT_TOOLS))
    episodic_path = Path(os.environ.get("AGENT_EPISODIC", DEFAULT_EPISODIC))
    concept_path  = Path(os.environ.get("AGENT_CONCEPT",  DEFAULT_CONCEPT))

    from agent.tools.standard import get_standard_tools, tool_load_tools, tool_search_episodic
    from agent.core.types_def import AgentState as _AgentState

    # ── (1) 预加载工具（Python 层，不依赖 LLM） ────────────────────────────────
    _preload_state = _AgentState(goal="__preload__", tools=get_standard_tools())
    if tools_path.exists():
        _r = tool_load_tools(_preload_state, str(tools_path))
        if _r.success:
            print(f"[run_goal] tools loaded: {_r.output}")
        else:
            print(f"[run_goal] tools load warning: {_r.error}")
    else:
        print(f"[run_goal] no tools file at {tools_path}, starting fresh")

    # ── (2) 预加载细粒度记忆（最近 20 条 → 格式化为字符串） ────────────────────
    preloaded_long_term: list[str] = []
    if episodic_path.exists():
        _r = tool_search_episodic(_preload_state, str(episodic_path), limit=20)
        if _r.success:
            for _e in _r.output.get("entries", []):
                _date    = (_e.get("ts") or "")[:10]
                _goal_s  = (_e.get("goal") or "")[:60]
                _summary = (_e.get("summary") or "")
                _tags    = ",".join(_e.get("tags") or [])
                _line    = f"[{_date}] {_goal_s} ── {_summary}"
                if _tags:
                    _line += f"  (tags:{_tags})"
                preloaded_long_term.append(_line)
            print(f"[run_goal] episodic memory: {len(preloaded_long_term)} entries loaded")
    else:
        print(f"[run_goal] no episodic file at {episodic_path}, starting fresh")

    # ── (3) 预加载概念记忆 ────────────────────────────────────────────────────
    preloaded_concept = ""
    if concept_path.exists():
        try:
            preloaded_concept = concept_path.read_text(encoding="utf-8").strip()
            print(f"[run_goal] concept memory: {len(preloaded_concept)} chars loaded")
        except Exception as _ce:
            print(f"[run_goal] concept memory load warning: {_ce}")
    else:
        print(f"[run_goal] no concept file at {concept_path}, starting fresh")

    # ── 构建 initial_meta（传递工具配方和修复元数据到 run state） ──────────────
    _META_KEYS = ("evolved_tools", "tool_repair_candidates", "tool_repair_failures", "tool_repair_history")
    initial_meta = {k: v for k, v in _preload_state.meta.items() if k in _META_KEYS}
    if preloaded_concept:
        initial_meta["concept_memory"] = preloaded_concept
    # 把文件路径传入 state.meta，供 episodic 验收门反馈消息使用
    initial_meta["_episodic_path"] = str(episodic_path)
    initial_meta["_concept_path"]  = str(concept_path)
    initial_meta["nostop"]         = nostop  # expose mode flag to agent state

    # ── (4) 预加载高级指导员系统提示词 ────────────────────────────────────────
    advisor_system = ""
    advisor_path = Path("./ADVISOR.md")
    if advisor_path.exists():
        try:
            advisor_system = advisor_path.read_text(encoding="utf-8").strip()
            print(f"[run_goal] advisor: ADVISOR.md loaded ({len(advisor_system)} chars)")
        except Exception as _ae:
            print(f"[run_goal] advisor: failed to load ADVISOR.md: {_ae}")
    else:
        print("[run_goal] advisor: no ADVISOR.md found, senior advisor disabled")
    initial_meta["_advisor_system"]   = advisor_system
    initial_meta["_advisor_log_path"] = str(run_dir / "advisor_log.jsonl")
    initial_meta["_patch_log_path"]   = str(run_dir / "patch_log.jsonl")

    # ── 目标前缀 ──────────────────────────────────────────────────────────────
    # 工具和记忆已由 Python 层预加载，无需 LLM 主动调用恢复命令。
    # 结束时告知 LLM 应调用 append_episodic 记录本次摘要。
    prefix = (
        "工具、细粒度记忆和概念记忆已自动预加载，请直接完成任务。\n\n"
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

    # ── (5) 加载领域技能（SKILLS/*.md）────────────────────────────────────────
    _skills_env = os.environ.get("AGENT_SKILLS", "").strip()
    if _skills_env:
        skills_dir = Path("./SKILLS")
        selected_skills: list[str] = []
        if _skills_env.lower() == "all":
            selected_skills = sorted(p.stem for p in skills_dir.glob("*.md")) if skills_dir.exists() else []
        else:
            selected_skills = [s.strip() for s in _skills_env.split(",") if s.strip()]

        loaded_skills: list[str] = []
        skill_blocks: list[str] = []
        for skill_name in selected_skills:
            skill_path = skills_dir / f"{skill_name}.md"
            if skill_path.exists():
                try:
                    skill_content = skill_path.read_text(encoding="utf-8").strip()
                    skill_blocks.append(skill_content)
                    loaded_skills.append(skill_name)
                except Exception as _se:
                    print(f"[run_goal] skill load warning ({skill_name}): {_se}")
            else:
                print(f"[run_goal] skill not found: {skill_name}")

        if skill_blocks:
            prefix = (
                prefix
                + "【领域技能】以下是本次任务激活的领域专业规范，请遵守：\n\n"
                + "\n\n---\n\n".join(skill_blocks)
                + "\n\n"
            )
            print(f"[run_goal] skills loaded: {loaded_skills}")
        initial_meta["_active_skills"] = loaded_skills

    full_goal = prefix + goal

    agent = Agent(
        backend="openai",
        api_key=os.environ.get("OPENAI_API_KEY"),
        max_iterations=int(os.environ.get("MAX_ITERS", "100")),
        verbose=True,
        long_term=preloaded_long_term,
        extra_tools={k: v for k, v in _preload_state.tools.items()
                     if k not in get_standard_tools()},
        concept_memory=preloaded_concept,
        initial_meta=initial_meta,
    )

    # ── 用户干预处理器：后台线程读取 stdin，"/" 开头为命令，其余为 ask_user 回答 ──
    from agent.runtime.user_interrupt import UserInterruptHandler
    interrupt_handler = UserInterruptHandler()
    agent.hooks.interrupt_handler = interrupt_handler  # 挂载到 hooks，loop.py 会检查

    GREEN = "\033[92m"
    BLUE  = "\033[94m"
    RESET = "\033[0m"
    _nostop_hint = (
        f"  /newtask <目标>  注入新目标（nostop 模式专用）\n"
        f"  [nostop 模式已启用] 任务完成后将持续等待下一个目标。\n"
    ) if nostop else ""
    print(
        f"{BLUE}[提示] Agent 运行期间可随时输入干预命令（以 / 开头）：\n"
        f"  /help   显示所有命令    /stop   停止当前工具\n"
        f"  /exit   退出程序        /inject <消息>  注入上下文\n"
        f"  /status 查看当前状态   /+N  增加 N 次迭代\n"
        f"{_nostop_hint}{RESET}"
    )

    persistence = RunPersistence(run_dir)
    state = None
    run_error = None

    # ── nostop: keys cleared between tasks ───────────────────────────────
    _NOSTOP_RESET_KEYS = (
        "final_answer", "completion_report", "completion_review",
        "acceptance_failures", "_episodic_appended",
        "nostop_idle", "paused", "awaiting_input",
    )

    current_goal = full_goal
    interrupt_handler.start()
    try:
        state = None  # created on first agent.run(); re-used in subsequent rounds

        while True:
            state = agent.run(current_goal, state=state)

            # ── /exit or timeout → stop unconditionally ──────────────────────
            if state.meta.get("user_stopped") or state.meta.get("timeout"):
                break

            # ── paused: ask_user / weak_pass / user_typing_pause / loop_help ─
            if state.meta.get("paused") and state.meta.get("awaiting_input"):

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
                    continue

                # 普通 ask_user / weak_pass 暂停：Agent 提问，等待用户文字回答
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

                # weak_pass 问题明确告知用户回复「完成」即可结束；检测到后直接退出
                if user_input.strip() in ("完成", "done", "finish", "finished", "ok", "好", "好的", "不用了"):
                    state.meta.pop("paused", None)
                    state.meta.pop("awaiting_input", None)
                    print("[run_goal] 用户确认完成，退出。")
                    break

                state.short_term.append({
                    "role": "user",
                    "content": f"[用户补充信息]\n{user_input}",
                })
                if state.persistence is not None:
                    state.persistence.append_short_term(state.short_term[-1])
                    state.persistence.checkpoint(state)
                state.meta.pop("paused", None)
                state.meta.pop("awaiting_input", None)
                continue

            # ── 正常完成（pass verdict，no paused flag）──────────────────────
            if not nostop:
                break  # 原有行为：直接退出

            # ── nostop idle：等待下一个目标 ──────────────────────────────────
            _final     = state.meta.get("final_answer") or ""
            _round_n   = state.meta.get("_nostop_round", 1)
            # session_answers.md 记录的目标使用原始用户输入（不含 prefix），可读性更佳
            _raw_goal  = state.meta.get("_task_desc") or current_goal
            print(f"\n{GREEN}{'='*60}{RESET}")
            print(f"{GREEN}[nostop] ✅ 第 {_round_n} 轮任务完成。{RESET}")
            if _final:
                _preview = _final[:300] + ("…" if len(_final) > 300 else "")
                print(f"{GREEN}{_preview}{RESET}")
            print(f"{GREEN}{'='*60}{RESET}")

            # 追加 session_answers.md 记录此轮成果
            _pers = state.persistence
            if _pers is not None and _final:
                try:
                    from datetime import datetime as _dt
                    _sa_path = _pers.run_dir / "session_answers.md"
                    _block = (
                        f"\n## Round {_round_n} — {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"**Goal:** {_raw_goal[:200]}\n\n"
                        f"{_final}\n\n---"
                    )
                    with _sa_path.open("a", encoding="utf-8") as _f:
                        _f.write(_block)
                except Exception:
                    pass

            # 写 idle 状态供看板感知
            state.meta["nostop_idle"]    = True
            state.meta["awaiting_input"] = (
                "任务完成，进入持续对话模式。请输入下一个目标，或 /exit 退出："
            )
            if state.persistence is not None:
                state.persistence.checkpoint(state, status="idle")

            print(
                f"\n{BLUE}[nostop] 请输入下一个目标（/exit 退出）：{RESET}",
                end="", flush=True,
            )
            next_input = interrupt_handler.get_user_input("")
            if next_input is None or not next_input.strip():
                break
            if next_input.strip().lower() in ("/exit", "exit"):
                break

            _raw_next_goal = next_input.strip()

            # 清除上一轮的一次性完成状态，开始新一轮
            for _k in _NOSTOP_RESET_KEYS:
                state.meta.pop(_k, None)
            state.meta["_nostop_round"] = _round_n + 1

            # ── 重置对话上下文，防止上轮历史（可能 200+ 条）污染新一轮 ────────────
            # state.short_term 是发给 LLM 的对话历史，跨轮保留会导致：
            #   1. 上下文爆炸（tokens 激增）
            #   2. LLM 复用上一轮 final_answer（而非重新执行新目标）
            #   3. 验收/acceptance 状态混淆
            state.short_term.clear()
            state.meta["scratchpad"]     = f"任务描述:\n{_raw_next_goal}\n"
            state.meta["_task_desc"]     = _raw_next_goal
            state.meta.pop("_loop_warn_counts", None)
            state.meta.pop("_call_sig_history", None)
            state.iteration = 0   # 重置迭代计数，新一轮可使用完整 max_iterations
            os.environ["USER_GOAL"] = _raw_next_goal

            # 重建完整目标（保留 prefix：工具已加载提示 + AGENTS.md 规范仍然有效）
            current_goal = prefix + _raw_next_goal

            # loop.py 的 else 分支（state 非 None）不会自动注入"请完成以下目标"消息，
            # 此处手动补充，确保 LLM 第一条消息就是新一轮目标
            state.short_term.append({
                "role": "user",
                "content": f"请完成以下目标：\n\n{current_goal}",
            })
            # continue → 回到 while True 顶部，以新 goal 继续

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

    # Auto-save tools on exit（Python 层直接调用，不依赖 LLM）
    if state is not None and os.environ.get("AUTO_SAVE_SNAPSHOT_ON_EXIT", "0") == "1":
        from agent.tools.standard import tool_save_tools
        try:
            _r = tool_save_tools(state, str(tools_path))
            if _r.success:
                print(f"\n[run_goal] tools saved: {_r.output}")
            else:
                print(f"\n[run_goal] tools save warning: {_r.error}")
        except Exception as _e:
            print(f"\n[run_goal] tools save failed: {_e}")

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
