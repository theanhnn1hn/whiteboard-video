#!/usr/bin/env python3
"""
Giọng đọc cho SRT: đọc từng câu phụ đề bằng TTS, xếp theo mốc thời gian thành một
track lời dẫn, rồi (tuỳ chọn) ghép thẳng vào MP4 (không mã hoá lại hình).

Bốn engine (--provider; mặc định theo TTS_PROVIDER trong .env, không đặt thì cit):
  cit         CIT Voice Studio chạy offline trên máy (mặc định http://127.0.0.1:8001),
              POST /api/tts/generate → WAV. Miễn phí; phải mở ứng dụng trước.
              --voice là tên giọng trong GET /voices (vd "Hải Đăng").
  elevenlabs  ElevenLabs API (trả phí theo ký tự). Cần ELEVENLABS_API_KEY.
              --voice là voice_id hoặc tên giọng trong tài khoản (tự tra GET /v2/voices).
              Mỗi câu gửi kèm câu trước/sau (previous_text/next_text) để ngữ điệu liền mạch.
  vbee        Vbee API: POST → request_id → hỏi lại → tải mp3. Cần VBEE_APP_ID / VBEE_ACCESS_TOKEN,
              trả phí theo ký tự. --voice là voice_code copy từ giao diện Vbee.
  edge        rany2/edge-tts (Microsoft Edge đọc online, miễn phí), mặc định vi-VN-NamMinhNeural.
Kết quả mỗi câu được cache theo engine + giọng + tốc độ + văn bản, chạy lại không gọi API lần nữa.
Câu đọc dài hơn khung thời gian (tới câu sau) sẽ được tăng tốc bằng atempo và cảnh báo.

Giá trị mặc định đọc từ .env ở thư mục gốc / biến môi trường:
  TTS_PROVIDER (cit | elevenlabs | edge | vbee)
  Mỗi engine một nhóm <ENGINE>_VOICE, <ENGINE>_SPEED (1.0 = bình thường), cùng:
    CIT_BASE_URL, CIT_API_KEY
    ELEVENLABS_API_KEY, ELEVENLABS_MODEL, ELEVENLABS_LANGUAGE, ELEVENLABS_STABILITY,
    ELEVENLABS_SIMILARITY, ELEVENLABS_STYLE, ELEVENLABS_SPEAKER_BOOST, ELEVENLABS_SEED,
    ELEVENLABS_CONTEXT, ELEVENLABS_BASE_URL
    VBEE_APP_ID, VBEE_ACCESS_TOKEN
  TTS_VOICE / TTS_SPEED (bản cũ) vẫn được dùng làm dự phòng.

Dùng:
  <ENV_PY> tts_narration.py <phụ-đề.srt> --output narration.m4a [--video final.mp4 --video-out final-voice.mp4]
                            [--provider cit|elevenlabs|vbee|edge] [--voice <giọng>] [--speed 1.1]
                            [--retime-out tight.srt --gap 0.3 --pause 6=0.8 --tail 1.0] [--no-trim]
                            [--dry-run]
  <ENV_PY> tts_narration.py --list-voices [--provider ...]     # liệt kê giọng của engine
  <ENV_PY> tts_narration.py --list-models --provider elevenlabs
  --retime-out: sắp lại mốc theo độ dài giọng thật (bỏ khoảng trống), rồi dùng
  retime_annotations.py để đồng bộ annotation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_srt import parse_srt  # noqa: E402


PROVIDERS = ["cit", "elevenlabs", "vbee", "edge"]
SAMPLE_RATE = 44100
USER_AGENT = "whiteboard-video/1.0"

VBEE_API_URL = "https://vbee.vn/api/v1/tts"
VBEE_CALLBACK_URL = "https://example.com/callback"  # API bắt buộc có; ở chế độ hỏi lại thì dùng địa chỉ giữ chỗ
VBEE_POLL_INTERVAL_S = 2
VBEE_POLL_MAX = 30
VBEE_DEFAULT_VOICE = "n_hanoi_male_protrainer_education_vc"

EDGE_DEFAULT_VOICE = "vi-VN-NamMinhNeural"
EDGE_COMMA_TRIES = 4  # câu gốc lỗi thì thử tối đa ngần này biến thể có chèn dấu phẩy

CIT_DEFAULT_URL = "http://127.0.0.1:8001"
CIT_DEFAULT_VOICE = "Hải Đăng"  # mô hình không có giọng này (vd Supertonic) thì dùng giọng mặc định của mô hình
CIT_TIMEOUT_S = 1800  # câu dài chạy trên CPU có thể chậm

ELEVEN_DEFAULT_URL = "https://api.elevenlabs.io"
ELEVEN_DEFAULT_MODEL = "eleven_v3"
ELEVEN_DEFAULT_LANGUAGE = "vi"
ELEVEN_OUTPUT_FORMAT = "mp3_44100_128"  # dùng được ở mọi gói
ELEVEN_RETRIES = 4
ELEVEN_TIMEOUT_S = 180
ELEVEN_OPTIONAL_FIELDS = ("previous_text", "next_text", "language_code", "seed")

MIN_SLOT_S = 0.15           # khung của một câu ngắn hơn mức này = mốc trùng/chồng, không xếp được giọng
MAX_COMFORT_SPEEDUP = 1.35  # tăng tốc quá mức này thì cảnh báo "khó nghe"
# Khoảng --speed cho phép của từng engine
SPEED_RANGES = {
    "cit": (0.75, 1.5),
    "elevenlabs": (0.5, 2.0),  # tạo ở tốc độ gốc rồi chỉnh bằng atempo (giữ cao độ)
    "edge": (0.5, 2.0),        # như trên
    "vbee": (0.1, 1.9),
}


class SlotError(RuntimeError):
    """Khung thời gian của một câu quá ngắn (mốc phụ đề trùng/chồng)."""


class SetupError(RuntimeError):
    """Engine chưa sẵn sàng (thiếu key, chưa mở ứng dụng, sai tên giọng...)."""


# ──────────────────────────────────────────────────────────────
# .env
# ──────────────────────────────────────────────────────────────
def _parse_env_value(raw: str) -> str:
    """Giá trị trong ngoặc kép/đơn giữ nguyên; không có ngoặc thì bỏ ghi chú cuối dòng (`  # ...`)."""
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end > 0:
            return value[1:end]
    return re.sub(r"(^|\s+)#.*$", "", value).strip()


def load_dotenv(path: Path) -> None:
    """Đọc .env tối giản: KEY=VALUE, bỏ dòng/đoạn ghi chú; biến môi trường đã có được ưu tiên."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _parse_env_value(value))


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        raise SetupError(f"{name} phải là số (đang là {raw!r})") from None


