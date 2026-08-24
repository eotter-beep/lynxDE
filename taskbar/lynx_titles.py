#!/usr/bin/env python3
"""Lynx titles: custom title bars over every window in lynxde, a Hyprland-based desktop.

Pure PySide6.
"""

from __future__ import annotations

import os
import re
import sys

from PySide6.QtCore import QPoint, QRect, QRectF, QLockFile, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    ANCHOR_BOTTOM,
    ANCHOR_LEFT,
    ANCHOR_TOP,
    OWN_CLASSES,
    Hyprland,
    SettingsWatcher,
    apply_hypr_keywords,
    apply_scheme_colors,
    attach_layershell,
    build_style,
    get_bar_side,
    get_scheme,
    get_title_height,
    maybe_enable_layer_shell,
    set_layershell_margins,
    titles_enabled,
)


class TitleBar(QWidget):
    def __init__(self, daemon: "TitleDaemon", addr: str, mon_name: str):
        super().__init__(None)
        self.daemon = daemon
        self.addr = addr
        self.mon_name = mon_name
        self.focused = False
        self.last_local: QRect | None = None

        self.setWindowTitle("lynx-title")
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        row = QHBoxLayout(self)
        row.setContentsMargins(9, 0, 5, 0)
        row.setSpacing(4)
        self.label = QLabel("")
        self.label.setStyleSheet("color: #cdd6f4; font-size: 12px; background: transparent;")
        row.addWidget(self.label, 1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Close window")
        # 'pressed' rather than 'clicked': layer surfaces can drop the pointer
        # grab between press and release, which would swallow 'clicked'.
        self.close_btn.pressed.connect(self.close_client)
        row.addWidget(self.close_btn)
        self.resize(400, daemon.title_height)
        self._restyle()

        self._closing = False
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(30)
        self._drag_timer.timeout.connect(self._drag_tick)
        self._drag_origin_at: QPoint | None = None
        self._drag_cursor_start: QPoint | None = None

    def _restyle(self):
        s = self.daemon.scheme
        fg_on = s["on_accent"] if self.focused else s["text"]
        self.label.setStyleSheet(
            f"color: {fg_on}; font-size: 12px; background: transparent;")
        self.close_btn.setStyleSheet(
            f"QPushButton {{ color: {s['text']}; background: transparent; border: none;"
            f" border-radius: 6px; padding: 0px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {s['red']}; color: {s['on_accent']}; }}")

    # ---- geometry/state -------------------------------------------------
    def configure_surface(self, mon_name: str) -> bool:
        for screen in QGuiApplication.screens():
            if screen.name() == mon_name:
                self.setScreen(screen)
                break
        self.winId()
        edge = ANCHOR_BOTTOM if self.daemon.bar_top else ANCHOR_TOP
        status = attach_layershell(self, anchors=edge | ANCHOR_LEFT,
                                   exclusive_zone=0, layer=2)
        print(f"lynx-titles[{self.addr}]: layer-shell {status}", file=sys.stderr)
        return status == "ok"

    def update_state(self, local: QRect, title: str, focused: bool):
        self.focused = focused
        self._restyle()
        th = self.daemon.title_height
        self.label.setText(self._elide(title, max(local.width() - 60, 40)))
        self.resize(local.width(), th)
        if self.last_local != local:
            self.last_local = QRect(local)
            mon = self.daemon.monitors.get(self.mon_name, {})
            if self.daemon.bar_top:
                l, t, r, b = local.x(), 0, 0, max(mon.get("height", 0) - local.y() - th, 0)
            else:
                l, t, r, b = local.x(), max(local.y(), 0), 0, 0
            if not set_layershell_margins(self, l, t, r, b):
                self.move(local.translated(mon.get("x", 0), mon.get("y", 0)).topLeft())
        self.update()

    def _elide(self, text: str, width: int) -> str:
        return QFontMetrics(self.font()).elidedText(text, Qt.TextElideMode.ElideRight, width)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.daemon.scheme["accent"] if self.focused
                                else self.daemon.scheme["surface"]))
        painter.drawPath(path)

    # ---- interaction ----------------------------------------------------
    def close_client(self):
        """Close the client behind this bar; escalate if it refuses to die."""
        if self._closing:
            return
        self._closing = True
        ok = self.daemon.hy.dispatch("closewindow", f"address:{self.addr}")
        if not ok:
            # hyprctl rejected it (stale address?): drop the orphan bar now.
            self.daemon.drop_overlay(self.addr)
            return
        QTimer.singleShot(600, self._verify_closed)

    def _client_alive(self) -> bool:
        for c in (self.daemon.hy.ctl_json("clients") or []):
            if c.get("address") == self.addr:
                return True
        return False

    def _verify_closed(self):
        if not self._closing:
            return
        if self._client_alive():
            print(f"lynx-titles[{self.addr}]: still alive after closewindow, "
                  "escalating to killwindow", file=sys.stderr)
            self.daemon.hy.dispatch("killwindow", f"address:{self.addr}")
            QTimer.singleShot(900, lambda: self.daemon.drop_overlay(self.addr))
        else:
            self.daemon.drop_overlay(self.addr)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = self.daemon.hy.ctl_text("cursorpos")
            m = re.match(r"(\d+)[, ]+(\d+)", pos)
            if m:
                self._drag_cursor_start = QPoint(int(m.group(1)), int(m.group(2)))
                self._drag_origin_at = QPoint(*self.daemon.client_pos(self.addr))
                self._drag_timer.start()
                return
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._stop_drag()
        super().mouseReleaseEvent(e)

    def _stop_drag(self):
        self._drag_timer.stop()
        self._drag_origin_at = None
        self._drag_cursor_start = None

    def _drag_tick(self):
        if self._drag_origin_at is None or self._drag_cursor_start is None:
            self._stop_drag()
            return
        pos = self.daemon.hy.ctl_text("cursorpos")
        m = re.match(r"(\d+)[, ]+(\d+)", pos)
        if not m:
            self._stop_drag()
            return
        cur = QPoint(int(m.group(1)), int(m.group(2)))
        delta = cur - self._drag_cursor_start
        target = self._drag_origin_at + delta
        self.daemon.hy.dispatch("movewindowpixel",
                                f"exact {target.x()} {target.y()},address:{self.addr}")


