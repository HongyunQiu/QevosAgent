"""
用户干预处理器
==============
在 Agent 执行过程中，允许用户通过 stdin 输入 "/" 开头的命令进行干预。

TTY 模式（交互终端）：
  - 逐字符读取，一旦检测到 "/" 作为行首字符立即反馈并标记暂停请求。
  - Agent 在当前工具调用结束后暂停，用户可继续输入完整命令。

支持命令：
  /help              立即显示帮助（不等工具结束）
  /stop              当前工具结束后停止
  /inject <消息>     注入消息到 Agent 上下文，下轮 LLM 可感知
  /status            显示当前迭代号和草稿本摘要（工具结束后）

非 "/" 文本在 Agent 运行期间路由到 user_input 队列，供 ask_user 恢复使用。
"""

import os
import queue
import sys
import threading
from typing import Optional

BLUE  = "\033[94m"
RESET = "\033[0m"

HELP_TEXT = """\
[用户干预命令] - 输入 / 即可触发：
  /help              立即显示此帮助（不等当前工具结束）
  /stop              终止当前正在执行的工具，Agent 继续下一步
  /exit              退出整个 Agent 程序
  /inject <消息>     将消息注入 Agent 上下文，下轮 LLM 可感知
  /compress [N]      下次 LLM 调用前压缩上下文（保留最近 N 条，默认 8）
  /status            显示当前状态：迭代号、正在执行的工具、草稿本
  /log [N]           显示最近 N 条执行记录（默认 5 条）
  /+N                增加 N 次最大迭代次数（例如 /+50）
  （/status 和 /log 在工具执行中也会立即响应）
提示: 只需输入 / 即可暂停，完整命令后按回车生效。
"""

# 可在后台线程立即处理的命令（不需要 state）
_IMMEDIATE_CMDS = {"/help"}
# 在工具执行轮询期间也可安全处理的只读命令
_READONLY_CMDS = {"/status", "/log"}


