"""Truyện audio: truyện (write-story) → giọng đọc CIT Voice Studio → video nền → video ngang + video dọc
→ (tuỳ chọn) đăng YouTube. Các module source/voice/render/watermark được import khi cần."""
import datetime as dt
import json
import random
import re
import shutil
from pathlib import Path

from .. import config, library, settings

CACHE = config.OUTPUT_DIR / ".cache" / "story"
GENERIC_TAGS = ["truyện audio", "truyện ngắn", "đoản văn", "nghe truyện", "truyện đêm khuya"]
VOICE_NOTE = "Giọng đọc: AI (CIT Voice Studio)"
WORDS_PER_MIN = 150


# ---------- truyện ----------

def story_root() -> Path | None:
    d = settings.load().get("story_dir")
    return Path(d).expanduser() if d else None


def list_stories() -> list:
    from . import source
    return source.list_stories(story_root())


def load_story(slug_or_path):
    from . import source
    root = story_root()
    if root and not Path(str(slug_or_path)).is_absolute() and (root / str(slug_or_path)).exists():
        return source.load(root / str(slug_or_path))
    return source.load(slug_or_path)


def minutes(story) -> float:
    return story.words / WORDS_PER_MIN


def story_status() -> dict:
    """Tình trạng cho giao diện: CIT Voice Studio, danh sách giọng, số truyện."""
    st = {"cit_ok": False, "cit": "", "voices": [], "stories": 0, "error": ""}
    try:
        from . import voice
        try:
            st["cit"] = voice.health()
            st["cit_ok"] = True
            st["voices"] = [v.get("name") or v.get("id") for v in voice.voices()]
        except Exception as e:
            st["cit"] = f"{e}"
    except Exception as e:
        st["cit"] = f"Chưa có module giọng đọc ({e})"
    try:
        st["stories"] = len(list_stories())
    except Exception as e:
        st["error"] = f"Không đọc được thư mục truyện: {e}"
    return st


# ---------- nhạc nền ----------

def ambient_tracks() -> list:
    return sorted(config.OUTPUT_DIR.glob("*/tracks/*.wav"))


def auto_music(seconds: float, out_dir: Path, log=print) -> Path | None:
    """Nối vài bài piano nhẹ (từ các video ambient đã làm) cho đủ dài; render tự lặp nếu vẫn thiếu."""
    from .. import audio
    out = out_dir / "music.m4a"
    if out.exists() and audio.duration(out) >= min(seconds, 1800):
        return out
    tracks = ambient_tracks()
    if not tracks:
        log("  ⚠️ Chưa có bài piano nào trong output/*/tracks, bỏ qua nhạc nền.")
        return None
    random.shuffle(tracks)
    picked, total = [], 0.0
    for t in tracks[:12]:                       # tối đa 12 bài, thiếu thì render tự lặp
        picked.append(t)
        total += audio.duration(t) - 4
        if total >= seconds:
            break
    log(f"  🎚 Nhạc nền: nối {len(picked)} bài piano nhẹ…")
    out_dir.mkdir(parents=True, exist_ok=True)
    audio.concat_crossfade(picked, out, 4.0)
    return out


# ---------- metadata ----------

def _hashtags(story) -> list:
    tags = [h if h.startswith("#") else f"#{h}" for h in (story.hashtags or [])]
    return [re.sub(r"\s+", "", h) for h in tags if h.strip("#")]


def _tags(story) -> list:
    tags, seen = [], set()
    for t in [h.lstrip("#") for h in _hashtags(story)] + GENERIC_TAGS:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    return tags


