#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.core.executor import execute
from agent.core.loop import (
    _build_feedback,
    _extract_claimed_artifact_paths,
    _failure_class,
    _parse_acceptance_evidence,
)
from agent.core.types_def import Action, ActionType, AgentState, ToolResult, ToolSpec
from agent.runtime.persistence import RunPersistence
from agent.tools.standard import (
    get_standard_tools,
    tool_edit_file,
    tool_promote_tool_candidate,
    tool_repair_tool_candidate,
    tool_save_concept,
    tool_scratchpad_set,
    tool_validate_tool_recipe,
    tool_write_file,
)
import run_goal
from run_goal import format_probe_summary, probe_openai_configuration


class ExecuteArgFilteringTests(unittest.TestCase):
    def test_execute_ignores_unknown_args_declared_outside_schema(self):
        seen = {}

        def sample_tool(state, code):
            seen["code"] = code
            return ToolResult(success=True, output=code)

        state = AgentState(
            goal="test",
            tools={
                "sample_tool": ToolSpec(
                    name="sample_tool",
                    description="sample",
                    args_schema={"code": "Python code"},
                    fn=sample_tool,
                )
            },
        )

        action = Action(
            type=ActionType.TOOL_CALL,
            thought="test",
            tool="sample_tool",
            args={"code": "print('ok')", "timeout": 20},
        )

        result = execute(action, state)

        self.assertTrue(result.success)
        self.assertEqual(seen["code"], "print('ok')")


class ToolRepairFlowTests(unittest.TestCase):
    def _make_state_with_broken_tool(self):
        def broken_tool(state, url):
            return ToolResult(success=False, output=None, error=f"broken:{url}")

        state = AgentState(
            goal="repair",
            tools={
                "http_get": ToolSpec(
                    name="http_get",
                    description="broken http get",
                    args_schema={"url": "target url"},
                    fn=broken_tool,
                )
            },
        )
        state.meta["evolved_tools"] = {
            "http_get": {
                "name": "http_get",
                "description": "broken http get",
                "args_schema": {"url": "target url"},
                "python_code": (
                    "def run(state, url):\n"
                    "    return ToolResult(success=False, output=None, error='broken')\n"
                ),
            }
        }
        return state

    def test_invalid_candidate_cannot_be_promoted(self):
        state = self._make_state_with_broken_tool()
        invalid_code = (
            "def run(state, url):\n"
            "    return ToolResult(output=url, error=None)\n"
        )

        validate_result = tool_validate_tool_recipe(
            state=state,
            name="http_get",
            description="candidate",
            args_schema={"url": "target url"},
            python_code=invalid_code,
        )
        self.assertFalse(validate_result.output["ok"])

        candidate_result = tool_repair_tool_candidate(
            state=state,
            name="http_get",
            description="candidate",
            args_schema={"url": "target url"},
            python_code=invalid_code,
        )
        self.assertFalse(candidate_result.success)

        promote_result = tool_promote_tool_candidate(state=state, name="http_get")
        self.assertFalse(promote_result.success)

    def test_valid_candidate_promotes_and_replaces_formal_tool(self):
        state = self._make_state_with_broken_tool()
        fixed_code = (
            "def run(state, url):\n"
            "    return ToolResult(success=True, output='fixed:' + url)\n"
        )

        validate_result = tool_validate_tool_recipe(
            state=state,
            name="http_get",
            description="fixed tool",
            args_schema={"url": "target url"},
            python_code=fixed_code,
        )
        self.assertTrue(validate_result.output["ok"])

        candidate_result = tool_repair_tool_candidate(
            state=state,
            name="http_get",
            description="fixed tool",
            args_schema={"url": "target url"},
            python_code=fixed_code,
        )
        self.assertTrue(candidate_result.success)
        self.assertIn("http_get", state.meta["tool_repair_candidates"])

        promote_result = tool_promote_tool_candidate(state=state, name="http_get")
        self.assertTrue(promote_result.success)
        self.assertNotIn("http_get", state.meta.get("tool_repair_candidates", {}))
        self.assertEqual(state.meta["evolved_tools"]["http_get"]["python_code"], fixed_code.strip())

        result = state.tools["http_get"].fn(state=state, url="example.com")
        self.assertTrue(result.success)
        self.assertEqual(result.output, "fixed:example.com")


