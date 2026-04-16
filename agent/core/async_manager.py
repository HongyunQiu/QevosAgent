"""
异步任务管理器

在后台线程中运行 shell 命令，主循环可随时轮询部分输出，
彻底解决 communicate(timeout=N) 阻塞导致的卡死与输出丢失问题。

生命周期：绑定在 state.meta["_async_manager"]，随 AgentState 存在。
不可序列化 —— persistence.py 在写 meta.json 时会跳过此键。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import IO, Optional


class JobStatus(str, Enum):
    RUNNING   = "running"
    DONE      = "done"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id:     str
    command:    str
    start_time: float
    proc:       subprocess.Popen

    _stdout_lines: list[str]       = field(default_factory=list)
    _stderr_lines: list[str]       = field(default_factory=list)
    _lock:         threading.Lock  = field(default_factory=threading.Lock)

    status:     JobStatus          = JobStatus.RUNNING
    returncode: Optional[int]      = None
    end_time:   Optional[float]    = None

    # 内部线程/定时器，不对外暴露
    _reader_thread:  Optional[threading.Thread] = field(default=None, repr=False)
    _timeout_timer:  Optional[threading.Timer]  = field(default=None, repr=False)

    # ── 快照读取（线程安全）────────────────────────────────────────────────────

    def stdout_snapshot(self) -> str:
        with self._lock:
            return "".join(self._stdout_lines)

    def stderr_snapshot(self) -> str:
        with self._lock:
            return "".join(self._stderr_lines)

    def elapsed(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time


# ── 进程树终止（跨平台）──────────────────────────────────────────────────────

def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            f"taskkill /F /T /PID {pid}",
            shell=True, capture_output=True,
        )
    else:
        try:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


# ── 主类 ──────────────────────────────────────────────────────────────────────

class AsyncJobManager:
    """
    后台任务管理器。

    典型用法（在 LLM agent 工具里）：
        job_id = manager.start_shell("npm install", timeout=120)
        # … 继续做其他工作 …
        info   = manager.peek(job_id, wait_secs=10)   # 最多等 10s
        if info["status"] == "running":
            # 还在跑，稍后再查
        else:
            print(info["output"])
    """

    def __init__(self, jobs_dir: Optional[Path] = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._global_lock = threading.Lock()
        self._jobs_dir: Optional[Path] = Path(jobs_dir) if jobs_dir else None
        if self._jobs_dir:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)

    # ── 启动 ──────────────────────────────────────────────────────────────────

    def start_shell(self, command: str, timeout: Optional[int] = None) -> str:
        """
        在后台线程中启动 shell 命令，立即返回 job_id。

        timeout: 整个命令允许运行的最长秒数（None / 0 = 不限制）。
                 超时后进程树被强制终止，状态变为 CANCELLED。
        """
        job_id = f"job_{uuid.uuid4().hex[:8]}"

        popen_kwargs: dict = {
            "shell":  True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text":   True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(command, **popen_kwargs)
        except Exception as e:
            # 启动失败 → 创建一个已失败的 Job 占位，保持接口一致
            dummy = Job(
                job_id=job_id,
                command=command,
                start_time=time.time(),
                proc=None,  # type: ignore[arg-type]
                status=JobStatus.FAILED,
                end_time=time.time(),
                returncode=-1,
            )
            dummy._stderr_lines.append(str(e))
            with self._global_lock:
                self._jobs[job_id] = dummy
            return job_id

        job = Job(
            job_id=job_id,
            command=command,
            start_time=time.time(),
            proc=proc,
        )

        # 启动后台读取线程
        reader = threading.Thread(target=self._reader, args=(job,), daemon=True)
        reader.start()
        job._reader_thread = reader

        # 可选：硬超时定时器
        if timeout and timeout > 0:
            timer = threading.Timer(timeout, self._on_timeout, args=(job_id,))
            timer.daemon = True
            timer.start()
            job._timeout_timer = timer

        with self._global_lock:
            self._jobs[job_id] = job

        return job_id

    # ── 后台读取线程 ──────────────────────────────────────────────────────────

    def _reader(self, job: Job) -> None:
        """
        同时用两个子线程读 stdout / stderr，避免一个管道满导致另一个阻塞。
        父线程等两者都结束后调用 proc.wait() 获取 returncode。
        如果 _jobs_dir 已设置，同时将输出实时写入 {jobs_dir}/{job_id}.txt。
        """
        job_file: Optional[IO[str]] = None
        if self._jobs_dir:
            try:
                job_file = open(
                    self._jobs_dir / f"{job.job_id}.txt",
                    "w", encoding="utf-8", errors="replace", buffering=1,
                )
                job_file.write(f"$ {job.command}\n")
                job_file.flush()
            except Exception:
                job_file = None

        def _drain(stream, lines, lock, prefix: str = ""):
            try:
                for line in stream:
                    with lock:
                        lines.append(line)
                        if job_file:
                            try:
                                job_file.write(prefix + line)
                                job_file.flush()
                            except Exception:
                                pass
            except Exception:
                pass

        t_out = threading.Thread(
            target=_drain,
            args=(job.proc.stdout, job._stdout_lines, job._lock, ""),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain,
            args=(job.proc.stderr, job._stderr_lines, job._lock, "[STDERR] "),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()

        job.proc.wait()
        job.returncode = job.proc.returncode

        with job._lock:
            if job.status == JobStatus.RUNNING:
                job.status = (
                    JobStatus.DONE if job.returncode == 0 else JobStatus.FAILED
                )
            job.end_time = time.time()

        if job_file:
            try:
                job_file.write(f"\n[Exit {job.returncode}]\n")
                job_file.close()
            except Exception:
                pass

        # 如果命令已自然结束，取消超时定时器
        if job._timeout_timer:
            job._timeout_timer.cancel()

    def _on_timeout(self, job_id: str) -> None:
        """超时定时器回调：标记 CANCELLED 并杀掉进程树。"""
        job = self._jobs.get(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        with job._lock:
            job.status = JobStatus.CANCELLED
        _kill_tree(job.proc.pid)
        try:
            job.proc.kill()
        except Exception:
            pass

    # ── 查询 / 等待 ────────────────────────────────────────────────────────────

    def peek(self, job_id: str, wait_secs: float = 0.0) -> dict:
        """
        返回任务的当前状态与已捕获输出。

        wait_secs > 0：在返回前最多阻塞 wait_secs 秒等待完成。
                       适合"等 10 秒，拿部分结果"的轮询模式。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"job_id '{job_id}' 不存在或已被清理"}

        if wait_secs > 0 and job.status == JobStatus.RUNNING:
            deadline = time.time() + wait_secs
            while time.time() < deadline and job.status == JobStatus.RUNNING:
                time.sleep(0.2)

        stdout = job.stdout_snapshot().strip()
        stderr = job.stderr_snapshot().strip()
        output = stdout
        if stderr:
            output += f"\n[STDERR]: {stderr}"

        return {
            "job_id":     job_id,
            "status":     job.status.value,
            "output":     output or "（暂无输出）",
            "returncode": job.returncode,
            "elapsed_s":  round(job.elapsed(), 1),
            "command":    job.command,
        }

    # ── 取消 ──────────────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> dict:
        """强制终止一个仍在运行的任务。"""
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"job_id '{job_id}' 不存在"}
        if job.status != JobStatus.RUNNING:
            return {"error": f"任务 {job_id} 已结束（状态: {job.status.value}），无需取消"}

        with job._lock:
            job.status = JobStatus.CANCELLED

        if job._timeout_timer:
            job._timeout_timer.cancel()

        _kill_tree(job.proc.pid)
        try:
            job.proc.kill()
        except Exception:
            pass

        return {"job_id": job_id, "cancelled": True}

    # ── 列表 ──────────────────────────────────────────────────────────────────

    def list_jobs(self) -> list[dict]:
        """返回所有任务的摘要列表（含已完成的，直到被 cleanup 清除）。"""
        with self._global_lock:
            jobs = list(self._jobs.values())
        return [
            {
                "job_id":     j.job_id,
                "status":     j.status.value,
                "command":    j.command[:100],
                "elapsed_s":  round(j.elapsed(), 1),
                "returncode": j.returncode,
            }
            for j in jobs
        ]

    # ── 清理 ──────────────────────────────────────────────────────────────────

    def cleanup(self, max_age_secs: int = 300) -> int:
        """
        删除已完成且存活超过 max_age_secs 秒的任务记录。
        返回删除数量。
        """
        cutoff = time.time() - max_age_secs
        to_remove: list[str] = []
        with self._global_lock:
            for jid, j in self._jobs.items():
                if j.status != JobStatus.RUNNING and j.end_time and j.end_time < cutoff:
                    to_remove.append(jid)
            for jid in to_remove:
                del self._jobs[jid]
        return len(to_remove)

    def cancel_all_running(self) -> int:
        """取消所有仍在运行的任务（agent 退出时调用）。"""
        count = 0
        with self._global_lock:
            running = [j for j in self._jobs.values() if j.status == JobStatus.RUNNING]
        for job in running:
            self.cancel(job.job_id)
            count += 1
        return count
