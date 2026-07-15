# AGENTS.md — 通用 Agent 运行规范

这份文件是 **总规范**：每次运行都必须遵守。
用户可根据自己的使用场景自由编辑本文件，追加领域专用规范。
启动时候使用获取时间和环境的工具。以便了解时间和所处位置。
重要要求:刚开始用户对话没有明确要求的，必须调用"ask_user"工具来回复，请用户给出详细要求。

重要：开始如果用户没有明确的问题，或者是简单的问候，可以用"ask_user"工具礼貌的回复用户，并请用户提出具体任务。

## 运行目录与工作目录

### 本次运行目录（$RUN_DIR）
- 本次运行的专属目录为环境变量 `RUN_DIR`（格式：`runs/YYYYMMDD-HHMMSS`）。
- **临时文件、抓取网页、中间产物、调试输出**，一律写入 `$RUN_DIR/artifacts/`。
- 运行日志、scratchpad、final_answer 等也自动落入 `RUN_DIR`，无需手动干预。

### 长期 Workspace（新增）
某些任务的产出需要跨多次运行持续维护，例如：开发一个项目代码库、维护一份长期文档、构建一个数据管道。这类任务不适合将文件写入 `runs/`，应使用固定的 workspace 目录，并通过 git 管理版本。

**判断规则：**

```
任务产出是否需要在未来的运行中继续访问或修改？
├── 明确是（长期项目/多次迭代开发）
│   └── 询问用户是否已有 workspace 目录，或让用户指定路径
│       → 将产出写入该目录，并在每次完成阶段性工作后 git commit
├── 明确否（一次性分析/临时输出）
│   └── 照常写入 $RUN_DIR/artifacts/
└── 不确定（无法从目标描述中判断）
    └── 通过 ask_user 询问用户："这个任务的产出是否需要长期维护？
        如果是，请告诉我 workspace 目录路径；如果否，我会写入本次运行目录。"
```

**长期 Workspace 的操作约定：**
- 文件写入用户指定的 workspace 路径（绝对路径或用户提供的相对路径），不受 `runs/` 约束。
- 每完成一个阶段性工作，执行 `git add` + `git commit`，commit message 说明本次做了什么。
- `RUN_DIR` 仍然用于本次运行的执行日志和 scratchpad，两者职责不重叠。

#### 工作日志（WORKLOG.md）—— 长期项目必备

长期项目**必须**在 workspace **根目录**维护一份工作日志文件 `WORKLOG.md`，记录每次阶段性工作，
让下一次运行（可能已丢失上下文）能快速回顾"之前做到哪了、为什么这么做"。

**格式约定：**
- 每条日志包含三要素：**日期**、**事项标题**、**详细内容**。
- 新日志一律**追加到文件末尾**，不修改、不删除已有历史条目（日志是流水账，不是可编辑文档）。
- 每条日志用如下格式（一行标题 + 内容段落）：

  ```
  ## YYYY-MM-DD | <事项标题>

  <详细内容：本次做了什么、为什么这么做、遇到的问题与结论、遗留项 / 下一步……>
  ```

**操作约定：**
- 首次进入 workspace 时若 `WORKLOG.md` 不存在，先创建（首行可写 `# 工作日志`），再追加第一条。
- 每完成一个阶段性工作：**先追加一条日志，再 `git add` + `git commit`**（日志和代码进同一个 commit）。
- 日期用当天真实日期——启动时已用获取时间/环境的工具确认过，直接采用即可，不要凭空猜。
- 内容写实：写清关键决策和踩过的坑，而不是只写"完成了 X"。这份日志是给"失忆的未来自己"看的。

### 写文件规范
- 使用工具 `write_file(path, content)` 时：
  - **临时/中间产物**：`path` 必须以 `runs/` 开头或使用 `$RUN_DIR`。
  - **长期 workspace 产出**：使用用户指定的 workspace 路径，可以是 `runs/` 以外的任意位置。
  - 大文件（HTML/JSON/XML）写入 `artifacts/`，文件名要有语义（如 `search_results.html`）。

---

## 操作系统：Windows（CMD + PowerShell 环境）

**本环境是 Windows，不是 Linux/Mac。** 执行命令前必须确认使用 Windows 命令。

### 常见 Unix → Windows 替代方案

