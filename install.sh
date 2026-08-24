#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lynxde"
SHARE_DIR="$DATA_DIR/taskbar"
VENV_DIR="$DATA_DIR/venv"
BIN="$HOME/.local/bin/lynx-taskbar"
TITLES_BIN="$HOME/.local/bin/lynx-titles"
WALLPAPER_BIN="$HOME/.local/bin/lynx-wallpaper"
SESSION_WAYLAND_BIN="$HOME/.local/bin/lynxde-session-wayland"
SESSION_X11_BIN="$HOME/.local/bin/lynxde-session-x11"
STUB_CONF="$HOME/.config/hypr/lynxde-wayland-only.conf"
WAYLAND_SESSIONS_DIR="${LYNX_SESSIONS_DIR:-/usr/share/wayland-sessions}"
CONF_CONF="${HYPRLAND_CONF:-$HOME/.config/hypr/hyprland.conf}"
CONF_LUA="${HYPRLAND_LUA:-$HOME/.config/hypr/hyprland.lua}"
BAR_HEIGHT="${LYNX_BAR_HEIGHT:-54}"
CLASS="lynx-taskbar"
LOG_FILE="${XDG_CACHE_HOME:-$HOME/.cache}/lynxde/taskbar.log"

usage() {
  cat <<EOF
usage: ./install.sh [install|--start|--stop|--restart|--uninstall]

  (default)   install files, wire up the Hyprland config, start if in a session
  --start     install (if needed) and start taskbar + titlebars now
  --stop      stop them
  --restart   restart them
  --uninstall remove files, venv, autostart entries and rules

Installs Hyprland itself if missing (pacman), then wires the desktop into
hyprland.lua when your Hyprland reads Lua configs (0.56+), falling back to
hyprland.conf otherwise. Both components are layer-shell surfaces: a top bar
plus per-window custom title bars, all pure Python. Also registers
display-manager sessions: "Lynxde (Wayland-only)" and "Lynxde (Wayland + X11)"
(XWayland compatibility layer).

env:
  LYNX_BAR_HEIGHT   bar height in px            (default 54)
  LYNX_TITLE_HEIGHT title bar height in px      (default 28)
  HYPRLAND_CONF     legacy config path override
  HYPRLAND_LUA      lua config path override
  LYNX_PYTHON       python interpreter to use   (default: auto-detect / venv)
  LYNX_BAR_LAYER    set to 0 to disable layer-shell mode
EOF
}

find_pyside_python() {
  local candidates=("$VENV_DIR/bin/python")
  [ -n "${LYNX_PYTHON:-}" ] && candidates=("$LYNX_PYTHON" "${candidates[@]}")
  candidates+=("python3")
  local c
  for c in "${candidates[@]}"; do
    if "$c" -c 'import PySide6' >/dev/null 2>&1; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

ensure_runtime() {
  PY="$(find_pyside_python || true)"
  if [ -n "$PY" ]; then
    echo "python: $PY"
    return 0
  fi
  if python3 -c 'import PyQt6' >/dev/null 2>&1; then
    PY="python3"
    echo "note: PySide6 not found, using system PyQt6 fallback"
    return 0
  fi
  echo "PySide6 is required."
  if [ ! -t 0 ]; then
    echo "non-interactive shell; set LYNX_PYTHON or install PySide6 manually:"
    echo "  sudo pacman -S python-pyqt6        (repo fallback, works out of the box)"
    echo "  paru -S pyside6                    (AUR, system-wide)"
    exit 1
  fi
  printf 'Create a private venv at %s and pip-install pyside6? [Y/n] ' "$VENV_DIR"
  read -r reply
  case "$reply" in n*|N*) ;; *)
    mkdir -p "$DATA_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet pyside6 || true
    ;;
  esac
  PY="$(find_pyside_python || true)"
  if [ -z "$PY" ]; then
    echo "error: no usable Qt6 python binding found. Install pyside6 or python-pyqt6." >&2
    exit 1
  fi
  echo "python: $PY"
}

