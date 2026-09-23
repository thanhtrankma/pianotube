"""Thư viện MIDI: lưu tên bài, nhà soạn nhạc, giấy phép, nguồn để ghi công."""
import json
import random
import re
import shutil
import unicodedata
from pathlib import Path

from . import config


def _load() -> list:
    if config.LIBRARY_FILE.exists():
        return json.loads(config.LIBRARY_FILE.read_text())
    return []


def _save(items: list):
    config.LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LIBRARY_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def all_pieces() -> list:
    return _load()


def add(src: Path, title: str, composer: str, license: str, source: str) -> dict:
    items = _load()
    pid = slug(f"{composer} {title}")
    dst = config.MIDI_DIR / f"{pid}.mid"
    if src.resolve() != dst.resolve():
        shutil.copy(src, dst)
    entry = {"id": pid, "file": dst.name, "title": title, "composer": composer,
             "license": license, "source": source, "used": 0}
    items = [i for i in items if i["id"] != pid] + [entry]
    _save(items)
    return entry


def find(key: str) -> dict:
    """Tìm bài theo id, tên file hoặc đường dẫn."""
    name = Path(key).name
    for item in _load():
        if key in (item["id"], item["file"]) or name == item["file"]:
            return item
    raise SystemExit(f"Không có '{key}' trong thư viện. Thêm bằng: pianotube library add ...")


def path(item: dict) -> Path:
    return config.MIDI_DIR / item["file"]


def mark_used(ids: list):
    items = _load()
    for item in items:
        item["used"] += ids.count(item["id"])
    _save(items)


def ordered_pool(composer: str | None) -> list:
    """Các bài xếp theo số lần đã dùng (ít trước), cùng mức thì xáo ngẫu nhiên."""
    pool = [i for i in _load() if not composer or composer.lower() in i["composer"].lower()]
    if not pool:
        raise SystemExit("Thư viện trống (hoặc không có bài của nhà soạn nhạc này).")
    random.shuffle(pool)
    pool.sort(key=lambda i: i["used"])
    return pool