class LoopDetectionTests(unittest.TestCase):
    """回归：run 20260806-183910 连续 14 轮完全相同的 shell 调用无人拦截。

    根因是模型把 thought 写成嵌套对象，`thought.lower()` 抛 AttributeError 被
    静默吞掉，导致 repeat_warning / _loop_advisor_pending 全部失效，且签名历史
    永久停止增长。
    """

    @staticmethod
    def _action(thought):
        return Action(
            type=ActionType.TOOL_CALL,
            thought=thought,
            tool="shell",
            args={"command": "ffmpeg -i a.mp4 -frames:v 1 -y /tmp/f.png 2>&1"},
        )

    @staticmethod
    def _failure():
        return ToolResult(success=False, output="ffmpeg: not found", error="exit code 127")

    def _run_n(self, thought, n, tool="shell", result=None):
        state = AgentState(goal="loop test")
        feedbacks = []
        for i in range(n):
            action = self._action(thought)
            action.tool = tool
            feedbacks.append(
                _build_feedback(action, result(i) if result else self._failure(), state)
            )
        return state, feedbacks

    def test_identical_failing_calls_trigger_loop_detection(self):
        state, feedbacks = self._run_n("提取视频帧确认格子形态", 5)
        self.assertTrue(state.meta.get("_loop_advisor_pending"))
        self.assertTrue(any("循环检测" in f for f in feedbacks))
        self.assertEqual(len(state.meta["_call_sig_history"]), 5)

    def test_dict_thought_still_triggers_loop_detection(self):
        thought = {"Observe": "画面是格子", "Decide": "提取视频帧"}
        state, feedbacks = self._run_n(thought, 5)
        self.assertTrue(
            state.meta.get("_loop_advisor_pending"),
            "dict 型 thought 不能让循环检测熄火",
        )
        self.assertTrue(any("循环检测" in f for f in feedbacks))

    def test_sig_history_keeps_growing_even_if_detection_body_raises(self):
        # thought 为不可 JSON 序列化的对象：走 default=str 兜底，仍须正常计数
        state, _ = self._run_n(object(), 4)
        self.assertEqual(len(state.meta["_call_sig_history"]), 4)

    def test_polling_thought_is_exempt_from_loop_detection(self):
        state, feedbacks = self._run_n("等待下载完成，稍等再看", 5)
        self.assertIsNone(state.meta.get("_loop_advisor_pending"))
        self.assertTrue(any("轮询提示" in f for f in feedbacks))

    # ── 轮询豁免的收窄 ──────────────────────────────────────────────────────
    def test_retry_wording_is_not_a_polling_exemption(self):
        # "重试"/"retry" 是卡死的模型最爱说的词，放行等于发免死金牌
        for thought in ("失败了，让我重试一次", "command failed, will retry"):
            state, feedbacks = self._run_n(thought, 5)
            self.assertTrue(
                state.meta.get("_loop_advisor_pending"),
                f"{thought!r} 不该被当成轮询豁免",
            )
            self.assertTrue(any("循环检测" in f for f in feedbacks))

    def test_short_incidental_words_are_not_polling_exemptions(self):
        # 裸的"进度"/"尚未"/"还没"/"稍后"/"wait" 在正常叙述里俯拾皆是，不再豁免
        for thought in ("进度不对", "尚未找到原因", "还没定位到", "稍后总结", "no wait, check again"):
            state, _ = self._run_n(thought, 5)
            self.assertTrue(
                state.meta.get("_loop_advisor_pending"),
                f"{thought!r} 不该被当成轮询豁免",
            )

    def test_polling_exemption_is_revoked_when_result_never_changes(self):
        # 豁免是宽限期不是免检权：结果一字不差地重复到硬上限就撤销
        state, feedbacks = self._run_n("等待下载完成", 8)
        self.assertTrue(state.meta.get("_loop_advisor_pending"))
        self.assertTrue(any("轮询无进展" in f for f in feedbacks))

    def test_real_polling_with_changing_result_never_triggers(self):
        # 真轮询的结果在变 → 签名跟着变 → consecutive 归零 → 永远碰不到上限
        state, feedbacks = self._run_n(
            "等待下载完成",
            30,
            result=lambda i: ToolResult(success=True, output=f"downloaded {i}MB"),
        )
        self.assertIsNone(state.meta.get("_loop_advisor_pending"))
        self.assertFalse(any("循环检测" in (f or "") for f in feedbacks))

    def test_job_wait_exemption_is_wider_but_still_finite(self):
        # job_wait 天然豁免，上限放宽到 20，但不是无限
        state, _ = self._run_n("查后台任务", 8, tool="job_wait")
        self.assertIsNone(state.meta.get("_loop_advisor_pending"))

        state, feedbacks = self._run_n("查后台任务", 20, tool="job_wait")
        self.assertTrue(state.meta.get("_loop_advisor_pending"))
        self.assertTrue(any("轮询无进展" in f for f in feedbacks))

    def test_failure_feedback_includes_output_not_just_error(self):
        action = self._action("提取视频帧")
        result = ToolResult(success=False, output="ffmpeg: not found", error="exit code 127")
        feedback = _build_feedback(action, result, AgentState(goal="x"))
        self.assertIn("exit code 127", feedback)
        self.assertIn("ffmpeg: not found", feedback)

    def test_empty_error_feedback_is_not_blank(self):
        action = self._action("提取视频帧")
        result = ToolResult(success=False, output="", error="")
        feedback = _build_feedback(action, result, AgentState(goal="x"))
        self.assertIn("无错误信息", feedback)


