# 问题记录：dashboard 连续出现 503 错误且烧光迭代预算

## 问题现象

部分用户在 dashboard 上看到 Agent 任务连续报红：

```
⚠️  错误: LLM 调用失败: ... 503 ...
⚠️  错误: LLM 调用失败: ... 503 ...
⚠️  错误: LLM 调用失败: ... 503 ...
（持续约 30 条）
[迭代上限] 任务结束
```

用户感受是"模型坏了"，而不是"模型在忙，需要等"。任务的迭代预算（默认 30
轮）被网络/服务端故障白白消耗，本来 10 轮能完成的目标直接失败。

---

## 根因分析

### 现象不是 vLLM 真的返回了 503

排查中确认了几点关键事实：

- vLLM 日志里**完全没有 503**，KV Cache 命中率约 11%，GPU 利用率 100%
  实际上只是 vLLM 启动时预分配显存的正常表现，单请求不繁忙。
- 部署在局域网，可排除公网丢包/抖动。
- 真实瓶颈是**多用户长上下文场景下 vLLM 的 KV cache 抖动**：不同用户
  的 prompt prefix 互相挤占 KV 块，prefill 命中率下降，单请求 prefill
  时间被拉长——客户端（或中间代理）等不及就主动断开。

也就是说，"503"实际是**客户端 SDK 超时 / 中间层超时**被冒成了一个错误
事件，并不是 vLLM 主动拒绝服务。

### 客户端层放大了故障

`agent/core/llm.py` 的 `_create_with_retry` 此前**只处理 400 参数错误**，
5xx / timeout / 连接错误直接 `raise` 上抛。OpenAI SDK 默认 `max_retries=2`
的快重试是亚秒级间隔，对"vLLM 排队消化一波长上下文"这类秒级故障窗口
完全无效——三次请求几乎瞬间全部打在同一个忙窗口里。

### loop.py 把故障当成"完成了一轮思考"

`agent/core/loop.py` 的 LLM 异常分支做了三件事：

1. 触发 `hooks.on_error(...)` → dashboard 渲染红色错误条
2. 把 `[系统] LLM调用异常: ...` 写进 short_term
3. `state.iteration += 1` 然后 `continue`

第三步是关键 bug：**网络/服务端故障不应该消耗模型的"思考预算"**。本应
留给模型解决问题用的 30 轮迭代，被 503 一轮一轮蚕食干净。

### 失败放大循环

合起来就是：

```
vLLM prefill 慢
  → 客户端 / 代理 timeout
  → SDK 抛异常
  → loop 报红 + iteration += 1
  → 立刻 continue 重新请求
  → vLLM 还在忙，又 timeout
  → ...
  → 30 轮全部用光，任务失败
```

用户在 dashboard 看到 ~30 条 503，但实际背后是 3× 这么多次的 HTTP 请求
打在 vLLM 上，**重试本身加剧了过载**。

---

## 修复方案

修复分两层：**客户端层吞住瞬时故障**（用户感受改善）+ **loop 层保护
预算**（正确性）。两层正交，互相不依赖。

### 修复 1：客户端显式 timeout + 内置重试关闭

`OpenAIBackend.__init__`：

```python
self.client = openai.OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=httpx.Timeout(
        connect=15.0,
        read=900.0,   # 默认 LLM_READ_TIMEOUT=900s
        write=30.0,
        pool=10.0,
    ),
    max_retries=0,   # 关掉 SDK 亚秒级快重试，统一交给业务层
)
```

`read=900s` 把"vLLM 排队 + prefill + decode"这一整套流程容下，
绝大多数瞬时慢响应根本不会进入异常路径。

### 修复 2：业务层指数退避 + 退避通知

`_create_with_retry` 拆为两层：

- **内层** `_try_create_with_param_strip`：保留原有的 400 参数剥离逻辑，
  一次尝试内自愈，不参与退避。
- **外层**：对 5xx / 429 / `APITimeoutError` / `APIConnectionError`
  做指数退避，默认 5 次尝试、间隔 `[3, 10, 30, 60, 120]` 秒 + ±20% jitter。
  退避期间通过 `self.on_retry(attempt, wait_seconds, reason)` 通知 UI。

可重试判定（`_is_retryable_error`）：

| 异常 | 判定 |
|---|---|
| `APITimeoutError` | ✅ |
| `APIConnectionError` | ✅ |
| `RateLimitError`（429） | ✅ |
| `APIStatusError` 5xx | ✅ |
| `APIStatusError` 4xx | ❌（含 400 参数错误，由内层处理）|
| 其他 | 退化为消息文本启发式匹配 |

### 修复 3：新增 `on_llm_retry` 钩子

`AgentHooks` 新增字段：

```python
on_llm_retry: Optional[Callable[[int, float, str], None]] = None
# 参数：尝试次数(1-based)、本次等待秒数、错误简短分类
```

`loop.run()` 入口处自动把它注入到 `llm.on_retry`：

```python
if hooks.on_llm_retry is not None and hasattr(llm, "on_retry"):
    llm.on_retry = hooks.on_llm_retry
```

