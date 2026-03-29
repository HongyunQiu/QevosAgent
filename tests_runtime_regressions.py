#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.core.executor import execute
from agent.core.loop import _extract_claimed_artifact_paths, _parse_acceptance_evidence
from agent.core.types import Action, ActionType, AgentState, ToolResult, ToolSpec
from agent.tools.standard import (
    tool_load_snapshot_meta,
    tool_promote_tool_candidate,
    tool_repair_tool_candidate,
    tool_save_snapshot_meta,
    tool_validate_tool_recipe,
)
from run_goal import ensure_env_defaults


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


class SnapshotValidationTests(unittest.TestCase):
    def test_load_snapshot_meta_skips_invalid_evolved_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "long_term": ["memory"],
                        "evolved_tools": {
                            "good_tool": {
                                "name": "good_tool",
                                "description": "works",
                                "args_schema": {"value": "payload"},
                                "python_code": (
                                    "def run(state, value):\n"
                                    "    return ToolResult(success=True, output=value)\n"
                                ),
                            },
                            "bad_tool": {
                                "name": "bad_tool",
                                "description": "broken",
                                "args_schema": {"url": "target"},
                                "python_code": (
                                    "def run(state, url):\n"
                                    "    return ToolResult(output=url, error=None)\n"
                                ),
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            state = AgentState(goal="test")
            result = tool_load_snapshot_meta(state=state, path=str(snapshot_path))

            self.assertTrue(result.success)
            self.assertIn("good_tool", state.tools)
            self.assertNotIn("bad_tool", state.tools)
            self.assertIn("invalid_evolved_tools", state.meta)
            self.assertIn("bad_tool", state.meta["invalid_evolved_tools"])
            self.assertEqual(result.output["restored"], 1)
            self.assertEqual(result.output["invalid"], 1)

    def test_candidate_repairs_restore_as_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "snapshot.json"
            state = AgentState(goal="test")
            state.meta["evolved_tools"] = {
                "formal_tool": {
                    "name": "formal_tool",
                    "description": "formal",
                    "args_schema": {"value": "payload"},
                    "python_code": (
                        "def run(state, value):\n"
                        "    return ToolResult(success=True, output=value)\n"
                    ),
                }
            }
            state.meta["tool_repair_candidates"] = {
                "formal_tool": {
                    "name": "formal_tool",
                    "description": "candidate",
                    "args_schema": {"value": "payload"},
                    "python_code": (
                        "def run(state, value):\n"
                        "    return ToolResult(success=True, output='candidate:' + value)\n"
                    ),
                    "validation": {"ok": True, "errors": []},
                }
            }

            save_result = tool_save_snapshot_meta(state=state, path=str(snapshot_path))
            self.assertTrue(save_result.success)

            new_state = AgentState(goal="restore")
            load_result = tool_load_snapshot_meta(state=new_state, path=str(snapshot_path))

            self.assertTrue(load_result.success)
            self.assertIn("formal_tool", new_state.tools)
            self.assertIn("tool_repair_candidates", new_state.meta)
            self.assertIn("formal_tool", new_state.meta["tool_repair_candidates"])


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


class EnvDefaultTests(unittest.TestCase):
    def test_ensure_env_defaults_enables_memory_persistence_flags(self):
        keys = (
            "AUTO_SAVE_SNAPSHOT_ON_EXIT",
            "AUTO_REMEMBER_ON_DONE",
            "OPENAI_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_PROFILE_OSS120B_BASE_URL",
        )
        old = {k: os.environ.get(k) for k in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)

            os.environ["OPENAI_PROFILE"] = "oss120b"
            os.environ["OPENAI_PROFILE_OSS120B_BASE_URL"] = "http://env-oss120b.example/v1"
            ensure_env_defaults()

            self.assertEqual(os.environ["AUTO_SAVE_SNAPSHOT_ON_EXIT"], "1")
            self.assertEqual(os.environ["AUTO_REMEMBER_ON_DONE"], "1")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ensure_env_defaults_uses_profile_specific_base_url_env(self):
        keys = (
            "OPENAI_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_PROFILE_OSS120B_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        )
        old = {k: os.environ.get(k) for k in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)

            os.environ["OPENAI_PROFILE"] = "oss120b"
            os.environ["OPENAI_PROFILE_OSS120B_BASE_URL"] = "http://env-oss120b.example/v1"

            ensure_env_defaults()

            self.assertEqual(os.environ["OPENAI_BASE_URL"], "http://env-oss120b.example/v1")
            self.assertEqual(os.environ["OPENAI_MODEL"], "openai/gpt-oss-120b")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ensure_env_defaults_requires_profile_base_url_when_missing(self):
        keys = (
            "OPENAI_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_PROFILE_QWEN3527DGX_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        )
        old = {k: os.environ.get(k) for k in keys}
        old_cwd = os.getcwd()
        try:
            for key in keys:
                os.environ.pop(key, None)

            with tempfile.TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                os.environ["OPENAI_PROFILE"] = "qwen3527dgx"

                with self.assertRaises(ValueError):
                    ensure_env_defaults()
        finally:
            os.chdir(old_cwd)
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ensure_env_defaults_auto_loads_dotenv_file(self):
        keys = (
            "OPENAI_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_PROFILE_OSS120B_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        )
        old = {k: os.environ.get(k) for k in keys}
        old_cwd = os.getcwd()
        try:
            for key in keys:
                os.environ.pop(key, None)

            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / ".env"
                env_path.write_text(
                    "OPENAI_PROFILE=oss120b\n"
                    "OPENAI_PROFILE_OSS120B_BASE_URL=http://from-dotenv.example/v1\n"
                    "OPENAI_API_KEY=dotenv-key\n",
                    encoding="utf-8",
                )
                os.chdir(tmpdir)

                ensure_env_defaults()

                self.assertEqual(os.environ["OPENAI_BASE_URL"], "http://from-dotenv.example/v1")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "dotenv-key")
                self.assertEqual(os.environ["OPENAI_MODEL"], "openai/gpt-oss-120b")
        finally:
            os.chdir(old_cwd)
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_ensure_env_defaults_does_not_override_existing_env_with_dotenv(self):
        keys = (
            "OPENAI_PROFILE",
            "OPENAI_BASE_URL",
            "OPENAI_PROFILE_OSS120B_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
        )
        old = {k: os.environ.get(k) for k in keys}
        old_cwd = os.getcwd()
        try:
            for key in keys:
                os.environ.pop(key, None)

            os.environ["OPENAI_PROFILE"] = "oss120b"
            os.environ["OPENAI_PROFILE_OSS120B_BASE_URL"] = "http://from-env.example/v1"
            os.environ["OPENAI_API_KEY"] = "preexisting-key"

            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / ".env"
                env_path.write_text(
                    "OPENAI_PROFILE=oss120b\n"
                    "OPENAI_PROFILE_OSS120B_BASE_URL=http://from-dotenv.example/v1\n"
                    "OPENAI_API_KEY=dotenv-key\n",
                    encoding="utf-8",
                )
                os.chdir(tmpdir)

                ensure_env_defaults()

                self.assertEqual(os.environ["OPENAI_BASE_URL"], "http://from-env.example/v1")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "preexisting-key")
        finally:
            os.chdir(old_cwd)
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
