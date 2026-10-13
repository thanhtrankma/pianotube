"""Truyện audio: dựng video từ giọng đọc + b-roll (thư mục clip người dùng chọn).

- Video ngang 16:9 (1920x1080) cho bản dài.
- Video dọc 9:16 (1080x1920) cho Reels/Shorts, cắt theo ranh giới câu.

Bản ffmpeg này không có filter subtitles/ass/drawtext nên phụ đề được vẽ bằng Pillow
thành ảnh PNG trong suốt, ghép thành 1 luồng ảnh (concat demuxer, mỗi ảnh có duration)
rồi overlay đúng 1 lần.
"""
from __future__ import annotations

import json
import math
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .. import config

if TYPE_CHECKING:  # chỉ để gợi ý kiểu, tránh import vòng / file chưa có
    from .voice import Narration
    from .watermark import WatermarkConfig

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

LANDSCAPE = (1920, 1080)
VERTICAL = (1080, 1920)
FPS = config.FPS
XFADE_FRAMES = max(1, round(0.5 * FPS))     # chuyển cảnh mờ ~0,5 s giữa các đoạn
BATCH = 10                                 # số đoạn b-roll mỗi lần gọi ffmpeg
WORKERS = 3                                # số ffmpeg dựng b-roll song song
VOICE_LUFS = -16.0
HOOK_SECONDS = 3.0

FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_FALLBACK = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

Progress = Callable[[float, str], None] | None


@dataclass
class RenderOptions:
    clips_dir: Path
    landscape: bool = True
    vertical: bool = True
    vertical_mode: str = "parts"      # "parts" | "teaser" | "full"
    vertical_seconds: int = 90
    subtitles_landscape: bool = False
    subtitles_vertical: bool = True
    music: Path | None = None
    music_volume: float = 0.12
    watermark: "WatermarkConfig | None" = None
    shuffle: bool = True
    seed: int | None = None
    clip_min: float = 4.0
    clip_max: float = 9.0
    hook_text: str = ""


# ───────────────────────── tiện ích ffmpeg ─────────────────────────

_encoder_lock = threading.Lock()
_encoder = config.VIDEO_ENCODER


STALL_SECONDS = 180      # ffmpeg không tiến thêm khung nào trong chừng này giây thì coi là treo