默认实现（`make_default_hooks`）打印一行黄色提示：

```
⏳ 模型繁忙，等待中… (尝试 2，等待 10.0s · 503 服务异常)
```

这是普通 stdout 行，**不会触发 dashboard 的红色错误渲染**，用户感受是
"任务在等"而不是"任务出错"。

### 修复 4：LLM 异常不消耗迭代预算（带熔断）

`loop.py` 的 LLM 异常分支：

```python
# 旧：
state.iteration += 1

# 新：
_consec = int(state.meta.get("_consec_llm_errors", 0)) + 1
state.meta["_consec_llm_errors"] = _consec
if _consec >= max(1, LLM_CONSEC_ERROR_BUDGET):  # 默认 10
    state.iteration += 1
```

成功一轮立即清零计数器：

```python
raw_response = llm.complete(messages, system)
if state.meta.get("_consec_llm_errors"):
    state.meta["_consec_llm_errors"] = 0
```

效果：

- 瞬时故障（vLLM 排队）→ 不扣预算，模型回头继续解决问题
- 永久性故障（API key 错、模型名错）→ 连续 10 次失败后开始扣预算，
  任务最终会因迭代上限正常结束，不会无限循环

---

## 用户体验对比

### 修复前

```
[迭代 1/30] ...
⚠️  错误: LLM 调用失败: 503 Service Unavailable
[迭代 2/30] ...
⚠️  错误: LLM 调用失败: 503 Service Unavailable
... (重复约 30 次) ...
[迭代上限] 任务失败
```

vLLM 慢 30 秒 = 任务直接报废，dashboard 一片红。

### 修复后

```
[迭代 1/30] ...
⏳ 模型繁忙，等待中… (尝试 1，等待 3.0s · 503 服务异常)
⏳ 模型繁忙，等待中… (尝试 2，等待 10.0s · 503 服务异常)
（vLLM 缓过来）
💭 思考: ...
🔧 调用工具: ...
```

无红色错误事件，任务正常推进；迭代计数不受影响。

---

## 可配置环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_READ_TIMEOUT` | `900` | 单次 HTTP 读超时（秒），覆盖 vLLM 长上下文 prefill+decode 全流程 |
| `LLM_CONNECT_TIMEOUT` | `15` | TCP 连接超时（秒） |
| `LLM_RETRY_MAX_ATTEMPTS` | `5` | 退避重试总尝试次数（含首次） |
| `LLM_RETRY_BACKOFF` | `3,10,30,60,120` | 各次重试前的基础等待秒数（最终值会叠加 ±20% jitter，并保底 0.5s） |
| `LLM_CONSEC_ERROR_BUDGET` | `10` | 连续多少次 LLM 异常后开始消耗迭代预算（熔断阈值） |

### 调参建议

- **本地 vLLM、上下文很长**：`LLM_READ_TIMEOUT=1800` 调宽一倍。
- **高并发场景**：`LLM_RETRY_MAX_ATTEMPTS=8`、`LLM_RETRY_BACKOFF=5,15,45,90,180,300,300,300`
  让退避更舒缓，避免重试加剧拥塞。
- **生产稳定场景（不希望重试掩盖真问题）**：`LLM_RETRY_MAX_ATTEMPTS=2`、
  `LLM_CONSEC_ERROR_BUDGET=3`，让故障尽快冒到运维。

---

## 涉及文件

| 文件 | 改动 |
|---|---|
| `agent/core/llm.py` | `OpenAIBackend.__init__` 加 timeout + `max_retries=0`；新增 `_is_retryable_error`、`_classify_error`、`_try_create_with_param_strip`；`_create_with_retry` 改为退避循环 + `on_retry` 通知 |
| `agent/core/types_def.py` | `AgentHooks` 新增 `on_llm_retry` 字段 |
| `agent/core/loop.py` | `run()` 入口把 `hooks.on_llm_retry` 接到 `llm.on_retry`；`make_default_hooks` 新增 `on_llm_retry` 默认实现；LLM 异常分支加连续失败计数 + 熔断；成功路径清零计数 |
| `agent/i18n.py` | 新增 `loop.llm_retry` 文案（zh / en） |

---

## 与"长上下文 / KV cache 优化"的关系

本次修复**不解决**根本性能问题（多用户长上下文导致 vLLM 慢）——那是
vLLM 部署层和 prompt prefix 稳定性的范畴（QevosAgent 已经做了不少工作，
比如把 scratchpad / runtime_patches 后置到 user 消息末尾、保持 system
prompt prefix 稳定以最大化 KV cache 命中率）。

本次修复**只解决**：

1. 短暂慢响应不被冒成错误
2. 故障不污染迭代预算

如果用户长期看到大量"⏳ 模型繁忙"提示而非很快推进，那才是 vLLM 性能
需要进一步优化的信号（调大 `--gpu-memory-utilization`、确认开启
`--enable-prefix-caching`、调 `--max-num-seqs` 控制并发等）。

---

## Commit

`aafc57e` — fix(llm): 5xx/timeout 退避重试 + LLM 异常不消耗迭代预算
