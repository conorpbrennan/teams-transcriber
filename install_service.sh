#!/usr/bin/env bash
# Install the Teams transcriber as a background daemon that starts on login.
#
#   - Linux: a systemd --user service (Restart=always on crash).
#   - Windows (Git Bash): a per-user HKCU\...\Run registry entry driving
#     pythonw.exe, so the daemon starts at logon, runs windowless, and can drive
#     a visible Chrome -- all without admin. (Task Scheduler is blocked by policy
#     on locked-down domain machines.) No .bat or .ps1 is generated -- reg.exe is
#     called directly. The daemon supervises itself (transcriber.py restarts its
#     own loop and relaunches Chrome), so the pairing recovers from crashes while
#     the process is alive and from logon on reboot.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK_NAME="TeamsTranscriber"
RUN_KEY="HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"

install_windows() {
  # Prefer pythonw.exe (no console window); fall back to python.exe.
  local py
  py="$(command -v pythonw || command -v pythonw.exe || command -v python || true)"
  if [[ -z "$py" ]]; then
    echo "[error] no python/pythonw on PATH" >&2
    exit 1
  fi
  local py_win script_win cmd
  py_win="$(cygpath -w "$py")"
  script_win="$(cygpath -w "$DIR/transcriber.py")"
  cmd="\"$py_win\" \"$script_win\""

  # Autostart via the per-user Run key. Task Scheduler is the natural home for a
  # daemon, but locked-down/domain machines block non-elevated task creation
  # ("Access is denied"), and we must not require admin. HKCU\...\Run needs no
  # elevation, runs at every logon, and -- pointed at pythonw.exe -- launches the
  # daemon windowless. Crash resilience lives inside transcriber.py's supervisor
  # loop (it restarts its own daemon and relaunches Chrome); the Run key covers
  # logon start, so the pairing is self-healing without a service manager.
  # //Flag form stops MSYS/Git Bash from mangling reg.exe's leading-slash options.
  reg add "$RUN_KEY" //v "$TASK_NAME" //t REG_SZ //d "$cmd" //f >/dev/null

  # Start it now so you don't have to log out and back in. Detached in a subshell
  # so it outlives this script; pythonw has no console to inherit.
  ("$py" "$DIR/transcriber.py" >/dev/null 2>&1 &)

  echo "Installed autostart (HKCU Run key '$TASK_NAME') and started the daemon."
  echo
  echo "  Autostart:  reg query \"$RUN_KEY\" //v $TASK_NAME"
  echo "  Stop now:   taskkill //F //IM pythonw.exe   (ends all pythonw processes)"
  echo "  Start now:  $cmd"
  echo "  Remove:     reg delete \"$RUN_KEY\" //v $TASK_NAME //f"
  echo
  echo "Transcripts are written to: $DIR/transcripts/"
}

install_systemd() {
  local py unit_dir unit
  py="$(command -v python3)"
  unit_dir="$HOME/.config/systemd/user"
  unit="$unit_dir/teams-transcriber.service"

  mkdir -p "$unit_dir"
  cat > "$unit" <<EOF
[Unit]
Description=Teams live-caption transcriber daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=$py $DIR/transcriber.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now teams-transcriber.service

  echo "Installed and started teams-transcriber.service"
  echo
  echo "  Status:  systemctl --user status teams-transcriber"
  echo "  Logs:    journalctl --user -u teams-transcriber -f"
  echo "  Stop:    systemctl --user stop teams-transcriber"
  echo "  Disable: systemctl --user disable --now teams-transcriber"
  echo
  echo "Transcripts are written to: $DIR/transcripts/"
}

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) install_windows ;;
  *)                    install_systemd ;;
esac
