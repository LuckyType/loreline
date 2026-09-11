---
status: accepted
date: 2026-09-12
---

# The client can be the microphone

## Context

Loreline has always assumed the machine it runs on is the machine that hears
the table. `_default_capture` builds a `SoundDeviceSource` over PortAudio,
`docker-compose.yml` passes `/dev/snd` through, `deploy/setup-bluetooth-audio.sh`
exists to wire a host PipeWire session into the container, and
`docs/DEPLOYMENT-NOTES.md` is mostly about that. All of it is correct and all
of it is unreachable for the deployment people actually have: a homelab box in
a cupboard with no sound hardware at all, and a laptop already open on the
table with a microphone in its lid.

That is the single largest adoption barrier the product has, and the thing that
removes it is already in the codebase. `SessionManager.__init__` takes a
`capture_factory`, and what it builds is described in one sentence:

```python
class CaptureSource(Protocol):
    def frames(self) -> AsyncIterator[tuple[bytes, float]]: ...
    def stop(self) -> None: ...
```

A stoppable source of timestamped PCM frames. Nothing downstream asks where
the frames came from: the capture loop records the stats, meters the level,
writes every frame to the continuous WAV, watches the disk, runs Silero and
the `VadChunker`, and hands the result to `StreamPath` or `SttRouter`. A
browser microphone is therefore a second `CaptureSource` and not a second
pipeline, and that distinction is the whole of this decision. The alternative,
a browser-capture path beside the session path, would mean a second WAV writer,
a second VAD, a second liveness story and a second set of bugs for every
feature added afterwards - the same trade ADR 0008 refused for imports.

ADR 0008's own follow-up list names "recording in the browser" as the natural
next step and stops at `MediaRecorder` plus an upload at the end of the
evening. That is a different feature: it produces an import, with no live
transcript, no interim text and nothing on the dashboard until it is over.
This is the live one.

The hard prerequisite is not application code. `navigator.mediaDevices
.getUserMedia` exists only in a secure context, so on `http://10.10.50.55/`
every browser refuses, and no amount of code in this repo changes that. The
deployment answer is Tailscale, below, and the application half is identical
under any answer, which is why it was built first.

## Decision

1. **A browser microphone is a `CaptureSource`.** `ClientCaptureSource`
   (`src/loreline/audio/client_source.py`) takes PCM16 blocks from a WebSocket,
   downmixes, resamples with the same `Pcm16Stream` `SoundDeviceSource` uses
   for a device that serves another rate, re-blocks to the 20 ms frame the
   chunker is built around, and yields `(pcm, monotonic)`. Not one line below
   the factory knows it exists.

2. **The clock is the server's.** Every frame is stamped with
   `time.monotonic()` at the moment it arrives at the process, never from
   anything the browser said. A client clock is a clock nobody can check, and
   the session clock is what the transcript rows, the WAV's utterance index,
   the timeline dots and click-to-jump are all measured against. This is also
   why the browser buffers nothing while it is disconnected: replaying held
   audio would compress a span of the evening into one instant of that clock,
   which is worse than the silence it replaces.

3. **One socket, refused rather than swapped.** `ClientMic` is a registry on
   the app state holding at most one capture socket, because `SessionManager`
   holds at most one `_Runtime` behind a lock. A second `WS /ws/audio/capture`
   is closed with 1013 and a reason the tab reads out. Swapping would hand a
   running session's audio to whichever tab connected last, with nothing on
   any screen to say so.

4. **Frames before a session are free, and they are what makes Start
   answerable.** A browser opens its microphone, the GM watches the meter, and
   only then presses Start. Those frames are discarded; their arrival time is
   not. `ClientCaptureSource.preflight()` asks exactly that - is a socket
   attached, and has it delivered audio in the last few seconds - which is the
   browser's equivalent of opening the device, and it fails the start request
   rather than recording silence.

