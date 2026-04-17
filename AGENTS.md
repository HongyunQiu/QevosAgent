# AGENTS.md — 通用 Agent 运行规范

这份文件是 **总规范**：每次运行都必须遵守。
用户可根据自己的使用场景自由编辑本文件，追加领域专用规范。
启动时候读一下系统时间。以便处理与时间相关的任务。
重要要求:刚开始用户对话没有明确要求的，必须调用"ask_user"工具来回复，请用户给出详细要求。

## 运行目录（最重要）
- 本次运行的工作目录为环境变量：`RUN_DIR`。
- **所有运行中产生的临时文件/抓取网页/中间产物/调试输出**，一律写入：`$RUN_DIR/artifacts/`。
- 除非用户明确要求，禁止在仓库根目录或其他目录写入临时文件。

## 写文件规范
- 使用工具 `write_file(path, content)` 时：
  - `path` 必须以 `runs/` 开头，或显式使用 `$RUN_DIR`（建议先把 `$RUN_DIR` 展开成具体路径再写）。
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
