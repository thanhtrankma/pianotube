"""Mẫu A: nhạc thư giãn dài. Dựng 1 đoạn hình lặp ngắn rồi lặp cho hết bài."""
import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from . import config, imaging

W, H = config.WIDTH, config.HEIGHT
OVERSCAN = 1.08          # ảnh nguồn lớn hơn khung hình để có chỗ zoom/pan
ZOOM = 0.06              # biên độ zoom "thở"
PAN = 24                 # biên độ lia ngang (px)


def _vignette(w: int, h: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    return np.clip(1.15 - 0.45 * r ** 2, 0.45, 1.0)[..., None]


def _sprite(r: int) -> np.ndarray:
    ax = np.arange(-r * 2, r * 2 + 1)
    g = np.exp(-(ax[None, :] ** 2 + ax[:, None] ** 2) / (2 * (r * 0.6) ** 2))
    return g.astype(np.float32)


class Dust:
    """Hạt bụi sáng bay lên chậm. Mọi chuyển động tuần hoàn theo độ dài đoạn lặp
    nên chỗ nối vòng lặp không bị giật."""

    def __init__(self, count: int, seed: int = 7):
        rng = np.random.default_rng(seed)
        self.x0 = rng.uniform(0, W, count)
        self.y0 = rng.uniform(0, H + 60, count)
        self.laps = rng.integers(1, 3, count)          # số vòng bay trong 1 đoạn lặp
        self.wob = rng.uniform(8, 30, count)
        self.wob_m = rng.integers(1, 3, count)
        self.ph = rng.uniform(0, 1, count)
        self.bright = rng.uniform(0.25, 0.7, count)
        self.sprites = [_sprite(int(r)) for r in rng.integers(2, 5, count)]
        self.color = np.array([255, 236, 200], np.float32)

    def draw(self, f: np.ndarray, phase: float):
        span = H + 60
        ys = (self.y0 - self.laps * phase * span) % span - 30
        xs = self.x0 + self.wob * np.sin(2 * math.pi * (self.wob_m * phase + self.ph))
        tw = 0.6 + 0.4 * np.sin(2 * math.pi * (2 * phase + self.ph))
        for x, y, b, s in zip(xs, ys, self.bright * tw, self.sprites):
            k = s.shape[0] // 2
            x0, y0 = int(x) - k, int(y) - k
            if x0 < 0 or y0 < 0 or x0 + s.shape[1] > W or y0 + s.shape[0] > H:
                continue
            region = f[y0:y0 + s.shape[0], x0:x0 + s.shape[1]]
            region += s[..., None] * self.color * b


def make_loop(image: Image.Image, out: Path, seconds: int = 30, effect: str = "dust") -> Path:
    """effect: "dust" (hạt bụi sáng), "grain" (hạt phim, hợp ảnh chụp điện ảnh), "none"."""
    sw, sh = int(W * OVERSCAN), int(H * OVERSCAN)
    src = imaging.fit_cover(image, (sw, sh))
    src = Image.fromarray((np.asarray(src, np.float32) * _vignette(sw, sh)).astype(np.uint8))
    dust = Dust(70) if effect == "dust" else None
    grain_rng = np.random.default_rng(3) if effect == "grain" else None
    n = seconds * config.FPS

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(config.FPS), "-i", "-",
         "-c:v", config.VIDEO_ENCODER, "-b:v", config.VIDEO_BITRATE, "-pix_fmt", "yuv420p",
         "-g", str(n), str(out)],
        stdin=subprocess.PIPE,
    )
    try:
        for i in range(n):
            phase = i / n
            z = (1 - math.cos(2 * math.pi * phase)) / 2
            # Chừa lề 2*PAN để khung lia ngang không bao giờ vượt ra ngoài ảnh.
            bw = (sw - 2 * PAN - 2) * (1 - ZOOM * z)
            bh = bw * H / W
            cx = sw / 2 + PAN * math.sin(2 * math.pi * phase)
            cy = sh / 2
            box = (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
            frame = src.resize((W, H), Image.BICUBIC, box=box)
            if dust or grain_rng is not None:
                f = np.asarray(frame, np.float32).copy()
                if dust:
                    dust.draw(f, phase)
                if grain_rng is not None:
                    f += grain_rng.normal(0, 5.5, (H, W, 1)).astype(np.float32)
                frame_bytes = np.clip(f, 0, 255).astype(np.uint8).tobytes()
            else:
                frame_bytes = frame.tobytes()
            proc.stdin.write(frame_bytes)
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise SystemExit("ffmpeg lỗi khi dựng đoạn lặp.")
    return out


def assemble(loop: Path, audio: Path, out: Path, seconds: float, visualizer: bool) -> Path:
    """Lặp đoạn hình cho đủ thời lượng và ghép nhạc.
    Không có visualizer thì copy luồng hình (rất nhanh, vài phút cho video 3 tiếng)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
           "-stream_loop", "-1", "-i", str(loop), "-i", str(audio)]
    if visualizer:
        cmd += [
            "-filter_complex",
            f"[1:a]showwaves=s={W}x140:mode=cline:draw=full:scale=sqrt:rate={config.FPS}:colors=0xFFF0DC,"
            "format=rgba,colorchannelmixer=aa=0.3[w];[0:v][w]overlay=0:H-170[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", config.VIDEO_ENCODER, "-b:v", config.VIDEO_BITRATE, "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "copy"]
    cmd += ["-c:a", "copy", "-t", f"{seconds:.2f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    return out