class ShellExitCodeTests(unittest.TestCase):
    """回归：命令自带 2>&1 时 stderr 为空，失败只回一条空 Error，模型无从调整。"""

    def test_failure_error_carries_exit_code_and_stdout(self):
        from agent.tools.standard import tool_shell

        state = AgentState(goal="x")
        # stderr 被重定向进 stdout → stderr_text 为空，错误信息必须由 stdout 兜底
        cmd = ("echo boom_marker 2>&1 & exit 3" if os.name == "nt"
               else "echo boom_marker 2>&1; exit 3")
        result = tool_shell(state, cmd)
        self.assertFalse(result.success)
        self.assertIn("exit code 3", result.error)
        self.assertIn("boom_marker", result.error)

    def test_success_has_no_error(self):
        from agent.tools.standard import tool_shell

        result = tool_shell(AgentState(goal="x"), "echo ok")
        self.assertTrue(result.success)
        self.assertIsNone(result.error)


class AcceptancePathParsingTests(unittest.TestCase):
    def test_parse_acceptance_evidence_for_tool_result_skips_artifact_checks(self):
        text = (
            "ACCEPTANCE:\n"
            "- criteria: 成功回复用户\n"
            "- evidence_type: tool_result\n"
            "- evidence: load_snapshot_meta restored=5 skipped=0 long_term=13\n"
            "- verdict: PASS\n"
        )

        parsed = _parse_acceptance_evidence(text)

        self.assertEqual(parsed["evidence_type"], "tool_result")
        self.assertEqual(parsed["paths"], [])

    def test_parse_acceptance_evidence_for_artifact_extracts_paths(self):
        text = (
            "ACCEPTANCE:\n"
            "- criteria: 生成报告\n"
            "- evidence_type: artifact\n"
            "- evidence: runs/20260328-104525/artifacts/analysis_20260328-014328.md\n"
            "- verdict: PASS\n"
        )

        parsed = _parse_acceptance_evidence(text)

        self.assertEqual(parsed["evidence_type"], "artifact")
        self.assertEqual(
            parsed["paths"],
            ["runs/20260328-104525/artifacts/analysis_20260328-014328.md"],
        )

    def test_extract_claimed_artifact_paths_ignores_human_labels(self):
        text = (
            "ACCEPTANCE:\n"
            "- evidence: 分析报告路径 ./runs/20260328-104525/artifacts/analysis_20260328-014328.md (1015 字符)\n"
            "- verdict: PASS\n"
        )

        paths = _extract_claimed_artifact_paths(text, run_dir="runs/20260328-104525")

        self.assertEqual(
            paths,
            ["runs/20260328-104525/artifacts/analysis_20260328-014328.md"],
        )


