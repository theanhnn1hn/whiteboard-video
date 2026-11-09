# Lộ trình nâng cấp

Các đề xuất để tool thành sản phẩm hoàn chỉnh, xếp theo tác động lên chất lượng video và độ tin cậy của quy trình. Mỗi mục ghi rõ vấn đề hiện tại, cách làm và ước lượng công sức (S: vài giờ, M: 1–2 ngày, L: vài ngày trở lên).

Đã làm trong đợt này: sửa lỗi renderer / preview / TTS, video 1920x1080 nhanh hơn ~7 lần, ElevenLabs, Việt hoá giao diện, `validate_annotation.py`, `burn_subtitles.py`, bộ test và CI.

## Ưu tiên cao

### 1. Đồng bộ theo từng từ (M)

**Vấn đề:** hình chỉ khớp giọng ở mức câu. Câu "Anh đưa cậu chiếc ô" dài 3 giây, nhưng chiếc ô có thể được vẽ trước khi từ "chiếc ô" được đọc.

**Cách làm:**
- ElevenLabs có `POST /v1/text-to-speech/{voice_id}/with-timestamps`, trả mốc từng ký tự. Lưu cạnh file cache (`cue-001-….align.json`).
- Với CIT/Edge, dùng forced alignment (vd `whisperX` hoặc `aeneas`) để lấy mốc từng từ.
- Annotation thêm trường tuỳ chọn `reveal.cueWord: "chiếc ô"`; `retime_annotations.py` đặt `startMs` đúng lúc từ đó được đọc.
- Phụ đề kiểu karaoke (ASS, tô sáng từ đang đọc), rất hợp video ngắn.

### 2. Tự đề xuất vùng từ ảnh (M)

**Vấn đề:** agent phải ước lượng toạ độ pixel bằng mắt. Đây là bước dễ sai nhất (vùng lệch, sót nét, phải sửa nhiều vòng).

**Cách làm:** script `propose_regions.py` tìm các cụm nét bằng OpenCV (ngưỡng thích nghi → giãn nở → connected components → gộp cụm gần nhau), xuất `candidates.json` cùng ảnh đánh số. Agent chỉ còn việc đặt tên, gộp và xếp thứ tự theo phụ đề, không phải đoán toạ độ. Có thể kết hợp model segmentation (SAM) khi cần tách vật chồng nhau. `validate_annotation.py` đã có sẵn phép kiểm tra "nét vẽ ngoài mọi vùng" để xác nhận độ phủ.

### 3. Âm thanh hoàn chỉnh (M)

**Vấn đề:** video chỉ có giọng đọc, nghe "trống".

**Cách làm:**
- Nhạc nền (`--music bg.mp3`) tự giảm âm lượng khi có giọng (ffmpeg `sidechaincompress`).
- Tiếng bút sột soạt: renderer biết chính xác lúc nào đang đi nét (ink) hay tô màu, nên xuất được một track hiệu ứng khớp từng khung hình.
- Chuẩn hoá độ to theo nền tảng (`loudnorm`: −14 LUFS cho YouTube/TikTok).

### 4. Khung dọc 9:16 và camera theo vùng đang vẽ (L)

**Vấn đề:** README nhắm tới TikTok/Reels/Shorts, nhưng cả quy trình là ảnh ngang 16:9. Lên khung dọc sẽ bị viền đen trên dưới.

**Cách làm:**
- Preset `--aspect 9:16` cho ảnh, renderer và phụ đề (chữ to hơn, đặt cao hơn để tránh nút bấm của ứng dụng).
- Camera Ken Burns: renderer đã biết khung của vùng đang vẽ, nên tính được đường camera mượt (zoom vào vùng đang vẽ, lùi ra ở cuối cảnh). Cách này cũng dùng lại được ảnh 16:9 cho khung dọc.

### 5. Một lệnh làm cả video, render song song (M)

**Vấn đề:** agent phải gọi 6–8 script theo đúng thứ tự, mỗi lần sửa nhỏ lại render lại tất cả.

**Cách làm:** `make_video.py <thư-mục-dự-án>` đọc `project.json` (thứ tự cảnh, engine, giọng, nhạc), chạy validate → render các cảnh song song (mỗi nhân CPU một cảnh) → ghép → phụ đề → giọng. Cảnh nào có ảnh và annotation không đổi (so hash) thì bỏ qua. Các điểm dừng duyệt trong SKILL.md vẫn giữ: lệnh có `--until render`, `--only scene-02`.

