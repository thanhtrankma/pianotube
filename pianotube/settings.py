"""Cài đặt đăng YouTube, lưu ở data/settings.json."""
import json

from . import config

FILE = config.ROOT / "data" / "settings.json"

DEFAULTS = {
    "slots": "20:00",            # khung giờ đăng mỗi ngày (giờ máy), vd "08:00, 20:00"
    "lead_minutes": 90,          # video phải đăng trước giờ công khai ít nhất bao lâu (để YouTube xử lý)
    "mode": "slot",              # slot = hẹn theo khung giờ | private | unlisted | public
    "synthetic": True,           # khai báo "nội dung do AI tạo" (ảnh nền Gemini trông như ảnh thật)
    "title_note": "",            # ghi chú thêm cho Gemini khi viết tiêu đề/mô tả
    "account": "",
    # Lịch tự động: đến giờ thì làm video ambient hàng loạt và đăng YouTube.
    "auto_enabled": False,
    "auto_times": "14:00",       # giờ chạy mỗi ngày (giờ máy), vd "09:00, 21:00"
    "auto_days": [0, 1, 2, 3, 4, 5, 6],  # 0 = Thứ 2 … 6 = Chủ nhật
    "auto_count": 1,             # số video mỗi lượt
    "auto_minutes": 50,
    "auto_effect": "grain",
    "auto_upload": True,
    "auto_mode": "slot",         # cách đăng: slot | private | unlisted | public
}


def load() -> dict:
    data = dict(DEFAULTS)
    if FILE.exists():
        try:
            data.update(json.loads(FILE.read_text()))
        except json.JSONDecodeError:
            pass
    return data


def save(**kw) -> dict:
    data = load()
    data.update({k: v for k, v in kw.items() if k in DEFAULTS})
    FILE.parent.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data