5. **A refusal says what to do about it.** `_preflight_capture` composed one
   sentence, about an input device and Settings, which is the wrong sentence
   for a browser that has not been granted permission. A source that already
   knows what a GM should be told raises `CaptureUnavailableError`
   (`src/loreline/audio/source.py`) and the manager passes the message through
   verbatim; a raw `PortAudioError` still gets today's device wording.

6. **A socket can come back.** The source survives a disconnect for
   `reconnect_window_s` (45 s), because a laptop that slept for twenty seconds
   should not end the evening. While it is away, `_CaptureStats.since_last_frame`
   climbs and the dashboard's existing "a climbing age is a stopped device"
   warning is already correct about it. On reconnect the gap is **padded with
   silence**, so the WAV's byte offset stays the session clock and every
   timestamp after the gap still points at the right second of the recording.
   Past the window `frames()` raises, and the session ends exactly the way a
   dead device ends it: `_live_finished`, `_end_unattended`, status `error`,
   and a complete re-transcribable WAV of everything that did arrive. The log
   says when audio stopped (`audio.client.audio_stopped`), when it resumed with
   how long the gap was (`audio.client.audio_resumed`), and when it gave up
   (`audio.client.gave_up`).

7. **`StartSessionRequest.source` is the discriminator**, a
   `CaptureSourceKind` of `device` or `client`, defaulting to `device`, so
   every stored default and every existing caller keeps working. The choice is
   remembered in **the browser's own storage**, not in the action defaults: the
   box wired to the table's USB microphone and the laptop that carries its own
   are two right answers on one server, and a shared default would have them
   overwrite each other every session. It also keeps a start that no browser is
   behind honest, since nothing stored server-side can ever select a source
   that needs one.

8. **Every screen says which microphone.** `HealthResponse.capture_source`
   carries the kind, so a phone opening the dashboard reads the session as
   capturing and still knows it is not the page to keep open, while the tab
   that *is* the source is told plainly that closing it ends the recording. The
   one fact the server cannot supply - whether *this* browser is the source -
   is local, and is the only thing read off the client store.

9. **Browser audio processing is off.** `getUserMedia` defaults echo
   cancellation, noise suppression and automatic gain control to on, tuned for
   one person on a headset with a loudspeaker to cancel. At a table all three
   are actively harmful: echo cancellation has no reference signal to cancel
   against and gates anyway; noise suppression is trained on one near voice
   against stationary noise and treats the quieter, further speakers as noise;
   automatic gain control raises the floor in pauses and ducks the loudest
   talker, destroying the relative levels a diarizer clusters on. All three are
   lossy, applied before any code here sees a sample, and cannot be undone by
   re-processing the stored WAV. The server's pipeline expects the raw room
   audio `SoundDeviceSource` delivers, so the client source delivers the same.
   Stated in the capture card as well as in the code, because a GM wearing a
   headset would reasonably want the opposite.

10. **The tab has to stay open, and the UI says so.** `navigator.wakeLock`
    is taken while streaming and re-taken on `visibilitychange`, since a
    browser releases it every time the tab is hidden. A browser that refuses
    the lock is reported as refusing it rather than quietly promising a lid
    that stays awake.

11. **Four failures, four sentences.** Missing secure context, permission
    denied, no input device, and a socket that is not connected have four
    different fixes in four different places. `clientMic.notice` keeps them
    apart; collapsing them into "microphone unavailable" is what sends
    somebody hunting through browser settings for a permission that was never
    the problem. The secure-context sentence names the fix and says outright
    that nothing on the page can change it.

## The secure context, and Tailscale

`getUserMedia` needs HTTPS or `localhost`. Caddy is already in the stack and
already terminates TLS, but with `tls internal` on demand: a certificate from
Caddy's own CA, which every browser distrusts until the CA is installed by
hand on every device - which is exactly the phone and the laptop that were
supposed to be able to just record.

