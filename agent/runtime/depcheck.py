"""启动时第三方依赖自检。

动机：曾出现远程实例漏装 json_repair（requirements.txt 里有，但环境没装全），
导致畸形 JSON 修复路径静默失效、被误判为"未转义引号"，模型陷入死循环。
缺依赖不应静默降级——启动时逐个探测，缺失即响亮告警，列出 pip 安装提示。

设计：
- 只用 importlib.util.find_spec 探测是否可导入，不真正 import（避免加载 cv2 等重模块）。
- 分两级：core（核心循环依赖，缺了会出诡异故障）与 optional（按功能用，缺了只是该功能不可用）。
- 非致命：打印醒目横幅后继续；设 QEVOS_STRICT_DEPS=1 可让 core 缺失直接退出。
"""
from __future__ import annotations

import importlib.util
import os
import sys

# (import 名, pip 包名, 级别, 用途说明)
_DEPS = [
    ("openai",      "openai",        "core",     "LLM 调用（OpenAI 兼容后端）"),
    ("pydantic",    "pydantic",      "core",     "数据模型校验"),
    ("json_repair", "json-repair",   "core",     "畸形 JSON 容错修复（缺失会导致格式错误死循环）"),
    ("httpx",       "httpx",         "core",     "HTTP 客户端"),
    ("tiktoken",    "tiktoken",      "core",     "token 计数"),
    ("anthropic",   "anthropic",     "optional", "Anthropic 后端（用 Claude 时需要）"),
    ("paramiko",    "paramiko",      "optional", "SSH / 远程 shell 工具"),
    ("ddgs",        "ddgs",          "optional", "网络搜索"),
    ("PIL",         "Pillow",        "optional", "图像 / 视觉"),
    ("cv2",         "opencv-python", "optional", "图像 / 视觉"),
]


def _is_available(import_name: str) -> bool:
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # find_spec can raise if a parent package itself is half-broken.
        return False


def check_dependencies(strict: bool | None = None) -> dict:
    """探测所有声明依赖，缺失则向 stderr 打印横幅。

    返回 {'missing_core': [...], 'missing_optional': [...]}，元素为 (import, pip, 用途)。
    strict=None 时读环境变量 QEVOS_STRICT_DEPS；strict 为真且有 core 缺失则 sys.exit(1)。
    """
    if strict is None:
        strict = os.environ.get("QEVOS_STRICT_DEPS", "0") not in ("0", "", "false", "False")

    missing_core, missing_optional = [], []
    for import_name, pip_name, tier, desc in _DEPS:
        if _is_available(import_name):
            continue
        (missing_core if tier == "core" else missing_optional).append((import_name, pip_name, desc))

    if missing_core or missing_optional:
        all_pip = [p for _, p, _ in missing_core + missing_optional]
        lines = ["", "=" * 64, "⚠️  依赖自检：检测到缺失的第三方库", "=" * 64]
        if missing_core:
            lines.append("【核心缺失】缺了会导致诡异故障，强烈建议先装：")
            for imp, pip_name, desc in missing_core:
                lines.append(f"    ✗ {pip_name:16} — {desc}")
        if missing_optional:
            lines.append("【可选缺失】对应功能不可用，按需安装：")
            for imp, pip_name, desc in missing_optional:
                lines.append(f"    · {pip_name:16} — {desc}")
        lines.append("-" * 64)
        lines.append("一键补全（用启动 Agent 的同一个 Python 环境执行）：")
        lines.append(f"    pip install {' '.join(all_pip)}")
        lines.append("或：pip install -r requirements.txt")
        lines.append("=" * 64)
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()

        if strict and missing_core:
            sys.stderr.write("QEVOS_STRICT_DEPS 已开启且存在核心依赖缺失，已中止启动。\n")
            sys.exit(1)

    return {"missing_core": missing_core, "missing_optional": missing_optional}
