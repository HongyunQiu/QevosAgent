"""SKILL 清单构建 —— 供 system prompt 与 advisor 复用。

只提取「名称 + 一句话简介」，不读正文：让 agent 一启动就知道有哪些领域技能存在、
各管什么，相关时自己调 read_skill 拉全文（渐进披露）。

与全文注入（run_goal.py 的 --skills / AGENT_SKILLS）是两条互补的路：
  - 清单：负责「发现」，恒定注入，约每条 1 行
  - 全文：负责「强制遵守」，由调用方显式勾选

依赖方向：i18n ← skills（不依赖 core 内其他模块，可被 llm/advisor 安全引用）
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..i18n import t

# agent/core/skills.py → 上溯三层 = 仓库根 → SKILLS/
# 与 agent/tools/standard.py 的 _SKILLS_DIR 保持一致。
_SKILLS_DIR = Path(__file__).parent.parent.parent / "SKILLS"

# 简介截断上限。SKILLS/ 里最长的「适用领域」行约 60 字，100 足够容纳且不会撑爆清单。
_DESC_MAX = 100

# 简介来源，按优先级从高到低：
#   1) YAML frontmatter 的 description:（forgecad.md 已用此格式）
#   2) 正文的「适用领域：」/「适用场景：」行（19/22 个文件有）
#   3) H1 标题（22/22 个文件都有，兜底）
#   4) 文件名
# 空白一律用 [ \t] 而非 \s：\s 含换行，冒号后为空时会跨行吃掉下一行当简介
# （TEST.md 的「适用领域：」为空值，曾因此抓到下一行的「## 规范」）。
_FM_DESC_RE = re.compile(r"^description[ \t]*[:：][ \t]*(.+)$", re.M)
_DOMAIN_RE = re.compile(
    r"^[ \t]*(?:适用领域|适用场景|Applicable[ \t]+(?:domains?|scenarios?))"
    r"[ \t]*[:：][ \t]*(.+)$",
    re.M | re.I,
)
_H1_RE = re.compile(r"^#[ \t]+(.+)$", re.M)
_H1_PREFIX_RE = re.compile(r"^SKILL\s*[:：]\s*", re.I)
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.S)


def skills_dir() -> Path:
    """SKILLS 目录。SKILLS_DIR 环境变量可覆盖（与 tool_list_skills 同源）。"""
    return Path(os.environ.get("SKILLS_DIR", str(_SKILLS_DIR)))


def _clean(text: str) -> str:
    """规范化一行简介：去 markdown 强调、压缩空白、去尾部标点、截断。"""
    s = text.strip().strip("\"'")
    s = s.replace("**", "").replace("*", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("。.,，、;；:：")
    if len(s) > _DESC_MAX:
        s = s[:_DESC_MAX].rstrip() + "…"
    return s


def _split_frontmatter(content: str) -> tuple[str, str]:
    """拆出 (frontmatter, body)。无 frontmatter 时前者为空串。"""
    m = _FRONTMATTER_RE.match(content)
    return (m.group(1), m.group(2)) if m else ("", content)


def describe_skill(path: Path) -> str:
    """提取单个 skill 的一句话简介。任何异常都降级到文件名，不抛。"""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return path.stem

    frontmatter, body = _split_frontmatter(content)

    if frontmatter:
        m = _FM_DESC_RE.search(frontmatter)
        if m and (desc := _clean(m.group(1))):
            return desc

    m = _DOMAIN_RE.search(body)
    if m and (desc := _clean(m.group(1))):
        return desc

    m = _H1_RE.search(body)
    if m and (title := _H1_PREFIX_RE.sub("", _clean(m.group(1))).strip()):
        return title

    return path.stem


def build_skills_catalog(active: list[str] | None = None) -> str:
    """构建「- 名称 — 简介」清单。

    active 中的 skill 已由 --skills 全文注入，标注出来免得 agent 再调一次
    read_skill 去读它手里已经有的东西，白烧一轮迭代。

    返回空串表示无可用 skill —— 调用方据此整节跳过。
    """
    directory = skills_dir()
    if not directory.exists():
        return ""

    active_set = {a.strip() for a in (active or []) if a.strip()}
    lines: list[str] = []
    for path in sorted(directory.glob("*.md")):
        tag = t("sys.skills_loaded_tag") if path.stem in active_set else ""
        lines.append(f"- {path.stem} — {describe_skill(path)}{tag}")
    return "\n".join(lines)