def _cut(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _write_meta(folder: Path, meta: dict):
    (folder / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    (folder / "title.txt").write_text(meta["title"])
    (folder / "description.txt").write_text(meta["description"])
    (folder / "tags.txt").write_text(", ".join(meta["tags"]))


def _base_meta(story, voice_name: str, kind: str) -> dict:
    return {"kind": kind, "slug": story.slug, "voice": voice_name, "category": "24", "language": "vi",
            "ai_image": False, "made_for_kids": False}


def landscape_meta(story, voice_name: str, duration: float, shorts: list) -> dict:
    title = _cut((story.yt_titles or [None])[0] or story.title, 100)
    desc = "\n\n".join(p for p in [story.description.strip(), " ".join(_hashtags(story)), VOICE_NOTE] if p)
    return {**_base_meta(story, voice_name, "story"), "title": title, "description": desc[:5000],
            "tags": _tags(story), "duration": round(duration, 1), "shorts": shorts}


def short_meta(story, voice_name: str, i: int, n: int) -> dict:
    suffix = (f" | Phần {i}/{n}" if n > 1 else "") + " #shorts"
    title = _cut(story.title, 100 - len(suffix)) + suffix
    hooks = story.hooks or [story.title]
    tags = _hashtags(story)
    tags = tags + ["#shorts"] if "#shorts" not in [t.lower() for t in tags] else tags
    desc = "\n\n".join([hooks[(i - 1) % len(hooks)].strip(), "🎧 Nghe trọn bộ trên kênh.", " ".join(tags),
                        VOICE_NOTE])
    return {**_base_meta(story, voice_name, "story_short"), "title": title, "description": desc[:5000],
            "tags": _tags(story) + ["shorts"], "part": i, "parts_total": n}


# ---------- làm video ----------

def _wm(watermark):
    if watermark is None or not isinstance(watermark, dict):
        return watermark
    from .watermark import WatermarkConfig
    w = dict(watermark)
    if w.get("logo"):
        w["logo"] = Path(w["logo"])
    return WatermarkConfig(**{k: v for k, v in w.items() if k in WatermarkConfig.__dataclass_fields__})


def make(slug: str, clips_dir, voice: str = None, speed: float = 1.0,
         landscape=True, vertical=True, vertical_mode="parts", vertical_seconds=90,
         subtitles_landscape=False, subtitles_vertical=True, music="auto", music_volume=0.12,
         watermark=None, upload=False, upload_mode=None, progress=None, log=print) -> Path:
    """Làm video truyện audio. music: "auto" (piano nhẹ tự chọn) | None | đường dẫn file nhạc."""
    from . import render, voice as cit
    if not (landscape or vertical):
        raise ValueError("Chọn ít nhất một định dạng: ngang hoặc dọc.")
    story = load_story(slug)
    clips_dir = Path(clips_dir).expanduser()
    clips = render.collect_clips(clips_dir)
    # Thumbnail lấy từ clip gốc (video đã dựng có phụ đề + watermark).
    thumb_src = random.Random(story.slug).choice(clips) if clips else None
    if not clips:
        raise FileNotFoundError(f"Không có video nền nào trong {clips_dir}")
    voice = voice or cit.DEFAULT_VOICE
    folder = config.OUTPUT_DIR / f"{dt.datetime.now():%Y-%m-%d_%H%M}_story_{story.slug[:50]}"
    folder.mkdir(parents=True, exist_ok=True)
    log(f"→ {folder}")
    log(f"📖 {story.title} · {story.words} từ · ~{minutes(story):.0f} phút")

    # Giọng đọc: cache theo truyện + giọng + tốc độ, đổi watermark/định dạng không phải đọc lại.
    cache = CACHE / story.slug / f"{library.slug(voice) or 'voice'}_{speed:g}"
    cache.mkdir(parents=True, exist_ok=True)
    log(f"🎙 Đọc truyện bằng giọng «{voice}» (tốc độ {speed:g})…")
    narr = cit.narrate(source_text(story), cache, voice=voice, speed=speed, progress=progress, log=log)
    log(f"🎙 Giọng đọc xong: {narr.duration / 60:.1f} phút")

    if music == "auto":
        music_path = auto_music(narr.duration, CACHE / story.slug, log)
    elif music:
        music_path = Path(music).expanduser()
        if not music_path.exists():
            raise FileNotFoundError(f"Không thấy file nhạc: {music}")
    else:
        music_path = None

    hook = (story.hooks or [""])[0]
    opts = render.RenderOptions(
        clips_dir=clips_dir, landscape=landscape, vertical=vertical, vertical_mode=vertical_mode,
        vertical_seconds=vertical_seconds, subtitles_landscape=subtitles_landscape,
        subtitles_vertical=subtitles_vertical, music=music_path, music_volume=music_volume,
        watermark=_wm(watermark), hook_text=hook)
    if narr.srt and Path(narr.srt).exists():
        shutil.copy(narr.srt, folder / "story.srt")     # phụ đề để tải lên YouTube nếu muốn

    shorts = []
    if vertical:
        log("🎞 Dựng video dọc 9:16…")
        sdir = folder / "shorts"
        parts = render.render_vertical(narr, opts, sdir, progress=progress, log=log)
        n = len(parts)
        for i, p in enumerate(parts, 1):
            sub = sdir / f"{folder.name}_phan{i:02d}"   # tên riêng để lịch sử đăng không lẫn giữa các truyện
            sub.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), sub / "video.mp4")
            try:
                render.thumbnail(thumb_src or sub / "video.mp4", hook or story.title, sub / "thumbnail.jpg", size=(1080, 1920))
            except Exception as e:
                log(f"  ⚠️ Chưa tạo được thumbnail phần {i}: {e}")
            meta = short_meta(story, voice, i, n)
            _write_meta(sub, meta)
            shorts.append({"folder": f"shorts/{sub.name}", "title": meta["title"]})
        log(f"🎞 Xong {n} video dọc.")

    meta = landscape_meta(story, voice, narr.duration, shorts)
    if landscape:
        log("🎬 Dựng video ngang 16:9…")
        render.render_landscape(narr, opts, folder / "video.mp4", progress=progress, log=log)
        log("📝 Tạo thumbnail…")
        render.thumbnail(thumb_src or folder / "video.mp4", hook or story.title, folder / "thumbnail.jpg")
    _write_meta(folder, meta)

    print(f"✓ Xong: {folder}", flush=True)
    if upload:
        upload_all(folder, upload_mode, log)
    return folder


def source_text(story) -> str:
    from . import source
    return source.story_text(story)


def upload_all(folder: Path, mode: str | None = None, log=print) -> list:
    """Đăng video ngang rồi từng video dọc; chế độ slot thì mỗi video một khung giờ khác nhau."""
    from .. import publish
    targets = ([folder] if (folder / "video.mp4").exists() else []) + \
        sorted(p.parent for p in (folder / "shorts").glob("*/video.mp4"))
    taken, recs = [], []
    for t in targets:
        try:
            rec = publish.publish(t, mode=mode, taken=taken, log=log)
            if rec.get("publish_at"):
                taken.append(rec["publish_at"])
            recs.append(rec)
            log(f"📤 Đã đăng: {rec['url']}" + (f" (công khai lúc {rec['publish_local']})"
                                                if rec.get("publish_local") else f" ({rec['privacy']})"))
        except Exception as e:
            log(f"✗ Đăng YouTube lỗi ({t.name}): {e}")
    return recs
