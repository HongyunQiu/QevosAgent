#!/usr/bin/env python3
"""后台任务管理器的回归测试。

覆盖三件此前静默出错的事：
  1. cleanup 曾直接删记录 → 事后 job_wait 只回「任务不存在」，而输出还在磁盘上；
  2. 进程退出后任务无台账 → 崩溃/被杀之后，那些后台进程再没有任何线索可查；
  3. 认领来的任务只有一个 PID → 拿可能已被复用的 PID 去杀进程树是错杀。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent.core.async_manager import AsyncJobManager, JobStatus, _identity_matches


def _wait_done(mgr: AsyncJobManager, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = mgr.peek(job_id)
        if info.get("status") != JobStatus.RUNNING.value:
            return info
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} 在 {timeout}s 内未结束")


class CleanupArchivesInsteadOfDeletingTests(unittest.TestCase):
    def test_cleanup_keeps_record_and_output_after_archiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            job_id = mgr.start_shell(f'"{sys.executable}" -c "print(\'marker-xyz\')"')
            info = _wait_done(mgr, job_id)
            self.assertIn("marker-xyz", info["output"])

            # max_age_secs=0 → 立刻归档
            self.assertEqual(mgr.cleanup(max_age_secs=0), 1)

            # 记录仍在（原实现这里会变成 "不存在或已被清理"）
            after = mgr.peek(job_id)
            self.assertNotIn("error", after)
            self.assertEqual(after["status"], JobStatus.DONE.value)
            # 输出从磁盘回读，没有蒸发
            self.assertIn("marker-xyz", after["output"])

            listed = {j["job_id"]: j for j in mgr.list_jobs()}
            self.assertIn(job_id, listed)
            self.assertTrue(listed[job_id].get("archived"))

    def test_cleanup_does_not_touch_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            job_id = mgr.start_shell(f'"{sys.executable}" -c "import time; time.sleep(30)"')
            try:
                self.assertEqual(mgr.cleanup(max_age_secs=0), 0)
                self.assertEqual(mgr.peek(job_id)["status"], JobStatus.RUNNING.value)
            finally:
                mgr.cancel(job_id)


class RegistryTests(unittest.TestCase):
    def test_registry_records_pid_and_final_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            job_id = mgr.start_shell(f'"{sys.executable}" -c "print(1)"')
            _wait_done(mgr, job_id)

            payload = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["owner_pid"], os.getpid())
            rec = {j["job_id"]: j for j in payload["jobs"]}[job_id]
            self.assertIsInstance(rec["pid"], int)
            self.assertEqual(rec["status"], JobStatus.DONE.value)
            self.assertEqual(rec["returncode"], 0)

    def test_load_registry_claims_only_live_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 伪造上一个进程留下的台账：一条仍在运行（PID 用本测试进程，必定活着）、
            # 一条已结束、一条 PID 早已消失。
            (Path(tmp) / "index.json").write_text(json.dumps({
                "owner_pid": os.getpid() + 1,
                "updated_at": time.time(),
                "jobs": [
                    {"job_id": "job_live", "command": "sleep 999", "pid": os.getpid(),
                     "status": "running", "returncode": None,
                     "start_time": time.time(), "end_time": None},
                    {"job_id": "job_done", "command": "echo hi", "pid": os.getpid(),
                     "status": "done", "returncode": 0,
                     "start_time": time.time(), "end_time": time.time()},
                    {"job_id": "job_gone", "command": "sleep 999", "pid": 999_999_9,
                     "status": "running", "returncode": None,
                     "start_time": time.time(), "end_time": None},
                ],
            }), encoding="utf-8")

            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            self.assertEqual(mgr.load_registry(), 1)
            ids = {j["job_id"] for j in mgr.list_jobs()}
            self.assertEqual(ids, {"job_live"})
            info = mgr.peek("job_live")
            self.assertTrue(info["reclaimed"])
            self.assertEqual(info["pid"], os.getpid())

    def test_load_registry_ignores_own_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            job_id = mgr.start_shell(f'"{sys.executable}" -c "import time; time.sleep(30)"')
            try:
                # 同一进程重复加载不应把自己的任务再认领一遍
                self.assertEqual(mgr.load_registry(), 0)
                self.assertEqual(len(mgr.list_jobs()), 1)
            finally:
                mgr.cancel(job_id)


class ReclaimedCancelSafetyTests(unittest.TestCase):
    def test_cancel_refuses_when_identity_does_not_match(self):
        """PID 复用防护：认领任务的命令与该 PID 上真正跑的东西对不上时必须拒杀。

        这里把命令写成一串绝不会出现在本测试进程命令行里的 token，
        身份校验必然判否——而那个 PID 是本测试进程自己，真杀下去就是自尽。
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "index.json").write_text(json.dumps({
                "owner_pid": os.getpid() + 1,
                "updated_at": time.time(),
                "jobs": [{
                    "job_id": "job_reused",
                    "command": "zzqq_definitely_not_running_anywhere --flag",
                    "pid": os.getpid(),
                    "status": "running", "returncode": None,
                    "start_time": time.time(), "end_time": None,
                }],
            }), encoding="utf-8")

            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            self.assertEqual(mgr.load_registry(), 1)

            result = mgr.cancel("job_reused")
            self.assertIn("error", result)
            self.assertNotIn("cancelled", result)
            self.assertIn(str(os.getpid()), result["error"])

            # 退出清场同样必须放过它，且不计入"已终止"
            self.assertEqual(mgr.cancel_all_running(), 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "index.json")))

    def test_identity_matches_returns_true_for_this_process(self):
        # 本进程的命令行里一定有解释器路径的组成部分
        token = Path(sys.executable).stem
        self.assertIs(_identity_matches(os.getpid(), f"{token} -m unittest"), True)

    def test_identity_matches_unknown_pid_is_unverifiable_not_false(self):
        # 取不到命令行必须回 None（无法证实），不能回 False 更不能回 True
        self.assertIsNone(_identity_matches(None, "anything"))


class ExitCleanupTests(unittest.TestCase):
    def test_cancel_all_running_kills_owned_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            ids = [
                mgr.start_shell(f'"{sys.executable}" -c "import time; time.sleep(60)"')
                for _ in range(3)
            ]
            self.assertEqual(mgr.cancel_all_running(), 3)
            for job_id in ids:
                self.assertEqual(mgr.peek(job_id)["status"], JobStatus.CANCELLED.value)

            payload = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
            self.assertTrue(all(j["status"] == "cancelled" for j in payload["jobs"]))


class ConcurrentJobsTests(unittest.TestCase):
    def test_multiple_jobs_run_and_report_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = AsyncJobManager(jobs_dir=Path(tmp))
            ids = [
                mgr.start_shell(f'"{sys.executable}" -c "print(\'out-{n}\')"')
                for n in range(4)
            ]
            for n, job_id in enumerate(ids):
                info = _wait_done(mgr, job_id)
                self.assertEqual(info["returncode"], 0)
                self.assertIn(f"out-{n}", info["output"])
            # 每个任务的输出各自落一个文件，互不覆盖
            for job_id in ids:
                self.assertTrue((Path(tmp) / f"{job_id}.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
