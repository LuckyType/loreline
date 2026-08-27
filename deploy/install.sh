#!/usr/bin/env bash
# Interactive installer for a Loreline Docker Compose deployment - the
# recommended path. Installs Docker (if missing), writes .env, and brings the
# stack up; everything else (the app, its dependencies, the built UI) lives
# inside the image, not on the host.
#
# Usage:
#   bash deploy/install.sh              # interactive
#   bash deploy/install.sh --defaults   # no prompts, use defaults (CI/scripted)
#
# For a source+systemd deployment instead (no Docker at all - e.g. a device
# where you'd rather not run a container runtime), see install-source.sh.
#
# A Bluetooth mic (or anything else only reachable via the host's
# PipeWire/PulseAudio session rather than a raw ALSA device) needs extra
# host-side setup - see setup-bluetooth-audio.sh, run separately.
set -euo pipefail

# --- output helpers ---------------------------------------------------------
if [[ -t 1 ]]; then
  RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; DIM=$'\033[2m'; CL=$'\033[m'
else
  RD=''; GN=''; YW=''; BL=''; DIM=''; CL=''
fi

msg_info() { echo -e " ${BL}·${CL} $1"; }
msg_ok() { echo -e " ${GN}✓${CL} $1"; }
msg_warn() { echo -e " ${YW}!${CL} $1"; }
die() {
  echo -e " ${RD}✗${CL} $1" >&2
  exit 1
}

header() {
  echo -e "${BL}"
  cat <<'ART'
   __                _ _
  / /  ___  _ __ ___| (_)_ __   ___
 / /  / _ \| '__/ _ \ | | '_ \ / _ \
/ /__| (_) | | |  __/ | | | | |  __/
\____/\___/|_|  \___|_|_|_| |_|\___|
ART
  echo -e "${CL}${DIM} Headless tabletop session transcriber - Docker install${CL}\n"
}

# --- prompts (whiptail when available + interactive, else plain read) --------
INTERACTIVE=1
[[ ${1:-} == "--defaults" || ${1:-} == "-y" ]] && INTERACTIVE=0
[[ -t 0 ]] || INTERACTIVE=0
HAVE_WHIPTAIL=1
command -v whiptail &>/dev/null || HAVE_WHIPTAIL=0

ask_yesno() { # ask_yesno <prompt> <default:yes|no>
  local prompt="$1" default="$2"
  if ((INTERACTIVE == 0)); then
    [[ $default == yes ]]
    return
  fi
  if ((HAVE_WHIPTAIL == 1)); then
    local flag=""
    [[ $default == no ]] && flag="--defaultno"
    whiptail --backtitle "Loreline installer" --title "Loreline" $flag \
      --yesno "$prompt" 10 70
    return
  fi
  local reply hint="[Y/n]"
  [[ $default == no ]] && hint="[y/N]"
  read -r -p "$prompt $hint " reply
  reply="${reply:-$default}"
  [[ ${reply,,} == y* ]]
}

ask_value() { # ask_value <prompt> <default>  -> echoes result
  local prompt="$1" default="$2" reply
  if ((INTERACTIVE == 0)); then
    echo "$default"
    return
  fi
  if ((HAVE_WHIPTAIL == 1)); then
    reply=$(whiptail --backtitle "Loreline installer" --title "Loreline" \
      --inputbox "$prompt" 10 70 "$default" 3>&1 1>&2 2>&3) || reply="$default"
  else
    read -r -p "$prompt [$default] " reply
  fi
  echo "${reply:-$default}"
}

gen_password() {
  # Trim with parameter expansion rather than `| tr | head -c`: `head` exits
  # once it has its bytes, SIGPIPEing the rest of the pipeline, which
  # `set -o pipefail` turns into a failure (see have_pkg for the same trap).
  local raw
  if command -v openssl &>/dev/null; then
    raw=$(openssl rand -base64 24)
  else
    raw=$(head -c 32 /dev/urandom | base64)
  fi
  raw="${raw//[^A-Za-z0-9]/}"
  printf '%s' "${raw:0:20}"
}

# --- preflight --------------------------------------------------------------
header

[[ $(uname -s) == Linux ]] || die "This installer targets Linux. On macOS/Windows use Docker Desktop and \`docker compose up -d\` directly (note: microphone capture can't work there)."
command -v apt-get &>/dev/null || die "This installer expects apt (Debian/Ubuntu). On another distro, install Docker + the Compose plugin yourself, then run: docker compose up -d --build"

# Runs fine either as root (`sudo bash deploy/install.sh`) or as a normal
# user with sudo - `as_root` papers over the difference so every privileged
# step reads the same. Files this script creates are chowned to whoever owns
# the checkout, so a root-run install doesn't leave a .env the cloning user
# can't read or a repo they can't `git pull` in.
if [[ $EUID -eq 0 ]]; then
  as_root() { "$@"; }
