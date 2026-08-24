#!/usr/bin/env python3
"""Lynx desktop widgets: Lynx Clock + OpenStreetMap panel, hosted by the wallpaper layer."""

from __future__ import annotations

import datetime
import math
import os
import subprocess
import sys
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtNetwork import (QNetworkAccessManager, QNetworkReply,
                               QNetworkRequest)
from PySide6.QtWidgets import QWidget

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    get_scheme,
    load_settings,
    save_settings,
    svg_pixmap,
)

TILE = 256
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_UA = b"lynxde-widgets/1.0 (Wayland desktop environment)"
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "lynxde", "osm")
MIN_ZOOM, MAX_ZOOM = 2, 18
MAX_LAT = 85.05112878


def deg2num(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(math.radians(lat)) +
                        1.0 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def num2deg(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def clamp_lat(lat: float) -> float:
    return max(-MAX_LAT, min(MAX_LAT, lat))


def clamp_lon(lon: float) -> float:
    return max(-180.0, min(180.0, lon))


def tinted(hex_col: str, alpha: int) -> QColor:
    c = QColor(hex_col)
    c.setAlpha(alpha)
    return c


class WidgetBase(QWidget):
    WID = ""
    LABEL = ""
    DEFAULT_SIZE = (200, 200)
    ANCHOR = "topright"
    MARGIN = (24, 70)

    def __init__(self, canvas: "DesktopCanvas"):
        super().__init__(canvas)
        self.canvas = canvas
        self.user_placed = False
        self._press_gp = None
        self._press_pos = None
        self.setMouseTracking(False)

    def scheme(self) -> dict:
        return self.canvas.scheme

    def place_default(self):
        dw, dh = self.DEFAULT_SIZE
        mr, mv = self.MARGIN
        x = self.parentWidget().width() - dw - mr
        y = mv if self.ANCHOR == "topright" else \
            self.parentWidget().height() - dh - mv
        self.setGeometry(max(8, int(x)), max(8, int(y)), dw, dh)

    def restore_or_place(self, entry: dict):
        x, y = entry.get("x"), entry.get("y")
        if isinstance(x, int) and isinstance(y, int):
            self.user_placed = True
            self.setGeometry(x, y, *self.DEFAULT_SIZE)
        else:
            self.place_default()

    def save_pos(self):
        st = load_settings()
        st.setdefault("widgets", {}).setdefault(self.WID, {})["x"] = self.x()
        st["widgets"][self.WID]["y"] = self.y()
        save_settings(st)
        self.user_placed = True

    def paintEvent(self, ev):
        s = self.scheme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1,
                                   self.height() - 1), 14, 14)
        p.setPen(QPen(QColor(s["surface_hi"]), 1))
        p.setBrush(tinted(s["surface"], 235))
        p.drawPath(path)

    def mousePressEvent(self, e):
        self.canvas.close_menu()
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_gp = e.globalPosition().toPoint()
            self._press_pos = self.pos()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_gp is None:
            return
        delta = e.globalPosition().toPoint() - self._press_gp
        par = self.parentWidget()
        np = self._press_pos + delta
        np.setX(max(-40, min(par.width() - self.width() + 40, np.x())))
        np.setY(max(0, min(par.height() - self.height(), np.y())))
        self.move(np)
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press_gp is not None:
            self._press_gp = None
            self.save_pos()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.canvas.open_menu(
            [{"kind": "action",
              "label": f"Remove “{self.LABEL}”",
              "icon": svg_pixmap("Widgets.svg", 16,
                                 bg=self.scheme()["surface"]),
              "on": lambda: self.canvas.toggle_widget(self.WID, False)}],
            e.globalPos())


