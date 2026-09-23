"""Mẫu B: phím đàn với nốt nhạc rơi xuống (kiểu Synthesia)."""
import bisect
import math
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pretty_midi

from . import config, imaging

W, H = config.WIDTH, config.HEIGHT
KB_H = 190                      # chiều cao bàn phím
KB_TOP = H - KB_H
LOOKAHEAD = 2.6                 # số giây nốt nhạc hiện trên màn hình trước khi chạm phím
PPS = KB_TOP / LOOKAHEAD        # pixel mỗi giây
LEAD_IN = 2.5                   # khoảng chờ trước nốt đầu tiên
TAIL = 3.0

LOW, HIGH = 21, 108             # A0 .. C8
WHITE_PCS = {0, 2, 4, 5, 7, 9, 11}
N_WHITE = sum(1 for p in range(LOW, HIGH + 1) if p % 12 in WHITE_PCS)
WW = W / N_WHITE                # độ rộng phím trắng
BW = WW * 0.62                  # độ rộng phím đen
BLACK_H = int(KB_H * 0.63)

RIGHT = np.array([92, 200, 250], np.float32)    # tay phải: xanh da trời
LEFT = np.array([255, 164, 92], np.float32)     # tay trái: cam ấm


@dataclass
class Note:
    start: float
    end: float
    pitch: int
    velocity: int
    right: bool


def key_geometry():
    """Trả về {pitch: (x0, x1, is_black)} cho 88 phím."""
    geo, white_i = {}, 0
    for p in range(LOW, HIGH + 1):
        if p % 12 in WHITE_PCS:
            geo[p] = (white_i * WW, (white_i + 1) * WW, False)
            white_i += 1
        else:
            cx = white_i * WW
            geo[p] = (cx - BW / 2, cx + BW / 2, True)
    return {p: (int(round(a)), int(round(b)), blk) for p, (a, b, blk) in geo.items()}


GEO = key_geometry()


def load_notes(midi_path: Path) -> list:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # cảnh báo tempo ở track phụ: đã kiểm tra khớp với FluidSynth
        pm = pretty_midi.PrettyMIDI(str(midi_path))
    insts = [i for i in pm.instruments if not i.is_drum and i.notes]
    notes = []
    if len(insts) >= 2:
        # Track có cao độ trung bình cao nhất là tay phải.
        ranked = sorted(insts, key=lambda i: np.mean([n.pitch for n in i.notes]), reverse=True)
        for rank, inst in enumerate(ranked):
            for n in inst.notes:
                notes.append(Note(n.start, n.end, n.pitch, n.velocity, rank == 0))
    else:
        for inst in insts:
            for n in inst.notes:
                notes.append(Note(n.start, n.end, n.pitch, n.velocity, n.pitch >= 60))
    notes = [n for n in notes if LOW <= n.pitch <= HIGH]
    notes.sort(key=lambda n: n.start)
    return notes


def _glow_sprite(w: int, h: int) -> np.ndarray:
    ys = np.linspace(1, 0, h)[:, None]
    xs = np.linspace(-1, 1, w)[None, :]
    return (np.exp(-(xs ** 2) * 4) * ys ** 2.2).astype(np.float32)


GLOW = _glow_sprite(int(WW * 3), 70)