Tailscale issues a real, publicly trusted certificate for the box's
`*.ts.net` name, with nothing to install on the recording device beyond the
Tailscale client it already needs to reach the tailnet. Two integrations were
available and the documented default is `tailscale serve` on the host, which
proxies the tailnet HTTPS name at the app's published port and needs no image
change, no socket mount and no compose edit. Caddy obtaining the certificate
itself is the alternative and is written up as one: it needs
`/var/run/tailscale/tailscaled.sock` mounted into the Caddy container and a
Caddy build carrying Tailscale certificate support, which the official
`caddy:2-alpine` image does not have, for the same result.

One consequence is worth stating on its own, because it fails silently.
`client_uses_https` marks the login cookie `Secure` from the connection's own
scheme, and believes `X-Forwarded-Proto` only from a peer inside
`LORELINE_TRUSTED_PROXIES`. With Tailscale terminating TLS in front, the app
sees plain HTTP from the proxy, so an untrusted proxy address means a genuinely
HTTPS session whose cookie is not marked `Secure`. In the compose stack the
existing `172.16.0.0/12` already covers it, because host traffic to the
published port arrives from the compose bridge gateway: the case
`client_uses_https`'s docstring describes as a hazard in the safe direction is
the intended path here. On a source install the value is `127.0.0.1/32`. The
same peer rule governs the only other header-derived decision, `client_address`
reading `X-Forwarded-For` for the login backoff, and it improves there too: the
bucket becomes per client instead of per proxy.

**Plain HTTP keeps working.** TLS is a requirement for this one feature, not
for the app. `http://10.10.50.55/` loses nothing it had, and the
insecure-context sentence is what points a reader at the README section.

## Consequences

* One new capture source, one new socket, one new request field, one new
  health field. No migration, no new column: which microphone fed a session is
  a fact about the evening, not about the recording it left behind, and the
  WAV a client capture produces is byte-for-byte the kind of thing a device
  capture produces.
* The AudioWorklet processor is the first static asset this app ships that the
  browser fetches by URL rather than through the bundle, because that is what
  an `AudioWorklet` is. It lives in `frontend/static/`, which the SvelteKit
  build copies verbatim and the FastAPI SPA mount serves, and a test asserts
  it arrives with a JavaScript content type - a worklet served as anything
  else is refused outright.
* A homelab box needs no sound hardware, no `/dev/snd` passthrough and none of
  `deploy/setup-bluetooth-audio.sh`. Those paths are unchanged and still the
  right answer for a box that is at the table.
* Bandwidth is the browser's native rate, typically 48 kHz mono PCM16, about
  96 kB/s. Nothing on a LAN notices, and the server resamples once, well, with
  the filter it already has. Asking the `AudioContext` for 16 kHz would cut
  that threefold and was rejected: some browsers refuse a non-native rate
  outright and the rest resample with a filter nobody here can inspect.
* A reconnect costs the audio of the gap. The browser holds nothing back, by
  Decision 2, so what is recovered is the alignment, not the words.

## Follow-ups, deliberately not in this pass

* **A toggle for the audio constraints.** Decision 9 is right for a room
  microphone and wrong for a GM on a headset, and the choice is one checkbox
  and one field on the hello away. It is left out because a wrong default that
  is invisible is the failure worth fixing first, and the default is now right.
* **Several devices feeding one session.** The real fix for one microphone at
  a six-person table is several of them, mixed or diarized per stream. It is a
  much larger design: `ClientMic` holds one socket on purpose, and mixing
  raises clock alignment, per-stream levels and a diarization model that could
  use the channel separation rather than fighting it.
* **Client-side buffering across a reconnect.** Recovering the gap's audio
  needs the client to declare how long the gap was, which is the client clock
  Decision 2 refuses. It would need an explicit, server-checked gap
  declaration, which is a protocol change rather than a buffer.
* **Recording in the browser and uploading afterwards** is already possible
  through the import feature (ADR 0008), and is the fallback where there is no
  secure context at all.
