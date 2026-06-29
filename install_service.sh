#!/usr/bin/env bash
# Install the Teams transcriber as a systemd --user service so it runs as a
# background daemon, starts on login, and restarts if it crashes.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/teams-transcriber.service"

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Teams live-caption transcriber daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=$PY $DIR/transcriber.py
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
