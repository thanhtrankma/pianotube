"""Sáng tác nhạc piano ambient nhẹ nhàng (chậm, thưa nốt, nhiều pedal).

Mỗi bài là một bản nhạc mới do thuật toán tạo ra từ seed, nên là nhạc gốc
của bạn: không lo bản quyền, có thể đăng cả lên Spotify.
"""
import random
from dataclasses import dataclass, field

import pretty_midi

MAJOR = [0, 2, 4, 5, 7, 9, 11]
KEYS = [0, 2, 3, 5, 7, 8, 10]           # C, D, Eb, F, G, Ab, Bb: các giọng nghe ấm trên piano

# Vòng hoà âm theo bậc (1 = chủ âm). Nhiều vòng bắt đầu từ bậc 6 cho cảm giác buồn nhẹ.
PROGRESSIONS = [
    [6, 4, 1, 5], [1, 5, 6, 4], [4, 1, 6, 5], [6, 5, 4, 4], [1, 6, 4, 5],
    [4, 5, 3, 6], [6, 4, 5, 1], [1, 4, 6, 5], [2, 5, 1, 6], [1, 3, 4, 4],
]

# Nhịp điệu giai điệu cho 1 ô nhịp 4/4: (phách bắt đầu, độ dài). Cố ý thưa.
RHYTHMS = [
    [(0, 3), (3, 1)], [(0, 4)], [(1, 1), (2, 2)], [(0, 2), (2, 2)],
    [(0, 1.5), (1.5, 0.5), (2, 2)], [(0.5, 1.5), (2, 1), (3, 1)], [(2, 2)], [(0, 2)],
]

# Kiểu đệm tay trái: chỉ số nốt trong hợp âm rải cho 8 nốt móc đơn.
ARPS = [
    [0, 1, 2, 3, 4, 3, 2, 1],
    [0, 1, 2, 3, 2, 3, 4, 3],
    [0, 2, 3, 4, None, 4, 3, 2],
    [0, None, 1, 2, 3, None, 2, None],
]


@dataclass
class Piece:
    midi: pretty_midi.PrettyMIDI
    chords: list = field(default_factory=list)      # [(start, end, [pitch, ...])] dùng cho lớp pad
    duration: float = 0.0
    key_name: str = ""
    bpm: float = 0.0


def _chord_pcs(key: int, degree: int, ext: str) -> list:
    """Các cao độ (pitch class) của hợp âm bậc `degree`, có thể thêm nốt 7/9."""
    i = degree - 1
    idx = [i, i + 2, i + 4]
    if ext == "7":
        idx.append(i + 6)
    elif ext == "9":
        idx.append(i + 8)
    return [(key + MAJOR[j % 7]) % 12 for j in idx]


def _voicing(pcs: list, bass_lo=38, bass_hi=50) -> list:
    """Xếp hợp âm tay trái: nốt gốc trầm rồi các nốt còn lại mở rộng lên trên."""
    root = pcs[0]
    bass = next(p for p in range(bass_lo, bass_hi + 1) if p % 12 == root)
    tones = [bass]
    order = [pcs[2], (pcs[3] if len(pcs) > 3 else pcs[0]), pcs[1], pcs[2]]  # 5, 9/7/8, 3(10), 5
    cur = bass
    for pc in order:
        cur = next(p for p in range(cur + 1, cur + 13) if p % 12 == pc)
        tones.append(cur)
    return tones


def _scale_pitches(key: int, lo: int, hi: int) -> list:
    return [p for p in range(lo, hi + 1) if (p - key) % 12 in MAJOR]


