# AGENTS.md — simpleAgent 运行规范（借鉴 OpenClaw 约定）

这份文件是 **总规范**：每次运行都必须遵守。

## 运行目录（最重要）
- 本次运行的工作目录为环境变量：`RUN_DIR`。
- **所有运行中产生的临时文件/抓取网页/中间产物/调试输出**，一律写入：`$RUN_DIR/artifacts/`。
- 除非用户明确要求，禁止在仓库根目录或其他目录写入临时文件。

## 写文件规范
- 使用工具 `write_file(path, content)` 时：
  - `path` 必须以 `runs/` 开头，或显式使用 `$RUN_DIR`（建议先把 `$RUN_DIR` 展开成具体路径再写）。
  - 大文件（HTML/JSON/XML）写入 `artifacts/`，文件名要有语义（如 `ddg_search_openclaw.html`）。
## CLI 命令优先

### 核心原则
**能用 CLI 直接执行的单一指令，优先用 CLI 执行，不必再做成工具。**

### 决策树

需要执行系统命令？
├── 简单命令（参数少、一次性使用）
│   └── 直接用 shell 工具执行
├── 复杂命令（多步骤、需要验证、频繁使用）
│   └── 封装成工具（参考 cli_tool_wrapper_guide.md）
└── 需要与 Agent 深度集成（参数验证、结果解析）
    └── 封装成工具

### CLI 使用指南

**简单场景 - 直接使用 shell 工具：**
示例：简单查询
shell(command='ls -la /tmp')
shell(command='grep -r pattern ./src')

**复杂场景 - 封装成工具：**
- 多步骤操作（如：查询→过滤→处理→保存）
- 需要参数验证
- 需要解析输出并结构化返回
- 频繁使用的命令组合

### 工具封装最佳实践
1. **参数验证**：在工具中验证参数合法性
2. **错误处理**：捕获并处理可能的错误
3. **结果解析**：将 CLI 输出解析为结构化数据
4. **日志记录**：记录执行过程和结果
5. **文档说明**：提供清晰的参数说明和使用示例

### 参考文档
- 详细封装指南：`runs/20260329-172845/artifacts/cli_tool_wrapper_guide.md`

## 草稿本（scratchpad）
- 草稿本用于"执行过程中的中间记录与分析"，不是最终答案。
- 多步任务必须维护草稿本：
  - 开始执行前：`scratchpad_set` 写计划/分解
  - 每次关键工具结果后：`scratchpad_append` 写关键发现/下一步

## Raw 数据与复盘
- 运行结束后会自动落盘：
  - `final_answer.md`（结果）
  - `execution_summary.md`（过程概览）
  - `reflection.md`（过程反思）
  - `issues.json`（结构化问题）
  - `short_term.jsonl`（raw 轨迹）
  - `meta.json`
- 如需追加"原始记忆片段"，优先调用 `raw_append(content)`（不传 path 时会自动写入 `RAW_MEMORY_PATH`，即本次 run 目录）。

## 风险控制
- 任何会生成大量输出/长字符串的 `args`（尤其是 `run_python.code`）要拆步，避免 JSON 输出被截断导致解析失败。
