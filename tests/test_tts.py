"""Test cho tts_narration.py: đọc .env, an toàn của atempo/khung giờ, và engine ElevenLabs
chạy với một máy chủ giả lập API (không cần key thật, không tốn ký tự).

Chạy: .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import tts_narration as tts  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
VOICE_ID = "AbCdEfGhIjKlMnOpQrSt"


class DotenvTest(unittest.TestCase):
    def test_inline_comments_and_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text(
                "CIT_VOICE=Hải Đăng      # tên giọng\n"
                "CIT_API_KEY=            # chỉ cần khi gọi từ máy khác\n"
                'QUOTED="abc#123"  # ghi chú\n'
                "export EXPORTED=1\n"
                "HASH_NO_SPACE=a#b\n"
                "# CHU_THICH=1\n",
                encoding="utf-8",
            )
            keys = ["CIT_VOICE", "CIT_API_KEY", "QUOTED", "EXPORTED", "HASH_NO_SPACE", "CHU_THICH"]
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in keys:
                    os.environ.pop(k, None)
                tts.load_dotenv(env)
                self.assertEqual(os.environ["CIT_VOICE"], "Hải Đăng")
                self.assertEqual(os.environ["CIT_API_KEY"], "")
                self.assertEqual(os.environ["QUOTED"], "abc#123")
                self.assertEqual(os.environ["EXPORTED"], "1")
                self.assertEqual(os.environ["HASH_NO_SPACE"], "a#b")
                self.assertNotIn("CHU_THICH", os.environ)


class AtempoAndSlotTest(unittest.TestCase):
    def test_atempo_rejects_non_positive(self):
        for bad in (-120.0, 0.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                tts._atempo_chain(bad)

    def test_atempo_chains_out_of_range(self):
        self.assertEqual(tts._atempo_chain(3.0), "atempo=2.0,atempo=1.5000")
        self.assertEqual(tts._atempo_chain(0.3), "atempo=0.5,atempo=0.6000")

    def test_overlapping_cues_raise_instead_of_hanging(self):
        cues = [{"index": 1, "startMs": 0, "endMs": 1000, "text": "a"},
                {"index": 2, "startMs": 20, "endMs": 2000, "text": "b"}]
        with self.assertRaises(tts.SlotError):
            tts.build_track(cues, [Path("x.wav"), Path("y.wav")], Path("o.m4a"), None)


# ──────────────────────────────────────────────────────────────
# Máy chủ giả lập ElevenLabs
# ──────────────────────────────────────────────────────────────
class FakeEleven:
    def __init__(self, mp3: bytes, reject_context: bool = False, rate_limit_first: bool = True):
        self.mp3 = mp3
        self.reject_context = reject_context
        self.rate_limit_left = 1 if rate_limit_first else 0
        self.tts_bodies: list[dict] = []
        self.keys: set[str] = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # im lặng
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data):
                self._send(code, json.dumps(data).encode())

            def do_GET(self):
                fake.keys.add(self.headers.get("xi-api-key", ""))
                url = urlparse(self.path)
                if url.path == "/v1/models":
                    return self._json(200, [
                        {"model_id": "eleven_v3", "name": "Eleven v3", "can_do_text_to_speech": True,
                         "languages": [{"language_id": "en", "name": "English"},
                                       {"language_id": "vi", "name": "Vietnamese"}]},
                        {"model_id": "eleven_multilingual_v2", "can_do_text_to_speech": True,
                         "languages": [{"language_id": "en", "name": "English"}]},
                    ])
                if url.path == "/v2/voices":
                    search = (parse_qs(url.query).get("search") or [""])[0].lower()
                    voices = [{"voice_id": VOICE_ID, "name": "Linh", "category": "cloned",
                               "verified_languages": [{"language": "vi"}]},
                              {"voice_id": "ZzZzZzZzZzZzZzZzZzZz", "name": "George", "category": "premade"}]
                    return self._json(200, {"voices": [v for v in voices if search in v["name"].lower()],
                                            "has_more": False, "total_count": 2})
                if url.path == f"/v1/voices/{VOICE_ID}":
                    return self._json(200, {"voice_id": VOICE_ID, "name": "Linh"})
                return self._json(404, {"detail": {"status": "not_found", "message": "not found"}})

            def do_POST(self):
                fake.keys.add(self.headers.get("xi-api-key", ""))
                url = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not url.path.startswith("/v1/text-to-speech/"):
                    return self._json(404, {"detail": "not found"})
                if fake.rate_limit_left:
                    fake.rate_limit_left -= 1
                    return self._send(429, b'{"detail":{"status":"too_many_concurrent_requests","message":"slow down"}}',
                                      headers={"Retry-After": "0"})
                if fake.reject_context and ("previous_text" in body or "next_text" in body):
                    return self._json(400, {"detail": {"status": "invalid_request",
                                                       "message": "previous_text is not supported for this model"}})
                assert parse_qs(url.query)["output_format"] == [tts.ELEVEN_OUTPUT_FORMAT]
                fake.tts_bodies.append(body)
                return self._send(200, fake.mp3, "audio/mpeg")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


SRT = """1
00:00:00,000 --> 00:00:02,000
Cho đi rồi, liệu có nhận lại không?

