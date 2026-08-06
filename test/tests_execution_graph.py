#!/usr/bin/env python3
"""执行图回归测试（批 1：数据模型 / 状态机 / 出口校验 / 投影 / 落盘）。

这些用例守的是执行图的三条底线：
  1. 图不能说谎——出口证据不实证就不能标"已核验"，废弃分支的环境残留必须留痕
  2. 图不能挡路——局部前向规划下走到已规划末端是常态，不许自动关图
  3. 图不能拖垮主流程——任何畸形输入都只能换回一条说明，不能抛异常

设计见 doc/execution-graph.md
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.core import graph as G
from agent.core.llm import _build_context_suffix, parse_response
from agent.core.types_def import AgentState
from agent.runtime.persistence import RunPersistence


def _state(iteration: int = 10) -> AgentState:
    st = AgentState(goal="测试目标")
    st.persistence = RunPersistence(tempfile.mkdtemp(prefix="graphtest-"))
    st.iteration = iteration
    return st


def _two_node_graph(st: AgentState, expect=None):
    return G.create_graph(
        st,
        title="测试图",
        nodes=[
            {"title": "第一步", "goal": "做 A", "budget": 8,
             "exit": {"evidence_type": "artifact", "expect": expect or []}},
            {"title": "第二步", "goal": "做 B"},
        ],
    )


class CreateGraphTests(unittest.TestCase):
    def test_root_node_carries_prior_work(self):
        """建图可发生在任务中途，之前的历史必须在图上有落点，否则地图无源之始。"""
        st = _state(iteration=40)
        st.meta["scratchpad"] = "任务描述:\n做个东西\n已经排查过 A 和 B"
        g, _ = _two_node_graph(st)
        root = g["nodes"][G.ROOT_ID]
        self.assertEqual(root["status"], "done")
        self.assertEqual(root["closed_by"], "implicit")
        self.assertEqual(root["iter_range"], [0, 40])
        self.assertIn("排查过 A", root["goal"])

    def test_missing_edges_chain_in_order(self):
        st = _state()
        g, _ = _two_node_graph(st)
        self.assertEqual(g["nodes"]["n1"]["parent"], G.ROOT_ID)
        self.assertEqual(g["nodes"]["n2"]["parent"], "n1")

    def test_evidence_type_inferred_from_expect(self):
        """给了产物路径就是可实证的 artifact，没给则老实降级为自证。"""
        st = _state()
        g, _ = _two_node_graph(st, expect=["out.txt"])
        self.assertEqual(g["nodes"]["n1"]["exit"]["evidence_type"], "artifact")
        self.assertEqual(g["nodes"]["n2"]["exit"]["evidence_type"], "observation")

    def test_empty_nodes_rejected(self):
        st = _state()
        g, msg = G.create_graph(st, title="空图", nodes=[])
        self.assertIsNone(g)
        self.assertTrue(msg)

    def test_node_without_incoming_edge_chains_to_previous_not_root(self):
        """复刻实战失误：模型以为自己的节点是 n0..n6，整套边错位一格，末节点漏了入边。

        nodes 的顺序就是模型自己的排序，据此补链远比"扔到根下"接近本意——
        挂到根上会让末节点在图上看起来与整条链毫无关系。
        """
        st = _state()
        g, msg = G.create_graph(st, title="复刻", nodes=[
            {"title": "初始化"}, {"title": "搜 A"}, {"title": "搜 B"},
            {"title": "综合"}, {"title": "出报告"}, {"title": "展示"},
        ], edges=[
            {"from": "n0", "to": "n1"}, {"from": "n0", "to": "n2"},
            {"from": "n1", "to": "n3"}, {"from": "n2", "to": "n3"},
            {"from": "n3", "to": "n4"}, {"from": "n4", "to": "n5"},
        ])
        self.assertEqual(g["nodes"]["n6"]["parent"], "n5")
        self.assertTrue(any(e["from"] == "n5" and e["to"] == "n6" for e in g["edges"]))
        self.assertIn("n6", msg)   # 必须把纠正过的结构告诉模型

    def test_parent_and_edges_never_diverge(self):
        """只设 parent 不补边会让渲染层画出 edges 里根本不存在的幽灵边。"""
        st = _state()
        for edges in (None, [{"from": "n0", "to": "n1"}], [{"from": "nope", "to": "n2"}]):
            g, _ = G.create_graph(st, title="一致性", nodes=[
                {"title": "a"}, {"title": "b"}, {"title": "c"},
            ], edges=edges)
            for nid, node in g["nodes"].items():
                if nid == G.ROOT_ID:
                    continue
                self.assertTrue(
                    any(e["from"] == node["parent"] and e["to"] == nid for e in g["edges"]),
                    f"{nid} 的 parent={node['parent']} 在 edges 里没有对应边（edges={edges}）",
                )

    def test_dropped_edges_are_reported(self):
        """引用了不存在 id 的边被静默丢弃，模型会以为图就是它画的那样。"""
        st = _state()
        _g, msg = G.create_graph(st, title="坏边", nodes=[{"title": "a"}, {"title": "b"}],
                                 edges=[{"from": "n1", "to": "n99"}, {"from": "n1", "to": "n2"}])
        self.assertIn("n99", msg)

    def test_second_graph_archives_the_first(self):
        """同时只能有一张活动图；旧图归档而非删除，最终产物是一串地图。"""
        st = _state()
        first, _ = _two_node_graph(st)
        second, _ = G.create_graph(st, title="换个分解", nodes=[{"title": "X", "goal": "做 X"}])
        self.assertEqual(first["status"], "abandoned")
        self.assertEqual(second["status"], "active")
        self.assertEqual(len(G.get_root(st)["graphs"]), 2)


class NodeStateMachineTests(unittest.TestCase):
    def test_only_one_active_node(self):
        st = _state()
        _two_node_graph(st)
        self.assertTrue(G.apply_op(st, {"op": "enter", "node": "n1"})[0])
        ok, msg = G.apply_op(st, {"op": "enter", "node": "n2"})
        self.assertFalse(ok)
        self.assertIn("n1", msg)

    def test_abandoned_node_cannot_be_reentered(self):
        """重走一条废弃的路必须走 fork 新建节点，否则地图会丢失"试过并失败"这个事实。"""
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "此路不通"})
        self.assertFalse(G.apply_op(st, {"op": "enter", "node": "n1"})[0])

    def test_abandon_cascades_to_unstarted_descendants(self):
        st = _state()
        _two_node_graph(st)
        ok, msg = G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "换路"})
        self.assertTrue(ok)
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n2"]["status"], "abandoned")
        self.assertIn("n2", msg)

    def test_root_is_immutable(self):
        st = _state()
        _two_node_graph(st)
        self.assertFalse(G.apply_op(st, {"op": "abandon", "node": G.ROOT_ID})[0])
        self.assertFalse(G.apply_op(st, {"op": "block", "node": G.ROOT_ID})[0])

    def test_inline_extend_accepts_one_node_only(self):
        """inline 字段跟着工具调用一起输出，塞一整棵树会撑爆单次输出上限。"""
        st = _state()
        _two_node_graph(st)
        ok, _ = G.apply_op(st, {"op": "extend", "node": [{"title": "a"}, {"title": "b"}]})
        self.assertFalse(ok)


class ExitEvidenceTests(unittest.TestCase):
    def test_missing_artifact_blocks_exit(self):
        """出口证据不实证就不放行——否则模型可以靠关节点伪造进展。"""
        st = _state()
        _two_node_graph(st, expect=["definitely_absent.txt"])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        ok, msg = G.apply_op(st, {"op": "exit", "node": "n1", "summary": "假装做完"})
        self.assertFalse(ok)
        self.assertIn("definitely_absent.txt", msg)
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n1"]["status"], "active")

    def test_existing_artifact_marks_verified(self):
        st = _state()
        _two_node_graph(st, expect=["made.txt"])
        run_dir = Path(st.persistence.run_dir)
        (run_dir / "made.txt").write_text("x", encoding="utf-8")
        G.apply_op(st, {"op": "enter", "node": "n1"})
        st.iteration = 25
        self.assertTrue(G.apply_op(st, {"op": "exit", "node": "n1", "summary": "做完了"})[0])
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n1"]
        self.assertEqual(node["closed_by"], "evidence_verified")
        self.assertEqual(node["iter_range"], [10, 25])

    def test_unverifiable_exit_is_labelled_self_certified(self):
        """observation 类出口无法实证，但必须如实标记，供后续批次统计。"""
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n2"})
        G.apply_op(st, {"op": "exit", "node": "n2", "summary": "观察到了"})
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n2"]["closed_by"], "self_certified")

    def test_side_effects_recorded_even_when_exit_rejected(self):
        """环境改动是既成事实，不因出口被拒就不记——残留台账不能有洞。"""
        st = _state()
        _two_node_graph(st, expect=["absent.txt"])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "exit", "node": "n1", "side_effects": ["改了远端 /opt/foo"]})
        self.assertIn("改了远端 /opt/foo",
                      st.meta["_graph"]["graphs"][0]["nodes"]["n1"]["side_effects"])


class DowngradeCloseTests(unittest.TestCase):
    """降级通过：给节点出口补上 run 级验收门早就有的第三态。

    但降级产生的遗留会在后继工作里被放大、甚至成为关键阻塞，而那时当初的
    上下文早已被压缩——所以遗留叙述是硬门，且必须常驻。
    """

    def _stuck(self):
        st = _state()
        _two_node_graph(st, expect=["never_written.txt"])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        return st

    def test_force_without_narrative_is_rejected(self):
        st = self._stuck()
        ok, msg = G.apply_op(st, {"op": "exit", "node": "n1", "force": True})
        self.assertFalse(ok)
        self.assertIn("residue", msg)
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n1"]["status"], "active")

    def test_vague_narrative_is_rejected(self):
        """"还有点小问题"这种写法等于没写，续作时毫无价值。"""
        st = self._stuck()
        for residue, impact in (("有点问题", "不影响"),
                                ("无", "没有"),
                                ("产物落在了别的目录里", "不影响"),      # 结论无依据
                                ("n/a", "none")):
            ok, _ = G.apply_op(st, {"op": "exit", "node": "n1", "force": True,
                                    "residue": residue, "impact": impact})
            self.assertFalse(ok, f"应被拒: {residue!r}/{impact!r}")

    def test_dense_chinese_assessment_is_not_over_rejected(self):
        """阈值按中文密度定：中文一个字顶英文好几个字符，
        定高了会把"会影响 n2 的对比分析"这种完全站得住的评估误杀。"""
        st = self._stuck()
        ok, _ = G.apply_op(st, {
            "op": "exit", "node": "n1", "force": True,
            "residue": "第 3 节图表没渲染出来",
            "impact": "会影响 n2 的对比分析",
        })
        self.assertTrue(ok)

    def test_detailed_narrative_allows_the_close(self):
        st = self._stuck()
        ok, msg = G.apply_op(st, {
            "op": "exit", "node": "n1", "force": True,
            "residue": "报告生成了，但第 3 节图表渲染失败，那一节目前是空的",
            "impact": "会影响 n2：n2 的对比分析要引用第 3 节的数据，届时会缺一块",
        })
        self.assertTrue(ok)
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n1"]
        self.assertEqual(node["closed_by"], "unverified_override")
        self.assertIn("第 3 节", node["outcome"]["residue"])
        # 必须当场把"要不要重新规划"摆到模型面前
        self.assertIn("plan_revise", msg)

    def test_residue_is_pinned_in_the_projection(self):
        """遗留必须扛过压缩——图是唯一能做到这点的结构化记忆。"""
        st = self._stuck()
        G.apply_op(st, {"op": "exit", "node": "n1", "force": True,
                        "residue": "第 3 节图表缺失，report.md 那一节是空的",
                        "impact": "会影响 n2 的对比分析，需要补数据源"})
        proj = G.render(st)
        self.assertIn("第 3 节图表缺失", proj)
        self.assertIn("会影响 n2", proj)

    def test_downgraded_nodes_reach_run_gaps(self):
        """节点状态是 done，但承诺没兑现——不进 gaps 就等于把这笔账抹掉。"""
        from agent.core.loop import RUN_OUTCOME_PARTIAL, _set_run_outcome
        st = self._stuck()
        G.apply_op(st, {"op": "exit", "node": "n1", "force": True,
                        "residue": "第 3 节图表缺失，report.md 那一节是空的",
                        "impact": "会影响 n2 的对比分析"})
        rec = _set_run_outcome(st, RUN_OUTCOME_PARTIAL)
        self.assertTrue(any("第 3 节图表缺失" in g for g in rec["gaps"]))
        self.assertIn("n1", [g["node"] for g in rec["graph_gaps"]])

    def test_downgrade_counts_as_unverified(self):
        st = self._stuck()
        G.apply_op(st, {"op": "exit", "node": "n1", "force": True,
                        "residue": "产物落不到 expect 指定的位置，实际写到了别处",
                        "impact": "不影响 n2，n2 读的是数据库不是这个文件"})
        self.assertEqual(G.metrics(st)["unverified_streak"], 1)

    def test_force_is_offered_when_the_check_blocks(self):
        """模型得知道有这条路，否则只能反复重试或违心地 abandon。"""
        st = self._stuck()
        _ok, msg = G.apply_op(st, {"op": "exit", "node": "n1", "summary": "做完了"})
        self.assertIn("force", msg)


class MisplacedGraphOpTests(unittest.TestCase):
    """graph_op 被写进 args 时必须救回来并纠正。

    实战：协议提示原先写"可在工具调用的同一个 JSON 里附带 graph_op"，模型理解成
    args，11 次推进全被 executor 的参数过滤静默丢弃，图与实际进度彻底脱节，
    模型最后自己放弃了图并写下"可能是 graph_op 未正确执行"。
    """

    def _action(self, *, top=None, in_args=None):
        from agent.core.types_def import Action, ActionType
        args = {"path": "x.py", "old_string": "a", "new_string": "b"}
        if in_args is not None:
            args["graph_op"] = in_args
        return Action(type=ActionType.TOOL_CALL, thought="t", tool="edit_file",
                      args=args, graph_op=top)

    def test_top_level_op_is_used_as_is(self):
        from agent.core.loop import _rescue_graph_op
        op, misplaced = _rescue_graph_op(self._action(top={"op": "exit", "node": "n1"}))
        self.assertEqual(op["op"], "exit")
        self.assertFalse(misplaced)

    def test_op_hidden_in_args_is_rescued_and_flagged(self):
        from agent.core.loop import _rescue_graph_op
        op, misplaced = _rescue_graph_op(self._action(in_args={"op": "exit", "node": "n1"}))
        self.assertEqual(op["op"], "exit")
        self.assertTrue(misplaced)

    def test_top_level_wins_when_both_present(self):
        from agent.core.loop import _rescue_graph_op
        op, misplaced = _rescue_graph_op(
            self._action(top={"op": "enter", "node": "n2"}, in_args={"op": "exit", "node": "n1"}))
        self.assertEqual(op["op"], "enter")
        self.assertFalse(misplaced)

    def test_unrelated_args_are_not_mistaken_for_ops(self):
        from agent.core.loop import _rescue_graph_op
        for junk in (None, "exit", 42, [], {}, {"node": "n1"}):     # 缺 op 键的不算
            op, misplaced = _rescue_graph_op(self._action(in_args=junk))
            self.assertIsNone(op, f"不该把 {junk!r} 当成 graph_op")
            self.assertFalse(misplaced)

    def test_protocol_text_pins_down_top_level(self):
        """措辞必须钉死"顶层字段"，否则模型只能猜——这正是当初出事的原因。"""
        from agent.i18n import _STRINGS
        for lang, needle in (("zh", "顶层字段"), ("en", "top-level field")):
            text = _STRINGS[lang]["graph.proj.protocol"]
            self.assertIn(needle, text)
            self.assertIn("args", text)
            self.assertNotIn("{{", text)     # 该条不带 kwargs，大括号不能双写


class RouteHintTests(unittest.TestCase):
    """运行时不做条件求值，但把模型自己写下的退路在它撞墙那一刻还给它。"""

    def _with_fallback(self):
        st = _state()
        G.create_graph(st, title="带退路", nodes=[
            {"id": "n1", "title": "主方案", "goal": "走 A",
             "exit": {"evidence_type": "artifact", "expect": ["nope.txt"]}},
            {"id": "n2", "title": "退路方案", "goal": "走 B"},
        ], edges=[{"from": "n0", "to": "n1", "kind": "then"},
                  {"from": "n1", "to": "n2", "kind": "fallback"}])
        return st

    def test_abandon_surfaces_the_recorded_fallback(self):
        st = self._with_fallback()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        _ok, msg = G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "不通"})
        self.assertIn("n2", msg)
        self.assertIn("退路方案", msg)

    def test_cascade_never_kills_the_fallback_it_should_offer(self):
        """退路在结构上是"被它兜底的那个节点"的子节点。级联若不区分边的种类，
        废弃主方案时会连退路一起废掉——正好在最需要它的时刻掐死它。"""
        st = self._with_fallback()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "不通"})
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n2"]["status"], "planned")
        self.assertTrue(G.apply_op(st, {"op": "enter", "node": "n2"})[0])

    def test_then_downstream_still_cascades(self):
        """依赖它的下游（then）该死还是要死，否则会留下孤儿节点。"""
        st = _state()
        G.create_graph(st, title="混合", nodes=[
            {"id": "n1", "title": "主", "goal": "A"},
            {"id": "n2", "title": "下游", "goal": "依赖 A"},
            {"id": "n3", "title": "退路", "goal": "A 的替代"},
        ], edges=[{"from": "n0", "to": "n1", "kind": "then"},
                  {"from": "n1", "to": "n2", "kind": "then"},
                  {"from": "n1", "to": "n3", "kind": "fallback"}])
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "不通"})
        nodes = st.meta["_graph"]["graphs"][0]["nodes"]
        self.assertEqual(nodes["n2"]["status"], "abandoned")   # then → 随之死
        self.assertEqual(nodes["n3"]["status"], "planned")     # fallback → 活着

    def test_blocked_exit_surfaces_the_fallback_too(self):
        st = self._with_fallback()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        _ok, msg = G.apply_op(st, {"op": "exit", "node": "n1"})
        self.assertIn("n2", msg)

    def test_no_fallback_no_noise(self):
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        _ok, msg = G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "x"})
        self.assertNotIn("退路", msg)

    def test_cond_field_is_gone(self):
        """留一个永不被读取的字段等于对模型撒谎。"""
        st = _state()
        g, _ = _two_node_graph(st)
        for e in g["edges"]:
            self.assertNotIn("cond", e)


class GraphLifecycleTests(unittest.TestCase):
    def test_reaching_planned_frontier_does_not_close_graph(self):
        """局部前向规划下走到已规划末端是常态；自动关图会让紧接着的 extend 撞空。"""
        st = _state()
        _two_node_graph(st)
        for nid in ("n1", "n2"):
            G.apply_op(st, {"op": "enter", "node": nid})
            G.apply_op(st, {"op": "exit", "node": nid, "summary": "ok"})
        g = st.meta["_graph"]["graphs"][0]
        self.assertEqual(g["status"], "active")
        self.assertTrue(G.apply_op(st, {"op": "extend", "after": "n2",
                                        "node": {"title": "第三步", "goal": "做 C"}})[0])

    def test_complete_requires_all_nodes_terminal(self):
        st = _state()
        _two_node_graph(st)
        self.assertFalse(G.apply_op(st, {"op": "complete"})[0])
        for nid in ("n1", "n2"):
            G.apply_op(st, {"op": "abandon", "node": nid, "reason": "收束"})
        self.assertTrue(G.apply_op(st, {"op": "complete"})[0])
        self.assertEqual(st.meta["_graph"]["graphs"][0]["status"], "completed")

    def test_open_nodes_export_as_gaps(self):
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "block", "node": "n1", "reason": "等外部依赖"})
        gaps = G.open_nodes(st)
        self.assertEqual([g["node"] for g in gaps], ["n1", "n2"])
        self.assertEqual(gaps[0]["title"], "第一步")


class ProjectionTests(unittest.TestCase):
    def test_no_graph_costs_nothing(self):
        """没建过图的 run 必须零成本——建图是能力，不是强制阶段。"""
        st = _state()
        self.assertEqual(G.render(st), "")
        self.assertEqual(G.summary(st), {})

    def test_projection_surfaces_residue_and_reasons(self):
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "与公理冲突",
                        "side_effects": ["远端建了 /opt/foo"]})
        G.apply_op(st, {"op": "fork", "from": G.ROOT_ID, "node": {"title": "换路", "goal": "走 B"}})
        G.apply_op(st, {"op": "enter", "node": "n3"})
        proj = G.render(st)
        self.assertIn("与公理冲突", proj)      # 防重走老路
        self.assertIn("/opt/foo", proj)        # 环境不回滚，残留必须可见
        self.assertIn("graph_op", proj)        # 推进协议就在投影里，不占 system prompt

    def test_protocol_braces_are_literal(self):
        """graph.proj.protocol 不带 kwargs，t() 不会 format——双写会让模型看到 {{node}}。"""
        from agent.i18n import _STRINGS
        for lang in ("zh", "en"):
            hint = _STRINGS[lang]["graph.proj.protocol"]
            self.assertNotIn("{{", hint, f"{lang} 的协议提示不能双写大括号")
            self.assertIn("{node}", hint)

    def test_inactive_graph_collapses_to_one_line(self):
        st = _state()
        _two_node_graph(st)
        G.abandon_graph(st, "分解方式错了")
        proj = G.render(st)
        self.assertNotIn("\n", proj)
        self.assertIn("g1", proj)

    def test_projection_respects_char_budget(self):
        st = _state()
        G.create_graph(st, title="大图", nodes=[
            {"title": f"步骤{i}", "goal": "x" * 200} for i in range(40)
        ])
        self.assertLessEqual(len(G.render(st)), G._projection_chars() + 200)


class ContextInjectionTests(unittest.TestCase):
    def test_suffix_order_puts_graph_after_scratchpad(self):
        """图是驱动下一步动作的骨架，应比草稿本更靠近生成点。"""
        suffix = _build_context_suffix(
            scratchpad="SPMARK", runtime_patches=["PATCHMARK"],
            thought_rigor=False, graph_projection="GRAPHMARK",
        )
        self.assertLess(suffix.index("PATCHMARK"), suffix.index("SPMARK"))
        self.assertLess(suffix.index("SPMARK"), suffix.index("GRAPHMARK"))

    def test_graph_op_parsed_on_both_action_types(self):
        for action, extra in (("tool_call", {"tool": "shell", "args": {}}), ("done", {"final_answer": "x"})):
            raw = json.dumps({"thought": "t", "action": action,
                              "graph_op": {"op": "enter", "node": "n1"}, **extra})
            self.assertEqual(parse_response(raw).graph_op, {"op": "enter", "node": "n1"})

    def test_malformed_graph_op_is_ignored_not_fatal(self):
        """graph_op 是附带记账，格式不对不该拖垮这一轮的工具调用。"""
        raw = json.dumps({"thought": "t", "action": "tool_call", "tool": "shell",
                          "args": {}, "graph_op": ["not", "a", "dict"]})
        act = parse_response(raw)
        self.assertIsNone(act.graph_op)
        self.assertEqual(act.tool, "shell")


class ConvergenceMetricTests(unittest.TestCase):
    """去掉迭代上限后，图本身就是替代守卫——这些指标是它的全部依据。"""

    def _running_graph(self, iteration=10):
        st = _state(iteration)
        _two_node_graph(st)
        return st

    def test_no_graph_means_no_metrics(self):
        self.assertEqual(G.metrics(_state()), {})

    def test_stall_counts_from_last_closure_not_from_activity(self):
        """抓的就是"每轮都在换工具、却一个节点都没关掉"——活跃度不算推进。"""
        st = self._running_graph()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        st.iteration = 18
        G.apply_op(st, {"op": "exit", "node": "n1", "summary": "ok"})
        st.iteration = 60
        self.assertEqual(G.metrics(st)["stall_iters"], 42)

    def test_abandoning_does_not_count_as_progress(self):
        """废弃省了力气但没把目标往前推；否则反复废弃就能把停滞计数刷掉。"""
        st = self._running_graph(iteration=10)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        st.iteration = 40
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "不通"})
        # 从建图那一刻起算，废弃没有把它清零
        self.assertEqual(G.metrics(st)["stall_iters"], 30)
        # 而真正闭合一个节点会清零（n2 是 n1 的下游、已被级联废弃，
        # 所以换路必须 fork 一个新节点——这正是逻辑回溯的走法）
        G.apply_op(st, {"op": "fork", "from": G.ROOT_ID,
                        "node": {"title": "换条路", "goal": "走 B 方案"}})
        G.apply_op(st, {"op": "enter", "node": "n3"})
        G.apply_op(st, {"op": "exit", "node": "n3", "summary": "真做完了"})
        self.assertEqual(G.metrics(st)["stall_iters"], 0)

    def test_revisits_catch_structural_loops(self):
        """每次用的工具都不同，签名式循环检测完全看不见，只有图看得见。"""
        st = self._running_graph()
        for _ in range(4):
            G.apply_op(st, {"op": "enter", "node": "n1"})
            G.apply_op(st, {"op": "exit", "node": "n1", "summary": "又试了一次"})
        m = G.metrics(st)
        self.assertEqual(m["node_revisits"], 4)
        self.assertEqual(m["revisit_node"], "n1")
        self.assertEqual(G.stall_level(m)[1], "revisit")

    def test_unverified_streak_counts_trailing_self_certified(self):
        st = self._running_graph()
        for nid in ("n1", "n2"):
            G.apply_op(st, {"op": "enter", "node": nid})
            G.apply_op(st, {"op": "exit", "node": nid, "summary": "我观察到了"})
        self.assertEqual(G.metrics(st)["unverified_streak"], 2)

    def test_a_freshly_planned_graph_is_not_a_stall(self):
        """扇出需要预热。一张刚画好的多节点计划，当然一个都还没闭合——
        建图后第一轮就判 L2 停滞是误判，实战中直接导致 advisor 空转介入、
        并在 20 轮后升级成 ask_user 打扰用户。"""
        st = _state()
        G.create_graph(st, title="刚画好", nodes=[
            {"title": f"步骤{i}", "goal": "x"} for i in range(8)
        ])
        m = G.metrics(st)
        self.assertGreaterEqual(m["open_fanout"], 5)      # 比值确实高
        self.assertEqual(m["done_count"], 0)
        self.assertEqual(G.stall_level(m)[0], 0)          # 但不构成停滞

    def test_fanout_flags_a_plan_that_only_widens_after_first_closure(self):
        """闭合过一个节点之后，"只分叉不闭合"才谈得上——它的前提是有过闭合的机会。"""
        st = _state()
        G.create_graph(st, title="只长叶子", nodes=[
            {"title": f"步骤{i}", "goal": "x"} for i in range(8)
        ])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_op(st, {"op": "exit", "node": "n1", "summary": "ok"})
        m = G.metrics(st)
        self.assertEqual(m["done_count"], 1)
        self.assertGreaterEqual(m["open_fanout"], 5)
        self.assertEqual(G.stall_level(m), (2, "fanout"))

    def test_levels_escalate_with_stall(self):
        st = self._running_graph()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        for iteration, expected in ((15, 0), (35, 1), (60, 2)):
            st.iteration = iteration
            self.assertEqual(G.stall_level(G.metrics(st))[0], expected, f"iter={iteration}")

    def test_user_guidance_restarts_the_stall_clock(self):
        """L3 求助之后用户答了话，不能拿着旧的停滞计数把他立刻再问一遍。"""
        st = self._running_graph()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        st.iteration = 80
        self.assertEqual(G.stall_level(G.metrics(st))[0], 2)
        st.meta["_graph_stall_baseline"] = 80        # 续跑时 loop 打的基线
        self.assertEqual(G.metrics(st)["stall_iters"], 0)
        self.assertEqual(G.stall_level(G.metrics(st))[0], 0)

    def test_hints_are_actionable_not_just_alarms(self):
        st = self._running_graph()
        G.apply_op(st, {"op": "enter", "node": "n1"})
        st.iteration = 35
        hint = G.stall_hint(G.metrics(st), "stall")
        for way_out in ("exit", "abandon", "extend"):
            self.assertIn(way_out, hint)


class BudgetGrantTests(unittest.TestCase):
    """预算按节点发放：这是"图激活期不设迭代上限"的落地方式。"""

    def test_grant_happens_on_entry_with_the_node_s_own_estimate(self):
        st = _state()
        _two_node_graph(st)                       # n1.budget=8, n2 无 budget
        self.assertEqual(st.meta.get("_add_iterations"), None)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.assertEqual(st.meta["_add_iterations"], 8)
        self.assertEqual(G.get_root(st)["budget_granted"], 8)

    def test_planning_nodes_you_never_enter_mints_nothing(self):
        """画了不走的节点一分钱不发——否则模型能靠画图凭空铸造预算。

        这不是"模型想作弊"，是结构诱导：快没预算时画图恰好是它眼前
        最像"推进"的动作。堵住的是这个诱导。
        """
        st = _state()
        G.create_graph(st, title="铸币尝试", nodes=[
            {"title": f"步骤{i}", "goal": "x", "budget": 50} for i in range(10)
        ])
        self.assertIsNone(st.meta.get("_add_iterations"))
        self.assertEqual(G.get_root(st)["budget_granted"], 0)

    def test_each_node_grants_only_once(self):
        """反复进出同一个节点不能重复领取。"""
        st = _state()
        _two_node_graph(st)
        for _ in range(4):
            G.apply_op(st, {"op": "enter", "node": "n1"})
            G.apply_op(st, {"op": "exit", "node": "n1", "summary": "ok"})
        self.assertEqual(G.get_root(st)["budget_granted"], 8)

    def test_revising_budget_does_not_re_grant(self):
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        G.apply_revision(st, [{"op": "update", "node": "n1", "budget": 99}])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.assertEqual(G.get_root(st)["budget_granted"], 8)

    def test_topup_only_while_there_is_open_work(self):
        st = _state()
        _two_node_graph(st)
        self.assertGreater(G.topup_budget(st), 0)
        for nid in ("n1", "n2"):
            G.apply_op(st, {"op": "enter", "node": nid})
            G.apply_op(st, {"op": "exit", "node": nid, "summary": "ok"})
        self.assertEqual(G.topup_budget(st), 0)       # 图上没活了，不再补
        self.assertFalse(G.has_open_work(st))

    def test_topup_is_recorded_even_though_uncapped(self):
        """不设上限是产品决策，但每一笔都要看得见。"""
        st = _state()
        _two_node_graph(st)
        before = G.get_root(st)["budget_granted"]
        total = sum(G.topup_budget(st) for _ in range(5))
        self.assertEqual(G.get_root(st)["budget_granted"], before + total)
        self.assertEqual(G.summary(st)["budget_granted"], before + total)

    def test_no_graph_means_no_grants_at_all(self):
        st = _state()
        self.assertEqual(G.topup_budget(st), 0)
        self.assertFalse(G.has_open_work(st))
        self.assertIsNone(st.meta.get("_add_iterations"))

    def test_read_paths_never_implant_an_empty_graph(self):
        """绝大多数 run 从不建图。"问一句有没有图"就在 meta 里种下空壳，
        会让每个 run 的 meta.json 都多出这块噪声，"这个 run 用过图吗"
        也就没法再靠键是否存在来回答。"""
        st = _state()
        for probe in (G.has_open_work, G.active_graph, G.metrics, G.summary,
                      G.open_nodes, G.render, G.gap_lines, G.topup_budget,
                      G.pending_isolate, G.peek_root):
            probe(st)
            self.assertNotIn("_graph", st.meta, f"{probe.__name__} 不该创建图根")
        G.save(st)
        self.assertNotIn("_graph", st.meta)


class IsolateTests(unittest.TestCase):
    def test_nodes_do_not_seal_by_default(self):
        """封段等于 KV 缓存清零，每个节点都封会比规划本身贵一个量级。"""
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.assertIsNone(G.pending_isolate(st))

    def test_isolate_node_is_flagged_once_then_marked_sealed(self):
        st = _state()
        G.create_graph(st, title="隔离", nodes=[
            {"title": "重活", "goal": "过程细节很多", "isolate": True},
        ])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        node = G.pending_isolate(st)
        self.assertIsNotNone(node)
        self.assertEqual(node["id"], "n1")
        G.mark_sealed(st, node, 3)
        self.assertIsNone(G.pending_isolate(st))      # 封过就不再重复
        self.assertEqual(st.meta["_graph"]["graphs"][0]["nodes"]["n1"]["seg"], 3)


class GapHandoffTests(unittest.TestCase):
    def test_open_nodes_flow_into_run_outcome_gaps(self):
        """未闭合节点是迄今最好的结构化 gaps，自动续作层直接消费。"""
        from agent.core.loop import RUN_OUTCOME_EXHAUSTED, _set_run_outcome
        st = _state()
        _two_node_graph(st, expect=["out.txt"])
        rec = _set_run_outcome(st, RUN_OUTCOME_EXHAUSTED, reason="iteration_budget_exhausted")
        self.assertTrue(any("n1" in g and "第一步" in g for g in rec["gaps"]))
        self.assertEqual([g["node"] for g in rec["graph_gaps"]], ["n1", "n2"])
        self.assertEqual(rec["graph_gaps"][0]["exit"]["expect"], ["out.txt"])

    def test_gaps_stay_a_list_of_strings(self):
        """既有契约是 list[str]，消费方按字符串处理，不能被结构化数据打破。"""
        from agent.core.loop import RUN_OUTCOME_PARTIAL, _set_run_outcome
        st = _state()
        _two_node_graph(st)
        st.meta["completion_report"] = {"remaining_gaps": ["原有的自由文本缺口"]}
        rec = _set_run_outcome(st, RUN_OUTCOME_PARTIAL)
        self.assertTrue(all(isinstance(g, str) for g in rec["gaps"]))
        self.assertIn("原有的自由文本缺口", rec["gaps"])

    def test_no_graph_leaves_outcome_untouched(self):
        from agent.core.loop import RUN_OUTCOME_COMPLETED, _set_run_outcome
        st = _state()
        rec = _set_run_outcome(st, RUN_OUTCOME_COMPLETED)
        self.assertEqual(rec["gaps"], [])
        self.assertNotIn("graph_gaps", rec)


class AdvisorContextTests(unittest.TestCase):
    def test_advisor_sees_the_graph(self):
        """advisor 原本只读得到散文，看不出"在两个节点间来回"这类结构性停滞。"""
        from agent.core.advisor import _build_advisor_context
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        ctx = _build_advisor_context(st)
        # 断言用语言中立的内容：本仓库 zh/en 双语，LANG 由环境决定
        self.assertIn("第一步", ctx)          # 节点标题（用户数据，不翻译）
        self.assertIn("graph_op", ctx)        # 投影里的协议提示，两种语言都有
        self.assertIn("n1", ctx)

    def test_advisor_context_unchanged_without_a_graph(self):
        from agent.core.advisor import _build_advisor_context
        self.assertNotIn("graph_op", _build_advisor_context(_state()))


class RobustnessTests(unittest.TestCase):
    def test_garbage_ops_never_raise(self):
        st = _state()
        _two_node_graph(st)
        for bad in ({"op": "nonsense"}, "garbage", None, [], {"op": "enter"},
                    {"op": "exit", "node": "n99"}, {"op": "extend", "after": "nope", "node": {}}):
            ok, msg = G.apply_op(st, bad)
            self.assertFalse(ok)
            self.assertTrue(msg)

    def test_ops_without_a_graph_are_rejected_cleanly(self):
        st = _state()
        ok, msg = G.apply_op(st, {"op": "enter", "node": "n1"})
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_graph_json_is_written_and_reloadable(self):
        st = _state()
        _two_node_graph(st)
        G.apply_op(st, {"op": "enter", "node": "n1"})
        disk = json.loads((Path(st.persistence.run_dir) / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["graphs"][0]["nodes"]["n1"]["status"], "active")
        self.assertEqual(disk["version"], 1)

    def test_graph_key_is_not_reset_on_resume(self):
        """图是必须跨续跑延续的载体，清掉等于每次续跑都把地图撕了重画。"""
        from agent.core.loop import _RESUME_RESET_KEYS
        self.assertNotIn("_graph", _RESUME_RESET_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
