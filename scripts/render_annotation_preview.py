#!/usr/bin/env python3
"""
Xuất ảnh kiểm tra vùng: vẽ khung, số thứ tự, nhãn và hướng vẽ của từng vùng
trong annotation.json lên ảnh gốc, để đối chiếu thứ tự / vị trí trước khi render.

Thứ tự đánh số = thứ tự vẽ thật của renderer (sắp theo reveal.startMs, rồi sequence).
Chạy được trên Windows, macOS và Linux: tự tìm font có dấu tiếng Việt (và chữ Hán
nếu nhãn có chữ Hán); có thể chỉ định font bằng --font.

Dùng:
  <ENV_PY> scripts/render_annotation_preview.py <ảnh> <annotation.json> <ảnh-kiểm-tra.png> [--font <file.ttf>]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Font có đủ dấu tiếng Việt, theo thứ tự ưu tiên trên từng hệ điều hành
LATIN_FONTS = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
# Font chữ Hán (chỉ dùng khi nhãn có chữ Hán)
CJK_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
COLORS = [(38, 103, 255), (255, 105, 92), (41, 167, 102), (181, 100, 255), (230, 140, 20)]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def load_font(size: int, cjk: bool, explicit: str | None) -> ImageFont.ImageFont:
    candidates = ([explicit] if explicit else []) + (CJK_FONTS if cjk else []) + LATIN_FONTS
    for path in candidates:
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    print("  [cảnh báo] Không tìm thấy font TrueType có dấu tiếng Việt, dùng font mặc định của Pillow "
          "(nhãn có thể mất dấu). Chỉ định font bằng --font.", file=sys.stderr)
    try:
        return ImageFont.load_default(size=size)  # Pillow ≥ 10.1
    except TypeError:
        return ImageFont.load_default()


def draw_arrow(draw: ImageDraw.ImageDraw, start, end, color, width: int = 4) -> None:
    draw.line((tuple(start), tuple(end)), fill=color, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    size = 14
    left = (end[0] - size * math.cos(angle - 0.45), end[1] - size * math.sin(angle - 0.45))
    right = (end[0] - size * math.cos(angle + 0.45), end[1] - size * math.sin(angle + 0.45))
    draw.polygon((tuple(end), left, right), fill=color)


def render(image_path: str, annotation_path: str, output_path: str, font_path: str | None = None) -> Path:
    image = Image.open(image_path).convert("RGBA")
    data = json.loads(Path(annotation_path).read_text(encoding="utf-8"))
    elements = sorted(data.get("elements", []),
                      key=lambda e: (e["reveal"]["startMs"], e.get("sequence", 0)))

    canvas = data.get("canvas") or {}
    sx = image.width / canvas["width"] if canvas.get("width") else 1.0
    sy = image.height / canvas["height"] if canvas.get("height") else 1.0
    if abs(sx - 1) > 1e-6 or abs(sy - 1) > 1e-6:
        print(f"  [cảnh báo] canvas {canvas.get('width')}x{canvas.get('height')} khác kích thước ảnh "
              f"{image.width}x{image.height}; toạ độ được co giãn theo ảnh.", file=sys.stderr)

    labels = " ".join(str(e.get("label", "")) for e in elements)
    cjk = _has_cjk(labels)
    small_font = load_font(18, cjk, font_path)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for index, element in enumerate(elements, start=1):
        region = element["region"]
        x, y = region["x"] * sx, region["y"] * sy
        right, bottom = x + region["width"] * sx, y + region["height"] * sy
        rgb = COLORS[(index - 1) % len(COLORS)]
        color = (*rgb, 225)
        draw.rounded_rectangle((x, y, right, bottom), radius=12, outline=color, width=4, fill=(*rgb, 24))

        # Các vùng bảo vệ: gạch chéo mờ để thấy chỗ vùng này không được vẽ
        for prot in element.get("reveal", {}).get("protectedRegions", []) or []:
            px, py = prot["x"] * sx, prot["y"] * sy
            pr, pb = px + prot["width"] * sx, py + prot["height"] * sy
            draw.rectangle((px, py, pr, pb), outline=(*rgb, 120), width=2)

        draw.ellipse((x + 8, y + 8, x + 44, y + 44), fill=color)
        draw.text((x + 26, y + 26), str(index), anchor="mm", font=small_font, fill="white")
        start_s = element["reveal"]["startMs"] / 1000
        end_s = start_s + element["reveal"]["durationMs"] / 1000
        label = f"{index}. {element.get('label', '')}  {start_s:.1f}–{end_s:.1f}s"
        text_w = draw.textlength(label, font=small_font)
        draw.rounded_rectangle((x + 52, y + 8, min(right - 8, x + 52 + text_w + 16), y + 46),
                               radius=6, fill=(255, 255, 255, 225))
        draw.text((x + 60, y + 14), label, font=small_font, fill=color)

        hand_path = element.get("handPath") or {}
        if hand_path.get("start") and hand_path.get("end"):
            start = (hand_path["start"][0] * sx, hand_path["start"][1] * sy)
            end = (hand_path["end"][0] * sx, hand_path["end"][1] * sy)
            draw_arrow(draw, start, end, color)

    result = Image.alpha_composite(image, overlay).convert("RGB")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(out, quality=95)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Xuất ảnh kiểm tra vùng từ annotation.json")
    p.add_argument("image", help="Ảnh gốc của cảnh")
    p.add_argument("annotation", help="File <tên>.annotation.json")
    p.add_argument("output", help="Ảnh kiểm tra xuất ra (.png/.jpg)")
    p.add_argument("--font", help="Đường dẫn font .ttf/.ttc (mặc định tự tìm)")
    args = p.parse_args(argv)
    try:
        out = render(args.image, args.annotation, args.output, args.font)
    except (OSError, KeyError, json.JSONDecodeError) as e:
        print(f"[lỗi] Không xuất được ảnh kiểm tra: {e}", file=sys.stderr)
        return 1
    print(f"OUTPUT={out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
