"""
观察器(Watcher)管理器

环境感知机制:一组注册过的、定时触发的用户代码,各自采集和处理某个环境切面,
框架按统一契约收集输出并注入 LLM 上下文。

设计要点:
- 纯注册机制:注册表(`<cwd>/.qevos/watchers.json`)是唯一真相,代码文件可在任意路径
- 文件本身只放代码,所有配置(interval/emit/enabled/params)都在注册表里
- 同一份代码可被多次注册,通过 params 实例化为不同 watcher
- 框架强制 500 字符注入硬顶,超限自动落 artifacts/ 降级为路径
- 用户代码异常被捕获,仅注入错误提示,不影响主循环
- 生命周期:绑定 state.meta["_watcher_manager"],随 AgentState 存在,不可序列化

执行约定:
- .py 文件:模块需定义 `run(prev, store, iter_n)` 函数
    - prev: 上次返回值(dict 或 None)
    - store: 可持久化字典,包含 'params'(注册表注入),代码可读写其他键
    - iter_n: 当前迭代号
    - 返回: None / {"type":"text","content":str} / {"type":"image","image_block":dict}
              / {"type":"path","path":str}
- .sh 文件:shell 脚本,stdout 当 text content,非零退出码视为失败
    - 环境变量:WATCHER_PARAMS_JSON, WATCHER_ITER, WATCHER_STORE_FILE
    - 脚本若想持久化状态可读写 WATCHER_STORE_FILE(JSON 格式)
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── 常量 ──────────────────────────────────────────────────────────────────────

# 单条注入消息的硬上限(含所有前缀)。超过则落盘 + 降级为路径注入。
INJECT_HARD_CAP = 500

# 落盘文件统一前缀
SPILL_PREFIX = "watch"


# ── 注册项数据类 ──────────────────────────────────────────────────────────────

@dataclass
class WatcherEntry:
    name:     str
    path:     str                     # 代码文件绝对路径
    interval: int = 10                # 触发间隔秒数(下界,实际由 poll 决定)
    emit:     str = "event"           # event | live
    enabled:  bool = True
    params:   dict = field(default_factory=dict)
    desc:     str = ""

    # 运行时状态(随注册表持久化)
    store:           dict   = field(default_factory=dict)
    last_run_time:   float  = 0.0     # wallclock 上次执行时间
    last_run_iter:   int    = -1      # 上次执行的 iteration
    last_result:     Any    = None    # 上次返回值(供下次 prev 用)
    error_streak:    int    = 0       # 连续异常次数(简单观测,暂未使用)

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "path":          self.path,
            "interval":      self.interval,
            "emit":          self.emit,
            "enabled":       self.enabled,
            "params":        self.params,
            "desc":          self.desc,
            "store":         self.store,
            "last_run_time": self.last_run_time,
            "last_run_iter": self.last_run_iter,
            "last_result":   self.last_result if _json_safe(self.last_result) else None,
            "error_streak":  self.error_streak,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatcherEntry":
        return cls(
            name=str(d.get("name", "")),
            path=str(d.get("path", "")),
            interval=int(d.get("interval", 10) or 10),
            emit=str(d.get("emit", "event") or "event"),
            enabled=bool(d.get("enabled", True)),
            params=dict(d.get("params", {}) or {}),
            desc=str(d.get("desc", "") or ""),
            store=dict(d.get("store", {}) or {}),
            last_run_time=float(d.get("last_run_time", 0) or 0),
            last_run_iter=int(d.get("last_run_iter", -1) if d.get("last_run_iter") is not None else -1),
            last_result=d.get("last_result"),
            error_streak=int(d.get("error_streak", 0) or 0),
        )


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
        return True
    except Exception:
        return False


# ── 主管理器 ──────────────────────────────────────────────────────────────────

class WatcherManager:
    """注册表 + 调度器 + 执行器,绑定在 state.meta['_watcher_manager']。"""

    def __init__(self, registry_path: Optional[Path] = None, artifacts_dir: Optional[Path] = None) -> None:
        if registry_path is None:
            registry_path = Path(os.environ.get("QEVOS_WATCHERS_REGISTRY") or ".qevos/watchers.json")
        self.registry_path = Path(registry_path).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir else None
        self._lock = threading.Lock()
        self._entries: dict[str, WatcherEntry] = {}
        # .py 模块缓存:path -> (mtime, module)
        self._module_cache: dict[str, tuple[float, Any]] = {}
        self.load()

    # ── 注册表持久化 ──────────────────────────────────────────────────────────

    def load(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            items = raw.get("watchers", []) if isinstance(raw, dict) else []
            with self._lock:
                self._entries = {
                    str(item.get("name", "")): WatcherEntry.from_dict(item)
                    for item in items
                    if item.get("name")
                }
        except Exception:
            # 损坏的注册表不应让 agent 无法启动,保持空注册表
            pass

    def save(self) -> None:
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "watchers": [e.to_dict() for e in self._entries.values()],
                }
            tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.registry_path)
        except Exception:
            pass  # 保存失败不阻塞运行

    # ── 注册管理(供 watch_* 工具调用)─────────────────────────────────────────

    def register(
        self,
        name:     str,
        path:     str,
        interval: int = 10,
        emit:     str = "event",
        params:   Optional[dict] = None,
        enabled:  bool = True,
        desc:     str = "",
    ) -> dict:
        if not name or not isinstance(name, str):
            return {"ok": False, "error": "name 必填"}
        abs_path = Path(path).expanduser().resolve()
        if not abs_path.exists():
            return {"ok": False, "error": f"代码文件不存在: {abs_path}"}
        if abs_path.suffix not in (".py", ".sh"):
            return {"ok": False, "error": f"仅支持 .py 或 .sh 文件,得到: {abs_path.suffix}"}
        if emit not in ("event", "live"):
            return {"ok": False, "error": f"emit 必须是 event 或 live,得到: {emit}"}

        entry = WatcherEntry(
            name=name,
            path=str(abs_path),
            interval=max(1, int(interval or 10)),
            emit=emit,
            enabled=bool(enabled),
            params=dict(params or {}),
            desc=str(desc or ""),
        )
        with self._lock:
            self._entries[name] = entry
        self.save()
        return {"ok": True, "name": name, "path": str(abs_path)}

    def unregister(self, name: str) -> dict:
        with self._lock:
            if name not in self._entries:
                return {"ok": False, "error": f"watcher '{name}' 不存在"}
            del self._entries[name]
        self.save()
        return {"ok": True, "name": name}

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return {"ok": False, "error": f"watcher '{name}' 不存在"}
            entry.enabled = bool(enabled)
        self.save()
        return {"ok": True, "name": name, "enabled": bool(enabled)}

    def update(self, name: str, **fields) -> dict:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return {"ok": False, "error": f"watcher '{name}' 不存在"}
            for k, v in fields.items():
                if v is None:
                    continue
                if k == "interval":
                    entry.interval = max(1, int(v))
                elif k == "emit":
                    if v not in ("event", "live"):
                        return {"ok": False, "error": "emit 必须是 event 或 live"}
                    entry.emit = v
                elif k == "params" and isinstance(v, dict):
                    entry.params = dict(v)
                elif k == "enabled":
                    entry.enabled = bool(v)
                elif k == "desc":
                    entry.desc = str(v)
                elif k == "path":
                    abs_path = Path(v).expanduser().resolve()
                    if not abs_path.exists():
                        return {"ok": False, "error": f"代码文件不存在: {abs_path}"}
                    entry.path = str(abs_path)
        self.save()
        return {"ok": True, "name": name}

    def list_entries(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name":     e.name,
                    "path":     e.path,
                    "interval": e.interval,
                    "emit":     e.emit,
                    "enabled":  e.enabled,
                    "desc":     e.desc,
                    "params":   e.params,
                    "last_run_iter": e.last_run_iter,
                    "error_streak":  e.error_streak,
                }
                for e in self._entries.values()
            ]

    # ── 调度 + 执行 ───────────────────────────────────────────────────────────

    def poll(self, iter_n: int) -> list[dict]:
        """
        遍历所有 enabled 项,到期则执行,返回待注入事件列表。
        事件格式:
          {"name": str, "emit": "event"|"live", "kind": "text"|"image"|"path"|"error",
           "content": str, "image_block": dict|None}
        """
        events: list[dict] = []
        now = time.time()
        # 拷贝避免持锁执行用户代码
        with self._lock:
            entries = list(self._entries.values())

        for entry in entries:
            if not entry.enabled:
                continue
            if (now - entry.last_run_time) < entry.interval:
                continue

            try:
                result = self._execute(entry, iter_n)
            except Exception as e:
                tb = traceback.format_exc(limit=2)
                events.append({
                    "name": entry.name,
                    "emit": entry.emit,
                    "kind": "error",
                    "content": f"[环境] watcher `{entry.name}` 执行异常: {type(e).__name__}: {e}",
                    "image_block": None,
                    "_traceback": tb,
                })
                entry.error_streak += 1
                entry.last_run_time = now
                entry.last_run_iter = iter_n
                continue

            entry.last_run_time = now
            entry.last_run_iter = iter_n
            entry.error_streak = 0

            if result is None:
                # 显式表示"本轮无内容",不投递
                continue

            entry.last_result = result if _json_safe(result) else None

            normalized = self._normalize_and_cap(entry, result, iter_n)
            if normalized is not None:
                events.append(normalized)

        # 任一项产生事件或更新了状态,保存注册表
        if events or any(e.last_run_time == now for e in entries):
            self.save()

        return events

    # ── 执行分发 ──────────────────────────────────────────────────────────────

    def _execute(self, entry: WatcherEntry, iter_n: int) -> Any:
        ext = Path(entry.path).suffix.lower()
        # 把 params 暴露给代码:store["params"] 为只读视图
        store_view = dict(entry.store)
        store_view["params"] = dict(entry.params)
        prev = entry.last_result

        if ext == ".py":
            module = self._load_py_module(entry.path)
            run_fn = getattr(module, "run", None)
            if not callable(run_fn):
                raise RuntimeError(f"{entry.path} 未定义 run(prev, store, iter_n) 函数")
            result = run_fn(prev, store_view, iter_n)
        elif ext == ".sh":
            result = self._execute_sh(entry, store_view, iter_n)
        else:
            raise RuntimeError(f"不支持的文件类型: {ext}")

        # 写回 store(剔除注入的 params 视图)
        store_view.pop("params", None)
        entry.store = store_view
        return result

    def _load_py_module(self, path: str) -> Any:
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            raise RuntimeError(f"无法读取 {path}: {e}")
        cached = self._module_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        # 每个 path 用唯一模块名,避免与其它 watcher 同名冲突
        mod_name = f"_qevos_watcher_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载模块: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        self._module_cache[path] = (mtime, module)
        return module

    def _execute_sh(self, entry: WatcherEntry, store_view: dict, iter_n: int) -> Any:
        # 给脚本一个临时 store 文件,允许它读写持久状态
        store_file = None
        if self.artifacts_dir:
            try:
                self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                store_file = self.artifacts_dir / f"_watcher_store_{entry.name}.json"
                # 写入"非 params 部分"作为脚本可见的 store
                writable = {k: v for k, v in store_view.items() if k != "params"}
                store_file.write_text(json.dumps(writable, ensure_ascii=False), encoding="utf-8")
            except Exception:
                store_file = None

        env = dict(os.environ)
        env["WATCHER_PARAMS_JSON"] = json.dumps(entry.params, ensure_ascii=False)
        env["WATCHER_ITER"] = str(iter_n)
        if store_file:
            env["WATCHER_STORE_FILE"] = str(store_file)

        try:
            proc = subprocess.run(
                entry.path if os.name != "nt" else ["bash", entry.path],
                shell=(os.name != "nt"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=max(5, entry.interval),  # 单次执行不超过 interval
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("shell watcher 执行超时")

        # 读回脚本可能更新的 store
        if store_file and store_file.exists():
            try:
                updated = json.loads(store_file.read_text(encoding="utf-8"))
                if isinstance(updated, dict):
                    for k, v in updated.items():
                        if k == "params":
                            continue
                        store_view[k] = v
            except Exception:
                pass

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            raise RuntimeError(f"shell watcher 退出码 {proc.returncode}: {err}")

        out = (proc.stdout or "").strip()
        if not out:
            return None
        return {"type": "text", "content": out}

    # ── 输出规范化 + 500 字符硬顶 + 溢出落盘 ──────────────────────────────────

    def _normalize_and_cap(self, entry: WatcherEntry, result: Any, iter_n: int) -> Optional[dict]:
        if not isinstance(result, dict):
            # 容错:非 dict 当 text 处理
            result = {"type": "text", "content": str(result)}
        kind = str(result.get("type", "text")).lower()

        if kind == "text":
            text = str(result.get("content", "") or "")
            if not text.strip():
                return None
            return self._wrap_text_event(entry, text, iter_n)

        if kind == "path":
            path = str(result.get("path", "") or "")
            if not path:
                return None
            content = self._format_path_injection(entry, path, hint="代码主动落盘")
            return {
                "name": entry.name,
                "emit": entry.emit,
                "kind": "path",
                "content": content,
                "image_block": None,
            }

        if kind == "image":
            image_block = result.get("image_block")
            if not isinstance(image_block, dict):
                return None
            return {
                "name": entry.name,
                "emit": entry.emit,
                "kind": "image",
                # 文字摘要,真正的图片由 live 面板渲染
                "content": f"[环境] watcher `{entry.name}` 输出图像 (iter={iter_n})",
                "image_block": image_block,
            }

        # 未知 type
        return None

    def _wrap_text_event(self, entry: WatcherEntry, text: str, iter_n: int) -> dict:
        header = f"[环境] {entry.name}: "
        candidate = header + text
        if len(candidate) <= INJECT_HARD_CAP:
            return {
                "name": entry.name,
                "emit": entry.emit,
                "kind": "text",
                "content": candidate,
                "image_block": None,
            }
        # 超 500 字符:落盘 + 降级为路径
        spill_path = self._spill(entry.name, iter_n, text)
        content = self._format_path_injection(entry, spill_path or "(落盘失败)",
                                              hint=f"{len(text)}字 已溢出")
        return {
            "name": entry.name,
            "emit": entry.emit,
            "kind": "path",
            "content": content,
            "image_block": None,
        }

    def _format_path_injection(self, entry: WatcherEntry, path: str, hint: str = "") -> str:
        # 一定要≤500;路径长极端情况下也截断
        body = f"[环境] {entry.name} [溢出] {hint} → {path}"
        if len(body) > INJECT_HARD_CAP:
            # 路径太长则保留尾部
            head = f"[环境] {entry.name} [溢出] {hint} → ...{path[-(INJECT_HARD_CAP - 80):]}"
            return head[:INJECT_HARD_CAP]
        return body

    def _spill(self, name: str, iter_n: int, text: str) -> Optional[str]:
        if not self.artifacts_dir:
            return None
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            safe = name.replace("/", "_").replace("\\", "_")
            target = self.artifacts_dir / f"{SPILL_PREFIX}_{safe}_iter{iter_n}.log"
            target.write_text(text, encoding="utf-8")
            return str(target)
        except Exception:
            return None