ensure_hyprland() {
  if command -v Hyprland >/dev/null 2>&1; then
    echo "hyprland: $(Hyprland --version 2>/dev/null | head -1 || echo 'installed')"
    return 0
  fi
  echo "Hyprland is required — lynxde is a Hyprland desktop."
  if ! command -v pacman >/dev/null 2>&1; then
    echo "error: no pacman found; install Hyprland with your package manager." >&2
    exit 1
  fi
  if [ ! -t 0 ]; then
    echo "non-interactive shell; install it manually:" >&2
    echo "  sudo pacman -S --needed hyprland" >&2
    exit 1
  fi
  printf 'install Hyprland now with pacman? [Y/n] '
  read -r reply
  case "$reply" in n*|N*)
    echo "cannot continue without Hyprland; aborting (install it, then re-run)." >&2
    exit 1
    ;;
    *) sudo pacman -S --needed hyprland || {
      echo "error: Hyprland install failed; fix pacman and re-run." >&2
      exit 1
    } ;;
  esac
}

ensure_session_deps() {
  command -v pacman >/dev/null 2>&1 || return 0
  local missing=()
  pacman -Qq xdg-desktop-portal-hyprland >/dev/null 2>&1 || missing+=("xdg-desktop-portal-hyprland")
  pacman -Qq qt6-wayland >/dev/null 2>&1 || missing+=("qt6-wayland")
  if [ ${#missing[@]} -eq 0 ]; then
    echo "portals/qt6-wayland: installed"
    return 0
  fi
  echo "tip: ${missing[*]} enable screen sharing, file dialogs and native Qt theming"
  if [ ! -t 0 ]; then
    echo "note: non-interactive; skipping (install manually: sudo pacman -S --needed ${missing[*]})"
    return 0
  fi
  printf 'install them now with pacman? [Y/n] '
  read -r reply
  case "$reply" in
    n*|N*) echo "continuing without them" ;;
    *) sudo pacman -S --needed "${missing[@]}" \
      || echo "warning: install failed, continuing" ;;
  esac
}

ensure_layer_shell() {
  command -v pacman >/dev/null 2>&1 || return 0
  if pacman -Qq layer-shell-qt >/dev/null 2>&1; then
    echo "layer-shell-qt: installed"
    return 0
  fi
  echo "tip: 'layer-shell-qt' enables proper layer-shell surfaces (docking, title bars)"
  if [ ! -t 0 ]; then
    echo "note: non-interactive; skipping install (plain-window fallback will be used)"
    return 0
  fi
  printf 'install it now with pacman? [Y/n] '
  read -r reply
  case "$reply" in
    n*|N*) echo "continuing without it (fallback plain-window mode)" ;;
    *) sudo pacman -S --needed layer-shell-qt || echo "warning: install failed, continuing with fallback mode" ;;
  esac
}
vendor_layer_shell() {
  local vd="$DATA_DIR/layershell"
  [ -f "$vd/plugins/wayland-shell-integration/liblayer-shell.so" ] && {
    echo "layer-shell runtime: vendored at $vd"
    return 0
  }
  command -v curl >/dev/null 2>&1 || return 0
  command -v bsdtar >/dev/null 2>&1 || return 0
  local base="https://geo.mirror.pkgbuild.com/extra/os/x86_64"
  local pkg
  pkg="$(curl -sf "$base/" | grep -oE 'layer-shell-qt-[0-9][^"]*\.pkg\.tar\.zst' | sort -u | tail -1)" || true
  if [ -z "$pkg" ]; then
    echo "warning: could not fetch layer-shell-qt package; layer-shell disabled"
    return 0
  fi
  echo "vendoring $pkg ..."
  mkdir -p "$vd"
  curl -sfL -o "$vd/pkg.tar.zst" "$base/$pkg" || { echo "warning: download failed"; rm -rf "$vd"; return 0; }
  bsdtar -xf "$vd/pkg.tar.zst" -C "$vd" || { echo "warning: extraction failed"; return 0; }
  mkdir -p "$vd/lib" "$vd/plugins"
  cp -a "$vd"/usr/lib/libLayerShellQtInterface.so* "$vd/lib/" || true
  cp -a "$vd/usr/lib/qt6/plugins/wayland-shell-integration" "$vd/plugins/" || true
  rm -rf "$vd/usr" "$vd/pkg.tar.zst" .BUILDINFO .MTREE .PKGINFO 2>/dev/null || true
  echo "layer-shell runtime: vendored at $vd"
}

