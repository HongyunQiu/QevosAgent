# Layered Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `simpleAgent` 实现分层记忆基础链路：任务结束时写入 `memory/task_memory.jsonl`，按条件重写 `memory/memory.md`，并在预热阶段加载 `memory.md` 与少量相关任务记忆。

**Architecture:** 新增独立的 `memory_engine` 模块作为唯一记忆编排入口，负责任务记忆写入、主记忆重写与启动记忆加载。保留现有 `MemoryManager` 对 `memory.md` 和 `long_term/` 的基础读写能力，但不再让其他模块直接拼接主记忆内容；启动链路通过 `memory_engine` 返回“总体记忆 + 相关任务记忆”两层结果。

**Tech Stack:** Python 3、`unittest`、JSONL、Markdown、现有 `run_goal.py` / `agent.core.router` / `agent.tools.standard` 启动流程

---

## File Structure

### New Files

- `agent/runtime/memory_engine.py`
  - 记忆系统唯一编排入口
  - 负责生成/覆盖单条任务记忆
  - 负责决定是否重写 `memory.md`
  - 负责启动阶段加载总体记忆和相关任务记忆
- `tests/test_memory_engine.py`
  - `memory_engine` 的单元测试
  - 覆盖 JSONL upsert、最小记录降级、主记忆重写触发、启动检索

### Modified Files

- `agent/core/memory.py`
  - 保留 `long_term` 和 `memory.md` 基础能力
  - 增补 `task_memory.jsonl` 的基础读取/写入帮助函数，或提供给 `memory_engine` 复用的极小辅助函数
- `run_goal.py`
  - 在任务结束路径接入 `record_task_memory(...)`
  - 在成功/失败两种正常收尾路径都尝试生成任务记忆
  - 在合适位置触发 `maybe_refresh_main_memory(...)`
- `agent/core/router.py`
  - 启动预热时改为读取 `memory.md` + 少量相关 `task_memory.jsonl`
  - 扩展 `memory_context`，让 `route_task` 看见双层记忆
- `agent/tools/standard.py`
  - `tool_recall_memory` 在启动阶段输出总体记忆和相关任务记忆
  - 如有必要，补充一个显式主记忆重写工具或保留内部调用入口
- `tests_runtime_regressions.py`
  - 新增启动阶段双层记忆加载的回归测试
- `doc/memory-layered-design.md`
  - 如实现时发现约束需微调，回写小幅勘误

## Task 1: Build Task Memory Primitives

**Files:**
- Create: `agent/runtime/memory_engine.py`
- Modify: `agent/core/memory.py`
- Test: `tests/test_memory_engine.py`

- [ ] **Step 1: Write the failing JSONL upsert tests**

```python
import json
import tempfile
import unittest
from pathlib import Path

from agent.runtime.memory_engine import upsert_task_memory_record, load_task_memory_records


class TaskMemoryStoreTests(unittest.TestCase):
    def test_upsert_task_memory_record_appends_first_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            upsert_task_memory_record(
                memory_dir=memory_dir,
                record={
                    "run_id": "20260407-233451",
                    "task": "搜索 Qwen3.5 27B 微调方法",
                    "summary": "整理了主流微调框架。",
                    "keywords": ["qwen3.5-27b", "finetune", "llama-factory"],
                },
            )

            records = load_task_memory_records(memory_dir)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["run_id"], "20260407-233451")

    def test_upsert_task_memory_record_overwrites_same_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            upsert_task_memory_record(
                memory_dir=memory_dir,
                record={
                    "run_id": "20260407-233451",
                    "task": "旧任务",
                    "summary": "旧摘要",
                    "keywords": ["old"],
                },
            )
            upsert_task_memory_record(
                memory_dir=memory_dir,
                record={
                    "run_id": "20260407-233451",
                    "task": "新任务",
                    "summary": "新摘要",
                    "keywords": ["new"],
                },
            )

            records = load_task_memory_records(memory_dir)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["task"], "新任务")
            self.assertEqual(records[0]["keywords"], ["new"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_memory_engine.TaskMemoryStoreTests -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent.runtime.memory_engine'`

