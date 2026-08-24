#!/usr/bin/env python3
"""lynx-update: self-updater for lynxde.

Watches the lynxDE GitHub repository for newer commits than the
recorded installed version. When an update is found it downloads the
zipball into ~/Documents/lynxde/updates/, extracts it there and runs
its install.sh — which replaces every component and restarts the live
session pieces — then records the new version.

Modes:
  --daemon   session mode: wait out a startup delay, then re-check
             every 'update_interval_h' hours (default)
  --now      check immediately and update if one is available
  --status   print the recorded version and recent activity

Pure standard library: no Qt, no network beyond api.github.com and the
repository zipball.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPO = "eotter-beep/lynxDE"
API_LATEST = f"https://api.github.com/repos/{REPO}/commits/main"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
UA = "lynxde-updater/1.0"

SETTINGS_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "lynxde")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
VERSION_PATH = os.path.join(SETTINGS_DIR, "version")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "lynxde")
LOG_PATH = os.path.join(CACHE_DIR, "update.log")
LOCK_PATH = f"/tmp/lynx-updater-{os.getuid()}.lock"


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def documents_dir() -> str:
    try:
        r = subprocess.run(["xdg-user-dir", "DOCUMENTS"],
                           capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        if p and os.path.isdir(p):
            return p
    except (OSError, subprocess.SubprocessError):
        pass
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return docs if os.path.isdir(docs) else home


def update_dir() -> str:
    return os.path.join(documents_dir(), "lynxde", "updates")


def current_version() -> str:
    try:
        with open(VERSION_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def log(msg: str):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(msg: str):
    try:
        subprocess.Popen(["notify-send", "-a", "lynxde", "Lynx Update", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


# ------------------------------ network --------------------------------------

def _fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/vnd.github+json"
        if url == API_LATEST else "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def remote_version() -> str | None:
    """Latest commit sha of the repository's main branch."""
    try:
        data = json.loads(_fetch(API_LATEST, timeout=15).decode())
        return data.get("sha") or None
    except (OSError, ValueError) as e:
        log(f"check failed: {e}")
        return None


def download_zip(dest: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        data = _fetch(ZIP_URL, timeout=120)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True
    except OSError as e:
        log(f"download failed: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def safe_extract(zpath: str, dest: str) -> bool:
    try:
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(dest, filter="data")
            return True
        except TypeError:  # pre-3.12 Python: no filter kwarg
            with zipfile.ZipFile(zpath) as z:
                for name in z.namelist():
                    norm = os.path.normpath(name)
                    if norm.startswith("..") or os.path.isabs(norm):
                        raise ValueError(f"unsafe zip entry: {name}")
                z.extractall(dest)
            return True
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        log(f"extract failed: {e}")
        return False


# ------------------------------ flow -----------------------------------------

def run_install(src_dir: str) -> bool:
    script = os.path.join(src_dir, "install.sh")
    if not os.path.isfile(script):
        log(f"no install.sh inside {src_dir}")
        return False
    log("running install.sh …")
    r = subprocess.run(["bash", "install.sh"], cwd=src_dir,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=900)
    tail = "\n".join((r.stdout or "").splitlines()[-25:])
    log(f"install.sh exit {r.returncode}\n{tail}")
    return r.returncode == 0


def record_version(sha: str):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = VERSION_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(sha + "\n")
    os.replace(tmp, VERSION_PATH)


def check_once(force: bool = False) -> bool:
    """Update if a newer commit exists. Returns True when updated."""
    if not force and not load_settings().get("auto_update", True):
        return False
    sha = remote_version()
    if not sha:
        return False
    local = current_version()
    if not force and local == sha:
        return False
    udir = update_dir()
    os.makedirs(udir, exist_ok=True)
    log(f"update available: {local or '(none)'} -> {sha[:10]}")
    notify("Downloading update…")
    zpath = os.path.join(udir, "lynxde-main.zip")
    if not download_zip(zpath):
        notify("Update download failed — see ~/.cache/lynxde/update.log")
        return False
    src = tempfile.mkdtemp(prefix="lynxde-", dir=udir)
    try:
        if not safe_extract(zpath, src):
            notify("Update failed to extract — see update.log")
            return False
        inner = os.path.join(src, os.listdir(src)[0]) if os.listdir(src) \
            else src
        if not run_install(inner):
            notify("Update install failed — see update.log")
            return False
        record_version(sha)
        log(f"updated to {sha[:10]}")
        notify("lynxde updated — components restarted.")
        return True
    finally:
        shutil.rmtree(src, ignore_errors=True)


def acquire_lock() -> int | None:
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def daemon() -> int:
    if acquire_lock() is None:
        log("another updater instance is active; exiting")
        return 0
    delay = float(os.environ.get("LYNX_UPDATE_DELAY", "180"))
    log(f"updater started (first check in {delay:g}s)")
    time.sleep(delay)
    while True:
        st = load_settings()
        interval_h = st.get("update_interval_h", 24)
        try:
            interval_h = max(1.0, float(interval_h))
        except (TypeError, ValueError):
            interval_h = 24.0
        try:
            check_once()
        except Exception as e:  # never die on a single bad cycle
            log(f"unexpected error: {e}")
        time.sleep(interval_h * 3600)


def status() -> int:
    st = load_settings()
    v = current_version()
    print(f"version: {v[:10] + '…' if len(v) > 10 else (v or '(not recorded)')}")
    print(f"auto update: {'on' if st.get('auto_update', True) else 'off'} "
          f"(every {st.get('update_interval_h', 24)}h)")
    print(f"updates folder: {update_dir()}")
    try:
        with open(LOG_PATH) as f:
            lines = f.read().splitlines()[-5:]
        for l in lines:
            print("  " + l)
    except OSError:
        pass
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    if "--status" in args:
        return status()
    if "--now" in args:
        if acquire_lock() is None:
            log("--now skipped: updater busy")
            return 0
        return 0 if check_once() else 0
    return daemon()


if __name__ == "__main__":
    raise SystemExit(main())