class ProbeConfigTests(unittest.TestCase):
    def test_probe_openai_configuration_auto_switches_to_only_available_model(self):
        keys = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["OPENAI_BASE_URL"] = "http://model-host.example/v1"
            os.environ["OPENAI_API_KEY"] = "local"
            os.environ["OPENAI_MODEL"] = "qwen3527dgx"

            result = probe_openai_configuration(
                list_models=lambda: SimpleNamespace(
                    data=[SimpleNamespace(id="/models/only-one")]
                )
            )

            self.assertTrue(result["auto_selected"])
            self.assertEqual(result["resolved_model"], "/models/only-one")
            self.assertEqual(os.environ["OPENAI_MODEL"], "/models/only-one")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_probe_openai_configuration_raises_when_model_missing_from_multi_model_server(self):
        keys = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["OPENAI_BASE_URL"] = "http://model-host.example/v1"
            os.environ["OPENAI_API_KEY"] = "local"
            os.environ["OPENAI_MODEL"] = "qwen3527dgx"

            with self.assertRaisesRegex(ValueError, "qwen3527dgx"):
                probe_openai_configuration(
                    list_models=lambda: SimpleNamespace(
                        data=[
                            SimpleNamespace(id="model-a"),
                            SimpleNamespace(id="model-b"),
                        ]
                    )
                )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # ── 三槽位 + 首选 ────────────────────────────────────────────────
    ALL_SLOT_KEYS = (
        "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
        "BACKUP_OPENAI_BASE_URL", "BACKUP_OPENAI_API_KEY", "BACKUP_OPENAI_MODEL",
        "BACKUP2_OPENAI_BASE_URL", "BACKUP2_OPENAI_API_KEY", "BACKUP2_OPENAI_MODEL",
        "PREFERRED_API",
    )

    def _set_three_slots(self, preferred):
        """把三个槽位都配满，返回恢复现场的 cleanup 函数。"""
        old = {k: os.environ.get(k) for k in self.ALL_SLOT_KEYS}

        def restore():
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        for prefix, tag in (("", "one"), ("BACKUP_", "two"), ("BACKUP2_", "three")):
            os.environ[prefix + "OPENAI_BASE_URL"] = f"http://host-{tag}.example/v1"
            os.environ[prefix + "OPENAI_API_KEY"] = "local"
            os.environ[prefix + "OPENAI_MODEL"] = f"model-{tag}"
        if preferred is None:
            os.environ.pop("PREFERRED_API", None)
        else:
            os.environ["PREFERRED_API"] = preferred

    def _stub_probe(self, unreachable=()):
        """替掉真实网络探测；记录探测顺序，unreachable 里的 base 一律连不上。"""
        tried = []

        def fake_probe(base_url, api_key, model, list_models=None):
            tried.append(base_url)
            if base_url in unreachable:
                raise RuntimeError(f"无法连接 {base_url}")
            return [model]

        real = run_goal._probe_one_endpoint
        run_goal._probe_one_endpoint = fake_probe
        self.addCleanup(lambda: setattr(run_goal, "_probe_one_endpoint", real))
        return tried

    def test_preferred_slot_is_probed_first_and_wins(self):
        self._set_three_slots("backup2")
        tried = self._stub_probe()

        result = probe_openai_configuration()

        self.assertEqual(tried, ["http://host-three.example/v1"])
        self.assertEqual(result["active_endpoint"], "backup2")
        self.assertEqual(result["preferred_endpoint"], "backup2")
        # 胜出的槽写回 OPENAI_*，其余代码只认这一组
        self.assertEqual(os.environ["OPENAI_BASE_URL"], "http://host-three.example/v1")
        self.assertEqual(os.environ["OPENAI_MODEL"], "model-three")

    def test_falls_back_to_remaining_slots_in_slot_order(self):
        self._set_three_slots("backup")
        tried = self._stub_probe(unreachable={
            "http://host-two.example/v1",     # 首选（槽 2）挂
            "http://host-one.example/v1",     # 兜底第一顺位（槽 1）也挂
        })

        result = probe_openai_configuration()

        self.assertEqual(tried, [
            "http://host-two.example/v1",
            "http://host-one.example/v1",
            "http://host-three.example/v1",
        ])
        self.assertEqual(result["active_endpoint"], "backup2")
        self.assertEqual(result["preferred_endpoint"], "backup")
        self.assertIn("已回退到 API 3", format_probe_summary(result))

    def test_raises_listing_every_slot_when_all_unreachable(self):
        self._set_three_slots(None)
        self._stub_probe(unreachable={
            "http://host-one.example/v1",
            "http://host-two.example/v1",
            "http://host-three.example/v1",
        })

        with self.assertRaises(RuntimeError) as ctx:
            probe_openai_configuration()

        for label in ("API 1", "API 2", "API 3"):
            self.assertIn(label, str(ctx.exception))

    def test_format_probe_summary_for_matched_model(self):
        summary = format_probe_summary(
            {
                "base_url": "http://host.example/v1",
                "configured_model": "qwen3527dgx",
                "resolved_model": "qwen3527dgx",
                "available_models": ["qwen3527dgx"],
                "auto_selected": False,
            }
        )

        self.assertIn("probe: endpoint ok", summary)
        self.assertIn("model='qwen3527dgx'", summary)
        self.assertNotIn("auto-selected", summary)

    def test_format_probe_summary_for_auto_selected_model(self):
        summary = format_probe_summary(
            {
                "base_url": "http://host.example/v1",
                "configured_model": "qwen3527dgx",
                "resolved_model": "/models/only-one",
                "available_models": ["/models/only-one"],
                "auto_selected": True,
            }
        )

        self.assertIn("configured='qwen3527dgx'", summary)
        self.assertIn("resolved='/models/only-one'", summary)
        self.assertIn("auto-selected", summary)


