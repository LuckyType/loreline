#!/usr/bin/env bash
# Host-side setup for a Bluetooth (or any PipeWire/PulseAudio-routed) mic,
# reachable by the app container via the bind-mount commented into
# docker-compose.yml. Run once per device, interactively, as the user whose
# session should own the audio - usually whoever you'll SSH in as day to day.
#
# A Bluetooth source is never a real ALSA hardware device: /dev/snd
# passthrough (what the app container gets by default) can't reach it at
# all, because it only exists inside a PipeWire/PulseAudio user session.
# This script gives that user a *persistent* such session (systemd
# lingering) even with no one ever logged in at a console, then walks you
# through pairing.
set -euo pipefail

TARGET_USER="${1:-${SUDO_USER:-$USER}}"
TARGET_UID="$(id -u "${TARGET_USER}")"

echo "==> Setting up Bluetooth audio for user '${TARGET_USER}' (uid ${TARGET_UID})"

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  bluez pipewire-audio libspa-0.2-bluetooth alsa-utils

sudo systemctl enable --now bluetooth

# Gives the target user a running systemd --user instance (and thus
# PipeWire/WirePlumber) with no console session - the state a service
# account or a headless SSH-only user is normally in.
sudo loginctl enable-linger "${TARGET_USER}"
sleep 2 # give the lingering user manager (and PipeWire/WirePlumber under it) a moment to start

# WirePlumber's Bluetooth monitor only starts once it sees an "active" seat
# from logind - a lingering session deliberately isn't one, so without this
# the monitor just never starts and nothing Bluetooth-related ever shows up,
# with no error to point at. main-systemwide is WirePlumber's own profile
# for exactly this case (see /usr/share/wireplumber/wireplumber.conf).
sudo mkdir -p /etc/systemd/user/wireplumber.service.d
cat <<'CONF' | sudo tee /etc/systemd/user/wireplumber.service.d/10-systemwide.conf > /dev/null
[Service]
ExecStart=
ExecStart=/usr/bin/wireplumber --profile=main-systemwide
CONF

sudo -u "${TARGET_USER}" env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  systemctl --user daemon-reload
sudo -u "${TARGET_USER}" env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  systemctl --user restart wireplumber

echo ""
echo "==> Pairing"
echo "    Put your Bluetooth mic into pairing mode now, then use:"
echo "      scan on                  (find its MAC address, then Ctrl-C the scan)"
echo "      pair   <MAC>"
echo "      trust  <MAC>"
echo "      connect <MAC>"
echo "      quit"
echo ""
sudo bluetoothctl

echo ""
echo "==> Verifying"
sudo -u "${TARGET_USER}" env XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" wpctl status | grep -A5 "Sources:" || true

cat <<EOF

==> Done. Uncomment the Bluetooth block in docker-compose.yml's "app"
    service with these values, then restart the stack:

    volumes:
      - /run/user/${TARGET_UID}/pulse:/run/user/${TARGET_UID}/pulse
    environment:
      XDG_RUNTIME_DIR: /run/user/${TARGET_UID}

    sudo docker compose up -d
EOF