def _ffmpeg(args: list, seconds: float = 0, progress: Progress = None,
            lo: float = 0.0, hi: float = 1.0, msg: str = "") -> None:
    """Chạy ffmpeg, đọc -progress pipe:1 để báo tiến độ và phát hiện treo
    (ffmpeg 9 thỉnh thoảng kẹt khi một input kết thúc sớm trong filter_complex)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats", *[str(a) for a in args]]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        state = {"t": -1.0, "at": time.monotonic(), "stalled": False}

        def watchdog():
            while proc.poll() is None:
                time.sleep(2)
                if time.monotonic() - state["at"] > STALL_SECONDS and proc.poll() is None:
                    state["stalled"] = True
                    proc.kill()

        threading.Thread(target=watchdog, daemon=True).start()
        last = -1.0
        for line in proc.stdout:
            if not line.startswith(("out_time_us=", "out_time_ms=")):
                continue
            try:
                t = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            if t != state["t"]:
                state["t"], state["at"] = t, time.monotonic()
            if progress and seconds > 0:
                f = min(1.0, max(0.0, t / seconds))
                if f - last >= 0.01:
                    last = f
                    progress(lo + (hi - lo) * f, msg)
        proc.wait()
        if state["stalled"]:
            raise RuntimeError(f"ffmpeg bị treo (không tiến triển {STALL_SECONDS} s) nên đã dừng.")
        if proc.returncode:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip()[-1500:]
            raise RuntimeError(f"ffmpeg lỗi ({proc.returncode}): {tail}")


def _venc_for(enc: str) -> list[str]:
    if "videotoolbox" in enc:
        return ["-c:v", enc, "-b:v", config.VIDEO_BITRATE, "-pix_fmt", "yuv420p", "-g", str(FPS * 2)]
    return ["-c:v", enc, "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-g", str(FPS * 2)]


def _encode(build: Callable[[list[str]], list], seconds: float = 0, progress: Progress = None,
            lo: float = 0.0, hi: float = 1.0, msg: str = "", log=print) -> None:
    """build(venc_args) -> args. Thử VideoToolbox trước; lỗi thì thử lại bằng libx264
    (chỉ chuyển hẳn sang libx264 nếu lần thử lại thành công)."""
    global _encoder
    enc = _encoder
    try:
        _ffmpeg(build(_venc_for(enc)), seconds, progress, lo, hi, msg)
        return
    except RuntimeError as e:
        if "videotoolbox" not in enc or "treo" in str(e):
            raise
        first = e
    log("VideoToolbox lỗi, thử lại bằng libx264…")
    try:
        _ffmpeg(build(_venc_for("libx264")), seconds, progress, lo, hi, msg)
    except RuntimeError:
        raise first
    with _encoder_lock:
        _encoder = "libx264"


def _probe(path: Path) -> tuple[float, bool]:
    """(thời lượng giây, có luồng hình không)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode:
        return 0.0, False
    j = json.loads(r.stdout or "{}")
    has_v = any(s.get("codec_type") == "video" for s in j.get("streams", []))
    try:
        d = float(j.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        d = 0.0
    return d, has_v


def _media_duration(path: Path) -> float:
    return _probe(path)[0]


# ───────────────────────── b-roll ─────────────────────────

def collect_clips(clips_dir: Path) -> list[Path]:
    clips_dir = Path(clips_dir).expanduser()
    if not clips_dir.is_dir():
        raise FileNotFoundError(f"Không tìm thấy thư mục clip: {clips_dir}")
    exts = VIDEO_EXTS | IMAGE_EXTS
    found = sorted(
        p for p in clips_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in exts
        and not any(part.startswith(".") for part in p.relative_to(clips_dir).parts)
    )
    if not found:
        raise FileNotFoundError(
            f"Thư mục {clips_dir} không có video ({', '.join(sorted(VIDEO_EXTS))}) "
            f"hay ảnh ({', '.join(sorted(IMAGE_EXTS))}) nào để làm b-roll.")
    return found


@dataclass
class _Seg:
    path: Path
    image: bool
    start: float          # giây trong clip
    frames: int           # số khung hình của đoạn (tính cả phần chồng crossfade)
    src_dur: float        # thời lượng clip (0 với ảnh)
    zoom_in: bool = True


_probe_cache: dict[tuple[str, float], tuple[float, bool]] = {}


def _probe_cached(p: Path) -> tuple[float, bool]:
    key = (str(p), p.stat().st_mtime)
    if key not in _probe_cache:
        _probe_cache[key] = _probe(p)
    return _probe_cache[key]


def _plan(clips: list[Path], total_frames: int, opts: RenderOptions, log=print,
          rng: random.Random | None = None) -> list[list[_Seg]]:
    """Chia b-roll thành các lô đoạn. Mỗi lô dựng bằng 1 lệnh ffmpeg (xfade nội bộ),
    các lô nối cứng bằng concat demuxer. Đảm bảo tổng số khung hình >= total_frames."""
    rng = rng or random.Random(opts.seed)
    info: dict[Path, tuple[float, bool]] = {}
    usable: list[Path] = []
    min_frames = 2 * XFADE_FRAMES + FPS // 2
    for c in clips:
        if c.suffix.lower() in IMAGE_EXTS:
            info[c] = (0.0, True)
            usable.append(c)
            continue
        d, has_v = _probe_cached(c)
        if not has_v or d <= 0.2:
            log(f"Bỏ qua clip lỗi/không có hình: {c.name}")
            continue
        info[c] = (d, False)
        usable.append(c)
    if not usable:
        raise FileNotFoundError("Không có clip nào dùng được làm b-roll.")

    lo_f = max(min_frames, round(opts.clip_min * FPS))
    hi_f = max(lo_f, round(opts.clip_max * FPS))
    cursor: dict[Path, float] = {}
    order: list[Path] = []
    last: Path | None = None

    def next_clip() -> Path:
        nonlocal order, last
        if not order:
            order = list(usable)
            if opts.shuffle:
                rng.shuffle(order)
                if len(order) > 1 and order[0] == last:   # tránh lặp lại liền nhau
                    order.append(order.pop(0))
        last = order.pop(0)
        return last

    batches: list[list[_Seg]] = []
    cur: list[_Seg] = []
    covered = 0
    zoom_flip = False
    while covered < total_frames:
        c = next_clip()
        d, is_img = info[c]
        frames = rng.randint(lo_f, hi_f)
        start = 0.0
        if not is_img:
            avail = int(d * FPS) - 1
            if avail >= min_frames:
                frames = min(frames, avail)
            seg_s = frames / FPS
            if opts.shuffle:
                start = rng.uniform(0, max(0.0, d - seg_s - 0.05))
            else:
                start = cursor.get(c, 0.0)
                if start + seg_s > d - 0.05:
                    start = 0.0
                cursor[c] = start + seg_s
        zoom_flip = not zoom_flip
        cur.append(_Seg(c, is_img, start, frames, d, zoom_flip))
        covered += frames - (XFADE_FRAMES if len(cur) > 1 else 0)
        if len(cur) >= BATCH:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    return batches


def _batch_frames(batch: list[_Seg]) -> int:
    return sum(s.frames for s in batch) - XFADE_FRAMES * (len(batch) - 1)


def _cover(W: int, H: int) -> str:
    """Phủ kín khung WxH, không viền đen: cắt giữa đúng tỉ lệ rồi scale."""
    return (f"crop='trunc(min(iw,ih*{W}/{H})/2)*2':'trunc(min(ih,iw*{H}/{W})/2)*2',"
            f"scale={W}:{H}:flags=bicubic,setsar=1")


def _seg_input(s: _Seg, W: int, H: int, idx: int) -> tuple[list, str]:
    """Trả về (tham số input, chuỗi filter) cho một đoạn, ra nhãn [s{idx}]."""
    secs = s.frames / FPS
    if s.image:
        # Ken Burns: 1 khung ảnh duy nhất (không giải mã PNG lại mỗi khung), phóng sẵn
        # lớn hơn khung 1,6 lần cho chuyển động mượt, zoompan d=N sinh N khung, zoom ~10%.
        bw, bh = (int(W * 1.6) // 2) * 2, (int(H * 1.6) // 2) * 2
        n = max(1, s.frames - 1)
        z = f"1+0.10*on/{n}" if s.zoom_in else f"1.10-0.10*on/{n}"
        args = ["-i", s.path]
        flt = (f"[{idx}:v]{_cover(bw, bh)},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
               f":d={s.frames}:s={W}x{H}:fps={FPS},")
    else:
        args = []
        if s.src_dur < secs + 0.1:
            args += ["-stream_loop", "-1"]
        if s.start > 0.01:
            args += ["-ss", f"{s.start:.3f}"]
        args += ["-t", f"{secs + 0.5:.3f}", "-i", s.path]
        # Cắt theo tỉ lệ khung TRƯỚC rồi mới thu nhỏ (rẻ hơn nhiều so với phóng to rồi cắt,
        # nhất là khi đưa clip ngang vào khung dọc).
        flt = (f"[{idx}:v]{_cover(W, H)},fps={FPS},")
    flt += (f"tpad=stop_mode=clone:stop_duration=2,trim=end_frame={s.frames},"
            f"setpts=PTS-STARTPTS,format=yuv420p,settb=1/{FPS}[s{idx}]")
    return args, flt


@dataclass
class _Deco:
    """Lớp phủ đốt thẳng vào hình khi dựng b-roll (để chỉ phải mã hoá 1 lần)."""
    subs: "_Subs | None" = None
    hook: Path | None = None
    watermark: "WatermarkConfig | None" = None


def _batch_build(batch: list[_Seg], W: int, H: int, out: Path, frames: int,
                 t_story: float, t_local: float, deco: _Deco | None, work: Path,
                 tag: str) -> Callable[[list[str]], list]:
    """Tạo hàm build(venc) -> tham số ffmpeg cho 1 lô.
    t_story: thời điểm (giây, theo truyện) của khung đầu lô; t_local: tính từ đầu file ra."""
    inputs, parts = [], []
    for i, s in enumerate(batch):
        a, f = _seg_input(s, W, H, i)
        inputs += a
        parts.append(f)
    label = "[s0]"
    acc = batch[0].frames
    for i in range(1, len(batch)):
        off = (acc - XFADE_FRAMES) / FPS
        nxt = f"[x{i}]"
        parts.append(f"{label}[s{i}]xfade=transition=fade:duration={XFADE_FRAMES / FPS:.4f}"
                     f":offset={off:.4f}{nxt}")
        label = nxt
        acc += batch[i].frames - XFADE_FRAMES
    n = len(batch)
    dur = frames / FPS
    if deco and deco.subs:
        lst = deco.subs.write_list(t_story, t_story + dur, work / f"subs_{tag}.txt")
        inputs += ["-f", "concat", "-safe", "0", "-i", lst]
        parts.append(f"[{n}:v]format=rgba[sb];{label}[sb]overlay=0:{deco.subs.st.band_y}"
                     f":format=auto[vs]")
        label, n = "[vs]", n + 1
    if deco and deco.hook and t_local < HOOK_SECONDS:
        # KHÔNG dùng input hữu hạn (-t 3) + eof_action=pass: ffmpeg 9 bị treo ở một số
        # vị trí. Lặp ảnh vô hạn ở 10 fps (rẻ) và tắt overlay sau HOOK_SECONDS.
        inputs += ["-loop", "1", "-framerate", 10, "-i", deco.hook]
        parts.append(f"[{n}:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1,"
                     f"fade=t=out:st={HOOK_SECONDS - 0.5:.2f}:d=0.5:alpha=1[hk];"
                     f"{label}[hk]overlay=0:0:format=auto:shortest=1"
                     f":enable='lt(t,{HOOK_SECONDS:.2f})'[vh]")
        label, n = "[vh]", n + 1
    if deco and deco.watermark is not None:
        from .watermark import ffmpeg_overlay
        extra, frag = ffmpeg_overlay(deco.watermark, W, H, work, n, "[wmin]", "[wmout]")
        extra = [str(a) for a in extra]
        if frag:
            # Dời mốc thời gian về thời gian truyện để watermark động (floating)
            # chạy liền mạch qua các lô; input watermark cũng dời theo (-itsoffset).
            off = f"{t_story:.6f}"
            k = len(extra) - 1 - extra[::-1].index("-i") if "-i" in extra else -1
            if k >= 0:
                extra = extra[:k] + ["-itsoffset", off] + extra[k:]
            parts.append(f"{label}setpts=PTS+{off}/TB[wmin]")
            parts.append(frag)
            parts.append("[wmout]setpts=PTS-STARTPTS[vwm]")
            label = "[vwm]"
        inputs += extra
    graph = ";".join(parts)
    return lambda venc: [*inputs, "-filter_complex", graph, "-map", label,
                         "-frames:v", frames, "-an", *venc, "-r", FPS, out]


def _make_videos(clips: list[Path], spans: list[tuple[float, float]], W: int, H: int,
                 opts: RenderOptions, work: Path, decos: list[_Deco | None],
                 log=print, progress: Progress = None) -> list[Path]:
    """Dựng 1 video câm (đã đốt phụ đề/hook/watermark) cho mỗi khoảng (a, b) của truyện.
    Mọi lô của mọi khoảng chạy chung 1 hàng đợi song song."""
    jobs = []           # (build, out, frames)
    per_span: list[list[Path]] = []
    total_segs = 0
    for k, (a, b) in enumerate(spans):
        total = max(1, round((b - a) * FPS))
        seed = None if opts.seed is None else opts.seed * 1000 + k
        rng = random.Random(seed) if seed is not None else random.Random()
        batches = _plan(clips, total, opts, log, rng)
        files, acc = [], 0
        for i, bt in enumerate(batches):
            fr = min(_batch_frames(bt), total - acc)
            if fr <= 0:
                break
            out = work / f"v{k:02d}_b{i:04d}.mp4"
            build = _batch_build(bt, W, H, out, fr, a + acc / FPS, acc / FPS, decos[k], work,
                                 f"{k:02d}_{i:04d}")
            jobs.append((build, out, fr))
            files.append(out)
            total_segs += len(bt)
            acc += fr
        per_span.append(files)
    log(f"Dựng hình {W}x{H}: {total_segs} đoạn b-roll, {len(jobs)} lô…")

    all_frames = sum(j[2] for j in jobs) or 1
    done = 0
    lock = threading.Lock()

    def run(job):
        nonlocal done
        build, out, fr = job
        _encode(build, log=log)
        with lock:
            done += fr
            if progress:
                progress(done / all_frames, f"Dựng hình {W}x{H}: {done * 100 // all_frames}%")

    with ThreadPoolExecutor(WORKERS) as ex:
        for f in [ex.submit(run, j) for j in jobs]:
            f.result()

    outs = []
    for k, files in enumerate(per_span):
        out = work / f"video_{k:02d}.mp4"
        if len(files) == 1:
            files[0].replace(out)
        else:
            lst = work / f"concat_{k:02d}.txt"
            lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files))
            _ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
            for f in files:
                f.unlink(missing_ok=True)
        outs.append(out)
    return outs


