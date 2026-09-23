"""MIDI → WAV (FluidSynth), chuẩn hoá âm lượng, nối bài bằng crossfade."""
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
import pretty_midi

from . import config


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(out.strip())


# Tiếng vang phòng lớn cho chất "ambient"; mặc định của FluidSynth khá khô.
SOFT_REVERB = ["-o", "synth.reverb.room-size=0.92", "-o", "synth.reverb.damp=0.35",
               "-o", "synth.reverb.width=1.0", "-o", "synth.reverb.level=0.85"]


def render_midi(midi: Path, wav: Path, soft: bool = False) -> Path:
    """Phát file MIDI bằng soundfont piano, xuất WAV stereo 48 kHz."""
    run(
        ["fluidsynth", "-ni", "-q",
         "-g", "0.6",                   # gain vừa phải, tránh méo tiếng
         "-R", "1", "-C", "0",          # bật reverb, tắt chorus
         *(SOFT_REVERB if soft else []),
         "-r", config.SAMPLE_RATE,
         "-F", wav,
         config.soundfont_path(), midi],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return wav


def soften_midi(src: Path, dst: Path, vel_lo: int = 22, vel_hi: int = 60, tempo: float = 0.9) -> Path:
    """Làm bản nhạc có sẵn (vd. cổ điển) nhẹ nhàng hơn: đánh khẽ tay và chậm lại."""
    pm = pretty_midi.PrettyMIDI(str(src))
    vels = [n.velocity for i in pm.instruments for n in i.notes] or [64]
    v0, v1 = min(vels), max(vels)
    for inst in pm.instruments:
        for n in inst.notes:
            k = (n.velocity - v0) / max(1, v1 - v0)
            n.velocity = int(vel_lo + k * (vel_hi - vel_lo))
    pm.adjust_times([0, pm.get_end_time()], [0, pm.get_end_time() / tempo])
    pm.write(str(dst))
    return dst


def pad(chords: list, seconds: float, dst: Path, level: float = 0.05) -> Path:
    """Lớp pad nền ấm, êm theo đúng hợp âm của bài (tổng hợp bằng sóng sine)."""
    sr = config.SAMPLE_RATE
    n = int(seconds * sr)
    out = np.zeros((n, 2), np.float32)
    fade = int(1.8 * sr)
    for start, end, voice in chords:
        a, b = int(start * sr), min(n, int((end + 1.8) * sr))
        if b <= a:
            continue
        tt = np.arange(b - a) / sr
        env = np.ones(b - a, np.float32)
        k = min(fade, len(env) // 2)
        env[:k] = np.linspace(0, 1, k) ** 2
        env[-k:] = np.linspace(1, 0, k) ** 2
        for p in voice[1:4]:                                  # bỏ nốt bass cho đỡ đục
            f = 440.0 * 2 ** ((p + 12 - 69) / 12)
            for ch, cents in ((0, -4), (1, 4)):               # lệch nhẹ 2 kênh cho rộng
                ff = f * 2 ** (cents / 1200)
                wave_ = np.sin(2 * np.pi * ff * tt) + 0.25 * np.sin(4 * np.pi * ff * tt)
                out[a:b, ch] += (wave_ * env).astype(np.float32)
    peak = np.abs(out).max() or 1
    out = out / peak * level
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((out * 32767).astype("<i2").tobytes())
    return dst


def warm(piano: Path, dst: Path, pad_wav: Path | None = None) -> Path:
    """Làm tiếng đàn ấm và mềm: cắt bớt tần số chói, trộn lớp pad nếu có."""
    tone = "highpass=f=35,lowpass=f=6200,equalizer=f=3200:t=q:w=1.2:g=-3"
    if pad_wav:
        graph = f"[0:a]{tone}[p];[1:a]lowpass=f=2200[q];[p][q]amix=inputs=2:weights=1 1:normalize=0"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", piano, "-i", pad_wav,
             "-filter_complex", graph, dst])
    else:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", piano, "-af", tone, dst])
    return dst


def normalize(src: Path, dst: Path, fade_in: float = 0.0, fade_out: float = 0.0,
              target: float = config.TARGET_LUFS) -> Path:
    """Chuẩn hoá loudness 2 lượt (mặc định -14 LUFS), cắt khoảng lặng cuối, fade tuỳ chọn."""
    # Cắt khoảng lặng cuối bài (FluidSynth thường để dư vài giây im lặng).
    trim = ("areverse,silenceremove=start_periods=1:start_threshold=-60dB"
            ":start_silence=0.8,areverse")
    loud = f"loudnorm=I={target}:TP=-1.5:LRA=11"
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
         "-af", f"{trim},{loud}:print_format=json", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stderr
    m = json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", probe).group(0))
    filters = [
        trim,
        f"{loud}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
        f":offset={m['target_offset']}:linear=true",
        f"aresample={config.SAMPLE_RATE}",
    ]
    if fade_in:
        filters.append(f"afade=t=in:d={fade_in}")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
         "-af", ",".join(filters), "-ac", "2", dst])
    if fade_out:
        d = duration(dst)
        tmp = dst.with_suffix(".fade.wav")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", dst,
             "-af", f"afade=t=out:st={max(0, d - fade_out)}:d={fade_out}", tmp])
        tmp.replace(dst)
    return dst


def concat_crossfade(wavs: list, dst: Path, xfade: float = 3.0) -> list:
    """Nối nhiều bài bằng crossfade. Trả về thời điểm bắt đầu (giây) của từng bài."""
    durs = [duration(w) for w in wavs]
    starts, t = [], 0.0
    for d in durs:
        starts.append(t)
        t += d - xfade
    if len(wavs) == 1:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", wavs[0],
             "-b:a", "192k", dst])
        return starts

    inputs = []
    for w in wavs:
        inputs += ["-i", w]
    chain, prev = [], "[0:a]"
    for i in range(1, len(wavs)):
        out = f"[x{i}]" if i < len(wavs) - 1 else ""
        chain.append(f"{prev}[{i}:a]acrossfade=d={xfade}:c1=tri:c2=tri{out}")
        prev = out
    total = starts[-1] + durs[-1]
    graph = ";".join(chain)
    graph += f",afade=t=in:d=2,afade=t=out:st={max(0, total - 6)}:d=6"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", graph, "-ac", "2", "-b:a", "192k", dst])
    return starts