## Ưu tiên vừa

### 6. Chữ viết tay động (M)

Video giải thích thường cần từ khoá và con số, nhưng quy chuẩn ảnh cấm chữ vì AI vẽ chữ hỏng. Giải pháp: thêm loại vùng `"type": "text"` trong annotation. Renderer tự viết chữ tiếng Việt bằng font viết tay có đủ dấu, đi theo nét chữ như bút thật. Chữ không nằm trong ảnh AI nên luôn đúng chính tả.

### 7. Tích hợp tạo ảnh và kiểm tra ảnh (M)

- Gọi API tạo ảnh với khuôn prompt khoá phong cách và ảnh nhân vật tham chiếu, để các cảnh giữ cùng một nhân vật.
- `check_image.py`: màu nền gần `#F5EBD7`, tỉ lệ 16:9, không có chữ (OCR), bảng màu chỉ gồm đỏ/cam/xanh dương (đo theo histogram màu). Ảnh sai thì báo trước khi chia vùng.

### 8. Trang xem trước có lời đọc (M)

- Phát `narration.m4a` đồng bộ với thanh thời gian, vẽ dạng sóng âm và các khối câu phụ đề, để kéo mốc vùng khớp tiếng bằng mắt và tai.
- Thêm hoàn tác/làm lại (Ctrl+Z), nút chạy `validate_annotation` ngay trên trang (cùng các quy tắc), và xem bản render nhanh `--cap-long-edge 960`.

### 9. ElevenLabs nâng cao (S–M)

- **Từ điển phát âm** (`pronunciation_dictionary_locators`) cho tên riêng, viết tắt ("TP.HCM", "AI", tên thương hiệu).
- **Thẻ cảm xúc của v3** (`[whispers]`, `[laughs]`, `[excited]`): cho phép đánh dấu trong `script.md`, script chuyển sang thẻ khi gọi API và bỏ khỏi phụ đề.
- **Nối giọng bằng `previous_request_ids`** thay cho `previous_text` khi tạo lại một câu, để ngữ điệu khớp hai câu bên cạnh.
- **Tạo song song** theo giới hạn đồng thời của gói (nhanh hơn nhiều lần với video dài).
- **Giọng của chính bạn** (Instant Voice Cloning, cần sự đồng ý của người được nhân bản giọng).

### 10. Chuẩn hoá văn bản tiếng Việt trước khi đọc (S)

Số, ngày tháng, đơn vị, viết tắt ("2026", "15%", "km/h", "TP.HCM") hay bị đọc sai, nhất là với Edge/CIT. Thêm bước chuẩn hoá (vd thư viện `vinorm`) chỉ cho văn bản gửi TTS; phụ đề giữ nguyên chữ số.

### 11. Chuyển cảnh (S)

Hiện các cảnh được nối cắt thẳng. Thêm `--transition erase|fade` (ffmpeg `xfade`, hoặc bàn tay cầm khăn lau bảng). Thời gian chồng lấn phải được trừ vào `sceneDurationMs` để không lệch tiếng.

## Kỹ thuật

| # | Việc | Công sức |
|---|---|---|
| 12 | Bỏ ~700 dòng không dùng trong `stream_render.py` (renderer cả ảnh, CLI riêng, logic "pause"); tách module: hình học, nét bút, bàn tay, mã hoá | M |
| 13 | Ghi H.264 trực tiếp qua đường ống ffmpeg thay vì mp4v rồi chuyển mã: nhanh hơn, không mất chất lượng hai lần | S |
| 14 | Giảm dung lượng repo: bỏ `preview-local.html` 3,8 MB, ảnh trùng giữa `examples/` và dự án mẫu, `tts-cache/` đã commit; cân nhắc Git LFS cho media | S |
| 15 | Thêm `ruff` (lint) và kiểm tra kiểu dữ liệu vào CI | S |
| 16 | Việt hoá nốt comment nội bộ còn tiếng Trung trong `stream_render.py` / `render_stream_whiteboard.py` | S |
| 17 | Thêm phong cách nền: bảng đen phấn trắng, giấy kẻ ô, blueprint (đổi màu giấy và màu mực trong renderer) | M |
