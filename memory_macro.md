# 宏观工作记忆

## 联网搜索与信息获取
集成 web_search、DDGS 元搜索库（ddgs），掌握 agent-reach CLI 工具栈（agent_reach_search/read），支持 exa/reddit/youtube/bilibili 等渠道；Twitter 通道限于搜索读取，无发帖能力。

## 工具开发与 Windows 适配
熟悉 Windows GBK 编码处理（subprocess + UTF-8 重定向），排查过 httpx socks 代理解析异常问题。

## 记忆与文件索引
集成 fff.nvim（MCP 协议），实现 fff_memory_search 工具，支持 grep/find_files/multi_grep 模式。

## Agent 运行机制
具备 JSON 解析失败自动翻倍 max_tokens 重试（4096→32768）、context 超限自动裁剪压缩机制。

## 无线网络分析
了解 WiFi 四指标体系（RSSI/噪声/SNR/信号条），通过树莓派 /proc/net/wireless 实测验证。
