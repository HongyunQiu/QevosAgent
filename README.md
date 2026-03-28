# simpleAgent

一个极简自主智能体框架。

它目前的核心能力已经不是单纯的 `LLM -> tool_call -> done` 循环，而是：

- 支持 **OpenAI-compatible** 后端，默认对接本地模型服务
- 每次运行自动创建独立 `runs/<timestamp>/` 工作目录并落盘过程产物
- 支持 **snapshot** 恢复长期记忆和进化工具
- 支持 **工具候选修复 -> 校验 -> 晋升**，而不只是盲目新增工具
- 在 `done` 前执行 **ACCEPTANCE 验收门禁**
- 在上下文过大、JSON 输出截断等常见失败场景下做自恢复

如果你想要的是“代码量不大，但已经开始具备 agent runtime 味道”的 Python 项目，这个仓库就是它。

## 当前现状

基于当前代码，项目大致处于下面这个阶段：

- **核心闭环已经可用**：有状态循环、工具执行、短期/长期记忆、最终结果落盘都已实现。
- **运行产物体系已成型**：`run_goal.py` 会为每次运行生成独立目录，并保存最终答案、原始轨迹、执行总结、反思和结构化问题列表。
- **快照恢复机制已成型**：可从 `agent_snapshot_meta.json` 恢复长期记忆、已进化工具及工具修复候选。
- **工具进化更稳了**：现在不仅能 `register_tool`，还支持 `validate_tool_recipe`、`repair_tool_candidate`、`promote_tool_candidate`。
- **回合结束前有硬验收**：模型如果在草稿本里没有写 `ACCEPTANCE`，或声称产出文件但文件不存在，主循环不会接受 `done`。
- **回归测试已覆盖关键问题**：当前测试聚焦参数过滤、快照恢复、工具修复链路、验收证据解析、环境默认值等近期高风险点。

这个仓库仍然小，但它已经明显演化成一个带运行规范和恢复机制的 agent runtime。

## 仓库结构

```text
agent/
├── __init__.py            # Agent 高层封装
├── core/
│   ├── types.py           # Action / ToolSpec / ToolResult / AgentState
│   ├── llm.py             # LLM 后端、system prompt、响应解析
│   ├── executor.py        # 工具执行与参数过滤
│   └── loop.py            # 主循环、验收门禁、上下文压缩、自恢复
└── tools/
    └── standard.py        # 标准工具集、snapshot、草稿本、工具进化/修复

run_goal.py                # 命令行入口，负责 RUN_DIR、默认模型配置、产物落盘
tests_parse_response.py    # 响应解析健壮性测试
tests_runtime_regressions.py
                           # 运行时回归测试
agent_snapshot_meta.json   # 默认快照文件
runs/                      # 每次运行的产物目录
```

## 运行模型

主循环仍然很简单，但现在多了运行时保护层：

```text
1. 构建 system prompt（含工具、长期记忆、scratchpad）
2. 构建 short_term messages
3. 估算 token，必要时自动压缩上下文
4. 调用 LLM
5. 解析 JSON 响应为 Action
6. 执行工具并把结果回灌到 short_term
7. 若模型尝试 done，则先过 ACCEPTANCE gate
8. 成功后保存 final_answer / meta / summary / reflection / issues
```

## 内置能力

### 1. 记忆与过程记录

- `short_term`：当前运行的完整线性对话/工具轨迹
- `long_term`：跨次运行保留的经验字符串列表
- `scratchpad`：模型工作台，要求多步任务先写计划，再持续追加关键发现
- `raw_append`：将原始片段写入 NDJSON，不做总结

### 2. 文件与执行工具

标准工具里已经包含：

- `read_file`
- `write_file`
- `shell`
- `run_python`
- `ask_user`
- `set_goal`
- `think`

这些工具是故意保持“通用且低抽象”的，方便模型自由组合。

### 3. 工具进化与修复

除了 `register_tool` 之外，还支持一条更稳妥的修复链路：

1. `validate_tool_recipe`
2. `repair_tool_candidate`
3. `promote_tool_candidate`

这比“发现坏工具后再注册一个同义新工具”更符合长期演化的需要。

### 4. 快照恢复

`save_snapshot_meta` / `load_snapshot_meta` 会处理：

- `long_term`
- `evolved_tools`
- `tool_repair_candidates`
- `tool_repair_failures`
- `tool_repair_history`

默认快照文件是仓库根目录的 `agent_snapshot_meta.json`。

### 5. 验收门禁

在模型输出 `done` 前，主循环会检查草稿本里是否存在 `ACCEPTANCE` 区块，并根据 `evidence_type` 决定是否校验引用的产物文件是否真实存在。

支持的 `evidence_type`：

- `artifact`
- `tool_result`
- `observation`
- `none`

其中只有 `artifact` 会触发文件存在性检查。

## 安装

仓库现在提供了基础版 `requirements.txt`，包含当前代码里实际用到的第三方依赖。

推荐安装：

```bash
pip install -r requirements.txt
```

如果你只打算使用 OpenAI-compatible 后端，也可以最小安装：

```bash
pip install openai
```

说明：

- `openai`：`OpenAIBackend` 必需
- `anthropic`：使用 `AnthropicBackend` 时需要
- `tiktoken`：可选；用于更准确地估算 prompt token。放进 `requirements.txt` 是为了开箱即用

## 快速开始

### 命令行运行

```bash
python3 run_goal.py "分析当前目录并总结问题"
```

