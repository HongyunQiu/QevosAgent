<p align="center">
  <img src="./assets/QevosAgent.png" alt="QevosAgent banner" width="100%" />
</p>

# QevosAgent

[![Stars](https://img.shields.io/github/stars/HongyunQiu/QevosAgent?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/HongyunQiu/QevosAgent?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent/commits/main)
[![OpenAI Compatible](https://img.shields.io/badge/OpenAI-Compatible-412991?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent)
[![Anthropic](https://img.shields.io/badge/Anthropic-Supported-black?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent)
[![Web Dashboard](https://img.shields.io/badge/Web-Dashboard-0A7CFF?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent)
[![Tool Repair](https://img.shields.io/badge/Tool-Repair-orange?style=for-the-badge)](https://github.com/HongyunQiu/QevosAgent)

🇺🇸 **English** (EN) • 🇨🇳 **中文** (ZH)

---

## English

**🦊 Your Local AI Agent, Ready Out of the Box**

A local AI agent truly designed for everyone — native Windows/macOS/Linux installation, no WSL required, works out of the box.

### 🌟 Why QevosAgent?

#### 🪟 Cross-Platform Native Experience

Unlike other AI Agents, **QevosAgent was designed for desktop users from day one**:

- ✅ **Windows/macOS/Linux native installer** — one-click install, no WSL, no Docker
- ✅ **Local model first** — supports Qwen3, Gemma4 and other open-source models, zero API cost
- ✅ **Data privacy** — all data stays on your machine, never leaked
- ✅ **Ready out of the box** — up and running in 5 minutes after download

#### 🎯 AI Assistant for Everyone

QevosAgent is not just a tool for developers:

- **Office tasks** — auto-organize files, process data, generate reports
- **Daily tasks** — search info, summarize documents, translate content
- **Creative work** — generate web pages, design charts, write code
- **System admin** — monitor disk, clean space, manage remote servers

#### 💡 Core Advantages

- **Persistent memory** — resume tasks after interruption, no lost progress on long projects
- **Self-evolving tools** — Agent can automatically repair and create new tools
- **Full observability** — every action is recorded, browse execution history anytime
- **Free and open source forever** — MIT license, no commercial restrictions

### ⬇️ Free Download

#### Desktop App (Recommended) — One-click install, easiest option

| Platform | Download |
| --- | --- |
| 🪟 **Windows** | [Download Windows Installer](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🍎 **macOS Apple Silicon** | [Download macOS ARM](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🍎 **macOS Intel** | [Download macOS Intel](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🐧 **Linux** | [Download Linux AppImage](https://github.com/HongyunQiu/QevosAgent/releases/latest) |

<p align="center">
  <img src="./assets/simpleAgent_dashboard.gif" alt="QevosAgent Demo" width="100%" />
</p>

#### Install from Source

```powershell
git clone https://github.com/HongyunQiu/QevosAgent.git
cd QevosAgent
pip install -r requirements.txt
copy .env.example .env
# Edit .env and fill in your API Key

# Run a task from the command line
python run_goal.py "your task here"

# Launch the web dashboard
cd dashboard
npm install
cd ..
node dashboard/server.js
```

### ✨ Core Capabilities

#### 🧬 Self-Evolving Tools

Any capability can be registered as a tool. Through tool evolution, you can massively expand the Agent's abilities — the possibilities are limitless.

#### 💾 Persistent Run Artifacts

Every task is automatically saved to disk — logs, scratchpads, summaries and final answers, auditable at any time.

#### 🔄 Snapshot Resume

Memory persists across sessions. The Agent picks up right where it left off — the more you use it, the smarter it gets.

#### 🔧 Auto Tool Repair

When a tool fails, the Agent automatically diagnoses and fixes it — no infinite loops, no deadlocks.

#### 🖥️ Web Dashboard

Launch tasks, inject commands, and browse history from your browser — fully visual.

#### 🤖 Local Model First

Deep support for Qwen3, Gemma4, and any OpenAI-compatible endpoint. Zero API cost, data stays on your machine.

#### 🛠️ 30+ Built-in Tools

File read/write, Python execution, shell commands, web search, memory management — all ready out of the box.

#### 🧠 Advanced Advisor Module

A dedicated high-level advisor LLM provides strategic guidance at critical moments, keeping the Agent on track.

### 🆚 Comparison with Other AI Agents

| Feature | QevosAgent | OpenClaw | Hermes | OpenCode |
| --- | --- | --- | --- | --- |
| **Desktop App Download** | ✅ One-click | ❌ | ❌ | ❌ |
| **Windows Native** | ✅ Full support | ❌ Requires WSL | ⚠️ Partial | ❌ Linux-first |
| **Setup Difficulty** | ⭐ Easy | ⭐⭐⭐ Complex | ⭐⭐ Medium | ⭐⭐⭐ Complex |
| **Target Users** | Everyone, office-focused | Requires some expertise | Requires some expertise | Developers |

**QevosAgent's unique positioning**:

- **OpenClaw/Hermes** — aimed at users with programming or ops experience, require Linux/WSL
- **OpenCode** — focused on code generation as a programming assistant
- **QevosAgent** — **local AI assistant for everyone**, handles everything from daily office work to professional development

### 💼 Use Cases

#### 👨‍💻 Developers

Auto-analyze codebases, generate documentation, run shell scripts — let the Agent handle the tedious work.

#### 🔬 Researchers

Auto-collect references, run Python analyses, generate fully documented research reports.

#### ⚙️ Automation Engineers

Run background shell tasks, batch file processing, workflow automation — no manual supervision needed.

#### 📊 Data Analysts

Let AI run Python scripts, process datasets, generate charts — fully auditable and reproducible.

#### 🎓 Students & Learners

Q&A, paper assistance, resource organization — AI accompanies your learning and remembers your knowledge base.

#### 🏢 Enterprise / Private Deployment

Connect local models, data never leaves the network — low-cost private AI Agent for your team.

### 🚀 Quick Start

#### 1. Install Python

Make sure Python 3.10 or higher is installed:

```powershell
python --version
```

#### 2. Clone the Repository

```powershell
git clone https://github.com/HongyunQiu/QevosAgent.git
cd QevosAgent
```

#### 3. Install Dependencies

```powershell
pip install -r requirements.txt
```

#### 4. Configure API Key

```powershell
copy .env.example .env
```

Edit the `.env` file and set your API key (or use a local model for zero cost).

#### 5. Run Your First Task

```powershell
python run_goal.py "Summarize today's news for me"
```

#### 6. Launch the Dashboard (Optional)

```powershell
cd dashboard
npm install
npm start
```

Open [http://localhost:3000](http://localhost:3000) in your browser to monitor task execution in real time.

### 🎬 Demo

The dashboard is included in the repository — launch tasks, stop tasks, inject commands, inspect history, and browse run artifacts.

### 📖 More Documentation

- [Quick Start](https://qevos.ai/quickstart) — detailed installation and usage guide
- [Official Website](https://qevos.ai) — product introduction, feature demos, downloads
- [Contributing Guide](CONTRIBUTING.md) — how to participate in the project

### 📜 License

This project is licensed under the [MIT](https://opensource.org/licenses/MIT) License — free and open source forever.

### 🙏 Acknowledgements

Thanks to all users and contributors!

If QevosAgent has been helpful to you, please give it a ⭐ Star!

---

**QevosAgent** — Local First · Private · Ready Out of the Box

[back to top](#qevosagent)

---

## 中文

**🦊 你的本地 AI 智能体，开箱即用**

一个真正为所有人设计的本地 AI 智能体——支持 Windows/macOS/Linux 原生安装，无需 WSL，开箱即用。

### 🌟 为什么选择 QevosAgent？

#### 🪟 跨平台原生体验

与其他 AI Agent 不同，**QevosAgent 从第一天起就为桌面用户设计**：

- ✅ **Windows/macOS/Linux 原生安装器** — 一键安装，无需 WSL、无需 Docker
- ✅ **本地模型优先** — 支持 Qwen3、Gemma4 等本地开源模型，零 API 成本
- ✅ **数据隐私** — 所有数据留在你的机器上，永不外泄
- ✅ **开箱即用** — 下载安装后，5 分钟即可开始使用

#### 🎯 面向所有人的 AI 助手

QevosAgent 不是只给程序员用的工具：

- **办公场景** — 自动整理文件、处理数据、生成报告
- **日常任务** — 搜索信息、总结文档、翻译内容
- **创意工作** — 生成网页、设计图表、编写代码
- **系统管理** — 监控磁盘、清理空间、远程服务器管理

#### 💡 核心优势

- **持久化记忆** — 任务中断后可恢复，长期项目不再丢失进度
- **工具自我进化** — Agent 可以自动修复和创建新工具
- **完整可观测** — 每个操作都有记录，随时查看执行历史
- **永久免费开源** — MIT 协议，无商业限制

### ⬇️ 免费下载

#### 桌面程序（推荐）一键安装，最为方便

| 平台 | 下载 |
| --- | --- |
| 🪟 **Windows** | [下载 Windows 安装器](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🍎 **macOS Apple Silicon** | [下载 macOS ARM 版](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🍎 **macOS Intel** | [下载 macOS Intel 版](https://github.com/HongyunQiu/QevosAgent/releases/latest) |
| 🐧 **Linux** | [下载 Linux AppImage](https://github.com/HongyunQiu/QevosAgent/releases/latest) |

#### 源码安装

```powershell
git clone https://github.com/HongyunQiu/QevosAgent.git
cd QevosAgent
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env 填入 API Key

# 命令行运行测试
python run_goal.py "你的任务"

# 使用 Dashboard 看板
cd dashboard
npm install
cd ..
node dashboard/server.js
```

### ✨ 核心能力

#### 🧬 工具自我进化

任何能力都可以注册为工具，通过工具进化可以大幅度扩展 Agent 能力，具有无限可能性。

#### 💾 持久化运行产物

每个任务自动保存到磁盘——日志、草稿本、摘要和最终答案，随时可审计。

#### 🔄 快照恢复

记忆跨会话持久化。Agent 从上次中断处继续——用得越多，越聪明。

#### 🔧 自动工具修复

工具失败时，Agent 自动诊断并修复——无无限循环，无卡死。

#### 🖥️ Web Dashboard

从浏览器启动任务、注入命令、浏览历史记录——完全可视化。

#### 🤖 本地模型优先

深度支持 Qwen3、Gemma4 及任何 OpenAI 兼容端点。零 API 成本，数据留在本机。

#### 🛠️ 30+ 内置工具

文件读写、Python 执行、Shell 命令、网络搜索、记忆管理——开箱即用。

#### 🧠 高级指导员模块

独立的高级指导员 LLM 在关键时刻提供战略指导，确保 Agent 不偏航。

### 🆚 与其他 AI Agent 的对比

| 特性 | QevosAgent | OpenClaw | Hermes | OpenCode |
| --- | --- | --- | --- | --- |
| **桌面程序下载** | ✅ 一键安装 | ❌ | ❌ | ❌ |
| **Windows 原生** | ✅ 完美支持 | ❌ 需要 WSL | ⚠️ 部分支持 | ❌ Linux 优先 |
| **安装难度** | ⭐ 简单 | ⭐⭐⭐ 复杂 | ⭐⭐ 中等 | ⭐⭐⭐ 复杂 |
| **目标用户** | 所有人，面向办公场景 | 需要一定基础 | 需要一定基础 | 程序员 |

**QevosAgent 的独特定位**：

- **OpenClaw/Hermes** — 面向有一定程序或者运维基础的用户，需要 Linux/WSL 环境
- **OpenCode** — 专注于代码生成的编程助手
- **QevosAgent** — **面向所有人的本地 AI 助手**，从日常办公到专业开发都能胜任

### 💼 使用场景

#### 👨‍💻 开发者

自动分析代码库、生成文档、运行 Shell 脚本——让 Agent 处理繁琐工作。

#### 🔬 研究人员

自动收集参考文献、运行 Python 分析、生成完整记录的研究报告。

#### ⚙️ 自动化工程师

运行后台 Shell 任务、批量文件处理、工作流自动化——无需人工监督。

#### 📊 数据分析师

让 AI 运行 Python 脚本、处理数据集、生成图表——完全可审计、可复现。

#### 🎓 学生与学习者

问答、论文辅助、资源整理——AI 陪伴学习并记住你的知识库。

#### 🏢 企业/私有部署

连接本地模型，数据永不离开网络——为团队提供低成本私有 AI Agent。

### 🚀 快速开始

#### 1. 安装 Python

确保已安装 Python 3.10 或更高版本：

```powershell
python --version
```

#### 2. 克隆仓库

```powershell
git clone https://github.com/HongyunQiu/QevosAgent.git
cd QevosAgent
```

#### 3. 安装依赖

```powershell
pip install -r requirements.txt
```

#### 4. 配置 API Key

```powershell
copy .env.example .env
```

编辑 `.env` 文件，设置你的 API 密钥（或使用本地模型实现零成本）。

#### 5. 运行第一个任务

```powershell
python run_goal.py "帮我总结一下今天的新闻"
```

#### 6. 启动 Dashboard（可选）

```powershell
cd dashboard
npm install
npm start
```

在浏览器打开 [http://localhost:3000](http://localhost:3000)，即可实时监控任务执行。

### 🎬 演示

Dashboard 已包含在仓库中，可以启动任务、停止任务、注入命令、检查历史、浏览运行产物。

### 📖 更多文档

- [快速入门](https://qevos.ai/quickstart) — 详细安装和使用指南
- [官网](https://qevos.ai) — 产品介绍、功能演示、下载
- [贡献指南](CONTRIBUTING.md) — 如何参与项目开发

### 📜 许可证

本项目采用 [MIT](https://opensource.org/licenses/MIT) 许可证 — 永久免费开源。

### 🙏 致谢

感谢所有使用者和贡献者！

如果 QevosAgent 对你有帮助，请给一个 ⭐ Star！

---

**QevosAgent** — 本地优先 · 数据私有 · 开箱即用  
让 AI 成为你的智能助手，而不仅仅是开发工具。

[back to top](#qevosagent)
