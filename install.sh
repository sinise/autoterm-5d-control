#!/bin/bash
# Autoterm Heater Control -- installer.
#
# Usage (fresh machine, one-liner):
#   curl -fsSL https://raw.githubusercontent.com/sinise/autoterm-heater-control/main/install.sh | bash
#
# Usage (already cloned the repo):
#   ./install.sh
#
# What it does:
#   1. Installs OS packages: git, python3, python3-serial
#   2. Clones (or updates) this repo to $AUTOTERM_INSTALL_DIR
#   3. Adds you to the `dialout` group if needed (serial port access)
#   4. Generates and installs a systemd service (auto-start on boot,
#      auto-restart on failure) pointing at autoterm/autoterm_web.py
#   5. Enables and starts the service
#
# Override defaults with environment variables:
#   AUTOTERM_REPO_URL     git remote to clone (default: this project's github)
#   AUTOTERM_INSTALL_DIR  where to put it (default: $HOME/autoterm-heater-control)
#   AUTOTERM_PANEL_PORT   serial port wired to the panel  (default: /dev/ttyUSB1)
#   AUTOTERM_HEATER_PORT  serial port wired to the heater (default: /dev/ttyUSB3)
#
# Wiring and port assignment are hardware-specific -- see README.md before
# assuming the defaults are right for your setup. Getting panel/heater
# swapped won't damage anything, but commands won't reach the heater; see
# docs/PROTOCOL.md for how that was diagnosed on the reference setup.

set -euo pipefail

REPO_URL="${AUTOTERM_REPO_URL:-https://github.com/sinise/autoterm-heater-control.git}"
INSTALL_DIR="${AUTOTERM_INSTALL_DIR:-$HOME/autoterm-heater-control}"
PANEL_PORT="${AUTOTERM_PANEL_PORT:-/dev/ttyUSB1}"
HEATER_PORT="${AUTOTERM_HEATER_PORT:-/dev/ttyUSB3}"

log() { echo ">> $*"; }

log "Installing OS packages (git, python3, python3-serial)..."
sudo apt-get update -qq
sudo apt-get install -y git python3 python3-serial

# If this script is being run from inside an already-cloned copy of the repo,
# use that directory directly instead of cloning again.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/autoterm/autoterm_web.py" ]; then
    INSTALL_DIR="$SCRIPT_DIR"
    log "Running from existing checkout: $INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
    log "Updating existing checkout at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only
elif [ -e "$INSTALL_DIR" ]; then
    echo "ERROR: $INSTALL_DIR already exists and isn't a git checkout of this repo." >&2
    echo "Remove it or set AUTOTERM_INSTALL_DIR to a different path." >&2
    exit 1
else
    log "Cloning $REPO_URL to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

if ! id -nG "$USER" | grep -qw dialout; then
    log "Adding $USER to the dialout group (needed for serial port access)..."
    sudo usermod -aG dialout "$USER"
    log "NOTE: group membership only takes effect after you log out and back in"
    log "      (or reboot). The service itself runs fine without a fresh login"
    log "      since systemd re-evaluates group membership at start time."
fi

SERVICE_FILE=/etc/systemd/system/autoterm-web.service
log "Writing $SERVICE_FILE..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Autoterm heater inline proxy + web control
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR/autoterm
ExecStart=/usr/bin/python3 $INSTALL_DIR/autoterm/autoterm_web.py --panel $PANEL_PORT --heater $HEATER_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Enabling and starting the service..."
sudo systemctl daemon-reload
sudo systemctl enable --now autoterm-web.service

sleep 2
log "Service status:"
sudo systemctl --no-pager status autoterm-web.service || true

echo
log "Done. Once it's running, check which port it bound to:"
log "  cat ~/autoterm_logs/web_port.txt"
log "Then open http://<this-pi's-address>:<that-port>/ in a browser."
log "Follow live logs with: journalctl -u autoterm-web -f"
