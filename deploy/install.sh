#!/usr/bin/env bash
# Interactive installer for a Loreline Docker Compose deployment - the
# recommended path. Installs Docker (if missing), writes .env, and brings the
# stack up; everything else (the app, its dependencies, the built UI) lives
# inside the image, not on the host.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/LuckyType/loreline/main/deploy/install.sh | bash
#   bash deploy/install.sh              # interactive, from a checkout
#   bash deploy/install.sh --defaults   # no prompts, use defaults (CI/scripted)
#
# Piped, it clones the repo to /opt/loreline (APP_DIR overrides that) and
# re-runs itself from there, so there is no checkout to make by hand first.
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
command -v apt-get &>/dev/null || die "This installer expects apt (Debian/Ubuntu). On another distro, install Docker plus the Compose and Buildx plugins yourself, then run: docker compose up -d --build"

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

# --- find the checkout, or fetch one ----------------------------------------
# Two supported ways in, and they need different things done first:
#
#   git clone ... && bash deploy/install.sh   the checkout is already here
#   curl -fsSL .../install.sh | bash          there is no checkout yet
#
# Piped, bash reads this script from stdin, so BASH_SOURCE is not set at all
# (under `set -u` the old dirname of it aborted the run) and there is no path
# to take a dirname of anyway. So the test is not "what does BASH_SOURCE look
# like" - that differs between `bash x.sh`, `./x.sh`, `cd deploy && bash
# install.sh` and a pipe - but "is the file I am running the install.sh of a
# checkout that holds the rest of what I need". `-ef` compares device and
# inode, so a relative path that merely happens to exist can't pass by chance.
REPO_URL="${LORELINE_REPO_URL:-https://github.com/LuckyType/loreline.git}"
SELF="${BASH_SOURCE[0]:-}"
CHECKOUT=""
if [[ -n $SELF && -f $SELF ]]; then
  CANDIDATE="$(cd "$(dirname "$SELF")/.." && pwd)"
  if [[ $SELF -ef ${CANDIDATE}/deploy/install.sh && -f ${CANDIDATE}/docker-compose.yml ]]; then
    CHECKOUT="$CANDIDATE"
  fi
fi

