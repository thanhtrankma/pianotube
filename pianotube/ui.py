"""Giao diện web chạy trên máy: python -m pianotube.ui"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import gradio as gr

from . import config, library
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
               theme, rel_minutes, composer, rel_effect, bright, piece, visualizer, no_ai):
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
    return argv


def generate(*inputs):
    if not _lock.acquire(blocking=False):
        raise gr.Error("Đang có một video được tạo. Đợi xong hoặc bấm Dừng.")
    try:
        argv = build_argv(*inputs)
        log, status, folder = [], "⏳ Bắt đầu…", None
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
            yield status, "\n".join(log[-60:]), *empty, gr.update()
        if folder:
            video, thumb, title, desc, tags, *_ = load_folder(folder)
            yield ("✅ Xong! Xem lại, sửa tiêu đề/mô tả ở tab Thư viện video nếu cần.",
                   "\n".join(log[-60:]), video, thumb, title, desc, tags,
                   gr.update(choices=output_folders(), value=folder))
        else:
            yield status, "\n".join(log[-60:]), *empty, gr.update()
    finally:
        _lock.release()


def stop():
    p = _proc["p"]
    if p and p.poll() is None:
        p.terminate()
        return "⏹ Đã dừng."
    return "Không có tác vụ nào đang chạy."


# ---------- giao diện ----------

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
                    save_btn = gr.Button("💾 Lưu thay đổi", variant="primary")
                    save_msg = gr.Markdown()
            gr.Markdown("**Khi đăng:** tải lên ở chế độ *Riêng tư* → đợi kiểm tra bản quyền → công khai. "
                        "Video ambient dùng ảnh AI: chọn *Altered or synthetic content → Yes*.")

            lib_outputs = [lib_video, lib_thumb, lib_title, lib_desc, lib_tags, track, track_audio]
            folder.change(load_folder, folder, lib_outputs)
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
                  theme, rel_minutes, composer, rel_effect, bright, piece, visualizer, no_ai]
        go.click(generate, inputs, [status, log, out_video, out_thumb, out_title, out_desc, out_tags, folder])
        stop_btn.click(stop, None, status)
    return app


def main():
    build().queue().launch(inbrowser=not os.getenv("PIANOTUBE_NO_BROWSER"), server_port=7860, allowed_paths=[str(config.OUTPUT_DIR)],
                           theme=gr.themes.Soft(primary_hue="indigo"), css=CSS)


if __name__ == "__main__":
    main()