class RunPersistenceTests(unittest.TestCase):
    def test_scratchpad_tool_persists_via_run_persistence(self):
        state = AgentState(goal="test")
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence = RunPersistence(tmpdir)
            state.persistence = persistence
            state.meta["_task_desc"] = "test"

            result = tool_scratchpad_set(state=state, content="计划:\n- 第一步")

            self.assertTrue(result.success)
            scratchpad_path = Path(tmpdir) / "scratchpad.md"
            self.assertTrue(scratchpad_path.exists())
            content = scratchpad_path.read_text(encoding="utf-8")
            self.assertIn("任务描述", content)
            self.assertIn("第一步", content)

    def test_finish_failed_writes_failure_status_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence = RunPersistence(tmpdir)
            state = AgentState(goal="failing run")
            state.persistence = persistence
            state.short_term.append({"role": "user", "content": "hello"})
            persistence.start(state)
            persistence.append_short_term(state.short_term[-1])
            persistence.finish(state, outcome="failed", error="boom")

            status = json.loads((Path(tmpdir) / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], "boom")

            issues = json.loads((Path(tmpdir) / "issues.json").read_text(encoding="utf-8"))
            self.assertIn("goal", issues)
            self.assertTrue((Path(tmpdir) / "execution_summary.md").exists())
            self.assertTrue((Path(tmpdir) / "reflection.md").exists())


class SameFailureClassLoopDetectionTests(unittest.TestCase):
    """同类失败循环检测。

    背景：现有检测器按 md5(tool+args+result) 做签名，模型只要把参数改一个字签名
    就变。真实 run 20260809-135124 里 33 次调用的签名**全部唯一**，而其中第
    27/29/31/32 次栽在同一个 SyntaxError 上（中间夹着成功，连续计数也归零），
    检测器全程沉默，agent 就那样一直磨下去。
    """

    @staticmethod
    def _drive(items):
        """回放一串 (tool, success, error)，返回触发同类失败告警的下标。

        每次 args 都不同，确保老的签名检测器不会插手——测的就是新那条路。
        """
        state = AgentState(goal="loop detection")
        fired = []
        for i, (tool, ok, err) in enumerate(items):
            action = Action(
                type=ActionType.TOOL_CALL, thought=f"step{i}", tool=tool, args={"n": i}
            )
            result = ToolResult(success=ok, output="ok" if ok else None, error=err)
            feedback = _build_feedback(action, result, state=state)
            if feedback and "同类失败" in feedback:
                fired.append(i)
        return fired

    def test_failure_class_fingerprint_ignores_volatile_parts(self):
        a = _failure_class('File "/tmp/tmpabc123.py", line 9\nSyntaxError: unterminated string')
        b = _failure_class('File "/tmp/tmpzzz999.py", line 30\nSyntaxError: invalid syntax')
        self.assertEqual(a, b, "同一异常类的不同措辞/路径/行号必须归并为同一指纹")
        self.assertNotEqual(a, _failure_class("KeyError: 'x'"))

    def test_fires_on_oscillating_same_class_failures(self):
        # 失败-成功-失败-成功-失败：连续计数永远够不到阈值，只有滑动窗口能抓
        fired = self._drive([
            ("run_python", False, "SyntaxError: a"),
            ("run_python", True, None),
            ("run_python", False, "SyntaxError: b"),
            ("run_python", True, None),
            ("run_python", False, "SyntaxError: c"),
        ])
        self.assertEqual(fired, [4])

    def test_silent_when_failures_are_different_classes(self):
        # 每次错误类型都不同 = 模型在推进，不该报警
        fired = self._drive([
            ("run_python", False, f"{name}: boom")
            for name in ("IndexError", "TypeError", "KeyError",
                         "ValueError", "AttributeError", "OSError")
        ])
        self.assertEqual(fired, [])

    def test_silent_on_all_success_and_on_sparse_failures(self):
        self.assertEqual(self._drive([("read_file", True, None)] * 12), [])
        # 同类失败但被 7 次成功隔开，落在窗口外
        sparse = ([("run_python", False, "SyntaxError: x")]
                  + [("read_file", True, None)] * 7
                  + [("run_python", False, "SyntaxError: y")])
        self.assertEqual(self._drive(sparse), [])

    def test_tool_name_is_part_of_the_fingerprint(self):
        # 同一类错误但分散在不同工具上，不构成"同一件事反复失败"
        fired = self._drive([
            ("run_python", False, "SyntaxError: a"),
            ("shell", False, "SyntaxError: b"),
            ("edit_file", False, "SyntaxError: c"),
        ])
        self.assertEqual(fired, [])


