# PianoTube

Tự động làm video nhạc piano cổ điển cho YouTube: phát file MIDI bằng tiếng piano thật (Salamander Grand Piano), dựng hình, và dùng Gemini để tạo ảnh nền, tiêu đề, mô tả, tag.

Mỗi lần chạy tạo một thư mục trong `output/`:

| File | Dùng để |
|---|---|
| `video.mp4` | Video để tải lên |
| `thumbnail.jpg` | Ảnh thumbnail 1280×720 |
| `title.txt`, `description.txt`, `tags.txt` | Copy/dán vào YouTube Studio |
| `metadata.json` | Toàn bộ thông tin trên |

## Cài đặt (một lần)

```bash
brew install ffmpeg fluid-synth python@3.12
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
./scripts/setup_assets.sh          # tải soundfont (~300 MB) + 5 bài mẫu
cp .env.example .env               # rồi điền GEMINI_API_KEY
```

Chưa có `GEMINI_API_KEY` thì app vẫn chạy: dùng nền gradient và mô tả theo mẫu có sẵn.

## Giao diện (dễ nhất)

Bấm đúp file **`Mo PianoTube.command`** trong Finder. Giao diện sẽ tự mở trong trình duyệt tại http://127.0.0.1:7860.

- **Tạo video:** chọn loại video, nhập chủ đề, chọn thời lượng rồi bấm ▶ Tạo video.
- **Thư viện video:** xem lại, nghe từng bài, sửa tiêu đề/mô tả/tag, bấm nút copy để dán vào YouTube Studio.
- **Thư viện MIDI:** thêm bài cổ điển mới.
- **Cài đặt:** nhập Gemini API key.

Muốn tắt app thì đóng cửa sổ Terminal đi kèm.

Lần đầu bấm, macOS có thể chặn vì file tải từ nơi khác. Khi đó: chuột phải vào file → **Open** → **Open**.

## Dùng bằng dòng lệnh

### Playlist piano ambient nhẹ nhàng (nhạc tự sáng tác), khuyên dùng

Phong cách "nơi yên tĩnh giữa cuộc sống ồn ào": piano chậm, thưa nốt, tiếng vang dài, có lớp pad nền.
Ảnh nền là ảnh điện ảnh (một người nhỏ bé giữa thiên nhiên) do Gemini tạo. Tiêu đề là câu tâm sự viết chữ thường.

```bash
./pianotube.sh ambient --mood "when the world feels too loud" --minutes 50
./pianotube.sh ambient --mood "it's okay to rest today" --minutes 45 --scene "an empty beach at dusk"
./pianotube.sh ambient --mood "missing someone" --minutes 50 --seed 1234   # cùng seed = cùng giai điệu
```

- Mỗi video có giai điệu mới, là **nhạc gốc của bạn**. File MIDI và WAV từng bài được giữ trong `tracks/`, có thể dùng để phát hành lên Spotify.
- Tiêu đề, mô tả và tên bài do Gemini viết theo đúng giọng "an ủi" của kênh.
- Thumbnail mặc định chỉ có ảnh, không chữ. Thêm `--thumb-text` nếu muốn có chữ.
- Khi đăng: YouTube Studio → **Altered or synthetic content → Yes**. Ảnh nền do AI tạo trông như ảnh thật nên cần khai báo.

### Nhạc cổ điển và phím đàn rơi

```bash
# Xem thư viện
./pianotube.sh library list

# Mẫu B: phím đàn rơi cho 1 bài (~30 giây dựng cho 1 bài 3 phút)
./pianotube.sh falling claude_debussy_clair_de_lune

# Mẫu A: nhạc thư giãn dài (ghép nhạc rất nhanh vì hình được lặp)
./pianotube.sh relax --theme "rainy night study" --minutes 120
./pianotube.sh relax --theme "sleep" --minutes 180 --composer Satie
./pianotube.sh relax --theme "cozy cafe" --minutes 60 --visualizer   # thêm sóng nhạc (chậm hơn)
# relax mặc định làm mềm tiếng đàn (khẽ hơn, chậm hơn, vang hơn); thêm --bright để giữ nguyên bản
```

## Thêm bài mới

1. Vào https://www.mutopiaproject.org, lọc Instrument = Piano.
2. Chọn bài có giấy phép **Public Domain** hoặc **CC BY / CC BY-SA**, tải file `.mid`.
3. Thêm vào thư viện, ghi đúng giấy phép và link nguồn (dùng để ghi công tự động):

```bash
./pianotube.sh library add ~/Downloads/xyz.mid --title "Prelude in E minor, Op. 28 No. 4" \
  --composer "Frédéric Chopin" --license "Public Domain" --source "https://www.mutopiaproject.org/..."
```

App tự ưu tiên bài ít dùng, nên thư viện càng lớn thì video càng ít lặp lại.

## Trước khi đăng: nên làm

- **Xem lại video và sửa mô tả** cho có chất riêng. YouTube không cho kiếm tiền với nội dung "sản xuất hàng loạt, lặp lại". Hãy thay đổi chủ đề, ảnh nền, cách chọn bài giữa các video.
- **Giữ nguyên phần "Music credits"** trong mô tả. Soundfont Salamander (CC BY 3.0) và một số MIDI (CC BY-SA) yêu cầu ghi công.
- Tải lên ở chế độ **Riêng tư** trước, đợi YouTube kiểm tra bản quyền xong rồi mới công khai.
- Nhạc thư giãn dài nên để chế độ **"Made for kids: No"** và chọn danh mục **Music**.

## Chi phí

FFmpeg, FluidSynth, soundfont và MIDI đều miễn phí. Gemini tạo văn bản gần như nằm trong gói miễn phí. Tạo ảnh tốn vài cent mỗi ảnh (mỗi video 1 ảnh). Thêm `--no-ai-image` để không tốn đồng nào.
