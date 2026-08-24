#!/usr/bin/env python3
"""Lynx Settings: full control panel for lynxde.

Everything the desktop can do — schemes, accent colors, bar & title-bar
geometry, clock formats, wallpapers, desktop widgets, launcher engines,
live Hyprland compositor tuning (gaps/borders/blur/animations/opacity),
input behavior and session startup — in one window. Changes apply live
and persist in ~/.config/lynxde/settings.json.
"""

from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    BAR_HEIGHT,
    SCHEMES,
    SETTINGS_DIR,
    TITLE_HEIGHT,
    VIDEO_EXTS,
    WALLPAPER_DIRS,
    SettingsWatcher,
    Hyprland,
    apply_hypr_keywords,
    apply_scheme_colors,
    build_style,
    get_bar_side,
    get_scheme,
    get_wall_path,
    load_settings,
    save_settings,
)
from lynx_blur import LynxBlur  # noqa: E402

WALLPAPER_BIN = os.path.expanduser("~/.local/bin/lynx-wallpaper")
LAUNCHER_STATE = os.path.join(SETTINGS_DIR, "launcher.json")

ACCENT_PRESETS = ("#cba6f7", "#f38ba8", "#fab387", "#f9e2af", "#a6e3a1",
                  "#94e2d5", "#74c7ec", "#89dceb", "#b4befe", "#eba0ac")
ENGINE_LABELS = (("duckduckgo", "DuckDuckGo"), ("mwmbl", "Mwmbl"))
FOCUS_MODES = ((0, "Click"), (1, "Follow"), (2, "Hover"), (3, "Drag"))


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
        self.blur.paint(painter, QRectF(self.rect()), corner=16)
        super().paintEvent(event)


class SchemeCard(QPushButton):
    def __init__(self, key: str):
        super().__init__(SCHEMES[key]["label"])
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(110, 64)

    def paintEvent(self, event):
        s = SCHEMES[self.key]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12, 12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(s["surface"]))
        painter.drawPath(path)
        pen_col = QColor(s["accent"]) if self.isChecked() else QColor(s["surface_hi"])
        painter.setPen(pen_col)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, k in enumerate(("accent", "surface_hi", "text")):
            painter.setBrush(QColor(s[k]))
            painter.drawEllipse(14 + i * 22, 36, 14, 14)
        painter.setPen(QColor(s["text"]))
        f = QFont(self.font())
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(QRectF(12, 6, self.width() - 20, 24),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.text())


# ---- generic setting rows -----------------------------------------------------

class Row(QFrame):
    """One labeled setting line; carries search keywords."""

    def __init__(self, title: str, desc: str = "", keywords: str = ""):
        super().__init__()
        self.setObjectName("row")
        self.keywords = f"{title} {desc} {keywords}".lower()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(QLabel(title, objectName="rowtitle"))
        if desc:
            text.addWidget(QLabel(desc, objectName="rowdesc"))
        lay.addLayout(text, 1)
        self.control_host = lay


class Toggle(QCheckBox):
    def __init__(self, checked: bool):
        super().__init__()
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class Chips(QWidget):
    """Exclusive row of pill buttons; mirrors a string/int setting."""

    def __init__(self, options, current, on_pick, minimum_width: int = 86):
        super().__init__()
        self.keywords = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for value, label in options:
            btn = QPushButton(str(label))
            btn.setCheckable(True)
            btn.setChecked(value == current)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumWidth(minimum_width)
            btn.clicked.connect(lambda _=False, v=value: on_pick(v))
            self.group.addButton(btn)
            lay.addWidget(btn)
        lay.addStretch(1)

    def set_keywords(self, kw: str):
        self.keywords = kw.lower()


