"""Watermark (tên kênh / logo) cho video truyện audio.

Bản ffmpeg đang dùng không có drawtext/subtitles/ass, nên watermark được vẽ bằng Pillow thành
ảnh PNG RGBA (vẽ phóng to SS lần rồi thu nhỏ cho mượt) và chồng lên video bằng filter `overlay`.
Kích thước tính theo min(W, H) nên dùng được cho cả video ngang 1920x1080 lẫn dọc 1080x1920.
"""
from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right", "top-center", "bottom-center", "center"]

TEMPLATES: dict[str, dict] = {
    "pill": {"name": "Viên thuốc", "desc": "Khung bo tròn nền tối mờ, logo tròn + tên kênh"},
    "glass": {"name": "Kính mờ", "desc": "Thẻ trong suốt viền sáng mảnh, logo + 2 dòng chữ"},
    "outline": {"name": "Chữ viền", "desc": "Chữ đậm có viền đen và bóng mềm, không khung"},
    "neon": {"name": "Đèn neon", "desc": "Chữ phát sáng theo màu chủ đạo"},
    "ribbon": {"name": "Dải ruy băng", "desc": "Dải ruy băng chéo ở góc khung hình"},
    "lower-third": {"name": "Thanh tên kênh", "desc": "Thanh tối có vạch màu nhấn kiểu truyền hình"},
    "stamp": {"name": "Con dấu", "desc": "Huy hiệu tròn, chữ chạy quanh vành, logo ở giữa"},
    "minimal": {"name": "Tối giản", "desc": "Chữ nghiêng có chân nhỏ, mờ nhẹ, thanh lịch"},
    "signature": {"name": "Chữ ký", "desc": "Chữ viết tay có nét gạch uốn lượn phía dưới"},
    "cinema": {"name": "Điện ảnh", "desc": "Chữ in hoa giãn cách giữa hai đường kẻ mảnh"},
    "tag": {"name": "Thẻ cạnh", "desc": "Thẻ dọc gắn sát mép khung hình"},
    "tiled": {"name": "Lặp chéo", "desc": "Chữ mờ lặp chéo khắp khung hình, chống reup"},
    "floating": {"name": "Di chuyển", "desc": "Như 'Viên thuốc' nhưng đổi góc mỗi 20 giây, chống cắt khung"},
}

SS = 3                  # hệ số vẽ phóng to (khử răng cưa)
FLOAT_PERIOD = 20.0     # floating: số giây ở mỗi góc
FLOAT_FADE = 0.8        # floating: thời gian mờ dần/hiện dần (giây)


@dataclass
class WatermarkConfig:
    template: str = "pill"
    text: str = ""            # tên kênh, vd. "Truyện Đêm Khuya"
    subtext: str = ""         # dòng phụ (tuỳ chọn), vd. "@truyendemkhuya"
    logo: Path | None = None  # ảnh logo (định dạng bất kỳ), cắt tròn hoặc giữ nguyên nếu có nền trong suốt
    position: str = "top-right"
    opacity: float = 0.85
    scale: float = 1.0
    color: str = "#ffffff"    # màu chính / màu nhấn (hex)


# ---------- phông chữ (phải có dấu tiếng Việt) ----------

_SYS = "/System/Library/Fonts/"
_SUP = _SYS + "Supplemental/"
_AVENIR = _SYS + "Avenir Next.ttc"
_HELV = _SYS + "HelveticaNeue.ttc"
_FALLBACK = [
    (_SUP + "Arial Unicode.ttf", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("C:/Windows/Fonts/arial.ttf", 0),
]
_ROLES = {
    "bold": [(_AVENIR, 0), (_HELV, 1), (_SUP + "Arial Bold.ttf", 0)],
    "demi": [(_AVENIR, 2), (_HELV, 1), (_SUP + "Arial Bold.ttf", 0)],
    "medium": [(_AVENIR, 5), (_HELV, 0), (_SUP + "Arial.ttf", 0)],
    "serif": [(_SUP + "Baskerville.ttc", 0), (_SUP + "Georgia.ttf", 0), (_SUP + "Times New Roman.ttf", 0)],
    "serif_bold": [(_SUP + "Baskerville.ttc", 4), (_SUP + "Georgia Bold.ttf", 0)],
    "serif_italic": [(_SUP + "Cochin.ttc", 2), (_SUP + "Baskerville.ttc", 2), (_SUP + "Georgia Italic.ttf", 0)],
    "script": [(_SUP + "SignPainter.ttc", 1), (_SUP + "Savoye LET.ttc", 1),
               (_SUP + "SnellRoundhand.ttc", 1), (_SUP + "Georgia Italic.ttf", 0)],
}
_VI_PROBE = "ệĐờặữỗ"


@lru_cache(None)
def _has_vi(path: str, index: int) -> bool:
    """Font có đủ chữ tiếng Việt không (so với ô vuông .notdef)."""
    try:
        f = ImageFont.truetype(path, 32, index=index)
    except OSError:
        return False
    notdef = bytes(f.getmask(chr(0xF0000)))
    return all(bytes(f.getmask(c)) != notdef for c in _VI_PROBE)


@lru_cache(None)
def _font_src(role: str):
    for path, idx in _ROLES.get(role, []) + _FALLBACK:
        if Path(path).exists() and _has_vi(path, idx):
            return path, idx
    return None


@lru_cache(512)
def _font(role: str, size: float) -> ImageFont.FreeTypeFont:
    size = max(4, int(round(size)))
    src = _font_src(role)
    if src:
        return ImageFont.truetype(src[0], size, index=src[1])
    return ImageFont.load_default(size=size)


# ---------- tiện ích màu & vẽ ----------

def _nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


def _rgb(s: str, default=(255, 255, 255)) -> tuple:
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _lum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _sat(c) -> float:
    return (max(c[:3]) - min(c[:3])) / 255


def _ink(c) -> tuple:
    """Màu chữ tương phản trên nền màu c."""
    return (22, 22, 28) if _lum(c) > 150 else (255, 255, 255)


def _mix(a, b, t: float) -> tuple:
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))