| Unix 命令 | Windows/PowerShell 替代方案 |
|-----------|--------------------------|
| `head -N file` | `powershell -Command "Get-Content 'file' -TotalCount N"` |
| `tail -N file` | `powershell -Command "Get-Content 'file' \| Select-Object -Last N"` |
| `grep pattern file` | `findstr "pattern" file` 或 `powershell -Command "Select-String ..."` |
| `cat file` | `type file`（cmd）或 `read_file` 工具（推荐） |
| `ls` | `dir /b` |
| `curl url` | `powershell -Command "Invoke-WebRequest -Uri 'url'"` |
| `export VAR=val` | `set VAR=val`（cmd）或 `$env:VAR='val'`（PowerShell） |
| `which cmd` | `where cmd` |

---

## run_python 工具使用须知

`run_python` 工具使用**当前框架运行时的 Python 解释器**（自动检测），可直接使用，无需担心找不到解释器。

若需要在 shell 中手动调用 Python，使用：
```
shell(command='python -c "print(1+1)"')
```
不要使用 `python3 -c`（在 Windows 上可能不可用）。

---

## CLI 命令优先

### 核心原则
**能用 CLI 直接执行的单一指令，优先用 CLI 执行，不必再做成工具。**

### 决策树

```
需要执行系统命令？
├── 简单命令（参数少、一次性使用）
│   └── 直接用 shell 工具执行（注意使用 Windows 命令）
├── 复杂命令（多步骤、需要验证、频繁使用）
│   └── 封装成工具
└── 需要与 Agent 深度集成（参数验证、结果解析）
    └── 封装成工具
```

---

## 大文件/大磁盘搜索规范

### 禁止全盘递归搜索

```
# 以下命令会导致超时，禁止使用！
dir /s /b C:\*程序名*
```

### 正确的搜索策略（按优先级）

1. **先查 PATH**：`where 程序名`
2. **查标准安装目录**：`%ProgramFiles%`、`%ProgramFiles(x86)%`、`%LocalAppData%`
3. **查注册表**：`reg query "HKLM\SOFTWARE" /s /f "程序名"`
4. **有限目录递归**：`where /R "C:\Program Files" 程序名.exe`（限定在已知目录内）

---

## 安装软件前的注意事项

1. 杜绝重复安装：安装前需要先查找一下是否已经安装。
2. 安装软件前，最好能询问用户，征得用户同意方可安装。
3. 安装GIT仓库的软件，必须先仔细阅读对应仓库的readme.md，作为安装方法的第一参考。

---

## 工具使用规范（重要）

### 1. 优先使用 `register_tool` 注册新工具，禁止直接修改框架源码

当需要扩展自身能力时，**必须**通过 `register_tool` 工具在运行时注册，而不是直接编辑 `agent/tools/standard.py` 或其他框架源文件。

```
需要新工具？
├── 可用现有工具组合完成  →  直接使用，不必新建工具
├── 需要全新能力          →  调用 register_tool 注册（存入用户工具集）
└── 禁止直接编辑          →  agent/tools/standard.py、agent/core/*.py 等框架文件
```

**原因：** 直接修改源码会污染 git 仓库，且变更对所有后续实例永久生效，影响难以追踪。`register_tool` 注册的工具保存在独立 JSON 文件中，可审查、可回滚。

### 2. `register_tool` 的适用边界

- 工具代码中可以 `import` 任何已安装的第三方库，`exec()` 环境与正常 Python 一致。
- 若依赖库未安装，应先通过 `shell` 工具安装，再注册工具，而不是将库代码写入源文件。
- 注册的工具在当次运行的 `state.tools` 中立即生效；下次启动时由 `load_tools` 从 JSON 自动恢复。

### 3. 不得修改的文件清单

以下文件属于框架核心，**禁止在任务执行中自行修改**，除非用户明确要求：

- `agent/tools/standard.py`
- `agent/core/llm.py`
- `agent/core/compression.py`
- `agent/core/types.py`
- `run_goal.py`
- `AGENTS.md`

如确实需要改动上述文件，必须先通过 `ask_user` 向用户说明原因并征得同意。

---

## 死循环处理规则（重要）

### 识别标志
- 同一工具 + 近似参数连续调用 3 次以上
- 错误信息完全相同但继续重试

### 强制处理规则

```
若某工具/命令连续失败 3 次（相同工具+相近参数）：
  → 必须停止，选择完全不同的策略

若已尝试超过 5 种不同方法仍失败：
  → 选择 ask_user（向用户报告障碍+请求指导）或 done（报告当前状态）
```

---

## 长耗时操作与合法轮询（重要）

### 循环 vs. 轮询的本质区别

