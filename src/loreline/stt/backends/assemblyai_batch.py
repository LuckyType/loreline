"""AssemblyAI batch STT connector (async pre-recorded API).

UNVERIFIED: written from AssemblyAI's documentation and exercised only against a
mocked transport, never against the real API, because this environment has no
AssemblyAI key. The model it unlocks, ``universal-2``, is therefore
``hidden: true`` in capabilities.yaml until someone runs it with a key; the note
above that entry says what flipping the gate requires.

Why it exists: universal-2 is a pre-recorded model ("High-volume,
price-sensitive batch transcription", absent from every streaming model list),
so with only the WebSocket connector in ``assemblyai.py`` there was no way to
reach it and capabilities.yaml could not honestly offer it.

Unlike Deepgram's single request/response, this API is a job queue, and the
three steps are the substance of this connector:

1. ``POST /v2/upload`` with the raw bytes, which answers with an ``upload_url``
   only AssemblyAI's own servers can read. (The alternative is hosting the audio
   ourselves on a URL the vendor can fetch, which a Raspberry Pi behind a home
   router cannot do.)
2. ``POST /v2/transcript`` creates the job and returns immediately with an id
   and ``status: "queued"``.
3. ``GET /v2/transcript/{id}`` until the status leaves queued/processing.

Polling therefore needs three things a single request never does: a schedule
that neither hammers the API nor adds seconds of latency to a two-second
utterance (see ``_await_completion``), a ceiling so a job that never finishes
cannot hang a session forever, and cleanup, because giving up locally does not
stop a job that is already running. Cancellation and timeout both DELETE the
transcript, which is also what removes the uploaded audio from the vendor.

Docs: https://www.assemblyai.com/docs/api-reference/files/upload
      https://www.assemblyai.com/docs/api-reference/transcripts/submit
      https://www.assemblyai.com/docs/api-reference/transcripts/get
      https://www.assemblyai.com/docs/api-reference/transcripts/delete
      https://www.assemblyai.com/docs/pre-recorded-audio/label-speakers
"""

from __future__ import annotations

import asyncio

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.capabilities import surface_for
from loreline.logging import get_logger
from loreline.models import Glossary, Interaction, ProviderConfig, ProviderKind
from loreline.secrets import SecretStore
from loreline.stt.backends._assemblyai import glossary_for, parse_words
from loreline.stt.backends._ws import as_obj_dict, get_str
from loreline.stt.base import HttpConnector, Transcription, glossary_terms, secret_for
from loreline.stt.registry import register

log = get_logger(__name__)

# Paths under the REST host declared as this kind's batch surface. The
# streaming surface is a different host entirely, which is why a stored
# base_url meant for it never reaches this connector (see Surface.resolve).
_UPLOAD_PATH = "/v2/upload"
_TRANSCRIPT_PATH = "/v2/transcript"
# Per-request HTTP timeout. Generous for the upload of an utterance-sized WAV;
# the job's own wall clock is _JOB_TIMEOUT_S below, not this.
_TIMEOUT_S = 60.0

# Polling schedule. The first look is soon, because an utterance is at most
# VadChunker.max_utterance_s of audio (30 s by default) and usually finishes in
# about a second, then the interval grows so a queued job costs a handful of
# requests rather than one per second.
_POLL_INITIAL_S = 0.5
_POLL_BACKOFF = 1.6
_POLL_MAX_S = 5.0
# Ceiling on one utterance's job. Re-processing replays stored audio with no
# deadline of its own, which is the intended use of this connector, but "no
# deadline" must not mean "hang forever": a job wedged in `queued` would
# otherwise stall a whole re-processing run silently. Live capture has its own,
# much shorter deadline (STTRouter's timeout_s), so this ceiling is the one that
# only re-processing ever reaches.
_JOB_TIMEOUT_S = 300.0

_PENDING = frozenset({"queued", "processing"})


