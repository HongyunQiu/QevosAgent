# 问题记录：看板启动的 Agent 执行 run_python 工具时永远 timeout

## 问题现象

通过 Web 看板（`node dashboard/server.js` → 点击启动）运行 `run_goal.py` 时，Agent 调用
`run_python` 工具执行任何 Python 代码都会超时报错：

```
执行超时（>30s）
```

即使是极简代码也不例外：

```json
{
  "tool": "run_python",
  "args": {
    "code": "print('Hello from Python!')\nprint('2 + 3 =', 2 + 3)"
  }
}
```

而直接在命令行执行 `python run_goal.py "<goal>"` 时，同样的工具调用完全正常。

---

## 根因分析

问题由两个因素叠加触发，缺一不可：

### 因素 1：Node.js 持有一个永不关闭的 stdin 管道

`dashboard/server.js` 使用 Node.js `spawn` 启动 `run_goal.py`，未指定 `stdio` 选项，
Node.js 的默认行为是为所有三路 stdio 创建管道（`['pipe', 'pipe', 'pipe']`）：

```js
// server.js（修复前）
agentProc = spawn(cmd, cmdArgs, {
  cwd:  AGENT_DIR,
  env:  { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  windowsHide: false,
  // stdio 未指定 → 默认 ['pipe', 'pipe', 'pipe']
});
```

- Node.js 拿到 `agentProc.stdin`（管道写端）但**从不写入、也从不关闭**。
- `run_goal.py` 的 stdin 因此是一个**永远空但永远开着**的管道读端。

### 因素 2：`_read_loop_pipe` 永久阻塞在 stdin 管道上

`UserInterruptHandler` 启动时检测 `sys.stdin.isatty()`：

| 启动方式 | isatty() | 使用的读取模式 |
|---|---|---|
| 命令行（conda 终端） | `True` | `_read_loop_tty_win` → `ReadConsoleW`（控制台 API） |
| 看板（Node.js spawn） | `False` | `_read_loop_pipe` → `sys.stdin.readline()` → `ReadFile()` |

在看板模式下，`_read_loop_pipe` 后台线程持续调用 `sys.stdin.readline()`，底层是
**Windows `ReadFile(stdin_pipe_handle, ...)`**，因管道写端从未关闭，该调用**永久阻塞**，
同时持有对 stdin 管道句柄的活跃 I/O 操作。

### 因素 3：`tool_run_python` 未指定 `stdin`，子进程继承同一管道句柄

```python
# agent/tools/standard.py（修复前）
result = subprocess.run(
    [python_exec, "-c", code],
    capture_output=True,   # stdout/stderr → 新管道
    text=True,
    timeout=timeout,
    # stdin 未指定 → 子进程继承父进程的 stdin 句柄
)
```

子进程（`python.exe -c "..."`）启动时：

1. 通过 `STARTUPINFO.hStdInput` 继承父进程的 stdin 管道读端句柄
2. Python 初始化阶段尝试设置/检测 stdin
3. 此时**父进程 `_read_loop_pipe` 的 `ReadFile` 已在同一句柄上挂起**
4. 两者竞争同一匿名管道读端 → 子进程挂起（竞态/Windows 管道语义问题）
5. `communicate()` 等待子进程退出 → 超过 30 秒 → `subprocess.TimeoutExpired`

### 为什么命令行模式不受影响

命令行下 `sys.stdin.isatty()` 返回 `True`，`UserInterruptHandler` 使用
`_read_loop_tty_win`，底层调用的是 **`ReadConsoleW`（Windows 控制台 API）**，
并非 `ReadFile`，不占用 stdin 的文件 I/O 通道，子进程可正常继承并使用 stdin 句柄。

### 为什么 `tool_shell` 不受影响

`tool_shell` 使用 `shell=True`，直接子进程是 `cmd.exe`，由 `cmd.exe` 再派生
`python.exe`（孙进程）。孙进程的 stdin 继承路径不同，规避了上述竞态条件。