| | 死循环 | 合法轮询 |
|---|---|---|
| 每次调用结果 | 完全相同，没有进展 | 结果在变化（进度在推进） |
| 策略是否有意义 | 继续重试不会带来新信息 | 等待是完成任务的必要手段 |
| 典型场景 | 命令一直报错、搜索一直无结果 | 下载进度、编译状态、服务启动检查 |

**框架已对思考中含"等待/轮询/进度"等关键词的调用给予豁免，不会触发循环警告。**
请在 thought 中明确写出轮询意图（如"等待下载完成"、"检查进度"），以避免被误判为死循环。

---

### 长耗时操作的推荐工作方式

#### 1. 触发式（推荐，无轮询）

框架在每次迭代开始时自动检查后台任务，完成后主动将结果注入上下文。
**agent 不需要手动轮询**，只需：

```
step 1: shell_bg("curl -L <url> -o output.zip", timeout=600) → job_id
step 2: [有其他工作就继续做；没有则调 wait_for_job]
step 3: 框架自动通知："[系统] 后台任务 job_xxx 已完成，退出码 0，输出：..."
step 4: 处理结果
```

**有其他工作可做时（最优）：**
```
shell_bg("curl -L <url> -o output.zip") → job_abc
↓ 继续做其他工作（搜索文档、写代码等）
↓ 框架自动注入完成通知
↓ agent 看到通知后继续处理
```

**无其他工作时（纯等待）：**
```
shell_bg("curl -L <url> -o output.zip") → job_abc
wait_for_job(job_id="job_abc", check_interval=30)
↓ 框架静默等待，不调 LLM，不消耗迭代
↓ 完成后自动恢复并注入结果
```

#### 2. 禁止反复调用 `job_wait` 轮询

`job_wait` 用于**一次性查询**当前状态，不应在循环中重复调用。
反复调用同一 `job_id` 的 `job_wait` 属于轮询循环，会触发循环检测。

```
✅ 正确：shell_bg → 做其他工作 → 等框架通知
✅ 正确：shell_bg → wait_for_job（纯等待场景）
✅ 正确：shell_bg → （迭代 N）job_wait 查一次状态 → （不再重复）
❌ 错误：反复调用 job_wait(same_job_id) 轮询
```

#### 3. 进度冻结时主动分析原因

如果连续 3 次 `job_wait` 返回的输出**完全没有变化**（进度冻结）：

```
→ 不要继续轮询
→ 检查：进程是否还在运行？网络是否中断？磁盘是否已满？
→ 找到根本原因后再决定：重试、换方案、还是 ask_user
```

#### 4. 每次关键进度变化后更新草稿本

```
scratchpad_append: 已启动下载 job_abc，文件大小约 2GB，预计 10 分钟
```

即使触发上下文折叠，关键进度信息也不会丢失。

#### 5. 大文件下载的额外建议

- 优先使用支持断点续传的工具（`curl -C -`、`wget -c`）
- 下载前先检查目标磁盘剩余空间
- 对超过 500MB 的文件，下载完毕后验证校验和（MD5/SHA256）

---

## 草稿本（scratchpad）
- 草稿本用于"执行过程中的中间记录与分析"，不是最终答案。
- 多步任务必须维护草稿本：
  - 开始执行前：`scratchpad_set` 写计划/分解
  - 每次关键工具结果后：`scratchpad_append` 写关键发现/下一步

---

## 风险控制
- 任何会生成大量输出/长字符串的 `args`（尤其是 `run_python.code`）要拆步，避免 JSON 输出被截断导致解析失败。

---

## JSON 输出规范（重要！）

### 1. action 字段必须是 tool_call 或 done

**错误示例：**
```json
{action: submit_completion_report, ...}
```

**正确示例：**
```json
{action: tool_call, tool: submit_completion_report, args: {...}}
```

所有工具调用都必须使用 `action: tool_call`，工具名放在 `tool` 字段中。

### 2. 字符串内不能包含未转义的换行符

**错误示例：**
```json
{final_answer: 第一行
第二行}
```

**正确示例：**
```json
{final_answer: 第一行\n第二行}
```

所有多行文本必须用 `\n` 表示换行，不能直接按回车换行。

### 3. 超长内容建议写入文件

如果 `args.command` 或 `args.content` 中包含超长内容（如 base64 编码、代码脚本），
不要在字符串中间折行——建议先用 `write_file` 工具将内容写入临时文件，
再在命令中引用该文件路径（如 `python3 /tmp/script.py`），可彻底避免此类问题。
