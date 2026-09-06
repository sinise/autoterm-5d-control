#!/bin/bash
# Stops and removes the autoterm-web systemd service. Leaves the repo
# checkout and logs in place -- delete those manually if you want them gone
# too.

set -euo pipefail

echo ">> Stopping and disabling autoterm-web.service..."
sudo systemctl disable --now autoterm-web.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/autoterm-web.service
sudo systemctl daemon-reload

echo ">> Done. The repo checkout and ~/autoterm_logs/ were left untouched."