# APP_DIR is the same knob install-source.sh takes, with the same default, and
# it wins over whatever was detected above.
APP_DIR="${APP_DIR:-${CHECKOUT:-/opt/loreline}}"
[[ $APP_DIR == /* ]] || APP_DIR="${PWD}/${APP_DIR}"
[[ -d $APP_DIR ]] && APP_DIR="$(cd "$APP_DIR" && pwd)"

if [[ $APP_DIR != "$CHECKOUT" ]]; then
  # Re-exec'd once already and still not inside a checkout: stop rather than
  # spawn ourselves forever.
  [[ -z ${LORELINE_BOOTSTRAPPED:-} ]] ||
    die "Bootstrapped into ${APP_DIR} but still can't find a checkout there. Not looping."

  if [[ -f ${APP_DIR}/docker-compose.yml && -f ${APP_DIR}/deploy/install.sh ]]; then
    # Already installed here. Nothing to clone, and nothing to clean up: the
    # steps below are written to be re-run (they ask before replacing .env,
    # and `compose up -d` is idempotent), so this just routes into them.
    ORIGIN="$(git -C "$APP_DIR" remote get-url origin 2>/dev/null || true)"
    if [[ ${ORIGIN,,} == *luckytype/loreline* || -z $ORIGIN ]]; then
      msg_ok "Using the existing checkout in ${APP_DIR}"
    else
      msg_warn "Using the existing checkout in ${APP_DIR} (origin: ${ORIGIN})"
    fi
  elif [[ -e $APP_DIR ]] && [[ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]]; then
    # Whatever this is, it is somebody else's. An installer that clones over
    # the top of it would be a bug that costs someone their data.
    die "${APP_DIR} already exists and is not a Loreline checkout. Refusing to write into it - move it aside, or re-run with APP_DIR=/some/other/path."
  else
    # git is not a given on a fresh box, and the one-liner is aimed squarely
    # at fresh boxes. ca-certificates for the same reason: cloning over HTTPS
    # from a minimal image otherwise fails on certificate verification.
    if ! command -v git &>/dev/null; then
      msg_info "Installing git"
      as_root apt-get update -qq
      as_root apt-get install -y --no-install-recommends git ca-certificates
    fi
    # Shallow, because a deployment does not need this project's past: 307
    # commits of history is 27MB of .git against roughly one of working tree,
    # and on the Raspberry Pi this installer targets that is mostly transfer
    # time on a slow link.
    #
    # It costs the `git log` an admin might want before letting the box update
    # itself, which is a real thing to give up, so: `git fetch --unshallow` in
    # ${APP_DIR} brings it all back whenever someone wants it, and the summary
    # at the end of this script says so.
    #
    # Every update path keeps working on a shallow checkout, which is the part
    # worth being sure about rather than assuming. deploy/update.sh and
    # update-fast.sh do `rev-parse HEAD`, `fetch origin`, `pull --ff-only origin
    # main` and `diff PREV NEW -- <path>`; the fetch adds the new commits
    # without discarding the one that was HEAD, so both ends of that diff are
    # present and the "what changed in this pull" notes still work.
    msg_info "Cloning ${REPO_URL} into ${APP_DIR}"
    as_root mkdir -p "$APP_DIR"
    as_root git clone --depth 1 "$REPO_URL" "$APP_DIR" ||
      die "Clone failed. Check network access to ${REPO_URL}, or clone it yourself and run bash ${APP_DIR}/deploy/install.sh"
    # Same reasoning as `own` below: a checkout owned by root is one the
    # invoking user can't `git pull` in, and pulling is how they update.
    [[ $EUID -eq 0 ]] || as_root chown -R "$(id -u):$(id -g)" "$APP_DIR"
    msg_ok "Cloned into ${APP_DIR}"
  fi

  # Hand over to the copy in the checkout, with the same arguments. Everything
  # past this point then runs exactly as it does for `bash deploy/install.sh`
  # in a clone - one code path, not two.
  #
  # stdin is reattached to the terminal on the way, and that is the difference
  # between a one-liner that asks and one that only pretends to: piped, stdin
  # *is* this script, so `read` would eat what's left of it and the `[[ -t 0 ]]`
  # test above has already concluded there is nobody to ask. /dev/tty is the
  # controlling terminal whatever stdin was redirected to. Where there is none
  # (CI, a systemd unit), /dev/null keeps the run non-interactive and stops any
  # unread tail of the piped script being read as answers.
  export LORELINE_BOOTSTRAPPED=1
  if (exec </dev/tty) 2>/dev/null; then
    exec bash "${APP_DIR}/deploy/install.sh" "$@" </dev/tty
  fi
  exec bash "${APP_DIR}/deploy/install.sh" "$@" </dev/null
fi

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
    msg_warn "No /dev/snd on this host - no host microphone. Record from the browser instead: \"This device's microphone\" in the capture card."
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

# The updater service's bearer token. Generated unprompted, whether or not this
# box ever runs the updater profile: it costs one line in .env, nothing else
# reads it, and it means a later `docker compose --profile updater up -d` comes
# up with the web UI's update button already working instead of needing a
# hand-edited .env first. Same generator as the login password above.
#
# One value rather than the pair its predecessor needed: the app sends this
# token and the updater service compares it, both reading the same .env entry,
# so there is no hash to keep in step with a plaintext and no second way for the
# two halves to drift apart. It also needs no openssl - gen_password falls back
# to /dev/urandom - so unlike that pair there is no box where this quietly comes
# out empty and leaves the button dead.
UPDATER_TOKEN=$(gen_password)

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
  # Buildx, for the same reason and with the same two spellings. This installer
  # ends in `docker compose up -d --build`, and Compose v2.29 and later refuse
  # to build without it: "compose build requires buildx 0.17.0 or later".
  #
  # It has to be named explicitly. It is a package of its own, and docker.io
  # does not pull it in by any route: on bookworm that package's Recommends are
  # apparmor, ca-certificates, cgroupfs-mount, git, needrestart and xz-utils,
  # with no mention of buildx, so this is not something --no-install-recommends
  # was hiding. Without this line a fresh box gets Docker, gets Compose, and
  # then fails on the one command this script exists to run.
  BUILDX_PKG=""
  for pkg in docker-buildx docker-buildx-plugin; do
    if have_pkg "$pkg"; then
      BUILDX_PKG="$pkg"
      break
    fi
  done
  [[ -n $BUILDX_PKG ]] || die "No Docker Buildx package available (looked for docker-buildx / docker-buildx-plugin). Compose cannot build images without it. Install Docker + Compose + Buildx manually, then re-run."
  as_root apt-get install -y --no-install-recommends docker.io "$COMPOSE_PKG" "$BUILDX_PKG"
  msg_ok "Docker installed (${COMPOSE_PKG}, ${BUILDX_PKG})"
fi
as_root systemctl enable --now docker >/dev/null 2>&1 || true
as_root docker compose version &>/dev/null || die "Docker is installed but \`docker compose\` doesn't work - check the Compose plugin installation."

# Checked here rather than only in the install branch above, because the branch
# that skips installation is exactly where this bites: a box that already had
# Docker and Compose from before buildx was a separate package sails past the
# "Docker already installed" check and then fails at the build, several minutes
# and one generated password later. One attempt to fix it, then a message that
# says what to run, since this is the last point where the answer is still cheap.
if ! as_root docker buildx version &>/dev/null; then
  msg_info "Installing Docker Buildx (Compose needs it to build)"
  as_root apt-get update -qq
  for pkg in docker-buildx docker-buildx-plugin; do
    have_pkg "$pkg" && as_root apt-get install -y --no-install-recommends "$pkg" && break
  done
  as_root docker buildx version &>/dev/null ||
    die "Compose cannot build images without Buildx, and no docker-buildx / docker-buildx-plugin package was available here. Install Buildx for your distro, then re-run this script."
  msg_ok "Docker Buildx installed"
fi

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

# Read only while the optional updater profile is running. The token lets the
# web UI's update button ask the updater service to pull a newer image and
# recreate the app container; docker-compose.yml hands the same value to both
# sides, so they cannot disagree. UPDATER_REPO_DIR is this checkout's path on
# this host, and that service is given the repo at the identical path inside
# itself: the two have to match, or the app container would be recreated against
# bind mounts that do not exist here.
UPDATER_TOKEN=${UPDATER_TOKEN}
UPDATER_REPO_DIR=${APP_DIR}
ENVFILE
  )
  own .env
  msg_ok "Wrote .env"
fi

# --- compose profiles -------------------------------------------------------
# Record a chosen diarization profile in .env, not only in the --profile flag
# passed to the `up` further down. Compose reads its own pre-defined variables
# from three places - "the .env file located in the working directory", the
# shell, and CLI flags - and COMPOSE_PROFILES is one of those variables, so a
# line here is what makes the profile this project's default for every later
# `docker compose` on this box.
#
# Without it the diarization image is built exactly once, by this install, and
# never again: deploy/update.sh runs `up -d --build` with no profile,
# deploy/update-fast.sh recreates only `app`, and neither has any way to know
# this box asked for diarization. The service would stay on its first-install
# image while the app it serves is updated around it.
#
# Only appended when the key is absent, so a value edited by hand (a second
# profile, say) survives re-running this installer, and only for diarization:
# local-stt is pulled rather than built here, so nothing about updating it
# depends on the profile being recorded. Non-fatal - a box whose .env this user
# cannot append to still has a working install, it just needs the manual
# rebuild command that the update scripts print.
if [[ $ENABLE_DIAR == yes ]] && ! grep -q '^COMPOSE_PROFILES=' .env 2>/dev/null; then
  if {
    echo ""
    echo "# Profiles Compose enables without being passed --profile. Written here"
    echo "# because deploy/update.sh passes none, and the diarization service is"
    echo "# built from this checkout: without this line an update rebuilds the app"
    echo "# and leaves the diarizer on the image it was installed with. Remove the"
    echo "# line to stop starting that service on a plain \`docker compose up -d\`."
    echo "COMPOSE_PROFILES=diarization"
  } >>.env 2>/dev/null; then
    msg_ok "Recorded COMPOSE_PROFILES=diarization in .env (so updates rebuild it)"
  else
    msg_warn "Could not add COMPOSE_PROFILES=diarization to .env - add it by hand, or updates will not rebuild the diarization image"
  fi
fi

# Mic passthrough is a compose-file concern, not an env var: an override file
# keeps docker-compose.yml itself untouched (so `git pull` never conflicts).
#
# The override adds the mount rather than removing it, which is the opposite of
# what this script did until the base file stopped mounting /dev/snd
# unconditionally. That inversion is why the default now starts on a box with
# no sound card at all instead of failing outright, and it means the file is
# only written on the hosts that actually record locally.
#
# The `off` branch deletes an override this script wrote before, including one
# in the old shape (`devices: !reset []`, from a box that answered no to this
# question), because against a base file with no `devices:` key that override
# now says nothing. An override written by hand is left alone and reported:
# this script owns the file it writes, not the filename.
if [[ $ENABLE_MIC == yes ]]; then
  cp deploy/mic-passthrough.override.yml docker-compose.override.yml
  own docker-compose.override.yml
  msg_ok "Microphone passthrough enabled (wrote docker-compose.override.yml)"
elif [[ -f docker-compose.override.yml ]]; then
  if grep -q 'deploy/install.sh\|mic-passthrough' docker-compose.override.yml; then
    rm -f docker-compose.override.yml
    msg_ok "Microphone passthrough disabled (removed docker-compose.override.yml)"
  else
    msg_warn "Left your docker-compose.override.yml alone - check it does not pass /dev/snd through, since this host has no microphone"
  fi
else
  msg_info "Microphone passthrough disabled - record from a browser instead (README, \"Recording from a laptop\")"
fi

# --- bring it up ------------------------------------------------------------
PROFILES=()
[[ $ENABLE_STT == yes ]] && PROFILES+=(--profile local-stt)
[[ $ENABLE_DIAR == yes ]] && PROFILES+=(--profile diarization)

# The revision the built image will report in Settings > Client. It has to be
# handed in at build time because git cannot be asked from inside the container
# (the image has no .git - see the Dockerfile), and this is the last moment
# anything knows it.
#
# Both are best-effort. A checkout is not guaranteed here - somebody may have
# unpacked a tarball - and an empty value is read as "unknown" and shown as a
# dash, which is the honest answer, so `|| true` rather than `die`.
#
# `git describe --tags` will usually find no tag on this box and fall back to
# the short SHA, which `--always` is there to guarantee: the clone above is
# --depth 1, so no tag is reachable from HEAD. That is expected, not a fault,
# and a short SHA still names the build exactly. `git fetch --unshallow --tags`
# in ${APP_DIR} and a rebuild is what turns it into a tag name.
BUILD_COMMIT="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
BUILD_DESCRIBED="$(git -C "$APP_DIR" describe --tags --always 2>/dev/null || true)"

msg_info "Building and starting the stack (first build takes a few minutes)"
# Passed through `env` rather than exported, because as_root is `sudo` on a
# non-root box and sudo does not carry the caller's environment across by
# default - an export here would arrive at Compose unset, and the build args in
# docker-compose.yml would quietly take their empty defaults.
as_root env "LORELINE_BUILD_COMMIT=${BUILD_COMMIT}" "LORELINE_BUILD_DESCRIBED=${BUILD_DESCRIBED}" \
  docker compose "${PROFILES[@]}" up -d --build
msg_ok "Stack is up"

# The Speaches image ships with no model and will not fetch one on demand, so
# a box that enabled local STT here would otherwise come up looking healthy and
# 404 on every utterance (GET /v1/models answers 200 with an empty list, which
# is the part that makes it look fine). Install one now, while we still have
# the operator's attention.
#
# `small` rather than `large-v3`: SttRouter allows 30 s per utterance, and
# large-v3 on a CPU blows through that on every one of them, which shows up as
# a re-transcription that runs forever and writes nothing. Multilingual,
# because the distil-whisper builds are English only. LORELINE_STT_MODEL
# overrides it for a box with the cores to spare.
STT_MODEL="${LORELINE_STT_MODEL:-Systran/faster-whisper-small}"
if [[ $ENABLE_STT == yes ]]; then
  if command -v curl &>/dev/null; then
    msg_info "Installing the ${STT_MODEL} model (a few hundred MB, one time)"
    # Give the service a moment to bind before asking it for anything.
    for _ in $(seq 1 30); do
      curl -fsS "http://127.0.0.1:8200/v1/models" >/dev/null 2>&1 && break
      sleep 2
    done
    # Non-fatal on purpose: a slow or absent download is not a reason to fail
    # an install that is otherwise complete, and the exact command to retry is
    # one line. The app works with a cloud provider either way.
    if as_root curl -fsS -X POST "http://127.0.0.1:8200/v1/models/${STT_MODEL}" >/dev/null 2>&1; then
      msg_ok "Local STT model installed (${STT_MODEL})"
    else
      msg_warn "Could not install ${STT_MODEL}. Local STT will 404 until you run:\n   curl -X POST http://127.0.0.1:8200/v1/models/${STT_MODEL}"
    fi
  else
    msg_warn "curl not found, so the local STT model was not installed. Run:\n   curl -X POST http://127.0.0.1:8200/v1/models/${STT_MODEL}"
  fi
fi

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
  # /livez, not /healthz: the snapshot needs a session cookie, and this poll
  # only asks whether the process is answering yet.
  if curl -fsS "http://127.0.0.1:${LORELINE_PORT}/api/system/livez" >/dev/null 2>&1; then
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
# Only when it is actually shallow, which is asked of git rather than inferred
# from whether this run did the cloning: a checkout that was already here, or
# one cloned by hand, has its full history and does not want this line.
if [[ $(git -C "$APP_DIR" rev-parse --is-shallow-repository 2>/dev/null) == true ]]; then
  echo -e "  History      git -C ${APP_DIR} fetch --unshallow ${DIM}(cloned shallow to save the download)${CL}${DIM}"
fi
[[ $ENABLE_STT == no ]] && echo -e "  Local STT    sudo docker compose --profile local-stt up -d"
[[ $ENABLE_DIAR == no ]] && echo -e "  Diarization  sudo docker compose --profile diarization up -d\n               (then add COMPOSE_PROFILES=diarization to .env, or updates won't rebuild it)"
[[ $ENABLE_MIC == no ]] && echo -e "  Microphone   no /dev/snd passed through; record from the browser instead,\n               or: cp deploy/mic-passthrough.override.yml docker-compose.override.yml"
echo -e "  Bluetooth    bash deploy/setup-bluetooth-audio.sh${CL}"
echo ""
