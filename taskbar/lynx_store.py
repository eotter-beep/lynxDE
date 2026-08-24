#!/usr/bin/env python3
"""Lynx Store: browse, search, install and remove pacman packages.

A standalone package manager window for lynxde backed entirely by the
pacman CLI: sync-database search ('pacman -Ss'), the local database
('pacman -Q'), pending updates ('pacman -Qu') and per-package details
('pacman -Si'/'-Qi'). Install/remove/update actions open the user's
terminal running the equivalent 'sudo pacman' command so password
prompts and transaction output stay visible.

Scheme-aware like every lynxde component: restyles live when the theme
changes in the settings app.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import (  # noqa: E402
    SETTINGS_DIR,
    SettingsWatcher,
    build_style,
    get_scheme,
    installed_packages,
    package_info,
    store_search,
    terminal_prefix,
    upgradable_packages,
)

MODES = (("all", "All"), ("installed", "Installed"), ("updates", "Updates"))
DETAIL_KEYS = ("Repository", "Version", "Description", "URL",
               "Licenses", "Depends On", "Required By", "Download Size",
               "Installed Size", "Install Reason", "Groups", "Provides")


def pacmd(script_tail: str, intro: str) -> bool:
    """Run a sudo pacman command visible in the user's terminal."""
    prefix = terminal_prefix()
    if not prefix:
        return False
    script = (f"echo {shlex.quote(intro)}; "
              f"{script_tail}; echo; "
              f"read -n 1 -s -r -p 'Done — press any key to close '")
    subprocess.Popen([*prefix, "sh", "-c", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    return True


class StoreRow(QFrame):
    """One package row: tile, name/description, state chip, actions."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("storerow")
        self.pkg: dict = {}
        self.mode = "all"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 8, 7)
        lay.setSpacing(10)
        self.tile = QLabel("?", objectName="tile")
        self.tile.setFixedSize(34, 34)
        self.tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.tile)
        mid = QVBoxLayout()
        mid.setSpacing(1)
        self.name = QLabel(objectName="pname")
        self.desc = QLabel(objectName="pdesc")
        mid.addWidget(self.name)
        mid.addWidget(self.desc)
        lay.addLayout(mid, 1)
        self.chip = QLabel(objectName="chip")
        lay.addWidget(self.chip)
        self.act_install = QPushButton("Install", objectName="pkgbtn")
        self.act_install.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.act_remove = QPushButton("Remove", objectName="pkgbtn danger")
        self.act_remove.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.act_install)
        lay.addWidget(self.act_remove)

    def bind(self, win: "StoreWindow"):
        self.mousePressEvent = lambda _ev: win.show_details(self.pkg)
        self.act_install.clicked.connect(
            lambda: win.install(self.pkg, reinstall=True))
        self.act_remove.clicked.connect(
            lambda: win.remove(self.pkg))

    def set_data(self, pkg: dict, mode: str):
        self.pkg = pkg
        self.mode = mode
        name = pkg.get("pkg") or "?"
        self.tile.setText(next((c.upper() for c in name if c.isalnum()), "?"))
        ver = pkg.get("version", "")
        self.name.setText(f"{name}  {ver}".strip())
        desc = pkg.get("desc") or ""
        self.desc.setText(desc[:110])
        self.desc.setToolTip(desc)
        installed = bool(pkg.get("installed"))
        self.chip.setText("update" if mode == "updates" else
                          "installed" if installed else pkg.get("repo", ""))
        self.act_install.setText(
            "Update" if mode == "updates" else
            "Reinstall" if installed else "Install")
        self.act_remove.setVisible(installed)


# ---- main window -------------------------------------------------------------------

class StoreWindow(QWidget):
    def __init__(self):
        super().__init__(None)
        self.scheme = get_scheme()
        self.setWindowTitle("Lynx Store")
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(880, 600)
        self.setMinimumSize(720, 480)
        self.mode = "all"
        self.rows: list[StoreRow] = []
        self._cache: dict[tuple[str, str], list[dict]] = {}
        self._detail_pkg: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        self.panel = QFrame(objectName="panel")
        outer.addWidget(self.panel)
        root = QVBoxLayout(self.panel)
        root.setContentsMargins(20, 14, 20, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(QLabel("⬢ Lynx Store", objectName="brand"))
        self.search = QLineEdit(objectName="search")
        self.search.setPlaceholderText("Search pacman packages…")
        self.search.returnPressed.connect(self._run_now)
        self.search.textChanged.connect(self._on_query)
        head.addWidget(self.search, 1)
        root.addLayout(head)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_buttons: dict[str, QPushButton] = {}
        for mid_, label in MODES:
            b = QPushButton(label, objectName="tab")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _f=False, k=mid_: self.set_mode(k))
            self.tab_group.addButton(b)
            tabs.addWidget(b)
            self.tab_buttons[mid_] = b
        self.tab_buttons["all"].setChecked(True)
        tabs.addStretch(1)
        root.addLayout(tabs)

        body = QHBoxLayout()
        body.setSpacing(12)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_host = QWidget(objectName="listhost")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 2, 6, 2)
        self.list_lay.setSpacing(4)
        self.list_lay.addStretch(1)
        self.note = QLabel("", objectName="hint")
        self.list_lay.insertWidget(0, self.note)
        self.scroll.setWidget(self.list_host)
        body.addWidget(self.scroll, 1)

        self.details = QFrame(objectName="details")
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(16, 14, 16, 14)
        dl.setSpacing(8)
        self.d_name = QLabel("Package details", objectName="dname")
        self.d_name.setWordWrap(True)
        dl.addWidget(self.d_name)
        self.d_grid = QVBoxLayout()
        self.d_grid.setSpacing(5)
        dl.addLayout(self.d_grid)
        dl.addStretch(1)
        self.d_hint = QLabel("Click a package for details.",
                             objectName="hint")
        dl.addWidget(self.d_hint)
        self.details.setFixedWidth(264)
        body.addWidget(self.details)
        root.addLayout(body, 1)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.status = QLabel("", objectName="hint")
        foot.addWidget(self.status, 1)
        self.update_all_btn = QPushButton("Update all", objectName="accentbtn")
        self.update_all_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.update_all_btn.clicked.connect(self.update_all)
        self.update_all_btn.setVisible(False)
        foot.addWidget(self.update_all_btn)
        root.addLayout(foot)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.refresh)

        self.restyle()
        self.refresh()

    # -- styling -----------------------------------------------------------------
    def restyle(self):
        s = self.scheme
        extra = f"""
        #panel {{
            background: {s['bg']};
            border: 1px solid {s['surface_hi']};
            border-radius: 16px;
        }}
        #brand {{ color: {s['accent']}; font-weight: 800; font-size: 15px; }}
        QLineEdit#search {{
            background: {s['surface']}; color: {s['text']};
            border: 1px solid {s['surface_hi']}; border-radius: 10px;
            padding: 8px 13px; font-size: 13px;
        }}
        QLineEdit#search:focus {{ border-color: {s['accent']}; }}
        QPushButton#tab {{
            color: {s['muted']}; background: transparent;
            border: 1px solid transparent; border-radius: 9px;
            padding: 5px 14px; font-size: 11px; font-weight: 700;
        }}
        QPushButton#tab:hover {{ background: {s['surface']}; }}
        QPushButton#tab:checked {{
            background: {s['accent']}; color: {s['on_accent']};
        }}
        #storerow {{
            background: {s['surface']}; border: 1px solid {s['surface_hi']};
            border-radius: 11px;
        }}
        #storerow:hover {{ border-color: {s['accent']}; }}
        #tile {{
            background: {s['surface_hi']}; color: {s['accent']};
            border: none; border-radius: 10px;
            font-size: 15px; font-weight: 800;
        }}
        #pname {{ color: {s['text']}; font-size: 13px; font-weight: 700; }}
        #pdesc {{ color: {s['muted']}; font-size: 11px; }}
        #chip {{
            color: {s['muted']}; background: {s['surface_hi']};
            border-radius: 7px; padding: 3px 8px;
            font-size: 10px; font-weight: 700;
        }}
        QPushButton#pkgbtn {{
            background: {s['accent']}; color: {s['on_accent']};
            border: none; border-radius: 8px; padding: 6px 12px;
            font-size: 11px; font-weight: 700;
        }}
        QPushButton#pkgbtn:hover {{ border: 1px solid {s['text']}; }}
        QPushButton#danger {{
            background: transparent; color: {s['red']};
            border: 1px solid {s['red']};
        }}
        QPushButton#accentbtn {{
            background: {s['accent']}; color: {s['on_accent']};
            border: none; border-radius: 10px; padding: 8px 16px;
            font-size: 12px; font-weight: 700;
        }}
        #details {{
            background: {s['surface']}; border: 1px solid {s['surface_hi']};
            border-radius: 13px;
        }}
        #dname {{ color: {s['text']}; font-size: 14px; font-weight: 800; }}
        #dkey {{ color: {s['muted']}; font-size: 10px; font-weight: 800; }}
        #dval {{ color: {s['text']}; font-size: 11px; }}
        #hint {{ color: {s['muted']}; font-size: 11px; background: transparent; }}
        #listhost {{ background: transparent; }}
        QScrollArea {{ background: transparent; border: none; }}
        """
        self.setStyleSheet(build_style(s) + extra)

    # -- data --------------------------------------------------------------------
    def set_mode(self, mode: str):
        if mode == self.mode:
            return
        self.mode = mode
        self.refresh()

    def _on_query(self, _t: str):
        self._debounce.start()

    def _run_now(self):
        self._debounce.stop()
        self.refresh()

    def _fetch(self) -> list[dict]:
        q = self.search.text().strip().lower()
        key = (self.mode, q)
        if key in self._cache:
            return self._cache[key]
        if self.mode == "installed":
            pkgs = installed_packages(limit=400)
            if q:
                pkgs = [p for p in pkgs if q in p["pkg"].lower()]
        elif self.mode == "updates":
            pkgs = upgradable_packages()
            if q:
                pkgs = [p for p in pkgs if q in p["pkg"].lower()]
        else:
            pkgs = store_search(q, limit=60)
        self._cache[key] = pkgs
        while len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return pkgs

    def refresh(self):
        pkgs = self._fetch()
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows.clear()
        if self.mode == "all" and len(self.search.text().strip()) < 2:
            self.note.setText("Type at least two letters to search the "
                              "repositories.")
            self.note.setVisible(True)
        else:
            self.note.setVisible(False)
        for p in pkgs:
            row = StoreRow(self.list_host)
            row.bind(self)
            row.set_data(p, self.mode)
            self.list_lay.insertWidget(self.list_lay.count() - 1, row)
            self.rows.append(row)
        total = len(pkgs)
        noun = {"all": "packages", "installed": "installed",
                "updates": "updates"}[self.mode]
        self.status.setText(f"{total} {noun}" if total else
                            ("No updates pending." if self.mode == "updates"
                             else "Nothing found."))
        self.update_all_btn.setVisible(
            self.mode == "updates" and total > 0)
        self.list_host.updateGeometry()

    # -- actions -------------------------------------------------------------------
    def install(self, pkg: dict, reinstall: bool = False):
        name = pkg.get("pkg") or ""
        verb = "Reinstalling" if reinstall else "Installing"
        if not pacmd(f"sudo pacman -S --needed {shlex.quote(name)}",
                     f"{verb} {name} …"):
            self.status.setText("No terminal found — cannot run pacman.")

    def remove(self, pkg: dict):
        name = pkg.get("pkg") or ""
        answer = QMessageBox.question(
            self, "Remove package",
            f"Remove {name} and its no-longer-needed dependencies?\n\n"
            "Runs: sudo pacman -Rns " + name,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not pacmd(f"sudo pacman -Rns {shlex.quote(name)}",
                     f"Removing {name} …"):
            self.status.setText("No terminal found — cannot run pacman.")

    def update_all(self):
        if not pacmd("sudo pacman -Syu", "Updating all packages …"):
            self.status.setText("No terminal found — cannot run pacman.")

    def show_details(self, pkg: dict):
        self._detail_pkg = pkg
        name = pkg.get("pkg") or ""
        self.d_name.setText(name)
        info = package_info(name, installed_only=bool(pkg.get("installed")))
        if not info:
            info = package_info(name)
        while self.d_grid.count():
            item = self.d_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        shown = 0
        for key in DETAIL_KEYS:
            val = info.get(key, "")
            if not val:
                continue
            kl = QLabel(key.upper(), objectName="dkey")
            vl = QLabel(val, objectName="dval")
            vl.setWordWrap(True)
            self.d_grid.addWidget(kl)
            self.d_grid.addWidget(vl)
            shown += 1
        self.d_hint.setVisible(shown == 0)

    # -- events ---------------------------------------------------------------------
    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(ev)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("lynx-store")
    app.setDesktopFileName("lynx-store")
    app.setFont(QFont("Inter", 9))

    win = StoreWindow()
    watcher = SettingsWatcher(win)
    watcher.changed.connect(
        lambda _st: (setattr(win, "scheme", get_scheme()), win.restyle()))

    argv = set(sys.argv[1:])
    if "--selftest" in argv:
        out_path = os.environ.get("LYNX_SELFTEST_OUT",
                                  "/tmp/opencode/lynx_store.png")

        def run():
            win.show()
            win.search.setText("arch")

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
