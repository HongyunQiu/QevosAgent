# AGENTS.md — FPGA 开发专业 Agent 运行规范

这份文件是 **总规范**：每次运行都必须遵守。
本 Agent 专注于 **FPGA/Verilog 开发全流程**：RTL 设计 → 编译 → 仿真 → 验证。

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

## shell 工具的实时输出与缓冲行为

看板的 Console 标签页能实时显示 `shell` 命令的输出，但各类程序的缓冲策略不同：

### Python（已自动处理）

`shell` 工具在启动子进程时已自动注入 `PYTHONUNBUFFERED=1`，Python 子进程的 `print()` 会立即 flush 到管道，Console 标签页**实时可见**。无需额外操作。

```python
# 以下命令的每行输出会立即出现在 Console 标签页
shell(command='python -c "import time\nfor i in range(10):\n  print(i)\n  time.sleep(0.5)"')
```

### 非 Python 程序的缓冲问题

其他语言/程序有各自的缓冲策略，输出**可能在进程结束时才一次性显示**：

| 程序类型 | 默认缓冲行为 | 实时输出解决方案 |
|---------|------------|----------------|
| **Node.js** | `console.log` 通常行缓冲，管道时可能变块缓冲 | 用 `--unhandled-rejections=strict` 或 `process.stdout.write()` |
| **Go 二进制** | `fmt.Println` 默认无缓冲，通常实时 | 一般无需处理 |
| **Rust 二进制** | `println!` 默认行缓冲，管道时变块缓冲 | `RUST_LOG=` 或用 `eprintln!`（stderr 通常无缓冲） |
| **vvp（Icarus Verilog）** | `$display` 输出实时，但量大时仍有延迟 | 控制 `$display` 数量（见仿真规范） |
| **PowerShell 脚本** | 输出经 PowerShell 对象流，完成后才写入管道 | 改用 `cmd /c` 直接调用，或在 PowerShell 内用 `[Console]::Out.Flush()` |
| **scoop / winget / pip** | 安装类工具通常有自己的进度刷新 | 部分工具支持 `--no-progress` 减少杂乱输出 |

### 处理策略

**策略 1：接受延迟，等命令结束看完整输出**（适合大多数情况）

对于安装、编译等一次性命令，不需要实时看输出，命令结束后 tool_result 里有完整内容。

**策略 2：stderr 往往实时（无缓冲）**

进度信息、警告、错误通常走 stderr，而 stderr 多数程序默认不缓冲。看 Console 标签页里带 `[stderr]` 前缀的行通常是实时的。

**策略 3：强制刷新技巧（按需使用）**

```bash
# 用 stdbuf 工具（Linux/WSL）强制行缓冲
stdbuf -oL <command>

# PowerShell：跳过对象流，直接调目标程序
cmd /c "your_program.exe args"

# Node.js：强制无缓冲
node --no-warnings script.js 2>&1
```

**策略 4：超时设置要给缓冲留余量**

程序运行完毕但输出还在缓冲区里，是正常现象。`shell` 的 `timeout` 参数应设为**预期运行时间 × 2** 以上，确保进程有时间正常退出并 flush 所有输出。

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

---

# FPGA / Verilog 开发专业规范

## 工具链

### 主工具链：Icarus Verilog + GTKWave（开源仿真）

| 工具 | 用途 | Windows 命令 |
|------|------|-------------|
| `iverilog` | Verilog 编译 | `iverilog -o out.vvp source.v tb.v` |
| `vvp` | 仿真执行 | `vvp out.vvp` |
| `gtkwave` | 波形查看 | `gtkwave dump.vcd` |

### 工具检测优先级

```
1. where iverilog          ← 检查 PATH
2. C:\iverilog\bin\        ← 常见安装路径
3. %USERPROFILE%\scoop\shims\iverilog.exe  ← scoop 安装
```

### 编译命令标准模板

```bash
# 单文件编译+仿真
iverilog -g2012 -o %RUN_DIR%/artifacts/sim.vvp src.v tb.v
vvp %RUN_DIR%/artifacts/sim.vvp

# 多文件编译（使用文件列表）
iverilog -g2012 -o %RUN_DIR%/artifacts/sim.vvp -f filelist.txt

# 仅语法检查（不生成输出）
iverilog -g2012 -t null src.v
```

