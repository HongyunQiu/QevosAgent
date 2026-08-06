"""时间账本与可注入时钟。

批 A：**纯计量，不改变任何行为**。终止条件仍由迭代预算与收敛检测负责，
本模块只负责把"时间花在哪了"如实记下来。时间接管终止是批 B 的事。

设计见 doc/execution-graph.md §7B。三条要点：

1. **分类而非单一数字**。只有分类了，"环境在退化"才成为可读信号——
   `llm` 占比从 40% 涨到 85%，agent 才可能得出"不是我慢、是服务端慢"。
   合成一个总数就什么都看不出来。

2. **累加式记账，不存绝对时间戳**。笔记本合盖、VM 挂起、跨进程续跑，
   绝对时间戳全会失真。唯一用到墙上时钟的地方是"暂停等人"——那段时间
   进程根本不在跑，只能靠续跑时对比上次落盘的时刻补记。

3. **时钟可注入**。时间相关的 bug 最难查，测试绝不允许真 sleep。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# 分类：五项互斥，加起来 ≤ total，差额是未归类开销（解析、记账、落盘等）
CATEGORIES = ("llm", "tool", "wait", "retry", "paused")

_META_KEY = "_time"


# ── 可注入时钟 ────────────────────────────────────────────────────────────────
# 用单调钟而非墙上钟：改系统时间、NTP 校正都不会让已记的账倒退。

_monotonic: Callable[[], float] = time.monotonic


def set_clock(fn: Callable[[], float]) -> Callable[[], float]:
    """替换单调钟（仅测试用），返回原来的以便还原。"""
    global _monotonic
    previous = _monotonic
    _monotonic = fn
    return previous


def reset_clock() -> None:
    global _monotonic
    _monotonic = time.monotonic


def now() -> float:
    return _monotonic()


def _wall_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_wall(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


# ── 账本 ──────────────────────────────────────────────────────────────────────

def ledger(state) -> dict:
    """取账本，不存在则建。挂在 state.meta["_time"] 单一子树下。"""
    book = state.meta.get(_META_KEY)
    if not isinstance(book, dict):
        book = {"total": 0.0}
        state.meta[_META_KEY] = book
    for key in CATEGORIES:
        if not isinstance(book.get(key), (int, float)):
            book[key] = 0.0
    if not isinstance(book.get("total"), (int, float)):
        book["total"] = 0.0
    return book


def peek(state) -> Optional[dict]:
    """只读地取账本；从未记过账时返回 None，无副作用。"""
    book = state.meta.get(_META_KEY)
    return book if isinstance(book, dict) else None


def start(state) -> None:
    """进程开始/恢复时调用：重置本进程的计时锚点。

    `_mark` 是进程内的单调钟读数，跨进程毫无意义——续跑时必须重打，
    否则会把两个进程的时钟差当成本次运行的耗时记进去。
    """
    book = ledger(state)
    book["_mark"] = now()
    book["_wall_seen"] = _wall_now()


def tick(state) -> float:
    """推进 total：把上次打点到现在这一段计入总耗时。每轮调用一次。

    total 是**实测**的，分类是**归因**的，差额自然就是未归类开销——
    这比强行让分类加满更诚实。
    """
    book = ledger(state)
    mark = book.get("_mark")
    current = now()
    if not isinstance(mark, (int, float)):
        book["_mark"] = current
        return 0.0
    delta = max(0.0, current - float(mark))
    book["total"] = float(book.get("total") or 0.0) + delta
    book["_mark"] = current
    book["_wall_seen"] = _wall_now()
    return delta


def add(state, category: str, seconds: float) -> None:
    """给某个分类记一笔。

    注意 `paused` 不要走这里——它必须同时记进 total 才能维持 `paused ≤ total`
    这个不变量（active = total - paused 否则会被夹到 0）。暂停一律走 absorb_pause。
    """
    if category not in CATEGORIES:
        return
    try:
        seconds = float(seconds)
    except Exception:
        return
    if seconds <= 0:
        return
    book = ledger(state)
    book[category] = float(book.get(category) or 0.0) + seconds


@contextmanager
def span(state, category: str):
    """给一段执行计时并归类。异常照常抛出，但时间照记——失败的调用同样花了时间。"""
    started = now()
    try:
        yield
    finally:
        try:
            add(state, category, now() - started)
        except Exception:
            pass


def absorb_pause(state) -> float:
    """续跑时补记"暂停等人"的时长。

    暂停期间进程根本不在跑，单调钟无从计量，只能靠对比上次落盘的墙上时刻。
    这是全模块唯一用到墙上时钟的地方，也只用于这一个目的。
    """
    book = ledger(state)
    seen = _parse_wall(book.get("_wall_seen"))
    if seen is None:
        return 0.0
    try:
        gap = (datetime.now(timezone.utc) - seen).total_seconds()
    except Exception:
        return 0.0
    if gap <= 0:
        return 0.0
    book["paused"] = float(book.get("paused") or 0.0) + gap
    book["total"] = float(book.get("total") or 0.0) + gap
    return gap


# ── 派生量 ────────────────────────────────────────────────────────────────────

def total_seconds(state) -> float:
    book = peek(state)
    return float((book or {}).get("total") or 0.0)


def active_seconds(state) -> float:
    """扣掉"等人"之后的工作时长。

    run 级时限用 total（对现实的承诺，挂起等人那段是真的消耗掉了），
    图级配额用 active（一份工作量额度，人去睡觉的 8 小时不该算）。
    """
    book = peek(state)
    if not book:
        return 0.0
    return max(0.0, float(book.get("total") or 0.0) - float(book.get("paused") or 0.0))


def snapshot(state) -> dict:
    """可 JSON 序列化的账本快照，供 status.json 与 dashboard 使用。"""
    book = peek(state)
    if not book:
        return {}
    out = {"total": round(float(book.get("total") or 0.0), 3)}
    for key in CATEGORIES:
        out[key] = round(float(book.get(key) or 0.0), 3)
    out["active"] = round(active_seconds(state), 3)
    tracked = sum(out[key] for key in CATEGORIES)
    out["untracked"] = round(max(0.0, out["total"] - tracked), 3)
    return out


def rate(state, iterations: int) -> dict:
    """平均每轮耗时与分解。批 C 的投影"速率"那一行用它。"""
    book = peek(state)
    if not book or iterations <= 0:
        return {}
    return {
        "per_iter": round(float(book.get("total") or 0.0) / iterations, 2),
        "llm_share": round(
            float(book.get("llm") or 0.0) / max(1e-9, float(book.get("total") or 0.0)), 3
        ),
    }


def fmt(seconds: float) -> str:
    """人类可读的时长：给模型和看板都用这一个，避免两处格式不一致。"""
    try:
        seconds = max(0.0, float(seconds))
    except Exception:
        return "0s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
