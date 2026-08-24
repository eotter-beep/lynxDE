#!/usr/bin/env python3
"""Lynx Launcher: SUPER + / command palette for lynxde.

A frosted-glass palette (lynxBlur backing, scheme-aware) listing every
installed application plus web-search actions for DuckDuckGo and Mwmbl.
Apps without a theme icon get a designed monogram tile — never a generic
placeholder glyph. Launch/search history is kept locally under
~/.config/lynxde/launcher.json ("Recent" section) and clearable with one
click from the palette footer.

Runs as a tiny daemon: the first launch listens on a private Unix socket,
every later `lynx-launcher` invocation just toggles visibility, so the
SUPER + / keybind opens/closes instantly with no startup cost.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import quote_plus

from PySide6.QtCore import (QPointF, QRectF, QSize, Qt, QTimer, QUrl, QEvent)
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFont,
                           QFontMetrics, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    SETTINGS_DIR,
    SettingsWatcher,
    store_search,
    terminal_prefix,
    get_scheme,
    load_settings,
)

STATE_PATH = os.path.join(SETTINGS_DIR, "launcher.json")
SOCK_NAME = f"lynx-launcher-{os.getuid()}.sock"

WIDTH = 640
VIEW_MAX = 450
ROW_H = 52
HEAD_H = 26
NOTE_H = 34
TILE = 34
ACTIONABLE = frozenset({"app", "web", "recent", "pkg"})

ENGINES = (
    ("duckduckgo", "DuckDuckGo", "https://duckduckgo.com/?q={}"),
    ("mwmbl", "Mwmbl", "https://mwmbl.org/search?q={}"),
)

_FIELD_CODE = re.compile(r"%[-]?[a-zA-Z%]")


# ---- .desktop scanning -----------------------------------------------------

def _clean_exec(exe: str) -> list[str]:
    exe = _FIELD_CODE.sub(lambda m: "%" if m.group(0) == "%%" else " ", exe)
    try:
        argv = shlex.split(exe)
    except ValueError:
        argv = exe.split()
    return argv


def _parse_desktop(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    kv: dict[str, str] = {}
    in_entry = False
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            in_entry = s == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        if "[" in k:                       # skip localized keys
            continue
        kv.setdefault(k, v.strip())
    if (kv.get("Type") != "Application"
            or kv.get("NoDisplay") in ("1", "true")
            or kv.get("Hidden") in ("1", "true")):
        return None
    exec_field = kv.get("Exec", "")
    argv = _clean_exec(exec_field)
    if not argv:
        return None
    try_exec = kv.get("TryExec")
    if try_exec and not shutil.which(try_exec):
        return None
    app_id = os.path.splitext(os.path.basename(path))[0]
    name = kv.get("Name") or app_id
    hay = " ".join((name, app_id, kv.get("GenericName", ""),
                    kv.get("Comment", ""), exec_field,
                    kv.get("Categories", "").replace(";", " "),
                    kv.get("Keywords", "").replace(";", " "))).lower()
    return {
        "id": app_id,
        "name": name,
        "argv": argv,
        "terminal": kv.get("Terminal") in ("1", "true"),
        "comment": kv.get("Comment", ""),
        "icon_name": kv.get("Icon", ""),
        "hay": hay,
    }


def scan_apps() -> list[dict]:
    dirs = []
    xdg_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    dirs.append(os.path.join(xdg_home, "applications"))
    for d in (os.environ.get("XDG_DATA_DIRS")
              or "/usr/local/share:/usr/share").split(":"):
        if d.strip():
            dirs.append(os.path.join(d.strip(), "applications"))
    seen: set[str] = set()
    apps: list[dict] = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".desktop"):
                continue
            aid = n[:-8]
            if aid in seen:
                continue
            app = _parse_desktop(os.path.join(d, n))
            if app is not None:
                seen.add(aid)
                apps.append(app)
    apps.sort(key=lambda a: a["name"].lower())
    return apps


def launch_app(app: dict):
    argv = list(app["argv"])
    if app["terminal"]:
        prefix = terminal_prefix()
        if prefix:
            argv = [*prefix, *argv]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---- fuzzy matching ----------------------------------------------------------

def _is_subseq(q: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in q)


def score_app(app: dict, q: str) -> int | None:
    name = app["name"].lower()
    words = re.split(r"[\s_\-\[\]()]+", name)
    if name == q:
        s = 1000
    elif name.startswith(q):
        s = 920
    elif any(w.startswith(q) for w in words):
        s = 850
    elif q in name:
        s = 720 - name.find(q)
    else:
        pos = app["hay"].find(q)
        if pos >= 0:
            s = 520
        elif len(q) >= 2 and _is_subseq(q, name):
            s = 380
        else:
            return None
    return s - min(len(name), 60)


# ---- history -------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_state(st: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_PATH)


def record_history(entry: dict):
    st = load_state()
    hist = [h for h in st.get("history", []) if h != entry]
    hist.insert(0, entry)
    st["history"] = hist[:20]
    save_state(st)


# ---- light theme --------------------------------------------------------------

INK = "#26262b"
MUTED_INK = "#8b8b94"
FIELD_BG = "#f2f2f4"
FIELD_LINE = "#e4e4e8"
PANEL_LINE = "#ececf1"
TILE_BG = "#f2f2f5"


class LauncherTheme(dict):
    """Fixed white-surface neutrals plus the active scheme's accent."""

    def __init__(self, scheme: dict):
        super().__init__(
            text=INK, muted=MUTED_INK,
            panel="#ffffff", panel_line=PANEL_LINE,
            field=FIELD_BG, field_line=FIELD_LINE,
            tile=TILE_BG, tile_line=QColor(0, 0, 0, 20),
            scrollbar=QColor(0, 0, 0, 45),
            accent=scheme["accent"], on_accent=scheme["on_accent"],
            red=scheme["red"],
        )