class UserInterruptHandler:
    """后台读取 stdin，支持逐字符 TTY 模式和行模式 pipe 模式。

    属性：
      pause_requested  当用户按下 / 作为行首时立即置为 True，
                       表示"用户正在输入干预命令"。主循环检测到后
                       在当前工具结束时暂停，给用户输入剩余命令的时间。
    """

    def __init__(self):
        self._cmd_queue: queue.Queue[str] = queue.Queue()
        self._input_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # 立即标志：一旦 '/' 被按下就为 True，直到完整命令提交
        self.pause_requested: bool = False
        # 强制停止标志：用户输入 /stop 后立即置 True，
        # 供工具执行轮询线程发现时主动放弃等待
        self.force_stop: bool = False
        self._is_tty: bool = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        target = self._read_loop_tty if self._is_tty else self._read_loop_pipe
        self._thread = threading.Thread(target=target, daemon=True, name="user-interrupt")
        self._thread.start()
        # Optional: watch for commands injected by the web dashboard
        if os.environ.get("RUN_DIR"):
            web_thread = threading.Thread(
                target=self._web_cmd_watcher, daemon=True, name="web-cmd-watcher"
            )
            web_thread.start()

    def stop(self) -> None:
        self._running = False
        self._input_queue.put(None)  # 解除 get_user_input 阻塞

    def _web_cmd_watcher(self) -> None:
        """Watch {RUN_DIR}/web_cmd.txt for commands injected by the web dashboard.

        Decoupled: if RUN_DIR is unset or the file never appears, this is a no-op.
        The dashboard writes the file; this thread reads, processes, then deletes it.
        """
        import time as _time

        run_dir = os.environ.get("RUN_DIR", "")
        if not run_dir:
            return
        cmd_file = os.path.join(run_dir, "web_cmd.txt")
        while self._running:
            try:
                if os.path.exists(cmd_file):
                    with open(cmd_file, "r", encoding="utf-8") as f:
                        cmd = f.read().strip()
                    try:
                        os.remove(cmd_file)
                    except OSError:
                        pass
                    if cmd:
                        print(f"\n{BLUE}[Web看板] 注入命令: {cmd}{RESET}", flush=True)
                        self._finish_line(cmd)
            except Exception:
                pass
            _time.sleep(0.5)

    # ── 后台读取线程 ──────────────────────────────────────────────────────────

    def _read_loop_tty(self) -> None:
        """TTY 模式路由。

        Windows: SetConsoleMode + ReadConsoleW 逐字符读取。
          - 不依赖 msvcrt.kbhit()（在 Windows Terminal/ConPTY 中不可用）
          - ReadConsoleW 阻塞等待单个 Unicode 字符，支持中文 IME
          - 若 Console API 不可用则降级为 readline() 行模式
        Unix: setcbreak + read(1) 逐字符读取，'/' 立即感知。
        """
        if os.name == "nt":
            self._read_loop_tty_win()
        else:
            self._read_loop_tty_unix()

    def _read_loop_tty_win(self) -> None:
        """Windows TTY：SetConsoleMode + ReadConsoleW 逐字符读取。"""
        import ctypes
        import ctypes.wintypes as _wt

        kernel32   = ctypes.windll.kernel32
        ENABLE_LINE_INPUT  = 0x0002
        ENABLE_ECHO_INPUT  = 0x0004
        STD_INPUT_HANDLE   = -10

        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode   = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 非真实控制台（管道/重定向），降级
            self._read_loop_pipe()
            return

        old_mode = mode.value
        new_mode = old_mode & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
        if not kernel32.SetConsoleMode(handle, ctypes.c_ulong(new_mode)):
            self._read_loop_pipe()
            return

        try:
            buf: list[str] = []
            wchar_buf   = ctypes.create_unicode_buffer(2)
            chars_read  = ctypes.c_ulong(0)
            while self._running:
                # ReadConsoleW：阻塞读取 1 个 Unicode 字符（支持 CJK/IME）
                ok = kernel32.ReadConsoleW(
                    handle,
                    wchar_buf,
                    ctypes.c_ulong(1),
                    ctypes.byref(chars_read),
                    None,
                )
                if not ok or chars_read.value == 0:
                    self._input_queue.put(None)
                    break
                ch  = wchar_buf.value
                if ch:
                    buf = self._handle_char_win(ch, buf)
        finally:
            kernel32.SetConsoleMode(handle, ctypes.c_ulong(old_mode))

    def _handle_char_win(self, ch: str, buf: list) -> list:
        """Windows TTY 单字符处理（手动回显），返回更新后的缓冲区。"""
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._finish_line("".join(buf))
            return []
        if ch == "\x08":  # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            return buf
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if ord(ch) < 32:  # 其他控制字符忽略
            return buf
        # 普通可打印字符（含 CJK）
        if not buf and ch == "/":
            self._on_slash_pressed()
        buf.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()
        return buf

    def _read_loop_tty_unix(self) -> None:
        """Unix TTY：setcbreak 逐字符读取，'/' 立即感知。"""
        try:
            import termios
            import tty
        except ImportError:
            self._read_loop_pipe()
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            buf: list[str] = []
            while self._running:
                ch = sys.stdin.read(1)
                if not ch:
                    self._input_queue.put(None)
                    break
                buf = self._handle_char_unix(ch, buf)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _handle_char_unix(self, ch: str, buf: list) -> list:
        """Unix TTY 单字符处理，返回更新后的缓冲区。"""
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._finish_line("".join(buf))
            return []
        if ch in ("\x08", "\x7f"):  # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            return buf
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if not ch.isprintable():
            return buf
        # 普通可打印字符
        if not buf and ch == "/":
            self._on_slash_pressed()
        buf.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()
        return buf

    def _read_loop_pipe(self) -> None:
        """Pipe/非 TTY 模式：逐行读取（无法立即感知 /）。"""
        while self._running:
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                self._input_queue.put(None)
                break
            self._finish_line(line.rstrip("\n"))

    # ── 核心分发逻辑 ──────────────────────────────────────────────────────────

    def _on_slash_pressed(self) -> None:
        """用户按下 / 作为行首时立即调用。

        立即向命令队列投入 /__pause__ 哨兵，确保主循环在当前迭代结束后
        一定能读到暂停请求——即使 _finish_line 在 pause_requested 被检查
        前就已经将其清除，哨兵也会留在队列里保证暂停生效。
        """
        if self.pause_requested:
            return  # 已经在等待中，避免重复投哨兵
        self.pause_requested = True
        self._cmd_queue.put("/__pause__")   # 哨兵：确保主循环收到暂停信号
        print(
            f"\n{BLUE}[干预] 检测到 /，Agent 将在当前操作结束后暂停。"
            f"请输入命令后回车，或直接回车显示帮助：{RESET}",
            flush=True,
        )

    def _finish_line(self, line: str) -> None:
        """用户按下回车后调用，分发完整行。"""
        line = line.strip()

        if not line.startswith("/"):
            # 普通文本 → 路由给 ask_user 恢复流程
            self.pause_requested = False
            self._input_queue.put(line)
            return

        # 仅输入 "/" 后直接回车 → 默认显示帮助
        if line == "/":
            line = "/help"

        self.pause_requested = False  # 完整命令已提交，清除立即标志
        parts = line.split(None, 1)
        name = parts[0].lower()

        if name in _IMMEDIATE_CMDS:
            self._handle_immediate(name)
        else:
            # /stop：仅终止当前工具（force_stop），Agent 继续执行
            # /exit /quit：退出整个程序，不需要 force_stop
            if name == "/stop":
                self.force_stop = True
            self._ack_deferred(name)
            self._cmd_queue.put(line)

    def _handle_immediate(self, name: str) -> None:
        """在后台线程立即执行的命令（不依赖 state）。"""
        if name == "/help":
            print(f"\n{BLUE}{HELP_TEXT}{RESET}", flush=True)

    def _ack_deferred(self, name: str) -> None:
        """对延迟命令给出即时回执。"""
        print(
            f"\n{BLUE}[用户干预] 已收到 {name}，将在当前工具调用结束后生效。{RESET}",
            flush=True,
        )

    # ── 供主循环和 run_goal.py 调用 ───────────────────────────────────────────

    def poll_command(self) -> Optional[str]:
        """非阻塞：返回下一条延迟命令，无则返回 None。"""
        try:
            return self._cmd_queue.get_nowait()
        except queue.Empty:
            return None

    def wait_command(self, timeout: float = 0.1) -> Optional[str]:
        """阻塞最多 timeout 秒等待一条命令，无则返回 None。"""
        try:
            return self._cmd_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_user_input(self, prompt: str = "") -> Optional[str]:
        """阻塞：等待用户输入非命令文本（替代 input()）。返回 None 表示 EOF。"""
        if prompt:
            print(prompt, end="", flush=True)
        return self._input_queue.get()

    # ── 命令解析（在主循环迭代边界或暂停模式下调用） ─────────────────────────

    def process_command(self, cmd: str, state) -> str:
        """解析并处理一条延迟命令。

        返回: "continue" / "stop"
        """
        parts = cmd.strip().split(None, 1)
        name  = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""

        if name == "/__pause__":
            # 内部哨兵：通知主循环暂停，无需打印任何消息
            return "pause"

        if name == "/stop":
            # 仅取消当前工具（force_stop 已在 _finish_line 里设置）
            # 工具被放弃后 Agent 会继续下一步，无需让主循环 break
            print(
                f"\n{BLUE}[用户干预] /stop 已生效：当前工具将被终止，Agent 继续执行。"
                f"（如需退出程序，请输入 /exit）{RESET}",
                flush=True,
            )
            return "continue"

        if name in ("/exit", "/quit"):
            print(f"\n{BLUE}[用户干预] /exit：Agent 即将退出。{RESET}", flush=True)
            return "stop"

        if name == "/inject":
            if not arg:
                print(f"\n{BLUE}[用户干预] 用法: /inject <消息内容>{RESET}", flush=True)
                return "continue"
            state.short_term.append({
                "role": "user",
                "content": f"[用户干预注入]\n{arg}",
            })
            try:
                persistence = getattr(state, "persistence", None)
                if persistence is not None:
                    persistence.append_short_term(state.short_term[-1])
            except Exception:
                pass
            print(f"\n{BLUE}[用户干预] 消息已注入，下轮 LLM 可感知。{RESET}", flush=True)
            return "continue"

        if name == "/compress":
            try:
                keep = int(arg) if arg.strip() else 8
                keep = max(2, keep)  # 至少保留 2 条，防止过度压缩
            except ValueError:
                keep = 8
            before = len(state.short_term)
            state.meta["_compress_requested"] = keep
            print(
                f"\n{BLUE}[压缩] 已标记：下次 LLM 调用前将压缩上下文，"
                f"保留最近 {keep} 条（当前共 {before} 条）。{RESET}",
                flush=True,
            )
            return "continue"

        if name == "/status":
            _print_status(state)
            return "continue"

        if name == "/log":
            try:
                n = int(arg) if arg.strip() else 5
            except ValueError:
                n = 5
            _print_log(state, n)
            return "continue"

        if name.startswith("/+"):
            suffix = name[2:]
            if suffix.isdigit() and int(suffix) > 0:
                n = int(suffix)
                state.meta["_add_iterations"] = state.meta.get("_add_iterations", 0) + n
                print(
                    f"\n{BLUE}[用户干预] 已增加 {n} 次迭代，"
                    f"累计待增加: {state.meta['_add_iterations']} 次。{RESET}",
                    flush=True,
                )
                return "continue"
            else:
                print(
                    f"\n{BLUE}[用户干预] 用法: /+<正整数>，例如 /+50{RESET}",
                    flush=True,
                )
                return "continue"

        print(
            f"\n{BLUE}[用户干预] 未知命令: {name}。输入 /help 查看可用命令。{RESET}",
            flush=True,
        )
        return "continue"


