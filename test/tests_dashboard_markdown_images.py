#!/usr/bin/env python3
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "dashboard" / "public" / "index.html"


def _extract_js_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(source)):
        ch = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError(f"function {name} is not balanced")


@unittest.skipUnless(shutil.which("node"), "node is required for dashboard JS regression tests")
class DashboardImageResolverTests(unittest.TestCase):
    def test_only_current_run_relative_images_are_resolved(self):
        source = INDEX.read_text(encoding="utf-8")
        fn = _extract_js_function(source, "_resolveRunImgSrc")
        cases = [
            ["artifacts/plain.png", "/api/run-file-raw/20261118-230000/artifacts/plain.png"],
            ["artifacts/a b.png", "/api/run-file-raw/20261118-230000/artifacts/a%20b.png"],
            ["artifacts/plot#1.png", "/api/run-file-raw/20261118-230000/artifacts/plot%231.png"],
            ["/api/run-file-raw/20261118-230000/artifacts/plot#1.png", "/api/run-file-raw/20261118-230000/artifacts/plot%231.png"],
            ["../outside.png", None],
            ["https://example.com/pixel.png", None],
            ["//example.com/pixel.png", None],
            ["data:image/png;base64,AAAA", None],
            ["E:/photos/p.png", None],
            ["/api/other-endpoint", None],
        ]
        script = (
            "const gViewRunId='20261118-230000'; const gState=null;\n"
            + fn
            + "\nconst cases=" + json.dumps(cases) + ";\n"
            + "for (const [src,want] of cases) { const got=_resolveRunImgSrc(src); "
              "if (got!==want) throw new Error(`${src}: ${got} !== ${want}`); }"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_lightbox_zoom_is_bounded_and_wired_to_markdown_images(self):
        source = INDEX.read_text(encoding="utf-8")
        fn = _extract_js_function(source, "_clampImageZoom")
        script = fn + "\nconst got=[_clampImageZoom(-1),_clampImageZoom(0.5),_clampImageZoom(99)];" \
            + "if (JSON.stringify(got)!=='[0.25,0.5,8]') throw new Error(JSON.stringify(got));"
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('id="imageLightbox"', source)
        self.assertIn("data-image-zoomable", source)
        self.assertIn("viewport.addEventListener('wheel'", source)
        self.assertIn("viewport.addEventListener('pointermove'", source)
        self.assertIn("event.key === 'Escape'", source)


if __name__ == "__main__":
    unittest.main()
