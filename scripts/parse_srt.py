#!/usr/bin/env python3
"""
Đọc SRT + gợi ý chia cảnh

Tách .srt thành từng câu có cấu trúc, rồi gom các câu liên tiếp thành cảnh (mặc định
25–35 giây/cảnh), cho mỗi cảnh mốc bắt đầu/kết thúc, thời lượng (→ sceneDurationMs) và lời.
Dùng ở bước 1 của quy trình whiteboard-video.

Dùng:
  python parse_srt.py <phụ-đề.srt> [--target-sec 30] [--min-sec 25] [--max-sec 35]

Kết quả: JSON ra stdout:
  cues    từng câu: {index, startMs, endMs, durMs, text}
  scenes  cảnh gợi ý: {sceneIndex, startMs, endMs, sceneDurationMs, cueRange, text}
Tóm tắt dễ đọc in ra stderr.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def parse_srt(text: str) -> list[dict]:
    """把 SRT 文本解析成字幕条列表。容忍多余空行、BOM、逗号/点毫秒分隔。"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())
    cues: list[dict] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        # 找到含时间轴的行
        time_line_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_line_idx is None:
            continue
        times = _TIME.findall(lines[time_line_idx])
        if len(times) < 2:
            continue
        start = _to_ms(*times[0])
        end = _to_ms(*times[1])
        body = " ".join(lines[time_line_idx + 1:]).strip()
        cues.append({
            "index": len(cues) + 1,
            "startMs": start,
            "endMs": end,
            "durMs": max(0, end - start),
            "text": body,
        })
    return cues


def group_scenes(cues: list[dict], target_sec: float, min_sec: float, max_sec: float) -> list[dict]:
    """
    按目标时长把连续字幕聚成场景：累积到 target 附近就断一幕，
    但不小于 min、不大于 max（超过 max 强制断幕）。
    """
    scenes: list[dict] = []
    bucket: list[dict] = []
    target_ms, min_ms, max_ms = target_sec * 1000, min_sec * 1000, max_sec * 1000

    def flush() -> None:
        if not bucket:
            return
        start = bucket[0]["startMs"]
        end = bucket[-1]["endMs"]
        scenes.append({
            "sceneIndex": len(scenes) + 1,
            "startMs": start,
            "endMs": end,
            "sceneDurationMs": max(0, end - start),
            "cueRange": [bucket[0]["index"], bucket[-1]["index"]],
            "text": " ".join(c["text"] for c in bucket).strip(),
        })
        bucket.clear()

    for cue in cues:
        # 若把这条并进当前幕会超过 max，先断幕（避免出现超长幕）
        if bucket:
            span_with = cue["endMs"] - bucket[0]["startMs"]
            if span_with > max_ms:
                flush()
        bucket.append(cue)
        span = bucket[-1]["endMs"] - bucket[0]["startMs"]
        # 达到目标且不短于 min 即断幕
        if span >= target_ms and span >= min_ms:
            flush()
    flush()
    return scenes


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Đọc SRT + gợi ý chia cảnh")
    p.add_argument("srt", help="File phụ đề (.srt)")
    p.add_argument("--target-sec", type=float, default=30.0, help="Thời lượng mục tiêu mỗi cảnh, giây (mặc định 30)")
    p.add_argument("--min-sec", type=float, default=25.0, help="Ngắn nhất mỗi cảnh, giây (mặc định 25)")
    p.add_argument("--max-sec", type=float, default=35.0, help="Dài nhất mỗi cảnh, giây (mặc định 35)")
    args = p.parse_args(argv)

    try:
        raw = Path(args.srt).read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"[lỗi] Không đọc được phụ đề: {e}", file=sys.stderr)
        return 1

    cues = parse_srt(raw)
    if not cues:
        print("[lỗi] Không đọc được câu phụ đề nào, kiểm tra định dạng SRT", file=sys.stderr)
        return 1
    scenes = group_scenes(cues, args.target_sec, args.min_sec, args.max_sec)

    total_ms = cues[-1]["endMs"] - cues[0]["startMs"]
    print(f"Số câu: {len(cues)}  Tổng: {total_ms/1000:.1f}s  Gợi ý: {len(scenes)} cảnh", file=sys.stderr)
    for s in scenes:
        print(f"  Cảnh {s['sceneIndex']:>2}  {s['startMs']/1000:6.1f}-{s['endMs']/1000:6.1f}s "
              f"({s['sceneDurationMs']/1000:4.1f}s, câu {s['cueRange'][0]}-{s['cueRange'][1]}): "
              f"{s['text'][:40]}", file=sys.stderr)

    json.dump({"cues": cues, "scenes": scenes}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
