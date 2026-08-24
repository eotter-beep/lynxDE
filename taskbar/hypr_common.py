"""Shared pieces for lynxde python components: Hyprland IPC, layer-shell bridge, style."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys

from PySide6.QtCore import QByteArray, QObject, QRectF, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap

APP_CLASS = "lynx-taskbar"
TITLES_CLASS = "lynx-titles"
LAUNCHER_CLASS = "lynx-launcher"
WELCOME_CLASS = "lynx-welcome"
OWN_CLASSES = {APP_CLASS, TITLES_CLASS, LAUNCHER_CLASS, WELCOME_CLASS}

BAR_HEIGHT = int(os.environ.get("LYNX_BAR_HEIGHT", "54"))
TITLE_HEIGHT = int(os.environ.get("LYNX_TITLE_HEIGHT", "28"))

ANCHOR_TOP = 1
ANCHOR_BOTTOM = 2
ANCHOR_LEFT = 4
ANCHOR_RIGHT = 8
LAYER_BACKGROUND = 0
LAYER_BOTTOM = 1
LAYER_TOP = 2
LAYER_OVERLAY = 3

BG = "rgba(24, 24, 37, 232)"
SURFACE = "#313244"
SURFACE_HI = "#45475a"
TEXT = "#cdd6f4"
MUTED = "#a6adc8"
ACCENT = "#cba6f7"
RED = "#f38ba8"

SCHEMES = {
    "lynx": {
        "label": "Lynx",
        "bg": "rgba(24, 24, 37, 232)", "surface": "#313244",
        "surface_hi": "#45475a", "text": "#cdd6f4", "muted": "#a6adc8",
        "accent": "#cba6f7", "on_accent": "#1e1e2e", "red": "#f38ba8",
    },
    "amber": {
        "label": "Amber",
        "bg": "rgba(30, 25, 20, 235)", "surface": "#3b322a",
        "surface_hi": "#4d4136", "text": "#f0e0d0", "muted": "#c0aa94",
        "accent": "#fab387", "on_accent": "#2a1f16", "red": "#f38ba8",
    },
    "forest": {
        "label": "Forest",
        "bg": "rgba(20, 28, 23, 234)", "surface": "#2b3a31",
        "surface_hi": "#3a4d41", "text": "#d5ecd9", "muted": "#93b8a0",
        "accent": "#a6e3a1", "on_accent": "#16211a", "red": "#f38ba8",
    },
    "ocean": {
        "label": "Ocean",
        "bg": "rgba(18, 26, 36, 234)", "surface": "#29394d",
        "surface_hi": "#374b64", "text": "#cfe3f5", "muted": "#8fa9c4",
        "accent": "#74c7ec", "on_accent": "#12202e", "red": "#f38ba8",
    },
    "rose": {
        "label": "Rose",
        "bg": "rgba(32, 21, 27, 235)", "surface": "#402c35",
        "surface_hi": "#543a46", "text": "#f2dbe3", "muted": "#c49aa9",
        "accent": "#f38ba8", "on_accent": "#2a171f", "red": "#eba0ac",
    },
}
DEFAULT_SCHEME = "lynx"

SETTINGS_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "lynxde")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
VERSION_PATH = os.path.join(SETTINGS_DIR, "version")

VIDEO_EXTS = {".mp4", ".gif", ".mkv", ".webm", ".mov", ".avi", ".apng"}
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
WALLPAPER_DIRS = [
    "~/.local/share/lynxde/wallpapers",
    "~/Pictures/Wallpapers",
    "~/Pictures/wallpapers",
]


def get_wall_path() -> str:
    st = load_settings()
    if not st.get("wall_enabled", True):
        return ""
    return os.path.expanduser(st.get("wall_path") or "")


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_settings(settings: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def current_version() -> str:
    """Installed lynxde version (git sha) as recorded by the installer/updater."""
    try:
        with open(VERSION_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def get_scheme(name: str | None = None) -> dict:
    key = name or load_settings().get("scheme") or DEFAULT_SCHEME
    sch = dict(SCHEMES.get(key, SCHEMES[DEFAULT_SCHEME]))
    acc = load_settings().get("accent")
    if isinstance(acc, str) and QColor(acc).isValid():
        sch["accent"] = QColor(acc).name()
        sch["on_accent"] = _readable_on(sch["accent"])
    return sch


def _readable_on(accent_hex: str) -> str:
    """Pick a text color readable on top of the given accent."""
    lum = _hex_lum(accent_hex)
    if lum is None:
        return "#1e1e2e"
    return "#1e1e2e" if lum > 0.45 else "#f4f4f7"


def get_bar_side() -> str:
    side = load_settings().get("bar_side")
    return side if side in ("top", "bottom") else "top"


def _cfg_int(key: str, default: int, lo: int, hi: int) -> int:
    v = load_settings().get(key, default)
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def get_bar_height() -> int:
    return _cfg_int("bar_height", BAR_HEIGHT, 28, 120)


def get_title_height() -> int:
    return _cfg_int("title_height", TITLE_HEIGHT, 18, 64)


def titles_enabled() -> bool:
    return bool(load_settings().get("titles_enabled", True))


def get_clock_opts() -> dict:
    st = load_settings()
    return {
        "h24": bool(st.get("clock_24h", True)),
        "seconds": bool(st.get("clock_seconds", False)),
        "date": bool(st.get("clock_show_date", True)),
        "brand": bool(st.get("bar_show_brand", True)),
    }


# ---- live Hyprland compositor tuning ------------------------------------------

ANIM_TARGETS = ("windows", "windowsOut", "windowsMove", "fade", "fadeDim",
                "border", "borderangle", "workspaces")


def _fmt_num(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def collect_hypr_commands(st: dict | None = None) -> list[tuple[str, str]]:
    """Translate settings.json keys into ('path:value', raw_value) hyprctl pairs."""
    st = load_settings() if st is None else st
    cmds: list[tuple[str, str]] = []

    def kw(path: str, val):
        cmds.append((path, val))

    if "hypr_gaps_in" in st:
        kw("general:gaps_in", _fmt_num(st["hypr_gaps_in"]))
    if "hypr_gaps_out" in st:
        kw("general:gaps_out", _fmt_num(st["hypr_gaps_out"]))
    if "hypr_border_size" in st:
        kw("general:border_size", _fmt_num(st["hypr_border_size"]))
    if "hypr_layout" in st and st["hypr_layout"] in ("dwindle", "master"):
        kw("general:layout", st["hypr_layout"])
    if "hypr_rounding" in st:
        kw("decoration:rounding", _fmt_num(st["hypr_rounding"]))
    if "hypr_blur" in st:
        kw("decoration:blur:enabled", "true" if st["hypr_blur"] else "false")
    if "hypr_shadows" in st:
        kw("decoration:shadow:enabled", "true" if st["hypr_shadows"] else "false")
    if "hypr_animations" in st:
        kw("animations:enabled", "true" if st["hypr_animations"] else "false")
    if "hypr_anim_speed" in st:
        spd = _fmt_num(max(0.2, min(20.0, float(st["hypr_anim_speed"] or 1))))
        for name in ANIM_TARGETS:
            kw("animation", f"{name},1,{spd},default")
    if "hypr_follow_mouse" in st:
        fm = max(0, min(3, int(st["hypr_follow_mouse"] or 0)))
        kw("input:follow_mouse", str(fm))
    if "hypr_natural_scroll" in st:
        kw("input:touchpad:natural_scroll",
           "true" if st["hypr_natural_scroll"] else "false")
    if "hypr_numlock" in st:
        kw("input:numlock_by_default",
           "true" if st["hypr_numlock"] else "false")
    if "hypr_cursor_size" in st:
        kw("cursor:size", _fmt_num(st["hypr_cursor_size"]))
    oa = st.get("hypr_opacity_active")
    oi = st.get("hypr_opacity_inactive")
    if oa is not None or oi is not None:
        a = _fmt_num(max(0.1, min(1.0, float(oa if oa is not None else 1))))
        i = _fmt_num(max(0.1, min(1.0, float(oi if oi is not None else a))))
        if a != "1" or i != "1":
            kw("windowrulev2", f'"opacity {a} {i},class:^(.*)$"')
    return cmds


def apply_hypr_keywords(st: dict | None = None) -> int:
    """Push stored compositor tweaks live via 'hyprctl keyword'. Returns #applied."""
    cmds = collect_hypr_commands(st)
    errs = []
    for path, val in cmds:
        r = Hyprland.ctl_text("keyword", path, val)
        if not r or "error" in r.lower() or "invalid" in r.lower():
            errs.append(f"{path} {val}: {r or 'no response'}")
    if errs:
        print("lynx: some hyprctl keywords failed:", file=sys.stderr)
        for e in errs:
            print(f"  {e}", file=sys.stderr)
    return len(cmds) - len(errs)