class ClockWidget(WidgetBase):
    WID = "clock"
    LABEL = "Lynx Clock"
    DEFAULT_SIZE = (196, 244)
    ANCHOR = "topright"
    MARGIN = (24, 70)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setToolTip("Lynx Clock")
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        s = self.scheme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = datetime.datetime.now()

        w = self.width()
        cx, cy, r = w / 2.0, 100.0, 84.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(s["surface"]).darker(118))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(tinted(s["surface_hi"], 255), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)

        sec_angle = now.second * 6.0
        min_angle = now.minute * 6.0 + now.second * 0.1
        hour_angle = (now.hour % 12) * 30.0 + now.minute * 0.5

        for i in range(60):
            p.save()
            p.translate(cx, cy)
            p.rotate(i * 6.0)
            major = i % 5 == 0
            p.setPen(QPen(QColor(s["text"]) if major
                          else tinted(s["muted"], 170),
                          2.4 if major else 1.1))
            ln = 9 if major else 4
            p.drawLine(QPointF(0, -(r - 6)), QPointF(0, -(r - 6 - ln)))
            p.restore()

        def hand(angle: float, length: float, width: float, col: str):
            p.save()
            p.translate(cx, cy)
            p.rotate(angle)
            pen = QPen(QColor(col))
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(0, length * 0.16), QPointF(0, -length))
            p.restore()

        hand(hour_angle, r * 0.50, 5.0, s["text"])
        hand(min_angle, r * 0.76, 3.4, s["text"])
        hand(sec_angle, r * 0.86, 1.6, s["accent"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(s["accent"]))
        p.drawEllipse(QPointF(cx, cy), 4.0, 4.0)
        p.setBrush(QColor(s["on_accent"]))
        p.drawEllipse(QPointF(cx, cy), 1.7, 1.7)

        f = QFont(self.font())
        f.setPixelSize(27)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(s["text"]))
        p.drawText(QRectF(0, r * 2 + 22, w, 34),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   now.strftime("%H:%M"))
        f2 = QFont(self.font())
        f2.setPixelSize(13)
        p.setFont(f2)
        p.setPen(QColor(s["muted"]))
        p.drawText(QRectF(0, r * 2 + 54, w, 22),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   now.strftime("%A, %d %B"))


