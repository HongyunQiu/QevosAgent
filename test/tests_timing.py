#!/usr/bin/env python3
"""时间账本回归测试（批 A：纯计量）。

守三条底线：
  1. 账要记得准，且分类互斥——"服务端在拒绝我"不能被记成"模型很慢"
  2. 跨进程续跑不能把两个进程的时钟差当成本次耗时
  3. 测试**绝不真 sleep**——时间相关的 bug 最难查，必须能确定性复现

设计见 doc/execution-graph.md §7B
"""
import tempfile
import unittest

from agent.core import graph as G
from agent.core import timing as T
from agent.core.types_def import AgentState
from agent.runtime.persistence import RunPersistence


class FakeClock:
    """可手动推进的单调钟。用它替换 time.monotonic，测试零等待。"""

    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


class TimingTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self._prev = T.set_clock(self.clock)
        self.addCleanup(T.set_clock, self._prev)

    def _state(self):
        st = AgentState(goal="计时测试")
        st.persistence = RunPersistence(tempfile.mkdtemp(prefix="timing-"))
        return st


class LedgerBasicsTests(TimingTestCase):
    def test_no_ledger_until_started(self):
        st = self._state()
        self.assertIsNone(T.peek(st))
        self.assertEqual(T.snapshot(st), {})
        self.assertEqual(T.total_seconds(st), 0.0)

    def test_tick_accumulates_total(self):
        st = self._state()
        T.start(st)
        self.clock.advance(30)
        T.tick(st)
        self.clock.advance(12)
        T.tick(st)
        self.assertAlmostEqual(T.total_seconds(st), 42.0)

    def test_span_attributes_to_category(self):
        st = self._state()
        T.start(st)
        with T.span(st, "tool"):
            self.clock.advance(7)
        self.assertAlmostEqual(T.peek(st)["tool"], 7.0)

    def test_span_records_even_when_the_body_raises(self):
        """失败的调用同样花了时间，不记就等于把它变成了免费的。"""
        st = self._state()
        T.start(st)
        with self.assertRaises(ValueError):
            with T.span(st, "tool"):
                self.clock.advance(5)
                raise ValueError("boom")
        self.assertAlmostEqual(T.peek(st)["tool"], 5.0)

    def test_unknown_category_is_ignored_not_fatal(self):
        st = self._state()
        T.start(st)
        T.add(st, "nonsense", 10)
        self.assertNotIn("nonsense", T.peek(st))

    def test_untracked_is_the_honest_remainder(self):
        """total 是实测的、分类是归因的，差额自然是未归类开销。
        强行让分类加满反而会说谎。"""
        st = self._state()
        T.start(st)
        with T.span(st, "llm"):
            self.clock.advance(10)
        self.clock.advance(4)          # 解析、记账、落盘之类，没被归类
        T.tick(st)
        snap = T.snapshot(st)
        self.assertAlmostEqual(snap["total"], 14.0)
        self.assertAlmostEqual(snap["llm"], 10.0)
        self.assertAlmostEqual(snap["untracked"], 4.0)


class ActiveVsWallTests(TimingTestCase):
    def test_active_excludes_paused(self):
        """run 级时限用 total（对现实的承诺），图级配额用 active（工作量额度）。"""
        st = self._state()
        T.start(st)
        self.clock.advance(100)
        T.tick(st)
        T.add(st, "paused", 60)
        self.assertAlmostEqual(T.total_seconds(st), 100.0)
        self.assertAlmostEqual(T.active_seconds(st), 40.0)

    def test_active_never_goes_negative(self):
        st = self._state()
        T.start(st)
        T.add(st, "paused", 999)
        self.assertEqual(T.active_seconds(st), 0.0)

    def test_absorb_pause_uses_wall_clock_gap(self):
        """暂停期间进程根本不在跑，单调钟无从计量——只能对比上次落盘的墙上时刻。"""
        from datetime import datetime, timedelta, timezone
        st = self._state()
        T.start(st)
        book = T.ledger(st)
        earlier = datetime.now(timezone.utc) - timedelta(seconds=120)
        book["_wall_seen"] = earlier.isoformat().replace("+00:00", "Z")
        gap = T.absorb_pause(st)
        self.assertGreater(gap, 100)
        self.assertGreater(book["paused"], 100)
        # 暂停既算进 total（现实流逝），又被 active 扣掉（不是工作时间）
        self.assertGreater(T.total_seconds(st), 100)
        self.assertLess(T.active_seconds(st), 1)

    def test_absorb_pause_is_a_noop_without_a_prior_stamp(self):
        st = self._state()
        T.start(st)
        T.ledger(st).pop("_wall_seen", None)
        self.assertEqual(T.absorb_pause(st), 0.0)


class CrossProcessTests(TimingTestCase):
    def test_start_reanchors_so_clock_gaps_are_not_billed(self):
        """`_mark` 是进程内的单调钟读数，跨进程毫无意义。
        续跑时若沿用，会把两个进程的时钟差当成本次运行的耗时记进去。"""
        st = self._state()
        T.start(st)
        self.clock.advance(20)
        T.tick(st)
        self.assertAlmostEqual(T.total_seconds(st), 20.0)

        # 模拟新进程：单调钟跳到一个完全无关的值，账本从磁盘恢复
        self.clock.t = 999999.0
        T.start(st)                      # 重打锚点
        self.clock.advance(5)
        T.tick(st)
        self.assertAlmostEqual(T.total_seconds(st), 25.0)   # 而不是 20 + 巨大差值

    def test_ledger_survives_a_meta_round_trip(self):
        import json
        st = self._state()
        T.start(st)
        with T.span(st, "llm"):
            self.clock.advance(9)
        T.tick(st)
        revived = AgentState(goal="x")
        revived.meta["_time"] = json.loads(json.dumps(T.peek(st)))
        self.assertAlmostEqual(T.peek(revived)["llm"], 9.0)