def _hex_lum(col: str) -> float | None:
    c = QColor(col)
    if not c.isValid():
        return None
    return (0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()) / 255.0


def svg_pixmap(svg_name: str, size: int = 18, *, bg: str | None = None,
               tint: str | None = None) -> QPixmap:
    """Render an SVG from assets/ into a pixmap (SVG support without icon plugins).

    With bg set, fills too close in luminance to the background are swapped for
    the scheme text color so glyphs like near-black Settings.svg stay visible on
    dark menus; tint forces a single color instead.
    """
    from PySide6.QtSvg import QSvgRenderer

    path = os.path.join(ASSETS_DIR, svg_name)
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return QPixmap()

    if tint or bg:
        fallback = get_scheme()["text"]

        def _sub(m: re.Match) -> str:
            val = m.group(1)
            if tint:
                return f'fill="{tint}"'
            l1, l2 = _hex_lum(val), _hex_lum(bg or "#000000")
            if l1 is not None and l2 is not None and abs(l1 - l2) < 0.25:
                return f'fill="{fallback}"'
            return m.group(0)

        data = re.sub(r'fill="([^"]+)"', _sub, data)

    renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
    if not renderer.isValid():
        return QPixmap()
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def apply_scheme_colors(scheme: dict):
    """Push the accent onto focused-window borders at runtime."""
    acc = scheme["accent"].lstrip("#")
    expr = ('hl.config({ general = { col = { active_border = '
            f'{{ colors = {{ "rgb({acc})" }}, angle = 45 }} }} }} }})')
    r = Hyprland.ctl_text("eval", expr)
    if "error" in r.lower():
        print(f"lynx: active_border eval failed: {r}", file=sys.stderr)


