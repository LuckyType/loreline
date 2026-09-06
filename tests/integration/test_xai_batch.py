"""Integration test for the xAI batch (multipart) connector.

Drives the backend two ways: against an ``httpx.MockTransport`` standing in for
api.x.ai, which is where the request we believe the documented API wants is
pinned field by field, and against ``mocks/xai_stt.py``, which enforces the two
rules that make this endpoint unlike the OpenAI-shaped ones beside it (``file``
last, no ``model`` field) and would reject a connector that broke either.

There is no xAI key in this environment, so these assertions are the whole
verification this connector has: they pin the request and the response we
believe in, so the gap between belief and reality is one run once a key exists.
See the note above the grok-stt-1.0 entry in capabilities.yaml.

Wire format per
https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.xai_batch import XaiBatchBackend
from loreline.stt.base import transcribe_capabilities
from mocks.xai_stt import create_app

BASE_URL = "https://api.x.ai/v1"
MODEL = "grok-stt-1.0"

_NAME = re.compile(rb'name="([^"]+)"')


def _parts(request: httpx.Request) -> list[tuple[str, bytes]]:
    """Every multipart field as (name, value), in the order it was written.

    Read off the encoded body rather than a parsed form, because the two things
    this endpoint is strict about - which field comes last, and how a repeated
    field is spelled - are exactly what parsing throws away.
    """
    boundary = request.headers["content-type"].partition("boundary=")[2].encode()
    parts: list[tuple[str, bytes]] = []
    for chunk in request.content.split(b"--" + boundary):
        head, separator, body = chunk.partition(b"\r\n\r\n")
        name = _NAME.search(head)
        if not separator or name is None:
            continue
        parts.append((name.group(1).decode(), body.removesuffix(b"\r\n")))
    return parts


def _names(request: httpx.Request) -> list[str]:
    return [name for name, _ in _parts(request)]


def _values(request: httpx.Request, field: str) -> list[str]:
    return [value.decode("utf-8", "replace") for name, value in _parts(request) if name == field]


def _config() -> ProviderConfig:
    return ProviderConfig(id="xai-1", name="xAI", kind=ProviderKind.XAI, language="de")


def _reply(words: list[dict[str, Any]], text: str = "Hallo Welt") -> dict[str, Any]:
    return {"text": text, "language": "de", "duration": 1.5, "words": words}


def _word(word: str, start: float, end: float, speaker: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"text": word, "start": start, "end": end}
    if speaker is not None:
        row["speaker"] = speaker
    return row


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        headers={"Authorization": "Bearer test-key"},
    )


def _one(start: float) -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 1600, start=start, end=start + 0.1)


async def _run(
    handler: Any,
    *,
    model: str | None = MODEL,
    glossary: Glossary | None = None,
    start: float = 0.0,
) -> TranscriptEvent | None:
    """One utterance through the connector, built the way the registry builds it.

    The capabilities come from the same accessor ``create_backend`` uses, so the
    ceilings these tests pin are still the yaml's answer and not ones restated
    here.
    """
    async with _client(handler) as client:
        backend = XaiBatchBackend(
            _config(),
            model=model,
            caps=transcribe_capabilities(ProviderKind.XAI, model),
            client=client,
        )
        return await backend.transcribe(_one(start), session_id="s1", glossary=glossary)


async def test_posts_a_wav_body_and_maps_words_onto_session_time() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([_word("Hallo", 0.1, 0.45, speaker=0)]))

    event = await _run(handler, start=12.0)

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/stt"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"RIFF" in request.content
    assert _values(request, "language") == ["de"]
    assert _values(request, "diarize") == ["true"]

    assert event is not None
    assert event.source == "xai-1"
    assert event.is_final
    assert event.text == "Hallo Welt"
    # Word times are relative to the audio posted; the session clock is not.
    assert abs(event.words[0].start - 12.1) < 1e-6
    assert abs(event.words[0].end - 12.45) < 1e-6
    assert event.words[0].speaker == "Speaker 0"


async def test_never_sends_a_model_field() -> None:
    """The one thing this endpoint does not have. Neither POST /v1/stt nor the
    socket documents a `model` parameter, so a chosen model reaches the
    connector (the registry resolved capabilities with it) and stops there."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    await _run(handler)

    assert "model" not in _names(seen[0])


