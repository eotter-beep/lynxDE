#!/usr/bin/env python3
"""Lynx Welcome: first-run greeting with keybind cheatsheet + startup chime."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import Qt, QRect, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import get_scheme, load_settings, save_settings  # noqa: E402
from lynx_blur import LynxBlur  # noqa: E402

WIDTH = 560

KEYBINDS = (
    ("SUPER + /", "Open the launcher — search apps & the web"),
    ("SUPER + O", "Open Lynx Settings"),
    ("SUPER + X", "Close the focused window"),
)


def _hex_to_rgba(hex_color: str, alpha: int) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


class GlassPanel(QFrame):
    """Panel with lynxBlur frosted-glass backing (plain fill if unavailable)."""

    def __init__(self, parent=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.blur = LynxBlur(self, radius=8, tint=(24, 24, 37, 80))

    def paintEvent(self, event):
        painter = QPainter(self)
        self.blur.paint(painter, QRectF(self.rect()), corner=18)
        super().paintEvent(event)


def find_sound() -> str | None:
    data_dir = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    for base in (os.path.join(data_dir, "lynxde", "sounds"),
                 os.path.join(os.path.dirname(os.path.dirname(
                     os.path.abspath(__file__))), "sounds")):
        hits = sorted(h for h in glob.glob(os.path.join(base, "*"))
                      if not h.endswith((".txt", ".md")))
        if hits:
            return hits[0]
    return None


def play_sound(path: str | None = None) -> bool:
    snd = path or find_sound()
    if not snd:
        return False
    ext = os.path.splitext(snd)[1].lower()
    quiet_ff = ("-nodisp", "-autoexit", "-loglevel", "quiet")
    if ext == ".mp3":
        players = (("mpg123", ("-q",)), ("ffplay", quiet_ff),
                   ("mpv", ("--no-video", "--really-quiet")))
    else:
        players = (("paplay", ()), ("ffplay", quiet_ff),
                   ("mpv", ("--no-video", "--really-quiet")))
    for cmd, extra in players:
        exe = shutil.which(cmd)
        if exe:
            subprocess.Popen([exe, *extra, snd],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
    return False


class KeyChip(QLabel):
    def __init__(self, text: str, s: dict):
        super().__init__(text)
        self.setStyleSheet(f"""
            QLabel {{
                color: {s['on_accent']}; background: {s['accent']};
                border-radius: 7px; padding: 4px 10px;
                font-size: 11px; font-weight: 800;
            }}
        """)


class WelcomeWindow(QWidget):
    def __init__(self):
        super().__init__(None)
        self.scheme = s = get_scheme()
        self.setWindowTitle("Welcome to Lynxde")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        self.panel = GlassPanel(objectName="panel")
        self.panel.blur.availableChanged.connect(lambda _on: self.restyle())
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(34, 26, 34, 24)
        lay.setSpacing(0)

        brand = QLabel("⬢ lynxde")
        brand.setStyleSheet(f"color: {s['accent']}; font-size: 12px;"
                            "font-weight: 800;")
        lay.addWidget(brand)
        lay.addSpacing(14)

        title = QLabel("Welcome to LynxDE Taiga!")
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {s['text']}; font-size: 23px;"
                            "font-weight: 800;")
        lay.addWidget(title)
        lay.addSpacing(6)

        sub = QLabel("Explore how to use this desktop.")
        sub.setStyleSheet(f"color: {s['muted']}; font-size: 13px;")
        lay.addWidget(sub)
        lay.addSpacing(20)

        card = QFrame(objectName="kbcard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 14)
        cl.setSpacing(11)
        head = QLabel("Keybinds")
        head.setObjectName("kbhead")
        cl.addWidget(head)
        for keys, desc in KEYBINDS:
            row = QHBoxLayout()
            row.setSpacing(12)
            chip = KeyChip(keys, s)
            row.addWidget(chip)
            d = QLabel(desc)
            d.setStyleSheet(f"color: {s['text']}; font-size: 12px;")
            row.addWidget(d, 1)
            cl.addLayout(row)
        hint = QLabel("In the launcher: type to search · ↑ ↓ pick · "
                      "Enter open · Esc dismiss")
        hint.setObjectName("kbhint")
        cl.addSpacing(2)
        cl.addWidget(hint)
        lay.addWidget(card)
        lay.addSpacing(18)

        bottom = QHBoxLayout()
        self.dont = QCheckBox("Don't show this again")
        self.dont.setCursor(Qt.CursorShape.PointingHandCursor)
        bottom.addWidget(self.dont)
        bottom.addStretch(1)
        go = QPushButton("Get started")
        go.setCursor(Qt.CursorShape.PointingHandCursor)
        go.setFixedHeight(34)
        go.clicked.connect(self.finish)
        bottom.addWidget(go)
        lay.addLayout(bottom)
        lay.addSpacing(2)

        self.restyle()

    def restyle(self):
        s = self.scheme
        alpha = 110 if self.panel.blur.available() else 245
        self.setStyleSheet(f"""
            QFrame#panel {{
                background: {_hex_to_rgba(s['bg'], alpha)};
                border: 1px solid {s['surface_hi']};
                border-radius: 16px;
            }}
            QFrame#kbcard {{
                background: rgba(255, 255, 255, 12);
                border: 1px solid {s['surface_hi']};
                border-radius: 14px;
            }}
            QLabel#kbhead {{
                color: {s['muted']}; font-size: 10px; font-weight: 700;
            }}
            QLabel#kbhint {{
                color: {s['muted']}; font-size: 11px; font-style: italic;
            }}
            QCheckBox {{ color: {s['muted']}; font-size: 12px; }}
            QPushButton {{
                color: {s['on_accent']}; background: {s['accent']};
                border: none; border-radius: 10px;
                padding: 4px 22px; font-weight: 800; font-size: 13px;
            }}
            QPushButton:hover {{ background: {s['surface_hi']};
                color: {s['text']}; }}
        """)

    def show_centered(self):
        from PySide6.QtGui import QGuiApplication

        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else QRect(0, 0, 1280, 720)
        self.adjustSize()
        self.move(int(geo.center().x() - self.width() / 2),
                  int(geo.center().y() - self.height() / 2) - 40)
        self.show()
        self.raise_()
        self.activateWindow()

    def finish(self):
        if self.dont.isChecked():
            st = load_settings()
            st["welcome_seen"] = True
            save_settings(st)
        self.close()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-welcome")
    app.setDesktopFileName("lynx-welcome")
    app.setQuitOnLastWindowClosed(True)

    args = set(sys.argv[1:])
    if "--selftest" in args or "--screenshot" in args:
        win = WelcomeWindow()
        win.show_centered()
        QTimer.singleShot(400, lambda: _shot(win))
        return app.exec()

    forced = "--show" in args or "--force" in args
    if not forced and load_settings().get("welcome_seen"):
        return 0
    win = WelcomeWindow()
    win.show_centered()
    if load_settings().get("startup_sound", True):
        QTimer.singleShot(250, play_sound)
    return app.exec()


def _shot(win: WelcomeWindow):
    pm = win.grab()
    out = os.environ.get("LYNX_WELCOME_SHOT",
                         "/tmp/opencode/lynx_welcome.png")
    pm.save(out)
    print(f"saved {out}")
    QApplication.quit()


if __name__ == "__main__":
    raise SystemExit(main())
