#!/usr/bin/env python3
"""Lynx taskbar: the layer-shell top bar of lynxde, a Hyprland-based desktop.

Driven by the Hyprland IPC sockets.
"""

from __future__ import annotations

import datetime
import os
import sys

from PySide6.QtCore import QSize, QLockFile, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    ANCHOR_BOTTOM,
    ANCHOR_LEFT,
    ANCHOR_RIGHT,
    ANCHOR_TOP,
    BAR_HEIGHT,
    OWN_CLASSES,
    Hyprland,
    SettingsWatcher,
    apply_hypr_keywords,
    apply_scheme_colors,
    attach_layershell,
    build_style,
    get_bar_height,
    get_bar_side,
    get_clock_opts,
    get_scheme,
    maybe_enable_layer_shell,
    set_layershell_anchor_side,
)
from lynx_blur import lynxBlur  # noqa: E402

DEMO_WORKSPACES = [
    {"id": n, "name": str(n)} for n in range(1, 6)
] + [{"id": -99, "name": "special:scratch"}]

DEMO_CLIENTS = [
    {"address": "0xd1", "class": "firefox", "initialClass": "firefox",
     "title": "Arch Linux - Mozilla Firefox", "workspace": {"id": 1, "name": "1"}},
    {"address": "0xd2", "class": "kitty", "initialClass": "kitty",
     "title": "~/Downloads/lynxde", "workspace": {"id": 1, "name": "1"}},
    {"address": "0xd3", "class": "code", "initialClass": "code-oss",
     "title": "lynx_taskbar.py - lynxde - Visual Studio Code", "workspace": {"id": 2, "name": "2"}},
    {"address": "0xd4", "class": "Spotify", "initialClass": "spotify",
     "title": "Radiohead - Weird Fishes", "workspace": {"id": 3, "name": "3"}},
    {"address": "0xd5", "class": "discord", "initialClass": "discord",
     "title": "#general - Discord", "workspace": {"id": 4, "name": "4"}},
]
DEMO_ACTIVE_WS = {1}
DEMO_FOCUSED = "0xd1"


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def make_sep() -> QFrame:
    sep = QFrame()
    sep.setObjectName("sep")
    sep.setFrameShape(QFrame.Shape.VLine)
    return sep


class TaskButton(QPushButton):
    def __init__(self, client: dict, focused: bool, hy: Hyprland,
                 height: int = 30, parent=None):
        super().__init__(parent)
        addr = client["address"]
        cls = client.get("class") or "?"
        title = client.get("title") or ""
        self._addr = addr
        self._hy = hy
        self.setObjectName("task")
        self.setProperty("focused", focused)
        icon = QIcon.fromTheme((client.get("initialClass") or cls).lower())
        if not icon.isNull():
            self.setIcon(icon)
            self.setIconSize(QSize(16, 16))
        self.setText(cls[:20])
        self.setToolTip(f"{cls}\n{title}" if title else cls)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(height)
        self.clicked.connect(lambda: hy.dispatch("focuswindow", f"address:{addr}"))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._hy.dispatch("closewindow", f"address:{self._addr}")
            return
        super().mousePressEvent(e)

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.addAction("Close", lambda: self._hy.dispatch("closewindow", f"address:{self._addr}"))
        menu.addAction("Toggle floating",
                       lambda: self._hy.dispatch("togglefloating", f"address:{self._addr}"))
        menu.exec(e.globalPos())


