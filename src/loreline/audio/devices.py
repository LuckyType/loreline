"""Audio input device enumeration (sounddevice / PortAudio).

Relaxed type-checking: ``sounddevice`` is an optional native dependency
installed only via the ``audio`` extra.
"""

# pyright: reportMissingImports=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportMissingModuleSource=false

from __future__ import annotations

from dataclasses import dataclass

# When a software audio server (PipeWire/PulseAudio) is present, PortAudio's
# ALSA host API also enumerates its plain software-routing PCM aliases
# alongside the real hardware devices - every one of them ultimately reaching
# the exact same destination as the "system default" choice already offers,
# so they'd only clutter the picker with indistinguishable duplicates.
_ALSA_ALIAS_NAMES = frozenset(
    {
        "pipewire",
        "pulse",
        "default",
        "sysdefault",
        "dmix",
        "front",
        "surround21",
        "surround40",
        "surround41",
        "surround50",
        "surround51",
        "surround71",
        "iec958",
        "hdmi",
    }
)


@dataclass(slots=True, frozen=True)
class InputDevice:
    """A selectable audio input device."""

    index: int
    name: str
    channels: int
    default_samplerate: float


def list_input_devices() -> list[InputDevice]:
    """Return available input devices via PortAudio.

    Returns an empty list if ``sounddevice`` (the ``audio`` extra) is not
    installed or no audio backend is available. Filters out ALSA's software
    passthrough aliases (see ``_ALSA_ALIAS_NAMES``) and PulseAudio/PipeWire
    ``*.monitor`` sources - a loopback tap of what a sink is *playing back*,
    not a microphone - so the list only shows genuinely distinct devices.
    """
    try:
        import sounddevice as sd  # noqa: PLC0415
    except (ImportError, OSError):  # pragma: no cover - host without audio backend
        return []

    try:
        hostapi_names = [str(a.get("name", "")) for a in sd.query_hostapis()]
    except Exception:  # pragma: no cover - defensive, host API query is optional
        hostapi_names = []

    devices: list[InputDevice] = []
    for index, info in enumerate(sd.query_devices()):
        channels = int(info.get("max_input_channels", 0))
        if channels <= 0:
            continue
        name = str(info.get("name", f"device-{index}"))
        if name.endswith(".monitor"):
            continue
        hostapi = info.get("hostapi")
        hostapi_name = ""
        if isinstance(hostapi, int) and 0 <= hostapi < len(hostapi_names):
            hostapi_name = hostapi_names[hostapi]
        if hostapi_name == "ALSA" and name.strip().lower() in _ALSA_ALIAS_NAMES:
            continue
        devices.append(
            InputDevice(
                index=index,
                name=name,
                channels=channels,
                default_samplerate=float(info.get("default_samplerate", 16000.0)),
            )
        )
    return devices