**关键参数：**
- `-g2012`：启用 SystemVerilog 2012 语法支持（推荐默认使用）
- `-Wall`：启用所有警告
- `-o`：输出文件必须放在 `artifacts/` 目录

---

## Verilog RTL 编写规范

### 文件组织

```
artifacts/
├── <module_name>.v          # RTL 源文件（每个模块一个文件）
├── tb_<module_name>.v       # 对应的 testbench
├── <module_name>_sim.vvp    # 编译产物
├── <module_name>.vcd        # 波形文件（受控大小）
└── sim_result.log           # 仿真文本输出
```

### 命名约定

| 类型 | 命名规则 | 示例 |
|------|---------|------|
| 模块名 | 小写 + 下划线 | `uart_tx`, `spi_master` |
| Testbench 模块名 | `tb_` 前缀 | `tb_uart_tx` |
| 时钟信号 | `clk` 或 `clk_<freq>` | `clk`, `clk_50m` |
| 复位信号 | `rst_n`（低有效）/ `rst`（高有效） | `rst_n` |
| 参数 | 全大写 + 下划线 | `BAUD_DIV`, `DATA_WIDTH` |
| 内部信号 | 小写 + 下划线 | `tx_busy`, `rx_data_valid` |

### RTL 编写要求

1. **必须使用参数化设计**：关键数值（位宽、分频系数、FIFO 深度等）用 `parameter` 定义
2. **时钟域显式标注**：跨时钟域信号必须注释说明
3. **复位逻辑统一**：同一模块内复位极性必须一致
4. **端口声明清晰**：每个端口必须注释功能说明
5. **避免 latch 推断**：组合逻辑 `always @(*)` 中所有分支必须对所有输出赋值
6. **时序逻辑和组合逻辑注意区分**：时序逻辑的赋值注意用"<=" 而避免用"=",除非非常有必要。注意对于时序逻辑，当前赋值要到下一拍才会在波形上体现。
7. **在编写时，心中对时序要有数**：确保自己能想到所编写逻辑的时序。
8. 希望对某一个信号延迟一个时钟，可以采用“打一拍”的方法，就是通过赋值的方法延迟一个时钟。
9. **计数器是时序逻辑的基础**。通过计数器可以实现复杂的时序控制。
10.**如果有复杂状态，需要考虑状态机**。
11.**异步复位要小心**：需要评估复位信号是异步还是同步（或同源）时钟。最好现将异步复位信号转换成当前相同时钟域的，作为当前时钟域复位信号。


### RTL 模块模板

```verilog
`timescale 1ns/1ps