class AssemblyAIBatchBackend(HttpConnector[list[str]]):
    """Pre-recorded transcription with inline diarization via AssemblyAI.

    The prepared value is the glossary as ``keyterms_prompt`` entries, capped
    for the model.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        language: str | None = None,
        poll_initial_s: float = _POLL_INITIAL_S,
        poll_max_s: float = _POLL_MAX_S,
        job_timeout_s: float = _JOB_TIMEOUT_S,
    ) -> None:
        endpoint = surface_for(config, Interaction.TRANSCRIBE, "batch")
        super().__init__(
            config,
            client=client,
            base_url=endpoint.url,
            headers=endpoint.request_headers(api_key),
            timeout=_TIMEOUT_S,
        )
        self._language = language or config.language
        # No default model: which one to fall back to is a capability-config
        # question, not a connector constant. Omitted, AssemblyAI applies its
        # own default order (universal-3-5-pro, then universal-2 for a language
        # the first does not cover), which is a better thing to inherit than a
        # value pinned here.
        self._model = model
        self._poll_initial_s = poll_initial_s
        self._poll_max_s = poll_max_s
        self._job_timeout_s = job_timeout_s

    def prepare(self, glossary: Glossary | None) -> list[str]:
        return glossary_for(self._model, glossary_terms(glossary), realtime=False)

    async def transcribe_one(self, utterance: Utterance, prepared: list[str]) -> Transcription:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        audio_url = await self._upload(wav)
        transcript_id = await self._create_job(audio_url, prepared)
        try:
            body = await self._await_completion(transcript_id)
        except BaseException:
            # Includes cancellation: the router cancels an utterance that blew
            # its deadline, and abandoning the coroutine would leave a paid job
            # running and the uploaded audio sitting at the vendor.
            await self._delete_job(transcript_id)
            raise
        return Transcription(
            text=get_str(body, "text").strip(),
            words=parse_words(body.get("words"), offset=utterance.start),
        )

    async def _upload(self, wav: bytes) -> str:
        """Hand the audio to AssemblyAI and get back a URL only it can read."""
        response = await self._client.post(
            _UPLOAD_PATH,
            content=wav,
            headers={"Content-Type": "application/octet-stream"},
        )
        self._raise_for_status(response)
        upload_url = get_str(as_obj_dict(response.json()), "upload_url")
        if not upload_url:
            msg = "AssemblyAI upload returned no upload_url"
            raise RuntimeError(msg)
        return upload_url

    def _request_body(self, audio_url: str, terms: list[str]) -> dict[str, object]:
        body: dict[str, object] = {"audio_url": audio_url}
        # `speech_models` (plural, a list) on the async API, against
        # `speech_model` (singular) on the streaming one. A single entry means
        # exactly this model: the default is a fallback ladder, and a GM who
        # picked a model did not ask for a different one silently.
        if self._model:
            body["speech_models"] = [self._model]
        # An empty language_code means auto-detection, which is what a provider
        # configured with no language should get.
        if self._language:
            body["language_code"] = self._language
        # Requested unconditionally, matching every other connector here: the
        # backend always asks for speakers and the router decides whether to use
        # them (see stt/router.py's DiarizationMode.INLINE branch).
        body["speaker_labels"] = True
        if terms:
            body["keyterms_prompt"] = terms
        return body

    async def _create_job(self, audio_url: str, terms: list[str]) -> str:
        body = self._request_body(audio_url, terms)
        response = await self._client.post(_TRANSCRIPT_PATH, json=body)
        self._raise_for_status(response)
        transcript_id = get_str(as_obj_dict(response.json()), "id")
        if not transcript_id:
            msg = "AssemblyAI transcript request returned no id"
            raise RuntimeError(msg)
        return transcript_id

    async def _await_completion(self, transcript_id: str) -> dict[str, object]:
        """Poll until the job leaves ``queued``/``processing``.

        The interval starts short and grows geometrically to a cap: an utterance
        usually completes on the first or second look, while a job stuck behind
        a queue costs a handful of requests a minute instead of sixty. The wait
        is also clipped to whatever is left of the deadline, so the ceiling is
        exact rather than overshot by one interval.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._job_timeout_s
        delay = self._poll_initial_s
        while True:
            response = await self._client.get(f"{_TRANSCRIPT_PATH}/{transcript_id}")
            self._raise_for_status(response)
            body = as_obj_dict(response.json())
            status = get_str(body, "status")
            if status not in _PENDING:
                if status != "completed":
                    # An `error` status is a normal outcome of the API, not an
                    # HTTP failure, and its message is the only thing that says
                    # why (unsupported language, corrupt audio).
                    detail = get_str(body, "error") or status or "unknown status"
                    msg = f"AssemblyAI transcription failed: {detail}"
                    raise RuntimeError(msg)
                return body
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = (
                    f"AssemblyAI transcript {transcript_id} still {status!r} after "
                    f"{self._job_timeout_s:.0f}s"
                )
                raise TimeoutError(msg)
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * _POLL_BACKOFF, self._poll_max_s)

    async def _delete_job(self, transcript_id: str) -> None:
        """Best effort: drop a job we are no longer waiting for.

        DELETE is documented as removing the transcript's data and, with it, the
        file uploaded for it; the docs do not promise it stops work already in
        flight, so this is cleanup rather than a guaranteed cancel. It is
        shielded because the usual reason to be here is that this task is being
        cancelled, and an unshielded request would be cancelled at its first
        await, leaving exactly the job and the stored audio it exists to clean
        up. Nothing it can raise is allowed to replace the error that got us
        here, cancellation included: a cleanup that reported its own failure
        instead of the cancellation would turn a cancelled utterance into a
        mystery. CancelledError is listed on its own because it is a
        BaseException, not an Exception, in current Python.
        """
        try:
            await asyncio.shield(self._client.delete(f"{_TRANSCRIPT_PATH}/{transcript_id}"))
        except (Exception, asyncio.CancelledError) as exc:
            log.warning(
                "stt.assemblyai_batch.cleanup_failed",
                provider_id=self.config.id,
                transcript_id=transcript_id,
                error=str(exc),
            )


@register(ProviderKind.ASSEMBLYAI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> AssemblyAIBatchBackend:
    """AssemblyAI's pre-recorded models, chosen by the registry on the model.

    A config whose model streams (universal-3-5-pro, the universal-streaming
    pair) still goes to the WebSocket connector; only a batch-only model lands
    here.
    """
    return AssemblyAIBatchBackend(config, model=model, api_key=secret_for(config, secrets))
