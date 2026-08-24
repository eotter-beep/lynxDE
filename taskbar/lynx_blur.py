#!/usr/bin/env python3
"""lynxBlur — lynxde's native frosted-glass blur, no compositor tricks needed.

Blur by resolution loss, fed by our own wallpaper process: lynx-wallpaper
already decodes every video frame, so it streams a tiny low-res snapshot
of the whole desktop (all screens composited) to a shared file in
XDG_RUNTIME_DIR — one write per decoded frame, coalesced to ~25 fps with
a 1 s heartbeat so paused video never looks broken. lynxBlur watches that
file's timestamp, crops the region behind the widget, shrinks it by
~radius/2 (smooth filtering -> soft glass) and blows it back up — every
new frame, unlimited, no cadence freezes. Each result replaces the single
cached frame in RAM, so memory stays flat no matter how long it runs;
repaints remain a single drawPixmap with no screen grabs or subprocesses.

If no fresh shared frame exists (wallpaper stopped), paint() returns False
and callers keep their plain translucent fill. Nothing else to go wrong.

Usage:
    from lynx_blur import LynxBlur

    self.blur = LynxBlur.attach(widget, radius=8)  # that's it

    # in paintEvent, first thing:
    if not self.blur.paint(painter, corner=14):
        painter.setBrush(fallback_color)           # no live backdrop

Publishing (done by lynx-wallpaper):
    publish_backdrop([(screen_geometry, frame_image_or_None), ...])
"""

from __future__ import annotations

import os
import struct
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap

BACKDROP_MAGIC = b"LNXB"
BACKDROP_VERSION = 1
BACKDROP_W = 256                      # shared snapshot width in px
_BACKDROP_HDR = struct.Struct("<4sIiiiiiiq")  # magic, ver, w, h, bbox x/y/w/h, ts_ns
STALE_NS = 3_000_000_000              # older than this -> unavailable


def backdrop_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"lynxde-backdrop-{os.getuid()}.bin")