- [ ] **Step 3: Write minimal JSONL store implementation**

```python
# agent/runtime/memory_engine.py
from __future__ import annotations

import json
from pathlib import Path


TASK_MEMORY_FILE = "task_memory.jsonl"


def _task_memory_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / TASK_MEMORY_FILE


def load_task_memory_records(memory_dir: Path) -> list[dict]:
    path = _task_memory_path(Path(memory_dir))
    if not path.exists():
        return []
    records: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        records.append(json.loads(raw))
    return records


def upsert_task_memory_record(memory_dir: Path, record: dict) -> None:
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    records = load_task_memory_records(memory_dir)
    run_id = str(record["run_id"]).strip()
    records = [item for item in records if str(item.get("run_id", "")).strip() != run_id]
    records.append(record)
    path = _task_memory_path(memory_dir)
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n"
    path.write_text(payload, encoding="utf-8")
```

- [ ] **Step 4: Add record normalization tests and implementation**

```python
def test_normalize_task_memory_record_enforces_minimal_shape(self) -> None:
    record = normalize_task_memory_record(
        run_id="20260407-174755",
        task="编写并仿真串口控制器 Verilog 代码",
        summary="围绕串口控制器实现与仿真进行了多轮尝试，留下了可继续排查的记录。",
        keywords=["uart", "verilog", "simulation"],
    )

    self.assertEqual(
        sorted(record.keys()),
        ["keywords", "run_id", "summary", "task"],
    )
    self.assertEqual(record["keywords"], ["uart", "verilog", "simulation"])
```

```python
def normalize_task_memory_record(run_id: str, task: str, summary: str, keywords: list[str]) -> dict:
    normalized_keywords: list[str] = []
    for item in keywords:
        text = str(item).strip()
        if text and text not in normalized_keywords:
            normalized_keywords.append(text)
    return {
        "run_id": str(run_id).strip(),
        "task": " ".join(str(task).split()).strip(),
        "summary": " ".join(str(summary).split()).strip(),
        "keywords": normalized_keywords[:6],
    }
```

- [ ] **Step 5: Run tests to verify Task 1 passes**

Run: `python -m unittest tests.test_memory_engine.TaskMemoryStoreTests -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/runtime/memory_engine.py tests/test_memory_engine.py agent/core/memory.py
git commit -m "feat: add task memory jsonl primitives"
```

## Task 2: Generate Task Memory On Every Finished Run

**Files:**
- Modify: `agent/runtime/memory_engine.py`
- Modify: `run_goal.py`
- Test: `tests/test_memory_engine.py`
- Test: `tests_runtime_regressions.py`

- [ ] **Step 1: Write the failing task-memory generation tests**

```python
from pathlib import Path
import tempfile
import unittest

from agent.runtime.memory_engine import build_task_memory_record_from_run


class TaskMemoryBuildTests(unittest.TestCase):
    def test_build_task_memory_record_prefers_execution_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "20260407-233451"
            run_dir.mkdir(parents=True)
            (run_dir / "execution_summary.md").write_text(
                "# Execution Summary\n\n## Goal\n搜索 Qwen3.5 微调方法\n\n## Final Answer\n整理了主流微调框架。\n",
                encoding="utf-8",
            )

            record = build_task_memory_record_from_run(run_dir)

            self.assertEqual(record["run_id"], "20260407-233451")
            self.assertIn("Qwen3.5", record["task"])
            self.assertIn("微调框架", record["summary"])
            self.assertGreaterEqual(len(record["keywords"]), 1)

    def test_build_task_memory_record_falls_back_to_minimal_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "20260407-174755"
            run_dir.mkdir(parents=True)

            record = build_task_memory_record_from_run(
                run_dir,
                fallback_goal="编写并仿真串口控制器 Verilog 代码",
            )

            self.assertEqual(record["run_id"], "20260407-174755")
            self.assertIn("串口控制器", record["task"])
            self.assertTrue(record["summary"])
            self.assertTrue(record["keywords"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_memory_engine.TaskMemoryBuildTests -v`