def build_style(s: dict) -> str:
    return f"""
#sep {{
    border-left: 1px solid {s['surface_hi']};
    margin: 8px 0;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget,
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QPushButton {{
    color: {s['text']};
    background: {s['surface']};
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 4px 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {s['surface_hi']};
}}
QPushButton#ws {{
    min-width: 26px;
    padding: 4px 9px;
    font-weight: 700;
}}
QPushButton#ws[active="true"] {{
    background: {s['accent']};
    color: {s['on_accent']};
}}
QPushButton#task {{
    padding: 3px 11px;
}}
QPushButton#task[focused="true"] {{
    background: {s['surface_hi']};
    border-color: {s['accent']};
}}
QPushButton::menu-indicator {{
    image: none;
    width: 0px;
}}
#brand {{
    color: {s['accent']};
    font-weight: 800;
    font-size: 15px;
}}
#clock {{
    font-weight: 700;
    font-size: 14px;
    color: {s['text']};
}}
#date {{
    font-size: 10px;
    color: {s['muted']};
}}
#empty {{
    color: {s['muted']};
    font-style: italic;
}}
QMenu {{
    background: {s['surface']};
    color: {s['text']};
    border: 1px solid {s['surface_hi']};
    border-radius: 10px;
    padding: 5px;
}}
QMenu::item {{
    padding: 5px 20px;
    border-radius: 7px;
}}
QMenu::item:selected {{
    background: {s['accent']};
    color: {s['on_accent']};
}}
"""


STYLE = build_style(get_scheme())


