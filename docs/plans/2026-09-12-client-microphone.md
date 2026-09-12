# Plan: capture from the client device's microphone

Goal: a GM opens Loreline on the laptop that is already on the table, picks
"this device's microphone", and presses Start. The audio is captured by the
browser and streamed to the server, which runs the session exactly as it runs
one from a local microphone: live transcript, VAD, streaming or batch STT,
diarization, the stored WAV, re-processing later. Loreline can then live on a
homelab machine with no sound hardware at all, which removes the single
largest adoption barrier the product has.

## The seam this fits into

`src/loreline/session/manager.py` already defines what a capture source is:

```python
class CaptureSource(Protocol):
    def frames(self) -> AsyncIterator[tuple[bytes, float]]: ...
    def stop(self) -> None: ...

class PreflightCapture(Protocol):          # optional half, structural check
    async def preflight(self) -> None: ...

CaptureFactory = Callable[[StartSessionRequest, int], tuple[CaptureSource, SpeechDetector]]
```

`SessionManager.__init__` already takes `capture_factory`, and
`_default_capture` builds the PortAudio source. A browser microphone is
therefore a second `CaptureSource` whose frames arrive over a WebSocket, and
**nothing downstream changes**: the capture loop, `_CaptureStats`, the level
publisher, the disk watch, the WAV writer, `StreamPath` and `SttRouter` all
consume the same frames. Do not add a parallel pipeline.

## Hard prerequisite: a secure context

`navigator.mediaDevices.getUserMedia` is only available in a secure context:
HTTPS, or `localhost`. The deployment today is plain HTTP on a LAN IP
(`http://10.10.50.55/`), where **every browser will refuse microphone access**,
and no amount of application code changes that.

The application half of this plan is identical regardless of how TLS is
obtained, so build it first. The UI must detect `window.isSecureContext ===
false` and say precisely that, naming the fix, rather than showing a broken
picker or a permission error the GM cannot act on. The deployment half (Caddy
configuration and README) follows the route the user picks; the stack already
runs Caddy, so it is configuration rather than a new component.

## Backend

1. **`ClientCaptureSource`** in a new `src/loreline/audio/client_source.py`:
   frames arrive on an `asyncio.Queue` fed by the WebSocket, `frames()` yields
   `(pcm, monotonic)` stamped **on arrival at the server**, never from a client
   clock. It resamples from the browser's rate to the session's target rate
   with `Pcm16Stream` in `src/loreline/audio/resample.py`, exactly as
   `SoundDeviceSource` resamples a device that serves another rate.
   `preflight()` proves the socket is connected and has delivered audio
   recently, which is the browser equivalent of opening the device: a GM who
   has not granted permission, or whose tab has gone, fails Start rather than
   recording silence. `stop()` ends the iteration.

2. **A registry for the pending capture socket.** There is at most one capture
   per process (`SessionManager` holds one `_Runtime` behind a lock), so there
   is at most one client capture socket. Hold it in app state, with the
   invariant written down: a second socket is refused with a close code and a
   reason, never silently swapped.

3. **`WS /ws/audio/capture`** in `src/loreline/web/routes/audio.py`,
   authenticated the same way the existing sockets are (see
   `src/loreline/web/ws_util.py` and the `/ws/audio/level` route). Protocol:
   the client sends one JSON hello (`sample_rate`, `channels`, an optional
   device label for the log), the server acknowledges, then the client sends
   binary PCM16 frames. Frames arriving before a session starts keep the
   pre-flight fresh and are otherwise discarded, so opening the microphone
   early costs nothing.

4. **Start request.** `StartSessionRequest` gains a source discriminator, for
   example `source: "device" | "client"`, defaulting to `"device"` so every
   stored default and every existing caller keeps working. The capture factory
   picks the source from it. A `client` start with no live socket fails with a
   message that says the browser is not streaming, not that a device is
   missing.

5. **Disconnect mid-session.** Frames stopping is already visible:
   `_CaptureStats.since_last_frame` drives the existing "a climbing age is a
   stopped device" warning, and the dashboard already surfaces it. Add a
   reconnect window: the browser may re-open the socket and resume feeding the
   same session, because a laptop that slept for twenty seconds should not end
   the evening's recording. Past the window the session ends the way a dead
   device ends it. Whatever you choose, the recorded WAV keeps everything that
   did arrive, and the log says when audio stopped and resumed.

6. **Autostart stays a device concept.** Autostart runs with no browser
   present, so it cannot select a client source; make that explicit rather
   than letting it fail at start time.

## Frontend

1. **`clientMic.svelte.ts`** in `frontend/src/lib`: `getUserMedia`, an
   `AudioWorklet` (not the deprecated `ScriptProcessor`) that converts Float32
   to PCM16 and posts buffers, a WebSocket that sends the hello and then the
   frames, a local peak/RMS level for the meter before a session starts,
   `navigator.wakeLock` so the lid staying open is not the GM's problem, and
   reconnect with backoff that reuses `frontend/src/lib/ws.ts` where it fits.
   Stop releases the tracks, so the browser's recording indicator goes out.

2. **Capture card** (`frontend/src/lib/CaptureControls.svelte`): a source
   choice between this device and a server microphone. The existing device
   picker stays for the server source. For the client source, list the
   browser's inputs with `enumerateDevices()`, remembering that labels are
   blank until permission is granted, so the first interaction is "Allow
   microphone" and the list fills in after. Show the live client level in the
   existing meter. State, clearly and separately: secure context missing,
   permission denied, no input device, socket not connected. Remember the
   choice in the action defaults so the box on the table and the laptop each
   keep their own habit.

3. **Honesty about what is running.** When a client capture is live, the
   dashboard should say the audio is coming from this browser, and a second
   tab or a phone opening the dashboard should see the session as capturing
   but know it is not the source. The header badge and the capture card
   already read the manager's state; extend the wording rather than the model.

## Deployment and docs

Once the user picks a TLS route, the Caddy configuration and the README get
it, plus a short "recording from a laptop" section describing the trade: no
sound hardware needed on the server, and the tab must stay open. `.env.example`
documents any new setting.

## Tests

- Unit: `ClientCaptureSource` yields what the socket feeds, resamples when the
  hello rate differs from the target, fails pre-flight without frames, and
  stops cleanly.
- Integration: the socket refuses an unauthenticated caller and a second
  concurrent connection; a `client` start with no socket is refused; a start
  with a fed socket produces a session whose WAV holds what was sent, using
  the fake provider the existing session tests use.
- The frontend has no test runner, so the browser half is covered by
  `npm run check` and the build.

## Out of scope, note as follow-ups in the ADR

- Recording in the browser and uploading afterwards: that is already possible
  through the import feature (ADR 0008) and is the fallback when there is no
  secure context.
- Several devices feeding one session, which is the real fix for one
  microphone at a six-person table and is a much larger design.
- Echo cancellation and noise suppression choices: `getUserMedia` constraints
  default to on for speech, which is wrong for a room mic; pick deliberately
  and say why.
