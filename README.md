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

## Kết nối YouTube: đăng tự động và hẹn lịch

Làm 1 lần (chi tiết có trong tab **📺 YouTube** của giao diện):

1. [Google Cloud Console](https://console.cloud.google.com/): tạo project, bật **YouTube Data API v3**.
2. **OAuth consent screen**: chọn External, thêm email của bạn vào Test users, rồi bấm **Publish app** (để token không hết hạn sau 7 ngày).
3. **Credentials → OAuth client ID → Desktop app**, tải file JSON về, rồi kéo vào tab YouTube (hoặc lưu thành `credentials/client_secret.json`).
4. Bấm **🔑 Đăng nhập tài khoản YouTube**. Đăng nhập được nhiều tài khoản.

⚠️ Project chưa qua [kiểm duyệt YouTube API](https://support.google.com/youtube/contact/yt_api_form) thì video đăng qua API **bị khoá ở chế độ Riêng tư**. Gửi form kiểm duyệt (miễn phí, mất vài tuần) để đăng công khai và hẹn giờ tự động.

Tính năng:
- **Hẹn lịch:** đặt khung giờ mỗi ngày (vd `08:00, 20:00`). Mỗi video tự lấy khung giờ trống tiếp theo, YouTube tự công khai đúng giờ.
- **Gemini viết lại tiêu đề:** nút ✍️ trong Thư viện video. Tracklist và ghi công được giữ nguyên. Có ô ghi chú phong cách ở tab YouTube.
- **Tự động hàng loạt:** Gemini nghĩ N chủ đề cảm xúc, app làm N video ambient và hẹn đăng vào N khung giờ liên tiếp.
- Thumbnail được đặt tự động (kênh cần xác minh số điện thoại). Tự khai báo "nội dung AI" khi dùng ảnh Gemini. Lịch sử đăng lưu trong `data/uploads.json`.

```bash
./pianotube.sh youtube login
./pianotube.sh ambient --mood "it's okay to rest" --minutes 50 --upload           # làm xong tự đăng theo lịch
./pianotube.sh upload 2026-09-23_1518_when_the_world_feels_too_loud --mode at --at "2026-09-25 20:00"
./pianotube.sh retitle 2026-09-23_1518_when_the_world_feels_too_loud --note "ngắn hơn"
./pianotube.sh batch --count 7 --minutes 50 --upload                               # 1 tuần video, mỗi tối 1 video
```

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