def build_broll(clips: list[Path], duration: float, W: int, H: int, out: Path,
                opts: RenderOptions, log=print, progress: Progress = None) -> Path:
    """Video câm dài đúng `duration`, phủ kín khung WxH (scale+crop), crossfade ~0,5 s."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".broll_", dir=out.parent))
    try:
        v = _make_videos(clips, [(0.0, float(duration))], W, H, opts, work, [None], log, progress)[0]
        _ffmpeg(["-i", v, "-c", "copy", "-movflags", "+faststart", out])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ───────────────────────── chữ / phụ đề ─────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in (FONT_BOLD, FONT_FALLBACK):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", s or "").strip())


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: float) -> list[str]:
    lines, cur = [], ""
    for w in text.split(" "):
        t = f"{cur} {w}".strip()
        if cur and font.getlength(t) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _balance(words: list[str], font, n_lines: int) -> list[str]:
    """Chia cụm từ thành n dòng sao cho dòng dài nhất ngắn nhất (đỡ chữ mồ côi)."""
    if n_lines <= 1 or len(words) < 2:
        return [" ".join(words)]
    best, best_w = None, 1e9
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        w = max(font.getlength(a), font.getlength(b))
        if w < best_w:
            best, best_w = [a, b], w
    return best


def _chunks(cues: list[dict], font, max_w: float, max_lines: int = 2) -> list[tuple[float, float, list[str]]]:
    """Cắt cue dài thành các trang ≤ max_lines dòng, chia thời gian theo số ký tự."""
    out = []
    for c in cues:
        text = _nfc(str(c.get("text", "")))
        s, e = float(c["start"]), float(c["end"])
        if not text or e <= s:
            continue
        lines = _wrap(text, font, max_w)
        pages = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        texts = [" ".join(p) for p in pages]
        total = sum(len(t) for t in texts) or 1
        t = s
        for i, (p, tx) in enumerate(zip(pages, texts)):
            d = (e - s) * len(tx) / total
            te = e if i == len(pages) - 1 else t + d
            words = tx.split(" ")
            ls = _balance(words, font, len(p)) if len(p) == 2 else p
            out.append((t, te, ls))
            t = te
    return out


@dataclass
class _SubStyle:
    W: int
    H: int
    size: int
    band_y: int           # vị trí y của dải phụ đề trên khung
    band_h: int
    center_y: int         # tâm khối chữ, tính trong dải
    max_w: int
    box: bool             # hộp nền tối (ngang) hay viền dày (dọc)
    stroke: int


def _sub_style(W: int, H: int, vertical: bool) -> _SubStyle:
    if vertical:
        size = round(H * 0.05)
        band_h = round(H * 0.24)
        cy = round(H * 0.65)
        return _SubStyle(W, H, size, cy - band_h // 2, band_h, band_h // 2,
                         int(W * 0.86), False, max(4, size // 9))
    size = round(H * 0.042)
    band_h = round(H * 0.22)
    return _SubStyle(W, H, size, H - band_h, band_h, band_h - round(H * 0.07) - size,
                     int(W * 0.78), True, max(2, size // 16))


def _draw_sub(lines: list[str], st: _SubStyle, font) -> Image.Image:
    img = Image.new("RGBA", (st.W, st.band_h), (0, 0, 0, 0))
    lh = round(st.size * 1.22)
    block_h = lh * len(lines)
    y0 = st.center_y - block_h // 2
    if st.box:
        y0 = st.center_y - block_h + lh // 2   # neo đáy: chữ mọc lên trên
        widths = [font.getlength(t) for t in lines]
        pad_x, pad_y = round(st.size * 0.55), round(st.size * 0.28)
        box = Image.new("RGBA", img.size, (0, 0, 0, 0))
        bd = ImageDraw.Draw(box)
        for i, w in enumerate(widths):
            x0 = (st.W - w) / 2 - pad_x
            yy = y0 + i * lh
            bd.rounded_rectangle((x0, yy - pad_y, x0 + w + 2 * pad_x, yy + lh + pad_y - lh * 0.1),
                                 radius=round(st.size * 0.3), fill=(0, 0, 0, 150))
        img = Image.alpha_composite(img, box)
    else:
        # Bóng mờ phía sau cho nổi trên nền sáng.
        sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        for i, t in enumerate(lines):
            x = (st.W - font.getlength(t)) / 2
            sd.text((x + 3, y0 + i * lh + 5), t, font=font, fill=(0, 0, 0, 170),
                    stroke_width=st.stroke + 3, stroke_fill=(0, 0, 0, 170))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(st.size // 8)))
    d = ImageDraw.Draw(img)
    for i, t in enumerate(lines):
        x = (st.W - font.getlength(t)) / 2
        d.text((x, y0 + i * lh), t, font=font, fill=(255, 255, 255, 255),
               stroke_width=st.stroke, stroke_fill=(0, 0, 0, 255))
    return img


class _Subs:
    """Ảnh phụ đề đã vẽ sẵn + cách ghi danh sách concat cho một khoảng thời gian."""

    def __init__(self, cues: list[dict], W: int, H: int, vertical: bool, work: Path, log=print):
        self.st = _sub_style(W, H, vertical)
        font = _font(self.st.size)
        self.dir = work / ("subs_v" if vertical else "subs_h")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.blank = self.dir / "blank.png"
        Image.new("RGBA", (W, self.st.band_h), (0, 0, 0, 0)).save(self.blank)
        self.events: list[tuple[float, float, Path]] = []
        chunks = _chunks(cues, font, self.st.max_w, 2)
        for i, (s, e, lines) in enumerate(chunks):
            p = self.dir / f"c{i:05d}.png"
            _draw_sub(lines, self.st, font).save(p, compress_level=1)
            self.events.append((s, e, p))
        log(f"Đã vẽ {len(chunks)} khung phụ đề.")

    def write_list(self, t0: float, t1: float, path: Path) -> Path:
        """Danh sách concat cho đoạn [t0, t1], thời gian dời về 0. Tính theo mili-giây
        nguyên từ mốc tuyệt đối nên tổng không bị trôi dù có hàng trăm câu."""
        total = round((t1 - t0) * 1000)
        items: list[tuple[int, int, Path]] = []
        for s, e, p in self.events:
            a, b = round((s - t0) * 1000), round((e - t0) * 1000)
            a, b = max(a, 0), min(b, total)
            if b - a >= 40:
                items.append((a, b, p))
        lines, t = [], 0
        for a, b, p in items:
            a = max(a, t)
            if a > t:
                lines.append((self.blank, a - t))
            if b > a:
                lines.append((p, b - a))
                t = b
        lines.append((self.blank, max(1000, total - t + 1000)))
        txt = "ffconcat version 1.0\n"
        for p, ms in lines:
            txt += f"file '{p.as_posix()}'\nduration {ms / 1000:.3f}\n"
        txt += f"file '{self.blank.as_posix()}'\n"
        path.write_text(txt)
        return path


def _hook_png(W: int, H: int, text: str, label: str, out: Path) -> Path:
    """Thẻ tiêu đề lớn ở 3 giây đầu video dọc."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = _nfc(text)
    size = round(H * 0.052)
    font = _font(size)
    lines = _wrap(text, font, W * 0.80) if text else []
    while len(lines) > 5 and size > H * 0.03:
        size -= 4
        font = _font(size)
        lines = _wrap(text, font, W * 0.80)
    lsize = round(H * 0.03)
    lfont = _font(lsize)
    lh = round(size * 1.2)
    block = lh * len(lines) + (lsize + round(H * 0.02) if label else 0)
    pad = round(H * 0.03)
    cy = round(H * 0.33)
    top = cy - block // 2
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((W * 0.05, top - pad, W * 0.95, top + block + pad),
                        radius=round(W * 0.04), fill=(10, 10, 14, 175))
    y = top
    if label:
        d.text(((W - lfont.getlength(label)) / 2, y), label, font=lfont, fill=(255, 210, 90, 255))
        y += lsize + round(H * 0.02)
    for t in lines:
        d.text(((W - font.getlength(t)) / 2, y), t, font=font, fill=(255, 255, 255, 255),
               stroke_width=max(2, size // 14), stroke_fill=(0, 0, 0, 255))
        y += lh
    img.save(out)
    return out


# ───────────────────────── âm thanh ─────────────────────────

def _voice(narr: "Narration", log=print) -> Path:
    """Chuẩn hoá giọng đọc về -16 LUFS (2 lượt loudnorm), lưu cache cạnh file wav."""
    src = Path(narr.wav)
    dst = src.with_name(f"{src.stem}.norm16.wav")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    log("Chuẩn hoá âm lượng giọng đọc…")
    loud = f"loudnorm=I={VOICE_LUFS}:TP=-1.5:LRA=11"
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-af",
         f"{loud}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", probe.stderr)
    if m:
        j = json.loads(m.group(0))
        af = (f"{loud}:measured_I={j['input_i']}:measured_TP={j['input_tp']}"
              f":measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}"
              f":offset={j['target_offset']}:linear=true")
    else:
        af = loud
    tmp = dst.with_suffix(".tmp.wav")
    _ffmpeg(["-i", src, "-af", f"{af},aresample={config.SAMPLE_RATE}", "-ac", "2",
             "-c:a", "pcm_s16le", tmp])
    tmp.replace(dst)
    return dst


def _audio_graph(voice_idx: int, music_idx: int | None, dur: float, mv: float) -> tuple[str, str]:
    fo = max(0.0, dur - 0.6)
    v = (f"[{voice_idx}:a]aformat=sample_rates={config.SAMPLE_RATE}:channel_layouts=stereo,"
         f"apad,atrim=duration={dur:.3f}")
    if music_idx is None:
        return f"{v},afade=t=in:d=0.05,afade=t=out:st={fo:.3f}:d=0.6[aout]", "[aout]"
    mfo = max(0.0, dur - 3.0)
    g = (f"{v},asplit=2[v1][vk];"
         f"[{music_idx}:a]aformat=sample_rates={config.SAMPLE_RATE}:channel_layouts=stereo,"
         f"atrim=duration={dur:.3f},volume={mv:.3f},"
         f"afade=t=in:d=2,afade=t=out:st={mfo:.3f}:d={min(3.0, dur):.3f}[m];"
         f"[m][vk]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=700[md];"
         f"[v1][md]amix=inputs=2:duration=first:normalize=0,"
         f"afade=t=in:d=0.05,afade=t=out:st={fo:.3f}:d=0.6[aout]")
    return g, "[aout]"


# ───────────────────────── ghép thành phẩm ─────────────────────────

def _music(opts: RenderOptions, work: Path, log=print) -> Path | None:
    """Chuẩn hoá nhạc nền 1 lần (-16 LUFS, 48 kHz stereo) để lúc ghép chỉ việc lặp."""
    if not opts.music:
        return None
    src = Path(opts.music)
    if not src.exists():
        log(f"Không thấy file nhạc nền {src}, bỏ qua nhạc.")
        return None
    dst = work / "music_norm.wav"
    if not dst.exists():
        log("Chuẩn hoá nhạc nền…")
        _ffmpeg(["-i", src, "-vn", "-af",
                 f"loudnorm=I={VOICE_LUFS}:TP=-2:LRA=11,aresample={config.SAMPLE_RATE}",
                 "-ac", "2", "-c:a", "pcm_s16le", dst])
    return dst


def _mux(video: Path, dur: float, voice: Path, v_start: float, music: Path | None,
         opts: RenderOptions, out: Path, progress: Progress = None,
         lo: float = 0.0, hi: float = 1.0, msg: str = "") -> Path:
    """Ghép hình (copy, không mã hoá lại) với giọng đọc + nhạc nền đã trộn."""
    inputs: list = ["-i", video]
    if v_start > 0.001:
        inputs += ["-ss", f"{v_start:.3f}"]
    inputs += ["-t", f"{dur + 0.2:.3f}", "-i", voice]
    mi = None
    if music:
        inputs += ["-stream_loop", "-1", "-i", music]
        mi = 2
    graph, alabel = _audio_graph(1, mi, dur, opts.music_volume)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg([*inputs, "-filter_complex", graph, "-map", "0:v", "-map", alabel,
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", config.SAMPLE_RATE,
             "-t", f"{dur:.3f}", "-movflags", "+faststart", out], dur, progress, lo, hi, msg)
    return out


def _work(out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{name}_", dir=out_dir))


def render_landscape(narr: "Narration", opts: RenderOptions, out: Path,
                     progress: Progress = None, log=print) -> Path:
    """Video ngang 1920x1080 cho cả truyện. Phụ đề/watermark được đốt ngay lúc dựng
    b-roll nên mỗi khung hình chỉ mã hoá đúng 1 lần."""
    out = Path(out)
    W, H = LANDSCAPE
    dur = float(narr.duration)
    work = _work(out.parent, ".story_h")
    p = progress or (lambda f, m: None)
    try:
        voice = _voice(narr, log)
        music = _music(opts, work, log)
        p(0.03, "Chuẩn bị âm thanh xong")
        clips = collect_clips(opts.clips_dir)
        subs = _Subs(narr.cues, W, H, False, work, log) \
            if opts.subtitles_landscape and narr.cues else None
        deco = _Deco(subs, None, opts.watermark)
        video = _make_videos(clips, [(0.0, dur)], W, H, opts, work, [deco], log,
                             lambda f, m: p(0.03 + 0.92 * f, m))[0]
        log("Ghép âm thanh (16:9)…")
        _mux(video, dur, voice, 0.0, music, opts, out, p, 0.95, 1.0, "Ghép âm thanh")
        p(1.0, "Xong video ngang")
        log(f"Đã xuất {out.name}")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _split_parts(cues: list[dict], total: float, limit: float,
                 balanced: bool = True) -> list[tuple[float, float]]:
    """Cắt truyện thành các phần ≤ limit giây tại khoảng lặng giữa các câu.
    balanced=True: độ dài các phần cân đối; False: mỗi phần dài nhất có thể (teaser)."""
    if total <= limit + 0.05:
        return [(0.0, total)]
    cs = sorted((float(c["start"]), float(c["end"])) for c in cues)
    cuts = []
    for (s0, e0), (s1, _) in zip(cs, cs[1:]):
        if s1 >= e0:
            cuts.append((e0 + s1) / 2)
        else:
            cuts.append(e0)
    parts, a = [], 0.0
    while total - a > limit + 0.05:
        left = math.ceil((total - a) / limit)
        target = a + (total - a) / left if balanced else a + limit
        cand = [c for c in cuts if a + 5 < c <= a + limit]
        if cand:
            b = min(cand, key=lambda c: abs(c - target))
        else:
            b = a + limit                       # câu quá dài: buộc phải cắt giữa câu
        parts.append((a, b))
        a = b
    parts.append((a, total))
    return parts


def render_vertical(narr: "Narration", opts: RenderOptions, out_dir: Path,
                    progress: Progress = None, log=print) -> list[Path]:
    """Video dọc 1080x1920: part01.mp4, part02.mp4… mỗi phần ≤ vertical_seconds,
    cắt ở khoảng lặng giữa các câu, có thẻ hook 3 giây đầu và phụ đề riêng."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = VERTICAL
    total = float(narr.duration)
    p = progress or (lambda f, m: None)
    if opts.vertical_mode == "full":
        parts = [(0.0, total)]
    else:
        parts = _split_parts(narr.cues or [], total, float(opts.vertical_seconds))
    n_total = len(parts)
    if opts.vertical_mode == "teaser":
        parts = _split_parts(narr.cues or [], total, float(opts.vertical_seconds), False)[:1]
    multi = opts.vertical_mode == "parts" and n_total > 1
    work = _work(out_dir, ".story_v")
    try:
        voice = _voice(narr, log)
        music = _music(opts, work, log)
        clips = collect_clips(opts.clips_dir)
        subs = _Subs(narr.cues, W, H, True, work, log) \
            if opts.subtitles_vertical and narr.cues else None
        decos = []
        for k in range(1, len(parts) + 1):
            label = f"Phần {k}/{n_total}" if multi else ""
            hook = None
            if opts.hook_text.strip() or label:
                hook = _hook_png(W, H, opts.hook_text, label, work / f"hook{k:02d}.png")
            decos.append(_Deco(subs, hook, opts.watermark))
        log(f"Dựng {len(parts)} video dọc…")
        videos = _make_videos(clips, parts, W, H, opts, work, decos, log,
                              lambda f, m: p(0.02 + 0.93 * f, m))
        outs: list[Path] = []
        for k, ((a, b), v) in enumerate(zip(parts, videos), 1):
            out = out_dir / f"part{k:02d}.mp4"
            _mux(v, b - a, voice, a, music, opts, out)
            p(0.95 + 0.05 * k / len(parts), f"Ghép âm thanh phần {k}/{len(parts)}")
            outs.append(out)
        log(f"Đã xuất {len(outs)} video dọc.")
        p(1.0, "Xong video dọc")
        return outs
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ───────────────────────── thumbnail ─────────────────────────

def _grab_frame(src: Path) -> Image.Image:
    src = Path(src)
    if src.suffix.lower() in IMAGE_EXTS:
        return Image.open(src).convert("RGB")
    d = _media_duration(src)
    t = min(5.0, d * 0.3) if d > 0 else 0
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "f.png"
        _ffmpeg(["-ss", f"{t:.3f}", "-i", src, "-frames:v", 1, png])
        return Image.open(png).convert("RGB")


def thumbnail(frame_source: Path, title: str, out: Path, size=(1280, 720)) -> Path:
    """Lấy 1 khung hình, làm tối phần dưới và vẽ tiêu đề lớn (tiếng Việt) dễ đọc."""
    w, h = size
    img = ImageOps.fit(_grab_frame(frame_source), (w, h), Image.LANCZOS)
    vertical = h > w
    title = _nfc(title)
    max_w = w * (0.86 if vertical else 0.90)
    size_px = round(h * (0.065 if vertical else 0.11))
    while True:
        font = _font(size_px)
        lines = _wrap(title, font, max_w)
        if len(lines) <= (5 if vertical else 3) or size_px < h * 0.03:
            break
        size_px -= 4
    lh = round(size_px * 1.18)
    block = lh * len(lines)
    # Vùng tối dần để chữ nổi rõ.
    arr = np.asarray(img, np.float32)
    ramp = np.linspace(0, 1, h)
    if vertical:
        cy = h * 0.42
        fade = np.clip(1 - np.abs(ramp - cy / h) / 0.30, 0, 1) * 0.65
    else:
        fade = np.clip((ramp - 0.30) / 0.70, 0, 1) * 0.80
    arr = arr * (1 - fade[:, None, None])
    img = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(img)
    y = (h * 0.42 - block / 2) if vertical else (h - h * 0.07 - block)
    stroke = max(3, size_px // 12)
    for i, t in enumerate(lines):
        x = (w - font.getlength(t)) / 2
        fill = (255, 214, 90) if (i == 0 and len(lines) > 1) else (255, 255, 255)
        d.text((x, y + i * lh), t, font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out
