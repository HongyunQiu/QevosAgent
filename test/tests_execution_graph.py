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

    def test_fanout_flags_a_plan_that_only_widens(self):
        st = _state()
        G.create_graph(st, title="只长叶子", nodes=[
            {"title": f"步骤{i}", "goal": "x"} for i in range(8)
        ])
        m = G.metrics(st)
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
