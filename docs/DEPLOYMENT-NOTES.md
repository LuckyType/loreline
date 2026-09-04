# Deployment notes

Specifics about running Loreline on a real device, the ones that cost time to
discover. The step-by-step install lives in the README's
[Deployment](../README.md#deployment) section.

## Bluetooth microphones need an Ubuntu-based image

If you use a Bluetooth mic, or any source that exists only inside the host's
PipeWire/PulseAudio session rather than as a raw ALSA device, three things all
have to be true. Miss one and the device simply never appears in the picker,
with nothing in the logs to explain why.

**The image must be Ubuntu 26.04 or newer.** Debian's `libportaudio2`, which
every `python:*-slim` image inherits, is built with the ALSA and JACK host APIs
only, no PulseAudio. A wired or USB mic still works, since that is ALSA through
`/dev/snd`, but a PipeWire-routed source is unreachable. Installing `libpulse0`
does not help, because the host API is not compiled in.

Verified with `ldd $(ls /usr/lib/*/libportaudio.so.2)`:

| Base image | links `libpulse` |
|---|---|
| `python:3.12-slim` (Debian) | no |
| `ubuntu:24.04` | no |
| `ubuntu:25.10` | no |
| `ubuntu:26.04` | yes |

That is why the Dockerfile pins `ubuntu:26.04` and gets Python from `uv` instead
of using a Python base image.

**The host's PipeWire socket must be bind-mounted in**, through a gitignored
`docker-compose.override.yml`:

```yaml
services:
  app:
    volumes:
      - /run/user/<UID>/pulse:/run/user/<UID>/pulse
    environment:
      XDG_RUNTIME_DIR: /run/user/<UID>
```

`<UID>` is the user whose session owns the paired device.

**That user needs a lingering session with the right WirePlumber profile.**
WirePlumber's Bluetooth monitor starts only once logind reports an active seat,
which a lingering, never-logged-in session never provides. Without this the
monitor silently never starts. WirePlumber ships a profile for exactly this
case:

```ini
# /etc/systemd/user/wireplumber.service.d/10-systemwide.conf
[Service]
ExecStart=
ExecStart=/usr/bin/wireplumber --profile=main-systemwide
```

`deploy/setup-bluetooth-audio.sh` does all of this plus the pairing.

Whichever user owns that PipeWire session is load-bearing. Deleting it, or
disabling its linger, kills Bluetooth audio even though the app does not run as
that user. Worth a note of your own if it is a leftover service account.

## There is no in-app update button in a Docker deployment

By design. The container has no systemd unit to restart itself with, and giving
it the Docker socket to do so would be root on the host, not a trade this
project makes silently. The app detects `/.dockerenv` and says so, rather than
attempting systemd-only mechanics and failing confusingly.

Update from the host instead:

```bash
deploy/update.sh                                   # git pull + rebuild + recreate
sudo systemctl enable --now loreline-update.timer  # daily, installed but off by default
```

The source plus systemd deployment does have the in-app button, since there the
app has a real unit to drive.

## Shell gotchas that bit the installer

Both turned up running against a real host, not in review.

`set -o pipefail` plus an early-exiting consumer. `apt-cache policy X | grep -q`
returns 141: `grep -q` exits at the first match, SIGPIPEs `apt-cache`, and
pipefail surfaces that as failure, so a package that plainly exists looks
missing. It does not reproduce with short inputs, because `grep` consumes them
fully before exiting. Capture into a variable, then match.

The Compose plugin package name differs by vendor. Debian and Ubuntu ship
`docker-compose-v2`; Docker's own apt repo calls it `docker-compose-plugin`.
Probe for both.

## Networking

A static IP on the client may be dropped silently on a managed network. Setting
one applied cleanly, `netplan generate` passed with no errors, but the host never
came back on that address. That is the signature of DHCP snooping or IP source
guard rejecting an address the controller never leased. Prefer a DHCP
reservation on the controller side. It gives the same stable address and keeps
the controller authoritative.

When changing network config on a remote box, arm an auto-revert first so a
mistake does not cost you physical access:

```bash
systemd-run --on-active=180 --unit=netplan-revert --collect \
  bash -c "cp /root/netplan-backup.yaml /etc/netplan/00-installer-config.yaml && netplan apply"
```

A controller listing one machine under two names is usually not a bug. It tracks
clients per MAC, so a box with both ethernet and Wi-Fi appears twice, each
remembered under whatever hostname it had when that interface was first seen.
Check `ip -br link`.

## The first capture is slower than the first request

Every heavy native import, torch, silero-vad and sounddevice, is deferred to the
point where it is actually needed rather than done at module import. The app
therefore answers `/api/system/healthz` quickly after `docker compose up`, but
the first Start after a fresh boot pays the model load, which runs well over a
minute on modest hardware. If a capture looks hung right after an install, give
it longer before digging in.
