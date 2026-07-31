"""落盘产物索引 —— 一份不会被压缩弄丢的确定性文件清单。

大块内容在进入上下文之前会先落盘，路径以一条 feedback 消息的形式告知模型：
  - 工具输出超过 MAX_TOOL_FEEDBACK_CHARS → loop._spill_large_output_to_disk
  - watcher 注入超过 500 字符          → watcher._spill
  - 模型主动写文件                      → tools.standard.tool_write_file

问题在于那条消息是路径的唯一载体，而 compression._collapse_to_bridge 会把
short_term 硬重置成 [goal, handoff]。消息没了，文件还躺在磁盘上，agent 却
不再知道它存在 —— 落盘恰恰因"内容太大"而触发，丢掉的往往是最重的那批数据。

本模块把每次落盘登记进 state.meta["_artifact_index"]，压缩封段时由
render_manifest() 渲染成固定文本附到交接文档后面。清单每次都从 meta 重新
渲染，不经过 LLM 复述，因此不随压缩代数衰减。

meta 会被 RunPersistence.checkpoint 整体落盘，所以索引天然跨续跑存活。

依赖方向：（无内部依赖）← artifact_index ← loop / compression / standard
"""

import os
from pathlib import Path
from typing import Optional

from ..i18n import t


# 索引最多保留多少条（超出后淘汰）
_MAX_ENTRIES = int(os.environ.get("ARTIFACT_INDEX_MAX", "30"))
# 渲染出的清单文本硬上限，防止清单本身撑爆上下文
_MANIFEST_MAX_CHARS = int(os.environ.get("ARTIFACT_MANIFEST_MAX_CHARS", "1800"))

# 淘汰时优先牺牲的来源：溢出转储是可再生的中间态，
# write_file 是模型主动产出的交付物，尽量留到最后。
_EVICTABLE_SOURCES = frozenset({"spill", "watcher"})

_SOURCE_LABEL_KEYS = {
    "spill":      "artifact.src.spill",
    "watcher":    "artifact.src.watcher",
    "write_file": "artifact.src.write_file",
}


# ── 路径规范化 ────────────────────────────────────────────────────────────────

def _run_dir(state) -> str:
    """定位当前 run 目录（优先 persistence，其次环境变量）。"""
    persistence = getattr(state, "persistence", None) if state is not None else None
    if persistence is not None:
        rd = str(getattr(persistence, "run_dir", "") or "")
        if rd:
            return rd
    return os.environ.get("RUN_DIR", "") or ""


def _display_path(path: str, run_dir: str) -> str:
    """能相对 run 目录就用相对路径 —— 清单要短，且换机器仍可读。"""
    s = str(path or "").strip()
    if not s or not run_dir:
        return s
    try:
        rel = os.path.relpath(Path(s).resolve(), Path(run_dir).resolve())
    except Exception:
        return s
    # 逃出 run 目录的（../..）保持原样，相对路径反而更难读
    if rel.startswith(".."):
        return s
    return rel.replace(os.sep, "/")


# ── 登记 ──────────────────────────────────────────────────────────────────────

def register_artifact(
    state,
    path: str,
    source: str,
    tool: str = "",
    chars: int = 0,
    iter_n: Optional[int] = None,
) -> None:
    """登记一个已确实写入磁盘的产物。任何异常静默吞掉，绝不影响主流程。

    同一路径重复写入时就地更新（不产生重复条目），保持首次出现的位置，
    使清单读起来仍是一条时间线。
    """
    if state is None or not path:
        return
    try:
        run_dir = _run_dir(state)
        disp = _display_path(path, run_dir)
        if not disp:
            return

        index: list[dict] = state.meta.setdefault("_artifact_index", [])
        if not isinstance(index, list):
            index = []
            state.meta["_artifact_index"] = index

        if iter_n is None:
            iter_n = int(getattr(state, "iteration", 0) or 0)

        entry = {
            "path":   disp,
            "source": str(source or "?"),
            "tool":   str(tool or ""),
            "iter":   int(iter_n),
            "chars":  int(chars or 0),
        }

        for i, old in enumerate(index):
            if isinstance(old, dict) and old.get("path") == disp:
                index[i] = entry
                return

        index.append(entry)

        # 超限淘汰：先吃可再生的溢出转储，实在没有才动交付物
        while len(index) > _MAX_ENTRIES:
            victim = 0
            for i, e in enumerate(index):
                if isinstance(e, dict) and e.get("source") in _EVICTABLE_SOURCES:
                    victim = i
                    break
            index.pop(victim)
    except Exception:
        pass


def get_artifacts(state) -> list[dict]:
    """返回索引副本（调用方只读）。"""
    if state is None:
        return []
    index = state.meta.get("_artifact_index")
    if not isinstance(index, list):
        return []
    return [e for e in index if isinstance(e, dict) and e.get("path")]


# ── 渲染 ──────────────────────────────────────────────────────────────────────

def _format_entry(entry: dict) -> str:
    label = t(_SOURCE_LABEL_KEYS.get(entry.get("source", ""), "artifact.src.other"))
    tool = entry.get("tool") or ""
    if tool:
        label = f"{label}({tool})"
    bits = [label, f"iter{int(entry.get('iter', 0) or 0)}"]
    chars = int(entry.get("chars", 0) or 0)
    if chars > 0:
        bits.append(t("artifact.chars", n=chars))
    return f"- {entry['path']} — " + " · ".join(bits)


def render_manifest(state, max_chars: Optional[int] = None) -> str:
    """渲染确定性文件清单；索引为空时返回空串。

    超出字符上限时从**最旧**的条目开始丢弃，并注明省略数量 —— 新产物更可能
    与当前工作相关，且旧条目在更早的 handoff 里已经出现过。
    """
    entries = get_artifacts(state)
    if not entries:
        return ""

    cap = _MANIFEST_MAX_CHARS if max_chars is None else int(max_chars)
    header = t("artifact.manifest_header") + "\n" + t("artifact.manifest_hint")

    lines = [_format_entry(e) for e in entries]
    dropped = 0
    while lines:
        body = "\n".join(lines)
        note = ("\n" + t("artifact.manifest_omitted", n=dropped)) if dropped else ""
        out = f"{header}\n{body}{note}"
        if len(out) <= cap:
            return out
        lines.pop(0)
        dropped += 1

    return ""
