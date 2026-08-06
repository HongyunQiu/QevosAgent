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
    _parse_acceptance_evidence,
)
from agent.core.types_def import Action, ActionType, AgentState, ToolResult, ToolSpec
from agent.runtime.persistence import RunPersistence
from agent.tools.standard import (
    get_standard_tools,
    tool_promote_tool_candidate,
    tool_repair_tool_candidate,
    tool_scratchpad_set,
    tool_validate_tool_recipe,
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

    def _run_n(self, thought, n):
        state = AgentState(goal="loop test")
        feedbacks = [
            _build_feedback(self._action(thought), self._failure(), state)
            for _ in range(n)
        ]
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
        state, feedbacks = self._run_n("等待下载完成，稍后重试", 5)
        self.assertIsNone(state.meta.get("_loop_advisor_pending"))
        self.assertTrue(any("轮询提示" in f for f in feedbacks))

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


if __name__ == "__main__":
    unittest.main()