# ── 只读状态展示（可在任何线程安全调用） ─────────────────────────────────────

def _print_status(state) -> None:
    """打印 Agent 当前运行状态（/status 命令）。"""
    import time as _time

    CYAN  = "\033[96m"
    GRAY  = "\033[90m"
    RESET = "\033[0m"

    lines = [f"\n{CYAN}{'─'*56}{RESET}"]
    lines.append(
        f"{CYAN}[状态]  迭代: {state.iteration}  "
        f"工具数: {len(state.tools)}  长期记忆: {len(state.long_term)} 条{RESET}"
    )

    # 当前正在执行的工具
    cur_tool = state.meta.get("_current_tool")
    cur_start = state.meta.get("_current_tool_start")
    if cur_tool:
        elapsed = f"{_time.time() - cur_start:.0f}s" if cur_start else "?"
        lines.append(f"{CYAN}  当前工具: {cur_tool}  已耗时: {elapsed}{RESET}")
    else:
        lines.append(f"{GRAY}  当前工具: (空闲中){RESET}")

    # 草稿本摘要
    scratchpad = (state.meta.get("scratchpad") or "").strip()
    sp_preview = scratchpad[:400] + "\n...[截断]" if len(scratchpad) > 400 else scratchpad
    lines.append(f"{CYAN}草稿本:{RESET}")
    for ln in sp_preview.splitlines():
        lines.append(f"  {ln}")

    lines.append(f"{CYAN}{'─'*56}{RESET}")
    print("\n".join(lines), flush=True)