module module_name #(
    parameter DATA_WIDTH = 8,
    parameter DEPTH      = 16
)(
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire [DATA_WIDTH-1:0] din,
    output reg  [DATA_WIDTH-1:0] dout,
    output wire                  valid
);

    // 内部信号声明
    reg [DATA_WIDTH-1:0] data_reg;

    // 时序逻辑
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            data_reg <= {DATA_WIDTH{1'b0}};
        end else begin
            data_reg <= din;
        end
    end

    assign dout  = data_reg;
    assign valid = |data_reg;

endmodule
```

---

## Testbench 编写规范（重点）

### ⚠️ 核心原则：仿真必须在有限时间内终止

**所有 testbench 必须满足以下三条铁律：**

1. **必须有全局超时保护**（`$finish` 兜底）
2. **所有等待循环必须有超时计数器**
3. **仿真参数必须针对仿真加速**（不能用真实硬件参数）

### 铁律 1：全局超时保护（强制）

每个 testbench **必须包含**一个独立的超时 `initial` 块：

```verilog
// ===== 全局仿真超时保护（必须有）=====
initial begin
    #(MAX_SIM_TIME);
    $display("ERROR: Simulation timeout at %0t!", $time);
    $finish(2);
end
```

**`MAX_SIM_TIME` 计算公式：**

```
MAX_SIM_TIME = 预期测试时间 × 3（安全余量）

示例：
- 简单组合逻辑测试：MAX_SIM_TIME = 10_000（10us）
- 时序逻辑（几十个周期）：MAX_SIM_TIME = 100_000（100us）
- UART 发送 1 字节（加速后）：MAX_SIM_TIME = 500_000（500us）
- UART 发送 4 字节（加速后）：MAX_SIM_TIME = 2_000_000（2ms）
```

**硬性上限：`MAX_SIM_TIME` 不得超过 `50_000_000`（50ms 仿真时间）。** 若超过，必须优化仿真参数。

### 铁律 2：等待循环必须有超时

```verilog
// ❌ 禁止：无限等待循环
while (!tx_done) @(posedge clk);

// ✅ 正确：带超时的等待循环
begin : wait_tx_done_block
    integer wait_cnt;
    wait_cnt = 0;
    while (!tx_done && wait_cnt < TIMEOUT_CYCLES) begin
        @(posedge clk);
        wait_cnt = wait_cnt + 1;
    end
    if (wait_cnt >= TIMEOUT_CYCLES) begin
        $display("ERROR: Timeout waiting for tx_done at %0t", $time);
        $finish(2);
    end
end
```

**或者用 `task` 封装：**

```verilog
task wait_signal;
    input signal;
    input integer max_cycles;
    integer cnt;
    begin
        cnt = 0;
        while (!signal && cnt < max_cycles) begin
            @(posedge clk);
            cnt = cnt + 1;
        end
        if (cnt >= max_cycles)
            $display("WARN: wait_signal timeout after %0d cycles", max_cycles);
    end
endtask
```

### 铁律 3：仿真加速参数（关键！）

**对于通信协议（UART/SPI/I2C 等），仿真中必须用缩小的分频系数，禁止使用真实硬件参数。**

```verilog
// ❌ 禁止：使用真实波特率参数（仿真耗时爆炸）
parameter BAUD_DIV = 5208;    // 50MHz / 9600 = 5208
// 发送 1 字节需要 10 × 5208 = 52080 个时钟周期！
// 发送 16 字节需要 833,280 个周期，仿真输出可能达到 GB 级

// ✅ 正确：仿真时缩小分频系数（加速 50 倍以上）
parameter SIM_BAUD_DIV = 100;    // 仿真用，每 bit 仅 100 个时钟周期
// 发送 1 字节仅需 1000 个周期
// 发送 16 字节仅需 16000 个周期

// 在 DUT 实例化时通过参数覆盖：
uart_controller #(.BAUD_DIV(SIM_BAUD_DIV)) u_dut ( ... );
```

**仿真加速参数对照表：**

| 模块类型 | 真实参数 | 仿真推荐参数 | 加速比 |
|---------|---------|------------|-------|
| UART 9600 @ 50MHz | BAUD_DIV=5208 | SIM_BAUD_DIV=50~100 | 50~100× |
| UART 115200 @ 50MHz | BAUD_DIV=434 | SIM_BAUD_DIV=20~50 | 9~22× |
| SPI @ 1MHz / 50MHz | CLK_DIV=50 | SIM_CLK_DIV=4~8 | 6~12× |
| I2C @ 100kHz / 50MHz | CLK_DIV=500 | SIM_CLK_DIV=10~20 | 25~50× |
| PWM 1kHz @ 50MHz | PERIOD=50000 | SIM_PERIOD=100~500 | 100~500× |
| 去抖动 10ms @ 50MHz | DEBOUNCE=500000 | SIM_DEBOUNCE=50~100 | 5000~10000× |

### $display 输出控制

```verilog
// ❌ 禁止：在 always 块或高频循环中无条件打印
always @(posedge clk) begin
    $display("clk tick: data=%h", data);  // 每个周期打印一行！
end

// ✅ 正确：仅在关键事件时打印
always @(posedge clk) begin
    if (data_valid && !prev_data_valid)  // 仅在上升沿打印
        $display("T=%0t: New data received: %h", $time, data);
end

// ✅ 正确：使用条件编译控制详细日志
`ifdef VERBOSE
    $display("DEBUG: internal_state=%b", state);
`endif
```

**$display 数量限制：** 单次仿真总输出行数应控制在 **500 行以内**。若需更多调试信息，使用 VCD 波形而非文本输出。

### VCD 波形文件控制

```verilog
// ❌ 禁止：无限制 dump 所有信号
initial begin
    $dumpfile("dump.vcd");
    $dumpvars(0, tb_top);       // dump 整棵层次树，且永不停止
end

// ✅ 正确：限制 dump 时间范围
initial begin
    $dumpfile("dump.vcd");
    $dumpvars(0, tb_top);
    #(VCD_DUMP_DURATION);       // 只 dump 前 N 个时间单位
    $dumpoff;
    $display("VCD dump stopped at %0t to save disk space", $time);
end

// ✅ 更好：只 dump 关心的信号
initial begin
    $dumpfile("dump.vcd");
    $dumpvars(1, tb_top);              // 仅顶层信号
    $dumpvars(1, tb_top.u_dut);        // 仅 DUT 顶层
    #(VCD_DUMP_DURATION);
    $dumpoff;
end
```

**VCD 时间限制推荐值：**

| 测试规模 | `VCD_DUMP_DURATION` |
|---------|-------------------|
| 简单组合逻辑 | 5_000（5us） |
| 小型时序测试 | 50_000（50us） |
| 通信协议（加速后） | 200_000（200us） |
| 最大允许值 | 1_000_000（1ms） |

### Testbench 标准模板

```verilog
`timescale 1ns/1ps

module tb_module_name;

    // ===== 仿真控制参数 =====
    parameter CLK_PERIOD    = 20;       // 50MHz → 20ns
    parameter SIM_SPEEDUP   = 100;      // 仿真加速用的分频系数
    parameter MAX_SIM_TIME  = 2_000_000;// 全局超时：2ms
    parameter VCD_DURATION  = 500_000;  // VCD 记录时长：500us
    parameter TIMEOUT_CYCLES = 10_000;  // 等待循环超时

    // ===== 信号声明 =====
    reg         clk;
    reg         rst_n;
    // ... 其他信号

    // ===== DUT 实例化（使用仿真加速参数）=====
    module_name #(
        .BAUD_DIV(SIM_SPEEDUP)
    ) u_dut (
        .clk(clk),
        .rst_n(rst_n)
        // ... 其他端口
    );

    // ===== 时钟生成 =====
    initial clk = 0;
    always #(CLK_PERIOD/2) clk = ~clk;

    // ===== 全局超时保护（必须有）=====
    initial begin
        #(MAX_SIM_TIME);
        $display("ERROR: Global simulation timeout at %0t!", $time);
        $display("TEST FAILED (timeout)");
        $finish(2);
    end

    // ===== VCD 波形控制 =====
    initial begin
        $dumpfile("module_name.vcd");
        $dumpvars(1, tb_module_name);
        $dumpvars(1, tb_module_name.u_dut);
        #(VCD_DURATION);
        $dumpoff;
    end

    // ===== 测试主体 =====
    initial begin
        // 初始化
        rst_n = 0;
        // ... 其他初始化

        // 复位
        #(CLK_PERIOD * 5);
        rst_n = 1;
        #(CLK_PERIOD * 5);

        $display("========== Test Started ==========");

        // ... 测试激励（数据量控制在必要最少量）

        $display("========== Test Completed ==========");
        $display("TEST PASSED");
        $finish(0);
    end

endmodule
```

---

## 仿真执行规范

### 执行流程（标准步骤）

```
步骤1：编写/修改 RTL 和 Testbench
步骤2：语法检查     → iverilog -g2012 -t null <files>
步骤3：编译         → iverilog -g2012 -o artifacts/sim.vvp <files>
步骤4：仿真执行     → vvp artifacts/sim.vvp（设置超时，见下文）
步骤5：检查结果     → 读取输出，确认 PASS/FAIL
步骤6：（可选）查看波形 → gtkwave artifacts/dump.vcd
```

### vvp 执行超时保护

**vvp 命令必须在 shell 层面设置超时，防止仿真卡死：**

```powershell
# 使用 shell 工具执行时，设置 block_until_ms 超时
# 小型测试：30秒（默认）
# 中型测试：60秒
# 大型测试：120秒（绝对上限）
```

**如果 vvp 执行超过 60 秒未返回，判定为仿真挂死，必须：**
1. 终止进程
2. 检查 testbench 是否有无限循环
3. 检查仿真加速参数是否合理
4. 修复后重新执行

### 仿真输出量控制

**vvp 输出行数硬性上限：1000 行。** 如果仿真输出超过这个量级：
- 检查是否有循环中的 `$display`
- 检查是否 `$monitor` 未及时关闭
- 将详细信息改为写入 VCD，仅在文本中输出关键节点

### 测试数据量控制

| 测试类型 | 推荐数据量 | 最大数据量 |
|---------|----------|----------|
| 功能验证（基本） | 4~8 个测试向量 | 16 个 |
| 边界测试 | 全 0 / 全 1 / 边界值 | 8 个 |
| 随机测试 | 8~16 个随机数据 | 32 个 |
| 通信协议（UART 等）| 2~4 字节 | 8 字节 |

**原则：验证功能正确性所需的最少数据量，不是越多越好。**

---

## 仿真常见陷阱与防护

### 陷阱 1：分频系数过大

```
症状：vvp 执行几分钟不返回，VCD 文件持续增长到 GB 级
原因：使用了真实硬件的分频系数（如 BAUD_DIV=5208）
修复：用仿真加速参数（BAUD_DIV=50~100）
```

### 陷阱 2：无限等待循环

```
症状：仿真永不结束，无输出
原因：while(!signal) 等待的信号永远不会变化（逻辑错误/连线错误）
修复：所有 while 循环必须有超时计数器 + 全局 $finish 兜底
```

### 陷阱 3：$monitor 未关闭

```
症状：输出行数暴增，每个时钟周期都打印
原因：$monitor 会在任何被监控信号变化时打印
修复：用 $display 替代，或在不需要时调用 $monitoroff
```

### 陷阱 4：VCD dump 范围过大

```
症状：VCD 文件数百 MB 到数 GB
原因：$dumpvars(0, top) dump 了整棵层次树的所有信号
修复：只 dump 关心的层次 + 设置 $dumpoff 时间限制
```

### 陷阱 5：组合逻辑环路

```
症状：仿真时间不推进，CPU 100%
原因：assign a = b; assign b = a; 形成组合逻辑振荡
修复：检查 RTL 中的组合逻辑反馈路径
```

---

## 仿真结果判定标准

### 输出格式要求

每个 testbench 的输出必须包含明确的 **PASS/FAIL 结论**：

```
========== Test Started ==========
... （关键检查点输出）...
========== Test Completed ==========
TEST PASSED                    ← 或 TEST FAILED
```

### $finish 退出码约定

| 退出码 | 含义 |
|-------|------|
| `$finish(0)` | 测试通过 |
| `$finish(1)` | 测试失败（功能错误） |
| `$finish(2)` | 测试异常（超时/基础设施问题） |

### Agent 判定逻辑

```
仿真输出包含 "TEST PASSED" 且 $finish(0) → 报告成功
仿真输出包含 "TEST FAILED"             → 分析失败原因，修复 RTL 或 TB
仿真输出包含 "timeout"                 → 检查仿真参数/无限循环
vvp 超过 60 秒未返回                   → 强制终止，检查 TB
编译报错                              → 修复语法错误后重试
```

---

## FPGA 开发任务执行清单

当收到一个 Verilog 开发任务时，按以下顺序执行：

```
1. 理解需求 → 确认模块功能、接口、时序要求
2. 设计 RTL
   - 确定参数（位宽、分频系数等）
   - 编写模块代码，确保参数化
3. 编写 Testbench
   ✅ 加仿真加速参数
   ✅ 加全局超时保护
   ✅ 所有等待循环加超时
   ✅ 控制 $display 输出量
   ✅ 控制 VCD dump 范围和时长
   ✅ 控制测试数据量（最少必要量）
4. 编译
   - iverilog -g2012 -Wall 编译，修复所有 warning
5. 仿真
   - vvp 执行（设 shell 超时）
   - 检查输出：PASS/FAIL
6. 迭代修复
   - 若 FAIL：分析原因 → 修复 → 重新编译仿真
   - 若超时：检查仿真参数 → 修复 → 重新执行
7. 交付
   - 所有文件放在 artifacts/
   - 报告测试结果
```
