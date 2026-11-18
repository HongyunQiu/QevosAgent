#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from agent.core import img_guard


class ImageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_dir = self.root / "20261118-230000"
        self.artifacts = self.run_dir / "artifacts"
        self.artifacts.mkdir(parents=True)
        (self.artifacts / "plain.png").write_bytes(b"png")
        (self.artifacts / "a b.png").write_bytes(b"png")
        (self.artifacts / "plot#1.png").write_bytes(b"png")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, text):
        return img_guard.check_answer_images(text, str(self.run_dir), self.run_dir.name)

    def test_existing_relative_image_passes(self):
        self.assertEqual(self.check("![x](artifacts/plain.png)"), [])

    def test_angle_bracket_destination_with_spaces_passes(self):
        text = "![x](<artifacts/a b.png>)"
        self.assertEqual(img_guard.extract_image_srcs(text), ["artifacts/a b.png"])
        self.assertEqual(self.check(text), [])

    def test_hash_in_filename_passes_guard(self):
        self.assertEqual(self.check("![x](artifacts/plot#1.png)"), [])

    def test_remote_and_data_images_are_blocked(self):
        for src in ("https://example.com/pixel.png", "//example.com/x.png", "data:image/png;base64,AAAA"):
            with self.subTest(src=src):
                broken = self.check(f'<img src="{src}">')
                self.assertEqual(broken[0]["reason"], "blocked_source")

    def test_absolute_server_path_is_blocked_even_when_it_exists(self):
        src = (self.artifacts / "plain.png").as_posix()
        broken = self.check(f"![x]({src})")
        self.assertEqual(broken[0]["reason"], "blocked_source")

    def test_parent_traversal_is_blocked_even_when_target_exists(self):
        (self.root / "outside.png").write_bytes(b"png")
        broken = self.check("![x](../outside.png)")
        self.assertEqual(broken[0]["reason"], "outside_run")

    def test_only_current_run_api_path_is_allowed(self):
        current = f"/api/run-file-raw/{self.run_dir.name}/artifacts/plain.png"
        other = "/api/run-file-raw/20261118-000000/artifacts/plain.png"
        self.assertEqual(self.check(f"![x]({current})"), [])
        self.assertEqual(self.check(f"![x]({other})")[0]["reason"], "blocked_source")

    def test_feedback_does_not_contain_image_syntax(self):
        feedback = img_guard.format_feedback(self.check("![x](https://example.com/x.png)"))
        self.assertEqual(img_guard.extract_image_srcs(feedback), [])
        self.assertIn("前端已禁止自动加载", feedback)


if __name__ == "__main__":
    unittest.main()
