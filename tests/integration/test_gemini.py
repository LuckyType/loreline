"""Integration test for the Gemini transcription connector.

Drives the backend against an ``httpx.MockTransport`` standing in for
``generativelanguage.googleapis.com``, asserting both halves of the contract:
the request body we build matches the documented ``transcription_config``
shape, and the ``word_info`` annotations come back mapped onto session time.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, Protocol, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.gemini import GeminiSTTBackend

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="gemini-1",
        name="Gemini",
        kind=ProviderKind.GEMINI,
        protocol=Protocol.HTTP_BATCH,
    )


def _reply(words: list[dict[str, Any]], text: str = "Hallo Welt") -> dict[str, Any]:
    return {
        "id": "interactions/abc",
        "status": "completed",
        "steps": [
            {
                "id": "step_001",
                "type": "model_output",
                "content": [{"type": "text", "text": text, "annotations": words}],
            }
        ],
    }


def _word(text: str, start: str, end: str, speaker: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "type": "word_info",
        "text": text,
        "start_offset": start,
        "end_offset": end,
    }
    if speaker:
        info["speaker"] = speaker
    return info


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        headers={"x-goog-api-key": "test-key"},
    )


async def _utterances(items: list[Utterance]) -> AsyncIterator[Utterance]:
    for item in items:
        yield item


async def test_transcribe_maps_words_onto_session_time() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json  # noqa: PLC0415

        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_reply([_word("Hallo", "0.100s", "0.450s", "spk_1")]),
        )

    async with _client(handler) as client:
        backend = GeminiSTTBackend(_config(), client=client, language="de-DE")
        events: list[TranscriptEvent] = [
            e
            async for e in backend.transcribe(
                _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=12.0, end=12.5)]),
                session_id="s1",
            )
        ]

    assert len(events) == 1
    event = events[0]
    assert event.source == "gemini-1"
    assert event.is_final
    assert event.text == "Hallo Welt"
    # Offsets are utterance-relative in the payload; transcript timings are not.
    assert abs(event.words[0].start - 12.1) < 1e-6  # 0.100s + utterance offset 12.0
    assert abs(event.words[0].end - 12.45) < 1e-6
    assert event.words[0].speaker == "Speaker spk_1"
    assert event.speaker == "Speaker spk_1"

    body = captured[0]
    assert body["model"] == "gemini-3.5-transcribe"
    audio = body["input"][0]
    assert audio["mime_type"] == "audio/wav"
    # Inline audio is base64 of a real WAV container, not raw PCM.
    assert base64.b64decode(audio["data"]).startswith(b"RIFF")
    config = body["generation_config"]["transcription_config"]
    assert config["language_codes"] == ["de-DE"]
    assert config["mode"]["diarization_mode"] == "speaker"
    assert config["mode"]["timestamp_granularities"] == ["word"]


async def test_glossary_becomes_custom_vocabulary() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json  # noqa: PLC0415

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_reply([]))

    async with _client(handler) as client:
        backend = GeminiSTTBackend(_config(), client=client)
        glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
        _ = [
            e
            async for e in backend.transcribe(
                _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]),
                session_id="s1",
                glossary=glossary,
            )
        ]

    config = captured[0]["generation_config"]["transcription_config"]
    assert config["custom_vocabulary"] == ["Drakonia", "Thalric"]


async def test_blank_language_omits_codes_for_auto_detection() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json  # noqa: PLC0415

        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_reply([]))

    config = _config()
    config.language = ""
    async with _client(handler) as client:
        backend = GeminiSTTBackend(config, client=client)
        _ = [
            e
            async for e in backend.transcribe(
                _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]),
                session_id="s1",
            )
        ]

    assert "language_codes" not in captured[0]["generation_config"]["transcription_config"]


async def test_empty_transcript_yields_no_event() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply([], text=""))

    async with _client(handler) as client:
        backend = GeminiSTTBackend(_config(), client=client)
        events = [
            e
            async for e in backend.transcribe(
                _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]),
                session_id="s1",
            )
        ]

    assert events == []


async def test_incomplete_status_yields_no_event() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "i", "status": "failed", "steps": []})

    async with _client(handler) as client:
        backend = GeminiSTTBackend(_config(), client=client)
        events = [
            e
            async for e in backend.transcribe(
                _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]),
                session_id="s1",
            )
        ]

    assert events == []


async def test_error_body_is_kept_in_the_exception() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})

    async with _client(handler) as client:
        backend = GeminiSTTBackend(_config(), client=client)
        with pytest.raises(httpx.HTTPStatusError, match="API key not valid"):
            _ = [
                e
                async for e in backend.transcribe(
                    _utterances([Utterance(pcm=b"\x01\x00" * 1600, start=0.0, end=0.1)]),
                    session_id="s1",
                )
            ]


async def test_health_checks_the_credential() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"models": []})

    async with _client(ok) as client:
        assert await GeminiSTTBackend(_config(), client=client).health() is True

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    async with _client(unauthorized) as client:
        assert await GeminiSTTBackend(_config(), client=client).health() is False
