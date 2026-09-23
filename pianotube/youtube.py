"""Đăng video lên YouTube (YouTube Data API v3), nhiều tài khoản, hẹn giờ công khai.

Cách làm theo automation-down-up-tool: OAuth Desktop client (credentials/client_secret.json),
token lưu riêng cho từng tài khoản, upload resumable có thử lại, publishAt để hẹn lịch.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

CRED_DIR = config.ROOT / "credentials"
CLIENT_SECRETS = CRED_DIR / "client_secret.json"
TOKENS_DIR = CRED_DIR / "tokens"
ACCOUNTS_FILE = CRED_DIR / "accounts.json"
UPLOADS_FILE = config.ROOT / "data" / "uploads.json"

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPES = [UPLOAD_SCOPE, "openid", "https://www.googleapis.com/auth/userinfo.email"]
MUSIC_CATEGORY = "10"


class UploadLimitExceeded(Exception):
    """Tài khoản đã hết hạn mức upload trong 24 giờ."""


# ---------- tài khoản ----------

def _load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"active": "", "accounts": {}}


def _save_accounts(data: dict):
    CRED_DIR.mkdir(exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def accounts() -> list:
    """[(label, id)] các tài khoản đã đăng nhập."""
    data = _load_accounts()
    return [(v["label"], k) for k, v in data["accounts"].items()]


def active_account() -> str:
    return _load_accounts().get("active", "")


def set_active(account_id: str):
    data = _load_accounts()
    if account_id in data["accounts"]:
        data["active"] = account_id
        _save_accounts(data)


def _token_path(account_id: str) -> Path:
    return TOKENS_DIR / f"{re.sub(r'[^\w-]', '_', account_id)}.json"


def _oauth(log=print):
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_SECRETS.exists():
        raise FileNotFoundError(
            "Chưa có credentials/client_secret.json. Xem mục 'Kết nối YouTube' trong README.")
    log("🌐 Mở trình duyệt để đăng nhập Google…")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if UPLOAD_SCOPE not in set(creds.scopes or []):
        raise PermissionError("Bạn chưa cấp quyền upload YouTube. Đăng nhập lại và tích đủ quyền.")
    return creds


def _identity(creds) -> tuple:
    from googleapiclient.discovery import build
    try:
        info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
        email = (info.get("email") or "").strip()
        if email:
            return email, "email_" + hashlib.sha256(email.encode()).hexdigest()[:12]
    except Exception:
        pass
    acc = "acc_" + hashlib.sha256(str(creds.refresh_token).encode()).hexdigest()[:12]
    return f"Tài khoản {acc[-6:]}", acc


def login(log=print) -> str:
    """Đăng nhập thêm một tài khoản YouTube. Trả về nhãn (email)."""
    creds = _oauth(log)
    label, acc_id = _identity(creds)
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    _token_path(acc_id).write_text(creds.to_json())
    data = _load_accounts()
    data["accounts"][acc_id] = {"label": label}
    data["active"] = acc_id
    _save_accounts(data)
    log(f"✅ Đã đăng nhập: {label}")
    return label


def service(account_id: str | None = None, log=print):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    data = _load_accounts()
    account_id = account_id or data.get("active")
    if not account_id or account_id not in data["accounts"]:
        raise RuntimeError("Chưa đăng nhập tài khoản YouTube nào.")
    path = _token_path(account_id)
    creds = Credentials.from_authorized_user_file(str(path), SCOPES) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            log(f"⚠️ Token hết hạn ({e}), đăng nhập lại…")
            creds = None
    if not creds or not creds.valid:
        creds = _oauth(log)
    path.write_text(creds.to_json())
    log(f"👤 Tài khoản: {data['accounts'][account_id]['label']}")
    return build("youtube", "v3", credentials=creds)


# ---------- lịch đăng ----------

def load_uploads() -> list:
    if UPLOADS_FILE.exists():
        return json.loads(UPLOADS_FILE.read_text())
    return []


def _save_uploads(items: list):
    UPLOADS_FILE.parent.mkdir(exist_ok=True)
    UPLOADS_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def upload_record(folder_name: str) -> dict | None:
    for u in reversed(load_uploads()):
        if u["folder"] == folder_name:
            return u
    return None


def parse_slots(text: str) -> list:
    """'08:00, 20:30' → [(8, 0), (20, 30)]"""
    slots = []
    for part in re.split(r"[,;\s]+", text.strip()):
        m = re.fullmatch(r"(\d{1,2})[:h.](\d{2})", part)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            slots.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(slots))


def next_free_slot(slots_text: str, lead_minutes: int = 60, taken: list | None = None) -> datetime:
    """Khung giờ trống gần nhất (giờ địa phương) chưa có video nào hẹn đăng."""
    slots = parse_slots(slots_text) or [(20, 0)]
    now = datetime.now().astimezone()
    earliest = now + timedelta(minutes=lead_minutes)
    busy = {u["publish_at"] for u in load_uploads() if u.get("publish_at")}
    busy |= set(taken or [])
    day = now.date()
    for _ in range(366):
        for h, m in slots:
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if t >= earliest and to_utc(t) not in busy:
                return t
        day += timedelta(days=1)
    raise RuntimeError("Không tìm được khung giờ trống trong 1 năm tới.")


def to_utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_local(text: str) -> datetime:
    """'2026-09-25 20:00' (giờ máy) → datetime có múi giờ."""
    t = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    return t.replace(tzinfo=datetime.now().astimezone().tzinfo)


# ---------- upload ----------

def upload(folder: Path, privacy: str = "private", publish_at: datetime | None = None,
           synthetic: bool = False, account_id: str | None = None,
           progress=None, log=print) -> dict:
    """Đăng video trong thư mục output. privacy: private | unlisted | public.
    Nếu có publish_at: video để riêng tư và YouTube tự công khai đúng giờ đó."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    meta = json.loads((folder / "metadata.json").read_text())
    title = (folder / "title.txt").read_text().strip()[:100] if (folder / "title.txt").exists() else meta["title"]
    desc = (folder / "description.txt").read_text() if (folder / "description.txt").exists() else meta["description"]
    tags_txt = (folder / "tags.txt").read_text() if (folder / "tags.txt").exists() else ", ".join(meta["tags"])
    tags = [t.strip() for t in tags_txt.split(",") if t.strip()]
    # YouTube giới hạn tổng độ dài tag ~500 ký tự.
    while len(",".join(tags)) > 480:
        tags.pop()

    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": bool(synthetic)}
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = to_utc(publish_at)
    body = {"snippet": {"title": title, "description": desc, "tags": tags,
                        "categoryId": MUSIC_CATEGORY, "defaultLanguage": "en"},
            "status": status}

    yt = service(account_id, log)
    media = MediaFileUpload(str(folder / "video.mp4"), chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    log(f"🚀 Đang tải lên: {title}")
    resp, retries = None, 5
    while resp is None:
        try:
            st, resp = req.next_chunk()
            if st and progress:
                progress(st.progress())
        except HttpError as e:
            if b"uploadLimitExceeded" in (e.content or b""):
                raise UploadLimitExceeded(
                    "YouTube báo hết hạn mức upload trong 24 giờ. Đợi 1 ngày, hoặc xác minh "
                    "số điện thoại kênh tại youtube.com/verify để tăng hạn mức.") from e
            if e.resp.status in (500, 502, 503, 504) and retries:
                retries -= 1
                log(f"⚠️ Lỗi máy chủ YouTube, thử lại… (còn {retries} lần)")
                time.sleep(5)
                continue
            raise
        except OSError as e:
            if retries:
                retries -= 1
                log(f"⚠️ Mất kết nối ({e}), thử lại… (còn {retries} lần)")
                time.sleep(5)
                continue
            raise
    vid = resp["id"]
    log(f"✅ Đã tải lên: https://youtu.be/{vid}")

    thumb = folder / "thumbnail.jpg"
    if thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg")).execute()
            log("🖼 Đã đặt thumbnail.")
        except HttpError as e:
            log(f"⚠️ Chưa đặt được thumbnail (kênh cần xác minh số điện thoại để dùng thumbnail tuỳ chỉnh): "
                f"{e.resp.status}")

    rec = {"folder": folder.name, "video_id": vid, "url": f"https://youtu.be/{vid}", "title": title,
           "privacy": status["privacyStatus"], "publish_at": status.get("publishAt"),
           "publish_local": publish_at.strftime("%Y-%m-%d %H:%M") if publish_at else None,
           "account": _load_accounts()["accounts"].get(account_id or active_account(), {}).get("label", ""),
           "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    items = load_uploads()
    items.append(rec)
    _save_uploads(items)
    return rec
