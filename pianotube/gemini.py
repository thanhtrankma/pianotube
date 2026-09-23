"""Gọi Gemini API: tạo ảnh nền và viết tiêu đề/mô tả/tag.

Nếu chưa có GEMINI_API_KEY, mọi hàm trả về None để phần còn lại dùng
phương án dự phòng (nền gradient, metadata theo mẫu).
"""
import io
import json

from PIL import Image

from . import config

_client = None


def available() -> bool:
    return bool(config.GEMINI_API_KEY)


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def image(prompt: str, aspect_ratio: str = "16:9") -> Image.Image | None:
    if not available():
        return None
    from google.genai import types
    try:
        resp = client().models.generate_content(
            model=config.GEMINI_IMAGE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
            ),
        )
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
    except Exception as e:  # lỗi mạng/quota không nên làm hỏng cả video
        print(f"  ! Gemini không tạo được ảnh ({e}); dùng nền dự phòng.")
    return None


METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {"type": "string"},
    },
    "required": ["title", "intro", "tags", "hashtags", "thumbnail_text"],
}

SYSTEM = """You write YouTube metadata for a classical piano channel.
Rules:
- English. Warm, honest, no clickbait, no fake claims (never say "live", "performed by", or name a pianist; the audio is a virtual piano rendering of a public-domain score).
- title: under 90 characters, include composer and piece or the mood/use-case.
- intro: 2 short paragraphs for the description. May include one or two true, well-known facts about the piece/composer. No timestamps, no links.
- tags: 12-20 relevant search tags, lowercase.
- hashtags: exactly 3, each starting with #.
- thumbnail_text: 2-5 words, punchy, for big thumbnail text."""


AMBIENT_SCHEMA = {
    "type": "object",
    "properties": {
        **METADATA_SCHEMA["properties"],
        "track_names": {"type": "array", "items": {"type": "string"}},
    },
    "required": METADATA_SCHEMA["required"] + ["track_names"],
}

AMBIENT_SYSTEM = """You write YouTube metadata for a gentle ambient piano channel that offers
emotional comfort: slow, soft, lonely-but-safe, healing music for hard days.
Rules:
- English. Honest: the music is original, algorithmically composed soft piano. Never claim a human pianist or "live".
- title: one short lowercase sentence (4-10 words) that speaks directly to a feeling the listener has
  right now (comforting, second person or first person), then " | piano playlist". Write fresh lines, never reuse famous titles.
- intro: 2-3 short, warm paragraphs addressed to "you". A quiet place to rest, you are not alone.
  End by inviting listeners to share in the comments where this music found them. No links, no timestamps.
- tags: 15-20 search tags, lowercase (e.g. sad piano, emotional piano, healing music, calm piano).
- hashtags: 5, each starting with #.
- track_names: exactly the requested number of short lowercase poetic track names (2-4 words each), all different.
- thumbnail_text: 2-4 lowercase words (optional use)."""


def metadata(brief: str, ambient: bool = False) -> dict | None:
    if not available():
        return None
    from google.genai import types
    try:
        resp = client().models.generate_content(
            model=config.GEMINI_TEXT_MODEL,
            contents=brief,
            config=types.GenerateContentConfig(
                system_instruction=AMBIENT_SYSTEM if ambient else SYSTEM,
                response_mime_type="application/json",
                response_schema=AMBIENT_SCHEMA if ambient else METADATA_SCHEMA,
                temperature=0.8,
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"  ! Gemini không viết được metadata ({e}); dùng mẫu có sẵn.")
    return None