def _a(c, alpha: int) -> tuple:
    return tuple(c[:3]) + (int(alpha),)


def _blank(w=2, h=2) -> Image.Image:
    return Image.new("RGBA", (max(1, int(w)), max(1, int(h))), (0, 0, 0, 0))


def _scale_alpha(img: Image.Image, k: float) -> Image.Image:
    a = img.getchannel("A").point(lambda v: min(255, int(v * k + 0.5)))
    img = img.copy()
    img.putalpha(a)
    return img


def _text(text: str, font, fill, *, tracking: float = 0, stroke: float = 0, stroke_fill=None) -> Image.Image:
    """Ảnh RGBA vừa khít dòng chữ (có giãn cách chữ tuỳ chọn)."""
    text = _nfc(text)
    if not text:
        return _blank(1, 1)
    stroke = int(round(stroke))
    fill = tuple(fill) if len(fill) == 4 else tuple(fill) + (255,)
    if tracking:
        chars, xs, x = list(text), [], 0.0
        for ch in chars:
            xs.append(x)
            x += font.getlength(ch) + tracking
        boxes = [font.getbbox(ch, anchor="ls", stroke_width=stroke) for ch in chars]
        l = min(xs[i] + b[0] for i, b in enumerate(boxes))
        r = max(xs[i] + b[2] for i, b in enumerate(boxes))
        t = min(b[1] for b in boxes)
        bt = max(b[3] for b in boxes)
    else:
        chars, xs = [text], [0.0]
        l, t, r, bt = font.getbbox(text, anchor="ls", stroke_width=stroke)
    img = _blank(math.ceil(r - l) + 4, math.ceil(bt - t) + 4)
    d = ImageDraw.Draw(img)
    for ch, x in zip(chars, xs):
        d.text((x - l + 2, -t + 2), ch, font=font, fill=fill, anchor="ls",
               stroke_width=stroke, stroke_fill=stroke_fill)
    return img


def _stack(imgs: list, align: str = "l", gap: float = 0) -> Image.Image:
    imgs = [i for i in imgs if i is not None]
    if not imgs:
        return _blank(1, 1)
    w = max(i.width for i in imgs)
    h = int(sum(i.height for i in imgs) + gap * (len(imgs) - 1))
    out, y = _blank(w, h), 0.0
    for i in imgs:
        x = 0 if align == "l" else (w - i.width if align == "r" else (w - i.width) // 2)
        out.alpha_composite(i, (int(x), int(y)))
        y += i.height + gap
    return out


def _glow(img: Image.Image, blur: float, alpha: float, color=(0, 0, 0), dy: float = 0) -> Image.Image:
    """Thêm bóng đổ (hoặc quầng sáng nếu color sáng) phía dưới img; trả về ảnh lớn hơn."""
    pad = int(blur * 3 + abs(dy)) + 2
    a = Image.new("L", (img.width + 2 * pad, img.height + 2 * pad), 0)
    a.paste(img.getchannel("A"), (pad, pad + int(dy)))
    if blur > 0:
        a = a.filter(ImageFilter.GaussianBlur(blur))
    a = a.point(lambda v: min(255, int(v * alpha)))
    out = Image.new("RGBA", a.size, tuple(color[:3]) + (255,))
    out.putalpha(a)
    out.alpha_composite(img, (pad, pad))
    return out


def _down(img: Image.Image) -> Image.Image:
    return img.resize((max(1, round(img.width / SS)), max(1, round(img.height / SS))), Image.LANCZOS)


def _trim(img: Image.Image, thr: int = 3) -> Image.Image:
    bb = img.getchannel("A").point(lambda v: 255 if v > thr else 0).getbbox()
    return img.crop(bb) if bb else _blank()


def _finish(img: Image.Image) -> Image.Image:
    """Ảnh vẽ ở SS → kích thước thật, cắt sát phần nhìn thấy."""
    return _trim(_down(img))


def _rrect(size, radius, fill, outline=None, width=0) -> Image.Image:
    img = _blank(*size)
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius,
                                          fill=fill, outline=outline, width=int(width))
    return img


def _vgradient(size, top, bottom) -> Image.Image:
    """Gradient dọc RGBA (top/bottom là bộ 4 RGBA)."""
    w, h = int(size[0]), int(size[1])
    t = np.linspace(0, 1, h)[:, None, None]
    arr = np.array(top, float)[None, None, :] * (1 - t) + np.array(bottom, float)[None, None, :] * t
    return Image.fromarray(np.repeat(arr, w, axis=1).clip(0, 255).astype(np.uint8), "RGBA")


def _hgradient(size, left, right) -> Image.Image:
    return _vgradient((size[1], size[0]), left, right).transpose(Image.Transpose.ROTATE_270)


def _mask_with(img: Image.Image, mask: Image.Image) -> Image.Image:
    img = img.copy()
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))
    return img


