#!/usr/bin/env python3
"""
Kiểm tra annotation.json trước khi render: thay cho việc agent "nhìn" thủ công các quy tắc
trong SKILL.md (pixel nguyên, nằm trong ảnh, canvas khớp ảnh, sequence liên tục, thời gian...).

Lỗi (thoát với mã 1):
  - JSON sai, thiếu canvas / elements / sceneDurationMs
  - canvas khác kích thước ảnh gốc (ảnh cùng tên đặt cạnh annotation, hoặc --image)
  - toạ độ không phải số nguyên, kích thước ≤ 0, vùng/vùng bảo vệ vượt ra ngoài ảnh
  - id trùng, sequence không liên tục từ 1 hoặc không khớp thứ tự vẽ thật (theo startMs)
  - startMs < 0, durationMs ≤ 0
Cảnh báo:
  - hai vùng chồng nhau về thời gian (chỉ có một cây bút)
  - vùng bị các vùng vẽ sau che gần hết (không còn gì để vẽ)
  - nét vẽ không thuộc vùng nào → sẽ hiện đột ngột ở cuối cảnh (cần numpy + OpenCV)
  - vẽ xong sát/qua sceneDurationMs (video dài hơn khai báo → lệch tiếng khi ghép cảnh)
  - thiếu label / narrativeRole / subtitle, direction không hợp lệ
  - với --srt: phụ đề của vùng không có trong SRT, vùng vẽ lệch xa câu đang đọc,
    tổng thời lượng các cảnh khác thời lượng SRT

Dùng:
  python scripts/validate_annotation.py scene-01.annotation.json [scene-02.annotation.json ...]
         [--srt input.srt] [--image ảnh.png]
  Truyền nhiều cảnh theo thứ tự phát để kiểm tra khớp thời gian với SRT.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_srt import parse_srt  # noqa: E402

DIRECTIONS = {"top_to_bottom", "bottom_to_top", "left_to_right", "right_to_left"}
IMG_EXT = (".png", ".jpg", ".jpeg")
END_HOLD_MS = 500
INK_THRESHOLD = 10        # giống renderer: điểm ảnh có ngưỡng thích nghi < 10 là nét mực
UNCOVERED_WARN = 0.01     # >1% nét vẽ nằm ngoài mọi vùng thì cảnh báo
EMPTY_REGION_WARN = 0.02  # phạm vi được vẽ còn < 2% khung thì cảnh báo


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").casefold()
    return re.sub(r"[\s\W_]+", " ", text).strip()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class Report:
    def __init__(self, name: str):
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def print(self) -> None:
        status = "LỖI" if self.errors else ("cảnh báo" if self.warnings else "ổn")
        print(f"[{status}] {self.name}")
        for m in self.errors:
            print(f"   ✗ {m}")
        for m in self.warnings:
            print(f"   ! {m}")


def find_image(ann_path: Path) -> Path | None:
    base = ann_path.name
    if not base.endswith(".annotation.json"):
        return None
    stem = base[: -len(".annotation.json")]
    # foo.tight.annotation.json (nhịp gọn) dùng chung ảnh foo.png
    stems = [stem] + ([stem.rsplit(".", 1)[0]] if "." in stem else [])
    for s in stems:
        for ext in IMG_EXT:
            cand = ann_path.with_name(s + ext)
            if cand.exists():
                return cand
    return None


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    try:  # PNG: đọc thẳng header, không cần thư viện
        data = path.read_bytes()[:32]
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except OSError:
        pass
    return None


def _rect_ok(rep: Report, where: str, r, cw: int, ch: int) -> bool:
    if not isinstance(r, dict):
        rep.err(f"{where}: thiếu hoặc sai kiểu (cần object x/y/width/height)")
        return False
    vals = [r.get(k) for k in ("x", "y", "width", "height")]
    if not all(_is_int(v) for v in vals):
        rep.err(f"{where}: x/y/width/height phải là số nguyên pixel, đang là {vals}")
        return False
    x, y, w, h = vals
    if w <= 0 or h <= 0:
        rep.err(f"{where}: width/height phải > 0 ({w}x{h})")
        return False
    if x < 0 or y < 0 or x + w > cw or y + h > ch:
        rep.err(f"{where}: vượt ra ngoài ảnh {cw}x{ch} (x={x}, y={y}, phải={x + w}, dưới={y + h})")
        return False
    return True


def check_scene(ann_path: Path, image: Path | None) -> tuple[Report, dict | None]:
    rep = Report(ann_path.name)
    try:
        data = json.loads(ann_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        rep.err(f"không đọc được JSON: {e}")
        return rep, None
    canvas = data.get("canvas") or {}
    cw, ch = canvas.get("width"), canvas.get("height")
    if not (_is_int(cw) and _is_int(ch) and cw > 0 and ch > 0):
        rep.err(f"canvas.width/height phải là số nguyên dương, đang là {cw}x{ch}")
        return rep, None
    image = image or find_image(ann_path)
    if image is None:
        rep.warn("không tìm thấy ảnh cùng tên để so kích thước (dùng --image)")
    else:
        size = _image_size(image)
        if size and size != (cw, ch):
            rep.err(f"canvas {cw}x{ch} khác kích thước ảnh {image.name} {size[0]}x{size[1]}")
    dur = data.get("sceneDurationMs")
    if not (_is_int(dur) and dur > 0):
        rep.err(f"sceneDurationMs phải là số nguyên dương (ms), đang là {dur!r}")
        dur = None
    elements = data.get("elements")
    if not isinstance(elements, list) or not elements:
        rep.err("elements rỗng hoặc không phải danh sách")
        return rep, data

    ids: set[str] = set()
    valid: list[dict] = []
    for i, e in enumerate(elements, 1):
        where = f"vùng #{i} ({e.get('id') or e.get('label') or '?'})"
        if e.get("id") in ids:
            rep.err(f"{where}: id trùng {e.get('id')!r}")
        ids.add(e.get("id"))
        for field in ("label", "narrativeRole", "subtitle"):
            if not str(e.get(field) or "").strip():
                rep.warn(f"{where}: thiếu {field}")
        ok = _rect_ok(rep, f"{where}.region", e.get("region"), cw, ch)
        rv = e.get("reveal") or {}
        start, length = rv.get("startMs"), rv.get("durationMs")
        if not (_is_int(start) and start >= 0):
            rep.err(f"{where}: reveal.startMs phải là số nguyên ≥ 0, đang là {start!r}")
            ok = False
        if not (_is_int(length) and length > 0):
            rep.err(f"{where}: reveal.durationMs phải là số nguyên > 0, đang là {length!r}")
            ok = False
        if rv.get("direction") not in DIRECTIONS:
            rep.warn(f"{where}: reveal.direction {rv.get('direction')!r} không hợp lệ ({', '.join(sorted(DIRECTIONS))})")
        for j, prot in enumerate(rv.get("protectedRegions") or [], 1):
            _rect_ok(rep, f"{where}.protectedRegions[{j}]", prot, cw, ch)
        if not _is_int(e.get("sequence")):
            rep.err(f"{where}: sequence phải là số nguyên")
            ok = False
        if ok:
            valid.append(e)
    if len(valid) != len(elements):
        return rep, data

    ordered = sorted(elements, key=lambda e: (e["reveal"]["startMs"], e["sequence"]))
    seqs = [e["sequence"] for e in ordered]
    if sorted(seqs) != list(range(1, len(seqs) + 1)):
        rep.err(f"sequence phải liên tục từ 1 đến {len(seqs)}, đang là {sorted(seqs)}")
    elif seqs != list(range(1, len(seqs) + 1)):
        rep.err("thứ tự sequence khác thứ tự vẽ thật (renderer vẽ theo startMs): "
                + ", ".join(f"{e['sequence']}:{e.get('label', e.get('id'))}@{e['reveal']['startMs']}ms" for e in ordered))

    last_end = 0
    for prev, cur in zip(ordered, ordered[1:]):
        prev_end = prev["reveal"]["startMs"] + prev["reveal"]["durationMs"]
        if cur["reveal"]["startMs"] < prev_end:
            rep.warn(f"'{cur.get('label')}' bắt đầu ({cur['reveal']['startMs']} ms) trước khi "
                     f"'{prev.get('label')}' vẽ xong ({prev_end} ms): chỉ có một cây bút, vùng sau sẽ bị dời lại")
    for e in ordered:
        last_end = max(last_end, e["reveal"]["startMs"] + e["reveal"]["durationMs"])
    if dur is not None and last_end + END_HOLD_MS > dur:
        rep.warn(f"vùng cuối xong lúc {last_end} ms, cộng 0,5 s giữ hình = {last_end + END_HOLD_MS} ms "
                 f"> sceneDurationMs {dur} ms: video cảnh sẽ dài hơn khai báo (lệch tiếng khi ghép cảnh)")

    _check_masks(rep, ordered, cw, ch, image)
    return rep, data


def _check_masks(rep: Report, ordered: list[dict], cw: int, ch: int, image: Path | None) -> None:
    try:
        import numpy as np
    except ImportError:
        return
    # Làm việc ở độ phân giải nhỏ cho nhanh
    scale = min(1.0, 800 / max(cw, ch))
    w, h = max(1, round(cw * scale)), max(1, round(ch * scale))

    def box(r):
        return (round(r["x"] * scale), round(r["y"] * scale),
                round((r["x"] + r["width"]) * scale), round((r["y"] + r["height"]) * scale))

    covered = np.zeros((h, w), dtype=bool)
    for idx, e in enumerate(ordered):
        mask = np.zeros((h, w), dtype=bool)
        x0, y0, x1, y1 = box(e["region"])
        mask[y0:y1, x0:x1] = True
        region_area = max(1, int(mask.sum()))
        for later in ordered[idx + 1:]:
            lx0, ly0, lx1, ly1 = box(later["region"])
            mask[ly0:ly1, lx0:lx1] = False
        for prot in e["reveal"].get("protectedRegions") or []:
            px0, py0, px1, py1 = box(prot)
            mask[py0:py1, px0:px1] = False
        if mask.sum() < EMPTY_REGION_WARN * region_area:
            rep.warn(f"'{e.get('label')}': gần như toàn bộ khung bị các vùng vẽ sau / vùng bảo vệ che, "
                     "vùng này hầu như không vẽ gì")
        covered |= mask

    if image is None:
        return
    try:
        import cv2
        raw = np.fromfile(str(image), dtype=np.uint8)
        img = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    except Exception:
        return
    if img is None:
        return
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    ink = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 10) < INK_THRESHOLD
    total = int(ink.sum())
    if total == 0:
        rep.warn("ảnh gần như không có nét vẽ")
        return
    uncovered = int((ink & ~covered).sum()) / total
    if uncovered > UNCOVERED_WARN:
        rep.warn(f"{uncovered:.0%} nét vẽ không thuộc phạm vi vẽ của vùng nào: sẽ hiện đột ngột ở cuối cảnh "
                 "(mở rộng vùng, hoặc thêm một vùng nền)")


def check_against_srt(reports: list[Report], scenes: list[dict | None], srt_path: Path) -> Report:
    rep = Report(f"Khớp với {srt_path.name}")
    try:
        cues = [c for c in parse_srt(srt_path.read_text(encoding="utf-8-sig")) if c["text"]]
    except OSError as e:
        rep.err(f"không đọc được SRT: {e}")
        return rep
    if not cues:
        rep.err("SRT không có câu nào")
        return rep
    by_text = {}
    for c in cues:
        by_text.setdefault(_norm(c["text"]), c)
    offset = 0
    for scene_rep, data in zip(reports, scenes):
        if not data or not isinstance(data.get("elements"), list):
            return rep
        for e in data["elements"]:
            sub = _norm(e.get("subtitle", ""))
            if not sub:
                continue
            cue = by_text.get(sub) or next((c for t, c in by_text.items() if sub in t or t in sub), None)
            if cue is None:
                scene_rep.warn(f"'{e.get('label')}': phụ đề không có trong {srt_path.name}: {e.get('subtitle')!r}")
                continue
            rv = e.get("reveal") or {}
            if not (_is_int(rv.get("startMs")) and _is_int(rv.get("durationMs"))):
                continue
            g_start = offset + rv["startMs"]
            if g_start < cue["startMs"] - 1500:
                scene_rep.warn(f"'{e.get('label')}' bắt đầu vẽ lúc {g_start / 1000:.1f}s, sớm hơn câu "
                               f"{cue['index']} ({cue['startMs'] / 1000:.1f}s) quá 1,5 s")
            elif g_start > cue["endMs"]:
                scene_rep.warn(f"'{e.get('label')}' bắt đầu vẽ lúc {g_start / 1000:.1f}s, sau khi câu "
                               f"{cue['index']} đã đọc xong ({cue['endMs'] / 1000:.1f}s)")
        offset += data.get("sceneDurationMs") or 0
    srt_end = cues[-1]["endMs"]
    if abs(offset - srt_end) > 1000:
        rep.warn(f"tổng thời lượng các cảnh {offset / 1000:.1f}s khác thời lượng phụ đề {srt_end / 1000:.1f}s: "
                 "giọng đọc và hình sẽ lệch nhau sau khi ghép (sửa sceneDurationMs, hoặc truyền đủ các cảnh theo thứ tự)")
    return rep


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Kiểm tra annotation.json trước khi render")
    p.add_argument("annotations", nargs="+", help="Các file .annotation.json (theo thứ tự phát nếu dùng --srt)")
    p.add_argument("--srt", help="SRT để kiểm tra phụ đề và độ khớp thời gian giữa hình và lời đọc")
    p.add_argument("--image", help="Ảnh gốc (chỉ khi kiểm tra một file; mặc định tìm ảnh cùng tên)")
    args = p.parse_args(argv)
    if args.image and len(args.annotations) > 1:
        p.error("--image chỉ dùng khi kiểm tra một annotation")

    reports, scenes = [], []
    for path in args.annotations:
        rep, data = check_scene(Path(path), Path(args.image) if args.image else None)
        reports.append(rep)
        scenes.append(data)
    extra = [check_against_srt(reports, scenes, Path(args.srt))] if args.srt else []
    for rep in reports + extra:
        rep.print()
    n_err = sum(len(r.errors) for r in reports + extra)
    n_warn = sum(len(r.warnings) for r in reports + extra)
    print(f"Tổng: {n_err} lỗi, {n_warn} cảnh báo")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