else
  command -v sudo &>/dev/null || die "Need either root or sudo to install Docker and manage services."
  sudo -v || die "This installer needs sudo access."
  as_root() { sudo "$@"; }
fi

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$APP_DIR"
[[ -f docker-compose.yml ]] || die "docker-compose.yml not found in ${APP_DIR} - run this from a checkout of the repo."
OWNER="$(stat -c '%u:%g' "$APP_DIR")"
own() { chown "$OWNER" "$@" 2>/dev/null || true; }
msg_ok "Project directory: ${APP_DIR}"

# --- gather settings --------------------------------------------------------
LORELINE_PORT=8000
AUTH_PASSWORD=""
ENABLE_MIC=no
ENABLE_STT=no
ENABLE_DIAR=no
ENABLE_AUTOUPDATE=no
[[ -d /dev/snd ]] && ENABLE_MIC=yes

USE_ADVANCED=0
if ((INTERACTIVE == 1)); then
  if ask_yesno "Use default settings?\n\n  Web UI port:        8000\n  Login password:     auto-generated\n  Microphone (/dev/snd): $([[ $ENABLE_MIC == yes ]] && echo "detected, enabled" || echo "not detected, disabled")\n  Self-hosted STT:    no  (several GB of models)\n  Diarization:        no  (needs ONNX models you supply)\n  Daily auto-update:  no\n\nChoose No to configure each of these." yes; then
    USE_ADVANCED=0
  else
    USE_ADVANCED=1
  fi
fi

if ((USE_ADVANCED == 1)); then
  LORELINE_PORT=$(ask_value "Port for the Loreline web UI:" "8000")
  AUTH_PASSWORD=$(ask_value "Web UI login password (blank = auto-generate):" "")
  if [[ -d /dev/snd ]]; then
    ask_yesno "Pass the host microphone (/dev/snd) into the container?\n\nNeeded to record on this device. Say no if this box only orchestrates remote STT." yes &&
      ENABLE_MIC=yes || ENABLE_MIC=no
  else
    msg_warn "No /dev/snd on this host - microphone capture disabled."
    ENABLE_MIC=no
  fi
  ask_yesno "Enable self-hosted STT (Speaches)?\n\nRuns transcription locally instead of a cloud API. Downloads several GB of models on first start." no &&
    ENABLE_STT=yes || ENABLE_STT=no
  ask_yesno "Enable self-hosted speaker diarization?\n\nRequires sherpa-onnx ONNX model files placed in ./models yourself." no &&
    ENABLE_DIAR=yes || ENABLE_DIAR=no
  ask_yesno "Enable daily automatic updates?\n\nRuns deploy/update.sh on a systemd timer (git pull + rebuild + restart)." no &&
    ENABLE_AUTOUPDATE=yes || ENABLE_AUTOUPDATE=no
fi

GENERATED_PASSWORD=0
if [[ -z $AUTH_PASSWORD ]]; then
  AUTH_PASSWORD=$(gen_password)
  GENERATED_PASSWORD=1
fi

# --- docker -----------------------------------------------------------------
# No pipe into `grep -q` here on purpose: it exits at the first match, which
# SIGPIPEs apt-cache, and `set -o pipefail` then reports the whole pipeline as
# failed - so every package looks missing. Capture, then match.
# LC_ALL=C keeps the field name "Candidate:" regardless of the host's locale.
have_pkg() {
  local out
  out=$(LC_ALL=C apt-cache policy "$1" 2>/dev/null) || return 1
  [[ $out == *"Candidate:"* && $out != *"Candidate: (none)"* ]]
}

if command -v docker &>/dev/null && as_root docker compose version &>/dev/null; then
  msg_ok "Docker already installed"
else
  msg_info "Installing Docker Engine + Compose plugin"
  as_root apt-get update -qq
  # The Compose v2 plugin is packaged under different names depending on
  # where it comes from: Debian/Ubuntu call it docker-compose-v2, Docker's
  # own apt repo calls it docker-compose-plugin. Pick whichever this box
  # actually has rather than hardcoding one and failing on the other.
  COMPOSE_PKG=""
  for pkg in docker-compose-v2 docker-compose-plugin; do
    if have_pkg "$pkg"; then
      COMPOSE_PKG="$pkg"
      break
    fi
  done
  [[ -n $COMPOSE_PKG ]] || die "No Docker Compose v2 package available (looked for docker-compose-v2 / docker-compose-plugin). Install Docker + Compose manually, then re-run."
  as_root apt-get install -y --no-install-recommends docker.io "$COMPOSE_PKG"
  msg_ok "Docker installed (${COMPOSE_PKG})"
fi
as_root systemctl enable --now docker >/dev/null 2>&1 || true
as_root docker compose version &>/dev/null || die "Docker is installed but \`docker compose\` doesn't work - check the Compose plugin installation."

