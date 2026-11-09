#!/usr/bin/env python3
"""
Co giãn thời gian vẽ trong annotation của nhiều cảnh theo mốc phụ đề mới
(dùng cùng tts_narration.py --retime-out, chế độ "nhịp gọn").

Cách làm: câu phụ đề cũ và mới tương ứng một-một; lấy mốc bắt đầu mỗi câu (và mốc kết thúc
câu cuối) làm điểm neo, nội suy tuyến tính từng đoạn: thời gian toàn cục cũ → mới. Các cảnh nối
tiếp theo thứ tự truyền vào (mốc đầu cảnh = tổng sceneDurationMs các cảnh trước); startMs / mốc
kết thúc của từng vùng và ranh giới cảnh được ánh xạ rồi đổi về thời gian trong cảnh.
Vùng, thứ tự, hướng giữ nguyên.

Ghi ra file mới (mặc định <tên>.tight.annotation.json), không ghi đè file gốc.

Dùng:
  python retime_annotations.py --old-srt input.srt --new-srt input.tight.srt \
      --annotations scene-01.annotation.json scene-02.annotation.json [--suffix .tight]
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_srt import parse_srt  # noqa: E402

END_HOLD_MS = 500  # 所有区域画完后至少停留完整原图的时长


def build_mapper(old: list[dict], new: list[dict]):
    xs = [c["startMs"] for c in old] + [old[-1]["endMs"]]
    ys = [c["startMs"] for c in new] + [new[-1]["endMs"]]

    def mapper(t: float) -> int:
        if t <= xs[0]:
            return round(ys[0] + (t - xs[0]))
        if t >= xs[-1]:
            return round(ys[-1] + (t - xs[-1]))
        i = bisect.bisect_right(xs, t) - 1
        span = xs[i + 1] - xs[i]
        k = (t - xs[i]) / span if span else 0.0
        return round(ys[i] + k * (ys[i + 1] - ys[i]))

    return mapper


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Co giãn thời gian vẽ trong annotation theo SRT mới")
    p.add_argument("--old-srt", required=True, help="SRT mà annotation đang dựa theo")
    p.add_argument("--new-srt", required=True, help="SRT đã sắp lại mốc")
    p.add_argument("--annotations", nargs="+", required=True, help="Các .annotation.json theo thứ tự phát")
    p.add_argument("--suffix", default=".tight", help="Hậu tố tên file ra (mặc định .tight)")
    args = p.parse_args(argv)

    old = parse_srt(Path(args.old_srt).read_text(encoding="utf-8-sig"))
    new = parse_srt(Path(args.new_srt).read_text(encoding="utf-8-sig"))
    if len(old) != len(new) or not old:
        print(f"[lỗi] Số câu phụ đề cũ và mới khác nhau: {len(old)} vs {len(new)}", file=sys.stderr)
        return 1
    remap = build_mapper(old, new)

    old_offset = 0
    for ann_path in map(Path, args.annotations):
        ann = json.loads(ann_path.read_text(encoding="utf-8"))
        old_dur = ann["sceneDurationMs"]
        new_start, new_end = remap(old_offset), remap(old_offset + old_dur)

        last_end = 0
        for el in ann["elements"]:
            rv = el["reveal"]
            g0 = old_offset + rv["startMs"]
            s, e = remap(g0) - new_start, remap(g0 + rv["durationMs"]) - new_start
            rv["startMs"], rv["durationMs"] = s, max(1, e - s)
            last_end = max(last_end, s + rv["durationMs"])
        ann["sceneDurationMs"] = max(new_end - new_start, last_end + END_HOLD_MS)

        out = ann_path.with_name(ann_path.name.replace(".annotation.json", f"{args.suffix}.annotation.json"))
        out.write_text(json.dumps(ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  {ann_path.name}: {old_dur / 1000:.1f}s → {ann['sceneDurationMs'] / 1000:.1f}s  => {out.name}")
        old_offset += old_dur
    return 0


if __name__ == "__main__":
    sys.exit(main())