def compose(seed: int, bars_hint: int = 44) -> Piece:
    rng = random.Random(seed)
    key = rng.choice(KEYS)
    bpm = rng.uniform(58, 70)
    beat = 60.0 / bpm
    prog_a = rng.choice(PROGRESSIONS)
    prog_b = rng.choice([p for p in PROGRESSIONS if p != prog_a])
    exts = [rng.choice(["9", "9", "7", ""]) for _ in range(8)]
    arp = rng.choice(ARPS)
    block_intro = rng.random() < 0.5

    # Cấu trúc: intro – A – A – B – A – outro. Mỗi ô nhịp 1 hợp âm.
    sections = [("intro", 4, prog_a), ("A", 8, prog_a), ("A2", 8, prog_a),
                ("B", 8, prog_b), ("A3", 8, prog_a), ("outro", 4, prog_a)]
    if bars_hint > 44:
        sections.insert(4, ("B2", 8, prog_b))

    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    rh = pretty_midi.Instrument(program=0, name="right hand")
    lh = pretty_midi.Instrument(program=0, name="left hand")
    chords = []

    # Giai điệu đoạn A sinh một lần rồi dùng lại (có biến tấu) để bài có "chủ đề".
    melody_scale = _scale_pitches(key, 67, 86)
    a_rhythms = [rng.choice(RHYTHMS) for _ in range(8)]
    a_melody = None

    t = 0.0
    bar_i = 0
    last_pitch = rng.choice(melody_scale[4:10])
    a3_shift = 12 if rng.random() < 0.35 else 0          # đoạn A cuối có thể lên 1 quãng tám

    for name, n_bars, prog in sections:
        section_notes = []
        for b in range(n_bars):
            # Outro chậm dần (ritardando).
            stretch = 1.0 + (0.12 * (b + 1) if name == "outro" else 0.0)
            bar_len = 4 * beat * stretch
            degree = prog[b % len(prog)]
            if name == "outro" and b == n_bars - 1:
                degree = 1
            pcs = _chord_pcs(key, degree, exts[(bar_i) % len(exts)])
            voice = _voicing(pcs)
            chords.append((t, t + bar_len, voice))

            # --- tay trái
            base_vel = 30 if name in ("intro", "outro") else 36
            if name == "B":
                base_vel += 4
            last_bar = name == "outro" and b == n_bars - 1
            if (name == "intro" and block_intro) or last_bar:
                hold = bar_len * (2.5 if last_bar else 1.0)
                for k, p in enumerate(voice[:4]):
                    roll = k * 0.045                               # rải nhẹ như tay người
                    lh.notes.append(pretty_midi.Note(base_vel + rng.randint(-4, 4), p,
                                                     t + roll, t + hold))
            else:
                step = bar_len / 8
                for k, idx in enumerate(arp):
                    if idx is None:
                        continue
                    on = t + k * step + rng.uniform(-0.012, 0.012)
                    vel = base_vel + (5 if k == 0 else 0) + rng.randint(-5, 3)
                    lh.notes.append(pretty_midi.Note(max(12, vel), voice[idx], max(0, on), on + step * 3))

            # --- tay phải (giai điệu)
            if name not in ("intro",) and not last_bar:
                if name in ("A", "A2", "A3") and a_melody is not None and name != "A":
                    bar_notes = a_melody[b]
                    shift = a3_shift if name == "A3" else 0
                    bar_notes = [(o, d, p + shift if p + shift <= 91 else p) for o, d, p in bar_notes]
                else:
                    rhythm = a_rhythms[b] if name.startswith("A") else rng.choice(RHYTHMS)
                    if name == "outro":
                        rhythm = [(0, 4)] if b == 0 else ([(1, 3)] if rng.random() < 0.5 else [])
                    bar_notes = []
                    for j, (o, d) in enumerate(rhythm):
                        chord_tones = [p for p in melody_scale if p % 12 in pcs]
                        if j == 0 or d >= 2:
                            # Nốt dài/đầu ô nhịp: chọn nốt thuộc hợp âm gần nốt trước.
                            cand = sorted(chord_tones, key=lambda p: abs(p - last_pitch))[:3]
                        else:
                            i = melody_scale.index(min(melody_scale, key=lambda p: abs(p - last_pitch)))
                            cand = [melody_scale[max(0, min(len(melody_scale) - 1, i + s))]
                                    for s in (-2, -1, 1, 2)]
                        last_pitch = rng.choice(cand)
                        bar_notes.append((o, d, last_pitch))
                    section_notes.append(bar_notes)

                for o, d, p in bar_notes:
                    on = t + o * beat * stretch + rng.uniform(-0.02, 0.02)
                    vel = 46 + (6 if name.startswith("B") else 0) + rng.randint(-6, 6)
                    rh.notes.append(pretty_midi.Note(vel, p, max(0, on), on + d * beat * stretch * 0.95))
                    # Thỉnh thoảng thêm một nốt cao lấp lánh ở đoạn B.
                    if name.startswith("B") and d >= 2 and rng.random() < 0.25 and p + 12 <= 96:
                        rh.notes.append(pretty_midi.Note(max(20, vel - 16), p + 12,
                                                         on + 0.03, on + d * beat * stretch))

            # --- pedal: nhấn ngay sau đầu ô nhịp, nhả ngay trước ô sau
            lh.control_changes.append(pretty_midi.ControlChange(64, 127, t + 0.03))
            lh.control_changes.append(pretty_midi.ControlChange(64, 0, t + bar_len * (3 if last_bar else 1) - 0.04))
            t += bar_len
            bar_i += 1

        if name == "A":
            a_melody = section_notes

    pm.instruments = [rh, lh]
    names = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    return Piece(pm, chords, t + 4 * beat * 2.5, f"{names[key]} major", bpm)