# ---- results canvas ------------------------------------------------------------

class ResultCanvas(QWidget):
    """Self-drawn result list: sections, rows, monogram tiles, scrollbar."""

    def __init__(self, win: "LauncherWindow"):
        super().__init__(win)
        self.win = win
        self.rows: list[dict] = []
        self.sel = -1
        self.offset = 0
        self.content_h = 0
        self._press_row: int | None = None
        self._icon_cache: dict[str, QPixmap | None] = {}
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        def font(px: int, *, bold=False, italic=False, demi=False) -> QFont:
            f = QFont(self.font())
            f.setPixelSize(px)
            if bold:
                f.setWeight(QFont.Weight.Bold)
            if demi:
                f.setWeight(QFont.Weight.DemiBold)
            f.setItalic(italic)
            return f

        self._f_name = font(14, demi=True)
        self._f_sub = font(11)
        self._f_head = font(10, bold=True)
        self._f_mono = font(15, bold=True)
        self._f_chip = font(11, bold=True)
        self._f_note = font(11, italic=True)

    # -- data ---------------------------------------------------------------
    def set_rows(self, rows: list[dict]):
        self.rows = rows
        y = 4
        for r in rows:
            r["_y"] = y
            r["_h"] = HEAD_H if r["kind"] == "header" else (
                NOTE_H if r["kind"] == "note" else ROW_H)
            y += r["_h"]
        self.content_h = y + 8
        actionable = [i for i, r in enumerate(rows) if r["kind"] in ACTIONABLE]
        if self.sel not in actionable:
            self.sel = actionable[0] if actionable else -1
        self.clamp_offset()
        self.update()

    def reset_icons(self):
        self._icon_cache.clear()

    def clamp_offset(self):
        max_off = max(0, self.content_h - self.height())
        self.offset = max(0, min(self.offset, max_off))

    def _order(self) -> list[int]:
        return [i for i, r in enumerate(self.rows) if r["kind"] in ACTIONABLE]

    def move_sel(self, delta: int):
        order = self._order()
        if not order:
            self.sel = -1
            self.update()
            return
        try:
            idx = order.index(self.sel)
        except ValueError:
            idx = 0 if delta > 0 else len(order) - 1
        else:
            idx = (idx + delta) % len(order)
        self.sel = order[idx]
        self.ensure_visible()
        self.update()

    def ensure_visible(self):
        if not (0 <= self.sel < len(self.rows)):
            return
        r = self.rows[self.sel]
        top, bottom = r["_y"], r["_y"] + r["_h"]
        if top < self.offset + 4:
            self.offset = top - 4
        elif bottom > self.offset + self.height() - 4:
            self.offset = bottom - self.height() + 4
        self.clamp_offset()

    def _row_at(self, y: float) -> int:
        cy = y + self.offset
        for i, r in enumerate(self.rows):
            if r["_y"] <= cy < r["_y"] + r["_h"]:
                return i if r["kind"] in ACTIONABLE else -1
        return -1

    # -- painting --------------------------------------------------------------
    def paintEvent(self, ev):
        s = self.win.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w = self.width()

        for i, r in enumerate(self.rows):
            y = r["_y"] - self.offset
            if y + r["_h"] < 0 or y > self.height():
                continue
            kind = r["kind"]
            if kind == "header":
                p.setFont(self._f_head)
                p.setPen(QColor(s["muted"]))
                p.drawText(QRectF(10, y, w - 20, HEAD_H),
                           Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter, r["label"])
            elif kind == "note":
                p.setFont(self._f_note)
                p.setPen(QColor(s["muted"]))
                p.drawText(QRectF(10, y, w - 20, NOTE_H),
                           Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter, r["label"])
            else:
                self._paint_row(p, s, i, r, y, w)

        if self.content_h > self.height() + 1:
            track_h = float(self.height())
            th = max(24.0, track_h * track_h / self.content_h)
            span = max(1.0, self.content_h - track_h)
            ty = (track_h - th) * (min(self.offset, span) / span)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(s["scrollbar"]))
            p.drawRoundedRect(QRectF(w - 7, ty + 2, 4, th - 4), 2, 2)

    def _tile_rect(self, rr: QRectF) -> QRectF:
        return QRectF(rr.left() + 9,
                      rr.top() + (rr.height() - TILE) / 2, TILE, TILE)

    def _paint_row(self, p: QPainter, s: dict, i: int, r: dict, y: float,
                   w: float):
        selected = i == self.sel
        rr = QRectF(6, y + 3, w - 18, ROW_H - 6)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(s["accent"]))
            p.drawRoundedRect(rr, 12, 12)
        tile = self._tile_rect(rr)

        if r["kind"] == "web":
            self._search_glyph(p, s, tile, selected)
        else:
            pm = self._app_pixmap(r)
            if pm is not None:
                p.drawPixmap(tile.toRect(), pm)
            else:
                self._monogram(p, s, tile, r, selected)

        tx = rr.left() + 9 + TILE + 13
        tw = rr.width() - (tx - rr.left()) - 42
        sub_col = QColor(s["muted"])
        if selected:
            sub_col = QColor(s["on_accent"])
            sub_col.setAlpha(200)
        p.setFont(self._f_name)
        p.setPen(QColor(s["on_accent"] if selected else s["text"]))
        fm = QFontMetrics(self._f_name)
        p.drawText(QRectF(tx, rr.top() + 4, tw, rr.height() * 0.55),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(r["name"], Qt.TextElideMode.ElideRight,
                                 int(tw)))
        sub = r.get("sub") or ""
        if sub:
            p.setFont(self._f_sub)
            p.setPen(sub_col)
            p.drawText(QRectF(tx, rr.top() + rr.height() * 0.52, tw,
                              rr.height() * 0.44),
                       Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter, sub)
        if selected:
            chip = QRectF(rr.right() - 30,
                          rr.top() + (rr.height() - 22) / 2, 22, 22)
            chip_fill = QColor(s["on_accent"])
            chip_fill.setAlpha(50)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(chip_fill)
            p.drawRoundedRect(chip, 7, 7)
            p.setFont(self._f_chip)
            p.setPen(QColor(s["on_accent"]))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, "↵")

    def _monogram(self, p: QPainter, s: dict, tile: QRectF, r: dict,
                  selected: bool):
        path = QPainterPath()
        path.addRoundedRect(tile, 10, 10)
        if selected:
            fill = QColor(s["on_accent"])
            fill.setAlpha(46)
            pen_col = QColor(s["on_accent"])
            pen_col.setAlpha(70)
            letter = QColor(s["on_accent"])
        else:
            fill = QColor(s["tile"])
            pen_col = QColor(s["tile_line"])
            letter = QColor(s["accent"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(path)
        pen = QPen(pen_col)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        name = r.get("name") or "?"
        ch = next((c for c in name if c.isalnum()), name[:1] or "?").upper()
        p.setFont(self._f_mono)
        p.setPen(letter)
        p.drawText(tile, Qt.AlignmentFlag.AlignCenter, ch)

    def _search_glyph(self, p: QPainter, s: dict, tile: QRectF,
                      selected: bool):
        path = QPainterPath()
        path.addRoundedRect(tile, 10, 10)
        if selected:
            fill = QColor(s["on_accent"])
            fill.setAlpha(46)
            stroke_col = QColor(s["on_accent"])
        else:
            fill = QColor(s["tile"])
            stroke_col = QColor(s["accent"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(path)
        cx, cy = tile.center().x() - 2, tile.center().y() - 2
        pen = QPen(stroke_col)
        pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 5.5, 5.5)
        p.drawLine(QPointF(cx + 4.2, cy + 4.2), QPointF(cx + 9.0, cy + 9.0))

    def _app_pixmap(self, r: dict) -> QPixmap | None:
        app = r.get("app") or self.win.by_id.get(r.get("id"))
        if app is None:
            return None
        key = app["id"]
        if key in self._icon_cache:
            return self._icon_cache[key]
        pm: QPixmap | None = None
        for cand in filter(None, (app.get("icon_name"), app["id"])):
            icon = QIcon(cand) if cand.startswith("/") \
                else QIcon.fromTheme(cand)
            if not icon.isNull():
                got = icon.pixmap(QSize(TILE * 2, TILE * 2))
                if not got.isNull():
                    pm = got
                    break
        self._icon_cache[key] = pm
        return pm

    # -- interaction -------------------------------------------------------------
    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d:
            self.offset += -56 if d > 0 else 56
            self.clamp_offset()
            self.update()
        e.accept()

    def mouseMoveEvent(self, e):
        row = self._row_at(e.position().y())
        if row != self.sel:
            self.sel = row
            self.update()

    def mousePressEvent(self, e):
        self._press_row = self._row_at(e.position().y())
        e.accept()

    def mouseReleaseEvent(self, e):
        row = self._row_at(e.position().y())
        if row >= 0 and row == self._press_row:
            self.win.activate_row(row)
        self._press_row = None
        e.accept()


# ---- window ----------------------------------------------------------------------

HINT_DEFAULT = "↑↓ select   ·   ↵ open   ·   esc dismiss"


class LauncherWindow(QWidget):
    def __init__(self, no_autohide: bool = False):
        super().__init__(None)
        self.no_autohide = no_autohide
        self.scheme = get_scheme()
        self.theme = LauncherTheme(self.scheme)
        self.apps: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self._screen_geom = None
        self.setWindowTitle("Lynx Launcher")
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        self.panel = QFrame(objectName="panel")
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand = QLabel("⬢ lynx launcher", objectName="brand")
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        lay.addLayout(brand_row)

        self.search = QLineEdit(objectName="palette")
        self.search.setPlaceholderText(
            "Search applications, packages or the web…")
        self.search.textChanged.connect(self.on_query_changed)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)

        self.canvas = ResultCanvas(self)
        lay.addWidget(self.canvas)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.hint = QLabel(HINT_DEFAULT, objectName="hint")
        foot.addWidget(self.hint)
        foot.addStretch(1)
        self.clear_btn = QPushButton("Clear history", objectName="ghost")
        self.clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_history)
        foot.addWidget(self.clear_btn)
        lay.addLayout(foot)

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(
            lambda: self.hint.setText(HINT_DEFAULT))

        self._store_rows: list[dict] = []
        self._store_q = ""
        self._store_cache: dict[str, list[dict]] = {}
        self._store_timer = QTimer(self)
        self._store_timer.setSingleShot(True)
        self._store_timer.setInterval(280)
        self._store_timer.timeout.connect(self.load_store)

        self.restyle()

    # -- styling ----------------------------------------------------------------
    def restyle(self):
        s = self.theme
        extra = f"""
        #panel {{
            background: {s['panel']};
            border: 1px solid {s['panel_line']};
            border-radius: 16px;
        }}
        #brand {{ color: {s['accent']}; font-weight: 800; font-size: 15px; }}
        QLineEdit#palette {{
            background: {s['field']}; color: {s['text']};
            border: 1px solid {s['field_line']}; border-radius: 10px;
            padding: 9px 13px; font-size: 13px;
        }}
        QLineEdit#palette:focus {{ border-color: {s['accent']}; }}
        #hint {{ color: {s['muted']}; font-size: 10px; font-weight: 600; }}
        QPushButton#ghost {{
            color: {s['muted']}; background: transparent; border: none;
            border-radius: 8px; padding: 3px 9px; font-size: 10px;
            font-weight: 700;
        }}
        QPushButton#ghost:hover {{ color: {s['red']};
            background: {s['field']}; }}
        """
        self.setStyleSheet(extra)
        self.canvas.update()

    # -- data ---------------------------------------------------------------------
    def rescan(self):
        self.apps = scan_apps()
        self.by_id = {a["id"]: a for a in self.apps}
        self.canvas.reset_icons()

    def rebuild(self):
        q = self.search.text().strip().lower()
        rows: list[dict] = []
        hist = load_state().get("history", [])
        if not q and hist:
            rows.append({"kind": "header", "label": "Recent"})
            for h in hist[:6]:
                if h.get("t") == "app":
                    live = self.by_id.get(h.get("id"))
                    rows.append({
                        "kind": "recent",
                        "entry": h,
                        "id": h.get("id"),
                        "name": (live or h).get("name", "?"),
                        "sub": "Application"
                        + ("" if live else "  ·  uninstalled"),
                    })
                elif h.get("t") == "web":
                    label = next((l for e, l, _u in ENGINES
                                  if e == h.get("e")), "Web")
                    rows.append({
                        "kind": "recent",
                        "entry": h,
                        "name": f"“{h.get('q', '')}”",
                        "sub": f"{label} · search",
                    })
        matched: list[tuple[int, dict]] = []
        if q:
            scored = ((score_app(a, q), a) for a in self.apps)
            matched = sorted((t for t in scored if t[0] is not None),
                             key=lambda t: (-t[0], t[1]["name"].lower()))[:40]
            if matched:
                rows.append({"kind": "header",
                             "label": f"Applications · {len(matched)}"})
            for _sc, a in matched:
                rows.append({"kind": "app", "app": a, "name": a["name"],
                             "sub": a["comment"][:90]})
        else:
            rows.append({"kind": "header",
                         "label": f"All applications · {len(self.apps)}"})
            for a in self.apps:
                rows.append({"kind": "app", "app": a, "name": a["name"],
                             "sub": a["comment"][:90]})
            if not self.apps:
                rows.append({"kind": "note",
                             "label": "No applications found"})
        raw = self.search.text().strip()
        if q and self._store_q == q and self._store_rows:
            rows.append({"kind": "header",
                         "label": f"Lynx Store · {len(self._store_rows)}"})
            for p in self._store_rows:
                mark = "✓ installed · " if p.get("installed") else ""
                rows.append({"kind": "pkg", "pkg": p["pkg"],
                             "name": f"{p['pkg']} {p.get('version', '')}",
                             "sub": (mark + p.get("desc", ""))[:110]})
        if raw:
            enabled = load_settings().get("web_engines")
            engines = [e for e in ENGINES if not isinstance(enabled, list)
                       or not enabled or e[0] in enabled]
            if not engines:
                engines = list(ENGINES)
            rows.append({"kind": "header", "label": "Search the web"})
            for eng, label, url in engines:
                rows.append({"kind": "web", "engine": eng, "q": raw,
                             "url": url, "name": f"Search on {label}",
                             "sub": f"“{raw}” · opens in your browser"})
        self.clear_btn.setVisible(bool(hist))
        self.canvas.set_rows(rows)
        view_h = min(VIEW_MAX, self.canvas.content_h)
        if self._screen_geom is not None:
            view_h = min(view_h, max(160, self._screen_geom.height() - 220))
        self.canvas.setFixedHeight(max(view_h, NOTE_H))
        target_h = 14 + 24 + 10 + 40 + 10 + view_h + 10 + 26 + 12 + 4
        self.resize(WIDTH, int(target_h))

    # -- actions ----------------------------------------------------------------------
    def on_query_changed(self, _t: str = ""):
        self.rebuild()
        self._store_timer.start()

    def load_store(self):
        q = self.search.text().strip().lower()
        if len(q) < 2:
            had = bool(self._store_rows)
            self._store_q = ""
            self._store_rows = []
            if had:
                self.rebuild()
            return
        rows = self._store_cache.get(q)
        if rows is None:
            rows = store_search(q)
            self._store_cache[q] = rows
            while len(self._store_cache) > 48:
                self._store_cache.pop(next(iter(self._store_cache)))
        self._store_q = q
        self._store_rows = rows
        self.rebuild()

    def install_pkg(self, pkg: str):
        prefix = terminal_prefix()
        if not prefix:
            self.flash("No terminal found — cannot run the installer")
            return
        script = ("sudo pacman -S --needed " + shlex.quote(pkg)
                  + "; echo; read -n 1 -s -r -p 'Done — press any key to close '")
        subprocess.Popen([*prefix, "sh", "-c", script],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         start_new_session=True)
        self.hide()

    def activate_row(self, i: int):
        if not (0 <= i < len(self.canvas.rows)):
            return
        r = self.canvas.rows[i]
        kind = r["kind"]
        if kind == "app":
            a = r["app"]
            record_history({"t": "app", "id": a["id"], "name": a["name"]})
            launch_app(a)
            self.hide()
        elif kind == "web":
            url = r["url"].format(quote_plus(r["q"]))
            record_history({"t": "web", "e": r["engine"], "q": r["q"]})
            QDesktopServices.openUrl(QUrl(url))
            self.hide()
        elif kind == "pkg":
            self.install_pkg(r["pkg"])
        elif kind == "recent":
            entry = r["entry"]
            if entry.get("t") == "app":
                live = self.by_id.get(entry.get("id"))
                if live is None:
                    self.flash(f"“{entry.get('name')}” is no longer installed")
                    return
                record_history(dict(entry))
                launch_app(live)
                self.hide()
            else:
                q = entry.get("q", "")
                eng, _label, url = next(
                    ((e, l, u) for e, l, u in ENGINES
                     if e == entry.get("e")), ENGINES[0])
                record_history({"t": "web", "e": eng, "q": q})
                QDesktopServices.openUrl(QUrl(url.format(quote_plus(q))))
                self.hide()

    def clear_history(self):
        st = load_state()
        st["history"] = []
        save_state(st)
        self.rebuild()
        self.flash("Search history cleared")

    def flash(self, msg: str):
        self.hint.setText(msg)
        self._flash_timer.start(1800)

    # -- open/close --------------------------------------------------------------------
    def show_palette(self):
        self.rescan()
        self.scheme = get_scheme()
        self.theme = LauncherTheme(self.scheme)
        self.restyle()
        screen = QApplication.screenAt(QCursor.pos()) \
            or QApplication.primaryScreen()
        geom = screen.availableGeometry() if screen else None
        self._screen_geom = geom
        self.search.clear()
        self.canvas.offset = 0
        self.rebuild()
        if geom is not None:
            x = geom.x() + (geom.width() - self.width()) // 2
            y = geom.y() + int(geom.height() * 0.30) - self.height() // 2
            self.move(max(geom.x() + 12, x), max(geom.y() + 12, y))
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show_palette()

    # -- events --------------------------------------------------------------------------
    def eventFilter(self, obj, ev):
        if obj is self.search and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.activate_row(self.canvas.sel)
                return True
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Tab):
                self.canvas.move_sel(1)
                return True
            if k in (Qt.Key.Key_Up, Qt.Key.Key_Backtab):
                self.canvas.move_sel(-1)
                return True
            if k == Qt.Key.Key_PageDown:
                self.canvas.move_sel(6)
                return True
            if k == Qt.Key.Key_PageUp:
                self.canvas.move_sel(-6)
                return True
            if k == Qt.Key.Key_Escape:
                self.hide()
                return True
        return super().eventFilter(obj, ev)

    def event(self, e):
        if (e.type() == QEvent.Type.WindowDeactivate and self.isVisible()
                and not self.no_autohide):
            QTimer.singleShot(0, self.hide)
        return super().event(e)


