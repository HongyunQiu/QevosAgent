"""
Agent 团队通信 HTTP 服务
========================
每个 QevosAgent 实例启动时可开启此服务，暴露以下端点供组网通信：

  GET  /agent/status      轻量状态快照（读 status.json）
  GET  /agent/snapshot    完整快照：meta + scratchpad + 最近 5 条 short_term
  GET  /agent/questions   主管侧：获取来自队员的待回答问题列表

  POST /agent/inject      注入一条消息到 Agent 上下文（任意方向）
  POST /agent/task        队员侧：接收主管分配的子任务（包装为 inject）
  POST /agent/question    主管侧：接收队员提交的问题（含 question_id）
  POST /agent/answer      队员侧：接收主管对某问题的回答（按 question_id 配对）

所有 POST body 均为 JSON，所有响应也为 JSON。
使用纯 stdlib（http.server + threading + urllib），不引入额外依赖。
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.runtime.user_interrupt import UserInterruptHandler


class _Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理器；通过 self.server.api 引用 TeamApiServer 实例。"""

    def log_message(self, format, *args):
        pass  # 静默，避免干扰 agent 控制台输出

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def do_GET(self) -> None:
        api: TeamApiServer = self.server.api
        path = self.path.split("?")[0].rstrip("/")

        if path == "/agent/status":
            self._send_json(200, api.get_status())
        elif path == "/agent/snapshot":
            self._send_json(200, api.get_snapshot())
        elif path == "/agent/questions":
            self._send_json(200, {"questions": api.get_questions()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        api: TeamApiServer = self.server.api
        path = self.path.split("?")[0].rstrip("/")
        body = self._read_json()
        if body is None:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if path == "/agent/inject":
            # 通用消息注入（主管 → 队员，或任意方向的指令）
            message = body.get("message", "").strip()
            if not message:
                self._send_json(400, {"error": "message required"})
                return
            api.inject_message(message)
            self._send_json(200, {"ok": True})

        elif path == "/agent/task":
            # 主管向队员分配子任务
            task = body.get("task", "").strip()
            context = body.get("context", "").strip()
            if not task:
                self._send_json(400, {"error": "task required"})
                return
            msg = f"[新任务来自主管] {task}"
            if context:
                msg += f"\n[背景] {context}"
            api.inject_message(msg)
            self._send_json(200, {"ok": True})

        elif path == "/agent/question":
            # 队员向主管提问（主管侧接收）
            qid = body.get("question_id", "").strip()
            worker_id = body.get("worker_id", "unknown")
            content = body.get("content", "").strip()
            if not (qid and content):
                self._send_json(400, {"error": "question_id and content required"})
                return
            api.add_question(qid, worker_id, content)
            self._send_json(200, {"ok": True})

        elif path == "/agent/answer":
            # 主管向队员回答问题（队员侧接收，按 question_id 配对）
            qid = body.get("question_id", "").strip()
            answer = body.get("answer", "").strip()
            if not (qid and answer):
                self._send_json(400, {"error": "question_id and answer required"})
                return
            api._answer_queue.put({"question_id": qid, "answer": answer})
            self._send_json(200, {"ok": True})

        else:
            self._send_json(404, {"error": "not found"})


class TeamApiServer:
    """
    Agent 团队通信 HTTP 服务端。

    在后台守护线程运行，与 Agent 主循环通过以下方式共享状态：
    - inject_message()  → 直接写入 interrupt_handler._cmd_queue（无文件竞争）
    - add_question()    → 写入本地 _questions 列表 + 通知主管循环
    - wait_for_answer() → 阻塞轮询 _answer_queue，带主管存活检测
    """

    def __init__(
        self,
        port: int,
        run_dir: str | Path,
        interrupt_handler: "UserInterruptHandler",
    ):
        self.port = port
        self.run_dir = Path(run_dir)
        self.interrupt_handler = interrupt_handler

        # 队员侧：等待主管回答的答案队列（question_id → answer）
        self._answer_queue: queue.Queue[dict] = queue.Queue()

        # 主管侧：来自队员的待回答问题列表
        self._questions: list[dict] = []
        self._questions_lock = threading.Lock()

        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── GET 端点实现 ──────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """读 status.json（轻量，不加载完整 meta）。"""
        try:
            return json.loads((self.run_dir / "status.json").read_text(encoding="utf-8"))
        except Exception:
            return {"status": "unknown"}

    def get_snapshot(self) -> dict:
        """返回 meta（过滤大字段）+ scratchpad + 最近 5 条 short_term。"""
        meta: dict = {}
        try:
            mp = self.run_dir / "meta.json"
            if mp.exists():
                raw = json.loads(mp.read_text(encoding="utf-8"))
                _SKIP = {"short_term", "_advisor_system", "evolved_tools",
                         "_team_api", "_async_manager", "_llm"}
                meta = {k: v for k, v in raw.items() if k not in _SKIP}
        except Exception:
            pass

        scratchpad = ""
        try:
            sp = self.run_dir / "scratchpad.md"
            if sp.exists():
                scratchpad = sp.read_text(encoding="utf-8")
        except Exception:
            pass

        recent: list[dict] = []
        try:
            stp = self.run_dir / "short_term.jsonl"
            if stp.exists():
                for line in stp.read_text(encoding="utf-8").splitlines()[-5:]:
                    try:
                        recent.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

        return {"meta": meta, "scratchpad": scratchpad, "recent_short_term": recent}

    def get_questions(self) -> list[dict]:
        with self._questions_lock:
            return list(self._questions)

    # ── POST 端点实现 ─────────────────────────────────────────────────────────

    def inject_message(self, message: str) -> None:
        """直接将 /inject 命令压入 interrupt_handler 的命令队列，避免文件 I/O 竞争。"""
        self.interrupt_handler._cmd_queue.put(f"/inject {message}")

    def add_question(self, question_id: str, worker_id: str, content: str) -> None:
        """主管侧：记录来自队员的问题，并注入提示让主管感知。"""
        from datetime import datetime, timezone
        entry = {
            "question_id": question_id,
            "worker_id": worker_id,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self._questions_lock:
            self._questions.append(entry)
        # 通知主管 LLM 有新问题（注入到主管的 short_term）
        self.inject_message(
            f"[队员 {worker_id} 提问，question_id={question_id}] {content}"
            f"\n请用 get_pending_questions 查看完整列表，用 answer_worker 回答。"
        )

    def remove_question(self, question_id: str) -> None:
        with self._questions_lock:
            self._questions = [q for q in self._questions if q["question_id"] != question_id]

    # ── 队员侧：等待主管回答（永久等待 + 存活检测）────────────────────────────

    def wait_for_answer(
        self,
        question_id: str,
        supervisor_url: str = "",
        check_interval: float = 0.5,
        ping_interval: float = 10.0,
        max_ping_failures: int = 6,
    ) -> str:
        """
        永久等待，直到收到与 question_id 匹配的回答。
        期间每 ping_interval 秒 ping 一次主管的 /agent/status。
        若连续 max_ping_failures 次失败，抛出 RuntimeError 供调用方决策。
        """
        import time
        import urllib.request

        last_ping = 0.0
        consecutive_fails = 0

        while True:
            # 检查答案队列
            try:
                item = self._answer_queue.get(timeout=check_interval)
                if item["question_id"] == question_id:
                    return item["answer"]
                # 不是给本问题的答案，放回队列（其他问题的配对）
                self._answer_queue.put(item)
            except queue.Empty:
                pass

            # 定期 ping 主管存活检测
            if supervisor_url:
                now = time.monotonic()
                if now - last_ping >= ping_interval:
                    last_ping = now
                    try:
                        urllib.request.urlopen(
                            f"{supervisor_url.rstrip('/')}/agent/status", timeout=3
                        ).read()
                        consecutive_fails = 0
                    except Exception:
                        consecutive_fails += 1
                        if consecutive_fails >= max_ping_failures:
                            raise RuntimeError(
                                f"主管 {supervisor_url} 疑似离线"
                                f"（连续 {max_ping_failures} 次 ping 失败）"
                            )

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        server = HTTPServer(("", self.port), _Handler)
        server.api = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="team-api"
        )
        self._thread.start()
        print(f"[team] Agent API 已启动，端口 {self.port}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