class SettingsWatcher(QObject):
    """Polls settings.json; emits changed(new_settings) on external edits."""

    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sig = None
        try:
            self._sig = (os.stat(SETTINGS_PATH).st_mtime_ns,
                         os.stat(SETTINGS_PATH).st_size)
        except OSError:
            self._sig = None
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        try:
            sig = (os.stat(SETTINGS_PATH).st_mtime_ns,
                   os.stat(SETTINGS_PATH).st_size)
        except OSError:
            return
        if sig != self._sig:
            self._sig = sig
            self.changed.emit(load_settings())

_LIB = "libLayerShellQtInterface.so.6"
VENDORED_DIR = os.path.expanduser("~/.local/share/lynxde/layershell")
_VENDORED_LIB = os.path.join(VENDORED_DIR, "lib", _LIB)
_VENDORED_PLUGINS = os.path.join(VENDORED_DIR, "plugins")
_SYM_GET = b"_ZN12LayerShellQt6Window3getEP7QWindow"
_SYM_SET_ANCHORS = b"_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE"
_SYM_SET_ZONE = b"_ZN12LayerShellQt6Window16setExclusiveZoneEi"
_SYM_SET_EDGE = b"_ZN12LayerShellQt6Window16setExclusiveEdgeENS0_6AnchorE"
_SYM_SET_LAYER = b"_ZN12LayerShellQt6Window8setLayerENS0_5LayerE"
_SYM_SET_MARGINS = b"_ZN12LayerShellQt6Window10setMarginsERK8QMargins"


def _shell_integration_plugin_present() -> bool:
    candidates = []
    qtpp = os.environ.get("QT_PLUGIN_PATH")
    if qtpp:
        candidates += [d for d in qtpp.split(os.pathsep) if d]
    if os.path.isdir(_VENDORED_PLUGINS):
        if _VENDORED_PLUGINS not in (os.environ.get("QT_PLUGIN_PATH") or ""):
            os.environ["QT_PLUGIN_PATH"] = (
                _VENDORED_PLUGINS
                + (os.pathsep + os.environ["QT_PLUGIN_PATH"]
                   if os.environ.get("QT_PLUGIN_PATH") else ""))
        candidates.insert(0, _VENDORED_PLUGINS)
    try:
        import PySide6

        candidates.append(os.path.join(os.path.dirname(PySide6.__file__),
                                       "Qt", "plugins"))
    except ImportError:
        pass
    candidates += [
        "/usr/lib/qt6/plugins",
        "/usr/lib/x86_64-linux-gnu/qt6/plugins",
    ]
    for base in candidates:
        d = os.path.join(base, "wayland-shell-integration")
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if any(n.startswith(("liblayer-shell", "liblayershell")) for n in names):
            return True
    return False


def maybe_enable_layer_shell() -> bool:
    if os.environ.get("LYNX_BAR_LAYER", "") in ("0", "false"):
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("WAYLAND_SOCKET")):
        return False
    if not _shell_integration_plugin_present():
        return False
    if "QT_WAYLAND_SHELL_INTEGRATION" not in os.environ:
        os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    return True


class LayerShellError(Exception):
    pass


def _layer_lib():
    import ctypes

    try:
        lib = ctypes.CDLL(_VENDORED_LIB)
    except OSError:
        lib = ctypes.CDLL(_LIB)
    get_fn = getattr(lib, _SYM_GET.decode())
    get_fn.restype = ctypes.c_void_p
    get_fn.argtypes = [ctypes.c_void_p]
    out = {"lib": lib, "get": get_fn}

    def bind(sym, *argtypes):
        fn = getattr(lib, sym.decode())
        fn.argtypes = [ctypes.c_void_p, *argtypes]
        return fn

    out["anchors"] = bind(_SYM_SET_ANCHORS, ctypes.c_uint32)
    out["zone"] = bind(_SYM_SET_ZONE, ctypes.c_int32)
    out["edge"] = bind(_SYM_SET_EDGE, ctypes.c_uint32)
    out["layer"] = bind(_SYM_SET_LAYER, ctypes.c_uint32)
    out["margins"] = bind(_SYM_SET_MARGINS, ctypes.c_void_p)
    return out


