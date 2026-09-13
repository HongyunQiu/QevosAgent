#!/usr/bin/env python3
"""远程图片加载的容错回归。

背景：run 20260824-005138 因为 load_image 把 `https://zh.moegirl.org.cn/File:xxx.jpg`
（一个 HTML 页面，不是图片直链）原样交给 LLM 后端去取，后端 Pillow 解不开，整轮调用
400。坏图片留在 short_term 里，之后每一轮都撞同一个 400，任务无声挂死。

这里守三条线：
  A 工具层——远程图片先下载校验，非图片绝不进上下文；
  B 循环层——万一还是有坏图进了上下文，后端报错时要把它摘掉而不是干重试；
  C 熔断层——确定性 4xx 且上下文没变时要写终态退出，不能空转到迭代耗尽。
"""
import http.server
import io
import os
import socketserver
import threading
import unittest

from agent.core import loop as L
from agent.core.llm import LLMBackend
from agent.core.types_def import AgentState
from agent.tools.standard import (
    _fetch_remote_image,
    _sniff_image_mime,
    tool_load_image,
)

# ── 测试素材 ──────────────────────────────────────────────────────────────────

try:
    from PIL import Image

    _buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(_buf, format="PNG")
    PNG = _buf.getvalue()
except ImportError:  # pragma: no cover - 环境没 Pillow 时退化成裸文件头
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

HTML = (
    "<!DOCTYPE html>\n<html><head><title>File:牛来2.jpg - 萌娘百科</title></head>"
    "<body>x</body></html>"
).encode("utf-8")

