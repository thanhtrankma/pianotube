"""Viết lại tiêu đề bằng Gemini và đăng video theo cài đặt lịch."""
import json
from pathlib import Path

from . import config, gemini, metadata, settings, youtube


def folder_path(name_or_path) -> Path:
    p = Path(name_or_path)
    return p if p.is_absolute() else config.OUTPUT_DIR / p.name


def retitle(folder, note: str | None = None, log=print) -> dict:
    """Gemini viết lại tiêu đề, đoạn giới thiệu, tag, hashtag. Giữ nguyên tracklist và ghi công."""
    folder = folder_path(folder)
    meta = json.loads((folder / "metadata.json").read_text())
    meta["title"] = (folder / "title.txt").read_text().strip() if (folder / "title.txt").exists() else meta["title"]
    if not gemini.available():
        raise RuntimeError("Chưa có Gemini API key (tab Cài đặt).")
    note = settings.load()["title_note"] if note is None else note
    ai = gemini.rewrite(meta, ambient=meta.get("kind") == "ambient", note=note)
    if not ai:
        raise RuntimeError("Gemini không trả về kết quả, thử lại sau.")
    parts = meta.get("parts") or metadata.split_description(meta["description"])
    parts.update(intro=ai["intro"].strip(), hashtags=ai["hashtags"])
    meta.update(title=ai["title"][:100], tags=ai["tags"], parts=parts,
                description=metadata.join_description(parts["intro"], parts["tracklist"],
                                                      parts["credits"], parts["hashtags"]))
    metadata.write(meta, folder)
    log(f"✍️ Tiêu đề mới: {meta['title']}")
    return meta


def publish(folder, mode: str | None = None, at: str | None = None, taken: list | None = None,
            progress=None, log=print) -> dict:
    """mode: slot (khung giờ trống tiếp theo) | at (giờ cụ thể) | private | unlisted | public."""
    folder = folder_path(folder)
    s = settings.load()
    mode = mode or s["mode"]
    meta = json.loads((folder / "metadata.json").read_text())
    publish_at, privacy = None, "private"
    if mode == "slot":
        publish_at = youtube.next_free_slot(s["slots"], int(s["lead_minutes"]), taken)
    elif mode == "at":
        publish_at = youtube.parse_local(at)
    else:
        privacy = mode
    if publish_at:
        log(f"🗓 Hẹn công khai lúc {publish_at:%Y-%m-%d %H:%M}")
    synthetic = bool(s["synthetic"]) and meta.get("ai_image", True)
    return youtube.upload(folder, privacy=privacy, publish_at=publish_at, synthetic=synthetic,
                          account_id=s["account"] or None, progress=progress, log=log)
