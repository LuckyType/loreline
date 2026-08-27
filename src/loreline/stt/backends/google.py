"""Google Cloud Speech-to-Text v2 streaming connector (gRPC).

Uses the v2 ``StreamingRecognize`` bidi-streaming RPC with inline recognition
config: explicit LINEAR16 decoding, word time offsets, speaker diarization
(inline ``speaker_label`` per word), and an inline phrase set built from the
campaign glossary.

``google-cloud-speech`` is an optional ``providers`` extra, so it is imported
lazily - this module must import cleanly in the base environment so the registry
can load it. Auth (stored in the secret store under the provider's
``auth_ref``) takes a service-account JSON key file's contents, or is left
blank to fall back to Application Default Credentials. This API does **not**
accept API keys - it requires an OAuth2 principal - so a bare key is rejected
up front rather than failing mid-session; see
``loreline.stt.backends.gemini`` for the API-key-authenticated option. The
provider's ``base_url`` carries the GCP **project id** (or a full
``projects/.../recognizers/_`` resource); ``language`` should be a BCP-47
code such as ``de-DE``.

Docs: https://cloud.google.com/speech-to-text/v2/docs/streaming
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false
# pyright: reportUnknownParameterType=false, reportUnknownLambdaType=false

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from inspect import isawaitable
from typing import Any

from loreline.audio.chunker import Utterance
from loreline.logging import get_logger
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent, Word
from loreline.secrets import SecretStore
from loreline.stt.base import glossary_terms
from loreline.stt.registry import register

log = get_logger(__name__)

_DEFAULT_MODEL = "long"
_DEFAULT_LOCATION = "global"
_DEFAULT_MAX_SPEAKERS = 6

_API_KEY_UNSUPPORTED = (
    "Google Cloud Speech-to-Text v2 does not accept API keys - it requires a "
    "service account. Paste a service-account JSON key here, leave this blank "
    "to use Application Default Credentials, or add a Gemini provider instead, "
    "which does authenticate with a plain API key."
)


def _recognizer_path(target: str | None, *, location: str = _DEFAULT_LOCATION) -> str:
    """Build a recognizer resource from a project id, or pass a full path through."""
    if target and target.startswith("projects/"):
        return target
    project = target or "-"
    return f"projects/{project}/locations/{location}/recognizers/_"


def _is_service_account_json(credential: str) -> bool:
    """True if ``credential`` looks like a service-account key file, not a bare API key."""
    try:
        parsed = json.loads(credential)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("type") == "service_account"


class GoogleSTTBackend:
    """Streaming transcription with inline diarization via Google STT v2 (gRPC)."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        credential: str | None = None,
        client: Any | None = None,
        language: str | None = None,
        diarize: bool = True,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> None:
        self.config = config
        self._credential = credential
        self._client = client
        self._language = language or config.language
        self._model = config.model or _DEFAULT_MODEL
        self._diarize = diarize
        self._min_speakers = min_speakers
        self._max_speakers = max_speakers
        self._recognizer = _recognizer_path(config.base_url)

    def _build_client(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud.speech_v2 import SpeechAsyncClient  # noqa: PLC0415

        if self._credential and _is_service_account_json(self._credential):
            from google.oauth2 import service_account  # noqa: PLC0415

            info = json.loads(self._credential)
            creds = service_account.Credentials.from_service_account_info(info)
            self._client = SpeechAsyncClient(credentials=creds)
        elif self._credential:
            # speech.googleapis.com refuses API keys: it wants an OAuth2
            # principal, so a bare key builds a client fine and then fails at
            # the first utterance with "API keys are not supported by this
            # API ... CREDENTIALS_MISSING". Refuse it here instead, where the
            # provider Test in Settings surfaces it before a session starts.
            raise ValueError(_API_KEY_UNSUPPORTED)
        else:
            self._client = SpeechAsyncClient()
        return self._client

    def _streaming_config(self, glossary: Glossary | None) -> Any:
        from google.cloud.speech_v2.types import cloud_speech as cs  # noqa: PLC0415

        features = cs.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        )
        if self._diarize:
            features.diarization_config = cs.SpeakerDiarizationConfig(
                min_speaker_count=self._min_speakers or 1,
                max_speaker_count=self._max_speakers or _DEFAULT_MAX_SPEAKERS,
            )
        config = cs.RecognitionConfig(
            explicit_decoding_config=cs.ExplicitDecodingConfig(
                encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.config.sample_rate,
                audio_channel_count=1,
            ),
            language_codes=[self._language],
            model=self._model,
            features=features,
        )
        terms = glossary_terms(glossary)
        if terms:
            config.adaptation = cs.SpeechAdaptation(
                phrase_sets=[
                    cs.SpeechAdaptation.AdaptationPhraseSet(
                        inline_phrase_set=cs.PhraseSet(
                            phrases=[cs.PhraseSet.Phrase(value=term) for term in terms]
                        )
                    )
                ]
            )
        return cs.StreamingRecognitionConfig(config=config)

    async def transcribe(
        self,
        audio: AsyncIterator[Utterance],
        *,
        session_id: str,
        glossary: Glossary | None = None,
    ) -> AsyncIterator[TranscriptEvent]:
        client = self._build_client()
        streaming_config = self._streaming_config(glossary)
        async for utterance in audio:
            event = await self._transcribe_one(client, streaming_config, utterance, session_id)
            if event is not None:
                yield event

    async def _transcribe_one(
        self, client: Any, streaming_config: Any, utterance: Utterance, session_id: str
    ) -> TranscriptEvent | None:
        from google.cloud.speech_v2.types import cloud_speech as cs  # noqa: PLC0415

        async def requests() -> AsyncIterator[Any]:
            yield cs.StreamingRecognizeRequest(
                recognizer=self._recognizer, streaming_config=streaming_config
            )
            yield cs.StreamingRecognizeRequest(audio=utterance.pcm)

        stream = client.streaming_recognize(requests=requests())
        if isawaitable(stream):
            stream = await stream

        transcript = ""
        words: list[Word] = []
        async for response in stream:
            for result in response.results:
                if result.is_final and result.alternatives:
                    alternative = result.alternatives[0]
                    if alternative.transcript:
                        transcript = alternative.transcript
                        words = [self._word(w, utterance.start) for w in alternative.words]
        if not transcript:
            return None
        speaker = words[0].speaker if words else None
        return TranscriptEvent(
            session_id=session_id,
            source=self.config.id,
            text=transcript,
            words=words,
            speaker=speaker,
            start_ts=utterance.start,
            end_ts=utterance.end,
            is_final=True,
        )

    def _word(self, word: Any, offset: float) -> Word:
        label = word.speaker_label
        return Word(
            text=word.word,
            start=word.start_offset.total_seconds() + offset,
            end=word.end_offset.total_seconds() + offset,
            confidence=word.confidence or None,
            speaker=f"Speaker {label}" if label else None,
        )

    async def health(self) -> bool:
        try:
            self._build_client()
        except Exception as exc:  # missing SDK / credentials / endpoint
            log.warning(
                "google.health.failed",
                provider=self.config.name,
                provider_id=self.config.id,
                error=str(exc),
            )
            return False
        return True

    async def aclose(self) -> None:
        return None


@register(ProviderKind.GOOGLE)
def _factory(  # pyright: ignore[reportUnusedFunction]
    config: ProviderConfig, secrets: SecretStore
) -> GoogleSTTBackend:
    credential = secrets.get(config.auth_ref) if config.auth_ref else None
    return GoogleSTTBackend(config, credential=credential)
