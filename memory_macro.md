# 宏观工作记忆

## 联网搜索与信息获取
集成 web_search、DDGS 元搜索库（ddgs），掌握 agent-reach CLI 工具栈（agent_reach_search/read），支持 exa/reddit/youtube/bilibili 等渠道；Twitter 通道限于搜索读取，无发帖能力。

## 工具开发与 Windows 适配
熟悉 Windows GBK 编码处理（subprocess + UTF-8 重定向），排查过 httpx socks 代理解析异常问题。
Windows OpenSSH 客户端传递含双引号的命令时会出现引号嵌套问题，导致远程 shell 语法错误；使用 base64 编码传输脚本可完全避免特殊字符转义问题。

## 记忆与文件索引
集成 fff.nvim（MCP 协议），实现 fff_memory_search 工具，支持 grep/find_files/multi_grep 模式。

## Agent 运行机制
具备 JSON 解析失败自动翻倍 max_tokens 重试（4096→32768）、context 超限自动裁剪压缩机制。

## 无线网络分析
了解 WiFi 四指标体系（RSSI/噪声/SNR/信号条），通过树莓派 /proc/net/wireless 实测验证。

## 大模型量化与显存需求
掌握 MiniMax M2.7 量化模型信息：BF16 原始模型 426GB，4bit 量化（MXFP4_MOE）压缩至 127GB（3.36x），显存需求从 473GB 降至 143GB，将 8 卡 A100 降至 2 卡；1bit 极致量化（UD-IQ1_M）仅需 57GB（7.54x 压缩比）。MXFP4_MOE 为 MoE 架构专用 4bit 格式，性能/显存平衡最佳。通过 Hugging Face API 获取真实文件大小数据。

## Agent 评测基准
SWE-bench 通过真实 GitHub Issue 解决能力评估 Agent 的软件开发水平。
LiveBench 作为 ICLR 2025 亮点论文，提供实时更新的动态评测基准。
HuggingFace Open LLM Leaderboard 和 OpenCompass 司南提供综合性大模型评测。
通用评测基准包括 MMLU、MATH、GPQA、IFEval 等知识推理类，以及 HumanEval、MBPP 等代码类。