class AckOnlyToolFeedbackTests(unittest.TestCase):
    """ACK-only 工具必须给回执，以及连续记账要被当成空转拦下。

    背景：_build_feedback 对 scratchpad_* / raw_append 曾返回 None（省上下文）。
    后果是那一轮只追加了 assistant、没有任何 user 消息 —— 部分服务端直接 400；
    补上占位消息后模型又只看到一句"请继续"，不知道自己刚才干了什么，于是一遍遍
    重发同一个 scratchpad_set（每次把内容里的时间戳 +1）。真实 run
    20260809-160233 连续 42 次，而两个既有检测器都抓不住：签名含 args 所以每次
    唯一，同类失败检测又只统计失败——这些调用全部成功。
    """

    @staticmethod
    def _drive(items):
        state = AgentState(goal="ack feedback")
        none_count = 0
        fired = []
        for i, tool in enumerate(items):
            action = Action(
                type=ActionType.TOOL_CALL, thought=f"s{i}", tool=tool,
                # 内容每次都不同，确保按 args 做签名的老检测器不会插手
                args={"content": f"任务: 继续20260809-{210000 + i * 1000}+3+3"},
            )
            feedback = _build_feedback(
                action, ToolResult(success=True, output="已写入"), state=state
            )
            if feedback is None:
                none_count += 1
            if feedback and "记账空转" in feedback:
                fired.append(i)
        return none_count, fired, state

    def test_ack_only_tool_always_returns_feedback(self):
        none_count, _, _ = self._drive(["scratchpad_set"] * 5)
        self.assertEqual(
            none_count, 0,
            "ACK-only 工具返回 None 会让消息列表以 assistant 结尾，并让模型失去反馈",
        )

    def test_consecutive_bookkeeping_trips_the_breaker(self):
        _, fired, state = self._drive(["scratchpad_set"] * 8)
        self.assertEqual(fired[0], 3, "第 4 次连续记账就该告警")
        self.assertTrue(state.meta.get("_loop_advisor_pending"))

    def test_bookkeeping_interleaved_with_real_work_is_fine(self):
        _, fired, state = self._drive(["scratchpad_set", "read_file"] * 5)
        self.assertEqual(fired, [])
        self.assertIsNone(state.meta.get("_loop_advisor_pending"))

    def test_streak_resets_after_real_work(self):
        _, fired, _ = self._drive(
            ["scratchpad_set", "scratchpad_set", "shell",
             "scratchpad_append", "scratchpad_append"]
        )
        self.assertEqual(fired, [], "中间干了实事，连续计数必须清零")


