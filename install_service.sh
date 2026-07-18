#!/usr/bin/env bash
# Install openrouter-free-model-proxy as a systemd --user service so it runs in
# the background and survives logout. Optional — you can also just run
# ./run_proxy.sh in tmux/screen. Portable: works wherever the repo lives.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="openrouter-free-model-proxy"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=$NAME (best free OpenRouter model, auto-selected)
After=network-online.target

[Service]
ExecStart=$DIR/run_proxy.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

echo "Wrote $UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$NAME.service"
# Keep the user service running even when no session is logged in.
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "Status:"
systemctl --user --no-pager status "$NAME.service" || true
