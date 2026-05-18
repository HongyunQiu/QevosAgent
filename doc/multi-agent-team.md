# 多 Agent 组网协作

## 背景

单个 QevosAgent 在面对复杂任务时，存在上下文容量和并行执行能力的限制。多 Agent 组网让若干个独立运行的 QevosAgent 实例，通过拓扑节点码声明相互关系，从而协同完成更大规模的任务。

## 设计原则

**无角色标签，纯拓扑关系。** 系统中不存在"主管"或"队员"这样的固定角色。一个节点的行为由其在拓扑图中的位置决定：有上游节点时，`ask_user` 自动路由给上游；有下游节点时，接收它们的问题和汇报。同一个节点可以同时是某节点的下游和另一些节点的上游，天然支持多层级结构。

**行为由节点码驱动，不由启动参数决定。** 所有 Agent 以相同方式启动。节点码可在运行时任意时刻设置（通过工具调用或斜杠命令），设置后立即生效；清除后恢复独立模式。启动参数无需修改。

**Team API 始终运行。** 每个 Agent 启动时自动开启 HTTP 服务（默认端口 9100，由 `TEAM_PORT` 环境变量配置）。节点码为 null 时处于独立模式，API 监听但对 Agent 自身行为零影响；节点码设置后进入组网模式。

**向后兼容，独立模式行为不变。** 对现有单机使用方式无任何影响。

---

## 节点码格式

```
nodeA ^ http://upstream-host:9100
```

| 部分 | 含义 |
|------|------|
| `nodeA` | 本节点在拓扑中的人类可读 ID |
| `^` | "上游连接"分隔符 |
| `http://upstream-host:9100` | 上游节点的可达地址 |

**顶层节点**（无上游）：

```
nodeRoot
```

**多层示例：**

```
nodeRoot                                      ← 顶层，端口 9100
nodeA ^ http://host-root:9100                 ← 第二层，端口 9101
nodeB ^ http://host-root:9100                 ← 第二层，端口 9102
nodeA1 ^ http://host-a:9101                  ← 第三层，端口 9103
```

当前仅支持单上游（树形拓扑），不支持多上游（DAG）。

---

## 启动方式

所有节点以相同方式启动，仅通过环境变量区分端口：

```bash
TEAM_PORT=9100 python run_goal.py "总体目标"   # 节点 root
TEAM_PORT=9101 python run_goal.py "子任务 A"   # 节点 A
TEAM_PORT=9102 python run_goal.py "子任务 B"   # 节点 B
```

启动后，节点码可在任意时刻通过工具调用分配：

```
# 顶层节点给自己设置节点码
set_node("nodeRoot")

# 顶层节点向其他节点分配拓扑位置
assign_node("http://host-a:9101", "nodeA ^ nodeRoot @ http://host-root:9100")
assign_node("http://host-b:9102", "nodeB ^ nodeRoot @ http://host-root:9100")
```

也可以通过斜杠命令在任意时刻配置：

```
/inject set_node("nodeRoot")
```

---

## 工具集

所有节点加载相同的工具集，行为由运行时拓扑节点码决定。

### 拓扑管理

| 工具 | 说明 |
|------|------|
| `set_node(node_code)` | 设置本节点的拓扑节点码；传 `"null"` 退出组网模式 |
| `assign_node(target_url, node_code)` | 向任意节点分配拓扑节点码 |

### 通信（任意方向，按 URL）

| 工具 | 说明 |
|------|------|
| `get_agent_status(agent_url)` | 查询任意节点的运行状态（轻量，无推理消耗） |
| `get_agent_snapshot(agent_url)` | 获取任意节点的完整快照（meta + scratchpad + 最近 5 条行动） |
| `send_to_agent(agent_url, message)` | 向任意节点注入消息（对方下次迭代可感知） |
| `delegate_task(agent_url, task, context)` | 向任意节点分配子任务 |

### 上游感知（依赖本节点拓扑码中的上游 URL）

| 工具 | 说明 |
|------|------|
| `report_to_upstream(message)` | 向上游节点发送汇报（自动读取上游地址） |

### 下游感知（处理来自下游的问题）