`run_goal.py` 会自动做几件事：

- 创建 `runs/<timestamp>/`
- 设置 `RUN_DIR`
- 设置默认 `RAW_MEMORY_PATH`
- 优先尝试加载 `agent_snapshot_meta.json`
- 在结束后默认保存 snapshot
- 将过程产物写入本次运行目录

### 模型切换

默认 profile 是 `oss120b`。现在 profile 对应的 base URL 不再写死在代码里，而是从环境变量读取。

仓库里提供了 `.env.example` 作为环境变量模板。注意：当前项目不会自动加载 `.env`，如果你想复用这个模板，需要在 shell 里手动导出，例如：

```bash
set -a
source .env.example
set +a
python3 run_goal.py "帮我总结这个项目"
```

```bash
# 默认：oss120b
export OPENAI_PROFILE_OSS120B_BASE_URL=http://your-oss120b-host:8389/v1
python3 run_goal.py "帮我总结这个项目"

# 切到 qwen3527dgx
export OPENAI_PROFILE_QWEN3527DGX_BASE_URL=http://your-qwen-host:8000/v1
OPENAI_PROFILE=qwen3527dgx python3 run_goal.py "帮我总结这个项目"
```

当前 `run_goal.py` 内置了两个 profile 名称和默认模型名：

- `oss120b` -> base URL 来自 `OPENAI_PROFILE_OSS120B_BASE_URL`，模型名 `openai/gpt-oss-120b`
- `qwen3527dgx` -> base URL 来自 `OPENAI_PROFILE_QWEN3527DGX_BASE_URL`，模型名 `qwen3527dgx`

如果你已经直接设置了 `OPENAI_BASE_URL`，则它的优先级更高；只有没设置 `OPENAI_BASE_URL` 时，才会回退去读 profile 对应的环境变量。

如果你不想使用内置 profile，也可以自己覆盖：

```bash
OPENAI_BASE_URL=http://your-server:8000/v1 \
OPENAI_API_KEY=local \
OPENAI_MODEL=your-model \
python3 run_goal.py "执行一个任务"
```

## 运行产物

每次运行默认会在 `runs/<timestamp>/` 下生成：

- `final_answer.md`
- `short_term.jsonl`
- `meta.json`
- `execution_summary.md`
- `reflection.md`
- `issues.json`
- `scratchpad.md`
- `raw_memory.ndjson`（默认通过 `RAW_MEMORY_PATH` 指向本次 run）

这使得项目不只是“拿到一个最终答案”，而是可以复盘本次 agent 到底做了什么。

## 作为库使用

### 最简单的方式

```python
from agent import Agent

agent = Agent(
    backend="openai",
    api_key="your-api-key",   # 本地 OpenAI-compatible 服务可填任意占位值
    max_iterations=40,
    verbose=True,
)

state = agent.run("帮我分析一个目录结构")
print(state.meta.get("final_answer"))
```

### 添加自定义工具

```python
from agent import Agent
from agent.core.types import ToolSpec, ToolResult

def my_tool(state, text: str) -> ToolResult:
    return ToolResult(success=True, output=text.upper())

agent = Agent(verbose=False)
agent.add_tool(ToolSpec(
    name="my_tool",
    description="把输入文本转成大写",
    args_schema={"text": "输入文本"},
    fn=my_tool,
))

state = agent.run("调用 my_tool 处理 hello")
```

### 切换后端

```python
from agent import Agent

# OpenAI / OpenAI-compatible
agent = Agent(backend="openai", model="gpt-4o", api_key="...")

# Anthropic
agent = Agent(backend="anthropic", model="claude-opus-4-6", api_key="...")
```

如果要接入其他模型供应商，直接实现 `agent.core.llm.LLMBackend` 即可。

## 测试

当前仓库内显式可运行的测试入口：

```bash
python3 tests_parse_response.py
python3 tests_runtime_regressions.py
```

其中：

- `tests_parse_response.py` 关注模型输出前缀文本、代码块包裹、多个 JSON 连发等解析鲁棒性
- `tests_runtime_regressions.py` 关注近期修复过的运行时回归点

## 重要环境变量

常用变量如下：

- `OPENAI_PROFILE`
- `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `RUNS_DIR`
- `AGENT_SNAPSHOT`
- `MAX_ITERS`
- `AUTO_REMEMBER_ON_DONE`
- `AUTO_SAVE_SNAPSHOT_ON_EXIT`
- `RAW_MEMORY_PATH`
- `LLM_MAX_TOKENS`
- `LLM_CONTEXT_WINDOW`
- `MAX_TOOL_FEEDBACK_CHARS`
- `SCRATCHPAD_MAX_CHARS`

## 已知边界

当前实现仍然故意保持“简单但直接”，因此也有明显边界：

- 没有打包配置文件，依赖管理靠手动安装
- `shell` / `run_python` 功能很强，但默认没有额外沙箱隔离
- 验收门禁只做最小硬校验，不会自动判断“答案语义上是否真的正确”
- `run_goal.py` 默认偏向本地 OpenAI-compatible 工作流，而不是通用云端部署模板
- 长期记忆目前仍是字符串列表，没有结构化检索或压缩策略

## 适合拿它做什么

- 验证 agent loop 的基本设计
- 研究“工具进化 + 快照恢复 + 运行复盘”的最小实现
- 做本地模型驱动的自主任务实验
- 继续往更完整的 agent runtime 方向演化
