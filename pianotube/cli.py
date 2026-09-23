"""Dòng lệnh:
  pianotube ambient --mood "when the world feels too loud" --minutes 50
  pianotube relax --theme "rainy night study" --minutes 90
  pianotube falling <id|file.mid>
  pianotube library add FILE.mid --title ... --composer ... --license ... --source URL
  pianotube library list
"""
import argparse
import datetime as dt
import random
import subprocess
from pathlib import Path

from PIL import Image

from . import (audio, compose, config, gemini, imaging, library, metadata, video_falling,
               video_relax)

CACHE = config.OUTPUT_DIR / ".cache"


def out_folder(name: str) -> Path:
    folder = config.OUTPUT_DIR / f"{dt.datetime.now():%Y-%m-%d_%H%M}_{library.slug(name)[:50]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def piece_audio(item: dict, soft: bool = False) -> Path:
    """MIDI → WAV đã chuẩn hoá, có cache để lần sau không phải render lại.
    soft=True: đánh khẽ, chậm hơn, vang hơn, ấm hơn (hợp video thư giãn)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    final = CACHE / f"{item['id']}{'.soft' if soft else ''}.wav"
    if not final.exists():
        print(f"  ♪ render {item['composer']} – {item['title']}")
        raw = CACHE / f"{item['id']}.raw.wav"
        if soft:
            mid = CACHE / f"{item['id']}.soft.mid"
            audio.soften_midi(library.path(item), mid)
            audio.render_midi(mid, raw, soft=True)
            audio.warm(raw, raw.with_suffix(".warm.wav"))
            raw.with_suffix(".warm.wav").replace(raw)
            mid.unlink()
            audio.normalize(raw, final, target=AMBIENT_LUFS)
        else:
            audio.render_midi(library.path(item), raw)
            audio.normalize(raw, final)
        raw.unlink()
    return final


AMBIENT_LUFS = -16.0        # nhạc êm để nhỏ hơn chuẩn -14 một chút, nghe dịu tai hơn

SCENES = [
    "sea cliffs above a grey ocean, green grass at the edge",
    "an empty beach at dusk with gentle waves and wet sand",
    "a misty pine forest path in the early morning",
    "a still mountain lake under low clouds",
    "a snowy field with a lone bare tree under a pale sky",
    "a quiet city street at night after rain, reflections on the road",
    "a small wooden pier on a calm lake in fog",
    "rolling hills with tall grass in soft wind at golden hour",
    "a rocky coastline with scattered boulders and calm blue-grey sea",
    "a rooftop overlooking a sleeping city at blue hour",
]


def ambient_image_prompt(scene: str) -> str:
    return (f"Cinematic film photograph: {scene}. One small solitary person seen from behind, far away, "
            "sitting or standing quietly. Peaceful, melancholic, safe. Overcast soft natural light, "
            "muted desaturated colors, subtle film grain, wide shot with lots of negative space, 16:9. "
            "No text, no logos, no watermark, no visible face.")


FALLBACK_TRACK_WORDS = (["quiet", "slow", "distant", "soft", "empty", "late", "gentle", "fading", "still", "grey"],
                        ["harbor", "letters", "rain", "window", "morning", "tide", "shore", "hallway", "snow", "light"])


def fallback_meta(kind: str, pieces: list, theme: str | None) -> dict:
    p = pieces[0]
    if kind == "falling":
        title = f"{p['composer']} – {p['title']} | Piano Tutorial (Falling Notes)"
        intro = (f"{p['title']} by {p['composer']}, visualized note by note on a virtual "
                 "piano keyboard. Right hand in blue, left hand in orange.")
        thumb = p["title"]
    else:
        title = f"{theme.title()} – Relaxing Classical Piano"
        intro = f"Calm classical piano for {theme}. Public-domain masterpieces, gently cross-faded."
        thumb = theme.title()
    return {"title": title, "intro": intro, "thumbnail_text": thumb,
            "tags": ["piano", "classical piano", "relaxing piano", p["composer"].lower()],
            "hashtags": ["#piano", "#classicalmusic", "#relaxingmusic"]}


def cmd_library(args):
    if args.action == "add":
        e = library.add(Path(args.file), args.title, args.composer, args.license, args.source)
        print(f"Đã thêm: {e['id']}")
    else:
        for i in library.all_pieces():
            print(f"{i['id']:<45} used={i['used']:<3} {i['composer']} – {i['title']} [{i['license']}]")


def cmd_falling(args):
    item = library.find(args.piece)
    folder = out_folder(f"{item['composer']} {item['title']}")
    print(f"→ {folder}")
    wav = piece_audio(item)

    bg_img = None
    if not args.no_ai_image:
        print("  🎨 Gemini tạo ảnh nền…")
        bg_img = gemini.image(
            f"Atmospheric, painterly background evoking '{item['title']}' by {item['composer']}. "
            "Soft light, elegant, no text, no people, no piano keys, cinematic 16:9.")
    background = imaging.falling_background(bg_img)

    video = folder / "video.mp4"
    video_falling.render(library.path(item), wav, video, background, item["title"], item["composer"])

    print("  📝 Viết metadata…")
    ai = gemini.metadata(
        f"Video type: falling-notes piano visualization (Synthesia style), right hand blue, left hand orange.\n"
        f"Piece: {item['title']}\nComposer: {item['composer']}") or fallback_meta("falling", [item], None)
    meta = metadata.build(ai, [item], None)
    metadata.write(meta, folder)

    frame = folder / "frame.png"
    dur = audio.duration(video)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{dur * 0.4:.1f}", "-i", str(video),
                    "-frames:v", "1", str(frame)], check=True)
    imaging.thumbnail(Image.open(frame), ai["thumbnail_text"], item["composer"], folder / "thumbnail.jpg",
                       top=True)
    frame.unlink()
    library.mark_used([item["id"]])
    print(f"✓ Xong: {folder}")


def cmd_relax(args):
    target = args.minutes * 60
    folder = out_folder(f"relax {args.theme}")
    print(f"→ {folder}")

    pool = library.ordered_pool(args.composer)
    pieces, wavs, total, k = [], [], 0.0, 0
    while total < target:
        item = pool[k % len(pool)]
        wav = piece_audio(item, soft=not args.bright)
        pieces.append(item)
        wavs.append(wav)
        total += audio.duration(wav) - (args.xfade if len(wavs) > 1 else 0)
        k += 1
    if k > len(pool):
        print(f"  ! Thư viện chỉ có {len(pool)} bài, một số bài được lặp lại.")

    print(f"  🎚 Nối {len(wavs)} bài…")
    mix = folder / "audio.m4a"
    starts = audio.concat_crossfade(wavs, mix, args.xfade)
    seconds = audio.duration(mix)

    bg_img = None
    if not args.no_ai_image:
        print("  🎨 Gemini tạo ảnh nền…")
        bg_img = gemini.image(
            f"Cozy, calm scene for '{args.theme}'. Painterly lo-fi illustration, warm soft light, "
            "gentle atmosphere, no text, no logos, no people's faces, cinematic 16:9.")
    bg_img = bg_img or imaging.gradient_background()
    bg_img.save(folder / "background.png")

    print("  🎞 Dựng đoạn hình lặp…")
    loop = folder / "loop.mp4"
    video_relax.make_loop(bg_img, loop, effect=args.effect)
    print("  🎬 Ghép video…")
    video_relax.assemble(loop, mix, folder / "video.mp4", seconds, args.visualizer)
    loop.unlink()
    mix.unlink()

    print("  📝 Viết metadata…")
    names = "\n".join(f"- {p['composer']}: {p['title']}" for p in dict((p["id"], p) for p in pieces).values())
    ai = gemini.metadata(
        f"Video type: long relaxing classical piano mix, {seconds / 3600:.1f} hours.\n"
        f"Theme / use-case: {args.theme}\nPieces:\n{names}") or fallback_meta("relax", pieces, args.theme)
    meta = metadata.build(ai, pieces, list(zip(starts, pieces)))
    metadata.write(meta, folder)
    imaging.thumbnail(bg_img, ai["thumbnail_text"], "Relaxing Classical Piano", folder / "thumbnail.jpg")
    library.mark_used([p["id"] for p in pieces])
    print(f"✓ Xong: {folder}")


def cmd_ambient(args):
    target = args.minutes * 60
    folder = out_folder(args.mood)
    tracks_dir = folder / "tracks"          # giữ MIDI + WAV từng bài (có thể đăng Spotify sau)
    tracks_dir.mkdir()
    print(f"→ {folder}")
    base_seed = args.seed if args.seed is not None else random.randrange(10 ** 9)

    wavs, total, k = [], 0.0, 0
    while total < target:
        piece = compose.compose(base_seed + k, bars_hint=52 if k % 2 else 44)
        mid = tracks_dir / f"{k + 1:02d}.mid"
        piece.midi.write(str(mid))
        print(f"  ♪ bài {k + 1}: {piece.key_name}, {piece.bpm:.0f} bpm")
        raw = tracks_dir / f"{k + 1:02d}.raw.wav"
        audio.render_midi(mid, raw, soft=True)
        pad_wav = None
        if not args.no_pad:
            pad_wav = audio.pad(piece.chords, audio.duration(raw), tracks_dir / "pad.wav")
        warm = audio.warm(raw, tracks_dir / f"{k + 1:02d}.warm.wav", pad_wav)
        final = audio.normalize(warm, tracks_dir / f"{k + 1:02d}.wav", target=AMBIENT_LUFS)
        for tmp in (raw, warm, pad_wav):
            if tmp:
                tmp.unlink()
        wavs.append(final)
        total += audio.duration(final) - (args.xfade if len(wavs) > 1 else 0)
        k += 1

    print(f"  🎚 Nối {len(wavs)} bài…")
    mix = folder / "audio.m4a"
    starts = audio.concat_crossfade(wavs, mix, args.xfade)
    seconds = audio.duration(mix)

    scene = args.scene or random.choice(SCENES)
    bg_img = None
    if not args.no_ai_image:
        print(f"  🎨 Gemini tạo ảnh: {scene}")
        bg_img = gemini.image(ambient_image_prompt(scene))
    bg_img = bg_img or imaging.gradient_background((18, 22, 28), (46, 52, 58))
    bg_img.save(folder / "background.png")

    print("  🎞 Dựng đoạn hình lặp…")
    loop = folder / "loop.mp4"
    video_relax.make_loop(bg_img, loop, effect=args.effect)
    print("  🎬 Ghép video…")
    video_relax.assemble(loop, mix, folder / "video.mp4", seconds, args.visualizer)
    loop.unlink()
    mix.unlink()

    print("  📝 Viết metadata…")
    ai = gemini.metadata(
        f"Feeling / theme of this video: {args.mood}\n"
        f"Length: about {seconds / 60:.0f} minutes, {len(wavs)} original soft piano tracks.\n"
        f"Visual: {scene}.\nNumber of track_names needed: {len(wavs)}", ambient=True)
    if not ai:
        rng = random.Random(base_seed)
        adj, noun = FALLBACK_TRACK_WORDS
        ai = {"title": f"{args.mood.lower()} | piano playlist",
              "intro": "A quiet place to rest for a while. Press play, breathe, and let the day soften.\n\n"
                       "Tell me in the comments where this music found you.",
              "tags": ["piano playlist", "sad piano", "emotional piano", "calm piano", "healing music"],
              "hashtags": ["#piano", "#pianoplaylist", "#calmmusic", "#emotionalmusic", "#ambient"],
              "thumbnail_text": args.mood.lower(),
              "track_names": [f"{rng.choice(adj)} {rng.choice(noun)}" for _ in wavs]}
    names = (ai.get("track_names") or [])[:len(wavs)]
    names += [f"track {i + 1}" for i in range(len(names), len(wavs))]
    pieces = [{"id": f"t{i}", "title": f"{i + 1:02d}. {n}", "composer": "", "original": True}
              for i, n in enumerate(names)]
    meta = metadata.build(ai, pieces, list(zip(starts, pieces)))
    meta["seed"] = base_seed
    meta["scene"] = scene
    metadata.write(meta, folder)
    if args.thumb_text:
        imaging.thumbnail(bg_img, ai["thumbnail_text"], "piano playlist", folder / "thumbnail.jpg")
    else:
        imaging.fit_cover(bg_img, (1280, 720)).save(folder / "thumbnail.jpg", quality=92)
    print(f"✓ Xong: {folder}")


def main():
    ap = argparse.ArgumentParser(prog="pianotube")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lib = sub.add_parser("library", help="quản lý thư viện MIDI")
    lib.add_argument("action", choices=["add", "list"])
    lib.add_argument("file", nargs="?")
    lib.add_argument("--title")
    lib.add_argument("--composer")
    lib.add_argument("--license", default="Public Domain")
    lib.add_argument("--source", default="")
    lib.set_defaults(func=cmd_library)

    fall = sub.add_parser("falling", help="video phím đàn rơi cho 1 bài")
    fall.add_argument("piece", help="id trong thư viện hoặc tên file .mid")
    fall.add_argument("--no-ai-image", action="store_true", help="dùng nền gradient, không gọi Gemini")
    fall.set_defaults(func=cmd_falling)

    rel = sub.add_parser("relax", help="video nhạc thư giãn dài")
    rel.add_argument("--theme", default="rainy night study")
    rel.add_argument("--minutes", type=float, default=60)
    rel.add_argument("--composer", help="chỉ chọn bài của nhà soạn nhạc này")
    rel.add_argument("--xfade", type=float, default=3.0)
    rel.add_argument("--visualizer", action="store_true", help="thêm sóng nhạc (chậm hơn nhiều)")
    rel.add_argument("--effect", choices=["dust", "grain", "none"], default="dust")
    rel.add_argument("--bright", action="store_true", help="giữ tiếng đàn gốc, không làm mềm")
    rel.add_argument("--no-ai-image", action="store_true")
    rel.set_defaults(func=cmd_relax)

    amb = sub.add_parser("ambient", help="playlist piano ambient nhẹ nhàng, nhạc tự sáng tác")
    amb.add_argument("--mood", default="when the world feels too loud",
                     help="cảm xúc/chủ đề của video (tiếng Anh)")
    amb.add_argument("--minutes", type=float, default=50)
    amb.add_argument("--scene", help="mô tả cảnh cho ảnh nền (mặc định chọn ngẫu nhiên)")
    amb.add_argument("--seed", type=int, help="cùng seed = cùng giai điệu")
    amb.add_argument("--xfade", type=float, default=4.0)
    amb.add_argument("--effect", choices=["grain", "dust", "none"], default="grain")
    amb.add_argument("--visualizer", action="store_true")
    amb.add_argument("--no-pad", action="store_true", help="bỏ lớp pad nền")
    amb.add_argument("--thumb-text", action="store_true", help="thêm chữ lên thumbnail")
    amb.add_argument("--no-ai-image", action="store_true")
    amb.set_defaults(func=cmd_ambient)

    args = ap.parse_args()
    if args.cmd == "library" and args.action == "add" and not (args.file and args.title and args.composer):
        ap.error("library add cần FILE, --title và --composer")
    args.func(args)
