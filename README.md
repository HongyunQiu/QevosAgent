# 通用极简自进化智能体

一个只有 ~400 行核心代码的通用自主智能体框架。

## 设计原则

- **极简**：去掉所有框架魔法，每一行都有明确职责
- **通用**：不针对特定应用，工具集完全可替换
- **透明**：完整的推理过程可见，便于调试
- **可进化**：运行时动态注册新工具，经验跨次运行积累

---

## 文件结构

```
agent/
├── __init__.py          ← 公共 API 入口，含 Agent 高层封装
├── core/
│   ├── types.py         ← 数据契约（Action, ToolSpec, AgentState...）
│   ├── llm.py           ← LLM 接口层（后端抽象 + Prompt构建 + 响应解析）
│   ├── executor.py      ← 工具执行器（安全执行 + 异常捕获）
│   └── loop.py          ← 主循环（感知→思考→行动→反思）
└── tools/
    └── standard.py      ← 内置标准工具集
```

---

## 核心循环（每次迭代）

```
┌─────────────────────────────────────┐
│  1. build_system_prompt(state)      │  ← 工具集变化后自动更新
│  2. llm.complete(messages, system)  │  ← 调用 LLM
│  3. parse_response(raw)             │  ← 解析为 Action
│  4. execute(action, state)          │  ← 执行工具
│  5. 更新 state.short_term           │  ← 结果反馈给 LLM
└─────────────────────────────────────┘
           ↕ 重复直到 DONE 或超限
```

---

## 记忆架构

| 类型 | 存储位置 | 生命周期 | 用途 |
|------|---------|---------|------|
| 短期记忆 | `state.short_term` | 当次运行 | LLM 对话历史 |
| 长期记忆 | `state.long_term` | 跨次运行（手动持久化）| 经验、结论 |

---

## 进化机制

智能体通过内置的 `register_tool` 工具实现自我扩展：

1. LLM 发现现有工具无法完成某个操作
2. 调用 `register_tool`，提供工具名称、描述和 Python 代码
3. 新工具注册到 `state.tools`
4. **下一轮的 system prompt 自动包含新工具**（因为 prompt 在每轮重新构建）
5. 工具注册事件写入长期记忆，供未来参考

---

## 快速开始

```python
from agent import Agent

agent = Agent(backend="openai", api_key="sk-...")
agent.run("你的目标")
```

### run_goal.py 切换本地模型

```bash
# 默认：oss120b
python run_goal.py "帮我总结这个项目"

# 切到 qwen3527dgx（ZeroTier 内网）
OPENAI_PROFILE=qwen3527dgx python run_goal.py "帮我总结这个项目"
```

## 自定义工具

```python
from agent import Agent
from agent.core.types import ToolSpec, ToolResult

def my_tool(state, param1: str) -> ToolResult:
    # 工具逻辑
    return ToolResult(success=True, output="结果")

agent = Agent(backend="openai", api_key="sk-...")
agent.add_tool(ToolSpec(
    name="my_tool",
    description="这个工具做什么",
    args_schema={"param1": "参数说明"},
    fn=my_tool,
))
agent.run("使用 my_tool 完成某件事")
```

## 切换 LLM 后端

```python
# OpenAI
agent = Agent(backend="openai", model="gpt-4o", api_key="...")

# Anthropic
agent = Agent(backend="anthropic", model="claude-opus-4-6", api_key="...")

# 完全自定义后端（实现 LLMBackend 接口）
from agent.core.llm import LLMBackend
class MyBackend(LLMBackend):
    def complete(self, messages, system) -> str:
        ...  # 调用任意 LLM API
```

## 依赖

```
openai>=1.0      # 使用 OpenAI 后端时需要
anthropic>=0.20  # 使用 Anthropic 后端时需要
```

标准库（`subprocess`, `pathlib`, `json`）满足其他所有需求。
