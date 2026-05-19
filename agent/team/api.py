"""
Agent 团队通信 HTTP 服务
========================
每个 Agent 启动时自动开启此服务并持续监听。
行为由拓扑节点码驱动——节点码为 null 时处于独立模式，设置后进入组网模式。

端点：
  GET  /agent/status      状态快照（读 status.json）
  GET  /agent/snapshot    完整快照：meta + scratchpad + 最近 5 条 short_term
  GET  /agent/questions   获取来自下游节点的待回答问题列表

  POST /agent/set_node    设置本节点的拓扑节点码（触发组网模式切换）
  POST /agent/inject      向本 Agent 注入一条消息
  POST /agent/task        接收上游分配的子任务（包装为 inject）
  POST /agent/question    接收下游节点提交的问题（含 question_id + 来源 URL）
  POST /agent/answer      接收上游对某问题的回答（按 question_id 配对）

纯 stdlib 实现，零额外依赖。
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


# ── 节点码解析 ────────────────────────────────────────────────────────────────

def parse_node_code(code: str) -> Optional[dict]:
    """
    解析拓扑节点码，返回结构化字典；null/空字符串返回 None（独立模式）。

    支持格式：
      "nodeRoot"                          → 顶层节点，无上游
      "nodeA ^ http://host:9100"          → 有上游（仅 URL）
      "nodeA ^ nodeRoot @ http://host:9100" → 有上游（含 ID）
    """
    code = (code or "").strip()
    if not code or code.lower() == "null":
        return None

    if "^" not in code:
        # 顶层节点，无上游
        return {"id": code.strip(), "upstream_id": "", "upstream_url": ""}

    self_part, upstream_part = code.split("^", 1)
    node_id = self_part.strip()
    upstream_part = upstream_part.strip()

    if "@" in upstream_part:
        up_id, up_url = upstream_part.split("@", 1)
        upstream_id = up_id.strip()
        upstream_url = up_url.strip()
    else:
        upstream_id = ""
        upstream_url = upstream_part

    return {
        "id": node_id,
        "upstream_id": upstream_id,
        "upstream_url": upstream_url.rstrip("/"),
    }


# ── HTTP 请求处理器 ───────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
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

        if path == "/agent/set_node":
            node_code = body.get("node_code", "")
            api.set_topology_node(node_code)
            self._send_json(200, {"ok": True, "topology_node": api.topology_node})

        elif path == "/agent/inject":
            message = body.get("message", "").strip()
            if not message:
                self._send_json(400, {"error": "message required"})
                return
            api.inject_message(message)
            self._send_json(200, {"ok": True})

        elif path == "/agent/task":
            task = body.get("task", "").strip()
            if not task:
                self._send_json(400, {"error": "task required"})
                return
            context = body.get("context", "").strip()
            msg = f"[新任务] {task}"
            if context:
                msg += f"\n[背景] {context}"
            api.inject_message(msg)
            self._send_json(200, {"ok": True})

        elif path == "/agent/question":
            qid = body.get("question_id", "").strip()
            content = body.get("content", "").strip()
            from_id = body.get("from_node_id", "unknown")
            from_url = body.get("from_node_url", "")
            if not (qid and content):
                self._send_json(400, {"error": "question_id and content required"})
                return
            api.add_question(qid, from_id, from_url, content)
            self._send_json(200, {"ok": True})

        elif path == "/agent/answer":
            qid = body.get("question_id", "").strip()
            answer = body.get("answer", "").strip()
            if not (qid and answer):
                self._send_json(400, {"error": "question_id and answer required"})
                return
            api._answer_queue.put({"question_id": qid, "answer": answer})
            self._send_json(200, {"ok": True})

        else:
            self._send_json(404, {"error": "not found"})


# ── TeamApiServer ─────────────────────────────────────────────────────────────

class TeamApiServer:
    """
    每个 Agent 实例的团队通信 HTTP 服务端。
    在后台守护线程运行，与主循环通过以下方式共享状态：
    - topology_node       拓扑节点信息（None = 独立模式）
    - inject_message()    直接写入 interrupt_handler._cmd_queue
    - wait_for_answer()   阻塞等待上游回答，带存活检测
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

        # 拓扑节点（None = 独立模式，dict = 组网模式）
        self.topology_node: Optional[dict] = None

        # 来自下游的待回答问题
        self._questions: list[dict] = []
        self._questions_lock = threading.Lock()

        # 来自上游的答案（按 question_id 配对）
        self._answer_queue: queue.Queue[dict] = queue.Queue()

        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── 拓扑节点管理 ──────────────────────────────────────────────────────────

    def set_topology_node(self, node_code: str) -> None:
        """解析并设置拓扑节点码，注入变更通知到 Agent 上下文。"""
        self.topology_node = parse_node_code(node_code)
        team_node_file = self.run_dir / "team_node.json"
        if self.topology_node:
            node_id = self.topology_node["id"]
            up_url  = self.topology_node.get("upstream_url", "")
            team_node_file.write_text(json.dumps({"id": node_id}), encoding="utf-8")
            if up_url:
                notice = (
                    f"[拓扑] 节点码已设置：我是 {node_id}，上游节点：{up_url}。"
                    "后续 ask_user 将自动路由到上游节点。"
                )
            else:
                notice = f"[拓扑] 节点码已设置：我是 {node_id}（顶层节点，无上游）。"
        else:
            team_node_file.unlink(missing_ok=True)
            notice = "[拓扑] 节点码已清除，恢复独立模式。"
        self.inject_message(notice)

    # ── GET 端点实现 ──────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        try:
            data = json.loads((self.run_dir / "status.json").read_text(encoding="utf-8"))
        except Exception:
            data = {"status": "unknown"}
        # 附加拓扑信息
        data["topology_node"] = self.topology_node
        return data

    def get_snapshot(self) -> dict:
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

        return {
            "topology_node": self.topology_node,
            "meta": meta,
            "scratchpad": scratchpad,
            "recent_short_term": recent,
        }

    def get_questions(self) -> list[dict]:
        with self._questions_lock:
            return list(self._questions)

    # ── POST 端点实现 ─────────────────────────────────────────────────────────

    def inject_message(self, message: str) -> None:
        """直接压入 interrupt_handler._cmd_queue，避免文件 I/O 竞争。"""
        self.interrupt_handler._cmd_queue.put(f"/inject {message}")

    def add_question(
        self,
        question_id: str,
        from_node_id: str,
        from_node_url: str,
        content: str,
    ) -> None:
        from datetime import datetime, timezone
        entry = {
            "question_id": question_id,
            "from_node_id": from_node_id,
            "from_node_url": from_node_url,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self._questions_lock:
            self._questions.append(entry)
        self.inject_message(
            f"[下游节点 {from_node_id} 提问，question_id={question_id}] {content}"
            "\n请用 get_pending_questions 查看，用 answer_downstream 回答。"
        )

    def remove_question(self, question_id: str) -> None:
        with self._questions_lock:
            self._questions = [
                q for q in self._questions if q["question_id"] != question_id
            ]

    # ── 等待上游回答（永久等待 + 存活检测）───────────────────────────────────

    def wait_for_answer(
        self,
        question_id: str,
        upstream_url: str = "",
        check_interval: float = 0.5,
        ping_interval: float = 10.0,
        max_ping_failures: int = 6,
    ) -> str:
        """
        永久等待，直到收到与 question_id 匹配的回答。
        每 ping_interval 秒 ping 一次上游 /agent/status。
        连续 max_ping_failures 次失败则抛 RuntimeError，由调用方决策（降级为 ask_user）。
        """
        import time
        import urllib.request

        last_ping = 0.0
        consecutive_fails = 0

        while True:
            try:
                item = self._answer_queue.get(timeout=check_interval)
                if item["question_id"] == question_id:
                    return item["answer"]
                self._answer_queue.put(item)  # 不是本问题的答案，放回
            except queue.Empty:
                pass

            if upstream_url:
                now = time.monotonic()
                if now - last_ping >= ping_interval:
                    last_ping = now
                    try:
                        urllib.request.urlopen(
                            f"{upstream_url}/agent/status", timeout=3
                        ).read()
                        consecutive_fails = 0
                    except Exception:
                        consecutive_fails += 1
                        if consecutive_fails >= max_ping_failures:
                            raise RuntimeError(
                                f"上游节点 {upstream_url} 疑似离线"
                                f"（连续 {max_ping_failures} 次 ping 失败）"
                            )

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        # 优先尝试指定端口；若被占用则让 OS 自动分配空闲端口
        for try_port in (self.port, 0):
            try:
                server = HTTPServer(("", try_port), _Handler)
                break
            except OSError:
                if try_port == 0:
                    print("[team] 警告：无法绑定任何端口，组网功能不可用。")
                    return
        actual_port = server.server_address[1]
        if actual_port != self.port:
            print(f"[team] 端口 {self.port} 已被占用，自动切换到空闲端口 {actual_port}。"
                  f"如需固定端口，请通过 TEAM_PORT 环境变量指定。")
            self.port = actual_port
        server.api = self
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="team-api"
        )
        self._thread.start()
        print(f"[team] Agent API 已启动，端口 {self.port}（独立模式，等待节点码）")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
