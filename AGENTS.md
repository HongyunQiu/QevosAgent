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

---

## 操作系统：Windows（CMD + PowerShell 环境）

**本环境是 Windows，不是 Linux/Mac。** 执行命令前必须确认使用 Windows 命令。

### ❌ 禁止使用的 Unix 命令（在本环境中不可用）

| Unix 命令 | 错误现象 | Windows/PowerShell 替代方案 |
|-----------|---------|--------------------------|
| `head -N file` | '头部' 不是内部命令 | `powershell -Command "Get-Content 'file' -TotalCount N"` |
| `tail -N file` | 'tail' 不是内部命令 | `powershell -Command "Get-Content 'file' \| Select-Object -Last N"` |
| `grep pattern file` | 'grep' 不是内部命令 | `findstr "pattern" file` 或 `powershell -Command "Select-String ..."` |
| `cat file` | 输出可能乱码 | `type file`（cmd）或 `read_file` 工具（推荐） |
| `ls` | 可能无输出 | `dir /b` 或 `cross_platform_file_list` 工具（推荐） |
| `wc -l file` | 'wc' 不是内部命令 | `find /c /v "" file` |
| `curl url \| head` | 管道输出被截断 | `powershell -Command "Invoke-WebRequest -Uri 'url'"` |
| `export VAR=val` | 'export' 不是内部命令 | `set VAR=val`（cmd）或 `$env:VAR='val'`（PowerShell） |
| `which cmd` | 'which' 不是内部命令 | `where cmd` |

### ✅ Windows 常用命令速查

```
# 查找文件（避免 dir /s /b 全盘搜索，会超时！）
where /R "C:\Program Files" program.exe   ← 在指定目录递归搜索
where program                             ← 在 PATH 中查找

# 读注册表（快速定位软件安装路径）
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" /s /f "软件名"

# PowerShell 网络请求
powershell -Command "Invoke-WebRequest -Uri 'URL' -OutFile 'file.zip'"

# 文件内容搜索
findstr /S /I "keyword" *.txt

# 环境变量展开
echo %USERPROFILE%    → C:\Users\92680
echo %ProgramFiles%   → C:\Program Files
```

---

## run_python 工具使用须知

`run_python` 工具使用**当前框架运行时的 Python 解释器**（自动检测），**不依赖 PATH 中的 python3**。可直接使用，无需担心找不到解释器。

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
│   └── 封装成工具（参考 cli_tool_wrapper_guide.md）
└── 需要与 Agent 深度集成（参数验证、结果解析）
    └── 封装成工具
```

### 参考文档
- 详细封装指南：`runs/20260329-172845/artifacts/cli_tool_wrapper_guide.md`

---

## 大文件/大磁盘搜索规范

### ⚠️ 禁止全盘递归搜索

```
# 以下命令会导致超时，禁止使用！
dir /s /b C:\*程序名*         ← 遍历整个C盘，必然超时
dir /s /b D:\*vivado*         ← 同上
find / -name "program" 2>/dev/null  ← Unix命令，不可用
```

### ✅ 正确的搜索策略（按优先级）

1. **先查 PATH**：`where 程序名`
2. **查标准安装目录**：`%ProgramFiles%`、`%ProgramFiles(x86)%`、`%LocalAppData%`
3. **查注册表**：`reg query "HKLM\SOFTWARE" /s /f "程序名"`
4. **查桌面/开始菜单快捷方式**：`dir "%USERPROFILE%\Desktop\*.lnk"`
5. **有限目录递归**：`where /R "C:\Program Files" 程序名.exe`（限定在已知目录内）
6. **cross_platform_file_list 工具**（推荐，已优化超时）

---

## 软件安装后的 PATH 刷新问题

**当前 shell 会话的 PATH 不会自动包含新安装的软件！** 这是最常见的失败原因之一。

### 安装后立即使用的正确方式

```
# ❌ 错误：安装后直接调用（会说"命令未找到"）
shell(command='scoop install iverilog')
shell(command='iverilog --version')   ← 失败！PATH 还没更新

# ✅ 正确：用完整路径调用，或刷新环境变量后再调用
shell(command='scoop install iverilog')
shell(command='%USERPROFILE%\\scoop\\shims\\iverilog.exe --version')  ← 用完整路径

# 或者在同一条命令里完成：
shell(command='scoop install iverilog && %USERPROFILE%\\scoop\\shims\\iverilog.exe --version')
```

### 常见工具的完整路径

| 工具 | 安装后的完整路径 |
|------|----------------|
| Scoop 自身 | `%USERPROFILE%\scoop\shims\scoop.cmd` |
| Scoop 安装的软件 | `%USERPROFILE%\scoop\shims\软件名.exe` |
| Chocolatey | `C:\ProgramData\chocolatey\bin\choco.exe` |
| winget | `winget`（已在 PATH，直接用） |

---

## 死循环处理规则（重要）

### 识别标志
- 同一工具 + 近似参数连续调用 3 次以上
- 错误信息完全相同但继续重试
- 在 thought 中已写"我陷入了循环"却还继续

### 强制处理规则

```
若某工具/命令连续失败 3 次（相同工具+相近参数）：
  → 必须停止，选择完全不同的策略

若已尝试超过 5 种不同方法仍失败：
  → 评估障碍是否属于环境限制（无法通过更多尝试解决）
  → 选择 ask_user（向用户报告障碍+请求指导）或 done（报告当前状态）

对于安装类任务，若某包管理器失败 2 次：
  → 立即切换到其他包管理器或手动下载方案
  → 包管理器优先级：winget → choco → scoop → 手动下载
```

### 安装类任务的失败升级路径

```
步骤1：winget install 软件名
  失败 → 步骤2
步骤2：choco install 软件名  （若 choco 可用）
  失败 → 步骤3
步骤3：scoop install 软件名  （若 scoop 可用）
  失败 → 步骤4
步骤4：web_search 搜索官方下载地址 → 手动下载安装包
  失败 → 步骤5
步骤5：ask_user 报告障碍，请求用户手动下载或提供安装包路径
```

---

## 草稿本（scratchpad）
- 草稿本用于"执行过程中的中间记录与分析"，不是最终答案。
- 多步任务必须维护草稿本：
  - 开始执行前：`scratchpad_set` 写计划/分解
  - 每次关键工具结果后：`scratchpad_append` 写关键发现/下一步

---

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
