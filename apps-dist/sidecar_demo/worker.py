"""受管 sidecar 示例 worker —— 平台管生命周期，你只写 handler。

协议（见 SKILLS/ui_app.md §4.5）：
  stdin  一行请求 {id, method, params, root}
  stdout 一行响应 {id, result} / {id, error}；不带 id 的行是主动事件 {event, data}
  stdout 是协议通道，只准 print 协议 JSON；日志走 stderr。

真实场景里这个进程持有的是相机 SDK 句柄、串口、加载好的模型 —— 那些"open 一次要一直活着"
的东西。本示例用一个内存计数器 + 一个后台任务替身来演示同样的生命周期。
"""

import json
import os
import sys
import threading
import time

STARTED_AT = time.time()

# —— 初始化只做一次，句柄常驻进程 ——
# 真实例子：import ctypes; sdk = ctypes.CDLL("qhyccd.dll"); sdk.OpenCamera(...)
STATE = {"counter": 0, "busy": False}


def emit(event, data=None):
    """主动推事件 → 面板 onPush 收到 {type:'sidecar-event', event, data}。"""
    print(json.dumps({"event": event, "data": data or {}}), flush=True)


def log(msg):
    """日志必须走 stderr；崩溃时平台会把尾部带给面板。"""
    print(f"[sidecar_demo] {msg}", file=sys.stderr, flush=True)


# ── handlers ────────────────────────────────────────────────────────────────

def ping(params):
    """最小 RPC：证明面板 ↔ worker 通了。"""
    return "pong"


def status(params):
    """进程内状态。counter 只活在内存里 —— 它能累加就说明进程一直没死。"""
    return {
        "pid": os.getpid(),
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "counter": STATE["counter"],
        "busy": STATE["busy"],
        "root": os.environ.get("QEVOS_ROOT", ""),
    }


def tick(params):
    """每调一次 +1。重启 worker 后归零 —— 这就是"常驻"与"per-call 脚本"的区别。"""
    STATE["counter"] += int(params.get("by", 1))
    return STATE["counter"]


def work(params):
    """长操作：丢线程、立即回 ack、完成后推事件。

    主循环阻塞期间所有 call（含状态查询）都会排队到超时，所以长活**必须**离开主循环。
    """
    if STATE["busy"]:
        raise RuntimeError("已经有一个任务在跑了")
    steps = max(1, min(int(params.get("steps", 5)), 50))
    root = params.get("root") or os.environ.get("QEVOS_ROOT", "")

    def run():
        STATE["busy"] = True
        try:
            lines = []
            for i in range(1, steps + 1):
                time.sleep(0.4)                       # 替身：真实场景是曝光/推理/读串口
                lines.append(f"step {i}/{steps} @ {time.strftime('%H:%M:%S')}")
                emit("progress", {"done": i, "total": steps})
            # 大件走文件通道，不经 RPC 返回：worker 落盘 → 面板 readFile/readBinary。
            # root 目录是平台在面板首次写文件时才建的，worker 直写要自己兜底。
            name = "sidecar_report.txt"
            if root:
                os.makedirs(root, exist_ok=True)
                with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                emit("done", {"file": name, "steps": steps})
            else:
                emit("done", {"file": "", "steps": steps})
        except Exception as e:                        # noqa: BLE001 — 线程里别把异常吞进虚空
            log(f"work failed: {e}")
            emit("failed", {"error": str(e)})
        finally:
            STATE["busy"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "steps": steps}


def boom(params):
    """故意崩给你看：面板会收到 {type:'sidecar-exit'}，下次 call 平台自动重启进程。"""
    log("boom() called — exiting on purpose")
    os._exit(3)


HANDLERS = {
    "ping": ping,
    "status": status,
    "tick": tick,
    "work": work,
    "boom": boom,
}


# ── 主循环：一行请求一行响应；异常回 error，进程不退出 ──────────────────────

log(f"started pid={os.getpid()} root={os.environ.get('QEVOS_ROOT', '')}")

for line in sys.stdin:
    mid = None
    try:
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        params.setdefault("root", msg.get("root") or "")
        out = {"id": mid, "result": HANDLERS[method](params)}
    except KeyError as e:
        out = {"id": mid, "error": f"unknown method: {e}"}
    except Exception as e:  # noqa: BLE001 — 任何 handler 异常都回给面板，不拖垮进程
        out = {"id": mid, "error": str(e)}
    print(json.dumps(out), flush=True)