class SliderRow(QWidget):
    """Int slider with live value caption; debounces commits."""

    def __init__(self, lo: int, hi: int, value: int, suffix: str,
                 on_commit, tick: int = 1):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setSingleStep(tick)
        self.slider.setPageStep(max(tick, (hi - lo) // 10))
        self.slider.setValue(value)
        self.slider.setMinimumWidth(170)
        self.slider.setMaximumWidth(230)
        self.suffix = suffix
        self.value_lab = QLabel(self._text(value), objectName="sliderval")
        self.slider.valueChanged.connect(self._changed)
        lay.addWidget(self.slider)
        lay.addWidget(self.value_lab)
        self._on_commit = on_commit
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(
            lambda: on_commit(self.slider.value()))

    def _text(self, v: int) -> str:
        return f"{v}{self.suffix}"

    def _changed(self, v: int):
        self.value_lab.setText(self._text(v))
        self._debounce.start()


# ---- one settings page ----------------------------------------------------------

class Page(QWidget):
    def __init__(self, pid: str):
        super().__init__()
        self.pid = pid
        self.sections: list[tuple[QLabel | None, list[QWidget]]] = []
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(26, 14, 26, 20)
        self.lay.setSpacing(8)

    def cap(self, text: str) -> QLabel:
        c = QLabel(text, objectName="section")
        self.lay.addWidget(c)
        self.sections.append((c, []))
        return c

    def add(self, w: QWidget):
        if not self.sections:
            self.sections.append((None, []))
        self.lay.addWidget(w)
        self.sections[-1][1].append(w)

    def stretch(self):
        self.lay.addStretch(1)

    def filter(self, q: str) -> int:
        """Hide rows/captions not matching q; returns number of visible rows."""
        visible = 0
        for cap, widgets in self.sections:
            hits = 0
            for w in widgets:
                kw = getattr(w, "keywords", "") or \
                     (w.text() if isinstance(w, QLabel) else "")
                hit = (not q) or (q in str(kw).lower())
                w.setVisible(hit)
                hits += hit
            if cap is not None:
                cap.setVisible(hits > 0)
            visible += hits
        return visible


# ---- main window -----------------------------------------------------------------

class SettingsWindow(QWidget):
    def __init__(self):
        super().__init__(None)
        self.scheme = get_scheme()
        self.bar_side = get_bar_side()
        self.wall_files: list[str] = []
        self.setWindowTitle("Lynx Settings")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(900, 620)
        self.setMinimumSize(760, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        self.panel = GlassPanel(objectName="panel")
        self.panel.blur.availableChanged.connect(lambda _on: self.restyle())
        outer.addWidget(self.panel)
        root = QVBoxLayout(self.panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget(objectName="head")
        hh = QVBoxLayout(head)
        hh.setContentsMargins(24, 16, 24, 10)
        hh.setSpacing(10)
        hh.addWidget(QLabel("Lynx Settings", objectName="brand"))
        self.search = QLineEdit(objectName="search")
        self.search.setPlaceholderText("Search every setting… gaps, clock, accent…")
        self.search.textChanged.connect(self.apply_filter)
        hh.addWidget(self.search)
        root.addWidget(head)

        mid = QHBoxLayout()
        mid.setContentsMargins(14, 0, 0, 0)
        mid.setSpacing(6)

        # -- navigation rail ------------------------------------------------------
        self.pages: dict[str, Page] = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        nav_w = QWidget(objectName="nav")
        nav_w.setFixedWidth(168)
        nav_lay = QVBoxLayout(nav_w)
        nav_lay.setContentsMargins(0, 8, 0, 8)
        nav_lay.setSpacing(4)
        self.nav_buttons: dict[str, QPushButton] = {}
        for pid, label in self._page_defs():
            b = QPushButton(label)
            b.setObjectName("navbtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _=False, k=pid: self.show_page(k))
            self.nav_group.addButton(b)
            nav_lay.addWidget(b)
            self.nav_buttons[pid] = b
        nav_lay.addStretch(1)
        mid.addWidget(nav_w)

        # -- stacked pages ----------------------------------------------------------
        self.stack = QStackedWidget()
        self.page_scrolls: dict[str, QScrollArea] = {}
        mid.addWidget(self.stack, 1)
        root.addLayout(mid, 1)

        self._build_appearance()
        self._build_bar()
        self._build_wallpaper()
        self._build_desktop()
        self._build_launcher()
        self._build_windows()
        self._build_input()
        self._build_startup()

        foot = QWidget(objectName="foot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(24, 6, 24, 12)
        self.status = QLabel("", objectName="hint")
        fl.addWidget(self.status, 1)
        self.refresh_btn = QPushButton("Reload Hyprland", objectName="refreshbtn")
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.setVisible(False)
        self.refresh_btn.clicked.connect(self.reload_hyprland)
        fl.addWidget(self.refresh_btn)
        close = QPushButton("Close", objectName="closebtn")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close)
        fl.addWidget(close)
        root.addWidget(foot)
        self._hypr_dirty = False

        self.restyle()
        self.refresh_wallpapers()
        self.show_page("appearance")
        self._autostart_timer = QTimer(self)
        self._autostart_timer.setSingleShot(True)
        self._autostart_timer.setInterval(700)
        self._autostart_timer.timeout.connect(self.save_autostart)

    @staticmethod
    def _page_defs():
        return (
            ("appearance", "Appearance"),
            ("bar", "Bar && Titles"),
            ("wallpaper", "Wallpaper"),
            ("desktop", "Desktop widgets"),
            ("launcher", "Launcher"),
            ("windows", "Windows && effects"),
            ("input", "Keyboard && mouse"),
            ("startup", "Startup"),
        )

    def show_page(self, pid: str):
        scroll = self.page_scrolls.get(pid)
        if scroll is None:
            return
        self.stack.setCurrentWidget(scroll)
        for k, b in self.nav_buttons.items():
            b.setChecked(k == pid)

    def _new_page(self, pid: str) -> Page:
        page = Page(pid)
        self.pages[pid] = page
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)
        self.page_scrolls[pid] = scroll
        return page

    # helpers ---------------------------------------------------------------
    def cur(self, key: str, default):
        return load_settings().get(key, default)

    def set_values(self, patch: dict, flash: str = ""):
        st = load_settings()
        st.update(patch)
        save_settings(st)
        touched_hypr = any(k.startswith("hypr_") for k in patch)
        touched_visual = any(k in ("scheme", "accent") for k in patch)
        if touched_visual:
            self.scheme = get_scheme()
            apply_scheme_colors(self.scheme)
            self.restyle()
        if touched_hypr:
            apply_hypr_keywords(st)
            self._hypr_dirty = True
            self.refresh_btn.setVisible(True)
            self.refresh_btn.setToolTip(
                "Runs 'hyprctl reload', then re-applies every Lynx tweak.")
        if flash:
            self.status.setText(flash)

    def reload_hyprland(self):
        """hyprctl reload, then re-apply all Lynx tweaks on the clean slate."""
        btn = self.refresh_btn
        btn.setEnabled(False)
        self.status.setText("Reloading Hyprland…")

        def step_reapply():
            self.status.setText("Re-applying tweaks…")
            QTimer.singleShot(1200, step_done)

        def step_done():
            st = load_settings()
            apply_hypr_keywords(st)
            apply_scheme_colors(get_scheme())
            self._hypr_dirty = False
            btn.setEnabled(True)
            btn.setVisible(False)
            self.status.setText("Hyprland reloaded — tweaks reapplied.")

        QTimer.singleShot(0, lambda: (Hyprland.compositor_reload(),
                                      step_reapply()))

    def make_row(self, page: Page, title: str, control: QWidget,
                 desc: str = "", keywords: str = "", cap: QLabel | None = None):
        row = Row(title, desc, keywords)
        row.control_host.addWidget(control)
        page.add(row)
        return row

    def make_toggle(self, page: Page, title: str, desc: str, key: str,
                    default: bool, flash: str = "", keywords: str = "",
                    cap: QLabel | None = None):
        ctl = Toggle(bool(self.cur(key, default)))
        ctl.toggled.connect(lambda state: self.set_values({key: bool(state)}, flash))
        return self.make_row(page, title, ctl, desc, keywords, cap)

    def make_slider(self, page: Page, title: str, desc: str, key: str,
                    lo: int, hi: int, default, suffix: str, flash: str = "",
                    keywords: str = "", scale: float = 1.0,
                    cap: QLabel | None = None):
        try:
            raw = float(self.cur(key, float(default)))
        except (TypeError, ValueError):
            raw = float(default)
        val = int(round(min(hi, max(lo, raw / scale))))
        ctl = SliderRow(lo, hi, val, suffix,
                        lambda v: self._commit_slider(key, v, scale, flash))
        return self.make_row(page, title, ctl, desc, keywords, cap)

    def _commit_slider(self, key: str, v: int, scale: float, flash: str):
        stored: int | float = round(v * scale, 4) if scale != 1.0 else v
        self.set_values({key: stored}, flash)

    # ---- page builders ------------------------------------------------------
    def _build_appearance(self):
        pg = self._new_page("appearance")
        cap_scheme = pg.cap("Color scheme")
        sroww = QWidget(objectName="schemes")
        sroww.keywords = "color scheme theme " + \
            " ".join(f"{k} {v['label']}" for k, v in SCHEMES.items())
        srow = QHBoxLayout(sroww)
        srow.setSpacing(8)
        srow.setContentsMargins(0, 0, 0, 0)
        self.scheme_group = QButtonGroup(self)
        current = load_settings().get("scheme") or "lynx"
        for key in SCHEMES:
            card = SchemeCard(key)
            card.setChecked(key == current)
            card.clicked.connect(lambda _=False, k=key: self.pick_scheme(k))
            self.scheme_group.addButton(card)
            srow.addWidget(card)
        srow.addStretch(1)
        pg.add(sroww)

        cap_acc = pg.cap("Accent color")
        accw = QWidget()
        accw.keywords = "accent color custom swatch preset border"
        al = QHBoxLayout(accw)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        self.accent_btns: dict[str, QPushButton] = {}
        for hexcol in ACCENT_PRESETS:
            b = QPushButton()
            b.setFixedSize(30, 30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(hexcol)
            b.clicked.connect(lambda _=False, h=hexcol: self.pick_accent(h))
            al.addWidget(b)
            self.accent_btns[hexcol] = b
        custom = QPushButton("Custom…")
        custom.setCursor(Qt.CursorShape.PointingHandCursor)
        custom.clicked.connect(self.pick_custom_accent)
        al.addWidget(custom)
        reset = QPushButton("Follow scheme")
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(lambda: self.pick_accent(""))
        al.addWidget(reset)
        al.addStretch(1)
        pg.add(accw)
        note = QLabel("The accent colors focused-window borders, the taskbar, "
                      "title bars and menus.", objectName="hint")
        pg.add(note)
        self.paint_accent_swatches(load_settings().get("accent", ""))

    def paint_accent_swatches(self, selected: str):
        s = self.scheme
        sel = str(selected or "").lower()
        for hexcol, b in self.accent_btns.items():
            b.setStyleSheet(
                f"QPushButton {{ background: {hexcol}; border: 2px solid "
                f"{'rgba(255,255,255,230)' if hexcol == sel else 'transparent'};"
                f" border-radius: 15px; }}"
                f"QPushButton:hover {{ border-color: {s['text']}; }}")

    def pick_accent(self, hexcol: str):
        st = load_settings()
        if hexcol:
            st["accent"] = hexcol
        else:
            st.pop("accent", None)
        save_settings(st)
        self.scheme = get_scheme()
        apply_scheme_colors(self.scheme)
        self.restyle()
        self.paint_accent_swatches(hexcol)
        self.status.setText("Accent updated everywhere."
                            if hexcol else "Accent follows the scheme again.")

    def pick_custom_accent(self):
        col = QColorDialog.getColor(QColor(str(self.cur("accent", "#cba6f7"))),
                                    self, "Pick accent color")
        if col.isValid():
            self.pick_accent(col.name())

    def _build_bar(self):
        pg = self._new_page("bar")
        cap_pos = pg.cap("App bar")
        broww = QWidget(objectName="side")
        broww.keywords = "app bar position edge top bottom dock"
        brow = QHBoxLayout(broww)
        brow.setSpacing(8)
        brow.setContentsMargins(0, 0, 0, 0)
        self.side_group = QButtonGroup(self)
        for side, label in (("top", "Top edge"), ("bottom", "Bottom edge")):
            btn = QPushButton(label, objectName="side")
            btn.setCheckable(True)
            btn.setChecked(side == self.bar_side)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, sd=side: self.pick_side(sd))
            self.side_group.addButton(btn)
            brow.addWidget(btn)
        brow.addStretch(1)
        pg.add(broww)
        self.make_slider(pg, "Bar height", "Pixels tall (28–120)", "bar_height",
                         28, 120, BAR_HEIGHT, "px", "Bar resized.",
                         keywords="size thickness dock height", cap=cap_pos)
        self.make_toggle(pg, "Show “lynx” brand label", "", "bar_show_brand",
                         True, "Brand label updated.",
                         keywords="logo name brand", cap=cap_pos)

        cap_clock = pg.cap("Clock")
        self.make_toggle(pg, "24-hour time", "", "clock_24h", True,
                         "Clock format updated.", keywords="clock format am pm 24h",
                         cap=cap_clock)
        self.make_toggle(pg, "Show seconds", "", "clock_seconds", False,
                         "Clock format updated.", keywords="clock seconds ticks",
                         cap=cap_clock)
        self.make_toggle(pg, "Show date under the clock", "", "clock_show_date",
                         True, "Date display updated.", keywords="clock date weekday",
                         cap=cap_clock)

        cap_titles = pg.cap("Title bars")
        self.make_toggle(pg, "Custom title bars",
                         "Per-window floating bars drawn by lynxde",
                         "titles_enabled", True, "Title bars updated.",
                         keywords="titlebar decorations header", cap=cap_titles)
        self.make_slider(pg, "Title bar height", "Pixels tall (18–64)",
                         "title_height", 18, 64, TITLE_HEIGHT, "px",
                         "Title bar height updated.", keywords="titlebar size",
                         cap=cap_titles)

    def _build_wallpaper(self):
        pg = self._new_page("wallpaper")
        cap_wall = pg.cap("Live wallpaper")
        self.wall_holder = QWidget()
        self.wall_holder.keywords = "live wallpaper video gif mp4 background"
        self.wall_box = QVBoxLayout(self.wall_holder)
        self.wall_box.setContentsMargins(0, 0, 0, 0)
        self.wall_box.setSpacing(6)
        pg.add(self.wall_holder)

        wall_btns = QWidget()
        wall_btns.keywords = self.wall_holder.keywords
        wb = QHBoxLayout(wall_btns)
        wb.setContentsMargins(0, 0, 0, 0)
        wb.setSpacing(8)
        browse = QPushButton("Browse…", objectName="wallaction")
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self.browse_wallpaper)
        stopb = QPushButton("Stop wallpaper", objectName="wallaction")
        stopb.setCursor(Qt.CursorShape.PointingHandCursor)
        stopb.clicked.connect(self.stop_wallpaper)
        open_dir = QPushButton("Open folder", objectName="wallaction")
        open_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        open_dir.clicked.connect(self.open_wall_folder)
        wb.addWidget(browse)
        wb.addWidget(stopb)
        wb.addWidget(open_dir)
        wb.addStretch(1)
        pg.add(wall_btns)

        self.make_slider(pg, "Dim wallpaper", "Darken it so windows pop (0–80%)",
                         "wall_dim", 0, 80, 0, "%", "Wallpaper dim applied.",
                         keywords="brightness darken dim overlay", cap=cap_wall)
        self.make_toggle(pg, "Play wallpaper audio",
                         "Video soundtracks are muted unless enabled",
                         "wall_audio", False, "Wallpaper audio preference saved.",
                         keywords="sound volume music video audio", cap=cap_wall)

    def open_wall_folder(self):
        import subprocess as sp

        d = os.path.expanduser(WALLPAPER_DIRS[0])
        os.makedirs(d, exist_ok=True)
        sp.Popen(["xdg-open", d], stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                 start_new_session=True)

    def _build_desktop(self):
        pg = self._new_page("desktop")
        cap = pg.cap("Desktop widgets")
        st = load_settings().get("widgets", {})
        for wid, label, desc in (
                ("clock", "Lynx Clock", "Analog flip clock in the corner"),
                ("osm", "OpenStreetMap", "Pannable map tile widget")):
            on = bool(st.get(wid, {}).get("enabled"))

            def on_toggled(state: int, k=wid):
                s = load_settings()
                s.setdefault("widgets", {}).setdefault(k, {})["enabled"] = bool(state)
                save_settings(s)
                self.status.setText("Widget toggled — applies within a second.")

            ctl = Toggle(on)
            ctl.toggled.connect(on_toggled)
            self.make_row(pg, label, ctl, desc, f"widget desktop {wid}", cap)
        rw = QPushButton("Reset widget positions")
        rw.setCursor(Qt.CursorShape.PointingHandCursor)
        rw.clicked.connect(self.reset_widget_positions)
        wrap = QWidget()
        wrap.keywords = "reset positions corners widgets"
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(rw)
        wl.addStretch(1)
        pg.add(wrap)

    def reset_widget_positions(self):
        s = load_settings()
        for entry in (s.get("widgets") or {}).values():
            entry.pop("x", None)
            entry.pop("y", None)
        save_settings(s)
        self.status.setText("Widgets snap back to their corners.")

    def _build_launcher(self):
        pg = self._new_page("launcher")
        cap_eng = pg.cap("Web search engines")
        box = QWidget()
        box.keywords = "web search engine duckduckgo mwmbl browser"
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(14)
        saved = self.cur("web_engines", [])
        # empty/missing list == every engine allowed
        enabled = saved if isinstance(saved, list) else []
        self.engine_boxes: list[QCheckBox] = []
        for eid, label in ENGINE_LABELS:
            chk = QCheckBox(label)
            chk.setChecked(not enabled or eid in enabled)
            chk.setCursor(Qt.CursorShape.PointingHandCursor)
            chk.toggled.connect(self.commit_engines)
            bl.addWidget(chk)
            self.engine_boxes.append(chk)
        bl.addStretch(1)
        pg.add(box)

        cap_hist = pg.cap("History")
        chw = QPushButton("Clear search history")
        chw.setCursor(Qt.CursorShape.PointingHandCursor)
        chw.clicked.connect(self.clear_history)
        hist_n = len(self.load_history())
        wrap = QWidget()
        wrap.keywords = "history clear recent launcher searches"
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(chw)
        self.hist_note = QLabel(
            f"{hist_n} entr{'y' if hist_n == 1 else 'ies'} stored",
            objectName="hint")
        wl.addWidget(self.hist_note)
        wl.addStretch(1)
        pg.add(wrap)
        tip = QLabel("Open the palette with SUPER + /. Arrow keys pick, "
                     "Enter opens, Esc dismisses.", objectName="hint")
        pg.add(tip)

    def commit_engines(self):
        labels = {label: eid for eid, label in ENGINE_LABELS}
        chosen = [labels[c.text()] for c in self.engine_boxes if c.isChecked()]
        if not chosen:
            # [] means "all" — keep at least one engine on instead
            for c in self.engine_boxes:
                c.blockSignals(True)
                c.setChecked(True)
                c.blockSignals(False)
            self.status.setText("At least one web engine stays enabled.")
            return
        self.set_values({"web_engines": chosen},
                        f"{len(chosen)} web engine(s) enabled.")

    @staticmethod
    def load_history() -> list:
        try:
            with open(LAUNCHER_STATE, encoding="utf-8") as f:
                d = json.load(f)
            h = d.get("history", []) if isinstance(d, dict) else []
            return h if isinstance(h, list) else []
        except (OSError, ValueError):
            return []

    def clear_history(self):
        try:
            with open(LAUNCHER_STATE, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        d["history"] = []
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        tmp = LAUNCHER_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, LAUNCHER_STATE)
        self.hist_note.setText("history cleared")
        self.status.setText("Launcher history cleared.")

    def _build_windows(self):
        pg = self._new_page("windows")
        cap_gaps = pg.cap("Layout & gaps")
        lay_ctl = Chips((("dwindle", "Dwindle"), ("master", "Master")),
                        self.cur("hypr_layout", "dwindle"),
                        lambda v: self.set_values({"hypr_layout": v},
                                                  f"Layout: {v}."))
        lay_ctl.set_keywords("tiling layout dwindle master spiral")
        self.make_row(pg, "Tiling layout", lay_ctl,
                      "How new windows arrange themselves",
                      "tiling layout dwindle master", cap_gaps)
        self.make_slider(pg, "Gaps between windows", "Inner gaps in px (0–40)",
                         "hypr_gaps_in", 0, 40, 5, "px",
                         keywords="gaps inner spacing", cap=cap_gaps)
        self.make_slider(pg, "Gaps around screen edges", "Outer gaps in px (0–80)",
                         "hypr_gaps_out", 0, 80, 5, "px",
                         keywords="gaps outer margin edge screen", cap=cap_gaps)

        cap_look = pg.cap("Borders & effects")
        self.make_slider(pg, "Border width", "Focused-window outline (0–10)",
                         "hypr_border_size", 0, 10, 1, "px",
                         keywords="border size stroke outline", cap=cap_look)
        self.make_slider(pg, "Corner rounding", "Window radius in px (0–32)",
                         "hypr_rounding", 0, 32, 8, "px",
                         keywords="rounding radius corners", cap=cap_look)
        self.make_toggle(pg, "Blur behind windows", "", "hypr_blur", True,
                         "Blur toggled.", keywords="blur frosted transparency",
                         cap=cap_look)
        self.make_toggle(pg, "Window shadows", "", "hypr_shadows", True,
                         "Shadows toggled.", keywords="shadow depth", cap=cap_look)
        self.make_toggle(pg, "Animations", "", "hypr_animations", True,
                         "Animations toggled.", keywords="animations motion",
                         cap=cap_look)
        self.make_slider(pg, "Animation speed", "Higher = snappier (×0.25–×6)",
                         "hypr_anim_speed", 1, 24, 1.0, "",
                         keywords="animation speed duration", scale=0.25,
                         cap=cap_look)

        cap_op = pg.cap("Transparency")
        self.make_slider(pg, "Active window opacity", "Percent (10–100)",
                         "hypr_opacity_active", 10, 100, 100, "%",
                         keywords="opacity transparent active focus", scale=0.01,
                         cap=cap_op)
        self.make_slider(pg, "Inactive window opacity", "Percent (10–100)",
                         "hypr_opacity_inactive", 10, 100, 100, "%",
                         keywords="opacity transparent inactive unfocused",
                         scale=0.01, cap=cap_op)
        note = QLabel("Applied instantly via hyprctl keyword and re-applied at "
                      "every login. A manual Hyprland config reload clears them — "
                      "the Reload Hyprland button (appears here when you change "
                      "these) restores everything.", objectName="hint")
        pg.add(note)

    def _build_input(self):
        pg = self._new_page("input")
        cap_focus = pg.cap("Focus")
        fm_ctl = Chips(tuple(FOCUS_MODES), self.cur("hypr_follow_mouse", 0),
                       lambda v: self.set_values({"hypr_follow_mouse": v},
                                                 "Focus behavior updated."))
        fm_ctl.set_keywords("focus follows mouse sloppy click hover drag mode")
        self.make_row(pg, "Mouse focus mode", fm_ctl,
                      "Click · follow cursor · hover · hover while dragging",
                      "focus follow mouse sloppy click hover drag", cap_focus)

        cap_kb = pg.cap("Touchpad & pointer")
        self.make_toggle(pg, "Natural scrolling",
                         "Two-finger direction like macOS",
                         "hypr_natural_scroll", False, "Natural scroll updated.",
                         keywords="touchpad natural scroll reverse", cap=cap_kb)
        self.make_toggle(pg, "NumLock on startup", "", "hypr_numlock", False,
                         "NumLock preference updated.", keywords="numlock keypad",
                         cap=cap_kb)
        self.make_slider(pg, "Cursor size", "Pointer theme size in px (16–96)",
                         "hypr_cursor_size", 16, 96, 24, "px",
                         keywords="cursor pointer size mouse", cap=cap_kb)

    def _build_startup(self):
        pg = self._new_page("startup")
        cap_wel = pg.cap("Welcome screen")
        roww = QWidget()
        roww.keywords = "welcome first run greeting chime sound"
        rl = QHBoxLayout(roww)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        show_b = QPushButton("Show welcome now")
        show_b.setCursor(Qt.CursorShape.PointingHandCursor)
        show_b.clicked.connect(lambda: self.spawn_tool(
            "lynx-welcome", ["--show"], "Welcome opened."))
        seen_b = QPushButton("Skip next time")
        seen_b.setCursor(Qt.CursorShape.PointingHandCursor)
        seen_b.clicked.connect(lambda: (
            self.set_values({"welcome_seen": True}),
            self.status.setText("Welcome suppressed for future logins.")))
        test_b = QPushButton("Test startup sound")
        test_b.setCursor(Qt.CursorShape.PointingHandCursor)
        test_b.clicked.connect(self.test_sound)
        for b in (show_b, seen_b, test_b):
            rl.addWidget(b)
        rl.addStretch(1)
        pg.add(roww)
        self.make_toggle(pg, "Play startup chime",
                         "Plays with the welcome window",
                         "startup_sound", True, "Startup sound preference saved.",
                         keywords="sound chime audio startup", cap=cap_wel)

        cap_auto = pg.cap("Autostart commands")
        hint = QLabel("One shell command per line — runs at every login.",
                      objectName="hint")
        pg.add(hint)
        self.autostart_edit = QPlainTextEdit(objectName="autostart")
        self.autostart_edit.keywords = "autostart startup commands shell login"
        self.autostart_edit.setPlaceholderText("# e.g.\nnm-applet\nwaybar")
        cmds = load_settings().get("autostart", [])
        if isinstance(cmds, list):
            self.autostart_edit.setPlainText("\n".join(str(c) for c in cmds))
        self.autostart_edit.setFixedHeight(110)
        self.autostart_edit.textChanged.connect(self._autostart_safe_start)
        pg.add(self.autostart_edit)

    def _autostart_safe_start(self):
        self._autostart_timer.start()

    def test_sound(self):
        from lynx_welcome import play_sound

        ok = play_sound()
        self.status.setText("Playing chime…" if ok
                            else "No player found (need mpg123/ffplay/mpv).")

    def save_autostart(self):
        lines = [ln.strip() for ln in self.autostart_edit.toPlainText().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        self.set_values({"autostart": lines})
        self.status.setText(f"Saved {len(lines)} autostart command(s).")

    def spawn_tool(self, name: str, args: list[str], flash: str):
        import subprocess as sp

        target = os.path.expanduser(f"~/.local/bin/{name}")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              f"{name.replace('-', '_')}.py")
        cmd = ([target] if os.access(target, os.X_OK)
               else [sys.executable or "python3", script])
        sp.Popen([*cmd, *args], stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                 start_new_session=True)
        self.status.setText(flash)

    # ---- wallpaper (kept API) -----------------------------------------------
    def refresh_wallpapers(self):
        while self.wall_box.count():
            item = self.wall_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.wall_files.clear()
        seen = set()
        for d in WALLPAPER_DIRS:
            try:
                entries = sorted(os.listdir(os.path.expanduser(d)))
            except OSError:
                continue
            for name in entries:
                ext = os.path.splitext(name)[1].lower()
                full = os.path.abspath(os.path.join(os.path.expanduser(d), name))
                if ext in VIDEO_EXTS and full not in seen and os.path.isfile(full):
                    seen.add(full)
                    self.wall_files.append(full)
        active = get_wall_path()
        for path in self.wall_files:
            row = QPushButton(f"▶  {os.path.basename(path)}", objectName="wallrow")
            row.setCheckable(True)
            row.setChecked(path == active)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.setToolTip(path)
            row.clicked.connect(lambda _=False, p=path: self.pick_wallpaper(p))
            self.wall_box.addWidget(row)
        if not self.wall_files:
            empty = QLabel("No MP4/GIF files found yet — add some to "
                           "~/.local/share/lynxde/wallpapers", objectName="hint")
            self.wall_box.addWidget(empty)
        else:
            self.wall_holder.keywords = "live wallpaper video gif mp4 background " \
                + " ".join(os.path.basename(p).lower() for p in self.wall_files)
        self.apply_filter(self.search.text())

    def pick_wallpaper(self, path: str):
        st = load_settings()
        st["wall_path"] = path
        st["wall_enabled"] = True
        save_settings(st)
        self._run_manager(["set", path])
        for btn in self.wall_holder.findChildren(QPushButton, "wallrow"):
            btn.setChecked(btn.toolTip() == path)
        self.status.setText(f"Wallpaper: {os.path.basename(path)}")

    def browse_wallpaper(self):
        pat = "Videos (*.mp4 *.gif *.mkv *.webm *.mov *.avi);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Choose wallpaper",
                                              os.path.expanduser("~"), pat)
        if path:
            self.pick_wallpaper(path)

    def stop_wallpaper(self):
        st = load_settings()
        st["wall_enabled"] = False
        save_settings(st)
        self._run_manager(["stop"])
        for btn in self.wall_holder.findChildren(QPushButton, "wallrow"):
            btn.setChecked(False)
        self.status.setText("Live wallpaper stopped.")

    @staticmethod
    def _run_manager(args: list[str]):
        import subprocess as sp

        if not os.access(WALLPAPER_BIN, os.X_OK):
            fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "lynx_wallpaper.py")
            sp.Popen([sys.executable, fallback, *args],
                     stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)
            return
        sp.Popen([WALLPAPER_BIN, *args], stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                 start_new_session=True)

    # ---- scheme / side / style --------------------------------------------------
    def restyle(self):
        s = self.scheme
        panel_alpha = 110 if self.panel.blur.available() else 245
        extra = f"""
        #panel {{
            background: {_hex_to_rgba(s['bg'], panel_alpha)};
            border: 1px solid {s['surface_hi']};
            border-radius: 16px;
        }}
        #head, #foot {{ background: transparent; }}
        #nav {{ background: {_hex_to_rgba(s['bg'], 90)};
               border-right: 1px solid {s['surface_hi']}; }}
        QPushButton#navbtn {{
            color: {s['muted']}; background: transparent; border: none;
            border-radius: 9px; padding: 9px 12px; text-align: left;
            font-weight: 600; font-size: 12px;
        }}
        QPushButton#navbtn:hover {{ background: {s['surface']};
            color: {s['text']}; }}
        QPushButton#navbtn:checked {{ background: {s['accent']};
            color: {s['on_accent']}; }}
        #section {{
            color: {s['muted']}; font-size: 11px; font-weight: 700;
            letter-spacing: 0.4px;
        }}
        #row {{
            background: {_hex_to_rgba(s['surface'], 110)};
            border: 1px solid transparent; border-radius: 11px;
        }}
        #row:hover {{ border-color: {s['surface_hi']}; }}
        #rowtitle {{ color: {s['text']}; font-size: 12px; font-weight: 600;
            background: transparent; }}
        #rowdesc {{ color: {s['muted']}; font-size: 10px; background: transparent; }}
        QCheckBox {{
            color: {s['text']}; font-weight: 700; font-size: 11px;
            spacing: 7px; background: transparent;
        }}
        QCheckBox::indicator {{
            width: 34px; height: 19px; border-radius: 10px;
            background: {s['surface_hi']}; border: none;
        }}
        QCheckBox::indicator:checked {{ background: {s['accent']}; }}
        QPushButton#side {{ min-width: 96px; padding: 8px 12px;
            font-weight: 700; }}
        QPushButton#side:checked {{ background: {s['accent']};
            color: {s['on_accent']}; }}
        QPushButton#wallrow {{
            text-align: left; padding: 7px 12px; font-weight: 600;
        }}
        QPushButton#wallrow:checked {{ background: {s['accent']};
            color: {s['on_accent']}; }}
        QPushButton#wallaction {{ padding: 6px 12px; }}
        QSlider {{ background: transparent; height: 22px; }}
        QSlider::groove:horizontal {{
            height: 5px; border-radius: 3px; background: {s['surface_hi']};
        }}
        QSlider::sub-page:horizontal {{ background: {s['accent']};
            border-radius: 3px; }}
        QSlider::handle:horizontal {{
            width: 15px; margin: -6px 0; border-radius: 8px;
            background: {s['text']};
        }}
        QSlider::handle:horizontal:hover {{ background: {s['accent']}; }}
        #sliderval {{ color: {s['accent']}; font-weight: 800; font-size: 11px;
            min-width: 44px; background: transparent; }}
        QPlainTextEdit#autostart {{
            background: {s['surface']}; color: {s['text']};
            border: 1px solid {s['surface_hi']}; border-radius: 10px;
            padding: 8px; font-family: monospace; font-size: 11px;
        }}
        QPlainTextEdit#autostart:focus {{ border-color: {s['accent']}; }}
        QLineEdit#search {{
            background: {s['surface']};
            color: {s['text']};
            border: 1px solid {s['surface_hi']};
            border-radius: 9px;
            padding: 7px 11px;
            font-size: 12px;
        }}
        QLineEdit#search:focus {{ border-color: {s['accent']}; }}
        QScrollArea {{ background: transparent; border: none; }}
        #body {{ background: transparent; }}
        #hint {{ color: {s['muted']}; font-size: 11px; background: transparent; }}
        QPushButton#refreshbtn {{
            background: {s['accent']}; color: {s['on_accent']};
            border: none; border-radius: 10px;
            padding: 8px 16px; font-size: 12px; font-weight: 700;
        }}
        QPushButton#refreshbtn:hover {{
            background: {s['accent']}; border: 1px solid {s['text']};
        }}
        QPushButton#refreshbtn:disabled {{ opacity: 0.6; }}
        """
        self.setStyleSheet(build_style(s) + extra)

    # ---- search ------------------------------------------------------------------
    def apply_filter(self, text: str):
        q = text.strip().lower()
        first_hit = ""
        for pid, page in self.pages.items():
            hits = page.filter(q)
            self.nav_buttons[pid].setVisible(not q or hits > 0)
            if hits and not first_hit:
                first_hit = pid
        if not q:
            self.status.setText("")
            return
        if first_hit:
            self.show_page(first_hit)
            self.status.setText("")
        else:
            self.status.setText("No settings match that search.")

    def pick_scheme(self, key: str):
        st = load_settings()
        st["scheme"] = key
        save_settings(st)
        self.scheme = get_scheme(key)
        apply_scheme_colors(self.scheme)
        self.restyle()
        self.status.setText(f"Scheme set to {SCHEMES[key]['label']}.")

    def pick_side(self, side: str):
        st = load_settings()
        st["bar_side"] = side
        save_settings(st)
        self.status.setText(f"App bar moved to the {side} edge.")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-settings")
    app.setDesktopFileName("lynx-settings")
    app.setFont(QFont("Inter", 9))

    win = SettingsWindow()

    def on_external(_settings):
        new_scheme = get_scheme()
        if new_scheme["accent"] != win.scheme["accent"]:
            win.scheme = new_scheme
            win.restyle()
        win.refresh_wallpapers()

    watcher = SettingsWatcher(win)
    watcher.changed.connect(on_external)

    argv = sys.argv[1:]
    if "--set" in argv:
        st = dict(load_settings())
        for kv in argv[argv.index("--set") + 1:]:
            k, _, v = kv.partition("=")
            st[k] = v
        save_settings(st)
        apply_scheme_colors(get_scheme())
        apply_hypr_keywords(st)
        print("settings:", st)
        return 0

    if "--selftest" in argv:
        out_path = os.environ.get("LYNX_SELFTEST_OUT",
                                  "/tmp/opencode/lynx_settings.png")

        def run():
            win.show()
            win.pick_scheme("ocean")
            win.pick_accent("#f9e2af")
            win.pick_accent("")
            win.pick_scheme("lynx")
            for pid in win.pages:
                win.show_page(pid)
            win.search.setText("gaps")
            win.search.clear()
            win.show_page("windows")
            win.autostart_edit.setPlainText("nm-applet\n# comment\nwaybar")
            win.save_autostart()
            win.commit_engines()
            win.clear_history()
            win.reset_widget_positions()
            win.show_page("appearance")
            win.set_values({"hypr_rounding": 12})
            print(f"selftest: refresh button visible after hypr change: "
                  f"{win.refresh_btn.isVisible()}")
            win.reload_hyprland()

        def snap():
            win.grab().save(out_path)
            print(f"saved {out_path}")
            app.quit()

        QTimer.singleShot(50, run)
        QTimer.singleShot(900, snap)
        return app.exec()

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
