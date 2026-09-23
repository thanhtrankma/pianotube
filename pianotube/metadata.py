"""Ghép mô tả YouTube: giới thiệu + chương + ghi công + hashtag."""
import json
from pathlib import Path

from . import config


def ts(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def label(p: dict) -> str:
    return p["title"] if p.get("original") else f"{p['composer']} – {p['title']}"


def credits(pieces: list) -> str:
    if all(p.get("original") for p in pieces):
        return "All music is original.\n" + config.SOUNDFONT_CREDIT
    lines = ["Music credits:"]
    seen = set()
    for p in pieces:
        if p.get("original") or p["id"] in seen:
            continue
        seen.add(p["id"])
        lines.append(f"• {p['composer']} – {p['title']} | score/MIDI: {p['source']} ({p['license']})")
    lines.append(config.SOUNDFONT_CREDIT)
    return "\n".join(lines)


def join_description(intro: str, tracklist: str, credits_text: str, hashtags: list) -> str:
    parts = [intro.strip(), tracklist, credits_text, " ".join(hashtags)]
    return "\n\n".join(p for p in parts if p)[:5000]


def build(ai: dict, pieces: list, chapters: list | None) -> dict:
    tracklist = ""
    if chapters and len(chapters) >= 3:
        # YouTube cần chương đầu ở 00:00 và ít nhất 3 chương.
        tracklist = "Tracklist:\n" + "\n".join(f"{ts(t)} {label(p)}" for t, p in chapters)
    credits_text = credits(pieces)
    return {
        "title": ai["title"][:100],
        "description": join_description(ai["intro"], tracklist, credits_text, ai["hashtags"]),
        "tags": ai["tags"],
        "thumbnail_text": ai["thumbnail_text"],
        "category": "Music",
        "made_for_kids": False,
        # Lưu từng phần để Gemini viết lại tiêu đề/giới thiệu mà giữ nguyên tracklist + ghi công.
        "parts": {"intro": ai["intro"].strip(), "tracklist": tracklist,
                  "credits": credits_text, "hashtags": ai["hashtags"]},
    }


def split_description(desc: str) -> dict:
    """Tách mô tả cũ (chưa có "parts") thành các phần."""
    blocks = desc.split("\n\n")
    parts = {"intro": [], "tracklist": "", "credits": "", "hashtags": []}
    for b in blocks:
        if b.startswith("Tracklist:"):
            parts["tracklist"] = b
        elif b.startswith(("Music credits:", "All music is original")):
            parts["credits"] = b
        elif b.strip().startswith("#") and all(w.startswith("#") for w in b.split()):
            parts["hashtags"] = b.split()
        else:
            parts["intro"].append(b)
    parts["intro"] = "\n\n".join(parts["intro"])
    return parts


def write(meta: dict, folder: Path):
    (folder / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    (folder / "description.txt").write_text(meta["description"])
    (folder / "title.txt").write_text(meta["title"])
    (folder / "tags.txt").write_text(", ".join(meta["tags"]))
