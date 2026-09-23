"""Cấu hình chung. Có thể ghi đè bằng biến môi trường hoặc file .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MIDI_DIR = ROOT / "midi"
LIBRARY_FILE = MIDI_DIR / "library.json"
OUTPUT_DIR = ROOT / "output"
SOUNDFONT_DIR = ROOT / "assets" / "soundfonts"

WIDTH, HEIGHT = 1920, 1080
FPS = 30
SAMPLE_RATE = 48000
TARGET_LUFS = -14.0

VIDEO_ENCODER = os.getenv("PIANOTUBE_ENCODER", "h264_videotoolbox")
VIDEO_BITRATE = os.getenv("PIANOTUBE_BITRATE", "10M")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

SOUNDFONT_CREDIT = (
    "Piano sound: Salamander Grand Piano by Alexander Holm "
    "(CC BY 3.0), SF2 conversion by FreePats."
)

FONT_SERIF = "/System/Library/Fonts/Supplemental/Georgia.ttf"
FONT_SERIF_BOLD = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
FONT_SERIF_ITALIC = "/System/Library/Fonts/Supplemental/Georgia Italic.ttf"


def soundfont_path() -> Path:
    env = os.getenv("PIANOTUBE_SOUNDFONT")
    if env:
        return Path(env)
    found = sorted(SOUNDFONT_DIR.rglob("*.sf2"))
    if not found:
        raise SystemExit(
            f"Không tìm thấy soundfont .sf2 trong {SOUNDFONT_DIR}. "
            "Chạy scripts/setup_assets.sh để tải Salamander Grand Piano."
        )
    return found[0]