def _layer_ptr(widget):
    import shiboken6

    handle = widget.windowHandle()
    if handle is None:
        return None
    lib = _layer_lib()
    wptr = lib["get"](shiboken6.getCppPointer(handle)[0])
    if not wptr:
        return None
    return lib, wptr


def attach_layershell(widget, *, anchors=0, exclusive_zone=0, exclusive_edge=0,
                      layer=LAYER_TOP) -> str:
    """Configure a created (not necessarily shown) QWindow as a layer surface.

    Call after widget.winId() so the platform window exists; values land on the
    first commit. Safe to call again later — changes propagate live.
    Returns a human-readable status string; non-'ok' means plain-window fallback.
    """
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "wayland":
        return "inactive (not a wayland session)"
    try:
        p = _layer_ptr(widget)
        if p is None:
            raise LayerShellError("layer shell window object missing")
        lib, wptr = p
        lib["layer"](wptr, int(layer))
        lib["anchors"](wptr, anchors)
        lib["zone"](wptr, exclusive_zone)
        if exclusive_edge:
            lib["edge"](wptr, exclusive_edge)
        return "ok"
    except Exception as e:
        return f"inactive ({e})"


def set_layershell_anchor_side(widget, *, side_top: bool, zone: int) -> bool:
    """Live-flip a bar-style surface between top and bottom docking edges."""
    p = _layer_ptr(widget)
    if p is None:
        return False
    lib, wptr = p
    edge = ANCHOR_TOP if side_top else ANCHOR_BOTTOM
    lib["anchors"](wptr, edge | ANCHOR_LEFT | ANCHOR_RIGHT)
    lib["zone"](wptr, zone)
    lib["edge"](wptr, edge)
    return True


def set_layershell_margins(widget, left: int, top: int, right: int, bottom: int) -> bool:
    try:
        import ctypes

        p = _layer_ptr(widget)
        if p is None:
            return False
        lib, wptr = p
        buf = (ctypes.c_int32 * 4)(left, top, right, bottom)
        lib["margins"](wptr, buf)
        return True
    except Exception:
        return False


class Hyprland(QObject):
    changed = Signal()
    config_reloaded = Signal()

    def __init__(self):
        super().__init__()
        self.sock: socket.socket | None = None
        self.notifier: QSocketNotifier | None = None
        self._buf = b""
        self._retry = QTimer(self)
        self._retry.setInterval(5000)
        self._retry.timeout.connect(self.connect_events)

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect_events(self) -> bool:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if not sig:
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(f"{runtime}/hypr/{sig}/.socket2.sock")
            s.setblocking(False)
        except OSError:
            if not self._retry.isActive():
                self._retry.start()
            return False
        self.sock = s
        self._buf = b""
        self.notifier = QSocketNotifier(s.fileno(), QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._on_event)
        self._retry.stop()
        self.changed.emit()
        return True

    def _on_event(self):
        try:
            data = self.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._lost()
            return
        if not data:
            self._lost()
            return
        self._buf += data
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            if line.startswith(b"configreloaded"):
                self.config_reloaded.emit()
        self.changed.emit()

    def _lost(self):
        if self.notifier is not None:
            self.notifier.setEnabled(False)
            self.notifier.deleteLater()
        if self.sock is not None:
            self.sock.close()
        self.sock = None
        self.notifier = None
        self._retry.start()

    @staticmethod
    def compositor_reload(timeout: float = 10.0) -> bool:
        """Run 'hyprctl reload' so runtime keywords start from a clean slate."""
        try:
            r = subprocess.run(["hyprctl", "reload"], capture_output=True,
                               text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"lynx: hyprctl reload failed: {e}", file=sys.stderr)
            return False
        out = (r.stdout or "").strip().lower()
        if r.returncode != 0 or "error" in out:
            print(f"lynx: hyprctl reload rejected: {(r.stdout or '').strip()} "
                  f"{(r.stderr or '').strip()}", file=sys.stderr)
            return False
        return True

    @staticmethod
    def ctl_json(*args):
        try:
            r = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=2)
            return json.loads(r.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def ctl_text(*args) -> str:
        try:
            r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=2)
            return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    @staticmethod
    def dispatch(*args) -> bool:
        try:
            r = subprocess.run(["hyprctl", "dispatch", *args],
                               capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"lynx: dispatch {' '.join(args)} failed: {e}", file=sys.stderr)
            return False
        out = (r.stdout or "").strip().lower()
        if r.returncode != 0 or "error" in out or "invalid" in out:
            print(f"lynx: dispatch {' '.join(args)} rejected: "
                  f"{(r.stdout or '').strip()} {(r.stderr or '').strip()}",
                  file=sys.stderr)
            return False
        return True


