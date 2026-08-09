"""
信心指数——S2：logprobs 熵信号。

原理：模型对自己下一个 token 的分布越平（越"迷茫"），采样出的 token 的
logprob 越低。对每轮输出的自由文本段（thought 字段）统计所选 token 的平均
负对数似然（即困惑度 PPL）与低置信 token 占比，得到一个不需要模型配合、
不可能被提示词糊弄的客观信心信号。

为什么只统计 thought 段：JSON 骨架 token（引号、冒号、固定键名）几乎是
确定性的，logprob≈0，混进来会稀释信号。thought 是每轮唯一的自由散文段，
熵变化最敏感。

对位策略：token 串联应等于原始输出的前缀（续写拼接时 raw 比 token 流长，
属正常）。对位在 **UTF-8 字节层** 做——CJK 字符常被切成多个字节级 token，
服务端返回的 token 字符串字段可能是乱码，但 bytes 字段是精确的。对不上时
（thinking 标签被剥、服务端不回 bytes 等）退化为全 token 统计，span 标记
为 "all"：JSON 骨架的稀释是常量级的，趋势仍然可读。

本模块无任何外部依赖，异常一律由调用方兜底（loop.py 包 try/except）。
"""

import math
import re
from typing import Optional

# token 概率低于 0.5 视为"低置信 token"（logprob < ln 0.5）
LOW_CONF_LOGPROB = math.log(0.5)

# thought 段命中的 token 少于该数时，样本太小，退回全 token 统计
MIN_SPAN_TOKENS = 5


def _find_thought_span(raw: str) -> Optional[tuple]:
    """定位 raw 中 "thought" 字段字符串值的字符区间 [start, end)。

    手写扫描而非 json.loads：raw 经常是残缺 JSON（截断/格式错误），而恰恰
    这些轮次的信心信号最有观察价值，不能因解析失败而丢样本。
    """
    m = re.search(r'"thought"\s*:\s*"', raw)
    if not m:
        return None
    start = m.end()
    i = start
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return (start, i)
        i += 1
    # 未闭合（输出被截断）：取到结尾
    return (start, n)


def compute_confidence(raw: str, token_logprobs: list) -> Optional[dict]:
    """从一轮输出的 per-token logprobs 计算信心指标。

    参数:
        raw            - 本轮 LLM 原始输出文本
        token_logprobs - [(token_bytes | token_str, logprob), ...]，
                         由后端的 last_token_logprobs 提供

    返回 dict（无有效数据时 None）:
        mean_lp  - 所选 token 的平均 logprob（越接近 0 越自信）
        ppl      - exp(-mean_lp)，困惑度，≥1，越高越迷茫
        low_conf - 概率 < 0.5 的 token 占比，0~1
        n_tok    - 参与统计的 token 数
        span     - "thought"（成功对位到 thought 段）或 "all"（退化为全量）
    """
    if not token_logprobs or not isinstance(raw, str):
        return None

    pairs = []
    for item in token_logprobs:
        try:
            tok, lp = item[0], float(item[1])
        except Exception:
            continue
        if isinstance(tok, str):
            tok = tok.encode("utf-8", errors="replace")
        elif not isinstance(tok, (bytes, bytearray)):
            continue
        pairs.append((bytes(tok), lp))
    if not pairs:
        return None

    selected = None
    span_kind = "all"
    char_span = _find_thought_span(raw)
    if char_span is not None:
        raw_b = raw.encode("utf-8")
        b_start = len(raw[: char_span[0]].encode("utf-8"))
        b_end = len(raw[: char_span[1]].encode("utf-8"))
        pos = 0
        picked = []
        aligned = True
        for tok, lp in pairs:
            t_start, t_end = pos, pos + len(tok)
            if raw_b[t_start:t_end] != tok:
                aligned = False
                break
            if t_end > b_start and t_start < b_end:
                picked.append(lp)
            pos = t_end
            if pos >= b_end:
                break
        if aligned and len(picked) >= MIN_SPAN_TOKENS:
            selected = picked
            span_kind = "thought"

    if selected is None:
        selected = [lp for _, lp in pairs]

    n = len(selected)
    mean_lp = sum(selected) / n
    low = sum(1 for lp in selected if lp < LOW_CONF_LOGPROB) / n
    try:
        ppl = math.exp(-mean_lp)
    except OverflowError:
        ppl = float("inf")
    return {
        "mean_lp": round(mean_lp, 4),
        "ppl": round(ppl, 3),
        "low_conf": round(low, 4),
        "n_tok": n,
        "span": span_kind,
    }