def publish_backdrop(entries, out_w: int = BACKDROP_W) -> bool:
    """Composite per-screen frames into the shared desktop snapshot.

    entries: iterable of (QRect screen_geometry, QImage | None latest_frame).
    Cheap by design: runs at ~2 Hz on a 256px-wide canvas inside the
    wallpaper daemon, which already owns decoded frames.
    """
    rects = [r for r, _img in entries if r is not None and not r.isEmpty()]
    if not rects:
        return False
    bx = min(r.x() for r in rects)
    by = min(r.y() for r in rects)
    br = max(r.right() for r in rects)
    bb = max(r.bottom() for r in rects)
    bw, bh = br - bx + 1, bb - by + 1

    out_h = max(1, round(bh * out_w / bw))
    img = QImage(out_w, out_h, QImage.Format.Format_RGBA8888)
    img.fill(QColor(17, 17, 25))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    sx = out_w / bw
    sy = out_h / bh
    for geo, frame in entries:
        if geo is None or geo.isEmpty() or frame is None or frame.isNull():
            continue
        slot = QRect(round((geo.x() - bx) * sx), round((geo.y() - by) * sy),
                     max(1, round(geo.width() * sx)),
                     max(1, round(geo.height() * sy)))
        iw, ih = frame.width(), frame.height()
        scale = max(slot.width() / iw, slot.height() / ih)   # cover-fit
        dw, dh = iw * scale, ih * scale
        painter.drawImage(QRectF(slot.x() - (dw - slot.width()) / 2,
                                 slot.y() - (dh - slot.height()) / 2,
                                 dw, dh), frame)
    painter.end()

    payload = bytes(img.constBits())
    if len(payload) != out_w * out_h * 4:
        return False
    blob = _BACKDROP_HDR.pack(BACKDROP_MAGIC, BACKDROP_VERSION,
                              out_w, out_h, bx, by, bw, bh,
                              time.time_ns()) + payload
    path = backdrop_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _read_backdrop() -> tuple[QImage, QRect] | None:
    """Latest shared snapshot: (image, virtual-desktop bbox in global px)."""
    try:
        with open(backdrop_path(), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < _BACKDROP_HDR.size:
        return None
    magic, ver, w, h, bx, by, bw, bh, ts = _BACKDROP_HDR.unpack_from(data)
    if (magic != BACKDROP_MAGIC or ver != BACKDROP_VERSION
            or w <= 0 or h <= 0 or bw <= 0 or bh <= 0
            or len(data) < _BACKDROP_HDR.size + w * h * 4
            or time.time_ns() - ts > STALE_NS):
        return None
    buf = data[_BACKDROP_HDR.size:_BACKDROP_HDR.size + w * h * 4]
    img = QImage(buf, w, h, w * 4, QImage.Format.Format_RGBA8888)
    if img.isNull():
        return None
    return img.copy(), QRect(bx, by, bw, bh)


def backdrop_key() -> int:
    """Cheap liveness key (header-only peek): frame ts, 0 when missing/stale.

    Consumers poll this instead of re-reading the payload; a changed key
    means a brand-new frame is ready.
    """
    try:
        with open(backdrop_path(), "rb") as f:
            head = f.read(_BACKDROP_HDR.size)
    except OSError:
        return 0
    if len(head) < _BACKDROP_HDR.size:
        return 0
    magic, ver, _w, _h, _bx, _by, _bw, _bh, ts = _BACKDROP_HDR.unpack(head)
    if magic != BACKDROP_MAGIC or ver != BACKDROP_VERSION \
            or time.time_ns() - ts > STALE_NS:
        return 0
    return ts


class LynxBlur(QObject):
    """RAM-cached low-res backdrop blur for one widget."""

    availableChanged = Signal(bool)

    def __init__(self, widget, *, radius: float = 8.0,
                 tint=(24, 24, 37, 110), refresh_ms: int = 33,
                 enabled: bool = True):
        super().__init__(widget)
        self.widget = widget
        self.radius = max(1.0, float(radius))
        self.tint = QColor(*tint) if tint else None
        self.refresh_ms = max(8, int(refresh_ms))
        self.enabled = bool(enabled)
        self._ram: QPixmap | None = None      # the cached blurred frame
        self._was_available: bool | None = None
        self._seen_key: int = -1              # last backdrop_key() acted on

        self._regen = QTimer(self)            # debounced geometry changes
        self._regen.setSingleShot(True)
        self._regen.setInterval(90)
        self._regen.timeout.connect(self.refresh)

        self._tick = QTimer(self)             # watch for new shared frames
        self._tick.setInterval(self.refresh_ms)
        self._tick.timeout.connect(self._poll)

        widget.installEventFilter(self)
        if self.enabled:
            self.start()

    @classmethod
    def attach(cls, widget, **kw) -> "LynxBlur":
        return cls(widget, **kw)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self.enabled = True
        self._seen_key = -1
        self.refresh()
        if not self._tick.isActive():
            self._tick.start()

    def stop(self):
        self.enabled = False
        self._regen.stop()
        self._tick.stop()

    def set_radius(self, radius: float):
        """Change blur strength live (CSS-like px; ~1/2 becomes downscale)."""
        self.radius = max(1.0, float(radius))
        self._refresh_soon()

    def set_tint(self, tint):
        """Change the legibility veil color/alpha; None disables it."""
        self.tint = QColor(*tint) if tint else None
        if self.widget is not None:
            self.widget.update()

    def set_refresh_ms(self, ms: int):
        """Watch interval for new frames (min 8 ms). Re-blur still only
        happens when the shared frame's timestamp actually changes."""
        self.refresh_ms = max(8, int(ms))
        self._tick.setInterval(self.refresh_ms)

    def _refresh_soon(self):
        self._regen.start()

    def _poll(self):
        if getattr(self, "widget", None) is None or not self.widget.isVisible():
            return
        key = backdrop_key()
        if key == self._seen_key:
            return                       # no new frame -> cached one is current
        self.refresh()

    # ---- core ------------------------------------------------------------
    def available(self) -> bool:
        return bool(self.enabled and self._ram is not None
                    and not self._ram.isNull())

    def frame(self) -> QPixmap | None:
        """The cached blurred frame as it lives in RAM."""
        return self._ram

    def _blur_pixmap(self, pm: QPixmap, w: int, h: int) -> QPixmap:
        """Low quality on purpose: shrink by ~radius/2, smooth back up."""
        f = max(1.0, self.radius / 2.0)
        sw = max(1, round(w / f))
        sh = max(1, round(h / f))
        small = pm.scaled(sw, sh,
                          Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
        out = small.scaled(w, h,
                           Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        if self.radius >= 20:                 # extra melt for big radii
            mid = out.scaled(max(1, sw // 2), max(1, sh // 2),
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            out = mid.scaled(w, h,
                             Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        return out

    def _backdrop_crop(self, gx: int, gy: int, gw: int, gh: int) -> QPixmap | None:
        """Crop the widget's global rect out of the shared snapshot."""
        snap = _read_backdrop()
        if snap is None:
            return None
        img, bb = snap
        fx = (gx - bb.x()) / bb.width()
        fy = (gy - bb.y()) / bb.height()
        fw = gw / bb.width()
        fh = gh / bb.height()
        x0 = max(0, round(fx * img.width()))
        y0 = max(0, round(fy * img.height()))
        cw = min(img.width() - x0, max(1, round(fw * img.width())))
        ch = min(img.height() - y0, max(1, round(fh * img.height())))
        if x0 >= img.width() or y0 >= img.height():
            return None
        crop = img.copy(QRect(x0, y0, cw, ch))
        pm = QPixmap.fromImage(crop)
        return None if pm.isNull() else pm

    def refresh(self):
        """Re-read the shared snapshot, re-blur, re-cache (one frame slot)."""
        wdg = getattr(self, "widget", None)
        if wdg is None or not self.enabled or not wdg.isVisible():
            return
        self._seen_key = backdrop_key()
        w, h = wdg.width(), wdg.height()
        if w <= 0 or h <= 0:
            return
        tl = wdg.mapToGlobal(QPoint(0, 0))
        src = self._backdrop_crop(tl.x(), tl.y(), w, h)
        had = self.available()
        if src is None:
            self._ram = None                  # no live backdrop right now
        else:
            self._ram = self._blur_pixmap(src, w, h)
        now_ok = self.available()
        if now_ok != had:
            self.availableChanged.emit(now_ok)
        wdg.update()

    def paint(self, painter: QPainter, rect=None, corner: float = 0.0) -> bool:
        """Draw the cached blurred frame; returns False to let the caller
        fall back to its plain translucent fill. `corner` clips rounded
        corners so the glass never leaks outside them."""
        if not self.available():
            return False
        wdg = self.widget
        r = rect or QRectF(0, 0, wdg.width(), wdg.height())
        painter.save()
        if corner > 0:
            path = QPainterPath()
            path.addRoundedRect(r, corner, corner)
            painter.setClipPath(path)
        painter.drawPixmap(r.toRect(), self._ram)
        if self.tint is not None and self.tint.alpha() > 0:
            painter.fillRect(r, self.tint)
        painter.restore()
        return True

    # ---- events ----------------------------------------------------------
    def eventFilter(self, obj, ev):
        if obj is self.widget:
            t = ev.type()
            if t == QEvent.Type.Show:
                self._seen_key = -1
                self.refresh()
                if self.enabled and not self._tick.isActive():
                    self._tick.start()
            elif t in (QEvent.Type.Move, QEvent.Type.Resize):
                self._refresh_soon()
            elif t == QEvent.Type.Hide:
                self._tick.stop()
        return False


def lynxBlur(widget, **kw) -> LynxBlur:
    """One-call helper: blur = lynxBlur(widget)."""
    return LynxBlur.attach(widget, **kw)