Expected: FAIL with `ImportError` or `AttributeError` for `build_task_memory_record_from_run`

- [ ] **Step 3: Implement run-artifact-driven record builder**

```python
def build_task_memory_record_from_run(run_dir: Path, fallback_goal: str = "") -> dict:
    run_dir = Path(run_dir)
    run_id = run_dir.name
    summary_path = run_dir / "execution_summary.md"
    final_answer_path = run_dir / "final_answer.md"
    reflection_path = run_dir / "reflection.md"

    task = ""
    summary = ""
    keywords: list[str] = []

    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8")
        task = _extract_goal_line(text) or fallback_goal
        summary = _extract_final_answer_summary(text)
        keywords = _extract_keywords(task + "\n" + summary)

    if not summary and final_answer_path.exists():
        final_text = final_answer_path.read_text(encoding="utf-8")
        summary = _compress_to_single_paragraph(final_text)
        keywords = _extract_keywords((task or fallback_goal) + "\n" + summary)

    if not summary and reflection_path.exists():
        summary = _compress_to_single_paragraph(reflection_path.read_text(encoding="utf-8"))

    if not task:
        task = _compress_goal(fallback_goal or f"运行 {run_id} 的任务")
    if not summary:
        summary = "本次围绕该任务进行了执行和记录，留下了可继续复用的运行摘要。"
    if not keywords:
        keywords = _extract_keywords(task + "\n" + summary)[:3] or ["task", run_id]

    return normalize_task_memory_record(run_id=run_id, task=task, summary=summary, keywords=keywords)
```

- [ ] **Step 4: Add run-goal hook tests and implementation**

```python
class RunGoalMemoryHookTests(unittest.TestCase):
    def test_try_record_task_memory_does_not_raise_on_failure(self) -> None:
        try:
            _try_record_task_memory(
                run_dir=Path("runs/20260407-174755"),
                memory_dir=Path("memory"),
                fallback_goal="编写并仿真串口控制器 Verilog 代码",
            )
        except Exception as exc:
            self.fail(f"_try_record_task_memory should swallow memory write failures, got: {exc}")
```

```python
# agent/runtime/memory_engine.py
def record_task_memory(run_dir: Path, memory_dir: Path, fallback_goal: str = "") -> dict:
    record = build_task_memory_record_from_run(run_dir=run_dir, fallback_goal=fallback_goal)
    upsert_task_memory_record(memory_dir=memory_dir, record=record)
    return record
```

```python
# run_goal.py
def _try_record_task_memory(run_dir: Path, memory_dir: Path, fallback_goal: str) -> None:
    try:
        from agent.runtime.memory_engine import record_task_memory

        record_task_memory(run_dir=run_dir, memory_dir=memory_dir, fallback_goal=fallback_goal)
    except Exception as exc:
        print(f"[run_goal] task memory skipped: {exc}")
```

- [ ] **Step 5: Call the hook in both successful and failed run-end paths**

```python
# run_goal.py
memory_dir = Path(os.environ.get("MEMORY_DIR") or "./memory")
try:
    state = agent.run(full_goal)
finally:
    _try_record_task_memory(
        run_dir=run_dir,
        memory_dir=memory_dir,
        fallback_goal=goal,
    )
```

- [ ] **Step 6: Run tests to verify Task 2 passes**

Run: `python -m unittest tests.test_memory_engine.TaskMemoryBuildTests tests_runtime_regressions.RunGoalMemoryHookTests -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add agent/runtime/memory_engine.py run_goal.py tests/test_memory_engine.py tests_runtime_regressions.py
git commit -m "feat: record task memory on run completion"
```

## Task 3: Refresh `memory.md` From Task Memory Records

**Files:**
- Modify: `agent/runtime/memory_engine.py`
- Test: `tests/test_memory_engine.py`

- [ ] **Step 1: Write the failing main-memory refresh tests**