def _print_log(state, n: int = 5) -> None:
    """打印最近 n 条 short_term 执行记录（/log 命令）。"""
    import json as _json

    YELLOW = "\033[93m"
    GREEN  = "\033[92m"
    GRAY   = "\033[90m"
    CYAN   = "\033[96m"
    RESET  = "\033[0m"

    history = list(state.short_term or [])
    recent = history[-n:] if len(history) > n else history

    print(f"\n{CYAN}{'─'*56}{RESET}", flush=True)
    print(f"{CYAN}[执行记录] 最近 {len(recent)} / 共 {len(history)} 条{RESET}", flush=True)

    for i, msg in enumerate(recent, start=len(history) - len(recent)):
        role = msg.get("role", "?")
        content = msg.get("content", "")

        # 尝试解析 assistant JSON（思考/工具调用/完成）
        if role == "assistant":
            try:
                obj = _json.loads(content) if isinstance(content, str) else content
                thought = obj.get("thought", "")
                action  = obj.get("action", "")
                tool    = obj.get("tool", "")
                ans     = obj.get("final_answer", "")
                if tool:
                    label = f"{YELLOW}🔧 [#{i}] 工具: {tool}{RESET}"
                    detail = _json.dumps(obj.get("args", {}), ensure_ascii=False)
                    detail = detail[:200] + "..." if len(detail) > 200 else detail
                elif ans:
                    label = f"{GREEN}✨ [#{i}] 完成{RESET}"
                    detail = ans[:200] + "..." if len(ans) > 200 else ans
                else:
                    label = f"{CYAN}💭 [#{i}] 思考{RESET}"
                    detail = (thought[:200] + "...") if len(thought) > 200 else thought
                print(f"  {label}", flush=True)
                if detail:
                    print(f"    {detail}", flush=True)
                continue
            except Exception:
                pass

        # user / system 消息
        preview = str(content)
        if len(preview) > 300:
            preview = preview[:300] + "..."
        color = GRAY if role == "user" else CYAN
        # 对工具结果做简化显示
        tag = "📥 结果" if "[工具结果]" in preview or "[系统]" in preview else f"{'👤' if role=='user' else '🤖'} {role}"
        first_line = preview.splitlines()[0] if preview else ""
        print(f"  {color}[#{i}] {tag}: {first_line}{RESET}", flush=True)

    print(f"{CYAN}{'─'*56}{RESET}", flush=True)
