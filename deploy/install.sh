#!/usr/bin/env bash
# Idempotent on-VM setup. Run as ubuntu after the repo is cloned to
# /opt/archon/app (cloud-init does the first clone; CI runs this on deploy).
set -euo pipefail

APP=/opt/archon/app

sudo mkdir -p /opt/archon/{data,secrets,backups}
sudo chown -R ubuntu:ubuntu /opt/archon
chmod 700 /opt/archon/secrets

# ffmpeg/ffprobe: required by neonize send_video and yt-dlp format merges.
command -v ffprobe >/dev/null 2>&1 || sudo apt-get install -y ffmpeg

# uv (pinned installer version; checksum checked by the installer itself)
if ! command -v ~/.local/bin/uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh
fi

cd "$APP"
~/.local/bin/uv sync --frozen

# systemd units
sudo cp "$APP"/deploy/archon.service /etc/systemd/system/
sudo cp "$APP"/deploy/archon-backup.service /etc/systemd/system/
sudo cp "$APP"/deploy/archon-backup.timer /etc/systemd/system/
sudo cp "$APP"/deploy/egress-monitor.service /etc/systemd/system/
sudo cp "$APP"/deploy/egress-monitor.timer /etc/systemd/system/
chmod +x "$APP"/deploy/egress-monitor.sh
sudo systemctl daemon-reload
sudo systemctl enable archon archon-backup.timer egress-monitor.timer

# sudoers drop-in so CI can restart without a password
echo 'ubuntu ALL=NOPASSWD: /usr/bin/systemctl restart archon, /usr/bin/systemctl status archon, /usr/bin/systemctl daemon-reload, /usr/bin/cp * /etc/systemd/system/*' \
  | sudo tee /etc/sudoers.d/archon-deploy >/dev/null
sudo chmod 440 /etc/sudoers.d/archon-deploy

echo "install.sh done. Put secrets in /opt/archon/secrets, then: sudo systemctl start archon"
