"""Giao diện web chạy trên máy: python -m pianotube.ui"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import gradio as gr

from . import config, imaging, library, publish, scheduler, settings, youtube
from .cli import SCENES

ENV_FILE = config.ROOT / ".env"
RANDOM_SCENE = "Ngẫu nhiên"
AUTO_BG = "🎲 Tự chọn (ảnh lâu chưa dùng nhất)"
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


def bg_names() -> list:
    return [p.name for p in imaging.local_backgrounds()]


def bg_gallery() -> list:
    return [(str(p), imaging.describe_background(p)) for p in imaging.local_backgrounds()]


def bg_preview(name: str):
    return str(imaging.BG_DIR / name) if name and name != AUTO_BG else None


def add_backgrounds(files):
    import shutil
    if not files:
        return gr.update(), "Chưa chọn ảnh.", gr.update(), gr.update()
    imaging.BG_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in files:
        src = Path(f)
        if src.suffix.lower() in imaging.BG_EXTS:
            shutil.copy(src, imaging.BG_DIR / src.name)
            n += 1
    return (bg_gallery(), f"✓ Đã thêm {n} ảnh.", gr.update(choices=[AUTO_BG] + bg_names()), bg_gallery())


def _ok(flag: bool) -> str:
    return "✅" if flag else "⚠️"


def overview_md() -> str:
    try:
        sf = config.soundfont_path().name
    except SystemExit:
        sf = None
    key = bool(read_env().get("GEMINI_API_KEY"))
    acc = dict((v, k) for k, v in youtube.accounts()).get(youtube.active_account())
    n_bg = len(imaging.local_backgrounds())
    s = settings.load()
    nxt = scheduler.next_run(s)
    auto = (f"Bật · lượt tới **{nxt:%H:%M %d/%m}**" if nxt else "Bật, nhưng chưa có giờ/ngày hợp lệ") \
        if s["auto_enabled"] else "Tắt"
    bg_mode = " · chạy nền macOS ✓" if scheduler.launchd_installed() else ""
    rows = [
        f"| {_ok(sf)} | Âm thanh piano | {sf or 'Chưa có soundfont: chạy `scripts/setup_assets.sh`'} |",
        f"| {_ok(key)} | Gemini (viết tiêu đề, mô tả) | {'Đã có key' if key else 'Chưa có key: vào tab ⚙️ Cài đặt'} |",
        f"| {_ok(acc)} | Kênh YouTube | {acc or 'Chưa đăng nhập: vào tab 📺 YouTube'} |",
        f"| {_ok(n_bg)} | Ảnh nền | {f'{n_bg} ảnh' if n_bg else 'Chưa có ảnh: vào tab 🖼 Ảnh nền'} |",
        f"| {_ok(s['auto_enabled'])} | Lịch tự động | {auto}{bg_mode} |",
        f"| 🗓 | Khung giờ đăng trống tiếp theo | {next_slot_text() or '—'} |",
    ]
    return "| | Mục | Trạng thái |\n|---|---|---|\n" + "\n".join(rows)


def recent_uploads_rows(n=8):
    return uploads_rows()[:n]


# ---------- tạo video ----------

def build_argv(kind, bg, mood, amb_minutes, scene, custom_scene, seed, amb_effect, amb_thumb,
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
    if bg and bg != AUTO_BG:
        argv += ["--background", bg]
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


def run_now():
    """Chạy ngay một lượt tự động theo cài đặt lịch, hiện nhật ký trực tiếp."""
    if not _lock.acquire(blocking=False):
        raise gr.Error("Đang có tác vụ khác chạy trong giao diện, đợi xong đã.")
    if scheduler.is_running():
        _lock.release()
        raise gr.Error("Một lượt tự động đang chạy nền. Xem nhật ký bên dưới.")
    try:
        log, status = [], "⏳ Bắt đầu…"
        for line in run_cli(["autorun", "--force"]):
            if line.startswith("__EXIT__"):
                status = "🏁 Hoàn tất." if line == "__EXIT__0" else "✗ Có lỗi hoặc đã dừng."
                break
            if line.startswith(("frame=", "size=")):
                continue
            log.append(line)
            if line.startswith(("━━", "🧠", "📤", "✗", "🏁", "⏰")):
                status = line
            yield status, "\n".join(log[-80:])
        yield status, "\n".join(log[-80:])
    finally:
        _lock.release()


def auto_status_md() -> str:
    s = settings.load()
    st = scheduler.load_state()
    lines = []
    if scheduler.is_running():
        lines.append("🔄 **Đang chạy một lượt tự động…** (nhật ký bên dưới tự cập nhật)")
    nxt = scheduler.next_run(s)
    if not s["auto_enabled"]:
        lines.append("⏸ Lịch tự động đang **tắt**.")
    elif nxt:
        lines.append(f"⏰ Lượt tiếp theo: **{nxt:%H:%M, %d/%m/%Y}** ({scheduler.DAY_NAMES[nxt.weekday()]})")
    else:
        lines.append("⚠️ Chưa có giờ hoặc ngày chạy hợp lệ.")
    lines.append("🖥 Chạy nền khi tắt giao diện: **" + ("đang bật" if scheduler.launchd_installed() else "tắt")
                 + "**")
    md = "\n\n".join(lines)
    if st["runs"]:
        rows = ["| Bắt đầu | Kết thúc | Kết quả | Video đã đăng |", "|---|---|---|---|"]
        for r in reversed(st["runs"][-6:]):
            res = "⏳ đang chạy" if r["ok"] is None else ("✅ xong" if r["ok"] else "✗ lỗi")
            ups = " ".join(f"[link]({u})" for u in r["uploads"]) or "—"
            rows.append(f"| {r['started']}{' (tay)' if r['manual'] else ''} | {r['finished'] or '—'} | {res} | {ups} |")
        md += "\n\n**Các lượt gần đây**\n\n" + "\n".join(rows)
    return md


def save_auto(enabled, times_txt, days, count, minutes, effect, upload, mode_label, background):
    if enabled and not youtube.parse_slots(times_txt):
        raise gr.Error("Giờ chạy không hợp lệ. Ví dụ đúng: 09:00, 21:00")
    if enabled and not days:
        raise gr.Error("Chọn ít nhất một ngày trong tuần.")
    s = settings.save(auto_enabled=bool(enabled), auto_times=times_txt, auto_days=[int(d) for d in days],
                      auto_count=int(count), auto_minutes=float(minutes), auto_effect=effect,
                      auto_upload=bool(upload), auto_mode=MODES[mode_label])
    msg = "✓ Đã lưu lịch."
    try:
        if enabled and background:
            scheduler.install_launchd(s)
            msg += " Đã bật chạy nền macOS."
        elif scheduler.launchd_installed():
            scheduler.uninstall_launchd()
            msg += " Đã tắt chạy nền macOS."
    except Exception as e:
        msg += f" ⚠️ Không cài được chạy nền: {e}"
    return msg, auto_status_md()


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
.gradio-container {max-width: 1200px !important; margin: auto;}
#status {font-size: 1.05rem;}
#hero h1 {margin-bottom: 0;}
#hero p {opacity: .75; margin-top: 4px;}
.step-note {opacity: .75; font-size: .92rem;}
"""

