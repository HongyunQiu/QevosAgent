#!/usr/bin/env python3
"""信心指数（S2：logprobs 熵）单元测试——纯离线，不碰任何后端。

守三条底线：
  1. thought 段对位必须在 UTF-8 字节层正确——CJK 多字节 token 是主场景
  2. 对不上时必须优雅退化为全 token 统计（span="all"），绝不返回错样本
  3. 残缺 JSON（截断/未闭合）也要能出数——那正是最值得观察的轮次
"""
import math
import unittest

from agent.core.confidence import (
    compute_confidence,
    _find_thought_span,
    LOW_CONF_LOGPROB,
    MIN_SPAN_TOKENS,
)


def _tokenize_bytes(raw: str, piece_len: int = 3):
    """把 raw 按固定字节长切成 token 流（模拟字节级 tokenizer）。"""
    b = raw.encode("utf-8")
    return [b[i:i + piece_len] for i in range(0, len(b), piece_len)]


class TestThoughtSpan(unittest.TestCase):
    def test_basic(self):
        raw = '{"thought": "hello world", "action": "done"}'
        s, e = _find_thought_span(raw)
        self.assertEqual(raw[s:e], "hello world")

    def test_escaped_quote(self):
        raw = '{"thought": "say \\"hi\\" now", "action": "done"}'
        s, e = _find_thought_span(raw)
        self.assertEqual(raw[s:e], 'say \\"hi\\" now')

    def test_truncated_unclosed(self):
        # 输出被截断，thought 字符串未闭合 → 取到结尾
        raw = '{"thought": "I am not sure what to'
        s, e = _find_thought_span(raw)
        self.assertEqual(raw[s:e], "I am not sure what to")

    def test_no_thought(self):
        self.assertIsNone(_find_thought_span('{"action": "done"}'))


class TestComputeConfidence(unittest.TestCase):
    def test_thought_span_selected(self):
        # thought 段 token 给低置信，骨架 token 给高置信；对位成功时
        # 指标应只反映 thought 段。
        raw = '{"thought": "aaaaaaaaaaaaaaaaaaaaaaaa", "action": "done"}'
        s, e = _find_thought_span(raw)
        toks = []
        pos = 0
        for tok in _tokenize_bytes(raw, 2):
            t_start, t_end = pos, pos + len(tok)
            inside = t_end > s and t_start < e  # ASCII：字节偏移=字符偏移
            toks.append((tok, -2.0 if inside else -0.01))
            pos = t_end
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "thought")
        self.assertAlmostEqual(conf["mean_lp"], -2.0, places=3)
        self.assertAlmostEqual(conf["ppl"], math.exp(2.0), places=2)
        self.assertEqual(conf["low_conf"], 1.0)

    def test_cjk_byte_alignment(self):
        # CJK thought：3 字节/字符，token 切在 2 字节边界上，跨字符切分。
        # 字节层对位必须仍然成功。
        raw = '{"thought": "我不确定接下来该做什么，也许应该重新检查", "action": "tool_call"}'
        toks = [(tok, -1.5) for tok in _tokenize_bytes(raw, 2)]
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "thought")
        self.assertAlmostEqual(conf["mean_lp"], -1.5, places=3)
        self.assertGreaterEqual(conf["n_tok"], MIN_SPAN_TOKENS)

    def test_misaligned_falls_back_to_all(self):
        # token 流与 raw 对不上（如 thinking 标签被剥掉）→ 退化为全量统计
        raw = '{"thought": "some reasoning here", "action": "done"}'
        toks = [(b"COMPLETELY", -0.5), (b"DIFFERENT", -0.5), (b"TOKENS", -0.5)]
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "all")
        self.assertEqual(conf["n_tok"], 3)
        self.assertAlmostEqual(conf["mean_lp"], -0.5, places=3)

    def test_too_few_span_tokens_falls_back(self):
        # thought 太短、命中 token 数 < MIN_SPAN_TOKENS → 全量统计
        raw = '{"thought": "ok", "action": "done"}'
        toks = []
        pos = 0
        for tok in _tokenize_bytes(raw, 4):
            toks.append((tok, -0.3))
            pos += len(tok)
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "all")

    def test_str_tokens_accepted(self):
        # 后端没回 bytes 字段时传 str token，也要能工作
        raw = '{"thought": "plain ascii thought text", "action": "done"}'
        toks = [(raw[i:i + 3], -0.8) for i in range(0, len(raw), 3)]
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "thought")

    def test_low_conf_ratio(self):
        raw = '{"thought": "xxxxxxxxxxxxxxxxxxxx", "action": "done"}'
        s, e = _find_thought_span(raw)
        toks = []
        pos = 0
        inside_count = 0
        for tok in _tokenize_bytes(raw, 2):
            t_start, t_end = pos, pos + len(tok)
            if t_end > s and t_start < e:
                # 交替给高/低置信
                lp = -0.1 if inside_count % 2 == 0 else LOW_CONF_LOGPROB - 0.5
                inside_count += 1
            else:
                lp = -0.01
            toks.append((tok, lp))
            pos = t_end
        conf = compute_confidence(raw, toks)
        self.assertEqual(conf["span"], "thought")
        self.assertAlmostEqual(conf["low_conf"], (inside_count // 2) / inside_count, places=2)

    def test_empty_inputs(self):
        self.assertIsNone(compute_confidence("{}", []))
        self.assertIsNone(compute_confidence("{}", None))
        self.assertIsNone(compute_confidence(None, [(b"a", -1.0)]))

    def test_truncated_json_still_scores(self):
        # 截断输出：token 流只覆盖 raw 前缀也能对位（续写场景 raw 更长）
        raw = '{"thought": "thinking about the problem carefully", "action"'
        full_toks = [(tok, -1.0) for tok in _tokenize_bytes(raw, 3)]
        # 只给前 80% 的 token（模拟续写后 raw 比 token 流长）
        toks = full_toks[: int(len(full_toks) * 0.8)]
        conf = compute_confidence(raw, toks)
        self.assertIsNotNone(conf)
        self.assertEqual(conf["span"], "thought")


if __name__ == "__main__":
    unittest.main(verbosity=2)
