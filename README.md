# lynxde

A Hyprland-based desktop environment for Arch Linux.

lynxde ships a layer-shell taskbar, per-window custom title bars, a
wallpaper manager, and a settings app — all written in pure Python on
top of Qt 6 (PySide6). Hyprland is its compositor, and the installer
installs it if missing.

## Components

| Command           | What it does                                        |
|-------------------|-----------------------------------------------------|
| `lynx-taskbar`    | Layer-shell top bar (window list, tray area)        |
| `lynx-titles`     | Per-window custom title bars via layer-shell         |
| `lynx-wallpaper`  | Wallpaper rendering                                  |
| `lynx-settings`   | Settings application (`SUPER + O`)                   |
| `lynx-launcher`   | Command palette: apps, Lynx Store (pacman), web search (`SUPER + /`) |
| `lynx-welcome`    | First-run welcome screen with keybinds + startup sound |
| `lynx-autostart`  | Runs your user-defined autostart commands at login   |
| `lynx-store`      | Package manager app: browse/install/remove pacman packages (also in the palette) |
| `lynx-update`     | Auto-updater: checks GitHub, downloads to Documents/lynxde/updates, runs install.sh |

## Requirements

- Arch Linux (the installer installs **Hyprland** via pacman if missing)
- A Qt 6 Python binding:
  - `paru -S pyside6` (AUR), or
  - `sudo pacman -S python-pyqt6` (repo fallback)
- `layer-shell-qt` for proper docking/title-bar surfaces (offered during install)
- `xorg-xwayland` only for the "Lynxde (Wayland + X11)" session entry

## Display-manager sessions

The installer registers two session entries so Lynxde can be picked from
SDDM/GDM/LightDM (system-wide via sudo; falls back to
`~/.local/share/wayland-sessions` otherwise):

| Session                    | What it runs                                          |
|----------------------------|-------------------------------------------------------|
| `Lynxde (Wayland-only)`    | Hyprland with XWayland disabled (`LYNXDE_WAYLAND_ONLY=1`; classic-conf setups get a generated `lynxde-wayland-only.conf` stub that sets `xwayland { enabled = false }` and sources your config) |
| `Lynxde (Wayland + X11)`   | Same desktop with the XWayland compatibility layer enabled, for running X11 apps |

## Install

```sh
./install.sh
```

The installer will:

1. Set up a private venv with PySide6 if no system binding is found
2. Vendor the `layer-shell-qt` runtime into `~/.local/share/lynxde`
3. Install launchers to `~/.local/bin`
4. Wire autostart, keybinds and window rules into your Hyprland config
   (a backup is kept at `hyprland.conf.bak-lynxde`)
5. Start everything immediately if run inside a live session

## Keybinds

- `SUPER + X` — close window
- `SUPER + O` — open lynxde settings
- `SUPER + /` — toggle the launcher palette (arrow keys / enter to open,
  click to open, DuckDuckGo or Mwmbl web search, clearable search history).
  Typing also surfaces matching pacman packages under **Lynx Store**; the
  full store app (search tabs for All / Installed / Updates, per-package
  details, install/remove/update actions) opens from the app list or by
  running `lynx-store`. All actions run `sudo pacman` in your terminal so
  password prompts stay visible

A first-run welcome screen (`lynx-welcome`) shows the keybinds at session
start and plays a startup chime from `sounds/`. Check "Don't show this
again" (or set `"welcome_seen": true` in
`~/.config/lynxde/settings.json`) to skip it; run `lynx-welcome --show`
to bring it back anytime.

## Settings (`SUPER + O`)

Everything is configurable live from the settings app, organized into
pages — Appearance, Bar & Titles, Wallpaper, Desktop widgets, Launcher,
Windows & effects, Keyboard & mouse, Startup:

- **Appearance** — five color schemes plus a custom accent color (also
  recolors focused-window borders)
- **Bar & titles** — top/bottom edge, bar and title-bar heights, brand
  label visibility, 12/24-hour clock with optional seconds and date,
  title bars on/off
- **Wallpaper** — video picker, dimming overlay, wallpaper audio
- **Desktop widgets** — clock and OpenStreetMap tiles on the background
  layer, position reset
- **Launcher** — DuckDuckGo/Mwmbl engine selection, search-history wipe
- **Windows & effects** — dwindle/master layout, inner/outer gaps,
  border width, corner rounding, blur, shadows, animation speed,
  active/inactive window opacity (applied instantly via `hyprctl keyword`,
  re-applied at every login). Changing any of these reveals a
  **Reload Hyprland** button in the footer that runs `hyprctl reload` and
  re-applies every Lynx tweak on the clean slate
- **Keyboard & mouse** — click-to-focus vs focus-follows-mouse modes,
  natural scrolling, NumLock at startup, cursor size
- **Startup** — welcome-screen controls, startup chime toggle, a
  one-command-per-line autostart editor executed by `lynx-autostart`,
  and auto-update controls (toggle, check-now button, installed version)

All of it persists to `~/.config/lynxde/settings.json` and applies live
to the running desktop within a second.

## Auto-updates

`lynx-update` keeps lynxde current on its own. About three minutes after
login (and then once every 24 h) it compares the recorded installed
version against the latest commit of this repository. On a new version
it:

1. downloads the zipball to `~/Documents/lynxde/updates/lynxde-main.zip`
2. extracts it there and runs its `install.sh`
3. records the new version, notifies you, and the installer restarts
   every running component — so updates apply live, no re-login needed

Disable it under Settings → Startup ("Check for updates automatically"),
or drive it by hand:

```sh
lynx-update --now      # check + install immediately
lynx-update --status   # show version, settings and recent activity
```

Activity is logged to `~/.cache/lynxde/update.log`.

## Configuration

| Variable           | Meaning                        | Default            |
|--------------------|--------------------------------|--------------------|
| `LYNX_BAR_HEIGHT`  | Bar height in px               | `54`               |
| `LYNX_TITLE_HEIGHT`| Title bar height in px         | `28`               |
| `HYPRLAND_CONF`    | Legacy config path override    | `~/.config/hypr/hyprland.conf` |
| `HYPRLAND_LUA`     | Lua config path override       | `~/.config/hypr/hyprland.lua`  |
| `LYNX_PYTHON`      | Python interpreter to use      | auto-detect / venv |
| `LYNX_BAR_LAYER`   | Set to `0` to disable layer-shell mode | enabled    |

## Manage

```sh
./install.sh --start      # install (if needed) and start
./install.sh --stop       # stop taskbar + titlebars
./install.sh --restart    # restart them
./install.sh --uninstall  # remove files, venv, autostart entries and rules
```

Logs go to `~/.cache/lynxde/taskbar.log`.

## Contributing

Issues and pull requests are welcome! For PRs, keep components pure
Python + PySide6, match the existing style (no external Python deps),
and test changes with `--selftest` flags where available:

```sh
./taskbar/lynx_settings.py --selftest
./taskbar/lynx_launcher.py --selftest
```

## License

Released under the [MIT License](LICENSE).