KIND_AMB, KIND_REL, KIND_FALL = "Ambient (nhạc tự sáng tác)", "Thư giãn (nhạc cổ điển)", "Phím đàn rơi (1 bài)"


def build() -> gr.Blocks:
    env = read_env()
    st = settings.load()
    mode_label = {v: k for k, v in MODES.items()}
    with gr.Blocks(title="PianoTube") as app:
        gr.Markdown("# 🎹 PianoTube\nTự làm video nhạc piano thư giãn và đăng lên YouTube.", elem_id="hero")

        # ---------------- Tổng quan ----------------
        with gr.Tab("🏠 Tổng quan"):
            with gr.Row():
                with gr.Column(scale=5):
                    gr.Markdown("### Tình trạng")
                    ov = gr.Markdown(overview_md())
                    ov_refresh = gr.Button("↻ Làm mới", size="sm")
                with gr.Column(scale=6):
                    gr.Markdown("### Bắt đầu thế nào?")
                    gr.Markdown(
                        "1. **🎬 Tạo video**: chọn chủ đề, thời lượng, ảnh nền rồi bấm *Tạo video*.\n"
                        "2. **📂 Video đã tạo**: xem lại, sửa tiêu đề/mô tả, bấm *Đăng video này*.\n"
                        "3. **⏰ Lịch tự động**: đặt giờ để app tự làm video và tự đăng mỗi ngày, "
                        "không cần ngồi bấm.\n\n"
                        "Các mục ⚠️ bên trái cần làm một lần trước khi đăng YouTube.")
            gr.Markdown("### Video đăng gần đây")
            ov_ups = gr.Dataframe(recent_uploads_rows(), headers=["Lịch / trạng thái", "Tiêu đề", "Link", "Tài khoản"],
                                  interactive=False, wrap=True)

        # ---------------- Tạo video ----------------
        with gr.Tab("🎬 Tạo video"):
            with gr.Row():
                with gr.Column(scale=5):
                    kind = gr.Radio([KIND_AMB, KIND_REL, KIND_FALL], value=KIND_AMB, label="① Loại video")

                    with gr.Group(visible=True) as g_amb:
                        mood = gr.Textbox(label="② Cảm xúc / chủ đề (tiếng Anh)",
                                          value="when the world feels too loud",
                                          info="Dùng để đặt tiêu đề, mô tả và tên các bài")
                        amb_minutes = gr.Slider(5, 180, value=50, step=5, label="③ Thời lượng (phút)")

                    with gr.Group(visible=False) as g_rel:
                        theme = gr.Textbox(label="② Chủ đề (tiếng Anh)", value="rainy night study")
                        rel_minutes = gr.Slider(5, 180, value=60, step=5, label="③ Thời lượng (phút)")
                        composer = gr.Dropdown(composer_choices(), value="Tất cả", label="Nhà soạn nhạc")

                    with gr.Group(visible=False) as g_fall:
                        piece = gr.Dropdown(piece_choices(), label="② Bài nhạc")

                    bg = gr.Dropdown([AUTO_BG] + bg_names(), value=AUTO_BG, label="④ Ảnh nền",
                                     info="Bấm vào một ảnh bên dưới để chọn")
                    bg_pick = gr.Gallery(bg_gallery(), columns=5, height=190, allow_preview=False,
                                         show_label=False, object_fit="cover")

                    auto_up = gr.Checkbox(label="📤 Làm xong thì tự đăng YouTube (theo lịch đăng ở tab 📺 YouTube)",
                                          value=False)
                    with gr.Accordion("Tuỳ chọn nâng cao", open=False):
                        with gr.Row():
                            amb_effect = gr.Radio(["grain", "dust", "none"], value="grain", label="Hiệu ứng hình (ambient)")
                            rel_effect = gr.Radio(["dust", "grain", "none"], value="dust", label="Hiệu ứng hình (thư giãn)")
                        with gr.Row():
                            seed = gr.Number(label="Seed giai điệu (để trống = mới)", precision=0)
                            amb_thumb = gr.Checkbox(label="Thêm chữ lên thumbnail", value=False)
                        with gr.Row():
                            visualizer = gr.Checkbox(label="Sóng nhạc (chậm hơn)", value=False)
                            bright = gr.Checkbox(label="Giữ tiếng đàn gốc (thư giãn)", value=False)
                        gr.Markdown("Chỉ dùng khi thư mục ảnh nền trống:", elem_classes="step-note")
                        with gr.Row():
                            scene = gr.Dropdown([RANDOM_SCENE] + SCENES, value=RANDOM_SCENE, label="Cảnh cho Gemini vẽ")
                            custom_scene = gr.Textbox(label="…hoặc tự mô tả cảnh (tiếng Anh)")
                        no_ai = gr.Checkbox(label="Không gọi Gemini vẽ ảnh (dùng nền màu trơn)", value=False)
                    with gr.Row():
                        go = gr.Button("▶ Tạo video", variant="primary", size="lg")
                        stop_btn = gr.Button("⏹ Dừng", size="lg")

                with gr.Column(scale=6):
                    status = gr.Markdown("Sẵn sàng.", elem_id="status")
                    bg_prev = gr.Image(label="Ảnh nền đã chọn", interactive=False, height=240)
                    out_video = gr.Video(label="Video", interactive=False)
                    out_thumb = gr.Image(label="Thumbnail", interactive=False, height=200, visible=False)
                    out_title = gr.Textbox(label="Tiêu đề", buttons=["copy"])
                    out_desc = gr.Textbox(label="Mô tả", lines=6, buttons=["copy"])
                    out_tags = gr.Textbox(label="Tags", buttons=["copy"])
                    with gr.Accordion("Nhật ký", open=False):
                        log = gr.Textbox(lines=14, max_lines=14, show_label=False, autoscroll=True)

            def toggle(k):
                return (gr.update(visible=k == KIND_AMB), gr.update(visible=k == KIND_REL),
                        gr.update(visible=k == KIND_FALL))
            kind.change(toggle, kind, [g_amb, g_rel, g_fall])

            def pick_bg(evt: gr.SelectData):
                names = bg_names()
                name = names[evt.index] if evt.index < len(names) else AUTO_BG
                return name, bg_preview(name)
            bg_pick.select(pick_bg, None, [bg, bg_prev])
            bg.change(bg_preview, bg, bg_prev)

        # ---------------- Lịch tự động ----------------
        with gr.Tab("⏰ Lịch tự động"):
            gr.Markdown("Đến giờ đã đặt, app tự **nghĩ chủ đề → sáng tác nhạc → dựng video → đăng YouTube**. "
                        "Ảnh nền lấy lần lượt trong tab 🖼 Ảnh nền.")
            with gr.Row():
                with gr.Column(scale=5):
                    a_on = gr.Checkbox(label="✅ Bật lịch tự động", value=st["auto_enabled"])
                    a_times = gr.Textbox(label="Giờ chạy mỗi ngày", value=st["auto_times"],
                                         info="Giờ máy, nhiều giờ cách nhau bằng dấu phẩy, vd: 09:00, 21:00")
                    a_days = gr.CheckboxGroup([(n, i) for i, n in enumerate(scheduler.DAY_NAMES)],
                                              value=st["auto_days"], label="Ngày trong tuần")
                    with gr.Row():
                        a_count = gr.Slider(1, 5, value=st["auto_count"], step=1, label="Số video mỗi lượt")
                        a_minutes = gr.Slider(10, 180, value=st["auto_minutes"], step=5, label="Mỗi video (phút)")
                    a_effect = gr.Radio(["grain", "dust", "none"], value=st["auto_effect"], label="Hiệu ứng hình")
                    a_up = gr.Checkbox(label="📤 Đăng YouTube sau khi làm xong", value=st["auto_upload"])
                    a_mode = gr.Radio([m for m in MODES if m != "Giờ cụ thể"],
                                      value=mode_label.get(st["auto_mode"], "Theo khung giờ trống tiếp theo"),
                                      label="Cách đăng")
                    a_bg = gr.Checkbox(label="🖥 Chạy cả khi đã tắt giao diện (macOS tự chạy nền, "
                                             "máy ngủ thì chạy bù khi thức dậy)",
                                       value=scheduler.launchd_installed())
                    with gr.Row():
                        a_save = gr.Button("💾 Lưu lịch", variant="primary")
                        a_now = gr.Button("▶ Chạy ngay 1 lượt")
                        a_stop = gr.Button("⏹ Dừng")
                    a_msg = gr.Markdown()
                with gr.Column(scale=6):
                    a_status = gr.Markdown(auto_status_md())
                    a_log = gr.Textbox(label="Nhật ký lượt gần nhất", value=scheduler.latest_log(), lines=18,
                                       max_lines=18, autoscroll=True)
            gr.Markdown("💡 Nên chọn giờ chạy sớm hơn giờ đăng vài tiếng (vd chạy 14:00, đăng 20:00) để kịp dựng "
                        "và tải video. Khi bật *Chạy cả khi đã tắt giao diện*, máy Mac vẫn cần bật (có thể đang ngủ).",
                        elem_classes="step-note")
            a_timer = gr.Timer(20)
            a_timer.tick(lambda: (auto_status_md(), scheduler.latest_log()), None, [a_status, a_log])
            a_save.click(save_auto, [a_on, a_times, a_days, a_count, a_minutes, a_effect, a_up, a_mode, a_bg],
                         [a_msg, a_status])
            a_now.click(save_auto, [a_on, a_times, a_days, a_count, a_minutes, a_effect, a_up, a_mode, a_bg],
                        [a_msg, a_status]).then(run_now, None, [a_msg, a_log])
            a_stop.click(stop, None, a_msg)

        # ---------------- Video đã tạo ----------------
        with gr.Tab("📂 Video đã tạo"):
            with gr.Row():
                folder = gr.Dropdown(output_folders(), label="Chọn video", scale=4)
                refresh = gr.Button("↻ Làm mới", scale=1)
                open_btn = gr.Button("📁 Mở trong Finder", scale=1)
            with gr.Row():
                with gr.Column(scale=6):
                    lib_video = gr.Video(label="Video", interactive=False)
                    with gr.Row():
                        track = gr.Dropdown([], label="Nghe từng bài (ambient)", scale=2)
                        track_audio = gr.Audio(label="", interactive=False, scale=3)
                    with gr.Group():
                        gr.Markdown("### 📤 Đăng lên YouTube")
                        up_mode = gr.Radio(list(MODES), value="Theo khung giờ trống tiếp theo", label="Cách đăng")
                        up_at = gr.Textbox(label="Giờ công khai (YYYY-MM-DD HH:MM), dùng khi chọn *Giờ cụ thể*",
                                           value=next_slot_text)
                        up_btn = gr.Button("📤 Đăng video này", variant="primary")
                        up_info = gr.Markdown("Chưa chọn video.")
                with gr.Column(scale=5):
                    lib_thumb = gr.Image(label="Thumbnail", interactive=False, height=220)
                    lib_title = gr.Textbox(label="Tiêu đề", buttons=["copy"])
                    lib_desc = gr.Textbox(label="Mô tả", lines=10, buttons=["copy"])
                    lib_tags = gr.Textbox(label="Tags (cách nhau bằng dấu phẩy)", buttons=["copy"])
                    retitle_note = gr.Textbox(label="Yêu cầu thêm cho Gemini (tuỳ chọn)",
                                              placeholder="vd: ngắn hơn, nói về đêm mưa")
                    with gr.Row():
                        save_btn = gr.Button("💾 Lưu thay đổi", variant="primary")
                        retitle_btn = gr.Button("✍️ Gemini viết lại")
                    save_msg = gr.Markdown()

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

        # ---------------- Ảnh nền ----------------
        with gr.Tab("🖼 Ảnh nền"):
            gr.Markdown(f"Ảnh trong thư mục `assets/backgrounds` được dùng lần lượt (ảnh lâu chưa dùng nhất trước). "
                        "Nên dùng ảnh ngang 16:9, từ 1920×1080 trở lên.")
            bg_all = gr.Gallery(bg_gallery(), columns=4, height=520, show_label=False, object_fit="cover")
            with gr.Row():
                bg_files = gr.File(label="Thêm ảnh mới", file_count="multiple", file_types=["image"], scale=3)
                with gr.Column(scale=1):
                    bg_add = gr.Button("➕ Thêm vào thư viện", variant="primary")
                    bg_open = gr.Button("📁 Mở thư mục ảnh")
                    bg_msg = gr.Markdown()
            bg_add.click(add_backgrounds, bg_files, [bg_all, bg_msg, bg, bg_pick])
            bg_open.click(lambda: subprocess.run(["open", str(imaging.BG_DIR)]) and None, None, None)

        # ---------------- Thư viện MIDI ----------------
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

        # ---------------- YouTube ----------------
        with gr.Tab("📺 YouTube"):
            with gr.Row():
                with gr.Column():
                    gr.Markdown("### Tài khoản")
                    cred_md = gr.Markdown(cred_status())
                    acc = gr.Dropdown(account_choices(), value=youtube.active_account() or None,
                                      label="Kênh đang dùng")
                    login_btn = gr.Button("🔑 Đăng nhập thêm kênh YouTube")
                    acc_msg = gr.Markdown()
                    with gr.Accordion("Chưa có client_secret.json? Làm 1 lần theo hướng dẫn", open=False):
                        gr.Markdown(CLIENT_SECRET_HELP)
                        secret_file = gr.File(label="Tải lên client_secret.json", file_types=[".json"])
                with gr.Column():
                    gr.Markdown("### Lịch đăng")
                    slots = gr.Textbox(label="Giờ công khai video mỗi ngày (giờ máy)", value=st["slots"],
                                       info="Nhiều khung cách nhau bằng dấu phẩy, vd: 08:00, 20:00")
                    lead = gr.Number(label="Tải lên trước giờ công khai ít nhất (phút)", value=st["lead_minutes"],
                                     precision=0, info="Để YouTube kịp xử lý video dài")
                    mode = gr.Radio([m for m in MODES if m != "Giờ cụ thể"],
                                    value=mode_label.get(st["mode"], "Theo khung giờ trống tiếp theo"),
                                    label="Cách đăng mặc định (tab Tạo video)")
                    synth = gr.Checkbox(label="Khai báo 'nội dung do AI tạo' (nên bật khi ảnh nền là ảnh AI)",
                                        value=st["synthetic"])
                    note = gr.Textbox(label="Ghi chú cho Gemini khi viết tiêu đề", value=st["title_note"],
                                      placeholder="vd: tiêu đề ngắn, hay nhắc tới đêm khuya và mưa")
                    save_sched = gr.Button("💾 Lưu", variant="primary")
                    sched_msg = gr.Markdown(f"Khung giờ trống tiếp theo: **{next_slot_text()}**")
            gr.Markdown("### Đã đăng")
            up_table = gr.Dataframe(uploads_rows(), headers=["Lịch / trạng thái", "Tiêu đề", "Link", "Tài khoản"],
                                    interactive=False, wrap=True)
            refresh_up = gr.Button("↻ Làm mới", size="sm")

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

        # ---------------- Cài đặt ----------------
        with gr.Tab("⚙️ Cài đặt"):
            gr.Markdown("Lấy Gemini API key miễn phí tại [Google AI Studio](https://aistudio.google.com/apikey). "
                        "Không có key app vẫn chạy, nhưng tiêu đề/mô tả sẽ theo mẫu có sẵn.")
            key = gr.Textbox(label="Gemini API key", type="password", value=env.get("GEMINI_API_KEY", ""))
            with gr.Row():
                tmodel = gr.Textbox(label="Model viết chữ", value=env.get("GEMINI_TEXT_MODEL", config.GEMINI_TEXT_MODEL))
                imodel = gr.Textbox(label="Model tạo ảnh", value=env.get("GEMINI_IMAGE_MODEL", config.GEMINI_IMAGE_MODEL))
            with gr.Row():
                save_env = gr.Button("💾 Lưu", variant="primary")
                test = gr.Button("Kiểm tra key")
            env_msg = gr.Markdown()
            gr.Markdown(soundfont_status())
            save_env.click(write_env, [key, tmodel, imodel], env_msg)
            test.click(test_key, [key, tmodel], env_msg)

        ov_refresh.click(lambda: (overview_md(), recent_uploads_rows()), None, [ov, ov_ups])
        app.load(lambda: (overview_md(), recent_uploads_rows(), gr.update(choices=[AUTO_BG] + bg_names()),
                          bg_gallery(), bg_gallery()), None, [ov, ov_ups, bg, bg_pick, bg_all])

        inputs = [kind, bg, mood, amb_minutes, scene, custom_scene, seed, amb_effect, amb_thumb,
                  theme, rel_minutes, composer, rel_effect, bright, piece, visualizer, no_ai, auto_up]
        go.click(generate, inputs, [status, log, out_video, out_thumb, out_title, out_desc, out_tags, folder])
        stop_btn.click(stop, None, status)
    return app


def main():
    scheduler.start_watcher(read_env)
    build().queue().launch(inbrowser=not os.getenv("PIANOTUBE_NO_BROWSER"), server_port=7860,
                           allowed_paths=[str(config.OUTPUT_DIR), str(imaging.BG_DIR)],
                           theme=gr.themes.Soft(primary_hue="indigo"), css=CSS)


if __name__ == "__main__":
    main()