ensure_xwayland() {
  command -v pacman >/dev/null 2>&1 || return 0
  if pacman -Qq xorg-xwayland >/dev/null 2>&1; then
    echo "xorg-xwayland: installed"
    return 0
  fi
  echo "tip: 'xorg-xwayland' powers the 'Lynxde (Wayland + X11)' session"
  if [ ! -t 0 ]; then
    echo "note: non-interactive; skipping install (Wayland-only session still works)"
    return 0
  fi
  printf 'install it now with pacman? [Y/n] '
  read -r reply
  case "$reply" in
    n*|N*) echo "continuing without XWayland (X11 session entry will fail until installed)" ;;
    *) sudo pacman -S --needed xorg-xwayland || echo "warning: install failed, install it manually later" ;;
  esac
}

write_session_scripts() {
  cat > "$SESSION_WAYLAND_BIN" <<'WRAPPER'
#!/usr/bin/env bash
# Lynxde session: Hyprland with XWayland disabled (native Wayland only).
set -euo pipefail
export LYNXDE_WAYLAND_ONLY=1
LUA="${HYPRLAND_LUA:-$HOME/.config/hypr/hyprland.lua}"
if [ -f "$LUA" ]; then            # lua config reads LYNXDE_WAYLAND_ONLY
  exec Hyprland "$@"
fi
STUB="$HOME/.config/hypr/lynxde-wayland-only.conf"
args=()
[ -f "$STUB" ] && args=(-c "$STUB")
exec Hyprland "${args[@]}" "$@"
WRAPPER
  cat > "$SESSION_X11_BIN" <<'WRAPPER'
#!/usr/bin/env bash
# Lynxde session: Hyprland on Wayland with the XWayland compatibility layer.
set -euo pipefail
unset LYNXDE_WAYLAND_ONLY
exec Hyprland "$@"
WRAPPER
  chmod +x "$SESSION_WAYLAND_BIN" "$SESSION_X11_BIN"
  echo "installed: $SESSION_WAYLAND_BIN"
  echo "installed: $SESSION_X11_BIN"
}

write_stub_conf() {
  # Only used when Hyprland reads classic conf: disables XWayland, then loads
  # the user's real config so every other setting keeps applying.
  mkdir -p "$(dirname "$STUB_CONF")"
  cat > "$STUB_CONF" <<STUB
# lynxde: generated session stub — do not edit.
# Disables XWayland for the "Lynxde (Wayland-only)" session, then sources
# your regular config for everything else.
xwayland { enabled = false }

source = $CONF_CONF
STUB
}

install_session_entries() {
  local dst="$WAYLAND_SESSIONS_DIR/lynxde-wayland.desktop"
  local dst_x="$WAYLAND_SESSIONS_DIR/lynxde-x11.desktop"
  local user_dir="${XDG_DATA_HOME:-$HOME/.local/share}/wayland-sessions"
  local tmp; tmp="$(mktemp -d)"
  cat > "$tmp/lynxde-wayland.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lynxde (Wayland-only)
Comment=Hyprland-based Lynxde desktop, native Wayland only (no X11 apps)
Exec=$SESSION_WAYLAND_BIN
TryExec=$SESSION_WAYLAND_BIN
DesktopNames=Lynxde
Terminal=false
EOF
  cat > "$tmp/lynxde-x11.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lynxde (Wayland + X11)
Comment=Lynxde on Hyprland/Wayland with the XWayland compatibility layer for X11 apps
Exec=$SESSION_X11_BIN
TryExec=$SESSION_X11_BIN
DesktopNames=Lynxde
Terminal=false
EOF
  if mkdir -p "$WAYLAND_SESSIONS_DIR" 2>/dev/null \
      && cp "$tmp/lynxde-wayland.desktop" "$dst" 2>/dev/null \
      && cp "$tmp/lynxde-x11.desktop" "$dst_x" 2>/dev/null; then
    echo "sessions: registered in $WAYLAND_SESSIONS_DIR"
  elif [ ! -t 0 ]; then
    mkdir -p "$user_dir"
    cp "$tmp/"*.desktop "$user_dir/"
    echo "sessions: no permission for $WAYLAND_SESSIONS_DIR; installed to $user_dir"
    echo "        (most display managers only read system dirs — run:" >&2
    echo "          sudo cp $user_dir/*.desktop $WAYLAND_SESSIONS_DIR/ )" >&2
  else
    printf 'Register sessions in %s (needs sudo)? [Y/n] ' "$WAYLAND_SESSIONS_DIR"
    read -r reply
    case "$reply" in n*|N*)
      mkdir -p "$user_dir"
      cp "$tmp/"*.desktop "$user_dir/"
      echo "sessions: installed to $user_dir instead"
      ;;
    *)
      sudo cp "$tmp/lynxde-wayland.desktop" "$dst" || true
      sudo cp "$tmp/lynxde-x11.desktop" "$dst_x" || true
      echo "sessions: registered in $WAYLAND_SESSIONS_DIR"
      ;;
    esac
  fi
  rm -rf "$tmp"
}