class MacroMemorySectionTests(unittest.TestCase):
    """宏观记忆的写入必须是增量的。

    全量覆盖模式下，模型每次更新都要把整份记忆（实测 21 KB）逐 token 重新解码
    一遍——单次 204 s，占掉整个 run 四成的墙上时间，而真正的改动往往只有几百字。
    章节模式把这笔开销降到 O(一个章节)。
    """

    SAMPLE = (
        "# 宏观工作记忆\n\n"
        "## 联网搜索\n集成 web_search、DDGS。\n\n"
        "## AI for EDA\n旧内容。\n\n"
        "### 子话题\n子内容。\n\n"
        "## 远程运维\nssh 到 xxx。\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "memory_macro.md")
        Path(self.path).write_text(self.SAMPLE, encoding="utf-8")
        self.state = AgentState(goal="g")
        self.state.meta["_concept_path"] = self.path

    def tearDown(self):
        self.tmp.cleanup()

    def _text(self):
        return Path(self.path).read_text(encoding="utf-8")

    def test_section_replace_leaves_other_sections_untouched(self):
        r = tool_save_concept(
            self.state, path=self.path, content="新内容。", section="AI for EDA")

        self.assertTrue(r.success)
        self.assertEqual(r.output["mode"], "section_replaced")
        text = self._text()
        self.assertIn("## 联网搜索\n集成 web_search、DDGS。", text)
        self.assertIn("## AI for EDA\n新内容。", text)
        self.assertIn("## 远程运维\nssh 到 xxx。", text)
        self.assertNotIn("旧内容。", text)

    def test_section_heading_casing_is_preserved(self):
        """section 参数里的大小写差异不得悄悄改写文件里的标题。"""
        tool_save_concept(self.state, path=self.path, content="x", section="ai for eda")

        self.assertIn("## AI for EDA", self._text())
        self.assertNotIn("## ai for eda", self._text())

    def test_content_may_carry_its_own_heading(self):
        tool_save_concept(
            self.state, path=self.path,
            content="## AI for EDA\n带标题的内容。", section="AI for EDA")

        text = self._text()
        self.assertIn("## AI for EDA\n带标题的内容。", text)
        self.assertEqual(text.count("## AI for EDA"), 1)

    def test_unknown_section_is_appended_with_existing_titles_listed(self):
        """章节名写错会静默追加出一个重复章节——回执里必须把现有章节名摆出来。"""
        r = tool_save_concept(
            self.state, path=self.path, content="笔记。", section="AI for EDA（旧）")

        self.assertEqual(r.output["mode"], "section_appended")
        self.assertIn("AI for EDA", r.output["appended_as_new"])
        self.assertTrue(self._text().rstrip().endswith("笔记。"))

    def test_shrinking_a_section_warns_about_lost_content(self):
        """整段替换会连子标题一起换掉；模型以为在补一句，实际删了半个章节。"""
        r = tool_save_concept(self.state, path=self.path, content="x", section="AI for EDA")

        self.assertIn("warning", r.output)
        self.assertNotIn("### 子话题", self._text())

    def test_full_overwrite_still_works_without_section(self):
        r = tool_save_concept(self.state, path=self.path, content="# 全新\n\n## A\n1")

        self.assertEqual(r.output["mode"], "full")
        self.assertEqual(self._text(), "# 全新\n\n## A\n1\n")

    def test_save_concept_syncs_state_and_marks_flags(self):
        tool_save_concept(self.state, path=self.path, content="新。", section="AI for EDA")

        self.assertIn("新。", self.state.meta["concept_memory"])
        self.assertTrue(self.state.meta["_concept_saved"])
        self.assertTrue(self.state.meta["_concept_dirty"])

    def test_edit_file_on_macro_memory_syncs_concept_memory(self):
        """宏观记忆靠 concept_memory 注入 system prompt；只写文件不同步 = 脏上下文。"""
        r = tool_edit_file(
            self.state, path=self.path, old_string="旧内容。", new_string="改过的内容。")

        self.assertTrue(r.success)
        self.assertIn("改过的内容。", self.state.meta["concept_memory"])
        self.assertTrue(self.state.meta["_concept_dirty"])

    def test_write_file_on_macro_memory_syncs_concept_memory(self):
        tool_write_file(self.state, path=self.path, content="# 覆盖\n")

        self.assertEqual(self.state.meta["concept_memory"], "# 覆盖")
        self.assertTrue(self.state.meta["_concept_dirty"])

    def test_edit_file_on_other_files_does_not_touch_concept_flags(self):
        other = str(Path(self.tmp.name) / "other.md")
        Path(other).write_text("hello\n", encoding="utf-8")

        tool_edit_file(self.state, path=other, old_string="hello", new_string="bye")

        self.assertNotIn("_concept_dirty", self.state.meta)
        self.assertNotIn("concept_memory", self.state.meta)


if __name__ == "__main__":
    unittest.main()
