# Startup And Finish Autonomy

这份文档说明 `simpleAgent` 当前 runtime 在启动阶段和结束阶段的新行为。

## 启动阶段

现在默认采用冷启动模式，而不是框架先把路由、记忆和 skill profile 全部预加载后再交给 Agent。

框架启动时只做三件事：

1. 创建本次运行的 `RUN_DIR`
2. 提供冷启动提示
3. 暴露启动准备工具

冷启动提示会直接告诉 Agent：

- `SKILL_DIR` 在哪里
- `MEMORY_DIR` 在哪里
- `skill/common.md` 是通用工具库
- `memory/memory.md` 是主记忆摘要
- `memory/long_term/` 是长期经验库

Agent 可按需使用这些工具：

- `list_profiles`
- `route_task`
- `recall_memory`
- `inspect_startup_capabilities`
- `load_skill_profile`
- `finalize_startup_context`

但这里的要求已经不是“先给一个 profile，然后围绕它启动”，而是先做一次组合式调查：

1. 先完成 `route_task`、`recall_memory`、`inspect_startup_capabilities`
2. 再基于调查结果决定是否调用 `load_skill_profile`
3. 无论最终选择直接执行还是继续装载 profile，都必须先调用 `finalize_startup_context`
4. 在 `startup decision` 未提交前，普通执行工具不会放行

### 组合式能力面

这次启动阶段的关键变化，是把“单一 profile 路由”改成“主判断 + 候选能力块”的组合方式。

`route_task` 不再只返回一个 `profile`，而是会给出：

- `primary_profile`
- `candidate_profiles`
- `candidate_profile_matches`
- `memory_tags`
- `reason`

其中：

- `primary_profile` 表示当前最核心的任务判断
- `candidate_profiles` 表示可以一起纳入考虑的候选能力块
- `candidate_profile_matches` 记录每个候选 profile 的匹配分数、强命中标记和命中术语
- `memory_tags` 继续用于长期经验检索

例如，一个联网检索类任务可以是：

- `primary_profile=general`
- `candidate_profiles=[general, agent_reach]`

这样做的目的不是提前替模型决定“必须用哪个工具”，而是避免启动调查只把视野收缩到单一 profile，导致像 `agent_reach` 这类高相关能力没有进入候选面。

当前这层候选扩展已经从手工关键词 hint 升级成基于 `skill/*.md` 描述文本的半结构化匹配。也就是说，后续新增 profile 时，优先在对应 skill 文档里写清适用场景、关键词和能力边界，而不是回到路由器里手工补规则。

对应地，`inspect_startup_capabilities` 现在也不再只是给出“某个 profile 的工具”，而是会分层返回：

- `loaded_tools`
- `loadable_common_tools`
- `candidate_profiles`

其中 `candidate_profiles` 会展开成每个候选 profile 的摘要、工具列表以及是否已加载，方便 Agent 在正式执行前做拼装式判断，而不是把 `common` 和单个 profile 混在一起理解。

### 分层保留

启动阶段的产物分成三层：

- `startup_decision.md`
- `startup_brief.md`
- `startup_trace.jsonl`

其中只有 `startup_brief` 会进入后续主上下文，目的是保留对执行真正有帮助的摘要，而不是把整段启动推理原样塞回 prompt。

### 这次修正解决的问题

组合式能力面主要针对两个启动失真：

1. `route_task` 只给一个 profile，模型起手时看不到其他高相关能力
2. `inspect_startup_capabilities` 以前更像“单 profile 工具清单”，不足以表达“已加载 / 可加载 / 候选能力块”的区别

修正后，启动协议强调的是：

- 先把能力面尽量展开
- 再让模型自主决定如何组合使用
- 宁可多给候选，也不要因为单一 profile 视角漏掉合适工具

## 结束阶段

结束阶段不再强制要求模型在草稿本中写死 `ACCEPTANCE` 区块。

推荐流程是：

1. 调用 `submit_completion_report`
2. 填写：
   - `goal_understanding`
   - `completed_work`
   - `remaining_gaps`
   - `evidence_type`
   - `evidence`
   - `outcome`
   - `confidence`
3. 再输出 `action=done`

### outcome 语义

- `done`: 任务完成
- `done_partial`: 主体完成，但仍有明确缺口
- `done_blocked`: 受外部阻塞，只完成了可完成部分

### 评审规则

- 如果 `evidence_type=artifact`，框架会继续校验文件是否真实存在
- 如果声称的 artifact 不存在，则不会接受 `done`
- 评审结果会写入 `completion_review.json`

## 相关产物

一次运行结束后，除了原有文件外，还应重点关注：

- `startup_decision.md`
- `startup_brief.md`
- `startup_trace.jsonl`
- `completion_review.json`

这几个文件分别对应启动决策、主上下文摘要、原始启动轨迹和结束评审结果。
