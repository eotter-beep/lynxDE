#!/usr/bin/env python3
"""lynx-wallpaper: MP4/GIF wallpapers on the Wayland background layer.

Renders via QtMultimedia inside our own zwlr-layer-shell surfaces.
NOTE: the layer surface is mapped FIRST; the QVideoWidget is only
installed after compositor exposure (video-before-map deadlocks mapping).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

SETTINGS_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "lynxde")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
VIDEO_EXTS = {".mp4", ".gif", ".mkv", ".webm", ".mov", ".avi", ".apng"}
DAEMON_PATTERN = "[l]ynx_wallpaper.py"


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def save_settings(st: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def daemon_pids() -> list[int]:
    r = subprocess.run(["pgrep", "-f", DAEMON_PATTERN], capture_output=True, text=True)
    me = os.getpid()
    out = []
    for tok in r.stdout.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid != me:
            out.append(pid)
    return out


def daemon_running() -> bool:
    return bool(daemon_pids())


def kill_daemon():
    for pid in daemon_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(30):
        if not daemon_running():
            return
        time.sleep(0.1)


def spawn_daemon():
    if daemon_running():
        return
    py = os.environ.get("LYNX_PYTHON") or sys.executable
    subprocess.Popen([py, os.path.abspath(__file__), "run"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


# ------------------------------ Qt daemon ------------------------------------

def run_daemon() -> int:
    from PySide6.QtCore import QLockFile, QTimer, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication, QWidget

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hypr_common import (  # noqa: E402
        ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT, ANCHOR_TOP,
        LAYER_BACKGROUND, SettingsWatcher, attach_layershell,
        get_scheme, maybe_enable_layer_shell,
    )
    from lynx_blur import publish_backdrop  # noqa: E402
    from lynx_widgets import DesktopCanvas  # noqa: E402

    layer_ok = maybe_enable_layer_shell()
    argv = list(sys.argv) + ["run"]
    if layer_ok:
        argv += ["-platform", "wayland"]
    app = QApplication(argv)
    app.setApplicationName("lynx-wallpaper")
    app.setDesktopFileName("lynx-wallpaper")

    lock = QLockFile(f"/tmp/lynx-wallpaper-{os.getuid()}.lock")
    if not lock.tryLock(0):
        print("another lynx-wallpaper is running", file=sys.stderr)
        return 0

    # Shared publish state: set on every decoded frame, drained by a pump.
    pub_state = {"dirty": True}

    class ScreenWall(QWidget):
        """Bare background-layer surface; gains a video child once exposed."""

        def __init__(self, screen, get_path):
            QWidget.__init__(self)
            self.screen_name = screen.name()
            self.get_path = get_path
            self.pending_path = ""
            self.player = None
            self.audio = None
            self.sink = None
            self._frame = None
            self.dim = 0
            self.setWindowFlags(self.windowFlags()
                                | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.WindowDoesNotAcceptFocus
                                | Qt.WindowType.Tool)
            self.setWindowTitle("lynx-wallpaper")
            self.setScreen(screen)
            self.winId()
            st = attach_layershell(
                self,
                anchors=ANCHOR_TOP | ANCHOR_BOTTOM | ANCHOR_LEFT | ANCHOR_RIGHT,
                exclusive_zone=-1, layer=LAYER_BACKGROUND)
            print(f"lynx-wallpaper[{self.screen_name}]: attach {st}", file=sys.stderr)
            self.show()
            self.desktop = DesktopCanvas(self)
            self.desktop.setGeometry(self.rect())
            self.desktop.show()
            self._poll = QTimer(self)
            self._poll.setInterval(120)
            self._poll.timeout.connect(self._check_exposed)
            self._poll.start()

        def resizeEvent(self, ev):
            if getattr(self, "desktop", None) is not None:
                self.desktop.setGeometry(self.rect())

        def _check_exposed(self):
            handle = self.windowHandle()
            if handle is not None and handle.isExposed():
                self._poll.stop()
                QTimer.singleShot(50, self._install_video)

        def _install_video(self):
            if self.player is not None or not self.windowHandle().isExposed():
                return
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

            self.player = QMediaPlayer(self)
            self.player.setLoops(-1)
            self.audio = QAudioOutput()
            self.audio.setMuted(not bool(load_settings().get("wall_audio", False)))
            self.player.setAudioOutput(self.audio)
            self.sink = QVideoSink(self)
            self.player.setVideoSink(self.sink)
            self.sink.videoFrameChanged.connect(self._on_frame)
            self.player.mediaStatusChanged.connect(self._on_status)
            path = self.pending_path or self.get_path()
            if path:
                self.set_file(path)

        def _on_frame(self, frame):
            if frame.isValid():
                self._frame = frame.toImage()
                pub_state["dirty"] = True   # stream it to lynxBlur consumers
                self.update()

        def _on_status(self, status):
            from PySide6.QtMultimedia import QMediaPlayer

            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
                self.player.play()

        def paintEvent(self, ev):
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QColor, QPainter

            p = QPainter(self)
            p.fillRect(self.rect(), QColor("#111119"))
            img = self._frame
            if img is None or img.isNull():
                return
            w, h = self.width(), self.height()
            iw, ih = img.width(), img.height()
            scale = max(w / iw, h / ih)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QRectF((w - iw * scale) / 2.0, (h - ih * scale) / 2.0,
                               iw * scale, ih * scale), img)
            dim = int(getattr(self, "dim", 0) or 0)
            if dim > 0:
                p.fillRect(self.rect(), QColor(0, 0, 0, round(dim * 2.55)))

        def set_file(self, path: str):
            if not path:
                self.stop_video()
                return
            self.pending_path = path
            if self.player is None:
                return
            from PySide6.QtCore import QUrl

            url = QUrl.fromLocalFile(path)
            if self.player.source() != url:
                self.player.setSource(url)
            if self.player.playbackState() != self.player.PlaybackState.PlayingState:
                self.player.play()

        def stop_video(self):
            self.pending_path = ""
            self._frame = None
            if self.player is not None:
                self.player.stop()
            self.update()

        def close(self):
            if self.player is not None:
                self.player.stop()
            QWidget.close(self)

    walls: dict[str, ScreenWall] = {}

    def current_file() -> str:
        st = load_settings()
        if not st.get("wall_enabled", True):
            return ""
        return os.path.expanduser(st.get("wall_path") or "")

    def rebuild_surfaces():
        for w in walls.values():
            w.close()
        walls.clear()
        for screen in QGuiApplication.screens():
            walls[screen.name()] = ScreenWall(screen, current_file)

    def apply_state():
        st = load_settings()
        path = current_file()
        scheme = get_scheme()
        widget_states = st.get("widgets", {})
        dim = max(0, min(80, int(st.get("wall_dim", 0) or 0)))
        audio_on = bool(st.get("wall_audio", False))
        want_desktop = bool(path) or any(
            bool(v.get("enabled")) for v in widget_states.values())
        for name, w in walls.items():
            w.dim = dim
            if w.audio is not None:
                w.audio.setMuted(not audio_on)
                if audio_on and path and \
                        w.player.playbackState() != w.player.PlaybackState.PlayingState:
                    w.player.play()
            if not want_desktop:
                w.hide()
                continue
            if not w.isVisible():
                w.show()
            if path:
                w.set_file(path)
            else:
                w.stop_video()
            w.desktop.apply_state()
            w.desktop.restyle(scheme)
            w.update()

    def _selftest():
        out_path = os.environ.get("LYNX_SELFTEST_OUT",
                                  "/tmp/opencode/lynx_widgets.png")

        def prep():
            geo = app.primaryScreen().geometry()
            for w in walls.values():
                w.setGeometry(geo)
                w.desktop.enable_for_selftest()

        def snap():
            for w in walls.values():
                w.grab().save(out_path)
                break
            print(f"saved {out_path}")
            app.quit()

        QTimer.singleShot(150, prep)
        QTimer.singleShot(5000, snap)

    watcher = SettingsWatcher(app)
    watcher.changed.connect(lambda _st: apply_state())
    app.primaryScreenChanged.connect(lambda *_: (rebuild_surfaces(), apply_state()))

    # Stream desktop snapshots to lynxBlur consumers: publish as soon as a
    # new video frame lands (coalesced to ~25 fps), plus a 1 s heartbeat so
    # paused/stopped video never goes stale on the consumer side.
    def publish_tick():
        entries = []
        for scr in QGuiApplication.screens():
            wall = walls.get(scr.name())
            frame = getattr(wall, "_frame", None) if wall is not None else None
            entries.append((scr.geometry(), frame))
        try:
            return publish_backdrop(entries)
        except Exception as e:  # never let the publisher take the daemon down
            print(f"lynx-wallpaper: backdrop publish failed: {e}", file=sys.stderr)
            return False

    last_pub = [0.0]

    def pump():
        now = time.monotonic()
        if pub_state["dirty"] or now - last_pub[0] >= 1.0:
            pub_state["dirty"] = False
            if publish_tick():
                last_pub[0] = now

    _pump = QTimer(app)
    _pump.setInterval(40)          # drain window: caps re-publish at ~25 fps
    _pump.timeout.connect(pump)
    _pump.start()
    pump()

    rebuild_surfaces()
    apply_state()
    print(f"lynx-wallpaper: running "
          f"(layer-shell {'on' if layer_ok else 'off'}, screens: {len(walls)})",
          file=sys.stderr)
    if "--selftest" in sys.argv[1:]:
        _selftest()
    return app.exec()


# -------------------------------- CLI ----------------------------------------

def cli_status() -> int:
    st = load_settings()
    print("daemon:", "running" if daemon_running() else "stopped",
          "| enabled:", st.get("wall_enabled", True),
          "| file:", st.get("wall_path") or "(none)")
    return 0


def cli_set(path: str) -> int:
    path = os.path.abspath(os.path.expanduser(path))
    ext = os.path.splitext(path)[1].lower()
    if ext not in VIDEO_EXTS or not os.path.isfile(path):
        print(f"not a usable media file: {path}")
        return 1
    st = load_settings()
    st["wall_path"] = path
    st["wall_enabled"] = True
    save_settings(st)
    spawn_daemon()
    print("wallpaper:", path)
    return 0


def cli_stop() -> int:
    st = load_settings()
    st["wall_enabled"] = False
    save_settings(st)
    kill_daemon()
    print("wallpaper stopped")
    return 0


def cli_enable() -> int:
    st = load_settings()
    st["wall_enabled"] = True
    save_settings(st)
    spawn_daemon()
    print("wallpaper enabled")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "start"
    if cmd == "status":
        return cli_status()
    if cmd == "set":
        return cli_set(args[1]) if len(args) > 1 else 1
    if cmd in ("stop", "disable"):
        return cli_stop()
    if cmd == "enable":
        return cli_enable()
    if cmd == "start":
        spawn_daemon()
        time.sleep(1.0)
        return cli_status()
    if cmd in ("run", "--selftest"):
        return run_daemon()
    print(__doc__)
    print("usage: lynx-wallpaper [start|run|set FILE|stop|enable|status]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