def _logo(path, d: float, ring: float = 0, ring_color=(255, 255, 255, 210)) -> Image.Image | None:
    """Logo d×d: cắt tròn; nếu ảnh có nền trong suốt thì giữ nguyên hình (contain)."""
    if not path:
        return None
    try:
        im = Image.open(Path(path))
        im.load()
        im = ImageOps.exif_transpose(im).convert("RGBA")
    except (OSError, ValueError):
        return None
    d = max(4, int(d))
    alpha = np.asarray(im.getchannel("A"))
    if (alpha < 128).mean() > 0.03:
        im = ImageOps.contain(im, (d, d), Image.LANCZOS)
        out = _blank(d, d)
        out.alpha_composite(im, ((d - im.width) // 2, (d - im.height) // 2))
        return out
    im = ImageOps.fit(im, (d, d), Image.LANCZOS)
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
    im = _mask_with(im, mask)
    if ring:
        ImageDraw.Draw(im).ellipse((0, 0, d - 1, d - 1), outline=ring_color, width=max(1, int(ring)))
    return im


def _align(pos: str) -> str:
    return "l" if "left" in pos else ("r" if "right" in pos else "c")


def _initials(text: str) -> str:
    words = [w for w in _nfc(text).split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:3]).upper()


# ---------- các mẫu ----------
# Mỗi hàm nhận (cfg, s, ctx) với s = min(W,H)·scale·SS (đơn vị vẽ), trả về ảnh ở kích thước thật.

def _pill_raw(cfg, s, ctx) -> Image.Image:
    col = _rgb(cfg.color)
    t1 = _text(cfg.text, _font("demi", s * 0.027), col) if _nfc(cfg.text) else None
    t2 = _text(cfg.subtext, _font("medium", s * 0.0175), _a(col, 190)) if _nfc(cfg.subtext) else None
    lines = [t for t in (t1, t2) if t]
    block = _stack(lines, "l", s * 0.005) if lines else None
    h = max(s * 0.064, (block.height if block else 0) + s * 0.03)
    pad = h * 0.13
    logo = _logo(ctx["logo"], h - 2 * pad, ring=s * 0.0018)
    if block is None:
        w = h
    else:
        left = (pad + logo.width + s * 0.013) if logo else h * 0.48
        w = left + block.width + h * 0.5
    w, h = int(w), int(h)
    img = _rrect((w, h), h / 2, (12, 12, 18, 155), outline=(255, 255, 255, 46), width=max(1, s * 0.0012))
    if logo:
        img.alpha_composite(logo, (int(pad), int(pad)))
    if block:
        img.alpha_composite(block, (int(left), int((h - block.height) / 2)))
    return _finish(_glow(img, s * 0.012, 0.45, dy=s * 0.004))


def _t_pill(cfg, s, ctx):
    return _pill_raw(cfg, s, ctx)


def _t_glass(cfg, s, ctx):
    col = _rgb(cfg.color)
    t1 = _text(cfg.text, _font("bold", s * 0.031), col) if _nfc(cfg.text) else None
    t2 = _text(cfg.subtext, _font("medium", s * 0.019), (255, 255, 255, 205)) if _nfc(cfg.subtext) else None
    lines = [t for t in (t1, t2) if t]
    block = _stack(lines, "l", s * 0.007) if lines else None
    pad = s * 0.021
    logo_d = max(s * 0.07, (block.height if block else 0))
    logo = _logo(ctx["logo"], logo_d, ring=s * 0.002, ring_color=(255, 255, 255, 150))
    inner_h = max(logo.height if logo else 0, block.height if block else 0)
    w = pad * 2 + (logo.width if logo else 0) + (block.width if block else 0) + (s * 0.017 if logo and block else 0)
    h = inner_h + pad * 2
    w, h, r = int(w), int(h), s * 0.024
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
    card = _blank(w, h)
    card.alpha_composite(_mask_with(Image.new("RGBA", (w, h), (16, 18, 30, 128)), mask))
    card.alpha_composite(_mask_with(_vgradient((w, h), (255, 255, 255, 48), (255, 255, 255, 8)), mask))
    # Vệt sáng chéo nhẹ ở nửa trên tạo cảm giác mặt kính.
    sheen = _blank(w, h)
    ImageDraw.Draw(sheen).polygon([(0, 0), (w * 0.55, 0), (w * 0.35, h), (0, h)], fill=(255, 255, 255, 16))
    card.alpha_composite(_mask_with(sheen, mask))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, outline=(255, 255, 255, 120),
                                           width=max(1, int(s * 0.0014)))
    x = pad
    if logo:
        card.alpha_composite(logo, (int(x), int((h - logo.height) / 2)))
        x += logo.width + s * 0.017
    if block:
        card.alpha_composite(_glow(block, s * 0.004, 0.35), (int(x) - int(s * 0.004 * 3) - 2,
                                                             int((h - block.height) / 2) - int(s * 0.004 * 3) - 2))
    return _finish(_glow(card, s * 0.02, 0.35, dy=s * 0.006))


def _t_outline(cfg, s, ctx):
    col = _rgb(cfg.color)
    al = _align(ctx["pos"])
    t1 = _text(cfg.text, _font("bold", s * 0.044), col, stroke=s * 0.0035, stroke_fill=(0, 0, 0, 235)) \
        if _nfc(cfg.text) else None
    t2 = _text(cfg.subtext, _font("demi", s * 0.022), _mix(col, (255, 255, 255), 0.3), stroke=s * 0.0026,
               stroke_fill=(0, 0, 0, 225)) if _nfc(cfg.subtext) else None
    logo = _logo(ctx["logo"], s * 0.08, ring=s * 0.003, ring_color=(0, 0, 0, 230))
    block = _stack([t for t in (t1, t2) if t], al, s * 0.002) if (t1 or t2) else None
    if logo and block:
        w = logo.width + s * 0.014 + block.width
        h = max(logo.height, block.height)
        img = _blank(w, h)
        if al == "r":
            img.alpha_composite(block, (0, int((h - block.height) / 2)))
            img.alpha_composite(logo, (int(w - logo.width), int((h - logo.height) / 2)))
        else:
            img.alpha_composite(logo, (0, int((h - logo.height) / 2)))
            img.alpha_composite(block, (int(logo.width + s * 0.014), int((h - block.height) / 2)))
    else:
        img = block or logo
    return _finish(_glow(img, s * 0.01, 0.65, dy=s * 0.005))