def _env_bool(name: str) -> bool | None:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on", "có")


def _check_header_value(name: str, value: str | None) -> None:
    if value and not value.isascii():
        raise SetupError(f"{name} chứa ký tự không hợp lệ (có dấu hoặc dính ghi chú?). Kiểm tra dòng {name} trong .env.")


# ──────────────────────────────────────────────────────────────
# Vbee
# ──────────────────────────────────────────────────────────────
def _vbee_request(url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if body is not None else "GET",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def vbee_synthesize(text: str, app_id: str, token: str, voice: str, speed: float) -> str:
    """Gửi yêu cầu tạo giọng rồi hỏi lại tới khi xong, trả về audio_link."""
    data = _vbee_request(VBEE_API_URL, token, {
        "app_id": app_id,
        "input_text": text,
        "voice_code": voice,
        "audio_type": "mp3",
        "speed_rate": speed,
        "callback_url": VBEE_CALLBACK_URL,
    })
    if data.get("status") != 1:
        raise RuntimeError(f"Vbee báo lỗi: {data.get('error_message') or data.get('error_code')}")
    result = data.get("result") or {}
    if result.get("audio_link"):
        return result["audio_link"]
    request_id = result.get("request_id")
    if not request_id:
        raise RuntimeError("Vbee không trả về request_id")

    for _ in range(VBEE_POLL_MAX):
        time.sleep(VBEE_POLL_INTERVAL_S)
        try:
            status = _vbee_request(f"{VBEE_API_URL}/{request_id}", token)
        except urllib.error.URLError:
            continue
        if status.get("status") != 1:
            continue
        res = status.get("result") or {}
        if res.get("status") == "SUCCESS" and res.get("audio_link"):
            return res["audio_link"]
        if res.get("status") == "FAILURE":
            raise RuntimeError(f"Vbee tạo giọng thất bại: request_id={request_id}")
    raise RuntimeError(f"Vbee quá thời gian chờ ({VBEE_POLL_INTERVAL_S * VBEE_POLL_MAX}s): request_id={request_id}")


# ──────────────────────────────────────────────────────────────
# CIT Voice Studio
# ──────────────────────────────────────────────────────────────
class CitClient:
    """API cục bộ của CIT Voice Studio (tài liệu: <CIT_BASE_URL>/cit-voice-studio.md)."""

    def __init__(self, base_url: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, path: str, body: dict | None = None, timeout: float = 10):
        headers = {"User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            method="POST" if body is not None else "GET",
            headers=headers,
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"CIT HTTP {e.code} {path}: {detail}") from None

    def health(self) -> dict:
        with self._request("/health") as resp:
            return json.loads(resp.read().decode("utf-8"))

    def voices(self) -> list[dict]:
        with self._request("/voices") as resp:
            return json.loads(resp.read().decode("utf-8"))

    def synthesize(self, text: str, voice: str | None, speed: float, out: Path) -> None:
        """POST /api/tts/generate → WAV (có metadata đánh dấu do AI tạo, đừng xoá)."""
        body = {"text": text, "speed": speed, "format": "wav"}
        if voice:
            body["voice_id"] = voice
        with self._request("/api/tts/generate", body, timeout=CIT_TIMEOUT_S) as resp:
            data = resp.read()
        tmp = out.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(out)


# ──────────────────────────────────────────────────────────────
# Edge TTS
# ──────────────────────────────────────────────────────────────
def _is_edge_voice(v: str) -> bool:
    return v.endswith("Neural")


def _is_vbee_voice(v: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)+", v))


def _edge_variants(text: str) -> list[str]:
    """Câu gốc ×2, rồi các biến thể chèn dấu phẩy giữa hai từ gần giữa câu (từ giữa ra hai bên)."""
    words = text.split(" ")
    mid = len(words) // 2
    order = sorted(range(1, len(words)), key=lambda k: abs(k - mid))
    commas = [" ".join(words[:k]) + ", " + " ".join(words[k:]) for k in order
              if not words[k - 1].endswith((",", ".", "!", "?", ":", ";"))]
    return [text, text] + commas[:EDGE_COMMA_TRIES]


def synthesize_edge(text: str, voice: str, speed: float, out: Path) -> None:
    """edge-tts tạo giọng ở tốc độ gốc, rồi chỉnh tốc độ bằng atempo của ffmpeg (giữ cao độ).

    Với một số câu tiếng Việt, máy chủ Edge luôn trả NoAudioReceived (hay gặp hơn khi có rate);
    không liên quan mạng, thử lại vô ích, nhưng chèn một dấu phẩy giữa câu là đọc được. Vì vậy:
    không truyền rate; thử câu gốc 2 lần, vẫn lỗi thì lần lượt chèn dấu phẩy gần giữa câu
    (chỉ thêm một chỗ ngắt nhẹ, nội dung không đổi).
    """
    import edge_tts

    raw = out.with_suffix(".raw.mp3")
    for variant in _edge_variants(text):
        try:
            edge_tts.Communicate(variant, voice).save_sync(str(raw))
        except edge_tts.exceptions.NoAudioReceived:
            time.sleep(1)
            continue
        if variant != text:
            print(f"  [cảnh báo] Edge không đọc được câu gốc, đã đổi thành: {variant}")
        break
    else:
        raise RuntimeError(f"Edge nhiều lần trả NoAudioReceived: {text}")
    apply_speed(raw, out, speed)


# ──────────────────────────────────────────────────────────────
# ElevenLabs
# ──────────────────────────────────────────────────────────────
class ElevenLabsError(RuntimeError):
    def __init__(self, code: int, status: str, message: str):
        self.code, self.status, self.message = code, status or "", message or ""
        super().__init__(self.friendly())

    def friendly(self) -> str:
        msg = self.message.strip()
        if self.status == "quota_exceeded":
            return f"Tài khoản ElevenLabs đã hết ký tự (quota). Nạp thêm, hoặc dùng --provider cit/edge. ({msg})"
        if self.code == 401:
            return ("API key ElevenLabs không hợp lệ hoặc thiếu quyền (cần quyền Text to Speech và Voices: Read). "
                    f"Kiểm tra ELEVENLABS_API_KEY trong .env. ({msg or self.status})")
        if self.status in ("voice_not_found",) or (self.code == 404 and "voice" in msg.lower()):
            return f"Không tìm thấy giọng ElevenLabs. Xem danh sách: tts_narration.py --list-voices --provider elevenlabs. ({msg})"
        if self.code == 0:
            return f"Không kết nối được ElevenLabs: {msg}"
        return f"ElevenLabs HTTP {self.code} {self.status}: {msg}".strip()


def _eleven_detail(body: str) -> tuple[str, str]:
    """Rút (status, message) từ body lỗi dạng {"detail": {...}} hoặc {"detail": "..."}."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "", body[:300]
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if isinstance(detail, dict):
        return str(detail.get("status") or detail.get("code") or ""), str(detail.get("message") or detail)
    if isinstance(detail, list):  # lỗi validate kiểu FastAPI
        parts = [f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail if isinstance(d, dict)]
        return "validation_error", "; ".join(parts) or str(detail)
    return "", str(detail)


def _is_vietnamese(lang: dict) -> bool:
    lid = str(lang.get("language_id", "")).lower()
    return lid in ("vi", "vie", "vi-vn") or "vietnam" in str(lang.get("name", "")).lower()


class ElevenLabsClient:
    """Gọi ElevenLabs REST API bằng thư viện chuẩn (không cần cài SDK)."""

    def __init__(self, api_key: str, base_url: str = ELEVEN_DEFAULT_URL, use_context: bool = True):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.use_context = use_context
        self.disabled_fields: set[str] = set()  # trường tuỳ chọn mà model đang dùng không nhận

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 accept: str = "application/json", timeout: float = 30) -> bytes:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url = self.base_url + path + (f"?{query}" if query else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"xi-api-key": self.api_key, "User-Agent": USER_AGENT, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(ELEVEN_RETRIES + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                status, message = _eleven_detail(e.read().decode("utf-8", "replace"))
                retryable = (e.code == 429 or e.code >= 500) and status != "quota_exceeded"
                if retryable and attempt < ELEVEN_RETRIES:
                    wait = _retry_after(e.headers)
                    wait = 2.0 ** (attempt + 1) if wait is None else wait
                    print(f"  [chờ] ElevenLabs trả HTTP {e.code} {status}, thử lại sau {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                raise ElevenLabsError(e.code, status, message) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < ELEVEN_RETRIES:
                    wait = 2.0 ** (attempt + 1)
                    print(f"  [chờ] Lỗi mạng khi gọi ElevenLabs ({getattr(e, 'reason', e)}), thử lại sau {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                raise ElevenLabsError(0, "network", str(getattr(e, "reason", e))) from None
        raise ElevenLabsError(0, "network", "hết số lần thử")  # không tới được

    def _json(self, method: str, path: str, **kw):
        return json.loads(self._request(method, path, **kw).decode("utf-8"))

    def models(self) -> list[dict]:
        return self._json("GET", "/v1/models")

    def voices(self, search: str | None = None, max_pages: int = 10) -> list[dict]:
        out: list[dict] = []
        token = None
        for _ in range(max_pages):
            page = self._json("GET", "/v2/voices",
                              params={"page_size": 100, "search": search, "next_page_token": token})
            out.extend(page.get("voices") or [])
            token = page.get("next_page_token")
            if not page.get("has_more") or not token:
                break
        return out

    def resolve_voice(self, value: str) -> tuple[str, str]:
        """Nhận voice_id (20 ký tự) hoặc tên giọng trong tài khoản → (voice_id, tên)."""
        if re.fullmatch(r"[A-Za-z0-9]{20}", value):
            try:
                v = self._json("GET", f"/v1/voices/{value}")
                return value, v.get("name") or value
            except ElevenLabsError as e:
                if e.code == 401 and e.status != "quota_exceeded":
                    print("  [cảnh báo] Key không có quyền đọc giọng; dùng thẳng voice_id đã nhập.")
                    return value, value
                if e.code not in (400, 404):
                    raise
        voices = self.voices(search=value)
        key = value.casefold()
        exact = [v for v in voices if (v.get("name") or "").casefold() == key]
        prefix = [v for v in voices if (v.get("name") or "").casefold().startswith(key)]
        pick = exact or (prefix if len(prefix) == 1 else [])
        if pick:
            if len(pick) > 1:
                print(f"  [cảnh báo] Có {len(pick)} giọng tên {value!r}, dùng giọng đầu tiên ({pick[0]['voice_id']}).")
            return pick[0]["voice_id"], pick[0].get("name") or value
        hint = ", ".join(f"{v.get('name')} ({v.get('voice_id')})" for v in (prefix or voices)[:8])
        raise ElevenLabsError(404, "voice_not_found",
                              f"không có giọng {value!r} trong tài khoản"
                              + (f"; gần giống: {hint}" if hint else "")
                              + ". Giọng trong Voice Library phải được thêm vào My Voices trước.")

    def check_model(self, model_id: str) -> dict | None:
        """Kiểm tra model có tồn tại, đọc được văn bản và có tiếng Việt. Không gọi được thì chỉ cảnh báo."""
        try:
            models = self.models()
        except ElevenLabsError as e:
            if e.code == 401 and e.status != "quota_exceeded":
                raise
            print(f"  [cảnh báo] Không kiểm tra được model: {e}")
            return None
        tts = [m for m in models if m.get("can_do_text_to_speech") is not False]
        model = next((m for m in models if m.get("model_id") == model_id), None)
        if model is None or model.get("can_do_text_to_speech") is False:
            names = ", ".join(m.get("model_id", "?") for m in tts)
            raise SetupError(f"Model ElevenLabs {model_id!r} không tồn tại hoặc không đọc được văn bản. Model đọc được: {names}")
        langs = model.get("languages") or []
        if langs and not any(_is_vietnamese(lang) for lang in langs):
            vi_models = ", ".join(m["model_id"] for m in tts if any(_is_vietnamese(x) for x in m.get("languages") or []))
            print(f"  [cảnh báo] Model {model_id} không có tiếng Việt trong danh sách ngôn ngữ; giọng có thể đọc sai."
                  + (f" Model có tiếng Việt: {vi_models}" if vi_models else ""))
        return model

    def synthesize(self, text: str, voice_id: str, out: Path, *, model: str, language: str | None = None,
                   settings: dict | None = None, seed: int | None = None,
                   previous_text: str | None = None, next_text: str | None = None) -> None:
        body: dict = {"text": text, "model_id": model}
        optional = {
            "language_code": language if not model.startswith("eleven_multilingual_v2") else None,
            "seed": seed,
            "previous_text": previous_text if self.use_context else None,
            "next_text": next_text if self.use_context else None,
        }
        for k, v in optional.items():
            if v not in (None, "") and k not in self.disabled_fields:
                body[k] = v
        if settings:
            body["voice_settings"] = settings
        try:
            data = self._request("POST", f"/v1/text-to-speech/{urllib.parse.quote(voice_id)}",
                                 params={"output_format": ELEVEN_OUTPUT_FORMAT}, body=body,
                                 accept="audio/mpeg", timeout=ELEVEN_TIMEOUT_S)
        except ElevenLabsError as e:
            # Model không nhận một trường tuỳ chọn → bỏ trường đó cho các câu sau rồi thử lại.
            # previous_text/next_text đi theo cặp: lỗi nhắc một trường thì bỏ cả hai.
            mentioned = {f for f in ELEVEN_OPTIONAL_FIELDS if f in e.message} if e.code in (400, 422) else set()
            if mentioned & {"previous_text", "next_text"}:
                mentioned |= {"previous_text", "next_text"}
            rejected = sorted(f for f in mentioned if f in body)
            if rejected:
                self.disabled_fields.update(mentioned)
                print(f"  [cảnh báo] Model {model} không nhận {', '.join(rejected)}; bỏ các trường này và thử lại.")
                return self.synthesize(text, voice_id, out, model=model, language=language, settings=settings,
                                       seed=seed, previous_text=previous_text, next_text=next_text)
            raise
        if len(data) < 64 or data[:1] in (b"{", b"<"):
            raise ElevenLabsError(0, "bad_response", f"phản hồi không phải âm thanh: {data[:200]!r}")
        tmp = out.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(out)


def _retry_after(headers) -> float | None:
    if not headers:
        return None
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw:
            try:
                return max(0.0, float(raw) * scale)
            except ValueError:
                return None
    return None


def eleven_settings_from_env() -> dict:
    settings: dict = {}
    for env, key in (("ELEVENLABS_STABILITY", "stability"), ("ELEVENLABS_SIMILARITY", "similarity_boost"),
                     ("ELEVENLABS_STYLE", "style")):
        value = _env_float(env)
        if value is not None:
            if not 0.0 <= value <= 1.0:
                raise SetupError(f"{env} phải trong khoảng 0–1 (đang là {value})")
            settings[key] = value
    boost = _env_bool("ELEVENLABS_SPEAKER_BOOST")
    if boost is not None:
        settings["use_speaker_boost"] = boost
    return settings


# ──────────────────────────────────────────────────────────────
# Xử lý âm thanh
# ──────────────────────────────────────────────────────────────
def download(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        tmp = out.with_suffix(".part")
        tmp.write_bytes(resp.read())
        tmp.replace(out)


def probe_duration(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(res.stdout.strip())


def trim_silence(clip: Path) -> Path:
    """Cắt khoảng lặng đầu/cuối mỗi câu (Edge thường lặng ~0,25 s đầu + 0,8 s cuối), chừa 50/100 ms;
    kết quả cache thành .trim.wav. File gốc mới hơn .trim.wav (vừa tạo lại) thì cắt lại."""
    out = clip.with_suffix(".trim.wav")
    if not out.exists() or out.stat().st_mtime < clip.stat().st_mtime:
        sr = "silenceremove=start_periods=1:start_threshold=-40dB:start_silence={}"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip),
             "-af", f"{sr.format(0.05)},areverse,{sr.format(0.1)},areverse",
             "-ar", str(SAMPLE_RATE), "-ac", "1", str(out)],
            check=True,
        )
    return out


def _atempo_chain(factor: float) -> str:
    """atempo mỗi tầng chỉ nhận 0.5–2.0, vượt thì nối nhiều tầng. factor phải > 0 (nếu không vòng lặp không dừng)."""
    if not factor > 0 or factor == float("inf"):
        raise ValueError(f"hệ số atempo không hợp lệ: {factor}")
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def apply_speed(raw: Path, out: Path, speed: float) -> None:
    """Đổi tốc độ bằng atempo (giữ cao độ); speed = 1 thì chỉ đổi tên file."""
    if abs(speed - 1.0) < 1e-3:
        raw.replace(out)
        return
    tmp = out.with_suffix(".part.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-af", _atempo_chain(speed),
         "-c:a", "libmp3lame", "-q:a", "2", str(tmp)],
        check=True,
    )
    raw.unlink(missing_ok=True)
    tmp.replace(out)


def build_track(cues: list[dict], clips: list[Path], output: Path, total_ms: int | None) -> None:
    """Mỗi câu nằm trong khung [mốc câu này, mốc câu sau): dài quá thì tăng tốc, thiếu thì đệm lặng, rồi nối lại."""
    filters: list[str] = []
    labels: list[str] = []
    inputs: list[str] = []
    first_start = cues[0]["startMs"]
    if first_start > 0:
        filters.append(f"aevalsrc=0:d={first_start / 1000:.3f}:s={SAMPLE_RATE}[lead]")
        labels.append("[lead]")

    for i, (cue, clip) in enumerate(zip(cues, clips)):
        if i + 1 < len(cues):
            slot_ms = cues[i + 1]["startMs"] - cue["startMs"]
        else:
            slot_ms = max(cue["endMs"], total_ms or 0) - cue["startMs"]
        slot_s = slot_ms / 1000
        if slot_s < MIN_SLOT_S:
            nxt = cues[i + 1]["index"] if i + 1 < len(cues) else "cuối"
            raise SlotError(
                f"Câu {cue['index']} chỉ có khung {slot_s * 1000:.0f} ms (tới câu {nxt}): mốc phụ đề bị trùng "
                f"hoặc chồng nhau nên không xếp được giọng đọc. Sửa mốc trong SRT, hoặc chạy thêm "
                f"--retime-out <ra.srt> để sắp lại mốc theo độ dài giọng thật."
            )
        dur = probe_duration(clip)
        chain = f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono"
        if dur > slot_s - 0.05:
            factor = dur / (slot_s - 0.05)
            hint = "  → khó nghe; nên dùng --retime-out hoặc rút gọn câu" if factor > MAX_COMFORT_SPEEDUP else ""
            print(f"  [cảnh báo] Câu {cue['index']}: giọng dài {dur:.2f}s, vượt khung {slot_s:.2f}s, "
                  f"tăng tốc x{factor:.2f}{hint}")
            chain += "," + _atempo_chain(factor)
        chain += f",apad,atrim=0:{slot_s:.3f}[c{i}]"
        inputs += ["-i", str(clip)]
        filters.append(chain)
        labels.append(f"[c{i}]")

    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(filters), "-map", "[out]",
         "-c:a", "aac", "-b:a", "192k", str(output)],
        check=True,
    )


def retime_cues(cues: list[dict], clips: list[Path], gap_s: float, pauses: dict[int, float],
                tail_s: float) -> list[dict]:
    """Sắp lại mốc theo độ dài giọng thật: câu sau nối ngay câu trước + gap (câu chỉ định nghỉ lâu hơn), câu cuối giữ thêm tail."""
    out: list[dict] = []
    cursor = cues[0]["startMs"]
    for i, (cue, clip) in enumerate(zip(cues, clips)):
        dur_ms = round(probe_duration(clip) * 1000)
        end = cursor + dur_ms
        is_last = i + 1 == len(cues)
        hold_ms = round((tail_s if is_last else pauses.get(cue["index"], gap_s)) * 1000)
        out.append({**cue, "startMs": cursor, "endMs": end + (hold_ms if is_last else 0),
                    "durMs": dur_ms})
        cursor = end + hold_ms
    return out


def _fmt_srt_time(ms: int) -> str:
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[dict], path: Path) -> None:
    blocks = [f"{c['index']}\n{_fmt_srt_time(c['startMs'])} --> {_fmt_srt_time(c['endMs'])}\n{c['text']}\n"
              for c in cues]
    path.write_text("\n".join(blocks), encoding="utf-8")


def mux(video: Path, audio: Path, output: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-t", f"{probe_duration(video):.3f}", "-movflags", "+faststart", str(output)],
        check=True,
    )


# ──────────────────────────────────────────────────────────────
# Chuẩn bị engine
# ──────────────────────────────────────────────────────────────
SynthFn = Callable[[str, Path, "str | None", "str | None"], None]  # (text, out, câu trước, câu sau)


def _env_voice(provider: str) -> str:
    """<ENGINE>_VOICE; TTS_VOICE (bản cũ) chỉ dùng khi đúng định dạng của engine."""
    legacy = os.environ.get("TTS_VOICE") or ""
    legacy_ok = {
        "cit": bool(legacy) and not _is_edge_voice(legacy) and not _is_vbee_voice(legacy),
        "edge": _is_edge_voice(legacy),
        "vbee": _is_vbee_voice(legacy),
        "elevenlabs": False,
    }[provider]
    return os.environ.get(f"{provider.upper()}_VOICE") or (legacy if legacy_ok else "")


def _cit_client() -> CitClient:
    key = os.environ.get("CIT_API_KEY") or None
    _check_header_value("CIT_API_KEY", key)
    return CitClient(os.environ.get("CIT_BASE_URL") or CIT_DEFAULT_URL, key)


def _eleven_client() -> ElevenLabsClient:
    key = os.environ.get("ELEVENLABS_API_KEY") or ""
    if not key:
        raise SetupError("Thiếu ELEVENLABS_API_KEY (.env hoặc biến môi trường). Tạo key ở elevenlabs.io → "
                         "Developers → API Keys, cấp quyền Text to Speech và Voices: Read.")
    _check_header_value("ELEVENLABS_API_KEY", key)
    context = _env_bool("ELEVENLABS_CONTEXT")
    return ElevenLabsClient(key, os.environ.get("ELEVENLABS_BASE_URL") or ELEVEN_DEFAULT_URL,
                            use_context=True if context is None else context)


def setup_provider(provider: str, voice_arg: str | None, speed: float) -> tuple[SynthFn, str, str, str]:
    """Trả về (hàm tạo giọng, khoá cache của giọng, đuôi file, tên giọng hiển thị)."""
    env_voice = _env_voice(provider)

    if provider == "cit":
        cit = _cit_client()
        try:
            health = cit.health()
            names = [v.get("id") for v in cit.voices()]
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            raise SetupError(f"Không kết nối được CIT Voice Studio ({cit.base_url}): {e}\n"
                             "      Hãy mở ứng dụng CIT Voice Studio, hoặc dùng --provider edge") from None
        if health.get("status") != "ok":
            raise SetupError(f"Mô hình CIT chưa sẵn sàng: {health.get('message', '')} (đợi /health báo \"ok\")")
        # Không chỉ định = Hải Đăng; mô hình không có thì dùng giọng mặc định của mô hình (đầu danh sách /voices)
        voice = voice_arg or env_voice or (CIT_DEFAULT_VOICE if CIT_DEFAULT_VOICE in names else None)
        if voice and voice not in names:
            raise SetupError(f"Mô hình CIT hiện tại không có giọng {voice!r}. Có: {', '.join(names)}")
        engine = health.get("engine", "")
        print(f"  CIT: {engine}, {health.get('sample_rate')} Hz, {health.get('device')}")
        label = voice or (names[0] if names else "default")
        # khoá cache kèm tên mô hình: đổi mô hình thì không dùng nhầm cache cũ
        return (lambda text, out, prev, nxt: cit.synthesize(text, voice, speed, out),
                f"{label}@{engine.split(' ')[0]}", ".wav", label)

    if provider == "elevenlabs":
        client = _eleven_client()
        model = os.environ.get("ELEVENLABS_MODEL") or ELEVEN_DEFAULT_MODEL
        language = os.environ.get("ELEVENLABS_LANGUAGE", ELEVEN_DEFAULT_LANGUAGE).strip() or None
        settings = eleven_settings_from_env()
        seed_raw = os.environ.get("ELEVENLABS_SEED") or ""
        if seed_raw and not seed_raw.isdigit():
            raise SetupError(f"ELEVENLABS_SEED phải là số nguyên ≥ 0 (đang là {seed_raw!r})")
        seed = int(seed_raw) if seed_raw else None
        voice_value = voice_arg or env_voice
        if not voice_value:
            try:
                sample = client.voices()[:12]
            except ElevenLabsError as e:
                raise SetupError(str(e)) from None
            listing = "\n".join(f"      {v.get('name')}: {v.get('voice_id')}" for v in sample)
            raise SetupError("Chưa chọn giọng ElevenLabs: đặt ELEVENLABS_VOICE trong .env hoặc dùng --voice "
                             "(voice_id hoặc tên giọng). Một số giọng trong tài khoản:\n" + listing)
        try:
            client.check_model(model)
            voice_id, voice_name = client.resolve_voice(voice_value)
        except ElevenLabsError as e:
            raise SetupError(str(e)) from None
        print(f"  ElevenLabs: model {model}, giọng {voice_name} ({voice_id}), ngôn ngữ {language or 'tự nhận'}"
              + (f", cài đặt {settings}" if settings else ""))

        def synth(text: str, out: Path, prev: str | None, nxt: str | None) -> None:
            raw = out.with_suffix(".raw.mp3")
            client.synthesize(text, voice_id, raw, model=model, language=language, settings=settings,
                              seed=seed, previous_text=prev, next_text=nxt)
            apply_speed(raw, out, speed)

        extras = json.dumps({"lang": language, "settings": settings, "seed": seed}, sort_keys=True)
        return synth, f"{voice_id}@{model}|{extras}", ".mp3", voice_name

    if provider == "vbee":
        app_id = os.environ.get("VBEE_APP_ID")
        token = os.environ.get("VBEE_ACCESS_TOKEN")
        if not app_id or not token:
            raise SetupError("Thiếu VBEE_APP_ID / VBEE_ACCESS_TOKEN (.env hoặc biến môi trường)")
        _check_header_value("VBEE_ACCESS_TOKEN", token)
        voice = voice_arg or env_voice or VBEE_DEFAULT_VOICE  # voice_code gửi nguyên văn cho API
        return (lambda text, out, prev, nxt: download(vbee_synthesize(text, app_id, token, voice, speed), out),
                voice, ".mp3", voice)

    try:
        import edge_tts  # noqa: F401
    except ImportError:
        raise SetupError("Thiếu thư viện edge-tts, hãy chạy python scripts/prepare_env.py") from None
    voice = voice_arg or env_voice or EDGE_DEFAULT_VOICE
    return (lambda text, out, prev, nxt: synthesize_edge(text, voice, speed, out), voice, ".mp3", voice)


# ──────────────────────────────────────────────────────────────
# Liệt kê giọng / model
# ──────────────────────────────────────────────────────────────
def list_voices(provider: str) -> None:
    if provider == "elevenlabs":
        voices = _eleven_client().voices()

        def has_vi(v: dict) -> bool:
            return any(str(x.get("language", "")).lower().startswith("vi") for x in v.get("verified_languages") or [])

        voices.sort(key=lambda v: (not has_vi(v), (v.get("name") or "").casefold()))
        print(f"{len(voices)} giọng ElevenLabs trong tài khoản (★ = đã kiểm định tiếng Việt):")
        for v in voices:
            labels = v.get("labels") or {}
            desc = ", ".join(str(labels[k]) for k in ("gender", "age", "accent") if labels.get(k))
            print(f"  {'★' if has_vi(v) else ' '} {v.get('name')}  |  {v.get('voice_id')}  |  "
                  f"{v.get('category', '')}  {desc}")
        print("Dùng: ELEVENLABS_VOICE=<voice_id hoặc tên> trong .env, hoặc --voice.")
    elif provider == "cit":
        for v in _cit_client().voices():
            print(f"  {v.get('id')}  {v.get('name', '') if v.get('name') != v.get('id') else ''}".rstrip())
    elif provider == "edge":
        import asyncio
        import edge_tts
        for v in asyncio.run(edge_tts.list_voices()):
            if v.get("Locale", "").startswith("vi-"):
                print(f"  {v['ShortName']}  ({v.get('Gender')})")
    else:
        print("Vbee không có API liệt kê giọng: copy mã giọng (voice_code) từ giao diện vbee.vn rồi đặt VBEE_VOICE.")


def list_models() -> None:
    models = _eleven_client().models()
    print("Model ElevenLabs đọc được văn bản (✓ = có tiếng Việt):")
    for m in models:
        if m.get("can_do_text_to_speech") is False:
            continue
        vi = any(_is_vietnamese(x) for x in m.get("languages") or [])
        limit = m.get("maximum_text_length_per_request") or m.get("max_characters_request_subscribed_user")
        print(f"  {'✓' if vi else ' '} {m.get('model_id'):<28} {m.get('name', '')}"
              + (f"  (tối đa {limit} ký tự/lần)" if limit else ""))


# ──────────────────────────────────────────────────────────────
# Chương trình chính
# ──────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    p = argparse.ArgumentParser(
        description="SRT → track giọng đọc (CIT Voice Studio / ElevenLabs / Vbee / Edge TTS), có thể ghép vào video")
    p.add_argument("srt", nargs="?", help="File phụ đề (.srt)")
    p.add_argument("--output", help="Track giọng đọc xuất ra (.m4a)")
    p.add_argument("--video", help="Video MP4 cần ghép giọng đọc")
    p.add_argument("--video-out", help="Video có giọng xuất ra (mặc định <video>-voice.mp4)")
    p.add_argument("--provider", choices=PROVIDERS,
                   default=(os.environ.get("TTS_PROVIDER") or "cit").strip().lower(),
                   help="Engine giọng đọc (mặc định TTS_PROVIDER trong .env, không đặt thì cit)")
    p.add_argument("--voice", help="Giọng: tên giọng CIT (vd 'Hải Đăng'), voice_id hoặc tên giọng ElevenLabs, "
                                   "voice_code Vbee, hoặc tên giọng Edge (vd vi-VN-HoaiMyNeural)")
    p.add_argument("--speed", type=float, default=None,
                   help="Tốc độ (1.0 = bình thường; mặc định đọc <ENGINE>_SPEED)")
    p.add_argument("--cache-dir", help="Thư mục cache từng câu (mặc định <thư-mục-srt>/tts-cache)")
    p.add_argument("--retime-out", help="Sắp lại mốc theo độ dài giọng thật, ghi SRT gọn (track cũng theo mốc mới)")
    p.add_argument("--gap", type=float, default=0.3, help="Khi sắp lại: nghỉ giữa hai câu, giây (mặc định 0.3)")
    p.add_argument("--pause", action="append", default=[], metavar="SỐ_CÂU=GIÂY",
                   help="Khi sắp lại: nghỉ lâu hơn sau một câu, dùng được nhiều lần, vd --pause 6=0.8")
    p.add_argument("--tail", type=float, default=1.0, help="Khi sắp lại: giữ thêm sau câu cuối, giây (mặc định 1.0)")
    p.add_argument("--no-trim", action="store_true", help="Không cắt khoảng lặng đầu/cuối mỗi câu")
    p.add_argument("--dry-run", action="store_true",
                   help="Chỉ đếm số câu/ký tự sẽ gửi tới engine (chưa có cache) rồi dừng, không tạo giọng")
    p.add_argument("--list-voices", action="store_true", help="Liệt kê giọng của engine rồi thoát")
    p.add_argument("--list-models", action="store_true", help="Liệt kê model ElevenLabs rồi thoát")
    args = p.parse_args(argv)

    try:
        if args.list_models:
            list_models()
            return 0
        if args.list_voices:
            list_voices(args.provider)
            return 0
    except (SetupError, ElevenLabsError, RuntimeError, OSError) as e:
        print(f"[lỗi] {e}", file=sys.stderr)
        return 1

    if not args.srt or not args.output:
        p.error("cần <phụ-đề.srt> và --output (trừ khi dùng --list-voices / --list-models)")
    try:
        pauses = {int(k): float(v) for k, v in (x.split("=", 1) for x in args.pause)}
    except ValueError:
        print("[lỗi] --pause phải có dạng SỐ_CÂU=GIÂY, vd 6=0.8", file=sys.stderr)
        return 1

    if args.speed is None:
        try:
            args.speed = float(os.environ.get(f"{args.provider.upper()}_SPEED") or os.environ.get("TTS_SPEED") or 1.0)
        except ValueError:
            print(f"[lỗi] {args.provider.upper()}_SPEED không phải là số", file=sys.stderr)
            return 1
    lo, hi = SPEED_RANGES[args.provider]
    if not lo <= args.speed <= hi:
        print(f"[lỗi] --speed với {args.provider} phải trong khoảng {lo}–{hi} (đang là {args.speed})",
              file=sys.stderr)
        return 1
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[lỗi] Cần ffmpeg / ffprobe trong PATH", file=sys.stderr)
        return 1

    srt = Path(args.srt)
    try:
        cues = [c for c in parse_srt(srt.read_text(encoding="utf-8-sig")) if c["text"]]
    except OSError as e:
        print(f"[lỗi] Không đọc được phụ đề: {e}", file=sys.stderr)
        return 1
    if not cues:
        print("[lỗi] Không đọc được câu phụ đề nào", file=sys.stderr)
        return 1

    try:
        synth, voice_key, ext, voice_label = setup_provider(args.provider, args.voice, args.speed)
    except SetupError as e:
        print(f"[lỗi] {e}", file=sys.stderr)
        return 1

    cache = Path(args.cache_dir) if args.cache_dir else srt.parent / "tts-cache"
    cache.mkdir(parents=True, exist_ok=True)
    plan: list[tuple[dict, Path]] = []
    for cue in cues:
        key_src = f"{args.provider}|{voice_key}|{args.speed}|{cue['text']}"
        key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]
        plan.append((cue, cache / f"cue-{cue['index']:03d}-{key}{ext}"))
    todo = [(c, clip) for c, clip in plan if not clip.exists()]
    chars = sum(len(c["text"]) for c, _ in todo)
    print(f"{args.provider}: {len(cues)} câu, giọng {voice_label}, tốc độ {args.speed} — "
          f"cần tạo {len(todo)} câu ({chars} ký tự), {len(cues) - len(todo)} câu có sẵn trong cache")
    if args.dry_run:
        if args.provider in ("elevenlabs", "vbee"):
            print("  (engine trả phí theo ký tự; số ký tự trên là phần sẽ bị tính phí)")
        print(f"CHARS={chars}")
        return 0

    clips: list[Path] = []
    for i, (cue, clip) in enumerate(plan):
        if not clip.exists():
            prev_text = cues[i - 1]["text"] if i > 0 else None
            next_text = cues[i + 1]["text"] if i + 1 < len(cues) else None
            try:
                synth(cue["text"], clip, prev_text, next_text)
            except Exception as e:  # lỗi mạng / máy chủ: báo và dừng (các câu đã tạo vẫn nằm trong cache)
                print(f"[lỗi] Câu {cue['index']} tạo giọng thất bại: {e}", file=sys.stderr)
                return 1
            print(f"  Câu {cue['index']:>2} xong ({probe_duration(clip):.2f}s): {cue['text'][:50]}")
        else:
            print(f"  Câu {cue['index']:>2} dùng cache")
        clips.append(clip if args.no_trim else trim_silence(clip))

    if args.retime_out:
        cues = retime_cues(cues, clips, args.gap, pauses, args.tail)
        retimed = Path(args.retime_out)
        write_srt(cues, retimed)
        print(f"  Đã sắp lại mốc: tổng {cues[-1]['endMs'] / 1000:.1f}s")
        print(f"SRT={retimed.resolve()}")

    video = Path(args.video) if args.video else None
    total_ms = int(probe_duration(video) * 1000) if video else None
    output = Path(args.output)
    try:
        build_track(cues, clips, output, total_ms)
    except SlotError as e:
        print(f"[lỗi] {e}", file=sys.stderr)
        return 1
    print(f"AUDIO={output.resolve()}")

    if video:
        video_out = Path(args.video_out) if args.video_out else video.with_name(f"{video.stem}-voice.mp4")
        mux(video, output, video_out)
        print(f"OUTPUT={video_out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
