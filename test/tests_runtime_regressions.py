#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.core.executor import execute
from agent.core.loop import _extract_claimed_artifact_paths, _parse_acceptance_evidence
from agent.core.types_def import Action, ActionType, AgentState, ToolResult, ToolSpec
from agent.runtime.persistence import RunPersistence
from agent.tools.standard import (
    get_standard_tools,
    tool_promote_tool_candidate,
    tool_repair_tool_candidate,
    tool_scratchpad_set,
    tool_validate_tool_recipe,
)
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