class WorkspaceButton(QPushButton):
    def __init__(self, ws: dict, active: bool, hy: Hyprland,
                 height: int = 30, parent=None):
        super().__init__(parent)
        name = ws.get("name") or str(ws["id"])
        special = name.startswith("special:")
        label = name.removeprefix("special:") if special else str(ws["id"])
        self.setObjectName("ws")
        self.setProperty("active", active)
        self.setText(label[:12])
        self.setToolTip(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(height)
        if special:
            self.clicked.connect(lambda: hy.dispatch("togglespecialworkspace", label))
        else:
            self.clicked.connect(lambda: hy.dispatch("workspace", str(ws["id"])))


class Bar(QWidget):
    def __init__(self, hy: Hyprland):
        super().__init__()
        self.hy = hy
        self.demo = "--demo" in sys.argv[1:]
        self.bar_h = get_bar_height()
        self.clock_opts = get_clock_opts()
        self.setWindowTitle("Lynx Taskbar")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("root")
        self.setFixedHeight(self.bar_h)
        self.blur = lynxBlur(self, radius=6, tint=(24, 24, 37, 90))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        row = QHBoxLayout()
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(10)
        outer.addLayout(row)

        self.brand = QLabel("⬢ lynx")
        self.brand.setObjectName("brand")
        self.brand_sep = make_sep()
        row.addWidget(self.brand)
        row.addWidget(self.brand_sep)
        self.apply_brand_visibility()

        inner = QWidget()
        self.tasks_layout = QHBoxLayout(inner)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(5)
        self.tasks_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(inner)
        self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.scroll, 1)

        row.addWidget(make_sep())

        self.ws_row = QHBoxLayout()
        self.ws_row.setSpacing(4)
        row.addLayout(self.ws_row)

        row.addWidget(make_sep())

        clock_box = QVBoxLayout()
        clock_box.setContentsMargins(0, 0, 0, 0)
        clock_box.setSpacing(0)
        self.clock = QLabel("--:--")
        self.clock.setObjectName("clock")
        self.date_label = QLabel("")
        self.date_label.setObjectName("date")
        for lbl in (self.clock, self.date_label):
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setMinimumWidth(72)
            clock_box.addWidget(lbl)
        row.addLayout(clock_box)

        screen = QGuiApplication.primaryScreen()
        width = screen.availableGeometry().width() if screen else 1600
        self.resize(width, self.bar_h)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self.refresh)

        self._safety = QTimer(self)
        self._safety.setInterval(15000)
        self._safety.timeout.connect(self.refresh)
        self._safety.start()

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self.tick_clock)
        self._clock_timer.start()
        self.tick_clock()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.blur.paint(painter, QRectF(self.rect()), corner=14):
            painter.setBrush(QColor(24, 24, 37, 70))
        else:
            painter.setBrush(QColor(24, 24, 37, 232))
        painter.drawRoundedRect(self.rect(), 14, 14)

    def schedule_refresh(self):
        self._debounce.start()

    def tick_clock(self):
        now = datetime.datetime.now()
        o = self.clock_opts
        if o.get("h24", True):
            fmt = "%H:%M" + (":%S" if o.get("seconds") else "")
        else:
            fmt = "%I:%M" + (":%S" if o.get("seconds") else "") + " %p"
        self.clock.setText(now.strftime(fmt).lstrip("0"))
        self.date_label.setText(now.strftime("%a %d %b") if o.get("date", True) else "")

    def apply_brand_visibility(self):
        show = bool(self.clock_opts.get("brand", True))
        self.brand.setVisible(show)
        self.brand_sep.setVisible(show)

    def refresh(self):
        if self.hy.connected and not self.demo:
            monitors = self.hy.ctl_json("monitors") or []
            active_ids = {m.get("activeWorkspace", {}).get("id") for m in monitors}
            workspaces = self.hy.ctl_json("workspaces") or []
            clients = [c for c in (self.hy.ctl_json("clients") or [])
                       if c.get("class") not in OWN_CLASSES]
            focused_win = self.hy.ctl_json("activewindow") or {}
            focused = focused_win.get("address")
        else:
            workspaces = DEMO_WORKSPACES
            clients = list(DEMO_CLIENTS)
            active_ids = DEMO_ACTIVE_WS
            focused = DEMO_FOCUSED

        clear_layout(self.ws_row)
        btn_h = max(20, min(32, self.height() - 22))
        for ws in sorted(workspaces, key=lambda w: w["id"]):
            self.ws_row.addWidget(
                WorkspaceButton(ws, ws["id"] in active_ids, self.hy, height=btn_h))

        clear_layout(self.tasks_layout)
        for client in sorted(clients, key=lambda c: (c.get("workspace", {}).get("id", 0),
                                                     c.get("class", ""))):
            self.tasks_layout.addWidget(
                TaskButton(client, client["address"] == focused, self.hy,
                           height=btn_h))
        if not clients:
            empty = QLabel("no windows")
            empty.setObjectName("empty")
            self.tasks_layout.addWidget(empty)


def main() -> int:
    layer_requested = maybe_enable_layer_shell()
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-taskbar")
    app.setDesktopFileName("lynx-taskbar")
    app.setFont(QFont("Inter", 9))

    scheme = get_scheme()
    app.setStyleSheet(build_style(scheme))
    apply_scheme_colors(scheme)

    lock = QLockFile(f"/tmp/lynx-taskbar-{os.getuid()}.lock")
    if not lock.tryLock(0):
        print("another lynx-taskbar instance is already running", file=sys.stderr)
        return 0

    hy = Hyprland()
    bar = Bar(hy)
    hy.changed.connect(bar.schedule_refresh)
    hy.config_reloaded.connect(lambda: apply_scheme_colors(get_scheme()))
    hy.connect_events()

    side_top = [get_bar_side() != "bottom"]

    def on_settings(_settings):
        want_top = get_bar_side() != "bottom"
        new_scheme = get_scheme()
        app.setStyleSheet(build_style(new_scheme))
        apply_scheme_colors(new_scheme)

        new_h = get_bar_height()
        if new_h != bar.bar_h:
            bar.bar_h = new_h
            bar.setFixedHeight(new_h)
            bar.resize(bar.width(), new_h)
            QTimer.singleShot(0, bar.refresh)

        bar.clock_opts = get_clock_opts()
        bar.apply_brand_visibility()

        if want_top != side_top[0]:
            side_top[0] = want_top
            if not set_layershell_anchor_side(bar, side_top=want_top,
                                              zone=bar.bar_h):
                print("lynx-taskbar: anchor flip failed (plain window?)",
                      file=sys.stderr)
        elif layer_attached[0]:
            set_layershell_anchor_side(bar, side_top=side_top[0], zone=bar.bar_h)
        apply_hypr_keywords(_settings)

    watcher = SettingsWatcher(app)
    watcher.changed.connect(on_settings)

    layer_attached = [False]
    status = "disabled (LYNX_BAR_LAYER=0)"
    if layer_requested:
        bar.winId()
        status = attach_layershell(
            bar,
            anchors=(ANCHOR_TOP if side_top[0] else ANCHOR_BOTTOM)
            | ANCHOR_LEFT | ANCHOR_RIGHT,
            exclusive_zone=bar.bar_h,
            exclusive_edge=ANCHOR_TOP if side_top[0] else ANCHOR_BOTTOM,
        )
        layer_attached[0] = status == "ok"
    bar.show()
    print(f"lynx-taskbar: layer-shell {status}", file=sys.stderr)
    QTimer.singleShot(2000, lambda: apply_hypr_keywords())

    if "--selftest" in sys.argv[1:]:
        out_path = os.environ.get("LYNX_SELFTEST_OUT", "/tmp/opencode/lynx_bar.png")

        def snap():
            bar.grab().save(out_path)
            print(f"saved {out_path}")
            app.quit()

        QTimer.singleShot(1500, snap)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
