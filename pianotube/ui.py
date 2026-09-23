"""Giao diện web chạy trên máy: python -m pianotube.ui"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import gradio as gr

from . import config, library, publish, settings, youtube
from .cli import SCENES

ENV_FILE = config.ROOT / ".env"
RANDOM_SCENE = "Ngẫu nhiên"
_proc = {"p": None}
_lock = threading.Lock()


# ---------- tiện ích ----------

def run_cli(argv: list):
    """Chạy lệnh pianotube trong tiến trình con, trả dần từng dòng log."""
    # Ưu tiên giá trị mới nhất trong .env (vừa lưu ở tab Cài đặt) hơn biến môi trường cũ.
    env = {**os.environ, **read_env(), "PYTHONUNBUFFERED": "1"}
    p = subprocess.Popen([sys.executable, "-m", "pianotube", *argv], cwd=config.ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _proc["p"] = p
    buf = b""
    while True:
        chunk = os.read(p.stdout.fileno(), 4096)
        if not chunk:
            break
        buf += chunk
        # ffmpeg dùng \r để cập nhật tiến độ trên cùng một dòng.
        *lines, buf = re.split(rb"[\r\n]", buf)
        for line in lines:
            if line.strip():
                yield line.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")
    p.wait()
    _proc["p"] = None
    yield f"__EXIT__{p.returncode}"


def status_from(line: str, current: str) -> str:
    if line.startswith("frame=") or line.startswith("size="):
        m = re.search(r"time=(\S+)", line)
        return f"🎬 Đang ghép video… {m.group(1) if m else ''}"
    s = line.strip()
    if s.startswith(("♪", "🎚", "🎨", "🎞", "🎬", "📝", "dựng hình")):
        return s
    return current


def output_folders() -> list:
    if not config.OUTPUT_DIR.exists():
        return []
    return sorted((d.name for d in config.OUTPUT_DIR.iterdir()
                   if d.is_dir() and not d.name.startswith(".") and (d / "video.mp4").exists()),
                  reverse=True)


def read_text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def load_folder(name: str):
    if not name:
        return None, None, "", "", "", gr.update(choices=[], value=None), None
    d = config.OUTPUT_DIR / name
    thumb = d / "thumbnail.jpg"
    tracks = sorted(str(p) for p in (d / "tracks").glob("*.wav")) if (d / "tracks").exists() else []
    return (str(d / "video.mp4"), str(thumb) if thumb.exists() else None,
            read_text(d / "title.txt"), read_text(d / "description.txt"), read_text(d / "tags.txt"),
            gr.update(choices=[(Path(t).stem, t) for t in tracks], value=tracks[0] if tracks else None),
            tracks[0] if tracks else None)


def save_folder(name: str, title: str, desc: str, tags: str):
    if not name:
        return "Chưa chọn video."
    d = config.OUTPUT_DIR / name
    (d / "title.txt").write_text(title)
    (d / "description.txt").write_text(desc)
    (d / "tags.txt").write_text(tags)
    meta_path = d / "metadata.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update(title=title, description=desc, tags=[t.strip() for t in tags.split(",") if t.strip()])
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return f"✓ Đã lưu lúc {__import__('datetime').datetime.now():%H:%M:%S}"


def piece_choices():
    return [(f"{i['composer']} – {i['title']}", i["id"]) for i in library.all_pieces()]


def composer_choices():
    return ["Tất cả"] + sorted({i["composer"] for i in library.all_pieces()})


def library_rows():
    return [[i["composer"], i["title"], i["license"], i["used"], i["id"]] for i in library.all_pieces()]


def read_env() -> dict:
    vals = {}
    for line in read_text(ENV_FILE).splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def write_env(key: str, text_model: str, image_model: str):
    vals = read_env()
    vals.update(GEMINI_API_KEY=key.strip(), GEMINI_TEXT_MODEL=text_model.strip() or config.GEMINI_TEXT_MODEL,
                GEMINI_IMAGE_MODEL=image_model.strip() or config.GEMINI_IMAGE_MODEL)
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    return "✓ Đã lưu. Lần tạo video tiếp theo sẽ dùng cài đặt mới."


def test_key(key: str, text_model: str):
    if not key.strip():
        return "Chưa nhập API key."
    try:
        from google import genai
        r = genai.Client(api_key=key.strip()).models.generate_content(
            model=text_model.strip() or config.GEMINI_TEXT_MODEL, contents="Reply with the single word: ok")
        return f"✓ Key hoạt động. Gemini trả lời: {r.text.strip()[:40]}"
    except Exception as e:
        return f"✗ Lỗi: {e}"


def soundfont_status() -> str:
    try:
        return f"✓ Soundfont: {config.soundfont_path().name}"
    except SystemExit:
        return "✗ Chưa có soundfont. Chạy scripts/setup_assets.sh trong Terminal."


# ---------- tạo video ----------

def build_argv(kind, mood, amb_minutes, scene, custom_scene, seed, amb_effect, amb_thumb,
               theme, rel_minutes, composer, rel_effect, bright, piece, visualizer, no_ai, auto_up):
    if kind == "Ambient (nhạc tự sáng tác)":
        argv = ["ambient", "--mood", mood or "when the world feels too loud",
                "--minutes", str(amb_minutes), "--effect", amb_effect]
        sc = custom_scene.strip() or (None if scene == RANDOM_SCENE else scene)
        if sc:
            argv += ["--scene", sc]
        if seed:
            argv += ["--seed", str(int(seed))]
        if amb_thumb:
            argv.append("--thumb-text")
    elif kind == "Thư giãn (nhạc cổ điển)":
        argv = ["relax", "--theme", theme or "rainy night study", "--minutes", str(rel_minutes),
                "--effect", rel_effect]
        if composer and composer != "Tất cả":
            argv += ["--composer", composer]
        if bright:
            argv.append("--bright")
    else:
        if not piece:
            raise gr.Error("Hãy chọn một bài nhạc.")
        argv = ["falling", piece]
    if visualizer and kind != "Phím đàn rơi (1 bài)":
        argv.append("--visualizer")
    if no_ai:
        argv.append("--no-ai-image")
    if auto_up:
        argv.append("--upload")
    return argv


def generate(*inputs):
    if not _lock.acquire(blocking=False):
        raise gr.Error("Đang có một video được tạo. Đợi xong hoặc bấm Dừng.")
    try:
        argv = build_argv(*inputs)
        log, status, folder, up_msg = [], "⏳ Bắt đầu…", None, ""
        empty = (None, None, "", "", "")
        yield status, "", *empty, gr.update()
        for line in run_cli(argv):
            if line.startswith("__EXIT__"):
                code = int(line[8:])
                if code != 0 or not folder:
                    status = "✗ Có lỗi hoặc đã dừng. Xem nhật ký bên dưới."
                break
            if line.startswith("frame="):
                status = status_from(line, status)
            else:
                log.append(line)
                status = status_from(line, status)
                m = re.search(r"✓ Xong: (.+)$", line)
                if m:
                    folder = Path(m.group(1).strip()).name
                    status = "✅ Đã tạo xong video" + (", đang đăng YouTube…" if inputs[-1] else "")
                if line.startswith(("📤", "✗ Đăng")):
                    up_msg = "  \n" + line
            yield status, "\n".join(log[-60:]), *empty, gr.update()
        if folder:
            video, thumb, title, desc, tags, *_ = load_folder(folder)
            yield ("✅ Xong! Xem lại, sửa tiêu đề/mô tả ở tab Thư viện video nếu cần." + up_msg,
                   "\n".join(log[-60:]), video, thumb, title, desc, tags,
                   gr.update(choices=output_folders(), value=folder))
        else:
            yield status, "\n".join(log[-60:]), *empty, gr.update()
    finally:
        _lock.release()


def upload_info(name: str) -> str:
    rec = youtube.upload_record(name) if name else None
    if not rec:
        return "Chưa đăng lên YouTube."
    when = f", công khai lúc **{rec['publish_local']}**" if rec.get("publish_local") else f" ({rec['privacy']})"
    return f"📤 Đã đăng {rec['uploaded_at']}{when}: [{rec['url']}]({rec['url']})"


def next_slot_text() -> str:
    s = settings.load()
    try:
        t = youtube.next_free_slot(s["slots"], int(s["lead_minutes"]))
        return t.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def do_retitle(name, note):
    if not name:
        raise gr.Error("Chưa chọn video.")
    try:
        meta = publish.retitle(name, note or None)
    except Exception as e:
        raise gr.Error(str(e))
    return meta["title"], meta["description"], ", ".join(meta["tags"]), "✍️ Gemini đã viết lại. Bấm Lưu nếu muốn sửa thêm."


MODES = {"Theo khung giờ trống tiếp theo": "slot", "Giờ cụ thể": "at", "Riêng tư": "private",
         "Không công khai (unlisted)": "unlisted", "Công khai ngay": "public"}


def do_upload(name, mode_label, at, title, desc, tags, progress=gr.Progress()):
    if not name:
        raise gr.Error("Chưa chọn video.")
    if not _lock.acquire(blocking=False):
        raise gr.Error("Đang có tác vụ khác chạy, đợi xong đã.")
    try:
        save_folder(name, title, desc, tags)            # đăng đúng nội dung đang hiển thị
        logs = []
        rec = publish.publish(name, mode=MODES[mode_label], at=at,
                              progress=lambda f: progress(f, desc="Đang tải lên YouTube"),
                              log=logs.append)
        return upload_info(name) + "\n\n" + "  \n".join(logs)
    except Exception as e:
        raise gr.Error(f"Đăng lỗi: {e}")
    finally:
        _lock.release()


def uploads_rows():
    return [[u.get("publish_local") or u.get("privacy"), u["title"], u["url"], u.get("account", "")]
            for u in reversed(youtube.load_uploads())]


def account_choices():
    return youtube.accounts()


def do_login():
    try:
        label = youtube.login(log=lambda *_: None)
    except Exception as e:
        raise gr.Error(str(e))
    settings.save(account=youtube.active_account())
    return gr.update(choices=account_choices(), value=youtube.active_account()), f"✅ Đã đăng nhập **{label}**"


def save_client_secret(f):
    if not f:
        return "Chưa chọn file."
    data = json.loads(Path(f).read_text())
    if "installed" not in data:
        return "✗ File không đúng loại. Cần OAuth client kiểu **Desktop app**."
    youtube.CRED_DIR.mkdir(exist_ok=True)
    youtube.CLIENT_SECRETS.write_text(json.dumps(data))
    return "✓ Đã lưu client_secret.json. Giờ bấm **Đăng nhập tài khoản YouTube**."


def cred_status() -> str:
    return ("✓ Đã có client_secret.json" if youtube.CLIENT_SECRETS.exists()
            else "✗ Chưa có client_secret.json (xem hướng dẫn bên dưới)")


def run_batch(count, minutes, effect, no_ai, auto_up, mode_label):
    if not _lock.acquire(blocking=False):
        raise gr.Error("Đang có tác vụ khác chạy.")
    try:
        argv = ["batch", "--count", str(int(count)), "--minutes", str(minutes), "--effect", effect]
        if no_ai:
            argv.append("--no-ai-image")
        if auto_up:
            argv += ["--upload", "--upload-mode", MODES[mode_label]]
        log, status = [], "⏳ Bắt đầu…"
        for line in run_cli(argv):
            if line.startswith("__EXIT__"):
                status = "🏁 Hoàn tất." if line == "__EXIT__0" else "✗ Có lỗi hoặc đã dừng."
                break
            if line.startswith("frame="):
                continue
            log.append(line)
            if line.startswith(("━━", "🧠", "📤", "✗", "🏁")):
                status = line
            yield status, "\n".join(log[-80:])
        yield status, "\n".join(log[-80:])
    finally:
        _lock.release()


def stop():
    p = _proc["p"]
    if p and p.poll() is None:
        p.terminate()
        return "⏹ Đã dừng."
    return "Không có tác vụ nào đang chạy."


# ---------- giao diện ----------

CLIENT_SECRET_HELP = """
1. Vào [Google Cloud Console](https://console.cloud.google.com/) → tạo project mới (vd *PianoTube*).
2. **APIs & Services → Library** → bật **YouTube Data API v3**.
3. **OAuth consent screen**: chọn *External*, điền tên app + email; thêm email của bạn vào **Test users**.
   Sau đó bấm **Publish app** để token không bị hết hạn sau 7 ngày (app vẫn chỉ mình bạn dùng).
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → tải file JSON.
5. Kéo file JSON đó vào ô bên dưới, rồi bấm **Đăng nhập tài khoản YouTube**.
   Khi Google cảnh báo *"app chưa được xác minh"*: bấm **Advanced → Go to PianoTube**.

⚠️ **Quan trọng:** YouTube khoá mọi video đăng qua API từ project **chưa được kiểm duyệt** ở chế độ
**Riêng tư**, kể cả video hẹn giờ, và không đổi sang công khai được. Để đăng/hẹn giờ công khai tự động,
gửi form kiểm duyệt miễn phí [YouTube API Audit](https://support.google.com/youtube/contact/yt_api_form)
(thường mất vài tuần). Trong lúc chờ: đăng thử 1 video để kiểm tra kết nối, còn video thật thì bấm
**Mở trong Finder** và tự tải lên YouTube Studio (Studio cũng hẹn giờ được).
"""

CSS = """
.gradio-container {max-width: 1180px !important; margin: auto;}
#status {font-size: 1.05rem;}
"""


def build() -> gr.Blocks:
    env = read_env()
    with gr.Blocks(title="PianoTube") as app:
        gr.Markdown("# 🎹 PianoTube\nTự động làm video nhạc piano cho YouTube.")

        with gr.Tab("🎬 Tạo video"):
            with gr.Row():
                with gr.Column(scale=5):
                    kind = gr.Radio(["Ambient (nhạc tự sáng tác)", "Thư giãn (nhạc cổ điển)", "Phím đàn rơi (1 bài)"],
                                    value="Ambient (nhạc tự sáng tác)", label="Loại video")

                    with gr.Group(visible=True) as g_amb:
                        mood = gr.Textbox(label="Cảm xúc / chủ đề (tiếng Anh)",
                                          value="when the world feels too loud",
                                          info="Gemini dựa vào đây để đặt tiêu đề, mô tả, tên bài")
                        amb_minutes = gr.Slider(5, 180, value=50, step=5, label="Thời lượng (phút)")
                        scene = gr.Dropdown([RANDOM_SCENE] + SCENES, value=RANDOM_SCENE, label="Cảnh nền")
                        custom_scene = gr.Textbox(label="…hoặc tự mô tả cảnh (tiếng Anh)",
                                                  placeholder="e.g. a lighthouse on a foggy coast")
                        with gr.Row():
                            amb_effect = gr.Radio(["grain", "dust", "none"], value="grain",
                                                  label="Hiệu ứng hình")
                            seed = gr.Number(label="Seed (để trống = giai điệu mới)", precision=0)
                        amb_thumb = gr.Checkbox(label="Thêm chữ lên thumbnail", value=False)

                    with gr.Group(visible=False) as g_rel:
                        theme = gr.Textbox(label="Chủ đề (tiếng Anh)", value="rainy night study")
                        rel_minutes = gr.Slider(5, 180, value=60, step=5, label="Thời lượng (phút)")
                        composer = gr.Dropdown(composer_choices(), value="Tất cả", label="Nhà soạn nhạc")
                        rel_effect = gr.Radio(["dust", "grain", "none"], value="dust", label="Hiệu ứng hình")
                        bright = gr.Checkbox(label="Giữ tiếng đàn gốc (không làm mềm)", value=False)

                    with gr.Group(visible=False) as g_fall:
                        piece = gr.Dropdown(piece_choices(), label="Bài nhạc")

                    with gr.Row():
                        visualizer = gr.Checkbox(label="Sóng nhạc (chậm hơn)", value=False)
                        no_ai = gr.Checkbox(label="Không dùng ảnh Gemini", value=not env.get("GEMINI_API_KEY"))
                    auto_up = gr.Checkbox(label="📤 Tự đăng YouTube khi xong (theo lịch ở tab YouTube)", value=False)
                    with gr.Row():
                        go = gr.Button("▶ Tạo video", variant="primary", size="lg")
                        stop_btn = gr.Button("⏹ Dừng", size="lg")

                with gr.Column(scale=6):
                    status = gr.Markdown("Sẵn sàng.", elem_id="status")
                    out_video = gr.Video(label="Video", interactive=False)
                    out_thumb = gr.Image(label="Thumbnail", interactive=False, height=220)
                    out_title = gr.Textbox(label="Tiêu đề", buttons=["copy"])
                    out_desc = gr.Textbox(label="Mô tả", lines=8, buttons=["copy"])
                    out_tags = gr.Textbox(label="Tags", buttons=["copy"])
                    with gr.Accordion("Nhật ký", open=False):
                        log = gr.Textbox(lines=14, max_lines=14, show_label=False, autoscroll=True)

            def toggle(k):
                return (gr.update(visible=k.startswith("Ambient")), gr.update(visible=k.startswith("Thư giãn")),
                        gr.update(visible=k.startswith("Phím")))
            kind.change(toggle, kind, [g_amb, g_rel, g_fall])

        with gr.Tab("📂 Thư viện video"):
            with gr.Row():
                folder = gr.Dropdown(output_folders(), label="Video đã tạo", scale=4)
                refresh = gr.Button("↻ Làm mới", scale=1)
                open_btn = gr.Button("Mở trong Finder", scale=1)
            with gr.Row():
                with gr.Column(scale=6):
                    lib_video = gr.Video(label="Video", interactive=False)
                    with gr.Row():
                        track = gr.Dropdown([], label="Nghe từng bài (ambient)", scale=2)
                        track_audio = gr.Audio(label="", interactive=False, scale=3)
                with gr.Column(scale=5):
                    lib_thumb = gr.Image(label="Thumbnail", interactive=False, height=220)
                    lib_title = gr.Textbox(label="Tiêu đề", buttons=["copy"])
                    lib_desc = gr.Textbox(label="Mô tả", lines=10, buttons=["copy"])
                    lib_tags = gr.Textbox(label="Tags (cách nhau bằng dấu phẩy)", buttons=["copy"])
                    with gr.Row():
                        save_btn = gr.Button("💾 Lưu thay đổi", variant="primary")
                        retitle_btn = gr.Button("✍️ Gemini viết lại tiêu đề")
                    retitle_note = gr.Textbox(label="Yêu cầu thêm cho Gemini (tuỳ chọn)",
                                              placeholder="vd: ngắn hơn, nói về đêm mưa")
                    save_msg = gr.Markdown()
            with gr.Group():
                gr.Markdown("### 📤 Đăng lên YouTube")
                with gr.Row():
                    up_mode = gr.Radio(list(MODES), value="Theo khung giờ trống tiếp theo", label="Cách đăng", scale=3)
                    up_at = gr.Textbox(label="Giờ công khai (YYYY-MM-DD HH:MM)", value=next_slot_text, scale=1)
                up_btn = gr.Button("📤 Đăng video này", variant="primary")
                up_info = gr.Markdown("Chưa chọn video.")
            gr.Markdown("**Khi đăng:** tải lên ở chế độ *Riêng tư* → đợi kiểm tra bản quyền → công khai. "
                        "Video ambient dùng ảnh AI: chọn *Altered or synthetic content → Yes*.")

            lib_outputs = [lib_video, lib_thumb, lib_title, lib_desc, lib_tags, track, track_audio]
            folder.change(load_folder, folder, lib_outputs).then(upload_info, folder, up_info)
            retitle_btn.click(do_retitle, [folder, retitle_note], [lib_title, lib_desc, lib_tags, save_msg])
            up_btn.click(do_upload, [folder, up_mode, up_at, lib_title, lib_desc, lib_tags], up_info)
            refresh.click(lambda: gr.update(choices=output_folders()), None, folder)
            def open_in_finder(n):
                if n:
                    subprocess.run(["open", str(config.OUTPUT_DIR / n)])
            open_btn.click(open_in_finder, folder, None)
            track.change(lambda t: t, track, track_audio)
            save_btn.click(save_folder, [folder, lib_title, lib_desc, lib_tags], save_msg)

        with gr.Tab("🎼 Thư viện MIDI"):
            gr.Markdown("Bài cổ điển dùng cho **Thư giãn** và **Phím đàn rơi**. Tìm file `.mid` tại "
                        "[Mutopia Project](https://www.mutopiaproject.org) (lọc Instrument = Piano), "
                        "chỉ chọn bài **Public Domain** hoặc **CC BY / CC BY-SA**.")
            table = gr.Dataframe(library_rows(), headers=["Nhà soạn nhạc", "Tên bài", "Giấy phép", "Đã dùng", "ID"],
                                 interactive=False)
            with gr.Row():
                up = gr.File(label="File .mid", file_types=[".mid", ".midi"], scale=2)
                with gr.Column(scale=3):
                    m_title = gr.Textbox(label="Tên bài")
                    m_comp = gr.Textbox(label="Nhà soạn nhạc")
                    m_lic = gr.Dropdown(["Public Domain", "CC BY 4.0", "CC BY-SA 4.0", "CC BY 3.0", "CC BY-SA 3.0"],
                                        value="Public Domain", label="Giấy phép", allow_custom_value=True)
                    m_src = gr.Textbox(label="Link nguồn")
                    add_btn = gr.Button("➕ Thêm vào thư viện", variant="primary")
                    add_msg = gr.Markdown()

            def add_midi(f, t, c, lic, src):
                if not (f and t and c):
                    return gr.update(), "Cần chọn file, nhập tên bài và nhà soạn nhạc.", gr.update(), gr.update()
                e = library.add(Path(f), t, c, lic, src)
                return (library_rows(), f"✓ Đã thêm **{e['title']}**",
                        gr.update(choices=piece_choices()), gr.update(choices=composer_choices()))
            add_btn.click(add_midi, [up, m_title, m_comp, m_lic, m_src], [table, add_msg, piece, composer])

        with gr.Tab("🤖 Tự động hàng loạt"):
            gr.Markdown("Gemini nghĩ ra các chủ đề cảm xúc khác nhau → app sáng tác nhạc, dựng video → "
                        "đăng lên YouTube hẹn giờ vào các khung giờ trống liên tiếp (vd mỗi tối 20:00).")
            with gr.Row():
                b_count = gr.Slider(1, 14, value=3, step=1, label="Số video")
                b_minutes = gr.Slider(10, 180, value=50, step=5, label="Mỗi video (phút)")
                b_effect = gr.Radio(["grain", "dust", "none"], value="grain", label="Hiệu ứng hình")
            with gr.Row():
                b_noai = gr.Checkbox(label="Không dùng ảnh Gemini", value=not env.get("GEMINI_API_KEY"))
                b_up = gr.Checkbox(label="📤 Đăng YouTube", value=True)
                b_mode = gr.Radio([m for m in MODES if m != "Giờ cụ thể"], value="Theo khung giờ trống tiếp theo",
                                  label="Cách đăng")
            with gr.Row():
                b_go = gr.Button("🤖 Bắt đầu", variant="primary", size="lg")
                b_stop = gr.Button("⏹ Dừng", size="lg")
            b_status = gr.Markdown("Sẵn sàng.")
            b_log = gr.Textbox(lines=16, max_lines=16, show_label=False, autoscroll=True)
            b_go.click(run_batch, [b_count, b_minutes, b_effect, b_noai, b_up, b_mode], [b_status, b_log])
            b_stop.click(stop, None, b_status)

        with gr.Tab("📺 YouTube"):
            st = settings.load()
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Tài khoản")
                    cred_md = gr.Markdown(cred_status())
                    acc = gr.Dropdown(account_choices(), value=youtube.active_account() or None,
                                      label="Tài khoản đang dùng")
                    login_btn = gr.Button("🔑 Đăng nhập tài khoản YouTube")
                    acc_msg = gr.Markdown()
                    with gr.Accordion("Chưa có client_secret.json? Làm 1 lần theo hướng dẫn", open=False):
                        gr.Markdown(CLIENT_SECRET_HELP)
                        secret_file = gr.File(label="Tải lên client_secret.json", file_types=[".json"])
                with gr.Column():
                    gr.Markdown("### Lịch đăng")
                    slots = gr.Textbox(label="Khung giờ đăng mỗi ngày (giờ máy)", value=st["slots"],
                                       info="Nhiều khung cách nhau bằng dấu phẩy, vd: 08:00, 20:00")
                    lead = gr.Number(label="Đăng trước giờ công khai ít nhất (phút)", value=st["lead_minutes"],
                                     precision=0, info="Để YouTube kịp xử lý video dài")
                    mode = gr.Radio([m for m in MODES if m != "Giờ cụ thể"],
                                    value={v: k for k, v in MODES.items()}.get(st["mode"], "Theo khung giờ trống tiếp theo"),
                                    label="Chế độ mặc định khi tự đăng")
                    synth = gr.Checkbox(label="Khai báo 'nội dung do AI tạo' (khuyên bật khi dùng ảnh Gemini)",
                                        value=st["synthetic"])
                    note = gr.Textbox(label="Ghi chú cho Gemini khi viết tiêu đề", value=st["title_note"],
                                      placeholder="vd: tiêu đề ngắn, hay nhắc tới đêm khuya và mưa")
                    save_sched = gr.Button("💾 Lưu lịch", variant="primary")
                    sched_msg = gr.Markdown(f"Khung giờ trống tiếp theo: **{next_slot_text()}**")
            gr.Markdown("### Đã đăng")
            up_table = gr.Dataframe(uploads_rows(), headers=["Lịch / trạng thái", "Tiêu đề", "Link", "Tài khoản"],
                                    interactive=False)
            refresh_up = gr.Button("↻ Làm mới")

            def save_schedule(a, sl, ld, md, sy, nt):
                if not youtube.parse_slots(sl):
                    raise gr.Error("Khung giờ không hợp lệ. Ví dụ đúng: 08:00, 20:00")
                settings.save(account=a or "", slots=sl, lead_minutes=int(ld or 60), mode=MODES[md],
                              synthetic=sy, title_note=nt)
                if a:
                    youtube.set_active(a)
                return f"✓ Đã lưu. Khung giờ trống tiếp theo: **{next_slot_text()}**"
            save_sched.click(save_schedule, [acc, slots, lead, mode, synth, note], sched_msg)
            def choose_account(a):
                if a:
                    youtube.set_active(a)
                settings.save(account=a or "")
            acc.change(choose_account, acc, None)
            login_btn.click(do_login, None, [acc, acc_msg])
            secret_file.change(save_client_secret, secret_file, acc_msg).then(cred_status, None, cred_md)
            refresh_up.click(uploads_rows, None, up_table)

        with gr.Tab("⚙️ Cài đặt"):
            gr.Markdown("Lấy Gemini API key miễn phí tại [Google AI Studio](https://aistudio.google.com/apikey). "
                        "Không có key app vẫn chạy, nhưng dùng nền đơn giản và mô tả theo mẫu.")
            key = gr.Textbox(label="Gemini API key", type="password", value=env.get("GEMINI_API_KEY", ""))
            with gr.Row():
                tmodel = gr.Textbox(label="Model viết chữ", value=env.get("GEMINI_TEXT_MODEL", config.GEMINI_TEXT_MODEL))
                imodel = gr.Textbox(label="Model tạo ảnh", value=env.get("GEMINI_IMAGE_MODEL", config.GEMINI_IMAGE_MODEL))
            with gr.Row():
                save_env = gr.Button("💾 Lưu", variant="primary")
                test = gr.Button("Kiểm tra key")
            env_msg = gr.Markdown()
            gr.Markdown(soundfont_status())
            save_env.click(write_env, [key, tmodel, imodel], env_msg).then(
                lambda k: gr.update(value=not k.strip()), key, no_ai)
            test.click(test_key, [key, tmodel], env_msg)

        inputs = [kind, mood, amb_minutes, scene, custom_scene, seed, amb_effect, amb_thumb,
                  theme, rel_minutes, composer, rel_effect, bright, piece, visualizer, no_ai, auto_up]
        go.click(generate, inputs, [status, log, out_video, out_thumb, out_title, out_desc, out_tags, folder])
        stop_btn.click(stop, None, status)
    return app


def main():
    build().queue().launch(inbrowser=not os.getenv("PIANOTUBE_NO_BROWSER"), server_port=7860, allowed_paths=[str(config.OUTPUT_DIR)],
                           theme=gr.themes.Soft(primary_hue="indigo"), css=CSS)


if __name__ == "__main__":
    main()
