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

    if not os.environ.get("OPENAI_BASE_URL"):
        raise ValueError("缺少 OPENAI_BASE_URL，请在 .env 中设置。")

    os.environ.setdefault("OPENAI_API_KEY", "local")

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
    _team_api_instance = None  # 提前初始化，确保 finally 块安全访问
    ensure_env_defaults()
    from agent.i18n import t  # import after env defaults so QEVOS_LANG from .env is visible
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
        help="Do not exit after task completion; wait for the next goal (continuous dialogue mode)",
    )
    _parser.add_argument(
        "--skills", default="",
        help="Comma-separated skill names (e.g. coding,data_analysis), or 'all' to load all skills",
    )
    _parser.add_argument(
        "--agents-profile", default="", dest="agents_profile", metavar="AGENTS_*.md",
        help="Full filename of the conventions file to use, e.g. AGENTS_WIN_EN.md. Must live in the repo root and match AGENTS.md or AGENTS_*.md. Overrides (not appends to) AGENTS.md. Falls back to AGENTS.md if missing.",
    )
    _parser.add_argument(
        "--advisor-profile", default="", dest="advisor_profile", metavar="ADVISOR_*.md",
        help="Full filename of the advisor file to use, e.g. ADVISOR_CRITIC.md. Must live in the repo root and match ADVISOR.md or ADVISOR_*.md. Overrides ADVISOR.md. Falls back to ADVISOR.md if missing.",
    )
    _pargs = _parser.parse_args()
    goal   = " ".join(_pargs.goal).strip()
    nostop = _pargs.nostop
    # --skills 优先于 AGENT_SKILLS 环境变量
    _skills_arg = _pargs.skills.strip() or os.environ.get("AGENT_SKILLS", "").strip()
    if _skills_arg:
        os.environ["AGENT_SKILLS"] = _skills_arg
    # CLI 优先于环境变量
    _agents_profile  = _pargs.agents_profile.strip()  or os.environ.get("AGENTS_PROFILE",  "").strip()
    _advisor_profile = _pargs.advisor_profile.strip() or os.environ.get("ADVISOR_PROFILE", "").strip()
    if _agents_profile:
        os.environ["AGENTS_PROFILE"] = _agents_profile
    if _advisor_profile:
        os.environ["ADVISOR_PROFILE"] = _advisor_profile

    def _validate_profile_filename(name: str, base: str) -> bool:
        """Allow only '<BASE>.md' or '<BASE>_<suffix>.md' as a bare filename
        (no path separators, no parent refs). Returns True if valid.
        """
        if not name:
            return True
        if "/" in name or "\\" in name or ".." in name:
            return False
        if name == f"{base}.md":
            return True
        if name.startswith(f"{base}_") and name.endswith(".md") and len(name) > len(base) + 4:
            return True
        return False

    if _agents_profile and not _validate_profile_filename(_agents_profile, "AGENTS"):
        print(f"[run_goal] conventions: WARNING --agents-profile '{_agents_profile}' is not a valid AGENTS.md / AGENTS_*.md filename, ignoring")
        _agents_profile = ""
    if _advisor_profile and not _validate_profile_filename(_advisor_profile, "ADVISOR"):
        print(f"[run_goal] advisor: WARNING --advisor-profile '{_advisor_profile}' is not a valid ADVISOR.md / ADVISOR_*.md filename, ignoring")
        _advisor_profile = ""
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
    initial_meta["_user_goal"]     = goal    # raw user input, used for run summary display

    # ── (4) 预加载高级指导员系统提示词 ────────────────────────────────────────
    # Override 语义：--advisor-profile <name> 选中 ADVISOR_<name>.md 替换 ADVISOR.md；
    # profile 文件不存在则 fallback 到 ADVISOR.md。
    advisor_system = ""
    _adv_chosen_path: Path | None = None
    if _advisor_profile:
        _cand = Path(f"./{_advisor_profile}")
        if _cand.exists():
            _adv_chosen_path = _cand
        else:
            print(f"[run_goal] advisor: WARNING {_advisor_profile} not found, falling back to ADVISOR.md")
    if _adv_chosen_path is None:
        _base_adv = Path("./ADVISOR.md")
        if _base_adv.exists():
            _adv_chosen_path = _base_adv
    if _adv_chosen_path is not None:
        try:
            advisor_system = _adv_chosen_path.read_text(encoding="utf-8").strip()
            print(f"[run_goal] advisor: {_adv_chosen_path.name} loaded ({len(advisor_system)} chars)")
        except Exception as _ae:
            print(f"[run_goal] advisor: failed to load {_adv_chosen_path.name}: {_ae}")
    if not advisor_system:
        print("[run_goal] advisor: no advisor content loaded, senior advisor disabled")
    initial_meta["_advisor_system"]   = advisor_system
    initial_meta["_advisor_log_path"] = str(run_dir / "advisor_log.jsonl")
    initial_meta["_patch_log_path"]   = str(run_dir / "patch_log.jsonl")

    # ── 目标前缀 ──────────────────────────────────────────────────────────────
    # 工具和记忆已由 Python 层预加载，无需 LLM 主动调用恢复命令。
    # 结束时告知 LLM 应调用 append_episodic 记录本次摘要。
    prefix = t("rg.prefix_preloaded")

    # Load repo conventions (OpenClaw-style) if present.
    # Keep it short; it's a hard constraint but should not bloat prompts.
    # 基础 AGENTS.md + （可选）按 --profile 追加 AGENTS_<name>.md
    # Override 语义：--agents-profile <name> 选中 AGENTS_<name>.md 替换 AGENTS.md；
    # profile 文件不存在则 fallback 到 AGENTS.md。两者都不存在则 warn。
    conventions = ""
    _conv_chosen_path: Path | None = None
    if _agents_profile:
        _cand = Path(f"./{_agents_profile}")
        if _cand.exists():
            _conv_chosen_path = _cand
        else:
            print(f"[run_goal] conventions: WARNING {_agents_profile} not found, falling back to AGENTS.md")
    if _conv_chosen_path is None:
        _base_conv_path = Path("./AGENTS.md")
        if _base_conv_path.exists():
            _conv_chosen_path = _base_conv_path
    if _conv_chosen_path is not None:
        try:
            conventions = _conv_chosen_path.read_text(encoding="utf-8").strip()
            if conventions:
                print(f"[run_goal] conventions: {_conv_chosen_path.name} loaded ({len(conventions)} chars)")
            else:
                print(f"[run_goal] conventions: WARNING {_conv_chosen_path.name} exists but is empty")
        except Exception as _ce:
            print(f"[run_goal] conventions: WARNING failed to read {_conv_chosen_path.name}: {_ce}")
    if not conventions:
        # AGENTS.md 内含 agent 通用运行规则，缺失会显著影响行为。
        print("[run_goal] conventions: WARNING no AGENTS.md or AGENTS_<profile>.md loaded — agent will run WITHOUT any repo conventions, runtime behavior may degrade")

    # ── 将 AGENTS.md 注入 advisor system（cacheable）──────────────────────────
    # advisor 现在能看到项目规范，给出的具体指导不会与 AGENTS.md 冲突。
    # 拼到 advisor_system 末尾、不进 user 段，整段在一次 run 内不变 → 可缓存。
    if conventions and initial_meta.get("_advisor_system"):
        try:
            _adv_base = initial_meta["_advisor_system"]
            _conv_header = t("advisor.sys.conv_header")
            _read_rules  = t("advisor.sys.read_rules")
            initial_meta["_advisor_system"] = (
                _adv_base.rstrip() + _conv_header + conventions.strip() + _read_rules
            )
            print(f"[run_goal] advisor: AGENTS.md merged into advisor_system "
                  f"({len(initial_meta['_advisor_system'])} chars total)")
        except Exception as _ae:
            print(f"[run_goal] advisor: failed to merge AGENTS.md: {_ae}")

    if conventions:
        prefix = (
            prefix
            + t("rg.agents_md_rule")
            + t("rg.run_dir_with_agents", run_dir=run_dir)
            + conventions
            + "\n\n"
        )
    else:
        prefix = prefix + t("rg.run_dir_hint", run_dir=run_dir)

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
                + t("rg.skills_header")
                + "\n\n---\n\n".join(skill_blocks)
                + "\n\n"
            )
            print(f"[run_goal] skills loaded: {loaded_skills}")
        initial_meta["_active_skills"] = loaded_skills

    full_goal = goal + "\n\n" + prefix

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

    # ── 加载团队协作工具（始终加载，行为由运行时拓扑节点码决定）────────────────
    from agent.team.tools import get_team_tools
    agent.tools.update(get_team_tools())

    # ── 用户干预处理器：后台线程读取 stdin，"/" 开头为命令，其余为 ask_user 回答 ──
    from agent.runtime.user_interrupt import UserInterruptHandler
    interrupt_handler = UserInterruptHandler()
    agent.hooks.interrupt_handler = interrupt_handler  # 挂载到 hooks，loop.py 会检查

    # ── Team API：始终启动，端口由 TEAM_PORT 环境变量控制（默认 9100）────────────
    from agent.team.api import TeamApiServer
    _team_port = int(os.environ.get("TEAM_PORT", "9100"))
    _team_api_instance = TeamApiServer(
        port=_team_port,
        run_dir=run_dir,
        interrupt_handler=interrupt_handler,
    )

    GREEN = "\033[92m"
    BLUE  = "\033[94m"
    RESET = "\033[0m"
    _nostop_hint = t("rg.hint_nostop") if nostop else ""
    print(f"{BLUE}{t('rg.hint_header')}{_nostop_hint}{RESET}")

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

    # ── Team API：在 interrupt_handler 启动后立即启动，注入 initial_meta ────────
    _team_api_instance.start()
    agent.initial_meta["_team_api"] = _team_api_instance

    try:
        state = None  # created on first agent.run(); re-used in subsequent rounds

        while True:
            state = agent.run(current_goal, state=state)

            # Team API 实例同步到 state.meta（state 在第一次 run 后才存在）
            state.meta["_team_api"] = _team_api_instance

            # ── /exit or timeout → stop unconditionally ──────────────────────
            if state.meta.get("user_stopped") or state.meta.get("timeout"):
                break

            # ── paused: ask_user / weak_pass / user_typing_pause / loop_help ─
            if state.meta.get("paused") and state.meta.get("awaiting_input"):

                if state.meta.get("user_typing_pause"):
                    # 用户按下 / 触发的暂停：等待完整命令或纯文本（自动包装为 /inject）
                    print(
                        f"\n{BLUE}{t('rg.intervention_header')}{RESET}\n"
                        f"{BLUE}{t('rg.intervention_prompt')}{RESET}"
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
                        print(f"{BLUE}{t('rg.intervention_timeout')}{RESET}")
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

                # ── [组网模式] ask_user 路由到上游节点 ──────────────────────
                _upstream_routed = False
                _tapi = state.meta.get("_team_api")
                _topo = _tapi.topology_node if _tapi else None
                _up_url = (_topo or {}).get("upstream_url", "")
                if _tapi and _up_url:
                    import uuid as _uuid
                    import urllib.request as _ureq
                    _q_content = state.meta.get("awaiting_input", "")
                    _node_id   = (_topo or {}).get("id", "unknown")
                    _qid = str(_uuid.uuid4())
                    _post_ok = False
                    try:
                        _body = json.dumps({
                            "question_id": _qid,
                            "from_node_id": _node_id,
                            "from_node_url": f"http://localhost:{_team_port}",
                            "content": _q_content,
                        }, ensure_ascii=False).encode("utf-8")
                        _req = _ureq.Request(
                            f"{_up_url}/agent/question",
                            data=_body,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        _ureq.urlopen(_req, timeout=5).read()
                        _post_ok = True
                        print(f"\n[team] 已向上游节点提问 (qid={_qid[:8]}…)")
                    except Exception as _pe:
                        print(f"\n[team] 无法联系上游节点 ({_pe})，转为等待用户输入")
                    if _post_ok:
                        try:
                            _answer = _tapi.wait_for_answer(_qid, upstream_url=_up_url)
                            state.short_term.append({
                                "role": "user",
                                "content": t("marker.user_info", content=f"[上游节点回复] {_answer}"),
                            })
                            if state.persistence is not None:
                                state.persistence.append_short_term(state.short_term[-1])
                                state.persistence.checkpoint(state)
                            state.meta.pop("paused", None)
                            state.meta.pop("awaiting_input", None)
                            _upstream_routed = True
                        except RuntimeError as _offline_err:
                            print(f"\n[team] {_offline_err}，升级为直接等待用户输入")

                if _upstream_routed:
                    continue

                # 普通 ask_user / weak_pass 暂停：Agent 提问，等待用户文字回答
                q = state.meta.get("awaiting_input")
                print("\n=== NEED INPUT ===")
                print(q)
                # 同时轮询两个队列：
                #   _cmd_queue  — web 看板的 /inject 消息走这里
                #   _input_queue — CLI stdin 直接输入走这里
                # 不能只用 get_user_input()（阻塞 _input_queue），否则 web 模式下
                # /inject 永远到不了 _input_queue，造成永久死锁。
                import time as _rt
                user_input = None
                _ask_stop = False
                while True:
                    _cmd = interrupt_handler.poll_command()
                    if _cmd is not None and _cmd != "/__pause__":
                        if _cmd.startswith("/inject "):
                            user_input = _cmd[len("/inject "):].strip()
                            break
                        else:
                            _r = interrupt_handler.process_command(_cmd, state)
                            if _r == "stop":
                                state.meta["user_stopped"] = True
                                _ask_stop = True
                                break
                        continue
                    try:
                        import queue as _q
                        _text = interrupt_handler._input_queue.get_nowait()
                        if _text is not None:
                            user_input = _text
                            break
                        # None = EOF from CLI
                        _ask_stop = True
                        break
                    except _q.Empty:
                        pass
                    _rt.sleep(0.1)
                if _ask_stop:
                    break
                if user_input is None:
                    print("\n[run_goal] No input received; exiting.")
                    break
                user_input = user_input.strip()
                if not user_input:
                    print("No input provided; exiting.")
                    break

                # weak_pass 问题明确告知用户回复「完成」即可结束；检测到后直接退出
                if user_input.strip() in ("完成", "done", "finish", "finished", "ok", "好", "好的", "不用了"):
                    state.meta.pop("paused", None)
                    state.meta.pop("awaiting_input", None)
                    print(t("rg.user_confirmed"))
                    break

                state.short_term.append({
                    "role": "user",
                    "content": t("marker.user_info", content=user_input),
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
            print(f"{GREEN}{t('rg.nostop_done', n=_round_n)}{RESET}")
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
            state.meta["awaiting_input"] = t("rg.nostop_await")
            if state.persistence is not None:
                state.persistence.checkpoint(state, status="idle")

            print(
                f"\n{BLUE}{t('rg.nostop_prompt')}{RESET}",
                end="", flush=True,
            )
            # 同时轮询两个队列：
            #   _cmd_queue  — 看板 /inject 和 view.html 聊天消息走这里
            #   _input_queue — stdin / /newtask / 看板 nostop-idle 专用路径走这里
            # 只用 get_user_input() 会错过 _cmd_queue，导致 web 端输入无法唤醒 Agent。
            import time as _ni_rt
            import queue as _ni_q
            next_input = None
            _ni_stop = False
            while True:
                _ni_cmd = interrupt_handler.poll_command()
                if _ni_cmd is not None and _ni_cmd != "/__pause__":
                    if _ni_cmd.startswith("/inject "):
                        _inject_content = _ni_cmd[len("/inject "):].strip()
                        if _inject_content:
                            next_input = _inject_content
                            break
                        # 空 inject，继续等待
                    elif _ni_cmd.lower() in ("/exit", "/quit"):
                        _ni_stop = True
                        break
                    else:
                        _r = interrupt_handler.process_command(_ni_cmd, state)
                        if _r == "stop":
                            _ni_stop = True
                            break
                    continue
                try:
                    _ni_text = interrupt_handler._input_queue.get_nowait()
                    if _ni_text is None:
                        _ni_stop = True
                        break
                    next_input = _ni_text
                    break
                except _ni_q.Empty:
                    pass
                _ni_rt.sleep(0.1)
            if _ni_stop or not next_input or not next_input.strip():
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
            state.meta["scratchpad"]     = t("rg.scratchpad_init", goal=_raw_next_goal)
            state.meta["_task_desc"]     = _raw_next_goal
            state.meta["_user_goal"]     = _raw_next_goal
            state.meta.pop("_loop_warn_counts", None)
            state.meta.pop("_call_sig_history", None)
            state.meta.pop("_advisor_last_iter", None)
            state.meta.pop("_advisor_tried_for_loop", None)
            state.meta.pop("_loop_advisor_pending", None)
            state.iteration = 0   # 重置迭代计数，新一轮可使用完整 max_iterations
            os.environ["USER_GOAL"] = _raw_next_goal

            # 重建完整目标（保留 prefix：工具已加载提示 + AGENTS.md 规范仍然有效）
            current_goal = prefix + _raw_next_goal

            # loop.py 的 else 分支（state 非 None）不会自动注入"请完成以下目标"消息，
            # 此处手动补充，确保 LLM 第一条消息就是新一轮目标
            state.short_term.append({
                "role": "user",
                "content": t("rg.next_goal_msg", goal=current_goal),
            })
            # continue → 回到 while True 顶部，以新 goal 继续

    except Exception as e:
        run_error = e
    finally:
        if _team_api_instance is not None:
            _team_api_instance.stop()
        interrupt_handler.stop()

        # Write the final status BEFORE removing the PID file so the dashboard
        # never sees "process dead + status=running" in the same poll cycle.
        if state is not None:
            if state.persistence is None:
                state.persistence = persistence
                state.persistence.start(state)

            if run_error is not None:
                _finish_outcome = "failed"
                _finish_error   = f"{type(run_error).__name__}: {run_error}"
            elif state.meta.get("paused"):
                _finish_outcome = "paused"
                _finish_error   = None
            elif state.meta.get("timeout"):
                _finish_outcome = "failed"
                _finish_error   = "timeout"
            else:
                _finish_outcome = "done"
                _finish_error   = None
            state.persistence.finish(state, outcome=_finish_outcome, error=_finish_error)

        # If web_notify was used this session, write a final web_chat message so
        # view.html immediately knows the agent has exited (before PID-based detection).
        if state is not None and _finish_outcome in ("done", "failed"):
            try:
                import time as _wct
                _wc_fp = state.persistence.run_dir / "web_chat.jsonl"
                if _wc_fp.exists():
                    _lang = os.environ.get("QEVOS_LANG", "zh")
                    if _finish_outcome == "done":
                        _wc_msg = (
                            "✓ 任务已完成，会话结束。"
                            if _lang.startswith("zh") else
                            "✓ Task complete — session ended."
                        )
                    else:
                        _wc_msg = (
                            "✗ Agent 已退出（出现错误）。"
                            if _lang.startswith("zh") else
                            "✗ Agent exited (error occurred)."
                        )
                    _wc_record = json.dumps(
                        {"role": "agent", "message": _wc_msg, "display_id": "*",
                         "ts": _wct.time()},
                        ensure_ascii=False,
                    )
                    with open(_wc_fp, "a", encoding="utf-8") as _wcf:
                        _wcf.write(_wc_record + "\n")
            except Exception:
                pass

        # Remove PID file so the dashboard immediately sees process is gone
        if _pid_file is not None:
            try:
                _pid_file.unlink(missing_ok=True)
            except OSError:
                pass

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
