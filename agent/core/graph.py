"""执行图（Execution Graph）

模型自主启用的**能力**：可在任务的任何时刻建图，也可随时放弃；简单任务永远不建图。
图不绑定工具集，工具仍由模型自主选择；图只显式化"方法/节点"与"沿着节点推进"这件事。

本模块只负责数据模型、状态机与上下文投影。收敛检测与迭代预算发放属于后续批次
（见 doc/execution-graph.md §7/§8），此处只把相应字段（budget/granted/visits）预留好。

单一真相源：内存挂 `state.meta["_graph"]` 单一子树，并镜像落盘到 `runs/<id>/graph.json`。
agent 读到的折叠投影与 dashboard 渲染的图形必须来自同一份数据。

节点是**逻辑边界，不是上下文边界**——默认不封段，避免每次节点切换等于 KV 缓存清零。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .types_def import AgentState
from ..i18n import t


# ── 常量 ──────────────────────────────────────────────────────────────────────

ROOT_ID = "n0"

_NODE_STATUS   = ("planned", "active", "done", "abandoned", "blocked")
_OPEN_STATUS   = ("planned", "active", "blocked")      # 未达终态 → 可并入 gaps
_GRAPH_STATUS  = ("active", "completed", "abandoned")
_EDGE_KINDS    = ("then", "alt", "fallback")
# 与验收门的证据分类保持一致（见 loop._parse_acceptance_evidence），不另起炉灶
_EVIDENCE_TYPES = ("artifact", "tool_result", "observation", "none")

# inline graph_op 每次只允许追加一个节点：跟着工具调用一起输出，塞一整棵树会撑爆
# 单次输出上限，正好撞上 _json_fail_streak 那套"args 过长 → 截断 → 解析失败"死循环。
_INLINE_OPS = ("enter", "exit", "extend", "fork", "abandon", "block", "complete")


def _projection_chars() -> int:
    """投影字符软上限（中文约 1.5 字符/token，1800 ≈ 600 token）。"""
    try:
        return max(400, int(os.environ.get("GRAPH_PROJECTION_CHARS", "1800")))
    except Exception:
        return 1800


# ── 运行环境 ──────────────────────────────────────────────────────────────────

def _iter(state: AgentState) -> int:
    try:
        return int(getattr(state, "iteration", 0) or 0)
    except Exception:
        return 0


def _run_dir(state: AgentState) -> str:
    persistence = getattr(state, "persistence", None)
    if persistence is not None:
        rd = str(getattr(persistence, "run_dir", "") or "")
        if rd:
            return rd
    return os.environ.get("RUN_DIR", "") or ""


def _get_persistence(state: AgentState):
    persistence = getattr(state, "persistence", None)
    if persistence is not None:
        return persistence
    try:
        from ..runtime.persistence import RunPersistence

        run_dir = os.environ.get("RUN_DIR")
        if not run_dir:
            return None
        persistence = RunPersistence(run_dir)
        state.persistence = persistence
        return persistence
    except Exception:
        return None


def save(state: AgentState) -> None:
    """把图镜像落盘到 graph.json（dashboard 的数据源）。失败不影响运行。"""
    persistence = _get_persistence(state)
    if persistence is None:
        return
    fn = getattr(persistence, "save_graph", None)
    if fn is None:
        return
    try:
        fn(get_root(state))
    except Exception:
        pass


# ── 文本工具 ──────────────────────────────────────────────────────────────────

def _clip(value: Any, limit: int) -> str:
    s = str(value or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def _str_list(value: Any, limit: int = 20, item_chars: int = 300) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        s = _clip(item, item_chars)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ── 数据访问 ──────────────────────────────────────────────────────────────────

def get_root(state: AgentState) -> dict:
    root = state.meta.get("_graph")
    if not isinstance(root, dict):
        root = {"version": 1, "budget_granted": 0, "graphs": []}
        state.meta["_graph"] = root
    if not isinstance(root.get("graphs"), list):
        root["graphs"] = []
    if not isinstance(root.get("budget_granted"), int):
        root["budget_granted"] = 0
    return root


def active_graph(state: AgentState) -> Optional[dict]:
    for g in get_root(state).get("graphs", []):
        if isinstance(g, dict) and g.get("status") == "active":
            return g
    return None


def _nodes(g: dict) -> dict:
    nodes = g.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
        g["nodes"] = nodes
    return nodes


def _active_node(g: dict) -> Optional[dict]:
    for node in _nodes(g).values():
        if node.get("status") == "active":
            return node
    return None


def _children(g: dict, node_id: str) -> list[dict]:
    return [n for n in _nodes(g).values() if n.get("parent") == node_id]


def _ancestors(g: dict, node_id: str) -> list[dict]:
    """从根到 node_id 的父链（不含自身）。带环保护。"""
    chain: list[dict] = []
    seen: set[str] = {node_id}
    cur = _nodes(g).get(node_id, {}).get("parent")
    while cur and cur not in seen:
        node = _nodes(g).get(cur)
        if node is None:
            break
        chain.append(node)
        seen.add(cur)
        cur = node.get("parent")
    chain.reverse()
    return chain


def _descendants(g: dict, node_id: str) -> list[dict]:
    out: list[dict] = []
    frontier = [node_id]
    seen: set[str] = {node_id}
    while frontier:
        cur = frontier.pop()
        for child in _children(g, cur):
            cid = child.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            out.append(child)
            frontier.append(cid)
    return out


# ── 规范化 ────────────────────────────────────────────────────────────────────

def _normalize_exit(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    expect = _str_list(raw.get("expect"), limit=12)
    evidence_type = str(raw.get("evidence_type") or "").strip().lower()
    if evidence_type not in _EVIDENCE_TYPES:
        # 未申报时按 expect 推断：给了产物路径就是 artifact（可实证），否则自证。
        evidence_type = "artifact" if expect else "observation"
    return {"evidence_type": evidence_type, "expect": expect}


def _normalize_node(raw: Any, node_id: str, parent: Optional[str] = None) -> dict:
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        raw = {}

    try:
        budget = int(raw.get("budget") or 0)
    except Exception:
        budget = 0
    budget = max(0, min(budget, 200))

    status = str(raw.get("status") or "planned").strip().lower()
    if status not in _NODE_STATUS:
        status = "planned"

    title = _clip(raw.get("title"), 40) or _clip(raw.get("goal"), 40) or node_id

    return {
        "id": node_id,
        "title": title,
        "goal": _clip(raw.get("goal"), 600),
        "status": status,
        "parent": parent,
        "exit": _normalize_exit(raw.get("exit")),
        "budget": budget,
        "granted": False,          # 预算发放（批 2）
        "iter_range": [None, None],
        "seg": None,               # 仅 isolate 封段时记录
        "isolate": bool(raw.get("isolate")),
        "outcome": {"summary": "", "gaps": []},
        "side_effects": [],
        "abandon_reason": "",
        "closed_by": "",
        "visits": 0,
    }


def _free_node_id(used: set[str]) -> str:
    i = 1
    while f"n{i}" in used:
        i += 1
    return f"n{i}"


def _next_node_id(g: dict) -> str:
    return _free_node_id(set(_nodes(g).keys()))


def _merge_side_effects(node: dict, items: Any) -> None:
    existing = node.get("side_effects")
    if not isinstance(existing, list):
        existing = []
        node["side_effects"] = existing
    for item in _str_list(items, limit=20):
        if item not in existing:
            existing.append(item)


def _add_edge(g: dict, src: str, dst: str, kind: str) -> None:
    edges = g.get("edges")
    if not isinstance(edges, list):
        edges = []
        g["edges"] = edges
    if kind not in _EDGE_KINDS:
        kind = "then"
    for e in edges:
        if isinstance(e, dict) and e.get("from") == src and e.get("to") == dst:
            return
    edges.append({"from": src, "to": dst, "kind": kind, "cond": ""})


# ── 建图 ──────────────────────────────────────────────────────────────────────

def _seed_summary(state: AgentState) -> str:
    """根节点 n0「前序工作」的摘要：取自最近 handoff 或草稿本，零 LLM 调用。"""
    handoff = str(state.meta.get("_last_handoff") or "").strip()
    if handoff:
        return _clip(handoff, 400)
    scratchpad = str(state.meta.get("scratchpad") or "").strip()
    if scratchpad:
        return _clip(scratchpad[-600:], 400)
    return ""


def create_graph(
    state: AgentState,
    title: str,
    nodes: Any,
    edges: Any = None,
    reason: str = "",
    from_skill: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """建立一张新图。返回 (graph, 说明文字)；nodes 为空时返回 (None, 错误说明)。"""
    raw_nodes = nodes
    if isinstance(raw_nodes, dict):
        raw_nodes = [raw_nodes]
    if not isinstance(raw_nodes, (list, tuple)) or not raw_nodes:
        return None, t("graph.tool.no_nodes")

    root = get_root(state)
    notes: list[str] = []

    # 同时只有一张 active 图：旧图自动废弃（串行多图，见 doc §2c）
    previous = active_graph(state)
    if previous is not None:
        previous["status"] = "abandoned"
        previous["closed_iter"] = _iter(state)
        previous["closed_reason"] = t("graph.tool.replaced")
        notes.append(t("graph.tool.replace_active", gid=previous.get("gid", "?")))

    gid = f"g{len(root['graphs']) + 1}"
    g: dict = {
        "gid": gid,
        "status": "active",
        "title": _clip(title, 60) or gid,
        "created_iter": _iter(state),
        "closed_iter": None,
        "closed_reason": "",
        "created_reason": _clip(reason, 300),
        "from_skill": _clip(from_skill, 60) or None,
        "cursor": ROOT_ID,
        "nodes": {},
        "edges": [],
    }

    # 根节点：建图之前的历史必须在图上有落点，否则地图无源之始（doc §2a）
    seed = _normalize_node(
        {"title": t("graph.root.title"), "goal": _seed_summary(state)},
        ROOT_ID,
        parent=None,
    )
    seed["status"] = "done"
    seed["closed_by"] = "implicit"
    seed["iter_range"] = [0, _iter(state)]
    g["nodes"][ROOT_ID] = seed

    # 分配 id（模型给的 id 若合法且不冲突就沿用，便于它在 edges 里引用）
    assigned: list[tuple[str, Any]] = []
    used: set[str] = {ROOT_ID}
    for raw in raw_nodes:
        wanted = ""
        if isinstance(raw, dict):
            wanted = str(raw.get("id") or "").strip()
        node_id = wanted if (wanted and wanted not in used) else _free_node_id(used)
        used.add(node_id)
        assigned.append((node_id, raw))

    # 边：模型给了就用；没给则按给出顺序串成一条链（最常见的用法）
    parent_of: dict[str, str] = {}
    explicit: list[tuple[str, str, str]] = []
    dropped: list[str] = []
    edges_given = isinstance(edges, (list, tuple)) and bool(edges)
    if edges_given:
        valid_ids = used
        for e in edges:
            if not isinstance(e, dict):
                continue
            src = str(e.get("from") or "").strip()
            dst = str(e.get("to") or "").strip()
            if src not in valid_ids or dst not in valid_ids or src == dst:
                dropped.append(f"{src or '?'}→{dst or '?'}")
                continue
            kind = str(e.get("kind") or "then").strip().lower()
            explicit.append((src, dst, kind))
            parent_of.setdefault(dst, src)
    else:
        prev = ROOT_ID
        for node_id, _raw in assigned:
            explicit.append((prev, node_id, "then"))
            parent_of.setdefault(node_id, prev)
            prev = node_id

    # 无入边的节点：按**给出顺序**接到前一个节点之后，而不是一律挂到根上。
    # nodes 的顺序就是模型自己的排序，据此补链远比"扔到根下"接近它的本意
    # （模型漏写最后一条边是常见失误，挂根会让末节点看起来与整条链无关）。
    # 并且必须同时补边——只设 parent 不补边会让 parent 与 edges 各说各话，
    # 渲染层就会画出一条 edges 里根本不存在的线。
    orphans: list[str] = []
    prev_id = ROOT_ID
    for node_id, _raw in assigned:
        if node_id not in parent_of:
            parent_of[node_id] = prev_id
            explicit.append((prev_id, node_id, "then"))
            if edges_given:
                orphans.append(f"{node_id}（已接到 {prev_id} 之后）")
        prev_id = node_id

    for node_id, raw in assigned:
        g["nodes"][node_id] = _normalize_node(raw, node_id, parent=parent_of[node_id])

    for src, dst, kind in explicit:
        _add_edge(g, src, dst, kind)

    # 结构没按模型预期落地时必须让它知道——静默纠正会让它以为图就是它画的那样
    if orphans:
        notes.append(t("graph.tool.orphans", ids="; ".join(orphans)))
    if dropped:
        notes.append(t("graph.tool.dropped_edges", edges="; ".join(dropped[:8])))

    root["graphs"].append(g)
    save(state)

    msg = t(
        "graph.tool.created",
        gid=gid,
        title=g["title"],
        n=len(g["nodes"]) - 1,
    )
    if notes:
        msg = msg + "\n" + "\n".join(notes)
    return g, msg


def abandon_graph(state: AgentState, reason: str) -> tuple[bool, str]:
    g = active_graph(state)
    if g is None:
        return False, t("graph.op.no_graph")
    g["status"] = "abandoned"
    g["closed_iter"] = _iter(state)
    g["closed_reason"] = _clip(reason, 300)
    save(state)
    return True, t("graph.tool.abandoned", gid=g.get("gid", "?"), reason=_clip(reason, 200))


# ── 出口证据校验 ──────────────────────────────────────────────────────────────

def _path_exists(path: str, state: AgentState) -> bool:
    """产物存在性检查：依次按 原样 / run_dir 相对 / cwd 相对 解析。"""
    s = str(path or "").strip().strip("`\"'")
    if not s:
        return False
    if s.startswith("./"):
        s = s[2:]
    candidates = [s]
    run_dir = _run_dir(state)
    if run_dir:
        candidates.append(os.path.join(run_dir, s))
    candidates.append(os.path.join(os.getcwd(), s))
    for candidate in candidates:
        try:
            if os.path.exists(candidate):
                return True
        except Exception:
            continue
    return False


# ── graph_op 应用 ─────────────────────────────────────────────────────────────

def apply_op(state: AgentState, op: Any) -> tuple[bool, str]:
    """应用一个 graph_op。返回 (是否成功, 给模型看的说明)。

    调用方保证异常安全：任何内部错误都被兜住，绝不让图的 bug 打断主循环。
    """
    try:
        return _apply_op(state, op)
    except Exception as e:  # pragma: no cover - 防御性
        return False, t("graph.op.internal_error", err=f"{type(e).__name__}: {e}")


def _apply_op(state: AgentState, op: Any) -> tuple[bool, str]:
    if not isinstance(op, dict):
        return False, t("graph.op.unknown", op=str(op)[:60])

    kind = str(op.get("op") or "").strip().lower()
    if kind not in _INLINE_OPS:
        return False, t("graph.op.unknown", op=kind or "(empty)")

    g = active_graph(state)
    if g is None:
        return False, t("graph.op.no_graph")

    if kind == "enter":
        return _op_enter(state, g, op)
    if kind == "exit":
        return _op_exit(state, g, op)
    if kind in ("extend", "fork"):
        return _op_add(state, g, op, kind)
    if kind == "abandon":
        return _op_abandon(state, g, op)
    if kind == "complete":
        return _op_complete(state, g, op)
    return _op_block(state, g, op)


def _require_node(g: dict, node_id: str) -> tuple[Optional[dict], str]:
    node = _nodes(g).get(node_id)
    if node is None:
        return None, t("graph.op.node_missing", id=node_id or "(empty)")
    return node, ""


def _op_enter(state: AgentState, g: dict, op: dict) -> tuple[bool, str]:
    node_id = str(op.get("node") or "").strip()
    node, err = _require_node(g, node_id)
    if node is None:
        return False, err
    if node.get("status") == "abandoned":
        return False, t("graph.op.enter_abandoned", id=node_id)

    current = _active_node(g)
    if current is not None and current.get("id") != node_id:
        return False, t("graph.op.busy", id=current.get("id", "?"))

    node["status"] = "active"
    node["visits"] = int(node.get("visits") or 0) + 1
    if not isinstance(node.get("iter_range"), list) or len(node["iter_range"]) != 2:
        node["iter_range"] = [None, None]
    if node["iter_range"][0] is None:
        node["iter_range"][0] = _iter(state)
    node["iter_range"][1] = None
    g["cursor"] = node_id
    save(state)
    return True, t("graph.op.entered", id=node_id, title=node.get("title", ""))


def _op_exit(state: AgentState, g: dict, op: dict) -> tuple[bool, str]:
    node_id = str(op.get("node") or "").strip() or str(g.get("cursor") or "")
    node, err = _require_node(g, node_id)
    if node is None:
        return False, err
    if node.get("status") != "active":
        return False, t("graph.op.not_active", id=node_id, status=node.get("status", "?"))

    # 残留台账是事实，无论出口是否通过都要记（doc §6）
    _merge_side_effects(node, op.get("side_effects"))

    exit_spec = node.get("exit") or {}
    evidence_type = exit_spec.get("evidence_type", "observation")
    expect = exit_spec.get("expect") or []

    if evidence_type == "artifact" and expect:
        missing = [p for p in expect if not _path_exists(p, state)]
        if missing:
            save(state)
            return False, t("graph.op.exit_missing_artifact", missing=", ".join(missing))
        closed_by = "evidence_verified"
    else:
        closed_by = "self_certified"

    node["status"] = "done"
    node["closed_by"] = closed_by
    node["iter_range"][1] = _iter(state)
    node["outcome"] = {
        "summary": _clip(op.get("summary"), 600),
        "gaps": _str_list(op.get("gaps"), limit=10),
    }
    g["cursor"] = node_id

    msg = t("graph.op.exited", id=node_id, closed_by=t(f"graph.closed_by.{closed_by}"))
    save(state)
    return True, msg


def _op_add(state: AgentState, g: dict, op: dict, kind: str) -> tuple[bool, str]:
    if kind == "extend":
        parent_id = str(op.get("after") or "").strip() or str(g.get("cursor") or ROOT_ID)
        edge_kind = str(op.get("kind") or "then").strip().lower()
    else:
        parent_id = str(op.get("from") or "").strip() or str(g.get("cursor") or ROOT_ID)
        edge_kind = "alt"

    parent, err = _require_node(g, parent_id)
    if parent is None:
        return False, err

    raw = op.get("node")
    if isinstance(raw, (list, tuple)):
        # inline 每次只允许一个节点，多节点必须走 plan_revise（doc §4）
        return False, t("graph.op.single_node_only")

    node_id = _next_node_id(g)
    node = _normalize_node(raw, node_id, parent=parent_id)
    if not node.get("goal") and not node.get("title"):
        return False, t("graph.op.bad_node", why=t("graph.op.bad_node_empty"))

    _nodes(g)[node_id] = node
    _add_edge(g, parent_id, node_id, edge_kind)
    save(state)
    return True, t(
        "graph.op.added",
        id=node_id,
        title=node.get("title", ""),
        parent=parent_id,
        kind=edge_kind,
    )


def _op_abandon(state: AgentState, g: dict, op: dict) -> tuple[bool, str]:
    node_id = str(op.get("node") or "").strip() or str(g.get("cursor") or "")
    node, err = _require_node(g, node_id)
    if node is None:
        return False, err
    if node_id == ROOT_ID:
        return False, t("graph.op.root_immutable")

    reason = _clip(op.get("reason"), 300)
    _merge_side_effects(node, op.get("side_effects"))
    node["status"] = "abandoned"
    node["abandon_reason"] = reason
    if isinstance(node.get("iter_range"), list) and len(node["iter_range"]) == 2:
        node["iter_range"][1] = _iter(state)

    # 级联：下游还没开始的节点一起废弃，避免留下孤儿
    cascaded: list[str] = []
    for child in _descendants(g, node_id):
        if child.get("status") == "planned":
            child["status"] = "abandoned"
            child["abandon_reason"] = t("graph.op.cascade_reason", id=node_id)
            cascaded.append(child.get("id", "?"))

    msg = t("graph.op.abandoned", id=node_id, reason=reason or "-")
    if cascaded:
        msg = msg + " " + t("graph.op.cascade", ids=", ".join(cascaded))
    save(state)
    return True, msg


def _op_block(state: AgentState, g: dict, op: dict) -> tuple[bool, str]:
    node_id = str(op.get("node") or "").strip() or str(g.get("cursor") or "")
    node, err = _require_node(g, node_id)
    if node is None:
        return False, err
    if node_id == ROOT_ID:
        return False, t("graph.op.root_immutable")

    reason = _clip(op.get("reason"), 300)
    _merge_side_effects(node, op.get("side_effects"))
    node["status"] = "blocked"
    outcome = node.get("outcome")
    if not isinstance(outcome, dict):
        outcome = {"summary": "", "gaps": []}
        node["outcome"] = outcome
    outcome["summary"] = reason
    save(state)
    return True, t("graph.op.blocked", id=node_id, reason=reason or "-")


def _op_complete(state: AgentState, g: dict, op: dict) -> tuple[bool, str]:
    """由模型显式声明图已走完。

    刻意**不做**"所有节点达终态就自动完成"：局部前向规划下，走到已规划节点的
    末端是常态而非终点——模型通常正要 extend 下一段。自动完成会把图关掉，
    紧接着的 extend 就会撞上"没有活动的图"。完成必须是一次显式声明。
    """
    open_nodes_left = [n.get("id", "?") for n in _nodes(g).values() if n.get("status") in _OPEN_STATUS]
    if open_nodes_left:
        return False, t("graph.op.complete_pending", ids=", ".join(sorted(open_nodes_left)))
    g["status"] = "completed"
    g["closed_iter"] = _iter(state)
    g["closed_reason"] = _clip(op.get("reason"), 300)
    save(state)
    return True, t("graph.op.graph_completed", gid=g.get("gid", "?"))


# ── 结构性修订（plan_revise 专用，付费操作）───────────────────────────────────

def apply_revision(state: AgentState, ops: Any) -> tuple[int, int, list[str]]:
    """批量应用结构性操作。返回 (成功数, 失败数, 逐条说明)。"""
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, (list, tuple)):
        return 0, 0, []

    ok_count = 0
    fail_count = 0
    details: list[str] = []
    for op in ops[:40]:
        kind = str(op.get("op") or "").strip().lower() if isinstance(op, dict) else ""
        if kind == "update":
            ok, msg = _op_update(state, op)
        else:
            ok, msg = apply_op(state, op)
        details.append(("✓ " if ok else "✗ ") + msg)
        if ok:
            ok_count += 1
        else:
            fail_count += 1
    return ok_count, fail_count, details


def _op_update(state: AgentState, op: dict) -> tuple[bool, str]:
    g = active_graph(state)
    if g is None:
        return False, t("graph.op.no_graph")
    node_id = str(op.get("node") or "").strip()
    node, err = _require_node(g, node_id)
    if node is None:
        return False, err
    if node.get("status") in ("done", "abandoned"):
        return False, t("graph.op.update_terminal", id=node_id, status=node.get("status", "?"))

    changed: list[str] = []
    if op.get("title"):
        node["title"] = _clip(op.get("title"), 40)
        changed.append("title")
    if op.get("goal"):
        node["goal"] = _clip(op.get("goal"), 600)
        changed.append("goal")
    if isinstance(op.get("exit"), dict):
        node["exit"] = _normalize_exit(op.get("exit"))
        changed.append("exit")
    if op.get("budget") is not None:
        try:
            node["budget"] = max(0, min(int(op.get("budget")), 200))
            changed.append("budget")
        except Exception:
            pass

    if not changed:
        return False, t("graph.op.update_noop", id=node_id)
    save(state)
    return True, t("graph.op.updated", id=node_id, fields=", ".join(changed))


# ── 上下文投影 ────────────────────────────────────────────────────────────────

_STATUS_MARK = {
    "done": "✓",
    "abandoned": "✗",
    "blocked": "⏸",
    "active": "▶",
    "planned": "·",
}


def _node_line(node: dict, *, with_reason: bool = False) -> str:
    mark = _STATUS_MARK.get(node.get("status", "planned"), "·")
    line = f"{node.get('id', '?')} {mark} 「{node.get('title', '')}」"
    if with_reason and node.get("status") == "abandoned" and node.get("abandon_reason"):
        line += f" — {_clip(node['abandon_reason'], 120)}"
    if node.get("status") == "blocked":
        reason = (node.get("outcome") or {}).get("summary") or ""
        if reason:
            line += f" — {_clip(reason, 120)}"
    return line


def render(state: AgentState) -> str:
    """渲染注入上下文尾部的折叠投影。任何异常都降级为空串，绝不影响运行。"""
    try:
        return _render(state)
    except Exception:
        return ""


def _render(state: AgentState) -> str:
    root = state.meta.get("_graph")
    if not isinstance(root, dict) or not root.get("graphs"):
        return ""  # 从未建图 → 零成本

    g = active_graph(state)
    if g is None:
        # 非 active 图收缩为一行（doc §2b），保留"曾经用过图"这个事实
        last = root["graphs"][-1]
        key = "graph.proj.completed_line" if last.get("status") == "completed" else "graph.proj.abandoned_line"
        return t(key, gid=last.get("gid", "?"), title=last.get("title", ""))

    nodes = _nodes(g)
    total = max(0, len(nodes) - 1)               # 不计根节点
    done = sum(1 for n in nodes.values() if n.get("status") == "done" and n.get("id") != ROOT_ID)

    sections: list[str] = [
        t("graph.proj.header", gid=g.get("gid", "?"), title=g.get("title", ""), done=done, total=total)
    ]

    current = _active_node(g)

    # ── 当前节点（完整）────────────────────────────────────────────────────
    if current is not None:
        rng = current.get("iter_range") or [None, None]
        entered = rng[0] if rng[0] is not None else _iter(state)
        used = max(0, _iter(state) - int(entered))
        sections.append(
            t(
                "graph.proj.current",
                id=current.get("id", "?"),
                title=current.get("title", ""),
                entered=entered,
                used=used,
                budget=current.get("budget") or "-",
            )
        )
        if current.get("goal"):
            sections.append(t("graph.proj.goal", goal=current["goal"]))
        exit_spec = current.get("exit") or {}
        sections.append(
            t(
                "graph.proj.exit",
                etype=exit_spec.get("evidence_type", "observation"),
                expect=", ".join(exit_spec.get("expect") or []) or "-",
            )
        )
    else:
        frontier = [n for n in nodes.values() if n.get("status") == "planned"]
        frontier.sort(key=lambda n: n.get("id", ""))
        if frontier:
            sections.append(
                t("graph.proj.no_active", frontier="; ".join(_node_line(n) for n in frontier[:5]))
            )
        else:
            sections.append(t("graph.proj.no_active_empty"))

    # ── 路径（祖先链）──────────────────────────────────────────────────────
    anchor_id = (current or {}).get("id") or str(g.get("cursor") or ROOT_ID)
    chain = _ancestors(g, anchor_id)
    if chain:
        sections.append(
            t("graph.proj.path", chain=" → ".join(_node_line(n) for n in chain[-6:]))
        )

    # ── 同层备选（含已废弃的兄弟）──────────────────────────────────────────
    anchor = nodes.get(anchor_id) or {}
    parent_id = anchor.get("parent")
    siblings = [n for n in _children(g, parent_id) if n.get("id") != anchor_id] if parent_id else []
    if siblings:
        sections.append(
            t("graph.proj.siblings", items="\n".join(f"  - {_node_line(n, with_reason=True)}" for n in siblings[:6]))
        )

    # ── 环境残留（废弃分支留下的改动）──────────────────────────────────────
    residue: list[str] = []
    for node in nodes.values():
        if node.get("status") != "abandoned":
            continue
        for item in node.get("side_effects") or []:
            residue.append(f"  - [{node.get('id')}] {item}")
    if residue:
        sections.append(t("graph.proj.residue", items="\n".join(residue[:10])))

    # ── 前方 planned ───────────────────────────────────────────────────────
    upcoming = [n for n in nodes.values() if n.get("status") == "planned"]
    upcoming.sort(key=lambda n: n.get("id", ""))
    if upcoming and current is not None:
        sections.append(
            t("graph.proj.next", items="\n".join(f"  - {_node_line(n)}" for n in upcoming[:5]))
        )

    # ── 已废弃分支（折叠）──────────────────────────────────────────────────
    shown = {n.get("id") for n in siblings}
    folded: list[str] = []
    for node in nodes.values():
        if node.get("status") != "abandoned" or node.get("id") in shown:
            continue
        if node.get("parent") in {n.get("id") for n in nodes.values() if n.get("status") == "abandoned"}:
            continue  # 只报子树根，后代折进计数
        subtree = [d for d in _descendants(g, node.get("id")) if d.get("status") == "abandoned"]
        folded.append(
            "  - " + t(
                "graph.proj.folded",
                id=node.get("id", "?"),
                n=len(subtree) + 1,
                reason=_clip(node.get("abandon_reason"), 120) or "-",
            )
        )
    if folded:
        sections.append(t("graph.proj.abandoned", items="\n".join(folded[:6])))

    sections.append(t("graph.proj.protocol"))

    text = "\n".join(s for s in sections if s)
    limit = _projection_chars()
    if len(text) > limit:
        text = text[:limit] + "\n" + t("graph.proj.truncated")
    return text


# ── 对外查询（loop / dashboard / 后续批次用）──────────────────────────────────

def summary(state: AgentState) -> dict:
    """图的状态快照，供 checkpoint 与 dashboard 顶部条使用。"""
    try:
        root = state.meta.get("_graph")
        if not isinstance(root, dict) or not root.get("graphs"):
            return {}
        g = active_graph(state) or root["graphs"][-1]
        nodes = _nodes(g)
        current = _active_node(g)
        return {
            "gid": g.get("gid", ""),
            "status": g.get("status", ""),
            "title": g.get("title", ""),
            "total": max(0, len(nodes) - 1),
            "done": sum(1 for n in nodes.values() if n.get("status") == "done" and n.get("id") != ROOT_ID),
            "open": sum(1 for n in nodes.values() if n.get("status") in _OPEN_STATUS),
            "active_node": (current or {}).get("id", ""),
            "budget_granted": int(root.get("budget_granted") or 0),
        }
    except Exception:
        return {}


# ── 收敛检测 ──────────────────────────────────────────────────────────────────
# 现有防呆全是签名式的（同工具同参数重复、逐字相同输出、连续格式错误），抓不住
# 最贵的那类失败：每轮都在调不同工具、写不同文件，四十轮下来一个节点都没关掉。
# 下面四个指标全是 graph.json 上的算术，零 LLM 调用。

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default


def _closure_order(g: dict) -> list[dict]:
    """已闭合节点按闭合轮次排序（根节点不计——它是隐式闭合的）。"""
    closed = [
        n for n in _nodes(g).values()
        if n.get("status") == "done" and n.get("id") != ROOT_ID
        and isinstance(n.get("iter_range"), list) and n["iter_range"][1] is not None
    ]
    closed.sort(key=lambda n: n["iter_range"][1])
    return closed


def metrics(state: AgentState) -> dict:
    """计算收敛指标。没有活动图时返回 {}。异常一律吞掉，绝不影响主循环。"""
    try:
        g = active_graph(state)
        if g is None:
            return {}
        nodes = [n for n in _nodes(g).values() if n.get("id") != ROOT_ID]
        if not nodes:
            return {}

        now = _iter(state)
        closed = _closure_order(g)
        # 废弃不算推进：它省下了力气，但没有把目标往前推。真正的进展是节点闭合。
        last_progress = closed[-1]["iter_range"][1] if closed else int(g.get("created_iter") or 0)
        # 用户中途给了新指导（续跑）→ 停滞时钟从那一刻重新起算。否则一次 L3 求助
        # 之后，stall_iters 仍是那个大数，用户刚答完就会被立刻再问一遍。
        baseline = state.meta.get("_graph_stall_baseline")
        if isinstance(baseline, int):
            last_progress = max(int(last_progress), baseline)

        done_count = len(closed)
        open_count = sum(1 for n in nodes if n.get("status") in _OPEN_STATUS)

        # 连续自证闭合：artifact 类出口能实证，其余三类只能自证。模型可以靠连关
        # 一串"我观察到了 X"把闭合率刷绿——这里只统计、只当软信号，不禁止。
        unverified = 0
        for n in reversed(closed):
            if n.get("closed_by") == "self_certified":
                unverified += 1
            else:
                break

        active = _active_node(g)
        return {
            "gid": g.get("gid", ""),
            "stall_iters": max(0, now - int(last_progress)),
            "node_revisits": max([int(n.get("visits") or 0) for n in nodes] or [0]),
            "revisit_node": max(nodes, key=lambda n: int(n.get("visits") or 0)).get("id", ""),
            "open_fanout": round(open_count / max(1, done_count), 2),
            "open_count": open_count,
            "done_count": done_count,
            "unverified_streak": unverified,
            "active_node": (active or {}).get("id", ""),
            "active_title": (active or {}).get("title", ""),
        }
    except Exception:
        return {}


def stall_level(m: dict) -> tuple[int, str]:
    """把指标翻译成升级层级 (0..2, 触发原因)。L3 由 loop 按 L2 之后的持续停滞判定。

    只给建议，不做动作——动作全部由 loop 走既有的升级梯，避免两套机制打架。
    """
    if not m:
        return 0, ""
    stall = int(m.get("stall_iters") or 0)
    revisits = int(m.get("node_revisits") or 0)
    fanout = float(m.get("open_fanout") or 0)
    unverified = int(m.get("unverified_streak") or 0)

    if (stall >= _env_int("GRAPH_STALL_L2", 40)
            or revisits >= _env_int("GRAPH_REVISIT_L2", 5)
            or fanout >= float(_env_int("GRAPH_FANOUT_L2", 5))):
        if stall >= _env_int("GRAPH_STALL_L2", 40):
            return 2, "stall"
        return 2, "revisit" if revisits >= _env_int("GRAPH_REVISIT_L2", 5) else "fanout"

    if stall >= _env_int("GRAPH_STALL_L1", 20):
        return 1, "stall"
    if revisits >= _env_int("GRAPH_REVISIT_L1", 3):
        return 1, "revisit"
    if unverified >= _env_int("GRAPH_UNVERIFIED_L1", 5):
        return 1, "unverified"
    return 0, ""


def stall_hint(m: dict, reason: str) -> str:
    """L1 软提示文字。措辞要给出路，不能只报警。"""
    node = m.get("active_node") or "-"
    if reason == "stall":
        return t("graph.stall.hint_stall", n=m.get("stall_iters", 0), node=node)
    if reason == "revisit":
        return t("graph.stall.hint_revisit", node=m.get("revisit_node", node), n=m.get("node_revisits", 0))
    if reason == "fanout":
        return t("graph.stall.hint_fanout", open=m.get("open_count", 0), done=m.get("done_count", 0))
    return t("graph.stall.hint_unverified", n=m.get("unverified_streak", 0))


def open_nodes(state: AgentState) -> list[dict]:
    """未达终态的节点，供 run 收尾时并入 run_outcome.gaps。"""
    try:
        g = active_graph(state)
        if g is None:
            return []
        out: list[dict] = []
        for node in _nodes(g).values():
            if node.get("status") not in _OPEN_STATUS:
                continue
            out.append({
                "node": node.get("id", ""),
                "title": node.get("title", ""),
                "goal": node.get("goal", ""),
                "status": node.get("status", ""),
                "exit": node.get("exit", {}),
                "parent": node.get("parent", ""),
            })
        out.sort(key=lambda n: n.get("node", ""))
        return out
    except Exception:
        return []


def gap_lines(state: AgentState) -> list[str]:
    """未闭合节点渲染成 gaps 文本行。

    run_outcome["gaps"] 的既有契约是 list[str]，消费方（自动续作）按字符串处理，
    这里不改契约、只把结构化信息压进一行；结构化原文另放 graph_gaps。
    """
    lines: list[str] = []
    for n in open_nodes(state):
        exit_spec = n.get("exit") or {}
        expect = ", ".join(exit_spec.get("expect") or [])
        lines.append(t(
            "graph.gaps.line",
            node=n.get("node", "?"),
            title=n.get("title", ""),
            status=n.get("status", ""),
            goal=_clip(n.get("goal"), 200) or "-",
            etype=exit_spec.get("evidence_type", "-"),
            expect=expect or "-",
        ))
    return lines
