"""OpenAI Realtime transcription connector (WebSocket).

OpenAI's Realtime API offers a transcription-only session (``type:
"transcription"``) that streams transcript deltas as audio arrives. This
connector implements both connector shapes over it, on two separate sockets,
because the two need incompatible session configurations.

**Streaming** (``StreamingConnector``, what a live capture takes) is the shape
this session type was built for and the reason it is first to be migrated (ADR
0006). Server VAD is on, so OpenAI decides the turns from the audio rather than
this app cutting them:

Not every model allows that, and the two this repo routes here are the two that
do not: ``gpt-live-transcribe`` and ``gpt-realtime-whisper`` both answer a
server-VAD ``session.update`` with "Turn detection is not supported for this
transcription model", while ``gpt-4o-transcribe``, ``gpt-4o-mini-transcribe``
and ``whisper-1`` accept it on the same socket. So :meth:`open_stream` raises
``StreamUnsupportedError`` on that refusal and the session runs this same
connector one utterance at a time instead, exactly as it did before streaming
existed. It is checked against the vendor rather than declared in
capabilities.yaml because the vendor is where the answer lives and the yaml has
no field for it yet; see ADR 0006 for the note that it probably should.

With server VAD on: ``speech_started`` opens a turn and carries the offset
its ``start_ts`` is derived from, ``delta`` events grow it as interim text,
``speech_stopped`` carries the offset for its ``end_ts``, and ``completed``
settles it. All four name the same ``item_id``, which is the handle the stream
correlates them by, and which becomes the turn id a growing interim is replaced
under. Audio is appended continuously and never committed by hand except once,
at the very end, to flush whatever turn was open when the microphone stopped.

**One utterance per call** (``Connector``, ADR 0005) is kept for two callers:
the call-shaped fallback path, where a session that lost its streaming primary
finishes on complete utterances, and the tests that predate streaming. Here
turn detection is off, the caller's utterance is appended and committed by
hand, and the reply is read until one ``completed`` arrives. It is one socket
for the whole session either way; ``_ensure_ws`` has cached it since before
streaming existed.

Which OpenAI transcription models reach this connector rather than the batch
one is decided per model in capabilities.yaml: gpt-live-transcribe and
gpt-realtime-whisper stream, while gpt-transcribe / gpt-4o-transcribe /
whisper-1 post through the ``openai_compat`` backend instead.

Docs:
- https://developers.openai.com/api/docs/guides/realtime-transcription
- https://developers.openai.com/api/docs/guides/realtime-vad
- https://developers.openai.com/api/docs/guides/realtime-websocket
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from loreline.audio.chunker import Utterance
from loreline.audio.resample import resample_pcm16
from loreline.capabilities import surface_for
from loreline.capability_config import TranscribeCapabilities
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._ws import (
    as_dict,
    as_obj_dict,
    close_socket,
    get_float,
    get_str,
)
from loreline.stt.base import Connector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register
from loreline.stt.streaming import (
    StreamAlive,
    StreamingConnector,
    StreamSignal,
    StreamUnsupportedError,
    TurnEnded,
    TurnFinal,
    TurnPartial,
    TurnStarted,
)

log = get_logger(__name__)

_COMPLETED = "conversation.item.input_audio_transcription.completed"
_DELTA = "conversation.item.input_audio_transcription.delta"
_FAILED = "conversation.item.input_audio_transcription.failed"
_SPEECH_STARTED = "input_audio_buffer.speech_started"
_SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
# OpenAI's own endpointing, which is the whole point of the streaming shape:
# it decides turns from the audio it is hearing rather than from a local VAD
# that has never heard the model. The values are OpenAI's documented defaults,
# restated so a change here is a change with a reason rather than a silent
# inheritance. 500 ms of silence closes a turn, against the 800 ms the local
# chunker needs, and the padding keeps a turn's first phoneme.
_SERVER_VAD: dict[str, object] = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 500,
}
# OpenAI Realtime rejects input sample rates below 24 kHz, while our capture
# pipeline is locked to 16 kHz by Silero VAD. Upsample to this rate on the way out.
_OUTPUT_RATE = 24_000
# OpenAI rejects a transcription prompt beyond this ("string_above_max_length"),
# and the rejection voids the whole session.update - language included.
_PROMPT_MAX_CHARS = 1024
_CONFIGURE_TIMEOUT_S = 10.0


def _capped_prompt(terms: list[str]) -> tuple[str | None, int]:
    """Join glossary terms into a prompt within OpenAI's length limit.

    Keeps whole leading terms only (glossary order is priority order); returns
    the prompt and how many trailing terms were dropped to fit.
    """
    kept: list[str] = []
    length = 0
    for term in terms:
        addition = len(term) + (2 if kept else 0)  # ", " separator
        if length + addition > _PROMPT_MAX_CHARS:
            break
        kept.append(term)
        length += addition
    return ", ".join(kept) or None, len(terms) - len(kept)


class OpenAIRealtimeBackend(Connector[None], StreamingConnector):
    """Transcription over an OpenAI Realtime transcription session, both shapes.

    See the module docstring for what each shape does. They hold separate
    sockets because the session configuration differs in exactly the thing that
    matters, ``turn_detection``, and one connection cannot be configured both
    ways at once.

    Nothing is prepared per call: the glossary prompt lives on the instance
    (``_prompt``) because every ``session.update`` reads it, so :meth:`prepare`
    sets it as a side effect and returns None. ``_prompt_rejected`` is the one
    thing deliberately kept across connections: a model that refuses the prompt
    parameter will refuse it on the next socket too, and re-learning that would
    cost a round trip and a voided session config per reconnect.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        api_key: str | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(config)
        self._api_key = api_key
        self._language = language or config.language
        self._model = model
        self._endpoint = surface_for(config, Interaction.TRANSCRIBE, "realtime")
        self._url = self._endpoint.url
        self._out_rate = max(config.sample_rate, _OUTPUT_RATE)
        self._ws: ClientConnection | None = None
        self._stream_ws: ClientConnection | None = None
        self._prompt: str | None = None
        self._prompt_rejected = False  # model refused the prompt param; stop sending it

    @property
    def _headers(self) -> dict[str, str]:
        return self._endpoint.request_headers(self._api_key)

    def _session_update(self, turn_detection: dict[str, object] | None = None) -> str:
        transcription: dict[str, object] = {"language": self._language}
        # A transcription session with no model named runs OpenAI's own
        # default, which is the right thing to inherit when nobody chose.
        if self._model:
            transcription["model"] = self._model
        if self._prompt and not self._prompt_rejected:
            # Bias recognition toward the campaign glossary (spell/char/place names).
            transcription["prompt"] = self._prompt
        return json.dumps(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcm",
                                "rate": self._out_rate,
                            },
                            "transcription": transcription,
                            "turn_detection": turn_detection,
                        }
                    },
                },
            }
        )

    def prepare(self, glossary: Glossary | None) -> None:
        """Set the session prompt from the glossary; see the class docstring.

        Truncation is logged once per glossary rather than per utterance,
        which is what comparing against the prompt already set achieves.
        """
        prompt, dropped = _capped_prompt(glossary_terms(glossary))
        if dropped and prompt != self._prompt:
            log.warning(
                "openai.realtime.glossary_truncated",
                provider=self.config.id,
                dropped_terms=dropped,
                max_chars=_PROMPT_MAX_CHARS,
            )
        self._prompt = prompt

    async def _ensure_ws(self) -> ClientConnection:
        """Open + configure the transcription session, reusing it across calls.

        OpenAI's transcription session stays open and emits one completed event per
        ``input_audio_buffer.commit``, so a single connection serves the whole
        session - no fresh WebSocket handshake per utterance.
        """
        if self._ws is None:
            self._ws = await self._connect(None)
        return self._ws

    async def _connect(self, turn_detection: dict[str, object] | None) -> ClientConnection:
        """Open one configured transcription session, or close it and raise."""
        ws = await connect(self._url, additional_headers=self._headers)
        try:
            await self._configure(ws, turn_detection)
        except BaseException:
            await close_socket(ws)
            raise
        return ws

    async def _configure(
        self, ws: ClientConnection, turn_detection: dict[str, object] | None
    ) -> None:
        """Send ``session.update`` and wait until the server settles it.

        Draining the config handshake here keeps a rejection out of the
        per-utterance receive loop, where it would silently swallow an
        utterance's transcript (and count as success, so no fallback fires).
        A rejected prompt - a model without prompt support - downgrades the
        session once to a promptless update, so the language/format config
        still applies instead of being voided along with the prompt.

        A rejected ``turn_detection`` is the other kind of no, and it is
        final: without the server deciding the turns there is nothing for the
        streaming shape to cut on, so this raises rather than carrying on into
        a session that would accept audio forever and answer nothing. See
        :class:`StreamUnsupportedError` for which models say it.

        Every other rejection raises too, and that is the point of draining
        the handshake here at all. A session that answered ``error`` and was
        treated as configured is a socket with no server VAD on it: it takes
        audio, produces nothing, and is only noticed ``watchdog_s`` later by
        the liveness timer, having cost a gap marker and a failover for an
        answer that was on the wire at connect. Raising makes it one failed
        attempt against the stream's reconnect budget instead.
        """
        await ws.send(self._session_update(turn_detection))
        async with asyncio.timeout(_CONFIGURE_TIMEOUT_S):
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind.endswith("session.updated"):
                    return
                if kind != "error":
                    continue  # session.created and other chatter
                detail = as_obj_dict(message.get("error", message))
                if "turn_detection" in get_str(detail, "param"):
                    raise StreamUnsupportedError(
                        f"{self._model or 'this model'} does not support server-side turn "
                        f"detection: {get_str(detail, 'message')}"
                    )
                if not self._prompt_rejected and "transcription.prompt" in get_str(detail, "param"):
                    self._prompt_rejected = True
                    log.warning(
                        "openai.realtime.prompt_rejected",
                        provider=self.config.id,
                        detail=detail,
                    )
                    await ws.send(self._session_update(turn_detection))
                    continue
                log.warning(
                    "openai.realtime.error",
                    provider=self.config.id,
                    event_type=kind,
                    detail=detail,
                )
                msg = f"OpenAI refused the session configuration: {get_str(detail, 'message')}"
                raise RuntimeError(msg)
        # The socket ended before it said anything about the update, which is
        # the same thing as a refusal for everyone downstream: nothing here is
        # configured, so nothing would be transcribed.
        msg = "the session ended before it was configured"
        raise RuntimeError(msg)

    async def _reset_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await close_socket(ws)

    async def transcribe_one(self, utterance: Utterance, prepared: None) -> Transcription:
        pcm = resample_pcm16(utterance.pcm, self.config.sample_rate, self._out_rate)
        transcript = ""
        try:
            ws = await self._ensure_ws()
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            async for raw in ws:
                message = as_dict(raw)
                kind = get_str(message, "type")
                if kind == _COMPLETED:
                    transcript = get_str(message, "transcript")
                    break
                if kind in {_FAILED, "error"}:
                    detail = message.get("error", message)
                    log.warning(
                        "openai.realtime.error",
                        provider=self.config.id,
                        event_type=kind,
                        detail=detail,
                    )
                    break
        except (OSError, WebSocketException):
            await self._reset_ws()  # drop the dead session; the next utterance reconnects
            raise
        return Transcription(text=transcript)

    # -- the streaming shape ---------------------------------------------

    @property
    def stream_rate(self) -> int:
        return self._out_rate

    async def open_stream(self, glossary: Glossary | None) -> None:
        """Open a server-VAD transcription session for a whole capture.

        The glossary prompt is applied here rather than per turn: the session
        carries it, and a reconnect re-applies it because this runs again.
        """
        self.prepare(glossary)
        await self.close_stream()  # a reconnect must not leak the dead socket
        self._stream_ws = await self._connect(_SERVER_VAD)

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._stream_ws
        if ws is None:
            msg = "send_audio before open_stream"
            raise RuntimeError(msg)
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )
        )

    async def signals(self) -> AsyncIterator[StreamSignal]:
        """Translate this session's events into signals.

        The offsets are OpenAI's own count of milliseconds into the audio it
        has received on this connection, which is exactly what the stream's t0
        mapping expects; nothing here converts to the session clock.

        ``delta`` is an increment, not the whole interim so far, hence
        ``append=True``.

        Two kinds of bad news arrive here and they are not the same size.
        ``error`` is about the session and ends the iteration: a transcription
        session that rejected something is not going to start working on the
        next frame, and ending here is what lets the stream reconnect or fail
        over instead of streaming into silence. ``...transcription.failed`` is
        about *one item* - audio too short to transcribe, a content filter -
        and ending a whole live session on one of those threw away every turn
        after it, plus a gap marker, for a two-word backchannel. It settles
        that item instead: an empty final, which the stream publishes as the
        turn's last interim text if it had any and drops if it had none.

        Everything else OpenAI narrates - ``session.updated``,
        ``input_audio_buffer.committed``, ``conversation.item.created`` - is
        yielded as :class:`StreamAlive`, since the liveness watchdog counts
        signals it was told about and a session between turns is a session
        that is alive.
        """
        ws = self._stream_ws
        if ws is None:
            return
        async for raw in ws:
            message = as_dict(raw)
            kind = get_str(message, "type")
            ref = get_str(message, "item_id")
            if kind == _SPEECH_STARTED:
                yield TurnStarted(at=_seconds(message, "audio_start_ms"), ref=ref)
            elif kind == _SPEECH_STOPPED:
                yield TurnEnded(at=_seconds(message, "audio_end_ms"), ref=ref)
            elif kind == _DELTA:
                yield TurnPartial(text=get_str(message, "delta"), ref=ref, append=True)
            elif kind == _COMPLETED:
                yield TurnFinal(text=get_str(message, "transcript"), ref=ref)
            elif kind == _FAILED:
                log.warning(
                    "openai.realtime.item_failed",
                    provider=self.config.id,
                    item_id=ref,
                    detail=message.get("error", message),
                )
                yield TurnFinal(text="", ref=ref)
            elif kind == "error":
                log.warning(
                    "openai.realtime.error",
                    provider=self.config.id,
                    event_type=kind,
                    detail=message.get("error", message),
                )
                return
            else:
                yield StreamAlive()

    async def flush_input(self) -> None:
        """Commit once, so a turn still open when the mic stopped is transcribed.

        Server VAD commits on its own at every turn boundary it finds, so this
        is only ever about the last one. A commit with nothing buffered is
        answered with an ``input_audio_buffer_commit_empty`` error, which the
        reader logs and stops on, which is the right ending for a session that
        was ending anyway.
        """
        ws = self._stream_ws
        if ws is not None:
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def close_stream(self) -> None:
        """Drop the streaming socket, bounded and cancellation-safe."""
        ws, self._stream_ws = self._stream_ws, None
        if ws is not None:
            await close_socket(ws)

    async def aclose(self) -> None:
        await self._reset_ws()
        await self.close_stream()


def _seconds(message: dict[str, object], key: str) -> float | None:
    """A vendor offset in milliseconds, as seconds, or None when absent.

    None matters: it is the difference between "this turn began 1.2s in" and
    "this vendor did not say", and the stream falls back to the last frame
    written for the second one rather than pinning the turn to zero.
    """
    return get_float(message, key) / 1000.0 if key in message else None


@register(ProviderKind.OPENAI, realtime=True)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig,
    secrets: SecretStore,
    model: str | None,
    # Unused: this connector's prompt has no per-model ceiling in the yaml to
    # read, so the resolved capabilities say nothing it acts on.
    _caps: TranscribeCapabilities | None,
) -> OpenAIRealtimeBackend:
    return OpenAIRealtimeBackend(config, model=model, api_key=secret_for(config, secrets))
