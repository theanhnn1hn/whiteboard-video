"""Smoke test cho phần hình: render một cảnh nhỏ tự sinh, kiểm tra annotation, ảnh kiểm tra vùng,
gắn phụ đề, ghép cảnh.

Chạy: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import burn_subtitles  # noqa: E402
import merge_scenes  # noqa: E402
import render_annotation_preview  # noqa: E402
import render_stream_whiteboard as rsw  # noqa: E402
import validate_annotation  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
PAPER_BGR = (215, 235, 245)  # #F5EBD7
W, H = 640, 360


def make_scene(folder: Path) -> tuple[Path, Path, dict]:
    """Ảnh nền giấy với 2 hình (người que, mặt trời) + annotation 3 vùng, vùng 3 là giấy trắng trơn."""
    img = np.full((H, W, 3), PAPER_BGR, dtype=np.uint8)
    cv2.circle(img, (150, 120), 50, (40, 40, 40), 4)
    cv2.line(img, (150, 170), (150, 300), (40, 40, 40), 4)
    cv2.line(img, (100, 220), (200, 220), (40, 40, 40), 4)
    cv2.circle(img, (470, 120), 45, (40, 120, 230), -1)
    cv2.circle(img, (470, 120), 45, (40, 40, 40), 4)
    image = folder / "scene-01-test.png"
    cv2.imwrite(str(image), img)
    ann = {
        "sceneId": "scene-01",
        "canvas": {"width": W, "height": H},
        "storyBasis": "test",
        "sceneDurationMs": 4000,
        "elements": [
            {"id": "nguoi", "label": "Người", "sequence": 1, "narrativeRole": "nhân vật", "subtitle": "Câu một.",
             "region": {"x": 80, "y": 50, "width": 160, "height": 280},
             "reveal": {"direction": "top_to_bottom", "startMs": 100, "durationMs": 1200, "protectedRegions": []},
             "handPath": {"start": [160, 50], "end": [160, 330]}},
            {"id": "mattroi", "label": "Mặt trời", "sequence": 2, "narrativeRole": "bối cảnh", "subtitle": "Câu hai.",
             "region": {"x": 400, "y": 50, "width": 150, "height": 150},
             "reveal": {"direction": "left_to_right", "startMs": 1400, "durationMs": 1000, "protectedRegions": []},
             "handPath": {"start": [400, 120], "end": [550, 120]}},
            {"id": "trong", "label": "Góc trống", "sequence": 3, "narrativeRole": "thử vùng không có nét",
             "subtitle": "Câu ba.",
             "region": {"x": 560, "y": 260, "width": 70, "height": 90},
             "reveal": {"direction": "top_to_bottom", "startMs": 2500, "durationMs": 600, "protectedRegions": []},
             "handPath": {"start": [595, 260], "end": [595, 350]}},
        ],
    }
    annotation = folder / "scene-01-test.annotation.json"
    annotation.write_text(json.dumps(ann, ensure_ascii=False), encoding="utf-8")
    return image, annotation, ann


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


def read_frames(video: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(video))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


@unittest.skipUnless(HAS_FFMPEG, "cần ffmpeg")
class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.image, cls.annotation, cls.ann = make_scene(cls.dir)
        cls.video = cls.dir / "scene-01-test-whiteboard.mp4"
        code, out, err = quiet(rsw.main, [str(cls.image), str(cls.annotation), str(cls.video),
                                          "--cap-long-edge", "320", "--fps", "20"])
        cls.render_result = (code, out, err)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_render_succeeds_with_blank_region(self):
        code, out, err = self.render_result
        self.assertEqual(code, 0, err + out)  # trước đây vùng không có nét làm sập renderer
        self.assertTrue(self.video.exists())

    def test_size_keeps_aspect_ratio(self):
        frames = read_frames(self.video)
        h, w = frames[0].shape[:2]
        self.assertEqual((w, h), (320, 180))

    def test_duration_matches_scene(self):
        frames = read_frames(self.video)
        self.assertAlmostEqual(len(frames) / 20, self.ann["sceneDurationMs"] / 1000, delta=0.1)

    def test_first_frame_is_blank_paper(self):
        first = read_frames(self.video)[0].astype(int)
        self.assertLess(first.std(axis=(0, 1)).max(), 3.0)
        self.assertLess(np.abs(first.mean(axis=(0, 1)) - np.array(PAPER_BGR)).max(), 6)

    def test_later_region_hidden_while_first_draws(self):
        frames = read_frames(self.video)
        mid_first = frames[int(1.0 * 20)]  # giữa lúc vẽ "Người", mặt trời chưa được lộ
        sun = mid_first[int(120 * 0.5) - 5:int(120 * 0.5) + 5, int(470 * 0.5) - 5:int(470 * 0.5) + 5].astype(int)
        self.assertLess(np.abs(sun.mean(axis=(0, 1)) - np.array(PAPER_BGR)).max(), 12)

    def test_last_frame_shows_full_image(self):
        last = read_frames(self.video)[-1].astype(int)
        src = cv2.resize(cv2.imread(str(self.image)), (320, 180), interpolation=cv2.INTER_AREA).astype(int)
        self.assertLess(np.abs(last - src).mean(), 6)


class ValidateTest(unittest.TestCase):
    def test_good_scene_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _, annotation, _ = make_scene(Path(d))
            code, out, _ = quiet(validate_annotation.main, [str(annotation)])
            self.assertEqual(code, 0, out)
            self.assertIn("[ổn]", out)

    def test_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            _, annotation, ann = make_scene(Path(d))
            els = ann["elements"]
            els[0]["region"]["x"] = 80.5                     # không phải số nguyên
            els[1]["region"]["width"] = 400                  # vượt ra ngoài ảnh
            els[2]["sequence"] = 1                           # sequence trùng
            ann["canvas"]["width"] = 641                     # khác kích thước ảnh
            annotation.write_text(json.dumps(ann), encoding="utf-8")
            code, out, _ = quiet(validate_annotation.main, [str(annotation)])
            self.assertEqual(code, 1)
            self.assertIn("số nguyên", out)
            self.assertIn("vượt ra ngoài ảnh", out)
            self.assertIn("khác kích thước ảnh", out)

    def test_warnings_for_overlap_uncovered_and_sequence_order(self):
        with tempfile.TemporaryDirectory() as d:
            _, annotation, ann = make_scene(Path(d))
            els = ann["elements"]
            els[1]["reveal"]["startMs"] = 900                # chồng thời gian với vùng 1
            els[1]["region"] = {"x": 420, "y": 70, "width": 40, "height": 40}  # mặt trời bị cắt → nét ngoài vùng
            ann["sceneDurationMs"] = 3000                    # vẽ xong 3100 ms + 0,5 s > 3000
            annotation.write_text(json.dumps(ann), encoding="utf-8")
            code, out, _ = quiet(validate_annotation.main, [str(annotation)])
            self.assertEqual(code, 0, out)
            self.assertIn("chỉ có một cây bút", out)
            self.assertIn("không thuộc phạm vi vẽ", out)
            self.assertIn("dài hơn khai báo", out)

            els[0]["reveal"]["startMs"], els[1]["reveal"]["startMs"] = 1500, 100  # sequence ≠ thứ tự vẽ
            annotation.write_text(json.dumps(ann), encoding="utf-8")
            code, out, _ = quiet(validate_annotation.main, [str(annotation)])
            self.assertEqual(code, 1)
            self.assertIn("khác thứ tự vẽ thật", out)

    def test_srt_timing_checks(self):
        with tempfile.TemporaryDirectory() as d:
            _, annotation, _ = make_scene(Path(d))
            srt = Path(d) / "input.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,300\nCâu một.\n\n"
                           "2\n00:00:01,300 --> 00:00:02,500\nCâu hai.\n\n"
                           "3\n00:00:02,500 --> 00:00:09,000\nCâu ba.\n", encoding="utf-8")
            code, out, _ = quiet(validate_annotation.main, [str(annotation), "--srt", str(srt)])
            self.assertEqual(code, 0)
            self.assertIn("khác thời lượng phụ đề", out)  # cảnh 4 s nhưng SRT dài 9 s


class AnnotationPreviewTest(unittest.TestCase):
    def test_renders_on_any_os(self):
        with tempfile.TemporaryDirectory() as d:
            image, annotation, _ = make_scene(Path(d))
            out = Path(d) / "regions.png"
            code, stdout, err = quiet(render_annotation_preview.main, [str(image), str(annotation), str(out)])
            self.assertEqual(code, 0, err)
            self.assertEqual(cv2.imread(str(out)).shape[:2], (H, W))


@unittest.skipUnless(HAS_FFMPEG, "cần ffmpeg")
class SubtitleAndMergeTest(unittest.TestCase):
    def test_burn_and_merge_with_awkward_paths(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "dự án, [thử]'s"
            folder.mkdir()
            clip = folder / "a.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xF5EBD7:s=320x180:d=1.5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
            shutil.copy(clip, folder / "b.mp4")
            merged = folder / "ab.mp4"
            code, out, err = quiet(merge_scenes.main, ["--inputs", str(clip), str(folder / "b.mp4"),
                                                       "--output", str(merged)])
            self.assertEqual(code, 0, err + out)
            srt = folder / "phụ đề.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:02,500\nXin chào, thế giới!\n", encoding="utf-8")
            subbed = folder / "ab-sub.mp4"
            code, out, err = quiet(burn_subtitles.main, [str(merged), str(srt), "--output", str(subbed)])
            self.assertEqual(code, 0, err + out)
            frame = read_frames(subbed)[10]
            bottom = frame[140:175, 40:280].astype(int)
            self.assertGreater(np.abs(bottom - np.array([215, 235, 245])).max(), 80)  # có chữ ở đáy khung

    def test_ass_colour_format(self):
        self.assertEqual(burn_subtitles.ass_color("#F5EBD7", 0x10), "&H10D7EBF5")
        self.assertEqual(burn_subtitles.ass_color("303030"), "&H00303030")
        with self.assertRaises(ValueError):
            burn_subtitles.ass_color("#12345")


if __name__ == "__main__":
    unittest.main()