async def test_the_audio_is_the_last_multipart_field() -> None:
    """Documented requirement: "Must be last field". httpx writes every `data`
    field before any `files` one, which is why the parameters go in one and the
    audio in the other - this asserts that arrangement survives."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    await _run(handler, glossary=Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"]))

    names = _names(seen[0])
    assert names[-1] == "file"
    assert names.count("file") == 1


async def test_glossary_terms_repeat_the_keyterm_field() -> None:
    """ "Repeat the field for multiple terms" - not a comma-joined list, which
    would bias recognition towards one long phrase nobody says."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
    await _run(handler, glossary=glossary)

    assert _values(seen[0], "keyterm") == ["Drakonia", "Thalric"]


async def test_the_glossary_is_trimmed_to_a_hundred_terms() -> None:
    """The documented ceiling, read off capabilities.yaml rather than restated
    here. Glossary order is priority order, so the head survives."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    glossary = Glossary(campaign_id="c1", terms=[f"term{i}" for i in range(150)])
    await _run(handler, glossary=glossary)

    sent = _values(seen[0], "keyterm")
    assert len(sent) == 100
    assert sent[0] == "term0"


async def test_an_overlong_term_is_dropped_rather_than_cut() -> None:
    """50 characters per key term is the documented limit. Half a proper noun
    would bias towards a word nobody said, so the term goes rather than its
    tail - and the terms around it still travel."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    glossary = Glossary(campaign_id="c1", terms=["Drakonia", "N" * 51, "Thalric"])
    await _run(handler, glossary=glossary)

    assert _values(seen[0], "keyterm") == ["Drakonia", "Thalric"]


async def test_an_unattributed_word_carries_no_speaker() -> None:
    """A response to a request that asked for diarization can still leave a
    word unattributed, and "no speaker" must not read as speaker zero."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_reply([_word("Hallo", 0.0, 0.4), _word("Welt", 0.4, 0.8, speaker=1)])
        )

    event = await _run(handler)

    assert event is not None
    assert event.words[0].speaker is None
    assert event.words[1].speaker == "Speaker 1"
    # The event's speaker is the first LABELLED word's, not the first word's.
    assert event.speaker == "Speaker 1"


async def test_empty_transcript_yields_no_event() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply([], text=""))

    assert await _run(handler) is None


async def test_a_failed_request_carries_the_vendors_own_message() -> None:
    """ "400 Bad Request" never says which parameter was wrong."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "vad_threshold out of range"}})

    with pytest.raises(httpx.HTTPStatusError, match="vad_threshold out of range"):
        await _run(handler)


async def test_a_streaming_base_url_is_not_handed_to_the_http_client() -> None:
    """base_url on this kind may name either transport; a wss:// one belongs to
    the socket connector and would fail every request here."""
    config = _config()
    config.base_url = "wss://api.x.ai/v1/stt"
    backend = XaiBatchBackend(config, api_key="k")
    try:
        # httpx normalises a base URL with a path to a trailing slash.
        assert str(backend._client.base_url) == f"{BASE_URL}/"  # pyright: ignore[reportPrivateUsage]
    finally:
        await backend.aclose()


async def test_against_the_mock_server_which_enforces_the_endpoints_rules() -> None:
    """The same connector against ``mocks/xai_stt.py``, which rejects a request
    carrying a `model` field or ending in anything but the audio, and echoes
    the language and key terms back so the whole round trip is asserted."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock/v1") as client:
        config = _config()
        config.base_url = "http://mock/v1"
        backend = XaiBatchBackend(
            config,
            model=MODEL,
            caps=transcribe_capabilities(ProviderKind.XAI, MODEL),
            client=client,
        )
        event = await backend.transcribe(
            _one(0.0), session_id="s1", glossary=Glossary(campaign_id="c1", terms=["Drakonia"])
        )

    assert event is not None
    assert "mock transcription" in event.text
    assert "keyterm: Drakonia" in event.text
    assert "[de]" in event.text
    # diarize=true went out, so the mock attributed every word.
    assert event.speaker is not None