# ---- shared environment & package helpers -----------------------------------------

TERMINALS = (("kitty", []), ("alacritty", ["-e"]), ("foot", []),
             ("wezterm", ["start", "--"]), ("gnome-terminal", ["--"]),
             ("konsole", ["-e"]), ("xterm", ["-e"]),
             ("x-terminal-emulator", ["-e"]))

_PKG_RE = re.compile(
    r"^([^/\s]+)/(\S+)\s+(\S+)(\s+\[installed[^\]]*\])?\s*$")


def terminal_prefix() -> list[str]:
    """[terminal, *flags] for the first available terminal, else []."""
    for term, flags in TERMINALS:
        path = shutil.which(term)
        if path:
            return [path, *flags]
    return []


def store_search(q: str, limit: int = 8) -> list[dict]:
    """Search the pacman sync databases (name + description matches)."""
    q = q.strip().lower()
    if len(q) < 2:
        return []
    try:
        r = subprocess.run(["pacman", "-Ss", "--color", "never", q],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"lynx: store search failed: {e}", file=sys.stderr)
        return []
    pkgs: list[dict] = []
    cur: dict | None = None
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        m = _PKG_RE.match(line)
        if m is not None:
            if cur is not None:
                pkgs.append(cur)
            cur = {"kind": "pkg", "repo": m.group(1), "pkg": m.group(2),
                   "version": m.group(3), "installed": bool(m.group(4)),
                   "desc": ""}
        elif line.startswith("    ") and cur is not None and not cur["desc"]:
            cur["desc"] = line.strip()
    if cur is not None:
        pkgs.append(cur)

    def score(p: dict) -> tuple:
        n = p["pkg"].lower()
        s = 0 if n == q else 1 if n.startswith(q) else 2 if q in n else 3
        return (s, 0 if p["installed"] else 1, n)

    pkgs.sort(key=score)
    return pkgs[:limit]


def installed_packages(limit: int = 400) -> list[dict]:
    """Explicitly + dependency packages from the local DB ('pacman -Q')."""
    try:
        r = subprocess.run(["pacman", "-Q"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"lynx: pacman -Q failed: {e}", file=sys.stderr)
        return []
    out: list[dict] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append({"kind": "pkg", "pkg": parts[0],
                        "version": parts[1], "installed": True,
                        "repo": "local", "desc": ""})
    return out[:limit]


def upgradable_packages() -> list[dict]:
    """Packages with a newer sync version ('pacman -Qu')."""
    try:
        r = subprocess.run(["pacman", "-Qu"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"lynx: pacman -Qu failed: {e}", file=sys.stderr)
        return []
    out: list[dict] = []
    for line in (r.stdout or "").splitlines():
        if "->" in line:
            name, _, rest = line.partition("->")
            parts = name.split()
            ver = rest.split("[")[0].strip().split()[0] if rest.strip() else ""
            out.append({"kind": "pkg", "pkg": parts[0] if parts else "",
                        "version": ver, "installed": True, "repo": "local",
                        "desc": f"{name.split()[-1]} -> {ver}".strip()})
    return out


def package_info(pkg: str, installed_only: bool = False) -> dict:
    """Key/value details from 'pacman -Si' (or -Qi when installed_only)."""
    cmd = ["pacman", "-Qi" if installed_only else "-Si", pkg]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return {}
    info: dict[str, str] = {}
    key = ""
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and key:
            info[key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            info[key] = val.strip()
    return info
