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
from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx

from loreline.audio.chunker import Utterance
from loreline.audio.wav import pcm_to_wav
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.backends._assemblyai import auth_headers, glossary_for, parse_words
from loreline.stt.backends._ws import as_obj_dict, get_str
from loreline.stt.base import error_detail, glossary_terms, http_base_url
from loreline.stt.registry import register

log = get_logger(__name__)

# REST host. The streaming connector's default is a different host entirely
# (wss://streaming.assemblyai.com), which is why a stored base_url meant for it
# is dropped rather than reused. EU accounts use https://api.eu.assemblyai.com,
# which an operator sets as the provider's base_url.
_DEFAULT_BASE_URL = "https://api.assemblyai.com"
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


class AssemblyAIBatchBackend:
    """Pre-recorded transcription with inline diarization via AssemblyAI."""

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
        self.config = config
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
        base_url = http_base_url(config.base_url) or _DEFAULT_BASE_URL
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=auth_headers(api_key), timeout=_TIMEOUT_S
        )

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        terms = glossary_for(self._model, glossary_terms(glossary), realtime=False)
        async for utterance in audio:
            event = await self._transcribe_one(utterance, session_id=session_id, terms=terms)
            if event is not None:
                yield event

    async def _transcribe_one(
        self, utterance: Utterance, *, session_id: str, terms: list[str]
    ) -> TranscriptEvent | None:
        wav = pcm_to_wav(utterance.pcm, sample_rate=self.config.sample_rate)
        audio_url = await self._upload(wav)
        transcript_id = await self._create_job(audio_url, terms)
        try:
            body = await self._await_completion(transcript_id)
        except BaseException:
            # Includes cancellation: the router cancels an utterance that blew
            # its deadline, and abandoning the coroutine would leave a paid job
            # running and the uploaded audio sitting at the vendor.
            await self._delete_job(transcript_id)
            raise
        text = get_str(body, "text").strip()
        if not text:
            return None
        words = parse_words(body.get("words"), offset=utterance.start)
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=text,
            words=words,
            speaker=_speaker(words),
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
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

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < HTTPStatus.BAD_REQUEST:
            return
        # Keep the body: AssemblyAI puts the actionable reason there ("Not
        # authorized", "invalid speech_models"), and the upload endpoint answers
        # 422 as plain text, which error_detail also handles.
        raise httpx.HTTPStatusError(
            f"{response.status_code} from {response.request.url}: {error_detail(response)}",
            request=response.request,
            response=response,
        )

    async def health(self) -> bool:
        """List one transcript, which exercises the credential.

        There is no model-list endpoint to ask instead: AssemblyAI's only
        /models route serves its LLM gateway.
        https://www.assemblyai.com/docs/api-reference/transcripts/list
        """
        try:
            response = await self._client.get(_TRANSCRIPT_PATH, params={"limit": 1})
        except httpx.HTTPError:
            return False
        return response.status_code < HTTPStatus.BAD_REQUEST

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _speaker(words: list[Word]) -> str | None:
    """The utterance's dominant speaker, from its first labelled word - the
    convention every other connector here uses."""
    return next((w.speaker for w in words if w.speaker), None)


@register(ProviderKind.ASSEMBLYAI)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore, model: str | None
) -> AssemblyAIBatchBackend:
    """AssemblyAI's pre-recorded models, chosen by the registry on the model.

    A config whose model streams (universal-3-5-pro, the universal-streaming
    pair) still goes to the WebSocket connector; only a batch-only model lands
    here.
    """
    api_key = secrets.get(config.auth_ref) if config.auth_ref else None
    return AssemblyAIBatchBackend(config, model=model, api_key=api_key)