class MapWidget(WidgetBase):
    WID = "osm"
    LABEL = "OpenStreetMap"
    DEFAULT_SIZE = (330, 260)
    ANCHOR = "bottomright"
    MARGIN = (24, 80)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.lat = 48.8566
        self.lon = 2.3522
        self.zoom = 11
        self._tiles: dict[tuple[int, int, int], QPixmap] = {}
        self._failed: dict[tuple[int, int, int], float] = {}
        self._inflight: dict[object, tuple[int, int, int]] = {}
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("OpenStreetMap\nDrag to pan · Scroll to zoom")

        self.nam = QNetworkAccessManager(self)
        self.nam.finished.connect(self._on_reply)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self.save_view)

    def restore_view(self, entry: dict):
        try:
            lat = float(entry.get("lat", self.lat))
            lon = float(entry.get("lon", self.lon))
            zoom = int(entry.get("zoom", self.zoom))
        except (TypeError, ValueError):
            return
        self.lat, self.lon = clamp_lat(lat), clamp_lon(lon)
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    def save_view(self):
        st = load_settings()
        e = st.setdefault("widgets", {}).setdefault(self.WID, {})
        e["lat"], e["lon"], e["zoom"] = round(self.lat, 6), round(self.lon, 6), self.zoom
        save_settings(st)

    def _queue_save(self):
        self._save_timer.start()

    def _center_px(self) -> tuple[float, float]:
        gx, gy = deg2num(self.lat, self.lon, self.zoom)
        return gx * TILE, gy * TILE

    def _latlon_at(self, lp: QPointF) -> tuple[float, float]:
        gx, gy = deg2num(self.lat, self.lon, self.zoom)
        px = gx + (lp.x() - self.width() / 2.0) / TILE
        py = gy + (lp.y() - self.height() / 2.0) / TILE
        return num2deg(px, py, self.zoom)

    def _clamp_center(self):
        self.lat = clamp_lat(self.lat)
        self.lon = clamp_lon(self.lon)

    def _tile(self, z: int, x: int, y: int) -> QPixmap | None:
        key = (z, x, y)
        pm = self._tiles.get(key)
        if pm is not None:
            return pm
        path = os.path.join(CACHE_DIR, str(z), str(x), f"{y}.png")
        if os.path.isfile(path):
            pm = QPixmap(path)
            if not pm.isNull():
                self._remember(pm, key)
                return pm
        fail = self._failed.get(key)
        if fail is not None and time.monotonic() - fail < 30.0:
            return None
        if key in self._inflight.values():
            return None
        req = QNetworkRequest(QUrl(OSM_TILE_URL.format(z=z, x=x, y=y)))
        req.setRawHeader(b"User-Agent", OSM_UA)
        req.setTransferTimeout(8000)
        reply = self.nam.get(req)
        self._inflight[reply] = key
        return None

    def _remember(self, pm: QPixmap, key):
        if len(self._tiles) > 180:
            self._tiles.clear()
        self._tiles[key] = pm

    def _on_reply(self, reply):
        key = self._inflight.pop(reply, None)
        if key is None:
            return
        data = bytes(reply.readAll())
        err = reply.error()
        reply.deleteLater()
        if err != QNetworkReply.NetworkError.NoError or not data:
            print(f"lynx-osm: tile {key} failed "
                  f"({err}: {reply.errorString()}, {len(data)} bytes)",
                  file=sys.stderr)
            self._failed[key] = time.monotonic()
            return
        self._failed.pop(key, None)
        path = os.path.join(CACHE_DIR, str(key[0]), str(key[1]))
        try:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, f"{key[2]}.png"), "wb") as f:
                f.write(data)
        except OSError:
            pass
        pm = QPixmap()
        if pm.loadFromData(data):
            self._remember(pm, key)
            self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        s = self.scheme()
        p = QPainter(self)
        p.setClipRect(QRectF(1, 1, self.width() - 2, self.height() - 2))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w, h = self.width(), self.height()
        n = 1 << self.zoom
        ccx, ccy = self._center_px()
        ox, oy = ccx - w / 2.0, ccy - h / 2.0
        tx0, tx1 = int(math.floor(ox / TILE)), int(math.floor((ox + w) / TILE))
        ty0, ty1 = int(math.floor(oy / TILE)), int(math.floor((oy + h) / TILE))

        placeholder = QColor(s["surface"]).darker(125)
        for ty in range(ty0, ty1 + 1):
            if ty < 0 or ty >= n:
                continue
            for tx in range(tx0, tx1 + 1):
                dst = QRectF(tx * TILE - ox, ty * TILE - oy, TILE, TILE)
                wx = ((tx % n) + n) % n
                pm = self._tile(self.zoom, wx, ty)
                if pm is not None:
                    p.drawPixmap(dst, pm, QRectF(0, 0, TILE, TILE))
                else:
                    p.fillRect(dst, placeholder)

        label = "© OpenStreetMap contributors"
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(label) + 12
        box = QRectF(w - tw - 5, h - fm.height() - 9, tw, fm.height() + 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("#e8e8ef"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_gp = e.globalPosition().toPoint()
            self._start_lat, self._start_lon = self.lat, self.lon
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_gp is None or not hasattr(self, "_start_lat"):
            return
        d = e.globalPosition().toPoint() - self._press_gp
        gx, gy = deg2num(self._start_lat, self._start_lon, self.zoom)
        self.lat, self.lon = num2deg(gx - d.x() / TILE, gy - d.y() / TILE,
                                     self.zoom)
        self._clamp_center()
        self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press_gp is not None:
            self._press_gp = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._queue_save()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d == 0:
            return
        nz = self.zoom + (1 if d > 0 else -1)
        nz = max(MIN_ZOOM, min(MAX_ZOOM, nz))
        if nz == self.zoom:
            return
        lp = e.position()
        anchor = self._latlon_at(lp)
        self.zoom = nz
        gx, gy = deg2num(anchor[0], anchor[1], nz)
        px = gx - (lp.x() - self.width() / 2.0) / TILE
        py = gy - (lp.y() - self.height() / 2.0) / TILE
        self.lat, self.lon = num2deg(px, py, nz)
        self._clamp_center()
        self.update()
        self._queue_save()


class DesktopMenu(QWidget):
    """Compact self-drawn menu living inside the canvas: exact cursor
    positioning and sizing, immune to layer-shell popup quirks."""

    ROW_H = 34
    HEADER_H = 28
    SEP_H = 9
    SIDE_PAD = 12

    def __init__(self, canvas: "DesktopCanvas", entries: list[dict],
                 local_pos):
        super().__init__(canvas)
        self.canvas = canvas
        self.entries = entries
        self.hover = -1
        self.press_row = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._row_font = QFont(self.font())
        self._row_font.setPixelSize(13)
        self._head_font = QFont(self.font())
        self._head_font.setPixelSize(10)
        fm = QFontMetrics(self._row_font)
        width = 0
        for e in entries:
            if e.get("label"):
                width = max(width, fm.horizontalAdvance(e["label"]))
        self._icon_w = 22 if any(e.get("icon") for e in entries) else 0
        width += self.SIDE_PAD * 2 + self._icon_w + (26 if any(
            e.get("kind") == "action" for e in entries) else 0) + 14
        width = max(width, 190)

        height = 8
        for e in entries:
            height += {"header": self.HEADER_H,
                       "sep": self.SEP_H}.get(e["kind"], self.ROW_H)

        x = int(local_pos.x())
        y = int(local_pos.y())
        par = canvas
        x = min(max(4, x), max(4, par.width() - width - 4))
        y = min(max(4, y), max(4, par.height() - height - 4))
        self.setGeometry(x, y, width, min(height, par.height() - 8))

    def _interactive(self) -> list[int]:
        return [i for i, e in enumerate(self.entries) if e["kind"] == "action"]

    def _row_at(self, pos) -> int:
        y = 8.0
        for i, e in enumerate(self.entries):
            h = {"header": self.HEADER_H,
                 "sep": self.SEP_H}.get(e["kind"], self.ROW_H)
            if e["kind"] == "action" and y <= pos.y() < y + h:
                return i
            y += h
        return -1

    def paintEvent(self, ev):
        s = self.canvas.scheme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, self.width() - 1,
                                   self.height() - 1), 12, 12)
        p.setPen(QPen(tinted(s["surface_hi"], 255), 1))
        p.setBrush(tinted(s["surface"], 246))
        p.drawPath(path)
        p.setClipPath(path)

        y = 8.0
        for i, e in enumerate(self.entries):
            kind = e["kind"]
            if kind == "sep":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(tinted(s["surface_hi"], 140))
                p.drawRect(QRectF(self.SIDE_PAD, y + self.SEP_H / 2 - 0.5,
                                  self.width() - self.SIDE_PAD * 2, 1))
                y += self.SEP_H
                continue
            row = QRectF(5, y + 2, self.width() - 10,
                         {"header": self.HEADER_H - 4,
                          }.get(kind, self.ROW_H - 4))
            if kind == "action" and i == self.hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(s["accent"]))
                p.drawRoundedRect(row, 8, 8)
            if kind == "header":
                f = self._head_font
                col = s["muted"]
            elif i == self.hover:
                f = self._row_font
                col = s["on_accent"]
            else:
                f = self._row_font
                col = s["text"]
            tx = self.SIDE_PAD
            if e.get("checked"):
                p.setFont(self._row_font)
                p.setPen(QColor(s["on_accent"] if i == self.hover
                                else s["accent"]))
                p.drawText(QRectF(tx - 2, row.y(), 20, row.height()),
                           Qt.AlignmentFlag.AlignVCenter, "✓")
            tx += self._icon_w
            if e.get("icon"):
                pm = e["icon"]
                p.drawPixmap(QRectF(tx - 18, row.y() + (row.height()
                                                    - pm.height()) / 2,
                                    pm.width(), pm.height()),
                             pm, QRectF(pm.rect()))
            p.setFont(f)
            p.setPen(QColor(col))
            p.drawText(QRectF(tx, row.y(), self.width() - tx - self.SIDE_PAD,
                              row.height()),
                       Qt.AlignmentFlag.AlignVCenter, e.get("label", ""))
            y += {"header": self.HEADER_H,
                  "sep": self.SEP_H}.get(kind, self.ROW_H)

    def mouseMoveEvent(self, e):
        row = self._row_at(e.position())
        if row != self.hover:
            self.hover = row
            self.update()

    def mousePressEvent(self, e):
        self.press_row = self._row_at(e.position())
        e.accept()

    def mouseReleaseEvent(self, e):
        row = self._row_at(e.position())
        if row >= 0 and row == self.press_row:
            entry = self.entries[row]
            cb = entry.get("on")
            self.canvas.close_menu()
            if cb:
                if "checked" in entry:
                    cb(not bool(entry["checked"]))
                else:
                    cb()
        self.press_row = None
        e.accept()

    def leaveEvent(self, e):
        self.hover = -1
        self.update()


