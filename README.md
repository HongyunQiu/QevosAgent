# simpleAgent

`simpleAgent` 是一个偏运行时视角的极简自主智能体框架。

它保留了最小闭环 `LLM -> tool_call -> feedback -> done`，但在这个基础上补上了几件对真实运行更重要的东西：

- 可恢复的长期记忆与工具快照
- 运行中持续落盘，而不是只在最后输出结果
- 对工具演化和工具修复的显式支持
- 对上下文膨胀、JSON 解析失败、重复调用循环等常见问题的自恢复
- 一个可直接启动任务、观察状态、查看历史 run 的 Web Dashboard

这个仓库适合拿来做两类事情：

- 研究一个小而完整的 agent runtime
- 作为本地模型驱动的实验底座继续迭代

## 当前能力

当前已跟踪代码覆盖的能力主要有：

- `OpenAI-compatible` 后端支持，可通过 `OPENAI_BASE_URL` 接入本地或远端兼容服务
- `Anthropic` 后端支持
- 默认标准工具集，包括文件读写、shell、Python 执行、草稿本、长期记忆、快照、后台任务
- 运行期 `scratchpad` 机制，要求模型在多步任务中维护计划和验收记录
- `agent_snapshot_meta.json` 快照恢复，保存长期记忆、进化工具和修复候选
- 工具修复链路：`validate_tool_recipe -> repair_tool_candidate -> promote_tool_candidate`
- 运行期持久化：`short_term.jsonl`、`status.json`、`meta.json`、`final_answer.md` 等持续写盘
- 用户干预机制：命令行输入 `/inject`、`/stop`、`/status`、`/+N` 等命令
- Web Dashboard：启动任务、停止任务、注入命令、查看 run 文件、编辑 `AGENTS.md` 和快照

## 仓库结构

```text
agent/
  core/
    llm.py               # LLM 后端、system prompt、响应解析
    executor.py          # 工具执行与参数过滤
    loop.py              # 主循环、自恢复、验收门禁、上下文压缩
    async_manager.py     # 后台 shell 任务管理
    types.py             # Action / ToolSpec / ToolResult / AgentState
  runtime/
    persistence.py       # 运行期落盘与 run 产物生成
    user_interrupt.py    # 命令行用户干预
  tools/
    standard.py          # 标准工具集、快照与工具演化

run_goal.py              # 命令行启动入口
demo.py                  # 最小示例与手动组装示例
dashboard/
  server.js              # Dashboard 服务端
  public/index.html      # Dashboard 前端

tests_parse_response.py
tests_runtime_regressions.py
agent_snapshot_meta.json # 默认快照文件（最小合法 JSON）
```

## 运行模型

主循环的大致流程如下：

1. 根据工具、长期记忆和草稿本构建 system prompt
2. 将 `short_term` 组装成对话消息
3. 估算 prompt 大小，必要时压缩上下文
4. 调用 LLM，解析返回的 JSON action
5. 执行工具并把结果回灌到 `short_term`
6. 自动提炼关键信息回写 `scratchpad`
7. 在 `done` 前检查 `ACCEPTANCE` 验收块
8. 持续更新运行状态和最终产物

当前实现特别关注几类运行时问题：

- 上下文过大时自动裁剪 `short_term`
- 过大的工具输出先落盘再向模型返回预览
- 响应带前缀文本、代码块包裹、多段 JSON 时尽量鲁棒解析
- 工具重复调用进入循环时发出警告，必要时触发硬封锁和上下文重建

## 标准工具集

当前标准工具包含：

- `remember`
- `raw_append`
- `scratchpad_get`
- `scratchpad_set`
- `scratchpad_append`
- `think`
- `run_python`
- `shell`
- `web_search`
- `write_file`
- `read_file`
- `read_file_lines`
- `file_outline`
- `grep_files`
- `analyze_content`
- `edit_file`
- `set_goal`
- `ask_user`
- `append_episodic`
- `search_episodic`
- `save_concept`
- `read_concept`
- `save_tools`
- `load_tools`
- `validate_tool_recipe`
- `repair_tool_candidate`
- `promote_tool_candidate`
- `register_tool`
- `delete_tool`
- `shell_bg`
- `job_wait`
- `job_cancel`
- `jobs_list`

其中 `register_tool` 用于新增工具，`repair_tool_candidate` 和 `promote_tool_candidate` 用于修复已有工具而不是无限注册同义新工具。

## 安装

### Python 依赖

```powershell
python -m pip install -r requirements.txt
```

当前 `requirements.txt` 只包含三个依赖：

- `openai`
- `anthropic`
- `tiktoken`

### Dashboard 依赖

如果你要使用 Web Dashboard，再安装 Node 侧依赖：

```powershell
cd dashboard
npm install
```

Dashboard 需要 Node.js 18 或更高版本。

## 配置

项目提供了 `.env.example`，`run_goal.py` 会在启动时自动尝试读取当前目录下的 `.env`。

最常用的环境变量是：