class SnapshotTests(TimingTestCase):
    def test_snapshot_is_json_serialisable_and_complete(self):
        import json
        st = self._state()
        T.start(st)
        for cat, secs in (("llm", 3), ("tool", 4), ("wait", 5), ("retry", 1), ("paused", 2)):
            T.add(st, cat, secs)
        self.clock.advance(20)
        T.tick(st)
        snap = T.snapshot(st)
        json.dumps(snap)
        for key in ("total", "active", "untracked", *T.CATEGORIES):
            self.assertIn(key, snap)
        self.assertAlmostEqual(snap["active"], snap["total"] - snap["paused"])

    def test_snapshot_lands_in_status_json(self):
        import json
        from pathlib import Path
        st = self._state()
        T.start(st)
        with T.span(st, "tool"):
            self.clock.advance(11)
        T.tick(st)
        st.persistence.checkpoint(st)
        status = json.loads((Path(st.persistence.run_dir) / "status.json").read_text(encoding="utf-8"))
        self.assertIn("time", status)
        self.assertAlmostEqual(status["time"]["tool"], 11.0)

    def test_no_ledger_leaves_status_time_empty(self):
        """从未计过时的 run 不该被塞进一块空账本噪声。"""
        import json
        from pathlib import Path
        st = self._state()
        st.persistence.checkpoint(st)
        status = json.loads((Path(st.persistence.run_dir) / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status.get("time"), {})

    def test_fmt_reads_naturally(self):
        self.assertEqual(T.fmt(45), "45s")
        self.assertEqual(T.fmt(90), "1m30s")
        self.assertEqual(T.fmt(120), "2m")
        self.assertEqual(T.fmt(3720), "1h02m")
        self.assertEqual(T.fmt(-5), "0s")


class NodeTimingTests(TimingTestCase):
    def test_node_records_its_own_elapsed(self):
        st = self._state()
        T.start(st)
        G.create_graph(st, title="计时", nodes=[{"title": "干活", "goal": "做点事"}])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.clock.advance(300)
        T.tick(st)
        G.apply_op(st, {"op": "exit", "node": "n1", "summary": "完成"})
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n1"]
        self.assertAlmostEqual(G.node_seconds(node), 300.0, places=1)

    def test_paused_time_does_not_inflate_node_elapsed(self):
        """节点耗时用 active——人去睡觉一小时不该记在这个节点头上。"""
        from datetime import datetime, timedelta, timezone
        st = self._state()
        T.start(st)
        G.create_graph(st, title="计时", nodes=[{"title": "干活", "goal": "做点事"}])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.clock.advance(100)
        T.tick(st)
        # 走真实路径模拟"暂停一小时等人"：absorb_pause 同时记 paused 与 total
        T.ledger(st)["_wall_seen"] = (
            datetime.now(timezone.utc) - timedelta(seconds=3600)
        ).isoformat().replace("+00:00", "Z")
        T.absorb_pause(st)
        G.apply_op(st, {"op": "exit", "node": "n1", "summary": "完成"})
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n1"]
        self.assertAlmostEqual(G.node_seconds(node), 100.0, places=0)

    def test_paused_never_exceeds_total_through_the_real_path(self):
        """paused ≤ total 是账本的不变量：absorb_pause 两边一起记才维持得住。
        直接 add('paused') 会破坏它，因此那是内部用法，不走这条路。"""
        from datetime import datetime, timedelta, timezone
        st = self._state()
        T.start(st)
        for gap in (30, 90, 600):
            T.ledger(st)["_wall_seen"] = (
                datetime.now(timezone.utc) - timedelta(seconds=gap)
            ).isoformat().replace("+00:00", "Z")
            T.absorb_pause(st)
            book = T.peek(st)
            self.assertLessEqual(book["paused"], book["total"] + 1e-6)

    def test_abandoned_node_also_gets_stamped(self):
        st = self._state()
        T.start(st)
        G.create_graph(st, title="计时", nodes=[{"title": "干活", "goal": "做点事"}])
        G.apply_op(st, {"op": "enter", "node": "n1"})
        self.clock.advance(50)
        T.tick(st)
        G.apply_op(st, {"op": "abandon", "node": "n1", "reason": "不通"})
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n1"]
        self.assertAlmostEqual(G.node_seconds(node), 50.0, places=1)

    def test_never_entered_node_has_no_elapsed(self):
        st = self._state()
        T.start(st)
        G.create_graph(st, title="计时", nodes=[{"title": "a", "goal": "x"}, {"title": "b", "goal": "y"}])
        G.apply_op(st, {"op": "abandon", "node": "n2", "reason": "不做了"})
        node = st.meta["_graph"]["graphs"][0]["nodes"]["n2"]
        self.assertIsNone(G.node_seconds(node))

    def test_graph_works_without_any_ledger(self):
        """批 A 是纯增量：没记时的 run，图的行为必须一字不变。"""
        st = self._state()
        G.create_graph(st, title="无账本", nodes=[{"title": "a", "goal": "x"}])
        self.assertTrue(G.apply_op(st, {"op": "enter", "node": "n1"})[0])
        self.assertTrue(G.apply_op(st, {"op": "exit", "node": "n1", "summary": "ok"})[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
