"""
使用示例集合
演示智能体的不同使用场景和进化能力。
"""

import sys
import os
# Ensure repo root is on PYTHONPATH so `import agent` works when running this file directly.
sys.path.insert(0, os.path.dirname(__file__))


# ══════════════════════════════════════════════════════
# 示例 1：最简使用
# ══════════════════════════════════════════════════════

def example_minimal():
    """最少代码启动智能体。"""
    from agent import Agent

    agent = Agent(
        backend="openai",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    agent.run("计算 1 到 100 的所有质数之和，并解释算法")


# ══════════════════════════════════════════════════════
# 示例 2：工具进化演示
# ══════════════════════════════════════════════════════

def example_evolution():
    """
    演示智能体如何自主创建新工具。
    目标包含 HTTP 请求任务，但初始工具集没有网络工具。
    智能体应该自主创建一个 http_get 工具。
    """
    from agent import Agent

    agent = Agent(
        backend="openai",
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    agent.run(
        "请获取 https://httpbin.org/json 的内容，解析并总结其中的数据。"
        "提示：你可以使用 register_tool 工具来创建一个 HTTP 请求工具。"
    )


# ══════════════════════════════════════════════════════
# 示例 3：预置领域知识（跨次运行经验复用）
# ══════════════════════════════════════════════════════

def example_with_domain_knowledge():
    """为智能体预注入领域知识，使其在特定领域表现更好。"""
    from agent import Agent

    agent = Agent(
        backend="anthropic",
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        long_term=[
            "ZEMAX ZOS-API 使用 .NET 接口，通过 pythonnet 在 Python 中调用",
            "优化时优先控制色差，其次控制像散，最后调整场曲",
            "F2 玻璃在蓝光波段（420-440nm）表现良好",
        ]
    )
    agent.run("分析三片式场平正器的最优玻璃组合策略")


# ══════════════════════════════════════════════════════
# 示例 4：手动组装（完全控制）
# ══════════════════════════════════════════════════════

def example_manual_assembly():
    """
    绕过 Agent 高层封装，手动组装所有组件。
    适合需要深度定制的场景。
    """
    from agent.core import run, console_hooks, OpenAIBackend
    from agent.core.types_def import ToolSpec, ToolResult
    from agent.tools import get_standard_tools

    # 自定义工具
    def my_calculator(state, expression: str) -> ToolResult:
        try:
            result = eval(expression, {"__builtins__": {}}, {})
            return ToolResult(success=True, output=str(result))
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    custom_tool = ToolSpec(
        name="calculate",
        description="安全地计算数学表达式",
        args_schema={"expression": "数学表达式字符串，如 '2**10 + 3*7'"},
        fn=my_calculator,
    )

    # 组合工具集
    tools = get_standard_tools()
    tools["calculate"] = custom_tool

    # 运行
    llm = OpenAIBackend(api_key=os.environ.get("OPENAI_API_KEY"))
    final_state = run(
        goal="计算 2^32 是多少，然后把结果写入 /tmp/result.txt",
        llm=llm,
        tools=tools,
        hooks=console_hooks(),
        max_iterations=10,
    )

    print(f"\n最终长期记忆: {final_state.long_term}")
    print(f"总迭代次数: {final_state.iteration}")


# ══════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    examples = {
        "1": ("最简使用", example_minimal),
        "2": ("工具进化演示", example_evolution),
        "3": ("预置领域知识", example_with_domain_knowledge),
        "4": ("手动组装", example_manual_assembly),
    }

    print("选择示例:")
    for k, (name, _) in examples.items():
        print(f"  {k}. {name}")

    choice = input("\n输入编号: ").strip()
    if choice in examples:
        _, fn = examples[choice]
        fn()
    else:
        print("无效选择")
