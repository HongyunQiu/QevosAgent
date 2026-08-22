#!/usr/bin/env python3
"""验收门 / run 级终态回归测试（层 0：判定基础）。

这些用例守的是自动续作的前提：终态必须存在、必须可区分、必须带得走 gaps。
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.core.loop import (
    RUN_OUTCOME_ABORTED,
    RUN_OUTCOME_BLOCKED,
    RUN_OUTCOME_COMPLETED,
    RUN_OUTCOME_EXHAUSTED,
    RUN_OUTCOME_PARTIAL,
    _RESUME_RESET_KEYS,
    _finalize_run,
    _review_completion_report,
    _set_run_outcome,
    _wrapup_blocks,
    run,
)
from agent.core.types_def import AgentHooks, AgentState, ToolResult, ToolSpec
from agent.runtime.persistence import RunPersistence


def _report(outcome="done", gaps=None, completed=None):
    return {
        "goal_understanding": "把 A 转成 B",
        "completed_work": completed or ["实现了 A→B 主流程"],
        "remaining_gaps": gaps or [],
        "evidence_type": "none",
        "evidence": [],
        "outcome": outcome,
        "confidence": "high",
    }


class SetRunOutcomeTests(unittest.TestCase):
    def test_extracts_gaps_and_marks_resumable(self):
        state = AgentState(goal="g")
        state.meta["completion_report"] = _report("done_partial", gaps=["格式 C 未实现"])

        rec = _set_run_outcome(state, RUN_OUTCOME_PARTIAL, reason="partial_completion")

        self.assertEqual(rec["outcome"], RUN_OUTCOME_PARTIAL)
        self.assertTrue(rec["resumable"])
        self.assertEqual(rec["gaps"], ["格式 C 未实现"])

    def test_completed_is_not_resumable(self):
        state = AgentState(goal="g")
        rec = _set_run_outcome(state, RUN_OUTCOME_COMPLETED)
        self.assertFalse(rec["resumable"])

    def test_first_writer_wins(self):
        """先到达的路径最贴近真实结束原因，后来的兜底不得覆盖。"""
        state = AgentState(goal="g")
        _set_run_outcome(state, RUN_OUTCOME_PARTIAL, reason="partial_completion")
        _set_run_outcome(state, RUN_OUTCOME_EXHAUSTED, reason="iteration_budget_exhausted")
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_PARTIAL)


class FinalizeRunOutcomeTests(unittest.TestCase):
    """pass 与 weak_pass 在 status 上都落 done，只有 run_outcome 能区分它们。"""

    def _finalize(self, verdict, report, nostop=False):
        state = AgentState(goal="g")
        state.meta["completion_report"] = report
        ret = _finalize_run(
            state,
            "最终答案",
            verdict,
            {"status": verdict, "reason": "r", "report": report},
            AgentHooks(),
            nostop,
        )
        return state, ret

    def test_pass_marks_completed(self):
        state, ret = self._finalize("pass", _report("done"))
        self.assertEqual(ret, "done")
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_COMPLETED)

    def test_weak_pass_partial_marks_partial_and_pauses(self):
        state, ret = self._finalize("weak_pass", _report("done_partial", gaps=["缺 C"]))
        self.assertEqual(ret, "paused")
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_PARTIAL)
        self.assertEqual(state.meta["run_outcome"]["gaps"], ["缺 C"])

    def test_weak_pass_blocked_marks_blocked(self):
        state, _ = self._finalize("weak_pass", _report("done_blocked", gaps=["API 不可达"]))
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_BLOCKED)

    def test_nostop_weak_pass_still_records_partial(self):
        """nostop 不 pause，但它仍然是一次部分完成，落盘不能混成完整完成。"""
        state, ret = self._finalize("weak_pass", _report("done_partial", gaps=["缺 C"]), nostop=True)
        self.assertEqual(ret, "done")
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_PARTIAL)


class ResumeResetTests(unittest.TestCase):
    def test_stale_report_would_repeat_weak_pass(self):
        """不清旧报告时，门 1 会拿上一轮的报告再判一次 weak_pass。"""
        state = AgentState(goal="g")
        state.meta["completion_report"] = _report("done_partial", gaps=["缺 C"])

        verdict, _ = _review_completion_report(state, "答案")
        self.assertEqual(verdict, "weak_pass")

        # 续跑重置后，门 1 要求 agent 重新提交，不会照搬旧结论
        for key in _RESUME_RESET_KEYS:
            state.meta.pop(key, None)
        verdict2, vd2 = _review_completion_report(state, "答案")
        self.assertEqual(verdict2, "needs_more_work")
        self.assertEqual(vd2["reason"], "missing_completion_report")

    def test_reset_keys_cover_memory_gates_and_wrapup(self):
        """门 2/3 标记与收尾窗口标记必须在续跑时清掉，否则续跑的工作没有记忆沉淀、
        或一进来工具就被全禁。"""
        for key in (
            "completion_report", "completion_review", "run_outcome",
            "_episodic_appended", "_concept_evaluated",
            "_wrapup_window", "_wrapup_window_used", "_stale_report_rejections",
        ):
            self.assertIn(key, _RESUME_RESET_KEYS)


class WrapupWindowTests(unittest.TestCase):
    def test_inactive_window_blocks_nothing(self):
        state = AgentState(goal="g")
        self.assertFalse(_wrapup_blocks(state, "run_python"))

    def test_active_window_blocks_execution_tools_only(self):
        state = AgentState(goal="g")
        state.meta["_wrapup_window"] = True
        self.assertTrue(_wrapup_blocks(state, "run_python"))
        self.assertTrue(_wrapup_blocks(state, "write_file"))
        self.assertFalse(_wrapup_blocks(state, "submit_completion_report"))
        self.assertFalse(_wrapup_blocks(state, "append_episodic"))


class _ScriptedLLM:
    """按脚本逐条返回响应；脚本用尽后重复最后一条。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.last_finish_reason = "stop"

    def complete(self, messages, system):
        idx = min(self._i, len(self._responses) - 1)
        self._i += 1
        return self._responses[idx]


