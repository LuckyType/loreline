#!/usr/bin/env bash
# Install Loreline as a source+systemd deployment (no Docker at all) on the
# device. install.sh (Docker Compose) is the recommended default - use this
# one only where a container runtime is a hard no; see the README's
# Deployment section for the trade-offs.
# Autostart is installed but DISABLED by default (enable via the web UI).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/loreline}"
SERVICE_USER="${SERVICE_USER:-loreline}"

echo "==> Installing Loreline to ${APP_DIR} (user: ${SERVICE_USER})"

if ! id "${SERVICE_USER}" &>/dev/null; then
  sudo useradd --system --create-home --home-dir "/home/${SERVICE_USER}" \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
# Allow audio device access.
sudo usermod -aG audio "${SERVICE_USER}" || true

# sounddevice's wheel links against the system PortAudio at import time (it
# doesn't bundle it on Linux) and silero-vad's onnxruntime needs OpenMP -
# without these the `audio` extra installs fine but fails at runtime.
# nodejs/npm build the SvelteKit UI below; the app serves nothing at "/"
# without a built `frontend/build` (see loreline.web.spa.spa_directory).
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends libportaudio2 libgomp1 nodejs npm

sudo mkdir -p "${APP_DIR}"
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# Same cache locations loreline.service sets for self-update's benefit
# (see its comment) - matched here too so install doesn't populate a second,
# separate cache under $HOME that self-update will never reuse.
UV_CACHE_DIR="${APP_DIR}/.cache/uv"
NPM_CACHE_DIR="${APP_DIR}/.cache/npm"

# Sync dependencies (expects the repo checked out at APP_DIR).
sudo -u "${SERVICE_USER}" env -C "${APP_DIR}" "UV_CACHE_DIR=${UV_CACHE_DIR}" \
  uv sync --frozen --extra audio --extra providers

# Build the web UI (static SPA served by the backend itself).
sudo -u "${SERVICE_USER}" env -C "${APP_DIR}/frontend" "npm_config_cache=${NPM_CACHE_DIR}" npm ci
sudo -u "${SERVICE_USER}" env -C "${APP_DIR}/frontend" "npm_config_cache=${NPM_CACHE_DIR}" npm run build

# Install systemd unit + sudoers rule (disabled by default).
sudo install -m 0644 "${APP_DIR}/deploy/loreline.service" /etc/systemd/system/loreline.service
sudo install -m 0440 "${APP_DIR}/deploy/sudoers.d/loreline" /etc/sudoers.d/loreline
sudo systemctl daemon-reload

echo "==> Installed. Autostart is DISABLED by default."
echo "    Start now:        sudo systemctl start loreline"
echo "    Enable autostart: sudo systemctl enable loreline   (or toggle in the web UI)"