2
00:00:02,000 --> 00:00:04,000
Chiều ấy, Nam thấy một cậu bé.

3
00:00:04,000 --> 00:00:06,000
Anh đưa cậu chiếc ô duy nhất.
"""


@unittest.skipUnless(HAS_FFMPEG, "cần ffmpeg")
class ElevenLabsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tone = Path(cls.tmp.name) / "tone.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                        "-c:a", "libmp3lame", str(tone)], check=True)
        cls.mp3 = tone.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_main(self, fake: FakeEleven, workdir: Path, *extra: str, env: dict | None = None) -> tuple[int, str, str]:
        srt = workdir / "input.srt"
        srt.write_text(SRT, encoding="utf-8")
        environ = {
            "ELEVENLABS_API_KEY": "test-key", "ELEVENLABS_BASE_URL": fake.url,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            **(env or {}),
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ), mock.patch.object(tts.time, "sleep"), \
                redirect_stdout(out), redirect_stderr(err):
            code = tts.main([str(srt), "--output", str(workdir / "narration.m4a"),
                             "--provider", "elevenlabs", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_with_context_retry_and_cache(self):
        fake = FakeEleven(self.mp3)
        try:
            with tempfile.TemporaryDirectory() as d:
                work = Path(d)
                code, out, err = self.run_main(fake, work, "--voice", "Linh",
                                               "--retime-out", str(work / "tight.srt"))
                self.assertEqual(code, 0, err + out)
                self.assertTrue((work / "narration.m4a").stat().st_size > 1000)
                self.assertIn("HTTP 429", out)  # đã thử lại sau 429
                self.assertEqual(len(fake.tts_bodies), 3)
                first, middle, last = fake.tts_bodies
                self.assertEqual(middle["previous_text"], "Cho đi rồi, liệu có nhận lại không?")
                self.assertEqual(middle["next_text"], "Anh đưa cậu chiếc ô duy nhất.")
                self.assertNotIn("previous_text", first)
                self.assertNotIn("next_text", last)
                self.assertEqual(middle["model_id"], "eleven_v3")
                self.assertEqual(middle["language_code"], "vi")
                self.assertEqual(fake.keys, {"test-key"})
                self.assertTrue((work / "tight.srt").exists())

                # Lần hai: lấy hết từ cache, không gọi API tạo giọng
                code, out, _ = self.run_main(fake, work, "--voice", VOICE_ID)
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.tts_bodies), 3)
                self.assertIn("cần tạo 0 câu", out)
        finally:
            fake.close()

    def test_model_without_context_support_falls_back(self):
        fake = FakeEleven(self.mp3, reject_context=True, rate_limit_first=False)
        try:
            with tempfile.TemporaryDirectory() as d:
                code, out, err = self.run_main(fake, Path(d), "--voice", "Linh")
                self.assertEqual(code, 0, err + out)
                self.assertIn("không nhận", out)
                self.assertTrue(all("previous_text" not in b and "next_text" not in b for b in fake.tts_bodies))
        finally:
            fake.close()

    def test_dry_run_counts_chars_without_synthesis(self):
        fake = FakeEleven(self.mp3, rate_limit_first=False)
        try:
            with tempfile.TemporaryDirectory() as d:
                code, out, _ = self.run_main(fake, Path(d), "--voice", "Linh", "--dry-run")
                self.assertEqual(code, 0)
                self.assertEqual(fake.tts_bodies, [])
                total = sum(len(c["text"]) for c in tts.parse_srt(SRT))
                self.assertIn(f"CHARS={total}", out)
        finally:
            fake.close()

    def test_unknown_voice_and_missing_key(self):
        fake = FakeEleven(self.mp3, rate_limit_first=False)
        try:
            with tempfile.TemporaryDirectory() as d:
                code, _, err = self.run_main(fake, Path(d), "--voice", "Không Có")
                self.assertEqual(code, 1)
                self.assertIn("Không tìm thấy giọng", err)
                code, _, err = self.run_main(fake, Path(d), "--voice", "Linh", env={"ELEVENLABS_API_KEY": ""})
                self.assertEqual(code, 1)
                self.assertIn("Thiếu ELEVENLABS_API_KEY", err)
        finally:
            fake.close()

    def test_warns_when_model_lacks_vietnamese(self):
        fake = FakeEleven(self.mp3, rate_limit_first=False)
        try:
            with tempfile.TemporaryDirectory() as d:
                code, out, err = self.run_main(fake, Path(d), "--voice", "Linh", "--dry-run",
                                               env={"ELEVENLABS_MODEL": "eleven_multilingual_v2"})
                self.assertEqual(code, 0, err)
                self.assertIn("không có tiếng Việt", out)
                self.assertIn("eleven_v3", out)
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