# 现场原话（vLLM 双 A6000），指纹匹配必须以它为准。
REAL_400 = (
    "Error code: 400 - {'object': 'error', 'message': \"An exception occurred while "
    "loading IMAGE data at index 0: Error while loading data ImageData("
    "url='https://zh.moegirl.org.cn/File:%E7%89%9B%E6%9D%A52.jpg', detail='auto', "
    "max_dynamic_p...: cannot identify image file <_io.BytesIO object at 0x785dc81bb830>\", "
    "'type': 'BadRequestError', 'param': None, 'code': 400}"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        table = {
            "/real.png": (PNG, "image/png"),
            "/liar.png": (HTML, "image/png"),          # 声称是图片，实为 HTML
            "/vec.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
            "/empty": (b"", "image/png"),
        }
        if self.path.startswith("/File:"):
            body, ctype = HTML, "text/html; charset=UTF-8"
        elif self.path in table:
            body, ctype = table[self.path]
        else:
            self.send_error(404, "Not Found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _LocalSite:
    """起一个本地站点，模拟"图片描述页 vs 图片直链"。"""

    def setUp(self):
        self.srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.state = AgentState(goal="t")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()


# ── A 层：工具层校验 ──────────────────────────────────────────────────────────


class SniffTests(unittest.TestCase):
    def test_known_formats(self):
        self.assertEqual(_sniff_image_mime(PNG), "image/png")
        self.assertEqual(_sniff_image_mime(b"\xff\xd8\xff\xe0" + b"0" * 20), "image/jpeg")
        self.assertEqual(_sniff_image_mime(b"GIF89a" + b"0" * 20), "image/gif")
        self.assertEqual(_sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_html_is_not_an_image(self):
        self.assertIsNone(_sniff_image_mime(HTML))


class FetchRemoteImageTests(_LocalSite, unittest.TestCase):
    def test_real_image_passes(self):
        raw, err, _ = _fetch_remote_image(self.base + "/real.png")
        self.assertEqual(err, "")
        self.assertEqual(raw, PNG)

    def test_file_page_rejected_permanently_with_actionable_hint(self):
        raw, err, permanent = _fetch_remote_image(self.base + "/File:x.jpg")
        self.assertIsNone(raw)
        self.assertTrue(permanent)
        self.assertIn("不是图片", err)
        self.assertIn("og:image", err)  # 必须告诉模型下一步怎么办

    def test_content_type_image_but_unknown_header_defers_to_pillow(self):
        # 声称 image/* 却认不出文件头时不要武断拒绝——可能只是没枚举到的格式。
        raw, err, _ = _fetch_remote_image(self.base + "/liar.png")
        self.assertEqual(err, "")
        self.assertIsNotNone(raw)

    def test_svg_rejected(self):
        raw, err, permanent = _fetch_remote_image(self.base + "/vec.svg")
        self.assertIsNone(raw)
        self.assertTrue(permanent)
        self.assertIn("SVG", err)

    def test_empty_body_is_transient_not_permanent(self):
        raw, err, permanent = _fetch_remote_image(self.base + "/empty")
        self.assertIsNone(raw)
        self.assertFalse(permanent)

    def test_http_404_permanent(self):
        raw, err, permanent = _fetch_remote_image(self.base + "/nope")
        self.assertIsNone(raw)
        self.assertTrue(permanent)
        self.assertIn("404", err)

    def test_size_cap(self):
        os.environ["LOAD_IMAGE_MAX_BYTES"] = "10"
        try:
            raw, err, permanent = _fetch_remote_image(self.base + "/real.png")
        finally:
            os.environ.pop("LOAD_IMAGE_MAX_BYTES")
        self.assertIsNone(raw)
        self.assertTrue(permanent)
        self.assertIn("上限", err)


class LoadImageToolTests(_LocalSite, unittest.TestCase):
    def test_remote_image_injected_as_base64_not_url(self):
        r = tool_load_image(self.state, self.base + "/real.png", caption="说明")
        self.assertTrue(r.success, r.error)
        blocks = r.content_blocks
        self.assertEqual(blocks[0]["type"], "text")
        # 关键回归：注入的必须是本地解过码的 base64，而不是让后端自己去取的 URL。
        self.assertEqual(blocks[-1]["type"], "image")
        self.assertIn("data", blocks[-1])
        self.assertNotIn("url", blocks[-1])

    def test_file_page_fails_at_tool_layer_without_polluting_context(self):
        url = self.base + "/File:x.jpg"
        r = tool_load_image(self.state, url)
        self.assertFalse(r.success)
        self.assertFalse(r.content_blocks)          # 一个字节都不许进上下文
        self.assertIn(url, self.state.meta["_bad_image_urls"])

    def test_second_attempt_on_known_bad_url_is_refused(self):
        url = self.base + "/File:x.jpg"
        tool_load_image(self.state, url)
        again = tool_load_image(self.state, url)
        self.assertFalse(again.success)
        self.assertIn("之前已被证实", again.error)

    def test_escape_hatch_keeps_url_passthrough(self):
        os.environ["LOAD_IMAGE_REMOTE_MODE"] = "url"
        try:
            r = tool_load_image(self.state, self.base + "/File:x.jpg")
        finally:
            os.environ.pop("LOAD_IMAGE_REMOTE_MODE")
        self.assertTrue(r.success)
        self.assertEqual(r.content_blocks[-1]["url"], self.base + "/File:x.jpg")


# ── B 层：错误识别与坏图剥离 ──────────────────────────────────────────────────


class ErrorClassificationTests(unittest.TestCase):
    def test_real_400_recognised_as_image_decode_error(self):
        self.assertTrue(L._is_image_decode_error(REAL_400))

    def test_network_error_not_misread_as_image_error(self):
        self.assertFalse(L._is_image_decode_error("Connection reset by peer"))

    def test_old_vision_unsupported_branch_does_not_swallow_it(self):
        # 这正是当初没能自愈的原因：现场错误一条 vision 关键词都不含。
        self.assertFalse(
            "image" in REAL_400.lower()
            and any(k in REAL_400 for k in ("0 image", "not support", "unsupport", "vision", "multimodal"))
        )

    def test_deterministic_vs_transient(self):
        class E400(Exception):
            status_code = 400

        self.assertTrue(L._is_deterministic_llm_error(E400("x"), REAL_400))
        self.assertFalse(L._is_deterministic_llm_error(Exception("x"), "Error code: 429 rate limited"))
        self.assertFalse(L._is_deterministic_llm_error(Exception("x"), "Error code: 503 overloaded"))
        self.assertFalse(L._is_deterministic_llm_error(Exception("x"), "Connection refused"))

    def test_signature_ignores_volatile_parts(self):
        other = REAL_400.replace("0x785dc81bb830", "0x7ffdeadbeef0")
        self.assertEqual(L._llm_error_signature(REAL_400), L._llm_error_signature(other))
        self.assertNotEqual(L._llm_error_signature(REAL_400), L._llm_error_signature("Connection refused"))


class StripBrokenImageTests(unittest.TestCase):
    def _state(self):
        st = AgentState(goal="t")
        st.short_term = [
            {"role": "user", "content": "纯文本"},
            {"role": "user", "content": [
                {"type": "text", "text": "看这张"},
                {"type": "image", "url": "https://zh.moegirl.org.cn/File:x.jpg"},
            ]},
            {"role": "user", "content": [{"type": "image", "media_type": "image/png", "data": "AAAA"}]},
        ]
        return st

    def test_url_blocks_stripped_first(self):
        st = self._state()
        count, urls = L._strip_broken_image_blocks(st)
        self.assertEqual(count, 1)
        self.assertEqual(urls, ["https://zh.moegirl.org.cn/File:x.jpg"])
        self.assertEqual(st.short_term[1]["content"], "看这张")
        # 本地截图是 Pillow 解过码才注入的，不该被误伤。
        self.assertEqual(st.short_term[2]["content"][0]["data"], "AAAA")

    def test_falls_back_to_all_images_when_no_url_blocks(self):
        st = AgentState(goal="t")
        st.short_term = [{"role": "user", "content": [{"type": "image", "media_type": "image/png", "data": "A"}]}]
        count, _ = L._strip_broken_image_blocks(st)
        self.assertEqual(count, 1)
        self.assertTrue(st.short_term[0]["content"].startswith("[图片已移除"))

    def test_noop_without_images(self):
        st = AgentState(goal="t")
        st.short_term = [{"role": "user", "content": [{"type": "text", "text": "无图"}]}]
        self.assertEqual(L._strip_broken_image_blocks(st)[0], 0)

    def test_legacy_entrypoint_still_works(self):
        st = AgentState(goal="t")
        st.short_term = [{"role": "user", "content": [
            {"type": "text", "text": "t"}, {"type": "image", "url": "u"}]}]
        self.assertEqual(L._strip_vision_blocks(st), 1)


# ── C 层：确定性错误熔断（端到端）────────────────────────────────────────────


class _AlwaysFails(LLMBackend):
    """每次调用都抛同一个异常，模拟"上下文没变 → 结果必然相同"。"""

    def __init__(self, error=REAL_400, status=400, exc_name="BadRequestError"):
        self.calls = 0
        self.error = error
        self.exc = type(exc_name, (Exception,), {"status_code": status})

    def complete(self, messages, system):
        self.calls += 1
        raise self.exc(self.error)


class DeterministicErrorBreakerTests(unittest.TestCase):
    def test_run_stops_and_records_outcome_instead_of_burning_iterations(self):
        llm = _AlwaysFails()
        state = L.run("测试目标", llm, tools={}, max_iterations=30)

        outcome = state.meta.get("run_outcome")
        self.assertIsInstance(outcome, dict, "确定性故障必须写 run_outcome，不能停在 running/None")
        self.assertEqual(outcome["outcome"], L.RUN_OUTCOME_FAILED)
        self.assertEqual(outcome["reason"], "llm_error_deterministic")
        # 旧行为是重试 10 次再空转到 max_iterations；现在 3 次就止损。
        self.assertLessEqual(llm.calls, 4, f"重试了 {llm.calls} 次，止损没生效")
        self.assertLess(state.iteration, 30)

    def test_transient_error_still_gets_full_retry_budget(self):
        # 5xx 是值得退避重试的瞬时故障，不能被确定性熔断误伤：必须重试到超过
        # LLM_DETERMINISTIC_ERROR_BUDGET（3）为止，最终按迭代耗尽而不是熔断收场。
        llm = _AlwaysFails("service overloaded", status=503, exc_name="APIStatusError")
        state = L.run("测试目标", llm, tools={}, max_iterations=3)
        self.assertGreater(llm.calls, 3, "瞬时错误被当成确定性错误提前熔断了")
        self.assertNotEqual(
            (state.meta.get("run_outcome") or {}).get("reason"), "llm_error_deterministic"
        )

    def test_status_json_says_failed_not_running(self):
        # 现场那次 run 的病征就是 status 停在 running、run_outcome 为空。
        import json
        import tempfile
        from pathlib import Path

        from agent.runtime.persistence import RunPersistence

        with tempfile.TemporaryDirectory() as tmp:
            st = AgentState(goal="t")
            st.persistence = RunPersistence(tmp)
            st.persistence.start(st)
            L.run("测试目标", _AlwaysFails(), tools={}, state=st, max_iterations=30)

            status = json.loads(Path(tmp, "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status.get("status"), "failed", status)
            meta = json.loads(Path(tmp, "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["run_outcome"]["outcome"], L.RUN_OUTCOME_FAILED)

    def test_healing_resets_the_streak(self):
        # 上下文里有坏图 → 第一轮自愈（摘图）应重置计数，让模型有机会换图重来。
        st = AgentState(goal="t")
        st.short_term = [{"role": "user", "content": [
            {"type": "text", "text": "看这张"},
            {"type": "image", "url": "https://zh.moegirl.org.cn/File:x.jpg"},
        ]}]
        llm = _AlwaysFails()
        state = L.run("测试目标", llm, tools={}, state=st, max_iterations=30)

        self.assertNotIn(
            "image",
            [b.get("type") for m in state.short_term if isinstance(m.get("content"), list)
             for b in m["content"] if isinstance(b, dict)],
            "坏图片没有被摘掉，上下文仍处于被污染状态",
        )
        self.assertIn("https://zh.moegirl.org.cn/File:x.jpg", state.meta.get("_bad_image_urls", {}))
        # 自愈那一轮不计入连击，所以总调用次数比纯熔断路径多一次。
        self.assertGreaterEqual(llm.calls, 4)


# ── D 层：图片数量超限（可自愈，绝不该走到熔断）──────────────────────────────
#
# 背景：run 20260913-111822 在第 77 轮 load_image 加载第 6 张截图后死掉。后端上限是
# 5 张，报 400；但这条错误一条 vision 关键词都不含、也不是解码失败，两个自愈分支全
# 落空 → _healed=False → 连撞三次 → 确定性熔断，任务停在"展示截图给用户确认收尾"
# 的最后一步。注入的那条"[系统] LLM调用异常…请重试"模型一次都没看到：下一轮请求还
# 是带着同样 6 张图，在模型读到它之前就被 400 掉了。
#
# 守三条线：
#   1 识别——"At most N image(s)" 要解析出 N，且不能被误判成"不支持多模态"；
#   2 自愈——摘掉最旧的几张、保留文字上下文，并把上限记进 meta；
#   3 预防——上限已知后，发请求前就把图片数收口，不再白撞 400。

IMAGE_LIMIT_400 = (
    "Error code: 400 - {'error': {'message': 'At most 5 image(s) may be provided "
    "in one prompt. (parameter=image)', 'type': 'BadRequestError', "
    "'param': 'image', 'code': 400}}"
)


def _img_state(n, goal="t"):
    """构造 n 张图的 short_term，每张都配一句说明文字。"""
    st = AgentState(goal=goal)
    st.short_term = [{"role": "user", "content": [
        {"type": "text", "text": f"第{i}张"},
        {"type": "image", "media_type": "image/png", "data": f"IMG{i}"},
    ]} for i in range(n)]
    return st


def _img_data(st):
    return [b["data"] for m in st.short_term if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "image"]


class ImageLimitParsingTests(unittest.TestCase):
    def test_parses_the_real_message(self):
        self.assertEqual(L._parse_image_limit(IMAGE_LIMIT_400), 5)

    def test_parses_other_phrasings(self):
        self.assertEqual(L._parse_image_limit("maximum of 3 images allowed"), 3)
        self.assertEqual(L._parse_image_limit("max 8 images per request"), 8)

    def test_unrelated_errors_yield_none(self):
        self.assertIsNone(L._parse_image_limit(REAL_400))
        self.assertIsNone(L._parse_image_limit("context length exceeded"))
        self.assertIsNone(L._parse_image_limit("Connection reset by peer"))

    def test_limit_error_is_not_read_as_vision_unsupported(self):
        # 这是关键的边界：裸子串 "0 image" 会把 "At most 10 image(s)" 误判成
        # 后端不支持图片，然后把上下文里所有图片永久摘光。
        self.assertFalse(L._is_vision_unsupported_error(IMAGE_LIMIT_400))
        self.assertFalse(L._is_vision_unsupported_error("At most 10 image(s) may be provided"))
        self.assertFalse(L._is_vision_unsupported_error("At most 20 image(s) may be provided"))
        self.assertTrue(L._is_vision_unsupported_error("At most 0 image(s) may be provided"))
        self.assertTrue(L._is_vision_unsupported_error("this model does not support image input"))


class TrimExcessImagesTests(unittest.TestCase):
    def test_keeps_the_newest_and_drops_the_oldest(self):
        st = _img_state(6)
        self.assertEqual(L._count_image_blocks(st), 6)
        self.assertEqual(L._trim_excess_image_blocks(st, 5), 1)
        # 该丢的一定是旧的——新截图才是模型当前正在看的那张。
        self.assertEqual(_img_data(st), ["IMG1", "IMG2", "IMG3", "IMG4", "IMG5"])

    def test_text_survives_so_the_model_still_knows_what_happened(self):
        st = _img_state(6)
        L._trim_excess_image_blocks(st, 5)
        self.assertEqual(st.short_term[0]["content"], "第0张")

    def test_noop_when_already_under_limit(self):
        st = _img_state(3)
        self.assertEqual(L._trim_excess_image_blocks(st, 5), 0)
        self.assertEqual(L._count_image_blocks(st), 3)


class ImageBudgetGateTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("LLM_MAX_IMAGES", None)
        self.hooks = L.AgentHooks()

    def tearDown(self):
        os.environ.pop("LLM_MAX_IMAGES", None)
        if self._saved is not None:
            os.environ["LLM_MAX_IMAGES"] = self._saved

    def test_no_cap_means_no_trimming(self):
        # Anthropic 这类后端能收上百张，硬塞一个保守默认值只会白白丢信息。
        st = _img_state(20)
        self.assertEqual(L._enforce_image_budget(st, self.hooks), 0)
        self.assertEqual(L._count_image_blocks(st), 20)

    def test_env_cap_applies_before_any_failure(self):
        os.environ["LLM_MAX_IMAGES"] = "2"
        st = _img_state(6)
        self.assertEqual(L._enforce_image_budget(st, self.hooks), 4)
        self.assertEqual(_img_data(st), ["IMG4", "IMG5"])

    def test_learned_cap_takes_precedence(self):
        os.environ["LLM_MAX_IMAGES"] = "2"
        st = _img_state(6)
        st.meta["_max_images"] = 5      # 上一次 400 学到的真实上限
        self.assertEqual(L._enforce_image_budget(st, self.hooks), 1)


class _FailsWhileTooManyImages(LLMBackend):
    """忠实复刻现场后端：请求里图片超过 limit 就 400，否则正常作答。"""

    def __init__(self, limit=5):
        self.limit = limit
        self.calls = 0
        self.rejections = 0
        self.exc = type("BadRequestError", (Exception,), {"status_code": 400})

    def complete(self, messages, system):
        self.calls += 1
        n = sum(
            1 for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "image"
        )
        if n > self.limit:
            self.rejections += 1
            raise self.exc(IMAGE_LIMIT_400)
        return '{"thought": "图都在了", "action": "done", "result": "ok"}'


class ImageLimitHealingTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("LLM_MAX_IMAGES", None)

    def tearDown(self):
        os.environ.pop("LLM_MAX_IMAGES", None)
        if self._saved is not None:
            os.environ["LLM_MAX_IMAGES"] = self._saved

    def test_run_heals_and_finishes_instead_of_tripping_the_breaker(self):
        st = _img_state(6, goal="测试目标")
        llm = _FailsWhileTooManyImages(limit=5)
        state = L.run("测试目标", llm, tools={}, state=st, max_iterations=30)

        self.assertEqual(llm.rejections, 1, "摘图后不该再撞同一个 400")
        self.assertEqual(L._count_image_blocks(state), 5)
        self.assertEqual(state.meta.get("_max_images"), 5, "上限没被记住，下一次还会白撞一次")
        self.assertNotEqual(
            (state.meta.get("run_outcome") or {}).get("reason"),
            "llm_error_deterministic",
            "可自愈的图片超限被当成不可自愈的确定性错误熔断了",
        )
        # 多模态能力不能被误判掉：这只是超限，不是"后端收不了图"。
        self.assertNotEqual(state.meta.get("_vision_supported"), False)

    def test_the_system_note_actually_reaches_the_model(self):
        # 现场的病根：错误提示写进了 short_term，却因为上下文没变而永远送不出去。
        st = _img_state(6, goal="测试目标")
        llm = _FailsWhileTooManyImages(limit=5)
        L.run("测试目标", llm, tools={}, state=st, max_iterations=30)

        notes = [m["content"] for m in st.short_term
                 if isinstance(m.get("content"), str) and m["content"].startswith("[系统]")]
        hits = [n for n in notes if "最多 5 张图片" in n]
        self.assertTrue(hits, f"没有给模型留下可操作的解释，只有：{notes}")
        self.assertIn("最旧的 1 张", hits[0], "没说清丢了哪几张")
        self.assertFalse(
            [n for n in notes if "请重试或换一种方式" in n],
            "还是那条什么都没说、而且根本送不到模型面前的泛泛提示",
        )

    def test_budget_gate_prevents_the_second_collision(self):
        # 上限学到之后，后面每加一张新图都该在发请求前自动挤掉最旧的一张。
        st = _img_state(6, goal="测试目标")
        st.meta["_max_images"] = 5
        llm = _FailsWhileTooManyImages(limit=5)
        L.run("测试目标", llm, tools={}, state=st, max_iterations=30)
        self.assertEqual(llm.rejections, 0, "上限已知却还是把超量的图送了出去")


if __name__ == "__main__":
    unittest.main(verbosity=2)
