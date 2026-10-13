"""Đọc truyện từ công cụ viết truyện (thư mục write-story/stories/<slug>/).

Mỗi truyện gồm:
  00-dan-y.md          dàn ý (dòng đầu "# Dàn ý: <Tên>", tên được chọn đánh dấu "(chọn)")
  01-phan-1.txt …      lời kể sẵn sàng cho TTS, đoạn cách nhau một dòng trống
  05-goi-xuat-ban.md   gói xuất bản (tiêu đề YouTube, mô tả, hashtag, hook thumbnail)
  full.txt             toàn bộ truyện (dự phòng khi thiếu các phần)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

STORY_DIR = Path(os.getenv("PIANOTUBE_STORY_DIR")
                 or "/Users/admin/Documents/write-story/stories").expanduser()

PART_GLOB = "[0-9][0-9]-phan-*.txt"


@dataclass
class Story:
    slug: str
    path: Path
    title: str
    yt_titles: list[str] = field(default_factory=list)
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    parts: list[Path] = field(default_factory=list)
    words: int = 0


# ---------------------------------------------------------------- markdown ---

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+•])\s+(.*)$")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError):
        return ""


def _strip_inline_md(s: str) -> str:
    """Bỏ **đậm**, *nghiêng*, `code`, [link](url) — giữ chữ."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = s.replace("`", "")
    return s.strip()


def _sections(md: str) -> tuple[str, dict[str, list[str]]]:
    """Trả về (tiêu đề H1, {tiêu đề mục (chữ thường): các dòng nội dung})."""
    h1 = ""
    secs: dict[str, list[str]] = {}
    cur: list[str] | None = None
    for line in md.split("\n"):
        m = _HEADING.match(line)
        if m:
            level, text = len(m.group(1)), _strip_inline_md(m.group(2))
            if level == 1 and not h1:
                h1 = text
                cur = None
                continue
            cur = secs.setdefault(text.lower(), [])
            continue
        if cur is not None:
            cur.append(line)
    return h1, secs


def _find(secs: dict[str, list[str]], *keys: str) -> list[str]:
    """Mục đầu tiên có tên chứa một trong các từ khoá (vd "tiêu đề youtube")."""
    for k in keys:
        for name, lines in secs.items():
            if k in name:
                return lines
    return []


def _list_items(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        m = _LIST_ITEM.match(line)
        if m:
            item = _strip_inline_md(m.group(1))
            if item:
                out.append(item)
    return out


def _paragraph(lines: list[str]) -> str:
    text = "\n".join(lines).strip()
    paras = [_strip_inline_md(" ".join(p.split())) for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "\n\n".join(paras)


def _title_after_colon(h1: str) -> str:
    # "# Gói xuất bản: Tên" / "# Dàn ý: Tên" → "Tên" (tên có thể chứa dấu ":")
    m = re.match(r"^\s*(?:gói xuất bản|dàn ý)\s*:\s*(.+)$", h1, re.IGNORECASE)
    return m.group(1).strip() if m else h1.strip()


# ------------------------------------------------------------------ đọc truyện ---

def _parse_outline(p: Path) -> tuple[str, str]:
    """(tên được chọn, logline) từ 00-dan-y.md."""
    md = _read(p)
    if not md:
        return "", ""
    h1, secs = _sections(md)
    chosen = ""
    for line in _find(secs, "tên kịch bản", "tên truyện", "tiêu đề"):
        if "(chọn)" in line.lower():
            m = _LIST_ITEM.match(line)
            chosen = _strip_inline_md((m.group(1) if m else line).replace("(chọn)", "").replace("(Chọn)", ""))
            break
    if not chosen:
        chosen = _title_after_colon(h1)
    logline = ""
    m = re.search(r"\*\*\s*Logline[^*]*\*\*\s*(.+)", md)
    if m:
        logline = _strip_inline_md(m.group(1))
    return chosen, logline


def _story_dir(slug_or_path: str | Path) -> Path:
    p = Path(slug_or_path).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = STORY_DIR / str(slug_or_path)
    if cand.is_dir():
        return cand.resolve()
    raise FileNotFoundError(f"Không tìm thấy truyện '{slug_or_path}' (thư mục truyện: {STORY_DIR})")


def load(slug_or_path: str | Path) -> Story:
    d = _story_dir(slug_or_path)
    slug = d.name

    pub_title = ""
    yt_titles: list[str] = []
    description = ""
    hashtags: list[str] = []
    hooks: list[str] = []
    pub = _read(d / "05-goi-xuat-ban.md")
    if pub:
        h1, secs = _sections(pub)
        pub_title = _title_after_colon(h1)
        yt_titles = _list_items(_find(secs, "tiêu đề youtube", "tiêu đề"))
        description = _paragraph(_find(secs, "mô tả"))
        tag_lines = _find(secs, "hashtag")
        hashtags = list(dict.fromkeys(re.findall(r"#[\w\-]+", " ".join(tag_lines))))
        hooks = _list_items(_find(secs, "hook"))

    chosen, logline = _parse_outline(d / "00-dan-y.md")
    if not description and logline:
        description = logline

    story = Story(
        slug=slug, path=d,
        title=pub_title or chosen or slug,
        yt_titles=yt_titles, description=description,
        hashtags=hashtags, hooks=hooks,
        parts=sorted(d.glob(PART_GLOB)),
    )
    story.words = len(story_text(story).split())
    return story


def _mtime(d: Path) -> float:
    try:
        return max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir() if f.is_file()])
    except OSError:
        return 0.0


def list_stories(root: Path | None = None) -> list[Story]:
    """Các truyện có lời kể (phần 01…/full.txt), mới sửa nhất lên đầu."""
    root = Path(root).expanduser() if root else STORY_DIR
    if not root.is_dir():
        return []
    dirs = [d for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and (any(d.glob(PART_GLOB)) or (d / "full.txt").is_file())]
    dirs.sort(key=_mtime, reverse=True)
    out = []
    for d in dirs:
        try:
            out.append(load(d))
        except Exception:  # truyện hỏng không làm hỏng cả danh sách
            continue
    return out


# --------------------------------------------------------------- lời kể ---

_SEPARATOR = re.compile(r"^\s*(?:[-=_*~#•·]\s*){3,}$")


def _clean_narration(raw: str) -> str:
    """Bỏ tiêu đề/định dạng markdown, chuẩn hoá đoạn (cách nhau 1 dòng trống)."""
    paras: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            p = " ".join(" ".join(buf).split())
            if p:
                paras.append(p)
            buf.clear()

    for line in raw.split("\n"):
        s = line.strip()
        if not s or _SEPARATOR.match(s):
            flush()
            continue
        if _HEADING.match(s):  # "# Phần 1" … không đọc thành tiếng
            flush()
            continue
        s = re.sub(r"^>\s?", "", s)
        buf.append(_strip_inline_md(s))
    flush()
    return "\n\n".join(paras)


def story_text(story: Story) -> str:
    """Toàn bộ lời kể: các phần nối bằng dòng trống (thiếu phần thì dùng full.txt)."""
    texts = [t for t in (_clean_narration(_read(p)) for p in story.parts) if t]
    if not texts:
        full = _clean_narration(_read(story.path / "full.txt"))
        return full
    return "\n\n".join(texts)


# ---------------------------------------------------------------- chia đoạn ---

# Hết câu: . ! ? … (có thể kèm dấu đóng ngoặc/nháy) rồi khoảng trắng
_SENT_END = re.compile(r"(?<=[.!?…])[\"'”’»)\]]*\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:—–])\s+")


def _split_at(text: str, pattern: re.Pattern) -> list[str]:
    out, last = [], 0
    for m in pattern.finditer(text):
        piece = text[last:m.end()].strip()
        if piece:
            out.append(piece)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        out.append(tail)
    return out


def _pack(pieces: list[str], max_chars: int) -> list[str]:
    """Gộp các mảnh liền nhau (nối bằng dấu cách) sao cho không vượt max_chars."""
    out: list[str] = []
    cur = ""
    for p in pieces:
        if not cur:
            cur = p
        elif len(cur) + 1 + len(p) <= max_chars:
            cur += " " + p
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def _split_words(text: str, max_chars: int) -> list[str]:
    return _pack(text.split(), max_chars)  # một "từ" dài hơn max_chars vẫn giữ nguyên


def _split_long(para: str, max_chars: int) -> list[str]:
    """Chia đoạn dài tại cuối câu; câu quá dài thì tại dấu phẩy, cuối cùng tại khoảng trắng."""
    pieces: list[str] = []
    for sent in _split_at(para, _SENT_END):
        if len(sent) <= max_chars:
            pieces.append(sent)
            continue
        for clause in _split_at(sent, _CLAUSE_END):
            if len(clause) <= max_chars:
                pieces.append(clause)
            else:
                pieces.extend(_split_words(clause, max_chars))
    return _pack(pieces, max_chars)


def chunk_spans(text: str, max_chars: int = 450) -> list[tuple[str, bool]]:
    """Như chunks() nhưng kèm cờ "đoạn này kết thúc một đoạn văn" (để nghỉ lâu hơn).

    Các đoạn văn ngắn liền nhau được gộp, nối bằng "\\n\\n".
    """
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text or "")]
    paras = [p for p in paras if p]
    out: list[tuple[str, bool]] = []
    cur = ""
    for para in paras:
        if len(para) > max_chars:
            if cur:
                out.append((cur, True))
                cur = ""
            pieces = _split_long(para, max_chars)
            out.extend((p, i == len(pieces) - 1) for i, p in enumerate(pieces))
            continue
        if cur and len(cur) + 2 + len(para) <= max_chars:
            cur += "\n\n" + para
        else:
            if cur:
                out.append((cur, True))
            cur = para
    if cur:
        out.append((cur, True))
    return [(c, end) for c, end in out if c.strip()]


def chunks(text: str, max_chars: int = 450) -> list[str]:
    """Chia lời kể thành các đoạn ≤ max_chars để gửi TTS (không cắt giữa từ, không rỗng)."""
    return [c for c, _ in chunk_spans(text, max_chars)]