```python
class MainMemoryRefreshTests(unittest.TestCase):
    def test_should_refresh_main_memory_when_main_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            self.assertTrue(should_refresh_main_memory(memory_dir, min_new_records=5))

    def test_should_refresh_main_memory_when_enough_new_records_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            for idx in range(5):
                upsert_task_memory_record(
                    memory_dir=memory_dir,
                    record={
                        "run_id": f"20260407-00000{idx}",
                        "task": f"任务 {idx}",
                        "summary": f"摘要 {idx}",
                        "keywords": [f"k{idx}"],
                    },
                )

            self.assertTrue(should_refresh_main_memory(memory_dir, min_new_records=5))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_memory_engine.MainMemoryRefreshTests -v`

Expected: FAIL with `NameError` or missing function assertions

- [ ] **Step 3: Implement refresh trigger and markdown synthesis**

```python
def should_refresh_main_memory(memory_dir: Path, min_new_records: int = 5) -> bool:
    memory_dir = Path(memory_dir)
    main_path = memory_dir / "memory.md"
    records = load_task_memory_records(memory_dir)
    if not main_path.exists():
        return bool(records)
    if len(records) >= min_new_records and main_path.read_text(encoding="utf-8").strip() == "":
        return True
    return len(records) >= min_new_records


def synthesize_main_memory(records: list[dict]) -> str:
    topics = _group_records_by_keywords(records)
    lines = ["# Agent 主记忆", ""]
    for paragraph in _render_topic_paragraphs(topics):
        if paragraph.strip():
            lines.append(paragraph.strip())
            lines.append("")
    return "\n".join(lines).strip() + "\n"
```

- [ ] **Step 4: Add guarded refresh entrypoint**

```python
def maybe_refresh_main_memory(memory_dir: Path, force: bool = False, min_new_records: int = 5) -> bool:
    memory_dir = Path(memory_dir)
    if not force and not should_refresh_main_memory(memory_dir, min_new_records=min_new_records):
        return False
    records = load_task_memory_records(memory_dir)
    content = synthesize_main_memory(records)
    (memory_dir / "memory.md").write_text(content, encoding="utf-8")
    return True
```

- [ ] **Step 5: Run tests to verify Task 3 passes**

Run: `python -m unittest tests.test_memory_engine.MainMemoryRefreshTests -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add agent/runtime/memory_engine.py tests/test_memory_engine.py
git commit -m "feat: synthesize main memory from task memory"
```

## Task 4: Load Layered Memory During Startup

**Files:**
- Modify: `agent/runtime/memory_engine.py`
- Modify: `agent/core/router.py`
- Modify: `agent/tools/standard.py`
- Test: `tests/test_memory_engine.py`
- Test: `tests_runtime_regressions.py`

- [ ] **Step 1: Write the failing startup-load tests**

```python
class StartupMemoryLoadTests(unittest.TestCase):
    def test_load_startup_memory_returns_main_memory_and_related_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp)
            (memory_dir / "memory.md").write_text("# Agent 主记忆\n\n最近主要在做 FPGA 和搜索工作。\n", encoding="utf-8")
            upsert_task_memory_record(
                memory_dir=memory_dir,
                record={
                    "run_id": "20260407-173737",
                    "task": "编写滑动平均 Verilog 模块",
                    "summary": "完成了滑动平均模块和 testbench 编写，并通过仿真验证。",
                    "keywords": ["verilog", "sliding-average", "simulation"],
                },
            )

            payload = load_startup_memory(memory_dir=memory_dir, goal="调试滑动平均 Verilog 仿真", top_k=3)

            self.assertIn("FPGA", payload["main_memory_summary"])
            self.assertEqual(len(payload["task_memory_hits"]), 1)
            self.assertEqual(payload["task_memory_hits"][0]["run_id"], "20260407-173737")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_memory_engine.StartupMemoryLoadTests -v`

Expected: FAIL with missing `load_startup_memory`

- [ ] **Step 3: Implement lightweight startup retrieval**