class TitleDaemon:
    def __init__(self, app: QApplication):
        self.app = app
        self.hy = Hyprland()
        self.overlays: dict[str, TitleBar] = {}
        self.monitors: dict[str, dict] = {}
        self.client_floating: dict[str, bool] = {}
        self.focused_addr = ""
        self.scheme = get_scheme()
        self.bar_top = get_bar_side() != "bottom"
        self.title_height = get_title_height()
        self.titles_on = titles_enabled()

        self._debounce = QTimer(app)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(90)
        self._debounce.timeout.connect(self.refresh)

        self._safety = QTimer(app)
        self._safety.setInterval(4000)
        self._safety.timeout.connect(self.refresh)

    def start(self):
        self.hy.changed.connect(self._debounce.start)
        self.hy.config_reloaded.connect(lambda: apply_scheme_colors(get_scheme()))
        self.hy.connect_events()

        watcher = SettingsWatcher(self.app)

        def on_settings(_):
            self.scheme = get_scheme()
            self.bar_top = get_bar_side() != "bottom"
            self.title_height = get_title_height()
            self.titles_on = titles_enabled()
            apply_scheme_colors(self.scheme)
            apply_hypr_keywords()
            for ov in list(self.overlays.values()):
                ov.close()
            self.overlays.clear()
            self.refresh()

        watcher.changed.connect(on_settings)

        self._safety.start()
        self.refresh()
        QTimer.singleShot(2500, lambda: apply_hypr_keywords())

    def shutdown(self):
        for ov in list(self.overlays.values()):
            ov.close()
        self.overlays.clear()

    def drop_overlay(self, addr: str):
        """Remove a title bar immediately (window gone / force-closed)."""
        ov = self.overlays.pop(addr, None)
        if ov is not None:
            ov.close()

    # ---- data helpers ---------------------------------------------------
    def client_pos(self, addr: str):
        for c in (self.hy.ctl_json("clients") or []):
            if c["address"] == addr:
                return c.get("at", [0, 0])
        return [0, 0]

    def _resolve_mon(self, client: dict) -> dict | None:
        key = client.get("monitor")
        if isinstance(key, int):
            names = sorted(self.monitors)
            key = names[key] if 0 <= key < len(names) else None
        return self.monitors.get(key or "")

    # ---- main sync ------------------------------------------------------
    def refresh(self):
        demo = "--demo" in sys.argv[1:]
        if not self.titles_on and not demo:
            for ov in list(self.overlays.values()):
                ov.close()
            self.overlays.clear()
            return
        if not demo:
            mons = self.hy.ctl_json("monitors") or []
            self.monitors = {m["name"]: m for m in mons}
            clients = [c for c in (self.hy.ctl_json("clients") or [])
                       if c.get("class") not in OWN_CLASSES]
            aw = self.hy.ctl_json("activewindow") or {}
            self.focused_addr = aw.get("address", "")
        else:
            self.monitors = {"DEMO": {"name": "DEMO", "x": 0, "y": 0, "width": 1920, "height": 1080}}
            clients = [
                {"address": "0xa1", "class": "firefox", "title": "Firefox demo window",
                 "floating": False, "fullscreen": 0, "monitor": "DEMO",
                 "workspace": {"id": 1}, "at": [200, 200], "size": [900, 600]},
                {"address": "0xa2", "class": "kitty", "title": "kitty — demo terminal",
                 "floating": True, "fullscreen": 0, "monitor": "DEMO",
                 "workspace": {"id": 1}, "at": [700, 420], "size": [640, 380]},
            ]
            self.focused_addr = "0xa1"

        desired: dict[str, tuple[str, QRect, dict, bool]] = {}
        for c in clients:
            if c.get("workspace", {}).get("id", -1) < 0:
                continue
            if c.get("fullscreen"):
                continue
            mon = self._resolve_mon(c)
            if mon is None:
                continue
            aw = mon.get("activeWorkspace")
            if aw is not None and c.get("workspace", {}).get("id") != aw.get("id"):
                continue
            if not c.get("floating"):
                continue
            x, y = c.get("at", [0, 0])
            w, h = c.get("size", [0, 0])
            if w <= 0 or h <= 0:
                continue
            local = QRect(x - mon["x"], y - mon["y"], w, self.title_height)
            addr = c["address"]
            desired[addr] = (mon["name"], local, c, addr == self.focused_addr)

        for addr in list(self.overlays):
            if addr not in desired:
                self.overlays.pop(addr).close()

        for addr, (mon_name, local, c, focused) in desired.items():
            ov = self.overlays.get(addr)
            if ov is None:
                ov = TitleBar(self, addr, mon_name)
                ov.configure_surface(mon_name)
                ov.show()
                self.overlays[addr] = ov
            self.client_floating[addr] = bool(c.get("floating"))
            ov.update_state(local, c.get("title") or c.get("class") or "", focused)


def main() -> int:
    layer_requested = maybe_enable_layer_shell()
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-titles")
    app.setDesktopFileName("lynx-titles")
    app.setFont(QFont("Inter", 9))
    app.setStyleSheet(build_style(get_scheme()))

    lock = QLockFile(f"/tmp/lynx-titles-{os.getuid()}.lock")
    if not lock.tryLock(0):
        print("another lynx-titles instance is already running", file=sys.stderr)
        return 0

    daemon = TitleDaemon(app)
    app.aboutToQuit.connect(daemon.shutdown)

    if "--selftest" in sys.argv[1:]:
        out_path = os.environ.get("LYNX_SELFTEST_OUT", "/tmp/opencode/lynx_titles.png")

        def snap():
            daemon.refresh()
            target = next(iter(daemon.overlays.values()), None)
            if target is not None:
                target.grab().save(out_path)
                print(f"saved {out_path}")
            app.quit()

        QTimer.singleShot(1500, snap)
        daemon.start()
        return app.exec()

    daemon.start()
    print(f"lynx-titles: running (layer-shell {'on' if layer_requested else 'off'})",
          file=sys.stderr)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