class DesktopCanvas(QWidget):
    """Transparent desktop overlay: hosts widgets, owns the right-click menu."""

    CATALOG = ((ClockWidget.WID, ClockWidget.LABEL, ClockWidget),
               (MapWidget.WID, MapWidget.LABEL, MapWidget))

    def __init__(self, wall):
        super().__init__(wall)
        self.wall = wall
        self.widgets: dict[str, WidgetBase] = {}
        self._menu: DesktopMenu | None = None
        self.scheme = get_scheme()

    def restyle(self, scheme: dict):
        self.scheme = scheme
        for w in self.widgets.values():
            w.update()

    def resizeEvent(self, ev):
        for wid, w in self.widgets.items():
            if not w.user_placed:
                w.place_default()

    def apply_state(self):
        st = load_settings().get("widgets", {})
        for wid, _label, cls in self.CATALOG:
            enabled = bool(st.get(wid, {}).get("enabled"))
            if enabled and wid not in self.widgets:
                self._spawn(wid, cls, st.get(wid, {}))
            elif not enabled and wid in self.widgets:
                self.widgets.pop(wid).deleteLater()

    def _spawn(self, wid: str, cls, entry: dict):
        w = cls(self)
        if wid == MapWidget.WID:
            w.restore_view(entry)
        w.restore_or_place(entry)
        w.show()
        self.widgets[wid] = w

    def toggle_widget(self, wid: str, on: bool):
        st = load_settings()
        st.setdefault("widgets", {}).setdefault(wid, {})["enabled"] = bool(on)
        save_settings(st)
        self.apply_state()

    def enable_for_selftest(self):
        st = {"widgets": {
            ClockWidget.WID: {"enabled": True},
            MapWidget.WID: {"enabled": True},
        }}
        for wid, _label, cls in self.CATALOG:
            if wid not in self.widgets:
                self._spawn(wid, cls, st["widgets"][wid])

    def open_settings(self):
        target = os.path.expanduser("~/.local/bin/lynx-settings")
        if os.access(target, os.X_OK):
            cmd = [target]
        else:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "lynx_settings.py")
            cmd = [sys.executable or "python3", script]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def open_menu(self, entries: list[dict], global_pos):
        self.close_menu()
        self._menu = DesktopMenu(self, entries, self.mapFromGlobal(global_pos))
        self._menu.show()
        self._menu.raise_()

    def close_menu(self):
        menu = getattr(self, "_menu", None)
        if menu is not None:
            menu.deleteLater()
            self._menu = None

    def mousePressEvent(self, e):
        self.close_menu()
        e.accept()

    def contextMenuEvent(self, e):
        s = self.scheme
        st = load_settings().get("widgets", {})
        entries = [{"kind": "header", "label": "WIDGETS",
                    "icon": svg_pixmap("Widgets.svg", 16, bg=s["surface"])}]
        for wid, label, _cls in self.CATALOG:
            entries.append({
                "kind": "action",
                "label": label,
                "checked": bool(st.get(wid, {}).get("enabled")),
                "on": lambda checked, k=wid: self.toggle_widget(k, bool(checked)),
            })
        entries.append({"kind": "sep"})
        entries.append({"kind": "action", "label": "Settings",
                        "icon": svg_pixmap("Settings.svg", 17, bg=s["surface"]),
                        "on": self.open_settings})
        self.open_menu(entries, e.globalPos())
        e.accept()