```python
def load_startup_memory(memory_dir: Path, goal: str, top_k: int = 5) -> dict:
    memory_dir = Path(memory_dir)
    main_memory_summary = ""
    main_path = memory_dir / "memory.md"
    if main_path.exists():
        main_memory_summary = main_path.read_text(encoding="utf-8").strip()

    records = load_task_memory_records(memory_dir)
    scored: list[tuple[int, dict]] = []
    goal_terms = set(_extract_keywords(goal))
    for record in records:
        haystack_terms = set(record.get("keywords", [])) | set(_extract_keywords(record.get("task", ""))) | set(_extract_keywords(record.get("summary", "")))
        score = len(goal_terms & haystack_terms)
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits = [item[1] for item in scored[:top_k]]
    return {
        "main_memory_summary": main_memory_summary,
        "task_memory_hits": hits,
    }
```

- [ ] **Step 4: Wire layered memory into router and startup tools**

```python
# agent/core/router.py
from agent.runtime.memory_engine import load_startup_memory


def _load_memory_context(self, goal: str, memory_dir: Path) -> dict[str, object]:
    payload = load_startup_memory(memory_dir=memory_dir, goal=goal, top_k=5)
    return {
        "main_memory_summary": payload.get("main_memory_summary", ""),
        "task_memory_hits": payload.get("task_memory_hits", []),
        "long_term_hits": [],
    }
```

```python
# agent/tools/standard.py
def tool_recall_memory(state: AgentState, query: str = "", top_k: int = 5) -> ToolResult:
    from agent.runtime.memory_engine import load_startup_memory
    memory_dir = _get_memory_dir(state)
    goal = query.strip() or str(state.goal)
    payload = load_startup_memory(memory_dir=Path(memory_dir), goal=goal, top_k=int(top_k))
    return ToolResult(success=True, output=payload)
```

- [ ] **Step 5: Add startup regression tests**

```python
class LayeredStartupMemoryTests(unittest.TestCase):
    def test_route_task_exposes_task_memory_hits_in_memory_context(self):
        from agent.core.router import TaskRouter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skill"
            memory_dir = root / "memory"
            skill_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (skill_dir / "general.md").write_text(
                "# 通用任务\n\n## ROUTING_META\n\n- domain: 通用\n- summary: 默认任务\n",
                encoding="utf-8",
            )
            (memory_dir / "memory.md").write_text(
                "# Agent 主记忆\n\n最近主要在做 FPGA 和搜索工作。\n",
                encoding="utf-8",
            )
            upsert_task_memory_record(
                memory_dir=memory_dir,
                record={
                    "run_id": "20260407-173737",
                    "task": "编写滑动平均 Verilog 模块",
                    "summary": "完成了滑动平均模块和 testbench 编写，并通过仿真验证。",
                    "keywords": ["verilog", "sliding-average", "simulation"],
                },
            )

            router = TaskRouter(skill_dir=skill_dir)
            router._llm_call = lambda prompt, max_tokens=200, timeout=15.0: json.dumps(
                {
                    "task_semantics": "调试滑动平均 Verilog 仿真",
                    "is_review_task": False,
                    "referenced_contexts": [],
                    "must_load_profiles": ["general"],
                    "should_load_profiles": [],
                    "memory_tags": ["verilog"],
                    "reasoning": "需要保留通用能力并参考近期硬件任务记忆。",
                },
                ensure_ascii=False,
            )

            result = router.classify("调试滑动平均 Verilog 仿真", memory_dir=memory_dir)

            self.assertIn("FPGA", result.memory_context["main_memory_summary"])
            self.assertEqual(len(result.memory_context["task_memory_hits"]), 1)
            self.assertEqual(result.memory_context["task_memory_hits"][0]["run_id"], "20260407-173737")
```

Expected assertions:

- `memory_context["main_memory_summary"]` is non-empty
- `memory_context["task_memory_hits"]` contains the relevant run record
- `tool_recall_memory(...)` returns both layers, not just long-term lessons

- [ ] **Step 6: Run tests to verify Task 4 passes**

