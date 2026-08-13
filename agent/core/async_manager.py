"""
异步任务管理器

在后台线程中运行 shell 命令，主循环可随时轮询部分输出，
彻底解决 communicate(timeout=N) 阻塞导致的卡死与输出丢失问题。

生命周期：绑定在 state.meta["_async_manager"]，随 AgentState 存在。
不可序列化 —— persistence.py 在写 meta.json 时会跳过此键。

任务元数据另外落一份到 {jobs_dir}/index.json（见 _flush_registry）。内存里的
manager 随进程消失，被它启动的**进程却不会**；没有这份注册表，进程一旦异常
退出，那些后台进程就再没有任何记录可查——既不知道有几个、也不知道 PID。
"""

from __future__ import annotations

import json
import os
import re
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

    # 进程 ID 单独存一份：proc 可能为 None（启动失败的占位，或从注册表认领来的
    # 上一进程遗留任务），那时 proc.pid 取不到，但 PID 仍是唯一能操作它的把手。
    pid:        Optional[int]      = None
    # 已归档：输出缓冲被释放（全文仍在 {jobs_dir}/{job_id}.txt），记录本身保留。
    # 见 AsyncJobManager.cleanup —— 删记录会让 job_wait 报「任务不存在」。
    retired:    bool               = False
    # 从上一进程的注册表认领而来（本进程没有它的 Popen 句柄）
    reclaimed:  bool               = False
    # 上次探测 PID 存活的时刻。探测在 Windows 上要拉起一个 tasklist 子进程，
    # 而 peek(wait_secs=10) 的等待循环是 0.2s 一转——不节流就是每次 job_wait
    # 拉起五十个进程。
    _probed_at: float              = 0.0

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

def _kill_tree(pid: Optional[int]) -> None:
    if not pid or pid <= 0:
        return
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


# ── 进程身份校验（PID 复用防护）──────────────────────────────────────────────
# 只在「认领上一进程遗留的任务」这条路径上用。那时我们手里只有一个 PID，而
# PID 是会被系统回收再分配的——尤其 Windows。拿一个复用后的 PID 去 taskkill /T
# 会杀掉一个完全无关的进程树，所以：**证实不了身份就不许杀**。


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return False
        return f'"{int(pid)}"' in (out.stdout or "")
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 活着，只是不归我们管
    except Exception:
        return False


def _pid_cmdline(pid: Optional[int]) -> Optional[str]:
    """取进程命令行。取不到返回 None —— 调用方必须把 None 当作「无法证实身份」，
    而不是「不匹配」或「匹配」。"""
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        probes = [
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            ["wmic", "process", "where", f"processid={int(pid)}", "get", "commandline", "/value"],
        ]
        for probe in probes:
            try:
                res = subprocess.run(probe, capture_output=True, text=True, timeout=20)
            except Exception:
                continue
            text = (res.stdout or "").strip()
            if text:
                return text
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        res = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            capture_output=True, text=True, timeout=15,
        )
        return (res.stdout or "").strip() or None
    except Exception:
        return None


