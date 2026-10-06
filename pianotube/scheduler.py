"""Lịch tự động: đến giờ đã đặt thì tự làm video ambient hàng loạt và đăng YouTube.

Hai cách kích hoạt, dùng chung lệnh `pianotube autorun` nên không bao giờ chạy trùng
(khoá file + ghi nhận khung giờ đã chạy):
- Trong giao diện: luồng nền kiểm tra mỗi 30 giây khi app đang mở.
- Chạy nền macOS (launchd): chạy đúng giờ kể cả khi đã tắt giao diện; nếu máy đang ngủ
  thì chạy bù ngay khi máy thức dậy.
"""
import fcntl
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, settings

STATE_FILE = config.ROOT / "data" / "autorun.json"
LOCK_FILE = config.ROOT / "data" / "autorun.lock"
LOG_DIR = config.ROOT / "data" / "logs"
LABEL = "com.pianotube.autorun"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DAY_NAMES = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"]
GRACE = timedelta(hours=3)   # lỡ giờ (máy ngủ, app tắt) quá 3 tiếng thì bỏ qua lượt đó


def _key(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def times(s: dict) -> list:
    from .youtube import parse_slots
    return parse_slots(s["auto_times"])


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"done": [], "runs": []}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    state["done"] = state["done"][-200:]
    state["runs"] = state["runs"][-50:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def next_run(s: dict | None = None, now: datetime | None = None) -> datetime | None:
    s = s or settings.load()
    if not s["auto_enabled"] or not times(s) or not s["auto_days"]:
        return None
    now = now or datetime.now()
    for k in range(8):
        d = (now + timedelta(days=k)).date()
        if d.weekday() in s["auto_days"]:
            for h, m in times(s):
                t = datetime(d.year, d.month, d.day, h, m)
                if t > now:
                    return t
    return None


def due_slot(s: dict | None = None, now: datetime | None = None) -> datetime | None:
    """Khung giờ vừa tới (trong vòng GRACE) mà chưa chạy."""
    s = s or settings.load()
    now = now or datetime.now()
    done = set(load_state()["done"])
    due = None
    for k in (1, 0):
        d = (now - timedelta(days=k)).date()
        if d.weekday() not in s["auto_days"]:
            continue
        for h, m in times(s):
            t = datetime(d.year, d.month, d.day, h, m)
            if now - GRACE <= t <= now and _key(t) not in done:
                due = t
    return due


def is_running() -> bool:
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def autorun(force: bool = False):
    """Chạy một lượt nếu tới giờ (hoặc force). Được gọi bởi `pianotube autorun`."""
    import argparse
    from . import cli, youtube

    s = settings.load()
    if not force and not s["auto_enabled"]:
        print("Lịch tự động đang tắt.")
        return
    slot = datetime.now() if force else due_slot(s)
    if not slot:
        print("Chưa tới giờ chạy.")
        return
    LOCK_FILE.parent.mkdir(exist_ok=True)
    lock = open(LOCK_FILE, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Đang có một lượt tự động khác chạy.")
        return

    state = load_state()
    if not force:
        state["done"].append(_key(slot))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"auto_{datetime.now():%Y%m%d_%H%M%S}.log"
    run = {"slot": _key(slot), "manual": force, "started": _key(datetime.now()), "finished": None,
           "ok": None, "log": log_path.name, "uploads": []}
    state["runs"].append(run)
    _save_state(state)
    uploads_before = len(youtube.load_uploads())

    old_out, old_err = sys.stdout, sys.stderr
    with open(log_path, "w") as logf:
        sys.stdout = sys.stderr = _Tee(old_out, logf)
        try:
            print(f"⏰ Lượt tự động {'(chạy tay) ' if force else ''}lúc {run['started']}: "
                  f"{s['auto_count']} video × {s['auto_minutes']} phút", flush=True)
            cli.cmd_batch(argparse.Namespace(
                count=int(s["auto_count"]), minutes=float(s["auto_minutes"]), effect=s["auto_effect"],
                no_ai_image=False, upload=bool(s["auto_upload"]), upload_mode=s["auto_mode"]))
            run["ok"] = True
        except BaseException as e:  # ghi lại cả khi bị dừng giữa chừng
            print(f"✗ Lỗi: {e}", flush=True)
            run["ok"] = False
            if not isinstance(e, Exception):
                raise
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            run["finished"] = _key(datetime.now())
            run["uploads"] = [u["url"] for u in youtube.load_uploads()[uploads_before:]]
            state = load_state()
            state["runs"] = [r for r in state["runs"] if r["log"] != run["log"]] + [run]
            _save_state(state)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


# ---------- kích hoạt ----------

def _python() -> str:
    venv = config.ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def spawn(force: bool = False, env: dict | None = None) -> subprocess.Popen:
    """Chạy `pianotube autorun` thành tiến trình riêng (không bị dừng khi đóng giao diện)."""
    argv = [_python(), "-m", "pianotube", "autorun"] + (["--force"] if force else [])
    return subprocess.Popen(argv, cwd=config.ROOT, env={**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


_thread = {"t": None}


def start_watcher(env_fn=None):
    """Luồng nền trong giao diện: tới giờ thì kích hoạt một lượt."""
    if _thread["t"]:
        return

    def loop():
        while True:
            try:
                s = settings.load()
                if s["auto_enabled"] and due_slot(s) and not is_running():
                    spawn(env=env_fn() if env_fn else None)
            except Exception as e:
                print(f"[lịch tự động] {e}")
            time.sleep(30)
    _thread["t"] = threading.Thread(target=loop, daemon=True)
    _thread["t"].start()


# ---------- launchd (chạy nền khi đã tắt giao diện) ----------

def launchd_installed() -> bool:
    return PLIST.exists()


def _launchctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install_launchd(s: dict | None = None):
    s = s or settings.load()
    intervals = []
    for h, m in times(s):
        if len(set(s["auto_days"])) == 7:
            intervals.append({"Hour": h, "Minute": m})
        else:
            # launchd: 0 = Chủ nhật; Python weekday(): 0 = Thứ 2.
            intervals += [{"Weekday": (d + 1) % 7, "Hour": h, "Minute": m} for d in sorted(s["auto_days"])]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [_python(), "-m", "pianotube", "autorun"],
        "WorkingDirectory": str(config.ROOT),
        "StartCalendarInterval": intervals,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                 "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
    }
    uninstall_launchd()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl lỗi: {r.stderr.strip() or r.stdout.strip()}")


def uninstall_launchd():
    if PLIST.exists():
        _launchctl("bootout", f"gui/{os.getuid()}", str(PLIST))
        PLIST.unlink()


def latest_log() -> str:
    logs = sorted(LOG_DIR.glob("auto_*.log")) if LOG_DIR.exists() else []
    if not logs:
        return ""
    lines = [l for l in logs[-1].read_text(errors="replace").replace("\r", "\n").splitlines()
             if l.strip() and not l.startswith(("frame=", "size="))]
    return "\n".join(lines[-80:])