Run: `python -m unittest tests.test_memory_engine.StartupMemoryLoadTests tests_runtime_regressions.LayeredStartupMemoryTests -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add agent/runtime/memory_engine.py agent/core/router.py agent/tools/standard.py tests/test_memory_engine.py tests_runtime_regressions.py
git commit -m "feat: load layered memory during startup"
```

## Task 5: End-to-End Verification And Cleanup

**Files:**
- Modify: `doc/memory-layered-design.md`
- Modify: `doc/memory-layered-implementation-plan.md`
- Test: `tests/test_memory_engine.py`
- Test: `tests_runtime_regressions.py`
- Test: `tests/test_persistence.py`

- [ ] **Step 1: Add an end-to-end memory lifecycle test**

```python
class MemoryLifecycleTests(unittest.TestCase):
    def test_full_memory_flow_from_run_finish_to_startup_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "20260407-233451"
            memory_dir = root / "memory"
            run_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (run_dir / "execution_summary.md").write_text(
                "# Execution Summary\n\n## Goal\n搜索 Qwen3.5 微调方法\n\n## Final Answer\n整理了主流微调框架。\n",
                encoding="utf-8",
            )

            record = build_task_memory_record_from_run(run_dir=run_dir)
            upsert_task_memory_record(memory_dir=memory_dir, record=record)
            refreshed = maybe_refresh_main_memory(memory_dir=memory_dir, force=True)
            payload = load_startup_memory(memory_dir=memory_dir, goal="继续搜索 Qwen 微调框架", top_k=3)

            records = load_task_memory_records(memory_dir)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["run_id"], "20260407-233451")
            self.assertTrue(refreshed)
            self.assertTrue((memory_dir / "memory.md").exists())
            self.assertTrue(payload["main_memory_summary"])
            self.assertGreaterEqual(len(payload["task_memory_hits"]), 1)
```

Required assertions:

- `task_memory.jsonl` contains exactly one record for the run
- `memory.md` is regenerated
- startup loader returns both `main_memory_summary` and at least one related hit

- [ ] **Step 2: Run the focused memory test suites**

Run: `python -m unittest tests.test_memory_engine tests_runtime_regressions -v`

Expected: PASS

- [ ] **Step 3: Run the existing persistence regression suite**

Run: `python -m unittest tests.test_persistence -v`

Expected: PASS

- [ ] **Step 4: Update docs if implementation diverged from the design**

```md
- 若最终实现把 `task_memory.jsonl` 文件名、重写阈值或返回字段命名做了小幅调整，
  必须同步更新 `doc/memory-layered-design.md`，保证设计文档与实现一致。
```

- [ ] **Step 5: Commit**

```bash
git add agent/runtime/memory_engine.py agent/core/memory.py agent/core/router.py agent/tools/standard.py run_goal.py tests/test_memory_engine.py tests_runtime_regressions.py tests/test_persistence.py doc/memory-layered-design.md doc/memory-layered-implementation-plan.md
git commit -m "feat: implement layered task and startup memory flow"
```

## Self-Review

### Spec Coverage

- 任务结束时生成结构化任务记忆：Task 1 + Task 2 覆盖
- 从任务记忆重写总体记忆：Task 3 覆盖
- 预热阶段加载 `memory.md` 与部分 `task_memory.jsonl`：Task 4 覆盖
- 失败时降级写入、不阻塞主任务结束：Task 2 覆盖
- 文档与回归验证：Task 5 覆盖

### Placeholder Scan

本计划避免使用 `TODO`、`TBD`、"类似 Task N" 之类的占位描述。所有任务都明确了目标文件、测试入口、命令和预期结果。

### Type Consistency

计划中统一使用以下命名：

- `task_memory.jsonl`
- `record_task_memory(...)`
- `build_task_memory_record_from_run(...)`
- `upsert_task_memory_record(...)`
- `maybe_refresh_main_memory(...)`
- `load_startup_memory(...)`
- `main_memory_summary`
- `task_memory_hits`

实现时不要再引入含义重复的别名，例如 `memory_digest`、`memory_records`、`startup_memory_blob`。