def _identity_matches(pid: Optional[int], command: str) -> Optional[bool]:
    """PID 上跑的是否还是当初那条命令。

    返回 True / False / None(无法证实)。判据是「原命令里几个最长的词是否都还
    出现在该进程的命令行里」——shell=True 时进程是 sh -c / cmd.exe /c，命令
    原文就在它的命令行里，比逐字相等稳，也不受引号与路径规范化影响。
    """
    cmdline = _pid_cmdline(pid)
    if cmdline is None:
        return None
    tokens = sorted(re.findall(r"[A-Za-z0-9_./\\-]{4,}", command or ""), key=len, reverse=True)[:3]
    if not tokens:
        return None
    low = cmdline.lower()
    return all(tok.lower() in low for tok in tokens)


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
        # 注册表写入串行化：每个 job 的读线程在自己结束时都会 flush 一次，多个
        # job 同时收尾就会并发写同一个 index.json.tmp，互相截断。
        # 锁序固定为 _registry_lock → _global_lock，没有反向获取，不会死锁。
        self._registry_lock = threading.Lock()
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
        else:
            # Give each job its own session so os.killpg() only kills that job's
            # process tree — without this, _kill_tree on any job would SIGKILL the
            # entire process group, taking down other running jobs and the agent.
            popen_kwargs["start_new_session"] = True

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
            self._flush_registry()
            return job_id

        job = Job(
            job_id=job_id,
            command=command,
            start_time=time.time(),
            proc=proc,
            pid=proc.pid,
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
        self._flush_registry()

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

        # 管道要显式关闭。_drain 只读到 EOF 就退出，句柄仍开着——一次 run 里
        # 起几十个 job 就是几十个泄漏的 fd（测试里表现为 ResourceWarning）。
        for stream in (job.proc.stdout, job.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

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

        self._flush_registry()

    def _on_timeout(self, job_id: str) -> None:
        """超时定时器回调：标记 CANCELLED 并杀掉进程树。"""
        job = self._jobs.get(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        with job._lock:
            job.status = JobStatus.CANCELLED
        _kill_tree(job.pid)
        try:
            if job.proc is not None:
                job.proc.kill()
        except Exception:
            pass
        self._flush_registry()

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
                if job.reclaimed:
                    self._refresh_reclaimed(job)

        if job.reclaimed:
            self._refresh_reclaimed(job)

        stdout = job.stdout_snapshot().strip()
        stderr = job.stderr_snapshot().strip()
        output = stdout
        if stderr:
            output += f"\n[STDERR]: {stderr}"
        # 归档/认领来的任务内存里没有缓冲，但全文一直在磁盘上。这里必须回读，
        # 否则 cleanup 之后再问一次就只剩「（暂无输出）」，等于输出凭空蒸发。
        if not output:
            output = self._read_archived_output(job).strip()

        info = {
            "job_id":     job_id,
            "status":     job.status.value,
            "output":     output or "（暂无输出）",
            "returncode": job.returncode,
            "elapsed_s":  round(job.elapsed(), 1),
            "command":    job.command,
        }
        if job.reclaimed:
            info["reclaimed"] = True
            info["pid"] = job.pid
            info["note"] = (
                "该任务由上一个 agent 进程启动，本进程只认领了它的记录，"
                "没有它的输出管道——输出仅到上个进程退出为止。"
            )
        return info

    # ── 归档输出回读 ──────────────────────────────────────────────────────────

    def _job_file(self, job_id: str) -> Optional[Path]:
        return (self._jobs_dir / f"{job_id}.txt") if self._jobs_dir else None

    def _read_archived_output(self, job: Job, tail_chars: int = 20000) -> str:
        path = self._job_file(job.job_id)
        if path is None or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return text if len(text) <= tail_chars else "…（已截断前段）\n" + text[-tail_chars:]

    def _refresh_reclaimed(self, job: Job, min_interval: float = 3.0) -> None:
        """认领来的任务没有 Popen 句柄，只能靠 PID 是否还在来判定死活。

        进程消失即视为结束：退出码无从得知（留 None），这比一直显示 running
        诚实——那会让 job_wait 永远等下去。
        """
        if job.status != JobStatus.RUNNING:
            return
        now = time.time()
        if now - job._probed_at < min_interval:
            return
        job._probed_at = now
        if _pid_alive(job.pid):
            return
        with job._lock:
            job.status = JobStatus.DONE
            job.end_time = time.time()
        self._flush_registry()

    # ── 取消 ──────────────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> dict:
        """强制终止一个仍在运行的任务。"""
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"job_id '{job_id}' 不存在"}
        if job.reclaimed:
            self._refresh_reclaimed(job)
        if job.status != JobStatus.RUNNING:
            return {"error": f"任务 {job_id} 已结束（状态: {job.status.value}），无需取消"}

        # 认领来的任务：手里只有一个 PID，而 PID 会被系统回收再分配。证实不了
        # 身份就拒绝动手——错杀一个复用了该 PID 的无关进程树，代价远大于留一个
        # 孤儿进程。把 PID 交还给调用方，由人来判断。
        if job.reclaimed:
            verdict = _identity_matches(job.pid, job.command)
            if verdict is not True:
                reason = "该 PID 上跑的已经不是这条命令" if verdict is False else "无法读取该进程的命令行"
                return {
                    "error": (
                        f"拒绝终止认领任务 {job_id}：{reason}，无法确认 PID {job.pid} "
                        f"仍是当初那个进程（PID 可能已被系统复用）。"
                        f"如确需终止，请人工核对后手动处理该 PID。"
                    )
                }

        with job._lock:
            job.status = JobStatus.CANCELLED

        if job._timeout_timer:
            job._timeout_timer.cancel()

        _kill_tree(job.pid)
        try:
            if job.proc is not None:
                job.proc.kill()
        except Exception:
            pass

        self._flush_registry()
        return {"job_id": job_id, "cancelled": True}

    # ── 列表 ──────────────────────────────────────────────────────────────────

    def list_jobs(self) -> list[dict]:
        """返回所有任务的摘要列表（含已完成与已归档的）。"""
        with self._global_lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            if j.reclaimed:
                self._refresh_reclaimed(j)
        out = []
        for j in jobs:
            entry = {
                "job_id":     j.job_id,
                "status":     j.status.value,
                "command":    j.command[:100],
                "elapsed_s":  round(j.elapsed(), 1),
                "returncode": j.returncode,
            }
            if j.retired:
                entry["archived"] = True
            if j.reclaimed:
                entry["reclaimed"] = True
                entry["pid"] = j.pid
            out.append(entry)
        return out

    # ── 清理 ──────────────────────────────────────────────────────────────────

    def cleanup(self, max_age_secs: int = 300) -> int:
        """把已完成且超过 max_age_secs 秒的任务**归档**：释放内存里的输出缓冲，
        记录本身保留。返回归档数量。

        原先这里是直接 `del self._jobs[jid]`。删记录有两个后果，都很难查：
          1. 事后 job_wait / wait_for_job 只会回一句「任务不存在或已被清理」，
             而输出其实好端端躺在 {jobs_dir}/{job_id}.txt 里；
          2. 若一个 job 在完成通知（_notify_completed_jobs 每轮开头推送）送达前
             就被删掉——比如 agent 卡在一个几分钟的工具调用里，回来先调了
             jobs_list——那条完成通知就**永远不会**出现，任务静默消失。
        归档只放掉内存，两个后果都没了；单条记录只有几十字节，不必回收。
        """
        cutoff = time.time() - max_age_secs
        retired = 0
        with self._global_lock:
            targets = [
                j for j in self._jobs.values()
                if not j.retired and j.status != JobStatus.RUNNING
                and j.end_time and j.end_time < cutoff
            ]
        for job in targets:
            with job._lock:
                job._stdout_lines = []
                job._stderr_lines = []
                job.retired = True
            retired += 1
        if retired:
            self._flush_registry()
        return retired

    # ── 注册表（跨进程可见的任务台账）────────────────────────────────────────

    def _flush_registry(self) -> None:
        """把任务元数据快照写到 {jobs_dir}/index.json。best-effort，绝不抛。

        这是进程崩溃/被杀之后唯一还能找到那些后台进程的线索（PID + 命令 + 状态）。
        输出全文本来就在同目录的 {job_id}.txt，这里只存元数据。
        """
        if not self._jobs_dir:
            return
        try:
            with self._registry_lock:
                self._write_registry()
        except Exception:
            pass

    def _write_registry(self) -> None:
        try:
            with self._global_lock:
                jobs = list(self._jobs.values())
            payload = {
                "owner_pid": os.getpid(),
                "updated_at": time.time(),
                "jobs": [
                    {
                        "job_id":     j.job_id,
                        "command":    j.command,
                        "pid":        j.pid,
                        "status":     j.status.value,
                        "returncode": j.returncode,
                        "start_time": j.start_time,
                        "end_time":   j.end_time,
                        "reclaimed":  j.reclaimed,
                    }
                    for j in jobs
                ],
            }
            path = self._jobs_dir / "index.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def load_registry(self) -> int:
        """认领 {jobs_dir}/index.json 里**仍在运行**的任务，返回认领数量。

        只认领 running 的：已结束的任务重新注入毫无用处，反而会让框架把上一轮
        的完成通知再推一遍。认领来的任务没有 Popen 句柄，只能按 PID 观察死活，
        且终止前必须先验身份（见 cancel）。
        """
        if not self._jobs_dir:
            return 0
        path = self._jobs_dir / "index.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if payload.get("owner_pid") == os.getpid():
            return 0        # 就是本进程写的，没什么可认领
        claimed = 0
        for rec in payload.get("jobs", []) or []:
            try:
                job_id = str(rec.get("job_id") or "")
                pid    = rec.get("pid")
                if not job_id or job_id in self._jobs:
                    continue
                if rec.get("status") != JobStatus.RUNNING.value:
                    continue
                if not _pid_alive(pid):
                    continue
                job = Job(
                    job_id=job_id,
                    command=str(rec.get("command") or ""),
                    start_time=float(rec.get("start_time") or time.time()),
                    proc=None,          # type: ignore[arg-type]
                    pid=int(pid),
                    reclaimed=True,
                    retired=True,       # 输出缓冲不在本进程，一律走磁盘回读
                )
                with self._global_lock:
                    self._jobs[job_id] = job
                claimed += 1
            except Exception:
                continue
        if claimed:
            self._flush_registry()
        return claimed

    def cancel_all_running(self) -> int:
        """取消所有仍在运行的任务（agent 退出时调用），返回**实际终止**的数量。

        认领来的任务若验不了身份会被 cancel 拒绝，那时不计数——宁可留一个孤儿
        进程，也不能拿一个可能已被复用的 PID 去杀进程树。
        """
        count = 0
        with self._global_lock:
            running = [j for j in self._jobs.values() if j.status == JobStatus.RUNNING]
        for job in running:
            result = self.cancel(job.job_id)
            if result.get("cancelled"):
                count += 1
        self._flush_registry()
        return count