def _noop_tool(state, **kwargs):
    return ToolResult(success=True, output="ok")


def _call(tool, **args):
    return json.dumps({"thought": "t", "action": "tool_call", "tool": tool, "args": args})


def _done(answer="答案"):
    return json.dumps({"thought": "t", "action": "done", "final_answer": answer})


class ExhaustionTerminalStateTests(unittest.TestCase):
    """迭代耗尽必须留下 exhausted 终态，且要先给 agent 一次交代缺口的机会。"""

    def _run_until_exhausted(self, responses, max_iterations=3, meta=None):
        tools = {
            "noop": ToolSpec(
                name="noop", description="noop", args_schema={}, fn=_noop_tool
            ),
        }
        state = AgentState(goal="g", tools=dict(tools))
        if meta:
            state.meta.update(meta)
        return run(
            "g",
            _ScriptedLLM(responses),
            tools,
            max_iterations=max_iterations,
            hooks=AgentHooks(),
            state=state,
        )

    def test_exhaustion_sets_exhausted_outcome(self):
        call = json.dumps({"thought": "t", "action": "tool_call", "tool": "noop", "args": {}})
        state = self._run_until_exhausted([call], max_iterations=3)

        self.assertTrue(state.meta.get("timeout"))
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_EXHAUSTED)
        self.assertTrue(state.meta["run_outcome"]["resumable"])

    def test_wrapup_window_opens_before_hard_exit(self):
        call = json.dumps({"thought": "t", "action": "tool_call", "tool": "noop", "args": {}})
        state = self._run_until_exhausted([call], max_iterations=3)

        self.assertTrue(state.meta.get("_wrapup_window_used"))
        joined = "\n".join(
            m.get("content", "") for m in state.short_term if isinstance(m.get("content"), str)
        )
        self.assertIn("[系统][收尾窗口]", joined)

    def test_wrapup_window_blocks_execution_tool(self):
        call = json.dumps({"thought": "t", "action": "tool_call", "tool": "noop", "args": {}})
        state = self._run_until_exhausted([call], max_iterations=3)

        joined = "\n".join(
            m.get("content", "") for m in state.short_term if isinstance(m.get("content"), str)
        )
        self.assertIn("已被暂时禁用", joined)

    def test_prior_final_answer_does_not_mask_exhaustion(self):
        """weak_pass 已写过 final_answer；续跑后再耗尽不得被静默记成正常完成。"""
        call = json.dumps({"thought": "t", "action": "tool_call", "tool": "noop", "args": {}})
        state = self._run_until_exhausted(
            [call], max_iterations=3, meta={"final_answer": "上一轮的答案"}
        )

        self.assertTrue(state.meta.get("timeout"))
        self.assertEqual(state.meta["run_outcome"]["outcome"], RUN_OUTCOME_EXHAUSTED)


class StatusPayloadTests(unittest.TestCase):
    def test_status_json_exposes_run_outcome_and_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            persistence = RunPersistence(run_dir)
            state = AgentState(goal="g")
            state.meta["completion_report"] = _report("done_partial", gaps=["缺 C"])
            _set_run_outcome(state, RUN_OUTCOME_PARTIAL, reason="partial_completion")

            persistence.start(state)
            persistence.finish(state, outcome="done")

            payload = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            # status 仍是 done（生命周期），run_outcome 才区分完成质量
            self.assertEqual(payload["status"], "done")
            self.assertEqual(payload["run_outcome"], RUN_OUTCOME_PARTIAL)
            self.assertTrue(payload["resumable"])
            self.assertEqual(payload["run_outcome_detail"]["gaps"], ["缺 C"])

            summary = (run_dir / "execution_summary.md").read_text(encoding="utf-8")
            self.assertIn("run_outcome: partial", summary)
            self.assertIn("缺 C", summary)

    def test_completed_run_is_not_resumable_in_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            persistence = RunPersistence(run_dir)
            state = AgentState(goal="g")
            _set_run_outcome(state, RUN_OUTCOME_COMPLETED)
            persistence.start(state)
            persistence.finish(state, outcome="done")

            payload = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run_outcome"], RUN_OUTCOME_COMPLETED)
            self.assertFalse(payload["resumable"])


