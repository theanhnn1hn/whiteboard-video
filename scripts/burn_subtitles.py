#!/usr/bin/env python3
"""
Gắn phụ đề SRT vào video (chữ xám đậm trên nền màu giấy, không đè nét vẽ).

Thay cho lệnh ffmpeg dài trong SKILL.md, và tránh lỗi đường dẫn của filter `subtitles`
(dấu `:` của ổ đĩa Windows, dấu `'`, `,`, `[`...): SRT được chép vào thư mục tạm với tên an toàn
rồi ffmpeg chạy trong thư mục đó. Giữ nguyên track âm thanh nếu có.

Dùng:
  <ENV_PY> scripts/burn_subtitles.py final.mp4 input.srt [--output final-sub.mp4]
         [--font Arial] [--font-size 15] [--margin 14] [--text-color #303030] [--box-color #F5EBD7]
Dòng cuối in OUTPUT=<video có phụ đề> (mặc định <video>-sub.mp4).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_BOX = "#F5EBD7"   # trùng màu giấy nền của renderer
DEFAULT_TEXT = "#303030"


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB → &HAABBGGRR (định dạng màu của ASS/libass; alpha 0 = đục)."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_color.strip())
    if not m:
        raise ValueError(f"màu không hợp lệ (cần #RRGGBB): {hex_color}")
    rr, gg, bb = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return f"&H{alpha:02X}{bb}{gg}{rr}".upper()


def force_style(font: str, size: int, margin: int, text_color: str, box_color: str) -> str:
    if re.search(r"[,':=\\\[\]]", font):
        raise ValueError(f"tên font không được chứa , ' : = \\ [ ]: {font!r}")
    return ",".join([
        f"FontName={font}", f"FontSize={size}",
        f"PrimaryColour={ass_color(text_color)}",
        f"OutlineColour={ass_color(box_color, 0x10)}",  # BorderStyle=3: Outline là màu hộp nền
        "BorderStyle=3", "Outline=6", "Shadow=0", f"MarginV={margin}",
    ])


def has_audio(video: Path) -> bool:
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    return bool(res.stdout.strip())


def burn(video: Path, srt: Path, output: Path, style: str, crf: int = 20) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(srt, Path(tmp) / "subs.srt")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video.resolve()),
               "-vf", f"subtitles=subs.srt:charenc=UTF-8:force_style='{style}'",
               "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        cmd += ["-c:a", "copy"] if has_audio(video) else ["-an"]
        cmd.append(str(output.resolve()))
        res = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip()[-600:] or "ffmpeg thất bại")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Gắn phụ đề SRT vào video (nền màu giấy, không đè nét vẽ)")
    p.add_argument("video", help="Video MP4 đầu vào")
    p.add_argument("srt", help="File phụ đề .srt")
    p.add_argument("--output", help="Video xuất ra (mặc định <video>-sub.mp4)")
    p.add_argument("--font", default="Arial", help="Tên font (mặc định Arial; cần có trên máy)")
    p.add_argument("--font-size", type=int, default=15, help="Cỡ chữ theo thang ASS (mặc định 15)")
    p.add_argument("--margin", type=int, default=14, help="Khoảng cách tới mép dưới (mặc định 14)")
    p.add_argument("--text-color", default=DEFAULT_TEXT, help=f"Màu chữ (mặc định {DEFAULT_TEXT})")
    p.add_argument("--box-color", default=DEFAULT_BOX, help=f"Màu hộp nền sau chữ (mặc định {DEFAULT_BOX})")
    p.add_argument("--crf", type=int, default=20, help="Chất lượng H.264, nhỏ hơn = đẹp hơn (mặc định 20)")
    args = p.parse_args(argv)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[lỗi] Cần ffmpeg / ffprobe trong PATH", file=sys.stderr)
        return 1
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    if not re.search(r"\bsubtitles\b", filters):
        print("[lỗi] ffmpeg này không có filter 'subtitles' (cần bản build có libass)", file=sys.stderr)
        return 1
    video, srt = Path(args.video), Path(args.srt)
    for f in (video, srt):
        if not f.exists():
            print(f"[lỗi] Không có file: {f}", file=sys.stderr)
            return 1
    output = Path(args.output) if args.output else video.with_name(f"{video.stem}-sub.mp4")
    try:
        style = force_style(args.font, args.font_size, args.margin, args.text_color, args.box_color)
        burn(video, srt, output, style, args.crf)
    except (ValueError, RuntimeError) as e:
        print(f"[lỗi] Gắn phụ đề thất bại: {e}", file=sys.stderr)
        return 1
    print(f"OUTPUT={output.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