# ---- single-instance toggle --------------------------------------------------------

def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, SOCK_NAME)


def try_toggle_existing() -> bool:
    sock = QLocalSocket()
    sock.connectToServer(socket_path())
    if not sock.waitForConnected(150):
        return False
    sock.write(b"toggle\n")
    sock.flush()
    sock.waitForBytesWritten(200)
    sock.disconnectFromServer()
    return True


def serve_socket(win: LauncherWindow):
    srv = QLocalServer(win)
    QLocalServer.removeServer(socket_path())

    def on_conn():
        conn = srv.nextPendingConnection()
        if conn is None:
            return

        def read_all():
            conn.readAll()
            win.toggle()
            conn.disconnectFromServer()

        conn.readyRead.connect(read_all)
        QTimer.singleShot(400, conn.deleteLater)

    srv.newConnection.connect(on_conn)
    srv.listen(socket_path())


# ---- main -------------------------------------------------------------------------

def main() -> int:
    args = set(sys.argv[1:])
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-launcher")
    app.setDesktopFileName("lynx-launcher")
    app.setFont(QFont("Inter", 9))

    if "--selftest" in args:
        out_dir = os.environ.get("LYNX_SELFTEST_DIR", "/tmp/opencode")
        os.makedirs(out_dir, exist_ok=True)
        win = LauncherWindow(no_autohide=True)
        watcher = SettingsWatcher(win)
        watcher.changed.connect(
            lambda _st: (setattr(win, "scheme", get_scheme()),
                         setattr(win, "theme", LauncherTheme(win.scheme)),
                         win.restyle()))
        win.show_palette()

        def snap_empty():
            win.grab().save(os.path.join(out_dir, "lynx_launcher_empty.png"))
            win.search.setText("fi")

        def snap_query():
            win.grab().save(os.path.join(out_dir, "lynx_launcher_query.png"))
            print(f"saved {out_dir}/lynx_launcher_*.png")
            app.quit()

        QTimer.singleShot(500, snap_empty)
        QTimer.singleShot(900, snap_query)
        return app.exec()

    standalone = ("--standalone" in args
                  or os.environ.get("LYNX_LAUNCHER_STANDALONE") == "1")
    if not standalone and try_toggle_existing():
        return 0

    win = LauncherWindow()
    if not standalone:
        serve_socket(win)
    # Keybind invocations show right away; only the session autostart
    # (explicit --autostart) warms the daemon up hidden.
    if "--autostart" not in args or "--show" in args:
        win.show_palette()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