class ConceptGateTests(unittest.TestCase):
    """门 3（宏观记忆）不该白烧迭代。

    这道门每次打回都发生在 run 的最末尾——上下文最大、单次迭代最贵的时刻，
    而且模型一旦选择"整份重写记忆"，那一次解码就能吃掉整个 run 四成的墙上时间。
    所以这里守两件事：已经写过记忆就别再问；真的写了就直接收尾，别再要一次 done。
    """

    @staticmethod
    def _meta_tool(**updates):
        def _fn(state, **kwargs):
            state.meta.update(updates)
            return ToolResult(success=True, output="ok")
        return _fn

    def _run(self, tail_responses, extra_tools=None, max_iterations=12):
        # run() 开头会清掉 _RESUME_RESET_KEYS，所以门 1/2 的前置状态必须由
        # 循环内的工具调用真实产生，不能靠预置 meta。
        tools = {
            "noop": ToolSpec(name="noop", description="noop", args_schema={}, fn=_noop_tool),
            "report": ToolSpec(
                name="report", description="r", args_schema={},
                fn=self._meta_tool(completion_report=_report())),
            "episodic": ToolSpec(
                name="episodic", description="e", args_schema={},
                fn=self._meta_tool(_episodic_appended=True)),
            "mark_saved": ToolSpec(
                name="mark_saved", description="s", args_schema={},
                fn=self._meta_tool(_concept_saved=True)),
            "open_wrapup": ToolSpec(
                name="open_wrapup", description="w", args_schema={},
                fn=self._meta_tool(_wrapup_window=True)),
        }
        tools.update(extra_tools or {})
        responses = [_call("report"), _call("episodic")] + list(tail_responses)
        state = AgentState(goal="g", tools=dict(tools))
        return run(
            "g", _ScriptedLLM(responses), tools,
            max_iterations=max_iterations, hooks=AgentHooks(), state=state,
        )

    @staticmethod
    def _joined(state):
        return "\n".join(
            m.get("content", "") for m in state.short_term if isinstance(m.get("content"), str)
        )

    def test_gate_fires_when_memory_untouched(self):
        state = self._run([_done(), _done()])

        self.assertIn("[系统][记忆评估]", self._joined(state))
        self.assertEqual(state.meta["final_answer"], "答案")

    def test_gate_prompt_pushes_section_mode(self):
        """提示词必须默认引导章节模式：整份重写是这道门最贵的失败模式。"""
        joined = self._joined(self._run([_done(), _done()]))

        self.assertIn("section=", joined)
        self.assertIn("不需要再 read_concept", joined)

    def test_gate_prompt_omits_edit_file_in_wrapup_window(self):
        """收尾窗口里 edit_file 是被禁用的，提它只会换来一次被拦截的空转迭代。"""
        joined = self._joined(self._run([_call("open_wrapup"), _done(), _done()]))

        self.assertIn("[系统][记忆评估]", joined)
        self.assertNotIn("edit_file", joined)

    def test_gate_skipped_when_memory_already_saved(self):
        """本次运行中途已写过宏观记忆：再问一遍只会换来一句"已经更新过了"。"""
        state = self._run([_call("mark_saved"), _done()])

        self.assertNotIn("[系统][记忆评估]", self._joined(state))
        self.assertTrue(state.meta.get("_concept_evaluated"))
        self.assertEqual(state.meta["final_answer"], "答案")

    def test_shortcut_finalizes_on_any_concept_write(self):
        """捷径看的是"记忆真的被写了"，不是工具叫 save_concept——否则用 edit_file
        增量改记忆的那条省 token 路径反而享受不到收尾捷径。"""
        state = self._run(
            [_done(), _call("edit_file"), _done()],
            extra_tools={"edit_file": ToolSpec(
                name="edit_file", description="e", args_schema={},
                fn=self._meta_tool(_concept_dirty=True))},
        )

        self.assertIn("[系统][记忆评估]", self._joined(state))
        self.assertEqual(state.meta["final_answer"], "答案")
        # 捷径生效 = 收尾信息已被消费，没有再走一轮 done 复评
        self.assertIsNone(state.meta.get("_pending_final"))

    def test_shortcut_not_triggered_by_unrelated_tool(self):
        """打回时会清掉遗留的脏标记，普通工具调用不得被误判成"写了记忆"。"""
        state = self._run([_done(), _call("noop"), _done()])

        joined = self._joined(state)
        self.assertIn("[系统][记忆评估]", joined)
        self.assertIn("[Tool: noop]", joined)
        self.assertEqual(state.meta["final_answer"], "答案")

    def test_concept_flags_are_reset_on_resume(self):
        """续跑不清 _concept_saved，门 3 就永远被跳过，新认知再也沉淀不下来。"""
        for key in ("_concept_saved", "_concept_dirty", "_concept_gate_skipped"):
            self.assertIn(key, _RESUME_RESET_KEYS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
