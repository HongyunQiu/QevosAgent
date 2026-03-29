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

## CLI命令优先
- 能用CLI直接执行的单一指令，优先用CLI执行，不必再做成工具。多步CLI指令可以做成工具。
- CLI

## 草稿本（scratchpad）
- 草稿本用于“执行过程中的中间记录与分析”，不是最终答案。
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
- 如需追加“原始记忆片段”，优先调用 `raw_append(content)`（不传 path 时会自动写入 `RAW_MEMORY_PATH`，即本次 run 目录）。

## 风险控制
- 任何会生成大量输出/长字符串的 `args`（尤其是 `run_python.code`）要拆步，避免 JSON 输出被截断导致解析失败。