- `OPENAI_PROFILE`
- `OPENAI_PROFILE_OSS120B_BASE_URL`
- `OPENAI_PROFILE_QWEN3527DGX_BASE_URL`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_API_KEY`
- `MAX_ITERS`
- `RUNS_DIR`
- `AGENT_SNAPSHOT`
- `AUTO_REMEMBER_ON_DONE`
- `AUTO_SAVE_SNAPSHOT_ON_EXIT`

默认行为有几点值得注意：

- 如果没有显式设置 `OPENAI_BASE_URL`，启动器会尝试根据 `OPENAI_PROFILE` 选择对应的 profile base URL
- 启动时会先探测模型服务；如果服务端只返回一个模型，会自动把 `OPENAI_MODEL` 切到该模型
- 默认启用 `AUTO_REMEMBER_ON_DONE=1` 和 `AUTO_SAVE_SNAPSHOT_ON_EXIT=1`
- 默认快照文件是仓库根目录的 `agent_snapshot_meta.json`

## 快速开始

### 1. 准备环境变量

在仓库根目录创建 `.env`，至少填好模型服务地址和 API key。可以直接参考 `.env.example`。

### 2. 从命令行运行

```powershell
python run_goal.py "分析当前目录并总结问题"
```

`run_goal.py` 启动时会做几件事：

- 自动读取 `.env`
- 检查并探测模型服务
- 创建 `runs/<timestamp>/` 作为本次运行目录
- 设置 `RUN_DIR` 和默认 `RAW_MEMORY_PATH`
- 如果 `agent_snapshot_meta.json` 存在，则要求优先加载快照
- 如果仓库根目录存在 `AGENTS.md`，则把它作为本次运行的额外规范注入
- 在结束后自动保存快照

### 3. 运行中人工干预

命令行模式下，可以在运行期间输入以下命令：

- `/help`
- `/stop`
- `/exit`
- `/inject <消息>`
- `/compress [N]`
- `/status`
- `/log [N]`
- `/+N` 形式的迭代扩容命令，例如 `/+50`

这些命令由 [`agent/runtime/user_interrupt.py`](./agent/runtime/user_interrupt.py) 处理。

## Dashboard

启动方式：

```powershell
cd dashboard
npm start
```

默认访问地址是 `http://localhost:8765`。可通过环境变量调整：

- `DASHBOARD_PORT`
- `RUNS_DIR`
- `AGENT_DIR`
- `POLL_MS`
- `PYTHON_CMD`

当前 Dashboard 支持：

- 启动一个新的 `run_goal.py` 任务
- 停止正在运行的任务
- 向运行中的任务注入 `/inject` 等命令
- 查看当前运行状态和历史 runs
- 浏览某次 run 下的文件
- 查看和编辑仓库根目录的 `AGENTS.md`
- 查看和编辑 `agent_snapshot_meta.json`

## 运行产物

每次运行默认会在 `runs/<timestamp>/` 下生成一组文件。当前持久化逻辑会写入：

- `short_term.jsonl`
- `meta.json`
- `status.json`
- `scratchpad.md`
- `final_answer.md`
- `execution_summary.md`
- `issues.json`
- `reflection.md`

如果工具输出过大，主循环还会把原始输出写入 `artifacts/` 目录，避免信息因为上下文截断而丢失。

## 快照机制

默认快照文件是仓库根目录的 `agent_snapshot_meta.json`。

当前快照会保存这些内容：

- `long_term`
- `evolved_tools`
- `tool_repair_candidates`
- `tool_repair_failures`
- `tool_repair_history`
- `scratchpad`

不过加载时默认只恢复长期记忆、正式进化工具和修复候选，不会直接恢复旧草稿本内容，避免把过期中间态带入新任务。

仓库中自带的 `agent_snapshot_meta.json` 是一个最小合法 JSON 占位文件，这样即使某些流程显式调用 `load_snapshot_meta`，也不会因为“文件不存在”或“空文件不是合法 JSON”而直接失败。

## 作为库使用

### 最简单的用法

```python
from agent import Agent

agent = Agent(
    backend="openai",
    api_key="local",
    max_iterations=20,
    verbose=True,
)

state = agent.run("帮我分析一个目录结构")
print(state.meta.get("final_answer"))
```

### 手动添加工具

```python
from agent import Agent
from agent.core.types import ToolSpec, ToolResult

def to_upper(state, text: str) -> ToolResult:
    return ToolResult(success=True, output=text.upper())

agent = Agent(verbose=False)
agent.add_tool(
    ToolSpec(
        name="to_upper",
        description="把输入文本转成大写",
        args_schema={"text": "输入文本"},
        fn=to_upper,
    )
)
```

### 更底层的组装方式

```python
from agent.core.loop import run, console_hooks
from agent.core.llm import OpenAIBackend
from agent.tools.standard import get_standard_tools

llm = OpenAIBackend(api_key="local", base_url="http://localhost:8000/v1")
state = run(
    goal="完成一个任务",
    llm=llm,
    tools=get_standard_tools(),
    hooks=console_hooks(),
    max_iterations=10,
)
```

更多示例可以看 `demo.py`。

## 测试

当前仓库里有两个直接可运行的测试入口：

```powershell
python tests_parse_response.py
python tests_runtime_regressions.py
```

覆盖重点包括：

- 响应解析鲁棒性
- 快照恢复与无效进化工具过滤
- 工具修复链路
- 验收证据解析
- `run_goal.py` 的环境变量默认值与服务探测
- 运行期持久化

## 设计边界

这个项目当前的定位仍然是“简单但完整”，所以也保留了一些明确边界：

- 依赖管理仍然是 `requirements.txt`，还不是完整的 Python 包发布结构
- `shell` 和 `run_python` 权限很强，没有做额外沙箱隔离
- 验收门禁只校验最基本的证据形式，不保证答案在语义上一定正确
- 长期记忆目前是轻量的字符串列表，不是完整检索系统
- Dashboard 是轻量 Node 服务，不是完整的多用户任务平台

## 相关文件

- [`run_goal.py`](./run_goal.py)
- [`agent/core/loop.py`](./agent/core/loop.py)
- [`agent/tools/standard.py`](./agent/tools/standard.py)
- [`agent/runtime/persistence.py`](./agent/runtime/persistence.py)
- [`dashboard/server.js`](./dashboard/server.js)