# --- .env -------------------------------------------------------------------
WRITE_ENV=1
if [[ -f .env ]]; then
  if ask_yesno "A .env already exists.\n\nKeep it as-is? (Choosing No overwrites it with the settings above - your existing password would change.)" yes; then
    WRITE_ENV=0
    msg_ok "Keeping existing .env"
  fi
fi

if ((WRITE_ENV == 1)); then
  # 0600: contains the web UI password.
  (
    umask 077
    cat >.env <<ENVFILE
# Written by deploy/install.sh - see .env.example for every available option.
LORELINE_PORT=${LORELINE_PORT}
LORELINE_AUTH_PASSWORD=${AUTH_PASSWORD}
ENVFILE
  )
  own .env
  msg_ok "Wrote .env"
fi

# Mic passthrough is a compose-file concern, not an env var: an override file
# keeps docker-compose.yml itself untouched (so `git pull` never conflicts).
if [[ $ENABLE_MIC == yes ]]; then
  msg_ok "Microphone passthrough enabled (/dev/snd)"
else
  cat >docker-compose.override.yml <<'OVERRIDE'
# Written by deploy/install.sh: this host has no microphone to pass through
# (or you chose not to). Delete this file and `docker compose up -d` to
# re-enable the /dev/snd passthrough from docker-compose.yml.
services:
  app:
    devices: !reset []
OVERRIDE
  own docker-compose.override.yml
  msg_warn "Microphone passthrough disabled (wrote docker-compose.override.yml)"
fi

# --- bring it up ------------------------------------------------------------
PROFILES=()
[[ $ENABLE_STT == yes ]] && PROFILES+=(--profile local-stt)
[[ $ENABLE_DIAR == yes ]] && PROFILES+=(--profile diarization)

msg_info "Building and starting the stack (first build takes a few minutes)"
as_root docker compose "${PROFILES[@]}" up -d --build
msg_ok "Stack is up"

# Create (but don't start) any optional service not selected above, so
# Settings > Services can start it later. Compose profiles are a client-side
# concept - the Docker API can only start a container that already exists, so
# without this the UI would have nothing to act on.
for profile in local-stt diarization; do
  case " ${PROFILES[*]} " in
  *" ${profile} "*) continue ;;
  esac
  as_root docker compose --profile "$profile" create >/dev/null 2>&1 || true
done
msg_ok "Optional services registered (start them in Settings > Services)"

# --- auto-update timer ------------------------------------------------------
as_root install -m 0644 "${APP_DIR}/deploy/loreline-update.service" /etc/systemd/system/loreline-update.service
as_root sed -i "s#^WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#; s#^ExecStart=.*#ExecStart=${APP_DIR}/deploy/update.sh#" \
  /etc/systemd/system/loreline-update.service
as_root install -m 0644 "${APP_DIR}/deploy/loreline-update.timer" /etc/systemd/system/loreline-update.timer
as_root systemctl daemon-reload
if [[ $ENABLE_AUTOUPDATE == yes ]]; then
  as_root systemctl enable --now loreline-update.timer >/dev/null
  msg_ok "Daily auto-update enabled"
else
  msg_ok "Auto-update installed but disabled"
fi

# --- health check -----------------------------------------------------------
msg_info "Waiting for the app to become healthy"
HEALTHY=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${LORELINE_PORT}/api/system/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 2
done
((HEALTHY == 1)) && msg_ok "App is healthy" || msg_warn "App didn't respond within 60s - check: sudo docker compose logs -f app"

# --- summary ----------------------------------------------------------------
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
IP="${IP:-<this-device>}"
echo ""
echo -e "${GN}────────────────────────────────────────────────────────${CL}"
echo -e " ${GN}Loreline is installed${CL}"
echo -e "${GN}────────────────────────────────────────────────────────${CL}"
echo -e "  Web UI     ${BL}http://${IP}:${LORELINE_PORT}${CL}"
echo -e "  Via Caddy  ${BL}https://${IP}${CL} ${DIM}(self-signed - expect a browser warning)${CL}"
if ((GENERATED_PASSWORD == 1)) && ((WRITE_ENV == 1)); then
  echo ""
  echo -e "  Login password  ${YW}${AUTH_PASSWORD}${CL}"
  echo -e "  ${DIM}Save this now. It's stored in ${APP_DIR}/.env${CL}"
fi
echo ""
echo -e "${DIM}  Logs         sudo docker compose logs -f app"
echo -e "  Update       deploy/update.sh"
[[ $ENABLE_STT == no ]] && echo -e "  Local STT    sudo docker compose --profile local-stt up -d"
[[ $ENABLE_DIAR == no ]] && echo -e "  Diarization  sudo docker compose --profile diarization up -d"
echo -e "  Bluetooth    bash deploy/setup-bluetooth-audio.sh${CL}"
echo ""
