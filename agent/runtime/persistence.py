"""
运行期持久化。

目标：
- 运行中持续把原始事实写入磁盘，减少异常退出时的信息丢失
- 运行结束后基于当前状态生成复盘文件
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class RunPersistence:
    def __init__(self, run_dir: str | os.PathLike[str]):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = _utc_now()

        self.short_term_path = self.run_dir / "short_term.jsonl"
        self.meta_path = self.run_dir / "meta.json"
        self.status_path = self.run_dir / "status.json"
        self.scratchpad_path = self.run_dir / "scratchpad.md"
        self.final_answer_path = self.run_dir / "final_answer.md"
        self.execution_summary_path = self.run_dir / "execution_summary.md"
        self.issues_path = self.run_dir / "issues.json"
        self.reflection_path = self.run_dir / "reflection.md"

    def _status_payload(
        self,
        state=None,
        status: str = "running",
        error: Optional[str] = None,
    ) -> dict:
        goal = ""
        iteration = 0
        if state is not None:
            goal = getattr(state, "goal", "") or ""
            iteration = int(getattr(state, "iteration", 0) or 0)

        return {
            "status": status,
            "run_id": self.run_dir.name,
            "goal": goal,
            "started_at": self.started_at,
            "updated_at": _utc_now(),
            "iteration": iteration,
            "final_answer_written": self.final_answer_path.exists(),
            "error": error,
        }

    def _collect_diagnostics(self, state) -> dict:
        short_term = list(getattr(state, "short_term", []) or [])
        long_term = list(getattr(state, "long_term", []) or [])
        meta = dict(getattr(state, "meta", {}) or {})

        used_tools: list[str] = []
        failures: list[str] = []
        issues: list[dict] = []
        json_parse_errors = 0
        self_heal_notes: list[str] = []

        for idx, message in enumerate(short_term):
            content = message.get("content", "")
            if not isinstance(content, str):
                continue

            if '"tool"' in content:
                import re

                match = re.search(r'"tool"\s*:\s*"([^"]+)"', content)
                if match:
                    used_tools.append(match.group(1))

            if "执行失败" in content or "[TOOL ERROR]" in content:
                failures.append(content[:800])
                issues.append(
                    {
                        "kind": "tool_failure",
                        "short_term_index": idx,
                        "snippet": content[:2000],
                    }
                )

            if "JSON 解析失败" in content:
                json_parse_errors += 1
                issues.append(
                    {
                        "kind": "json_parse_error",
                        "short_term_index": idx,
                        "snippet": content[:2000],
                    }
                )

        for item in long_term:
            if isinstance(item, str) and ("[自我修复]" in item or "[RUN_OK]" in item):
                self_heal_notes.append(item)

        used_tools = list(dict.fromkeys(used_tools))
        return {
            "used_tools": used_tools,
            "failures": failures,
            "issues": issues,
            "json_parse_errors": json_parse_errors,
            "self_heal_notes": self_heal_notes,
            "timeout": bool(meta.get("timeout")),
            "prompt_est": meta.get("prompt_tokens_est"),
            "context_window": meta.get("context_window"),
        }

    def start(self, state) -> None:
        if state is not None:
            self.checkpoint(state, status="running")
            scratchpad = getattr(state, "meta", {}).get("scratchpad", "")
            if isinstance(scratchpad, str) and scratchpad:
                self.save_scratchpad(scratchpad)
        else:
            _write_json_atomic(self.status_path, self._status_payload(status="running"))

    def append_short_term(self, record: dict) -> None:
        self.short_term_path.parent.mkdir(parents=True, exist_ok=True)
        with self.short_term_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_scratchpad(self, text: str) -> None:
        _write_text_atomic(self.scratchpad_path, text or "")

    def checkpoint(self, state, status: str = "running", error: Optional[str] = None) -> None:
        if state is not None:
            meta = dict(getattr(state, "meta", {}) or {})
            meta["_persistence"] = {
                "updated_at": _utc_now(),
                "iteration": int(getattr(state, "iteration", 0) or 0),
            }
            _write_json_atomic(self.meta_path, meta)
        _write_json_atomic(self.status_path, self._status_payload(state=state, status=status, error=error))

    def save_final_answer(self, text: str) -> None:
        _write_text_atomic(self.final_answer_path, text or "")

    def _write_execution_summary(self, state, outcome: str, diagnostics: dict, error: Optional[str]) -> None:
        final_answer = ""
        goal = ""
        if state is not None:
            final_answer = (getattr(state, "meta", {}) or {}).get("final_answer") or ""
            goal = getattr(state, "goal", "") or ""

        lines = [
            "# Execution Summary",
            "",
            f"## Outcome",
            f"- status: {outcome}",
            f"- error: {error or '(none)'}",
            "",
            "## Goal",
            goal or "(unknown)",
            "",
            "## Final Answer",
            final_answer or "(no final_answer)",
            "",
            "## Run Artifacts",
            "- short_term.jsonl",
            "- meta.json",
            "- status.json",
            "- scratchpad.md",
            "- final_answer.md",
            "- execution_summary.md",
            "- issues.json",
            "- reflection.md",
            "",
            "## Tools Used",
        ]
        if diagnostics["used_tools"]:
            lines.extend(f"- {name}" for name in diagnostics["used_tools"])
        else:
            lines.append("- (none inferred)")

        lines.extend(
            [
                "",
                "## Issues Observed",
                f"- JSON parse errors: {diagnostics['json_parse_errors']}",
                f"- Timeout hit: {diagnostics['timeout']}",
            ]
        )
        if diagnostics["failures"]:
            lines.extend(["", "### Failure Snippets"])
            for idx, snippet in enumerate(diagnostics["failures"][:10], 1):
                lines.append(f"{idx}. {snippet.replace('```', '')}")

        lines.extend(["", "## Self-Healing Notes"])
        if diagnostics["self_heal_notes"]:
            lines.extend(f"- {note}" for note in diagnostics["self_heal_notes"][-20:])
        else:
            lines.append("- (none)")

        _write_text_atomic(self.execution_summary_path, "\n".join(lines).strip() + "\n")

    def _write_issues(self, state, diagnostics: dict, error: Optional[str]) -> None:
        goal = getattr(state, "goal", "") if state is not None else ""
        payload = {
            "goal": goal,
            "timeout": diagnostics["timeout"],
            "json_parse_errors": diagnostics["json_parse_errors"],
            "used_tools": diagnostics["used_tools"],
            "issues": list(diagnostics["issues"]),
        }
        if error:
            payload["issues"].append({"kind": "run_failure", "message": error})
        _write_json_atomic(self.issues_path, payload)

    def _write_reflection(self, diagnostics: dict, error: Optional[str]) -> None:
        lines = [
            "# Reflection",
            "",
            "## 实际执行链路（概览）",
        ]
        if diagnostics["used_tools"]:
            lines.extend(f"- {name}" for name in diagnostics["used_tools"])
        else:
            lines.append("- (unknown)")

        lines.extend(
            [
                "",
                "## 发生的问题/异常",
                f"- JSON 解析失败次数：{diagnostics['json_parse_errors']}",
                f"- Timeout: {diagnostics['timeout']}",
            ]
        )
        if error:
            lines.append(f"- 运行异常：{error}")
        if diagnostics["failures"]:
            lines.append("- 观察到工具执行失败片段（见 issues.json）")

        lines.extend(
            [
                "",
                "## 下次行动清单",
                "- 原始 short_term 继续保持逐条追加写，避免任务中途退出时丢失关键轨迹。",
                "- 对外展示类文件依赖事实层文件生成，不再把它们当成唯一信息源。",
            ]
        )
        _write_text_atomic(self.reflection_path, "\n".join(lines).strip() + "\n")

    def finish(self, state, outcome: str, error: Optional[str] = None) -> None:
        status = outcome if outcome in {"running", "paused", "done", "failed"} else "failed"
        final_answer = ""
        if state is not None:
            final_answer = (getattr(state, "meta", {}) or {}).get("final_answer") or ""
        if final_answer:
            self.save_final_answer(final_answer)

        diagnostics = self._collect_diagnostics(state) if state is not None else {
            "used_tools": [],
            "failures": [],
            "issues": [],
            "json_parse_errors": 0,
            "self_heal_notes": [],
            "timeout": False,
            "prompt_est": None,
            "context_window": None,
        }

        self.checkpoint(state, status=status, error=error)
        self._write_execution_summary(state, status, diagnostics, error)
        self._write_issues(state, diagnostics, error)
        self._write_reflection(diagnostics, error)
