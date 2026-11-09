#!/usr/bin/env python3
"""
Ghép các cảnh: nối các MP4 theo thứ tự thành một video (cắt thẳng, không chuyển cảnh).

Ưu tiên ffmpeg nối không mã hoá lại (-c copy); nếu các đoạn khác kích thước/mã hoá
thì mã hoá lại bằng ffmpeg, không có ffmpeg thì dùng PyAV và co về kích thước đoạn đầu.

Dùng:
  <ENV_PY> merge_scenes.py --inputs a.mp4 b.mp4 c.mp4 --output final.mp4
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _ffmpeg_concat_copy(inputs: list[Path], output: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in inputs:
            # cú pháp concat của ffmpeg: dấu ' trong đường dẫn phải viết thành '\''
            quoted = p.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{quoted}'\n")
        list_path = Path(f.name)
    try:
        res = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_path), "-c", "copy", str(output)],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            print(f"  Đã nối bằng ffmpeg (không mã hoá lại): {output}")
            return True
        print(f"  [cảnh báo] Nối -c copy thất bại, thử mã hoá lại: {res.stderr.strip()[:200]}")
        res = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_path), "-c:v", "libx264", "-crf", "20",
             "-pix_fmt", "yuv420p", "-vf", "scale='trunc(iw/2)*2':'trunc(ih/2)*2'", str(output)],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            print(f"  Đã nối bằng ffmpeg (mã hoá lại): {output}")
            return True
        print(f"  [cảnh báo] Mã hoá lại cũng thất bại: {res.stderr.strip()[:200]}")
        return False
    finally:
        list_path.unlink(missing_ok=True)


def _pyav_concat(inputs: list[Path], output: Path) -> bool:
    try:
        import av
    except ImportError:
        return False
    import numpy as np  # noqa: F401
    first = av.open(str(inputs[0]))
    vs = first.streams.video[0]
    w, h = vs.codec_context.width, vs.codec_context.height
    rate = vs.average_rate
    first.close()

    out = av.open(str(output), mode="w")
    ostream = out.add_stream("h264", rate=rate)
    ostream.width, ostream.height = w, h
    ostream.pix_fmt = "yuv420p"
    ostream.options = {"crf": "24", "preset": "medium"}
    for p in inputs:
        cont = av.open(str(p))
        for frame in cont.decode(video=0):
            if frame.width != w or frame.height != h:
                frame = frame.reformat(width=w, height=h)
            for pkt in ostream.encode(frame):
                out.mux(pkt)
        cont.close()
    for pkt in ostream.encode(None):
        out.mux(pkt)
    out.close()
    print(f"  Đã nối bằng PyAV: {output}")
    return True


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Ghép các cảnh MP4 theo thứ tự")
    p.add_argument("--inputs", nargs="+", required=True, help="Các file MP4 theo thứ tự phát")
    p.add_argument("--output", required=True, help="Video ghép xuất ra")
    args = p.parse_args(argv)

    inputs = [Path(x) for x in args.inputs]
    missing = [str(x) for x in inputs if not x.exists()]
    if missing:
        print(f"[lỗi] Thiếu file đầu vào: {', '.join(missing)}", file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if _ffmpeg_concat_copy(inputs, output) or _pyav_concat(inputs, output):
        print(f"OUTPUT={output.resolve()}")
        return 0
    print("[lỗi] Ghép thất bại: không có ffmpeg và PyAV", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
