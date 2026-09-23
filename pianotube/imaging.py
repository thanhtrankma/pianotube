"""Tiện ích hình ảnh: nền, chữ, thumbnail."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import config

W, H = config.WIDTH, config.HEIGHT


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size=size)


def fit_cover(img: Image.Image, size=(W, H)) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), size, Image.LANCZOS)


def gradient_background(top=(12, 14, 30), bottom=(38, 22, 52), size=(W, H)) -> Image.Image:
    """Nền gradient dùng khi không có ảnh Gemini."""
    w, h = size
    t = np.linspace(0, 1, h)[:, None, None]
    arr = np.array(top)[None, None, :] * (1 - t) + np.array(bottom)[None, None, :] * t
    arr = np.repeat(arr, w, axis=1)
    # Vầng sáng mờ ở giữa cho đỡ phẳng.
    yy, xx = np.mgrid[0:h, 0:w]
    glow = np.exp(-(((xx - w * 0.5) / (w * 0.45)) ** 2 + ((yy - h * 0.35) / (h * 0.5)) ** 2))
    arr = arr + glow[..., None] * np.array([40, 30, 60])
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def falling_background(image: Image.Image | None) -> np.ndarray:
    """Nền cho mẫu phím rơi: ảnh làm mờ + tối đi để nốt nhạc nổi bật."""
    if image is None:
        img = gradient_background()
    else:
        img = fit_cover(image).filter(ImageFilter.GaussianBlur(18))
        img = ImageEnhance.Brightness(img).enhance(0.32)
    return np.asarray(img, dtype=np.uint8).copy()


def text_block(title: str, subtitle: str | None, max_width: int, big: bool):
    """Vẽ chữ trắng có bóng mờ. Trả về (rgb float32 HxWx3, alpha float32 HxW)."""
    t_size, s_size = (84, 44) if big else (30, 0)
    tf = font(config.FONT_SERIF_BOLD if big else config.FONT_SERIF, t_size)
    sf = font(config.FONT_SERIF_ITALIC, s_size) if subtitle else None
    while tf.getlength(title) > max_width - 40 and t_size > 24:
        t_size -= 4
        tf = font(config.FONT_SERIF_BOLD if big else config.FONT_SERIF, t_size)

    pad = 30
    h = t_size + (s_size + 24 if subtitle else 0) + pad * 2
    canvas = Image.new("L", (max_width, h), 0)
    d = ImageDraw.Draw(canvas)
    align_x = (lambda w: (max_width - w) / 2) if big else (lambda w: pad)
    d.text((align_x(tf.getlength(title)), pad), title, font=tf, fill=255)
    if subtitle:
        d.text((align_x(sf.getlength(subtitle)), pad + t_size + 24), subtitle, font=sf, fill=220)

    shadow = canvas.filter(ImageFilter.GaussianBlur(8))
    text_a = np.asarray(canvas, np.float32) / 255
    shadow_a = np.asarray(shadow, np.float32) / 255 * 0.7
    alpha = np.maximum(text_a, shadow_a)
    # Chữ trắng, bóng đen: màu = trắng theo tỉ lệ text/alpha.
    white = np.divide(text_a, alpha, out=np.zeros_like(alpha), where=alpha > 0)
    rgb = np.repeat((white * 255)[..., None], 3, axis=2)
    if not big:
        alpha *= 0.75
    return rgb, alpha


def blend(frame: np.ndarray, rgb: np.ndarray, alpha, x: int, y: int):
    """Phủ lớp (rgb, alpha) lên frame tại (x, y), ngay trên mảng frame."""
    h, w = rgb.shape[:2]
    h, w = min(h, frame.shape[0] - y), min(w, frame.shape[1] - x)
    if h <= 0 or w <= 0:
        return
    region = frame[y:y + h, x:x + w].astype(np.float32)
    a = np.asarray(alpha)
    a = a[:h, :w, None] if a.ndim else a
    frame[y:y + h, x:x + w] = (region * (1 - a) + rgb[:h, :w] * a).astype(np.uint8)


def thumbnail(base: Image.Image, title: str, subtitle: str, out: Path, top: bool = False) -> Path:
    """Thumbnail 1280x720: ảnh nền + chữ lớn, dễ đọc trên điện thoại."""
    img = fit_cover(base, (1280, 720))
    arr = np.asarray(img, np.float32)
    # Làm tối phía có chữ để chữ dễ đọc.
    ramp = np.linspace(0, 1, 720)
    if top:
        ramp = ramp[::-1]
    fade = np.clip((ramp - 0.35) / 0.65, 0, 1)[:, None, None] * 0.75
    arr = arr * (1 - fade)
    img = Image.fromarray(arr.astype(np.uint8))

    d = ImageDraw.Draw(img)
    size = 96
    tf = font(config.FONT_SERIF_BOLD, size)
    while tf.getlength(title) > 1180 and size > 40:
        size -= 4
        tf = font(config.FONT_SERIF_BOLD, size)
    sf = font(config.FONT_SERIF_ITALIC, 44)
    y = 60 if top else 720 - 70 - size - 60
    for dx, dy in ((3, 3), (0, 0)):
        fill = (0, 0, 0) if dx else (255, 255, 255)
        d.text((60 + dx, y + dy), title, font=tf, fill=fill)
        d.text((62 + dx, y + size + 18 + dy), subtitle, font=sf,
               fill=(0, 0, 0) if dx else (240, 214, 160))
    img.save(out, quality=92)
    return out
