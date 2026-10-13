"""Đọc truyện bằng CIT Voice Studio (API TTS chạy trên máy, mặc định cổng 8001).

Máy chủ chỉ xử lý một yêu cầu một lúc nên các đoạn được đọc lần lượt. Mỗi đoạn
được lưu đệm trong out_dir/tts_cache để chạy lại thì đọc tiếp chỗ đang dở.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from . import source

BASE_URL = os.getenv("CIT_VOICE_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_VOICE = "Giọng của tôi"
APP_NAME = "CIT Voice Studio"
APP_BUNDLE = Path("/Volumes/CIT Voice Studio/CIT Voice Studio.app")

TEMPERATURE = 0.7
MAX_CHARS = 450
RETRIES = 2

_HOW_TO_OPEN = "Mở ứng dụng CIT Voice Studio rồi thử lại"


class CitError(RuntimeError):
    """Lỗi khi gọi CIT Voice Studio (thông báo đã kèm cách khắc phục)."""


# ------------------------------------------------------------------- API cơ bản ---

def _get(path: str, timeout: float = 5):
    try:
        r = requests.get(f"{BASE_URL}{path}", timeout=timeout)
    except requests.ReadTimeout as e:      # đã kết nối nhưng máy chủ đang đọc giọng (xử lý 1 yêu cầu/lần)
        raise CitBusy("CIT Voice Studio đang bận tạo giọng đọc.") from e
    except requests.RequestException as e:
        raise CitError(f"Không kết nối được CIT Voice Studio ở {BASE_URL} ({e.__class__.__name__}). "
                       f"{_HOW_TO_OPEN}.") from e
    if r.status_code == 503:
        raise CitError("CIT Voice Studio đang tải mô hình, đợi vài giây rồi thử lại.")
    if not r.ok:
        raise CitError(f"CIT Voice Studio trả lỗi {r.status_code} ở {path}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise CitError(f"CIT Voice Studio trả dữ liệu không hợp lệ ở {path}.") from e


class CitBusy(CitError):
    """Máy chủ đang chạy nhưng bận xử lý yêu cầu khác."""


def health(timeout=5) -> dict:
    return _get("/health", timeout)


def voices() -> list[dict]:
    data = _get("/voices", 10)
    return data if isinstance(data, list) else []


def _launch(log=print) -> None:
    log(f"  ▶ Đang mở {APP_NAME}…")
    try:
        r = subprocess.run(["open", "-a", APP_NAME], capture_output=True, timeout=30)
        if r.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    if APP_BUNDLE.exists():
        try:
            r = subprocess.run(["open", str(APP_BUNDLE)], capture_output=True, timeout=30)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    raise CitError(f"Không mở được {APP_NAME}. Hãy cài đặt hoặc gắn (mount) file DMG của ứng dụng, "
                   f"mở nó bằng tay rồi thử lại.")


def ensure_running(launch: bool = True, timeout: float = 180, log=print) -> dict:
    """Đảm bảo máy chủ TTS sẵn sàng (status "ok"); tự mở ứng dụng nếu cần."""
    try:
        h = health()
        if h.get("status") == "ok":
            return h
    except CitError:
        if not launch:
            raise
        _launch(log)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            h = health()
            if h.get("status") == "ok":
                log(f"  ✓ CIT Voice Studio sẵn sàng ({h.get('engine', '?')}, {h.get('sample_rate', '?')} Hz)")
                return h
            last = h.get("message") or h.get("status") or ""
        except CitError as e:
            last = str(e)
        time.sleep(2)
    raise CitError(f"CIT Voice Studio chưa sẵn sàng sau {int(timeout)} giây ({last}). {_HOW_TO_OPEN}.")


# --------------------------------------------------------------------- đọc truyện ---

@dataclass
class Narration:
    wav: Path
    srt: Path
    cues: list[dict]   # {"start","end","text"} — giây, trên dòng thời gian toàn truyện
    duration: float


def _cache_key(voice: str, speed: float, text: str) -> str:
    return hashlib.sha1(f"{voice}|{speed}|{text}".encode("utf-8")).hexdigest()


def _tts(text: str, voice: str, speed: float, log=print) -> dict:
    payload = {"text": text, "voice_id": voice, "speed": speed, "temperature": TEMPERATURE,
               "format": "wav", "use_pronunciation_dict": True}
    timeout = max(180.0, len(text) * 2.0)
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(f"{BASE_URL}/api/tts/generate-with-subtitles", json=payload, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt >= RETRIES:
                raise CitError(f"Mất kết nối với CIT Voice Studio khi đọc ({e.__class__.__name__}). "
                               f"{_HOW_TO_OPEN} — chạy lại sẽ đọc tiếp chỗ đang dở.") from e
            log(f"  ⚠ Lỗi kết nối, thử lại ({attempt + 1}/{RETRIES})…")
            ensure_running(launch=True, log=log)
            continue
        if r.status_code == 503 and attempt < RETRIES:
            log("  ⚠ Máy chủ đang tải mô hình, đợi…")
            ensure_running(launch=False, log=log)
            continue
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise CitError(f"CIT Voice Studio trả lỗi {r.status_code}: {str(detail)[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise CitError("CIT Voice Studio trả dữ liệu không hợp lệ.") from e
    raise CitError(f"Không đọc được đoạn văn. {_HOW_TO_OPEN}.")


def _read_wav(data: bytes) -> tuple[np.ndarray, int, int, int]:
    """(mẫu int16/…, sample_rate, số kênh, sampwidth)."""
    with wave.open(io.BytesIO(data)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw)
    if dtype is None:
        raise CitError(f"Định dạng WAV không hỗ trợ (sampwidth={sw}).")
    return np.frombuffer(frames, dtype=dtype).reshape(-1, ch), sr, ch, sw


def _ts(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[dict], path: Path) -> None:
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_ts(c['start'])} --> {_ts(c['end'])}", c["text"].strip(), ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def narrate(text: str, out_dir: Path, voice: str = DEFAULT_VOICE, speed: float = 1.0,
            pause: float = 0.35, progress=None, log=print) -> Narration:
    """Đọc toàn bộ text → out_dir/narration.wav, narration.srt, cues.json."""
    out_dir = Path(out_dir)
    cache = out_dir / "tts_cache"
    cache.mkdir(parents=True, exist_ok=True)

    spans = source.chunk_spans(text, MAX_CHARS)
    if not spans:
        raise CitError("Không có nội dung để đọc (văn bản rỗng).")

    ensure_running(launch=True, log=log)
    names = [v.get("id") or v.get("name") for v in voices()]
    if voice not in names:
        raise CitError(f"Không có giọng '{voice}' trong CIT Voice Studio. Các giọng hiện có: "
                       f"{', '.join(n for n in names if n)}. Hãy tạo/lưu giọng đó trong tab "
                       f"\"Nhân bản giọng\" hoặc chọn giọng khác.")

    # Trong một đoạn gộp nhiều đoạn văn: chèn thẻ nghỉ để ngắt đoạn rõ hơn
    # (thẻ nghỉ thay hẳn khoảng lặng ~0.5s máy chủ tự chèn giữa các câu, nên lấy 2×pause)
    para_tag = f" [nghỉ {2 * pause:g}s] " if pause > 0 else " "
    texts = [c.replace("\n\n", para_tag) for c, _ in spans]

    total = len(texts)
    total_chars = sum(len(t) for t in texts)
    done_chars = 0
    gen_chars = 0
    gen_elapsed = 0.0
    log(f"  🎙 Đọc {total} đoạn bằng giọng '{voice}' (tốc độ {speed:g})")

    results: list[tuple[Path, list[dict]]] = []
    for i, t in enumerate(texts, 1):
        key = _cache_key(voice, speed, t)
        wav_p, json_p = cache / f"{key}.wav", cache / f"{key}.json"
        cached = wav_p.is_file() and json_p.is_file()
        if cached:
            try:
                meta = json.loads(json_p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = False
        if not cached:
            t0 = time.monotonic()
            res = _tts(t, voice, speed, log=log)
            dt = time.monotonic() - t0
            audio = base64.b64decode(res.get("audio_base64") or "")
            if not audio:
                raise CitError("CIT Voice Studio trả audio rỗng.")
            _, sr, _, _ = _read_wav(audio)
            meta = {"cues": res.get("cues") or [], "duration": res.get("duration_seconds"),
                    "sample_rate": sr, "rtf": res.get("rtf"), "text": t}
            tmp = wav_p.with_suffix(".wav.part")
            tmp.write_bytes(audio)
            tmp.replace(wav_p)
            json_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            gen_chars += len(t)
            gen_elapsed += dt
        results.append((wav_p, meta.get("cues") or []))
        done_chars += len(t)

        msg = f"  🎙 {i}/{total}"
        if cached:
            msg += " (đã có sẵn)"
        else:
            rtf = meta.get("rtf")
            msg += f" · RTF {rtf:.2f}" if isinstance(rtf, (int, float)) else ""
            if gen_chars:
                eta = gen_elapsed / gen_chars * (total_chars - done_chars)
                msg += f" · còn ~{_fmt_dur(eta)}"
        log(msg)
        if progress:
            progress(done_chars / total_chars, f"Đọc đoạn {i}/{total}")

    # Ghép các đoạn + khoảng lặng, dời mốc phụ đề sang dòng thời gian chung
    pieces: list[np.ndarray] = []
    all_cues: list[dict] = []
    sr0 = ch0 = sw0 = None
    pos = 0  # số mẫu đã ghép
    for idx, ((wav_p, cues), (_, para_end)) in enumerate(zip(results, spans)):
        samples, sr, ch, sw = _read_wav(wav_p.read_bytes())
        if sr0 is None:
            sr0, ch0, sw0 = sr, ch, sw
        elif (sr, ch, sw) != (sr0, ch0, sw0):
            raise CitError(f"Các đoạn audio khác định dạng ({sr} Hz/{ch} kênh so với {sr0} Hz/{ch0} kênh) — "
                           f"có thể đã đổi mô hình giữa chừng. Xoá thư mục {cache} rồi chạy lại.")
        off = pos / sr0
        for c in cues:
            txt = str(c.get("text", "")).strip()
            if not txt:
                continue
            all_cues.append({"start": round(off + float(c["start"]), 3),
                             "end": round(off + float(c["end"]), 3), "text": txt})
        pieces.append(samples)
        pos += len(samples)
        if idx < len(results) - 1:
            gap = pause * (2 if para_end else 1)
            n = int(round(gap * sr0))
            if n > 0:
                silence = np.full((n, ch0), 128 if sw0 == 1 else 0, dtype=samples.dtype)
                pieces.append(silence)
                pos += n

    audio = np.concatenate(pieces, axis=0)
    wav_out = out_dir / "narration.wav"
    with wave.open(str(wav_out), "wb") as w:
        w.setnchannels(ch0)
        w.setsampwidth(sw0)
        w.setframerate(sr0)
        w.writeframes(audio.tobytes())
    duration = len(audio) / sr0

    srt_out = out_dir / "narration.srt"
    write_srt(all_cues, srt_out)
    (out_dir / "cues.json").write_text(json.dumps(all_cues, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  ✓ Giọng đọc xong: {_fmt_dur(duration)} · {len(all_cues)} câu phụ đề → {wav_out.name}")
    return Narration(wav=wav_out, srt=srt_out, cues=all_cues, duration=duration)
