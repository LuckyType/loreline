"""Integration test for the Google STT v2 connector via an injected fake client.

google-cloud-speech ships no server-side gRPC stubs, so instead of a network
mock we inject a fake async client (the DI pattern the rest of the codebase
uses): it captures the request stream the connector builds and yields canned
``StreamingRecognizeResponse`` objects built from the real proto types. Requires
the ``providers`` extra; skipped otherwise.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind
from loreline.stt.backends.google import (
    GoogleSTTBackend,
    _is_service_account_json,  # pyright: ignore[reportPrivateUsage]
)

cs = pytest.importorskip("google.cloud.speech_v2.types.cloud_speech")


class FakeGoogleClient:
    """Captures the streamed requests and replays canned responses."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.seen_requests: list[Any] = []

    async def streaming_recognize(self, *, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        async for request in requests:
            self.seen_requests.append(request)

        async def _gen() -> AsyncIterator[Any]:
            for response in self._responses:
                yield response

        return _gen()


def _word(text: str, start: float, end: float, speaker: str) -> Any:
    return cs.WordInfo(
        word=text,
        speaker_label=speaker,
        start_offset=timedelta(seconds=start),
        end_offset=timedelta(seconds=end),
    )


async def _one_utterance() -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x00\x00" * 1600, start=5.0, end=5.5)


async def test_google_streaming_with_diarization_and_glossary() -> None:
    response = cs.StreamingRecognizeResponse(
        results=[
            cs.StreamingRecognitionResult(
                is_final=True,
                alternatives=[
                    cs.SpeechRecognitionAlternative(
                        transcript="Hallo Welt",
                        words=[
                            _word("Hallo", 0.0, 0.4, "1"),
                            _word("Welt", 0.4, 0.8, "2"),
                        ],
                    )
                ],
            )
        ]
    )
    client = FakeGoogleClient([response])
    config = ProviderConfig(
        id="g1",
        name="Google",
        kind=ProviderKind.GOOGLE,
        protocol=Protocol.GRPC,
        base_url="my-project",
        language="de-DE",
    )
    backend = GoogleSTTBackend(config, client=client)
    glossary = Glossary(campaign_id="c1", terms=["Drakonia"])

    events = [
        e async for e in backend.transcribe(_one_utterance(), session_id="s1", glossary=glossary)
    ]

    assert len(events) == 1
    event = events[0]
    assert event.text == "Hallo Welt"
    assert event.source == "g1"
    assert event.is_final
    # inline diarization + utterance offset applied to word timings
    assert {w.speaker for w in event.words} == {"Speaker 1", "Speaker 2"}
    assert abs(event.words[0].start - 5.0) < 1e-6  # 0.0 + utterance offset 5.0
    assert abs(event.words[1].end - 5.8) < 1e-6  # 0.8 + 5.0

    # The first request carried the recognizer + diarization + glossary phrase set.
    config_request = client.seen_requests[0]
    assert config_request.recognizer == "projects/my-project/locations/global/recognizers/_"
    stream_config = config_request.streaming_config
    assert stream_config.config.language_codes == ["de-DE"]
    assert stream_config.config.features.diarization_config.max_speaker_count >= 1
    phrases = stream_config.config.adaptation.phrase_sets[0].inline_phrase_set.phrases
    assert [p.value for p in phrases] == ["Drakonia"]
    # The second request carried the audio bytes.
    assert client.seen_requests[1].audio


async def test_google_recognizer_passthrough_and_no_final() -> None:
    # A full recognizer resource is passed through unchanged; no final result -> no event.
    client = FakeGoogleClient([cs.StreamingRecognizeResponse(results=[])])
    config = ProviderConfig(
        id="g2",
        name="Google",
        kind=ProviderKind.GOOGLE,
        protocol=Protocol.GRPC,
        base_url="projects/p/locations/us/recognizers/r",
        language="de-DE",
    )
    backend = GoogleSTTBackend(config, client=client)
    events = [e async for e in backend.transcribe(_one_utterance(), session_id="s1")]
    assert events == []
    assert client.seen_requests[0].recognizer == "projects/p/locations/us/recognizers/r"


@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        (json.dumps({"type": "service_account", "project_id": "p"}), True),
        (json.dumps({"type": "authorized_user"}), False),  # a gcloud user cred, not a SA key
        (json.dumps(["not", "an", "object"]), False),
        ("AIzaSyDaGmWKa4JsXZ-HjGw7ISLan_g9Y5mJEeE", False),  # a bare API key
        ("not json at all", False),
        ("", False),
    ],
)
def test_is_service_account_json(credential: str, expected: bool) -> None:
    assert _is_service_account_json(credential) is expected


def _config(**overrides: Any) -> ProviderConfig:
    defaults: dict[str, Any] = {
        "id": "g",
        "name": "Google",
        "kind": ProviderKind.GOOGLE,
        "protocol": Protocol.GRPC,
        "base_url": "my-project",
        "language": "de-DE",
    }
    return ProviderConfig(**{**defaults, **overrides})


def test_build_client_routes_service_account_json_through_oauth() -> None:
    sa_json = json.dumps({"type": "service_account", "project_id": "p"})
    backend = GoogleSTTBackend(_config(), credential=sa_json)
    with (
        patch("google.oauth2.service_account.Credentials.from_service_account_info") as from_info,
        patch("google.cloud.speech_v2.SpeechAsyncClient") as speech_client,
    ):
        backend._build_client()  # pyright: ignore[reportPrivateUsage]
        from_info.assert_called_once_with(json.loads(sa_json))
        speech_client.assert_called_once_with(credentials=from_info.return_value)


def test_build_client_routes_bare_key_through_api_key_credentials() -> None:
    backend = GoogleSTTBackend(_config(), credential="AIzaSyDaGmWKa4JsXZ-HjGw7ISLan_g9Y5mJEeE")
    with patch("google.cloud.speech_v2.SpeechAsyncClient") as speech_client:
        backend._build_client()  # pyright: ignore[reportPrivateUsage]
        (_, kwargs) = speech_client.call_args
        assert kwargs["credentials"].token == "AIzaSyDaGmWKa4JsXZ-HjGw7ISLan_g9Y5mJEeE"


def test_build_client_falls_back_to_adc_when_blank() -> None:
    backend = GoogleSTTBackend(_config(), credential=None)
    with patch("google.cloud.speech_v2.SpeechAsyncClient") as speech_client:
        backend._build_client()  # pyright: ignore[reportPrivateUsage]
        speech_client.assert_called_once_with()