def _t_neon(cfg, s, ctx):
    col = _rgb(cfg.color)
    if _sat(col) < 0.15:
        col = (64, 224, 255)  # màu trắng/xám không phát sáng đẹp → dùng xanh neon
    core = _mix(col, (255, 255, 255), 0.72)
    t1 = _text(cfg.text, _font("medium", s * 0.04), core, tracking=s * 0.0015) if _nfc(cfg.text) else None
    t2 = _text(cfg.subtext, _font("medium", s * 0.019), _mix(col, (255, 255, 255), 0.5), tracking=s * 0.002) \
        if _nfc(cfg.subtext) else None
    logo = _logo(ctx["logo"], s * 0.07, ring=s * 0.003, ring_color=_a(core, 255))
    block = _stack([t for t in (t1, t2) if t], _align(ctx["pos"]), s * 0.012) if (t1 or t2) else None
    if logo and block:
        g = s * 0.02
        base = _blank(logo.width + g + block.width, max(logo.height, block.height))
        base.alpha_composite(logo, (0, (base.height - logo.height) // 2))
        base.alpha_composite(block, (int(logo.width + g), (base.height - block.height) // 2))
    else:
        base = block or logo
    pad = int(s * 0.07)
    canvas = _blank(base.width + 2 * pad, base.height + 2 * pad)
    a = Image.new("L", canvas.size, 0)
    a.paste(base.getchannel("A"), (pad, pad))
    # Nền tối loang nhẹ để neon nổi trên ảnh sáng.
    dark = a.filter(ImageFilter.GaussianBlur(s * 0.03)).point(lambda v: min(255, int(v * 1.1)))
    layer = Image.new("RGBA", canvas.size, (0, 0, 10, 255))
    layer.putalpha(dark)
    canvas.alpha_composite(layer)
    for blur, k in ((s * 0.024, 1.3), (s * 0.009, 1.6), (s * 0.003, 1.8)):
        g = Image.new("RGBA", canvas.size, col + (255,))
        g.putalpha(a.filter(ImageFilter.GaussianBlur(blur)).point(lambda v, k=k: min(255, int(v * k))))
        canvas.alpha_composite(g)
    canvas.alpha_composite(base, (pad, pad))
    return _finish(canvas)


def _t_ribbon(cfg, s, ctx):
    col = _rgb(cfg.color)
    ink = _ink(col)
    pos = ctx["pos"]
    bh = s * 0.054
    label = _nfc(cfg.text).upper() or _nfc(cfg.subtext)
    fsize = bh * 0.4
    t = _text(label, _font("demi", fsize), ink, tracking=fsize * 0.08) if label else None
    logo = _logo(ctx["logo"], bh * 0.7, ring=s * 0.0015, ring_color=_a(ink, 200))
    parts = [p for p in (logo, t) if p]
    gap = bh * 0.25
    tw = sum(p.width for p in parts) + gap * (len(parts) - 1)
    d_max = s * 0.36
    d = max(s * 0.1, tw / 2 + bh * 1.15)
    if d > d_max and t:  # chữ quá dài → thu nhỏ chữ
        k = max(0.3, (d_max - bh * 1.15) * 2 / max(1, tw))
        t = t.resize((max(1, int(t.width * k)), max(1, int(t.height * k))), Image.LANCZOS)
        if logo:
            logo = logo.resize((max(1, int(logo.width * k)),) * 2, Image.LANCZOS)
        parts = [p for p in (logo, t) if p]
        tw = sum(p.width for p in parts) + gap * (len(parts) - 1)
        d = max(s * 0.1, tw / 2 + bh * 1.15)
    frame_cap = min(ctx["W"], ctx["H"]) * SS * 0.3  # góc ruy băng không quá ~42% cạnh ngắn
    if d > frame_cap * 1.01:
        return _t_ribbon(cfg, s * frame_cap / d * 0.98, ctx)
    T = int((d + bh / 2) * math.sqrt(2) + s * 0.012)
    L = int(2 * (d + bh) * 1.6)
    band = _vgradient((L, int(bh)), _a(_mix(col, (255, 255, 255), 0.18), 250), _a(_mix(col, (0, 0, 0), 0.18), 250))
    dr = ImageDraw.Draw(band)
    stitch = _a(ink, 90)
    m, dash = bh * 0.11, bh * 0.18
    for yy in (m, bh - m - 1):
        x = 0.0
        while x < L:
            dr.line((x, yy, x + dash, yy), fill=stitch, width=max(1, int(s * 0.001)))
            x += dash * 2
    x = (L - tw) / 2
    for p in parts:
        band.alpha_composite(p, (int(x), int((bh - p.height) / 2)))
        x += p.width + gap
    up = pos in ("top-left", "bottom-right")
    rot = band.rotate(45 if up else -45, resample=Image.BICUBIC, expand=True)
    rot = _glow(rot, s * 0.008, 0.55, dy=s * 0.003)
    c = d / math.sqrt(2)
    cx = c if "left" in pos else T - c
    cy = c if "top" in pos else T - c
    tile = _blank(T, T)
    tile.paste(rot, (int(cx - rot.width / 2), int(cy - rot.height / 2)), rot)
    return _down(tile)


def _t_lower_third(cfg, s, ctx):
    col = _rgb(cfg.color)
    main, sub = _nfc(cfg.text), _nfc(cfg.subtext)
    t1 = _text(main, _font("bold", s * 0.03), (255, 255, 255)) if main else None
    hb = s * 0.07
    stripe = s * 0.008
    logo = _logo(ctx["logo"], hb * 0.72, ring=s * 0.0015)
    x0 = stripe + hb * 0.2
    content = (logo.width if logo else 0) + (s * 0.014 if logo and t1 else 0) + (t1.width if t1 else 0)
    tail = hb * 1.4 if "right" not in ctx["pos"] else hb * 0.35
    wb = int(x0 + content + tail)
    bar = _hgradient((wb, int(hb)), (10, 12, 20, 225), (10, 12, 20, 190))
    if "right" not in ctx["pos"]:  # đuôi thanh mờ dần
        fade = np.ones(wb, float)
        n = int(hb * 1.0)
        fade[-n:] = np.linspace(1, 0, n) ** 1.5
        a = np.asarray(bar.getchannel("A"), float) * fade[None, :]
        bar.putalpha(Image.fromarray(a.astype(np.uint8)))
    ImageDraw.Draw(bar).rectangle((0, 0, stripe, hb), fill=_a(col, 255))
    x = x0
    if logo:
        bar.alpha_composite(logo, (int(x), int((hb - logo.height) / 2)))
        x += logo.width + s * 0.014
    if t1:
        bar.alpha_composite(t1, (int(x), int((hb - t1.height) / 2)))
    if not sub:
        return _finish(_glow(bar, s * 0.01, 0.4, dy=s * 0.004))
    ink = _ink(col)
    fs = s * 0.0165
    t2 = _text(sub.upper() if sub.startswith("@") is False else sub, _font("demi", fs), ink, tracking=fs * 0.06)
    ht = int(t2.height + s * 0.014)
    tab = _rrect((int(t2.width + s * 0.03), ht), 0, _a(col, 240))
    tab.alpha_composite(t2, (int(s * 0.015), (ht - t2.height) // 2))
    out = _blank(max(wb, int(stripe) + tab.width), int(hb) + ht)
    out.alpha_composite(bar)
    out.alpha_composite(tab, (0, int(hb)))
    return _finish(_glow(out, s * 0.01, 0.4, dy=s * 0.004))


def _arc_text(img, text, font, color, cx, cy, r, center_deg, bottom=False, tracking=0.0):
    """Vẽ chữ chạy theo cung tròn bán kính r (tâm chữ nằm trên cung). Góc tính từ đỉnh, chiều kim đồng hồ."""
    text = _nfc(text)
    if not text:
        return
    l, t, rr, b = font.getbbox("ẤỆĐgyỵ", anchor="ls")
    lh = b - t
    adv = [font.getlength(ch) + tracking for ch in text]
    total = sum(adv) - tracking
    span = math.degrees(total / r)
    pos = -span / 2
    for ch, a in zip(text, adv):
        w = font.getlength(ch)
        mid = pos + math.degrees((w / 2) / r)
        theta = center_deg + (-mid if bottom else mid)
        pad = int(lh * 0.3)
        g = _blank(w + 2 * pad, lh + 2 * pad)
        ImageDraw.Draw(g).text((pad, pad - t), ch, font=font, fill=color, anchor="ls")
        rot = g.rotate((180 - theta) if bottom else -theta, resample=Image.BICUBIC, expand=True)
        rad = math.radians(theta)
        px, py = cx + r * math.sin(rad), cy - r * math.cos(rad)
        img.alpha_composite(rot, (int(px - rot.width / 2), int(py - rot.height / 2)))
        pos += math.degrees(a / r)


def _t_stamp(cfg, s, ctx):
    col = _rgb(cfg.color)
    D = int(s * 0.19)
    R = D / 2
    img = _blank(D + 8, D + 8)
    cx = cy = (D + 8) / 2
    dr = ImageDraw.Draw(img)
    ring_w = D * 0.022
    band_in = R - D * 0.175
    dr.ellipse((cx - R, cy - R, cx + R, cy + R), fill=(10, 10, 16, 150))
    dr.ellipse((cx - band_in, cy - band_in, cx + band_in, cy + band_in), fill=(10, 10, 16, 60))
    dr.ellipse((cx - R, cy - R, cx + R, cy + R), outline=_a(col, 245), width=int(ring_w))
    r2 = R - ring_w - D * 0.012
    dr.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), outline=_a(col, 150), width=max(1, int(D * 0.005)))
    dr.ellipse((cx - band_in, cy - band_in, cx + band_in, cy + band_in), outline=_a(col, 220),
               width=max(1, int(D * 0.009)))
    r_text = (r2 + band_in) / 2
    main = _nfc(cfg.text).upper()
    sub = _nfc(cfg.subtext)
    # Cỡ chữ vòng trên: vừa trong khoảng 200°.
    fs = D * 0.075
    f = _font("demi", fs)
    while main and fs > D * 0.035:
        f = _font("demi", fs)
        if (f.getlength(main) + fs * 0.12 * len(main)) / r_text < math.radians(196):
            break
        fs *= 0.93
    _arc_text(img, main, f, _a(col, 255), cx, cy, r_text - fs * 0.05, 0, tracking=fs * 0.12)
    sub_text = sub if sub else "★ ★ ★"
    fs2 = min(fs * 0.85, D * 0.06)
    f2 = _font("medium", fs2)
    while fs2 > D * 0.03 and f2.getlength(sub_text) / r_text > math.radians(120):
        fs2 *= 0.93
        f2 = _font("medium", fs2)
    _arc_text(img, sub_text, f2, _a(col, 230), cx, cy, r_text + fs2 * 0.05, 180, bottom=True, tracking=fs2 * 0.08)
    # Chấm ngăn cách hai bên.
    for ang in (90, 270):
        rad = math.radians(ang)
        px, py, rd = cx + r_text * math.sin(rad), cy - r_text * math.cos(rad), D * 0.012
        dr.ellipse((px - rd, py - rd, px + rd, py + rd), fill=_a(col, 240))
    inner = band_in - D * 0.02
    logo = _logo(ctx["logo"], inner * 2)
    if logo:
        img.alpha_composite(logo, (int(cx - logo.width / 2), int(cy - logo.height / 2)))
    else:
        ini = _initials(cfg.text) or "★"
        fi = inner * 0.9
        ft = _font("serif_bold", fi)
        while fi > 8 and ft.getlength(ini) > inner * 1.5:
            fi *= 0.9
            ft = _font("serif_bold", fi)
        ti = _text(ini, ft, _a(col, 255))
        img.alpha_composite(ti, (int(cx - ti.width / 2), int(cy - ti.height / 2)))
    img = img.rotate(-8, resample=Image.BICUBIC, expand=True)
    return _finish(_glow(img, s * 0.01, 0.45, dy=s * 0.004))


def _t_minimal(cfg, s, ctx):
    col = _rgb(cfg.color)
    al = _align(ctx["pos"])
    t1 = _text(cfg.text, _font("serif_italic", s * 0.034), _a(col, 240)) if _nfc(cfg.text) else None
    fs = s * 0.0135
    t2 = _text(_nfc(cfg.subtext), _font("medium", fs), _a(col, 205), tracking=fs * 0.18) \
        if _nfc(cfg.subtext) else None
    rule = None
    if t1 and t2:
        rw = int(max(t1.width * 0.28, s * 0.03))
        rule = _blank(rw, max(2, int(s * 0.0012)) + 2)
        ImageDraw.Draw(rule).line((0, 1, rw, 1), fill=_a(col, 140), width=max(1, int(s * 0.0012)))
    logo = _logo(ctx["logo"], s * 0.045)
    block = _stack([p for p in (t1, rule, t2) if p], al, s * 0.006) if (t1 or t2) else None
    if logo and block:
        g = s * 0.012
        img = _blank(logo.width + g + block.width, max(logo.height, block.height))
        lx, bx = (img.width - logo.width, 0) if al == "r" else (0, int(logo.width + g))
        img.alpha_composite(logo, (int(lx), (img.height - logo.height) // 2))
        img.alpha_composite(block, (int(bx), (img.height - block.height) // 2))
    else:
        img = block or logo
    return _finish(_glow(img, s * 0.006, 0.6, dy=s * 0.002))


def _swash(w: float, h: float, thick: float, color) -> Image.Image:
    """Nét gạch viết tay thon hai đầu, hơi cong."""
    img = _blank(w, h + thick * 2)
    n = 80
    pts_top, pts_bot = [], []
    for i in range(n + 1):
        t = i / n
        x = w * t
        y = thick + h * (0.15 + 0.85 * (1 - t) ** 1.6) * 0.9 + h * 0.1 * math.sin(t * math.pi)
        th = thick * math.sin(math.pi * min(1, t * 1.15)) ** 0.8 if t * 1.15 < 1 else thick * 0.15
        pts_top.append((x, y - th / 2))
        pts_bot.append((x, y + th / 2))
    ImageDraw.Draw(img).polygon(pts_top + pts_bot[::-1], fill=color)
    return img


def _t_signature(cfg, s, ctx):
    col = _rgb(cfg.color)
    al = _align(ctx["pos"])
    main = _nfc(cfg.text) or _nfc(cfg.subtext)
    sub = _nfc(cfg.subtext) if _nfc(cfg.text) else ""
    t1 = _text(main, _font("script", s * 0.066), col) if main else None
    logo = _logo(ctx["logo"], s * 0.065, ring=s * 0.002)
    parts = []
    if t1:
        sw = _swash(t1.width * 0.9, s * 0.012, s * 0.0045, _a(col, 230))
        head = _blank(t1.width, t1.height + sw.height * 0.55)
        head.alpha_composite(sw, (int(t1.width * 0.08), int(t1.height - sw.height * 0.45)))
        head.alpha_composite(t1, (0, 0))
        parts.append(head)
    if sub:
        fs = s * 0.0155
        parts.append(_text(sub, _font("medium", fs), _a(col, 200), tracking=fs * 0.12))
    block = _stack(parts, "r" if al != "l" else "l", s * 0.004) if parts else None
    if logo and block:
        g = s * 0.014
        img = _blank(logo.width + g + block.width, max(logo.height, block.height))
        lx, bx = (img.width - logo.width, 0) if al == "r" else (0, int(logo.width + g))
        img.alpha_composite(logo, (int(lx), (img.height - logo.height) // 2))
        img.alpha_composite(block, (int(bx), (img.height - block.height) // 2))
    else:
        img = block or logo
    img = _glow(img, s * 0.003, 0.5)
    return _finish(_glow(img, s * 0.009, 0.55, dy=s * 0.003))


def _t_cinema(cfg, s, ctx):
    col = _rgb(cfg.color)
    main = _nfc(cfg.text).upper()
    fs = s * 0.025
    t1 = _text(main, _font("serif", fs), col, tracking=fs * 0.28) if main else None
    t2 = _text(cfg.subtext, _font("serif_italic", s * 0.02), _a(col, 200)) if _nfc(cfg.subtext) else None
    logo = _logo(ctx["logo"], s * 0.05, ring=s * 0.0015)
    centre = t1 or logo
    if centre is None:
        return _finish(_glow(t2, s * 0.006, 0.5))
    lw, gap, lt = s * 0.07, s * 0.02, max(1, int(s * 0.0013))
    row = _blank(centre.width + 2 * (lw + gap), max(centre.height, s * 0.02))
    dr = ImageDraw.Draw(row)
    ym = row.height / 2
    dd = s * 0.0035
    for x0, x1 in ((0, lw), (row.width - lw, row.width)):
        dr.line((x0, ym, x1, ym), fill=_a(col, 170), width=lt)
    for xd in (lw, row.width - lw):  # hạt kim cương ở đầu đường kẻ
        dr.polygon([(xd, ym - dd), (xd + dd, ym), (xd, ym + dd), (xd - dd, ym)], fill=_a(col, 220))
    row.alpha_composite(centre, (int((row.width - centre.width) / 2), int((row.height - centre.height) / 2)))
    img = _stack([row] + ([t2] if t2 else []), "c", s * 0.008)
    if logo and t1:
        img = _stack([logo, img], "c", s * 0.008)
    return _finish(_glow(img, s * 0.008, 0.55, dy=s * 0.003))


def _t_tag(cfg, s, ctx):
    col = _rgb(cfg.color)
    ink = _ink(col)
    right = "left" not in ctx["pos"]
    th = s * 0.056
    label = _nfc(cfg.text) or _nfc(cfg.subtext)
    fs = th * 0.4
    t = _text(label, _font("demi", fs), ink, tracking=fs * 0.05) if label else None
    logo = _logo(ctx["logo"], th * 0.72, ring=s * 0.0015, ring_color=_a(ink, 200))
    pad, g = th * 0.42, th * 0.22
    length = pad * 2 + (t.width if t else 0) + (logo.width if logo else 0) + (g if t and logo else 0)
    over = th * 0.5  # phần bo tròn bị cắt ở mép khung
    strip = _hgradient((int(length), int(th + over)), _a(_mix(col, (255, 255, 255), 0.1), 245),
                       _a(_mix(col, (0, 0, 0), 0.12), 245))
    mask = Image.new("L", strip.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, strip.width - 1, strip.height - 1 + 0), radius=th * 0.32, fill=255)
    strip = _mask_with(strip, mask)
    x, oy = pad, over
    if logo:
        strip.alpha_composite(logo, (int(x), int(oy + (th - logo.height) / 2)))
        x += logo.width + g
    if t:
        strip.alpha_composite(t, (int(x), int(oy + (th - t.height) / 2)))
    strip = strip.crop((0, int(over), strip.width, int(over + th)))  # mặt trên phẳng (sát mép)
    # Dải nằm ngang, mặt phẳng ở trên → xoay để mặt phẳng chạm mép khung
    # (mép phải: chữ đọc từ trên xuống; mép trái: từ dưới lên).
    tab = strip.rotate(-90 if right else 90, expand=True)
    sh = _glow(tab, s * 0.01, 0.45)
    pad_px = (sh.width - tab.width) // 2
    sh = sh.crop((0, 0, sh.width - pad_px, sh.height)) if right else sh.crop((pad_px, 0, sh.width, sh.height))
    return _down(sh)


def _t_tiled(cfg, s, ctx):
    W, H = ctx["W"], ctx["H"]
    col = _rgb(cfg.color)
    u = s / SS
    parts = [p for p in (_nfc(cfg.text), _nfc(cfg.subtext)) if p]
    if parts:
        f = _font("demi", u * 0.03)
        cell = _text("   ·   ".join(parts), f, _a(col, 255), stroke=max(1, u * 0.0012), stroke_fill=(0, 0, 0, 90))
    else:
        cell = _logo(ctx["logo"], u * 0.08)
        if cell is None:
            return _blank(W, H)
    side = int(math.hypot(W, H)) + cell.width * 2
    big = _blank(side, side)
    dx, dy = cell.width + u * 0.16, cell.height + u * 0.13
    row = 0
    y = 0.0
    while y < side:
        x = -(dx / 2 if row % 2 else 0)
        while x < side:
            big.paste(cell, (int(x), int(y)), cell)
            x += dx
        y += dy
        row += 1
    big = big.rotate(28, resample=Image.BICUBIC)
    l, t = (side - W) // 2, (side - H) // 2
    return _scale_alpha(big.crop((l, t, l + W, t + H)), 0.2)


_RENDER = {
    "pill": (_t_pill, "box"),
    "glass": (_t_glass, "box"),
    "outline": (_t_outline, "box"),
    "neon": (_t_neon, "box"),
    "ribbon": (_t_ribbon, "corner"),
    "lower-third": (_t_lower_third, "box"),
    "stamp": (_t_stamp, "box"),
    "minimal": (_t_minimal, "box"),
    "signature": (_t_signature, "box"),
    "cinema": (_t_cinema, "box"),
    "tag": (_t_tag, "edge"),
    "tiled": (_t_tiled, "full"),
    "floating": (_t_pill, "box"),
}


# ---------- vị trí ----------

def _key(cfg) -> str:
    return cfg.template if cfg.template in _RENDER else "pill"


def _pos(cfg) -> str:
    return cfg.position if cfg.position in POSITIONS else "top-right"


def _margins(W: int, H: int) -> dict:
    """Lề an toàn. Video dọc: tránh vùng giao diện Shorts/Reels (đáy ~18%, cột nút bên phải ~12%)."""
    m = round(min(W, H) * 0.035)
    if H > W:
        return {"l": m, "t": max(m, round(H * 0.05)), "r_top": max(m, round(W * 0.06)),
                "r_bot": max(m, round(W * 0.12)), "b": max(m, round(H * 0.18))}
    return {"l": m, "t": m, "r_top": m, "r_bot": m, "b": m}


def _corner(pos: str) -> str:
    if pos in ("top-left", "top-right", "bottom-left", "bottom-right"):
        return pos
    return "bottom-right" if pos == "bottom-center" else "top-right"


def _box_xy(pos: str, W: int, H: int, w: int, h: int) -> tuple[int, int]:
    m = _margins(W, H)
    if "left" in pos:
        x = m["l"]
    elif "right" in pos:
        x = W - w - (m["r_bot"] if "bottom" in pos else m["r_top"])
    else:
        x = (W - w) // 2
    if pos.startswith("top"):
        y = m["t"]
    elif pos.startswith("bottom"):
        y = H - h - m["b"]
    else:
        y = (H - h) // 2
    return int(min(max(0, x), max(0, W - w))), int(min(max(0, y), max(0, H - h)))


def position_xy(cfg: WatermarkConfig, W: int, H: int, size: tuple[int, int]) -> tuple[int, int]:
    """Toạ độ góc trên-trái để đặt ảnh watermark (kích thước size) lên khung W×H."""
    kind = _RENDER[_key(cfg)][1]
    w, h = size
    pos = _pos(cfg)
    if kind == "full":
        return 0, 0
    if kind == "corner":
        c = _corner(pos)
        return (0 if "left" in c else W - w), (0 if "top" in c else H - h)
    if kind == "edge":
        _, y = _box_xy(pos, W, H, w, h)
        return (0 if "left" in pos else W - w), y
    return _box_xy(pos, W, H, w, h)


# ---------- API ----------

def _has_content(cfg) -> bool:
    return bool(_nfc(cfg.text) or _nfc(cfg.subtext) or (cfg.logo and Path(cfg.logo).exists()))


def render_png(cfg: WatermarkConfig, W: int, H: int) -> Image.Image:
    """Ảnh RGBA của watermark (chỉ phần watermark; riêng mẫu 'tiled' là nguyên khung W×H)."""
    if not _has_content(cfg):
        return _blank(2, 2)
    fn, kind = _RENDER[_key(cfg)]
    pos = _pos(cfg)
    if kind == "corner":
        pos = _corner(pos)
    logo = Path(cfg.logo) if cfg.logo and Path(cfg.logo).exists() else None
    ctx = {"W": W, "H": H, "pos": pos, "logo": logo}
    scale = min(3.0, max(0.3, float(cfg.scale or 1.0)))
    s = min(W, H) * scale * SS
    img = fn(cfg, s, ctx)
    if kind in ("box", "edge"):
        m = _margins(W, H)
        max_w = W - m["l"] - m["r_bot"]
        max_h = H * 0.6 if kind == "edge" else H - m["t"] - m["b"]
        k = min(max_w / img.width, max_h / img.height)
        if k < 1:  # tên quá dài so với khung → vẽ lại nhỏ hơn
            img = fn(cfg, s * k * 0.98, ctx)
    op = min(1.0, max(0.0, float(cfg.opacity)))
    return _scale_alpha(img, op) if op < 1 else img


def _gradient_bg(W: int, H: int) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = (yy / max(1, H - 1))[..., None]
    arr = np.array([28, 36, 66], np.float32) * (1 - t) + np.array([92, 52, 84], np.float32) * t
    glow = np.exp(-(((xx - W * 0.65) / (W * 0.4)) ** 2 + ((yy - H * 0.3) / (H * 0.45)) ** 2))
    arr += glow[..., None] * np.array([70, 55, 40], np.float32)
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8))


def preview(cfg: WatermarkConfig, W: int = 1280, H: int = 720, background: Path | None = None) -> Image.Image:
    """Ảnh RGB xem trước: watermark đặt lên ảnh nền (cắt vừa khung) hoặc nền gradient."""
    bg = None
    if background:
        try:
            bg = ImageOps.fit(Image.open(background).convert("RGB"), (W, H), Image.LANCZOS)
        except OSError:
            bg = None
    bg = (bg or _gradient_bg(W, H)).convert("RGBA")
    tile = render_png(cfg, W, H)
    x, y = position_xy(cfg, W, H, tile.size)
    layer = _blank(W, H)
    layer.paste(tile, (x, y))
    return Image.alpha_composite(bg, layer).convert("RGB")


def _float_exprs(cfg, W: int, H: int, size) -> tuple[str, str]:
    order = ["top-left", "top-right", "bottom-right", "bottom-left"]
    pos = _pos(cfg)
    start = order.index(pos) if pos in order else 1
    order = order[start:] + order[:start]
    pts = [_box_xy(p, W, H, *size) for p in order]
    P = FLOAT_PERIOD
    k = f"floor(mod(t,{4 * P:g})/{P:g})"

    def chain(vals):
        return f"if(eq({k},0),{vals[0]},if(eq({k},1),{vals[1]},if(eq({k},2),{vals[2]},{vals[3]})))"
    return chain([p[0] for p in pts]), chain([p[1] for p in pts])


def ffmpeg_overlay(cfg: WatermarkConfig, W: int, H: int, workdir: Path, input_index: int,
                   in_label: str = "[v]", out_label: str = "[vw]") -> tuple[list[str], str]:
    """Trả về (tham số input thêm cho ffmpeg, đoạn filter_complex).
    Đoạn filter nhận in_label + [input_index:v] và xuất ra out_label."""
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    img = render_png(cfg, W, H)
    info = asdict(cfg)
    info["logo"] = str(cfg.logo or "")
    if cfg.logo and Path(cfg.logo).exists():
        info["logo_mtime"] = Path(cfg.logo).stat().st_mtime
    digest = hashlib.md5(repr((sorted(info.items()), W, H)).encode()).hexdigest()[:10]
    png = workdir / f"watermark_{_key(cfg)}_{W}x{H}_{digest}.png"
    img.save(png)
    args = ["-loop", "1", "-i", str(png)]
    src, wm = f"[{input_index}:v]", f"[wm{input_index}]"
    if _key(cfg) == "floating" and _has_content(cfg):
        xe, ye = _float_exprs(cfg, W, H, img.size)
        P, F = FLOAT_PERIOD, FLOAT_FADE
        fade = f"clip(min(mod(T,{P:g}),{P:g}-mod(T,{P:g}))/{F:g},0,1)"
        frag = (f"{src}format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*{fade}'{wm};"
                f"{in_label}{wm}overlay=x='{xe}':y='{ye}':eval=frame:shortest=1{out_label}")
    else:
        x, y = position_xy(cfg, W, H, img.size)
        frag = f"{src}format=rgba{wm};{in_label}{wm}overlay=x={x}:y={y}:shortest=1{out_label}"
    return args, frag