---

## 调用链示意

```
Node.js  ──stdin pipe(写端从不关闭)──▶  run_goal.py
                                           │
                                    _read_loop_pipe 线程
                                    永久阻塞在 ReadFile(stdin_handle)
                                           │
                                    tool_run_python 主线程
                                    subprocess.run([python, "-c", code])
                                           │
                                    子进程继承同一 stdin_handle
                                           │
                                    子进程 Python 初始化时竞争 stdin_handle
                                           ▼
                                    子进程挂起 > 30s → TimeoutExpired
```

---

## 修复方案

### 修复 1：`server.js` — 不创建 stdin 管道

```js
// dashboard/server.js
agentProc = spawn(cmd, cmdArgs, {
  cwd:   AGENT_DIR,
  env:   { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  windowsHide: false,
  // stdin 设为 ignore：Agent 通过 CLI 参数获取 goal，
  // 看板命令通过 web_cmd.txt 文件传递（_web_cmd_watcher 线程处理），
  // 不需要 stdin 管道。忽略 stdin 可避免 _read_loop_pipe 永久阻塞。
  stdio: ['ignore', 'pipe', 'pipe'],
});
```

效果：
- `run_goal.py` 的 stdin 为 NUL（Windows 空设备）
- `_read_loop_pipe` 调用 `readline()` 立即得到 EOF（空字符串），线程正常退出
- 无活跃 ReadFile 持有 stdin 句柄

### 修复 2：`tool_run_python` — 显式指定 `stdin=DEVNULL`

```python
# agent/tools/standard.py
result = subprocess.run(
    [python_exec, "-c", code],
    capture_output=True,
    stdin=subprocess.DEVNULL,  # 子进程 stdin → NUL，不继承父进程管道句柄
    text=True,
    timeout=timeout,
    encoding="utf-8",
    errors="replace",
)
```

效果：
- 子进程 stdin 重定向到 NUL，与父进程的任何 stdin 句柄完全隔离
- 即使父进程存在活跃的 stdin I/O，子进程也不受影响
- 对代码执行语义没有影响（执行代码不需要 stdin）

两个修复互为补充：修复 1 解决根源（管道永不关闭），修复 2 是防御性兜底（就算将来 stdin 管道再次被引入，子进程也不会受影响）。

---

## 涉及文件

| 文件 | 修改内容 |
|---|---|
| `dashboard/server.js` | `spawn` 添加 `stdio: ['ignore', 'pipe', 'pipe']` |
| `agent/tools/standard.py` | `tool_run_python` 的 `subprocess.run` 添加 `stdin=subprocess.DEVNULL` |
| `agent/tools/standard.py` | `tool_shell` 的 `child_env` 中将 `sys.executable` 目录前置到 PATH（关联问题，见下节） |

---

## 附：关联问题 — `tool_shell` 中 `python` 命令找不到

**现象**：看板模式下，`tool_shell` 执行 `python foo.py` 类命令时，找不到 `python` 可执行文件。

**原因**：`tool_shell` 构建子进程环境时使用 `{**os.environ, ...}`，而 `os.environ` 中的
`PATH` 来自 Node.js 启动时的环境。若 Node.js 启动时未激活 conda 环境，`PATH` 中没有
conda Python 路径，shell 命令中的 `python` 无法解析。

**修复**：在 `tool_shell` 的 `child_env` 中将 `sys.executable` 所在目录前置到 PATH：

```python
import sys as _sys
_python_dir = str(Path(_sys.executable).parent)
_path = os.environ.get("PATH", "")
_patched_path = (
    _python_dir + os.pathsep + _path
    if _python_dir not in _path.split(os.pathsep)
    else _path
)
child_env = {**os.environ, "PYTHONUNBUFFERED": "1", "PATH": _patched_path}
```

确保 `tool_shell` 中的 `python` 始终解析为当前运行 Agent 的同一个解释器。
