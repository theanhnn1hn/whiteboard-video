#!/usr/bin/env python3
"""
Chuẩn bị môi trường Python cho skill.

  1. Tạo môi trường ảo .venv ở thư mục gốc repo (có rồi thì dùng lại)
  2. Kiểm tra các thư viện cần thiết có import được không
  3. Cài bổ sung thư viện còn thiếu (theo requirements.txt nếu có)
  4. Dòng cuối in ENV_PY=<đường dẫn python>, để agent/script khác đọc

Dùng:
  python prepare_env.py          # tạo môi trường + cài thư viện, in ENV_PY
  python prepare_env.py --check  # chỉ kiểm tra, thiếu thì thoát với mã khác 0
"""
from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

# skill 根目录 = 本脚本向上两级
SKILL_ROOT = Path(__file__).resolve().parent.parent
VENV_ROOT = SKILL_ROOT / ".venv"

REQUIREMENTS = SKILL_ROOT / "requirements.txt"

# tên import -> tên gói pip (phiên bản lấy từ requirements.txt)
DEPS: dict[str, str] = {
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "av": "av",  # PyAV: chuyển mã H.264 dự phòng khi không có ffmpeg
    "PIL": "Pillow",  # render_annotation_preview.py vẽ ảnh kiểm tra vùng
    "edge_tts": "edge-tts",  # tts_narration.py --provider edge (giọng miễn phí)
}


def requirement_specs() -> dict[str, str]:
    """Đọc requirements.txt → {tên gói viết thường: dòng yêu cầu có phiên bản}."""
    specs: dict[str, str] = {}
    if REQUIREMENTS.exists():
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name = line.split(";")[0]
                for op in ("<", ">", "=", "!", "~", "["):
                    name = name.split(op)[0]
                specs[name.strip().lower()] = line
    return specs


def interpreter_path() -> Path:
    """虚拟环境里的 python 可执行文件位置（跨平台）。"""
    if sys.platform.startswith("win"):
        return VENV_ROOT / "Scripts" / "python.exe"
    return VENV_ROOT / "bin" / "python"


def ensure_venv(check_only: bool) -> Path:
    py = interpreter_path()
    if VENV_ROOT.exists() and py.exists():
        print(f"[ok] Dùng lại môi trường ảo: {VENV_ROOT}")
        return py

    if check_only:
        print(f"[lỗi] Chưa có môi trường ảo: {VENV_ROOT}")
        sys.exit(1)

    print(f"[..] Tạo môi trường ảo: {VENV_ROOT}")
    venv.create(str(VENV_ROOT), with_pip=True)
    print("[ok] Môi trường ảo sẵn sàng")
    return py


def can_import(py: Path, import_name: str) -> bool:
    probe = subprocess.run(
        [str(py), "-c", f"import {import_name}"],
        capture_output=True,
    )
    return probe.returncode == 0


def install(py: Path, packages: list[str]) -> bool:
    if not packages:
        return True
    print(f"[..] Cài thư viện: {', '.join(packages)}")
    res = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", *packages],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"[lỗi] Cài thất bại:\n{res.stderr}")
        return False
    print("[ok] Đã cài xong thư viện")
    return True


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):  # console Windows mặc định cp1252, in tiếng Việt sẽ lỗi
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    check_only = "--check" in sys.argv

    py = ensure_venv(check_only)

    missing: list[str] = []
    for import_name, pip_name in DEPS.items():
        if can_import(py, import_name):
            print(f"[ok] {pip_name}")
        else:
            print(f"[miss] {pip_name}")
            missing.append(pip_name)

    if missing:
        if check_only:
            print(f"\nThiếu {len(missing)} thư viện: {', '.join(missing)}")
            sys.exit(1)
        # Chỉ cài gói còn thiếu (không đụng gói đã có, vd opencv-python bản đầy đủ đã cài trước)
        specs = requirement_specs()
        if not install(py, [specs.get(name.lower(), name) for name in missing]):
            sys.exit(1)

    # 末行：供调用方捕获的约定输出
    print(f"\nENV_PY={py}")


if __name__ == "__main__":
    main()
