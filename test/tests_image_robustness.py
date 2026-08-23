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


if __name__ == "__main__":
    unittest.main(verbosity=2)
