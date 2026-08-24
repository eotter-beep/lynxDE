#!/usr/bin/env python3
"""lynx-autostart: runs user-defined startup commands from lynxde settings.

Commands live in settings.json under "autostart" as a list of shell strings
(edit them in Lynx Settings → Startup). Started once per session from the
Hyprland config block the installer manages.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_common import load_settings  # noqa: E402

LOG = os.path.join(os.environ.get("XDG_CACHE_HOME")
                   or os.path.expanduser("~/.cache"), "lynxde", "autostart.log")


def commands() -> list[str]:
    st = load_settings()
    raw = st.get("autostart", [])
    out = []
    for entry in raw if isinstance(raw, list) else []:
        cmd = str(entry).strip()
        if cmd and not cmd.startswith("#"):
            out.append(cmd)
    return out


def run_all() -> int:
    cmds = commands()
    if not cmds:
        return 0
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as log:
        for cmd in cmds:
            print(f"[autostart] {cmd}", file=log, flush=True)
            try:
                subprocess.Popen(shlex.split(cmd) or [cmd],
                                 stdout=log, stderr=log,
                                 start_new_session=True)
            except (OSError, ValueError) as e:
                print(f"[autostart] FAILED {cmd}: {e}", file=log, flush=True)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--list" in args:
        cmds = commands()
        print("\n".join(cmds) if cmds else "(no autostart commands set)")
        return 0
    return run_all()


if __name__ == "__main__":
    raise SystemExit(main())