class Renderer:
    def __init__(self, notes: list, background: np.ndarray, title_layer=None, corner_layer=None):
        self.notes = notes
        self.starts = [n.start for n in notes]
        self.max_len = max((n.end - n.start for n in notes), default=0)
        self.bg = background
        self.title_layer = title_layer      # (rgb, alpha) giữa màn hình, hiện vài giây đầu
        self.corner_layer = corner_layer    # (rgb, alpha) góc trên trái, hiện suốt video

    def _visible(self, t: float):
        lo = bisect.bisect_left(self.starts, t - self.max_len)
        hi = bisect.bisect_right(self.starts, t + LOOKAHEAD)
        return [n for n in self.notes[lo:hi] if n.end > t]

    def frame(self, t: float) -> np.ndarray:
        f = self.bg.copy()
        active = {}
        visible = self._visible(t)

        # Phím trắng vẽ trước, phím đen đè lên sau.
        for black_pass in (False, True):
            for n in visible:
                x0, x1, blk = GEO[n.pitch]
                if blk != black_pass:
                    continue
                if n.start <= t:
                    active[n.pitch] = n
                y_bot = int(KB_TOP - (n.start - t) * PPS)
                y_top = int(KB_TOP - (n.end - t) * PPS)
                y0, y1 = max(0, y_top), min(KB_TOP, y_bot)
                if y1 - y0 < 2:
                    continue
                base = RIGHT if n.right else LEFT
                shade = 0.72 if blk else 1.0
                col = base * shade * (0.8 + 0.2 * n.velocity / 127)
                f[y0:y1, x0 + 1:x1 - 1] = (col * 0.55).astype(np.uint8)       # viền
                f[y0 + 2:y1 - 2, x0 + 3:x1 - 3] = col.astype(np.uint8)       # thân nốt

        self._draw_keyboard(f, active)
        for pitch, n in active.items():
            self._glow(f, pitch, RIGHT if n.right else LEFT)

        if self.corner_layer is not None:
            imaging.blend(f, *self.corner_layer, 48, 40)
        if self.title_layer is not None and t < LEAD_IN + 5:
            a = min(1.0, (t + 0.5) / 1.0, (LEAD_IN + 5 - t) / 1.5)
            rgb, alpha = self.title_layer
            imaging.blend(f, rgb, alpha * max(0.0, a),
                          (W - rgb.shape[1]) // 2, int(H * 0.30))
        return f

    def _draw_keyboard(self, f: np.ndarray, active: dict):
        f[KB_TOP - 4:KB_TOP] = (180, 40, 60)                  # vạch chạm phím
        f[KB_TOP:] = (18, 18, 22)
        for p, (x0, x1, blk) in GEO.items():
            if blk:
                continue
            col = (236, 236, 232)
            if p in active:
                col = (RIGHT if active[p].right else LEFT).astype(np.uint8)
            f[KB_TOP:H - 6, x0 + 1:x1 - 1] = col
            f[H - 14:H - 6, x0 + 1:x1 - 1] = (np.array(col) * 0.8).astype(np.uint8)
        for p, (x0, x1, blk) in GEO.items():
            if not blk:
                continue
            if p in active:
                col = ((RIGHT if active[p].right else LEFT) * 0.75).astype(np.uint8)
                f[KB_TOP:KB_TOP + BLACK_H, x0:x1] = col
            else:
                f[KB_TOP:KB_TOP + BLACK_H, x0:x1] = (22, 22, 26)
                f[KB_TOP + BLACK_H - 10:KB_TOP + BLACK_H, x0 + 2:x1 - 2] = (60, 60, 66)

    def _glow(self, f: np.ndarray, pitch: int, color: np.ndarray):
        x0, x1, _ = GEO[pitch]
        gh, gw = GLOW.shape
        cx = (x0 + x1) // 2
        gx0, gy0 = cx - gw // 2, KB_TOP - gh
        sx0, sx1 = max(0, -gx0), gw - max(0, gx0 + gw - W)
        region = f[gy0:KB_TOP, gx0 + sx0:gx0 + sx1]
        add = GLOW[:, sx0:sx1, None] * color[None, None, :] * 0.9
        np.minimum(region + add, 255, out=add)
        region[:] = add.astype(np.uint8)


def render(midi: Path, audio: Path, out: Path, background: np.ndarray,
           title: str, composer: str) -> float:
    """Dựng video. Trả về thời lượng (giây)."""
    notes = load_notes(midi)
    if not notes:
        raise SystemExit(f"{midi} không có nốt nhạc nào.")
    audio_len = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
        check=True, capture_output=True, text=True).stdout)
    total = LEAD_IN + max(audio_len, notes[-1].end) + TAIL
    n_frames = math.ceil(total * config.FPS)

    renderer = Renderer(
        notes, background,
        title_layer=imaging.text_block(title, composer, 1500, big=True),
        corner_layer=imaging.text_block(f"{composer} — {title}", None, 1100, big=False),
    )
    delay_ms = int(LEAD_IN * 1000)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(config.FPS), "-i", "-",
         "-i", str(audio),
         "-af", f"adelay={delay_ms}:all=1,apad",
         "-c:v", config.VIDEO_ENCODER, "-b:v", config.VIDEO_BITRATE, "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE,
    )
    try:
        for i in range(n_frames):
            t = i / config.FPS - LEAD_IN
            proc.stdin.write(renderer.frame(t).tobytes())
            if i % (config.FPS * 15) == 0:
                print(f"  dựng hình {i / config.FPS:6.0f}s / {total:.0f}s", flush=True)
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise SystemExit("ffmpeg lỗi khi dựng video.")
    return total