install_files() {
  local bin script base
  mkdir -p "$SHARE_DIR" "$HOME/.local/bin" "$(dirname "$LOG_FILE")" "$DATA_DIR/wallpapers"
  for script in "$SRC_DIR"/taskbar/*.py; do
    install -m 644 "$script" "$SHARE_DIR/"
  done
  if [ -d "$SRC_DIR/sounds" ]; then
    rm -rf "$DATA_DIR/sounds"
    mkdir -p "$DATA_DIR/sounds"
    cp -a "$SRC_DIR/sounds/." "$DATA_DIR/sounds/"
    echo "installed: $DATA_DIR/sounds"
  fi
  if [ -d "$SRC_DIR/taskbar/assets" ]; then
    rm -rf "$SHARE_DIR/assets"
    cp -a "$SRC_DIR/taskbar/assets" "$SHARE_DIR/assets"
    echo "installed: $SHARE_DIR/assets"
  fi
  for base in lynx-taskbar lynx-titles lynx-settings lynx-wallpaper \
    lynx-launcher lynx-welcome lynx-autostart lynx-store; do
    bin="$HOME/.local/bin/$base"
    cat > "$bin" <<WRAPPER
#!/usr/bin/env bash
VDIR="$DATA_DIR/layershell"
[ -d "\$VDIR/lib" ] && export LD_LIBRARY_PATH="\$VDIR/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
[ -d "\$VDIR/plugins" ] && export QT_PLUGIN_PATH="\$VDIR/plugins\${QT_PLUGIN_PATH:+:\$QT_PLUGIN_PATH}"
exec "$PY" "$SHARE_DIR/${base//-/_}.py" "\$@"
WRAPPER
    chmod +x "$bin"
    echo "installed: $bin"
  done
  app_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$app_dir"
  cat > "$app_dir/lynx-store.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lynx Store
GenericName=Package Manager
Comment=Browse, install and remove pacman packages
Exec=$HOME/.local/bin/lynx-store
Icon=system-software-install
Terminal=false
Categories=System;PackageManager;
Keywords=store;package;pacman;install;remove;software;
StartupWMClass=lynx-store
EOF
  echo "installed: $app_dir/lynx-store.desktop"
}

detect_conf() {
  MODE_LUA=0
  CONF="$CONF_CONF"
  local sig="${HYPRLAND_INSTANCE_SIGNATURE:-}"
  local rt="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  local log="$rt/hypr/$sig/hyprland.log"
  if [ -n "$sig" ] && grep -qs "Using lua config" "$log"; then
    CONF="$CONF_LUA"; MODE_LUA=1
  elif [ -f "$CONF_LUA" ]; then
    CONF="$CONF_LUA"; MODE_LUA=1
  fi
  if [ "$MODE_LUA" = 1 ]; then
    MARK_BEGIN="-- >>> lynxde-taskbar (managed block) >>>"
    MARK_END="-- <<< lynxde-taskbar <<<"
  else
    MARK_BEGIN="# >>> lynxde-taskbar (managed block) >>>"
    MARK_END="# <<< lynxde-taskbar <<<"
  fi
  echo "config target: $CONF ($([ "$MODE_LUA" = 1 ] && echo lua || echo classic))"
}

lua_block_body() {
  cat <<'LUABLOCK'
hl.on("hyprland.start", function ()
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-taskbar")
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-titles")
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-wallpaper")
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-launcher --autostart")
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-welcome")
    hl.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-autostart")
end)

hl.bind("SUPER + X", hl.dsp.window.close())
hl.bind("SUPER + O", hl.dsp.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-settings"))
hl.bind("SUPER + slash", hl.dsp.exec_cmd(os.getenv("HOME") .. "/.local/bin/lynx-launcher"))

-- lynxde sessions: "Wayland-only" entry sets LYNXDE_WAYLAND_ONLY=1
hl.config({ xwayland = { enabled = (os.getenv("LYNXDE_WAYLAND_ONLY") ~= "1") } })

hl.window_rule({
    name  = "lynx-taskbar-fallback",
    match = { class = "^lynx-taskbar$" },
    float = true,
    pin   = true,
    no_focus = true,
})

hl.window_rule({
    name  = "lynx-launcher",
    match = { class = "^lynx-launcher$" },
    float = true,
    pin   = true,
})

hl.window_rule({
    name  = "lynx-welcome",
    match = { class = "^lynx-welcome$" },
    float = true,
    pin   = true,
})
LUABLOCK
}

conf_block_body() {
  cat <<BODY
exec-once = \$HOME/.local/bin/lynx-taskbar
exec-once = \$HOME/.local/bin/lynx-titles
exec-once = \$HOME/.local/bin/lynx-wallpaper
exec-once = \$HOME/.local/bin/lynx-launcher --autostart
exec-once = \$HOME/.local/bin/lynx-welcome
exec-once = \$HOME/.local/bin/lynx-autostart
bind = SUPER, X, closewindow
bind = SUPER, O, exec, \$HOME/.local/bin/lynx-settings
bind = SUPER, slash, exec, \$HOME/.local/bin/lynx-launcher
windowrule = float, class:^($CLASS)\$
windowrule = pin, class:^($CLASS)\$
windowrule = size 100% $BAR_HEIGHT, class:^($CLASS)\$
windowrule = move 0 0, class:^($CLASS)\$
windowrule = bordersize 0, class:^($CLASS)\$
windowrule = rounding 0, class:^($CLASS)\$
windowrule = noborder, class:^($CLASS)\$
windowrule = noshadow, class:^($CLASS)\$
windowrule = nofocus, class:^($CLASS)\$
windowrule = noinitialfocus, class:^($CLASS)\$
windowrule = float, class:^(lynx-launcher)\$
windowrule = pin, class:^(lynx-launcher)\$
windowrule = noborder, class:^(lynx-launcher)\$
windowrule = noshadow, class:^(lynx-launcher)\$
windowrule = float, class:^(lynx-welcome)\$
windowrule = pin, class:^(lynx-welcome)\$
windowrule = noborder, class:^(lynx-welcome)\$
windowrule = noshadow, class:^(lynx-welcome)\$
BODY
}

block_body() {
  if [ "$MODE_LUA" = 1 ]; then lua_block_body; else conf_block_body; fi
}

strip_block_from() {
  local file="$1" b="$2" e="$3"
  awk -v b="$b" -v e="$e" '
    index($0, b) { skip = 1 }
    index($0, e) { skip = 0; next }
    !skip { print }
  ' "$file"
}

apply_conf() {
  if [ ! -f "$CONF" ]; then
    echo "warning: $CONF not found; wire this block up manually:"
    echo
    echo "$MARK_BEGIN"
    block_body
    echo "$MARK_END"
    return 0
  fi
  [ -f "$CONF.bak-lynxde" ] || cp "$CONF" "$CONF.bak-lynxde"
  strip_block_from "$CONF" "$MARK_BEGIN" "$MARK_END" > "$CONF.tmp-lynxde"
  {
    cat "$CONF.tmp-lynxde"
    echo "$MARK_BEGIN"
    block_body
    echo "$MARK_END"
  } > "$CONF.new-lynxde"
  mv "$CONF.new-lynxde" "$CONF"
  rm -f "$CONF.tmp-lynxde"
  echo "config updated: $CONF (backup at $CONF.bak-lynxde)"
}

components_running() {
  pgrep -f "lynx_taskbar.py|lynx_titles.py|lynx_wallpaper.py|lynx_launcher.py|lynx_welcome.py|lynx_autostart.py" >/dev/null 2>&1
}

stop_components() {
  pkill -f "lynx_taskbar.py" 2>/dev/null || true
  pkill -f "lynx_titles.py" 2>/dev/null || true
  pkill -f "lynx_wallpaper.py" 2>/dev/null || true
  pkill -f "lynx_launcher.py" 2>/dev/null || true
  pkill -f "lynx_welcome.py" 2>/dev/null || true
  pkill -f "lynx_autostart.py" 2>/dev/null || true
}

start_components() {
  if [ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; then
    echo "not inside a Hyprland session; skipping start (autostart will handle it)"
    return 0
  fi
  stop_components
  sleep 0.4
  ( setsid "$BIN" >>"$LOG_FILE" 2>&1 & )
  ( setsid "$TITLES_BIN" >>"$LOG_FILE" 2>&1 & )
  ( setsid "$WALLPAPER_BIN" >>"$LOG_FILE" 2>&1 & )
  if [ -x "$HOME/.local/bin/lynx-welcome" ]; then
    ( setsid "$HOME/.local/bin/lynx-welcome" >>"$LOG_FILE" 2>&1 & )
  fi
  if [ -x "$HOME/.local/bin/lynx-autostart" ]; then
    # mirror what exec-once would have done at login
    ( setsid "$HOME/.local/bin/lynx-autostart" >>"$LOG_FILE" 2>&1 & )
  fi
  echo "started (log: $LOG_FILE)"
}

uninstall() {
  stop_components
  rm -rf "$SHARE_DIR" "$VENV_DIR" "$BIN" "$TITLES_BIN" \
    "$HOME/.local/bin/lynx-settings" "$HOME/.local/bin/lynx-wallpaper" \
    "$HOME/.local/bin/lynx-launcher" "$HOME/.local/bin/lynx-welcome" \
    "$HOME/.local/bin/lynx-autostart" "$HOME/.local/bin/lynx-store" \
    "${XDG_DATA_HOME:-$HOME/.local/share}/applications/lynx-store.desktop" \
    "$SESSION_WAYLAND_BIN" "$SESSION_X11_BIN" "$STUB_CONF"
  local f d
  for d in "$WAYLAND_SESSIONS_DIR" \
    "${XDG_DATA_HOME:-$HOME/.local/share}/wayland-sessions"; do
    for f in lynxde-wayland.desktop lynxde-x11.desktop; do
      if [ -f "$d/$f" ] && ! rm -f "$d/$f" 2>/dev/null; then
        sudo rm -f "$d/$f" 2>/dev/null || true
      fi
    done
  done
  local f
  for f in "$CONF_LUA" "$CONF_CONF"; do
    [ -f "$f" ] || continue
    strip_block_from "$f" "-- >>> lynxde-taskbar (managed block) >>>" \
      "-- <<< lynxde-taskbar <<<" > "$f.new-lynxde"
    mv "$f.new-lynxde" "$f"
    strip_block_from "$f" "# >>> lynxde-taskbar (managed block) >>>" \
      "# <<< lynxde-taskbar <<<" > "$f.new-lynxde"
    mv "$f.new-lynxde" "$f"
  done
  echo "removed."
}

mode="${1:-install}"
case "$mode" in
  -h|--help)
    usage
    exit 0
    ;;
  --stop)
    stop_components
    echo "stopped"
    exit 0
    ;;
  --uninstall)
    uninstall
    exit 0
    ;;
  install|--start|--restart|"") ;;
  *)
    usage
    exit 1
    ;;
esac

ensure_hyprland
ensure_runtime
ensure_layer_shell
vendor_layer_shell
ensure_session_deps
ensure_xwayland
install_files
write_session_scripts
write_stub_conf
install_session_entries
detect_conf
apply_conf
start_components

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "hint: add ~/.local/bin to your PATH to run 'lynx-taskbar' manually" ;;
esac