| 工具 | 说明 |
|------|------|
| `get_pending_questions()` | 获取来自下游节点的待回答问题列表 |
| `answer_downstream(agent_url, question_id, answer)` | 按 question_id 精确回答某下游问题 |

---

## ask_user 路由机制

`ask_user` 是现有工具，代码层面无任何修改。路由行为由 `run_goal.py` 的暂停处理逻辑在运行时透明切换：

```
Agent 调用 ask_user("问题")
  ├── topology_node.upstream_url 有值（组网模式）
  │     → POST 问题到上游节点的 /agent/question（携带 question_id）
  │     → 永久等待上游回答（每 10s ping 一次上游存活状态）
  │     → 上游离线（连续 6 次 ping 失败）→ 降级为等待用户直接输入
  │     → 收到回答 → 注入 short_term → Agent 继续执行
  │
  └── topology_node 为 null（独立模式）
        → 完全走原有逻辑（CLI stdin / web 看板输入）
```

问题与回答通过 `question_id`（UUID）精确配对，避免并发场景下的错配。

---

## HTTP API 端点

每个 Agent 实例暴露以下端点（纯 stdlib 实现，零额外依赖）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agent/status` | 轻量状态快照（含拓扑节点信息） |
| GET | `/agent/snapshot` | 完整快照：meta + scratchpad + 最近 5 条 short_term |
| GET | `/agent/questions` | 来自下游节点的待回答问题列表 |
| POST | `/agent/set_node` | 设置拓扑节点码 `{"node_code": "..."}` |
| POST | `/agent/inject` | 注入消息 `{"message": "..."}` |
| POST | `/agent/task` | 接收子任务 `{"task": "...", "context": "..."}` |
| POST | `/agent/question` | 下游节点提问 `{"question_id", "from_node_id", "from_node_url", "content"}` |
| POST | `/agent/answer` | 上游节点回答 `{"question_id", "answer"}` |

---

## 异常处理

| 场景 | 处理方式 |
|------|----------|
| 端口被占用 | 打印警告后继续运行（Agent 正常启动，组网功能不可用） |
| 上游节点离线（等待回答期间） | 每 10s ping 一次；连续 6 次失败（约 60s）后降级为 ask_user |
| 无法联系上游（提问时）| 打印警告，直接降级为 ask_user |
| 下游节点无响应 | 上游节点自行决策（重新委派、等待、或报告用户） |

---

## 文件结构

```
agent/team/
    __init__.py      空文件，使 team 成为 Python 包
    api.py           TeamApiServer：HTTP 服务 + 拓扑节点管理 + 问答配对
    tools.py         get_team_tools()：统一工具集，始终随 Agent 加载
```

**修改的文件：**

- `run_goal.py` — Team API 无条件启动；团队工具无条件加载；ask_user 路由逻辑
- `agent/runtime/persistence.py` — `_team_api` 加入不可序列化键过滤列表

---

## 典型使用流程（两级扁平拓扑）

```
1. 启动顶层节点（root）和两个子节点（A、B）
   TEAM_PORT=9100 python run_goal.py "协调完成 X 项目"
   TEAM_PORT=9101 python run_goal.py "执行子任务 A"
   TEAM_PORT=9102 python run_goal.py "执行子任务 B"

2. root 节点在推理中规划拓扑并分配节点码
   set_node("root")
   assign_node("http://host-a:9101", "nodeA ^ root @ http://host-root:9100")
   assign_node("http://host-b:9102", "nodeB ^ root @ http://host-root:9100")

3. root 分配子任务
   delegate_task("http://host-a:9101", "完成模块 A 的实现")
   delegate_task("http://host-b:9102", "完成模块 B 的测试")

4. 子节点执行任务
   - 遇到问题时调用 ask_user → 自动路由到 root
   - 完成后调用 report_to_upstream("模块 A 实现完毕，详见 /output/a.py")

5. root 处理来自子节点的问题
   get_pending_questions()        ← 查看待回答问题
   answer_downstream(url, qid, "回答内容")

6. root 监控进度
   get_agent_snapshot("http://host-a:9101")  ← 无需 A 消耗推理资源
```
