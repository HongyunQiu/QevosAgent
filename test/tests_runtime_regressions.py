#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.core.llm import LLMBackend
from agent.core.loop import run
from agent.core.executor import execute
from agent.core.loop import _extract_claimed_artifact_paths, _parse_acceptance_evidence
from agent.core.types_def import Action, ActionType, AgentState, ToolResult, ToolSpec
from agent.runtime.persistence import RunPersistence
from agent.tools.standard import (
    get_standard_tools,
    tool_load_snapshot_meta,
    tool_promote_tool_candidate,
    tool_repair_tool_candidate,
    tool_scratchpad_set,
    tool_save_snapshot_meta,
    tool_validate_tool_recipe,
)
from run_goal import ensure_env_defaults, format_probe_summary, probe_openai_configuration


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

    def test_probe_openai_configuration_wraps_connection_errors(self):
        keys = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
        old = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["OPENAI_BASE_URL"] = "http://bad-host.example/v1"
            os.environ["OPENAI_API_KEY"] = "local"
            os.environ["OPENAI_MODEL"] = "qwen3527dgx"

            def boom():
                raise RuntimeError("Connection error")

            with self.assertRaisesRegex(RuntimeError, "LLM 服务探测失败"):
                probe_openai_configuration(list_models=boom)
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


class _SequenceLLM(LLMBackend):
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, messages: list[dict], system: str) -> str:
        if not self._responses:
            raise AssertionError("No more fake LLM responses available")
        return self._responses.pop(0)


class RunPersistenceTests(unittest.TestCase):
    def test_run_streams_short_term_and_final_answer_during_execution(self):
        keys = ("RUN_DIR", "USER_GOAL")
        old = {k: os.environ.get(k) for k in keys}
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["RUN_DIR"] = tmpdir
                os.environ["USER_GOAL"] = "测试持久化"

                llm = _SequenceLLM(
                    [
                        json.dumps(
                            {
                                "thought": "先补验收块",
                                "action": "tool_call",
                                "tool": "scratchpad_append",
                                "args": {
                                    "content": (
                                        "ACCEPTANCE:\n"
                                        "- criteria: 返回最终答案\n"
                                        "- evidence_type: none\n"
                                        "- verdict: PASS"
                                    )
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "thought": "完成任务",
                                "action": "done",
                                "final_answer": "持久化完成",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )

                state = run(
                    goal="测试持久化",
                    llm=llm,
                    tools=get_standard_tools(),
                    max_iterations=4,
                )
                state.persistence.finish(state, outcome="done")

                short_term_path = Path(tmpdir) / "short_term.jsonl"
                self.assertTrue(short_term_path.exists())
                short_term_lines = short_term_path.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(len(short_term_lines), 4)
                self.assertIn("请完成以下目标", short_term_lines[0])

                final_answer_path = Path(tmpdir) / "final_answer.md"
                self.assertTrue(final_answer_path.exists())
                self.assertEqual(final_answer_path.read_text(encoding="utf-8"), "持久化完成")

                status = json.loads((Path(tmpdir) / "status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["status"], "done")
                self.assertTrue(status["final_answer_written"])
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
