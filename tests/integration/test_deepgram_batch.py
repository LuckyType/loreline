"""Integration test for the Deepgram batch (pre-recorded) connector.

Drives the backend against an ``httpx.MockTransport`` standing in for
``api.deepgram.com``. There is no Deepgram key in this environment, so these
assertions are the whole verification the connector has: they pin the request
we believe the documented API wants (raw WAV body, ``audio/wav``, the query
parameters, the per-model biasing field) and the response we believe it
returns, so the gap between belief and reality is one test run once a key
exists. See the hidden ``whisper-large`` entry in capabilities.yaml.

Wire format per
https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded.md
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from loreline.audio.chunker import Utterance
from loreline.models import Glossary, ProviderConfig, ProviderKind, TranscriptEvent
from loreline.stt.backends.deepgram_batch import DeepgramBatchBackend

BASE_URL = "https://api.deepgram.com"


def _config() -> ProviderConfig:
    return ProviderConfig(
        id="dg-1",
        name="Deepgram",
        kind=ProviderKind.DEEPGRAM,
        language="de",
    )


def _reply(words: list[dict[str, Any]], text: str = "Hallo Welt") -> dict[str, Any]:
    return {
        "metadata": {"duration": 1.5},
        "results": {"channels": [{"alternatives": [{"transcript": text, "words": words}]}]},
    }


def _word(word: str, start: float, end: float, speaker: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "word": word.lower(),
        "punctuated_word": word,
        "start": start,
        "end": end,
        "confidence": 0.98,
    }
    if speaker is not None:
        row["speaker"] = speaker
    return row


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=BASE_URL,
        headers={"Authorization": "Token test-key"},
    )


async def _one(start: float) -> AsyncIterator[Utterance]:
    yield Utterance(pcm=b"\x01\x00" * 1600, start=start, end=start + 0.1)


async def _run(
    handler: Any,
    *,
    model: str | None = "whisper-large",
    glossary: Glossary | None = None,
    start: float = 0.0,
) -> list[TranscriptEvent]:
    async with _client(handler) as client:
        backend = DeepgramBatchBackend(_config(), model=model, client=client)
        return [
            event
            async for event in backend.transcribe(_one(start), session_id="s1", glossary=glossary)
        ]


async def test_posts_a_wav_body_and_maps_words_onto_session_time() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([_word("Hallo", 0.1, 0.45, speaker=0)]))

    events = await _run(handler, start=12.0)

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/listen"
    # Raw bytes with the container's media type, not multipart and not the
    # {"url": ...} form, which is for audio Deepgram fetches itself.
    assert request.headers["content-type"] == "audio/wav"
    assert request.content.startswith(b"RIFF")
    params = request.url.params
    assert params["model"] == "whisper-large"
    assert params["language"] == "de"
    assert params["diarize"] == "true"
    assert params["punctuate"] == "true"
    # Streaming-only parameters: a WAV header already carries these.
    assert "encoding" not in params
    assert "sample_rate" not in params

    assert len(events) == 1
    event = events[0]
    assert event.source == "dg-1"
    assert event.is_final
    assert event.text == "Hallo Welt"
    # Word times are relative to the audio posted; transcript times are not.
    assert abs(event.words[0].start - 12.1) < 1e-6
    assert abs(event.words[0].end - 12.45) < 1e-6
    assert event.words[0].text == "Hallo"  # punctuated_word wins over word
    assert event.words[0].speaker == "Speaker 0"


async def test_nova_3_sends_keyterm_and_nova_2_sends_legacy_keywords() -> None:
    """The per-model trap capabilities.yaml records: nova-3 takes `keyterm`,
    nova-2 does not and takes `keywords` instead. Sending the wrong one is a
    400, so the connector reads the field from the capability config rather
    than pinning one for the whole vendor."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    glossary = Glossary(campaign_id="c1", terms=["Drakonia", "Thalric"])
    await _run(handler, model="nova-3", glossary=glossary)
    await _run(handler, model="nova-2", glossary=glossary)

    assert seen[0].url.params.get_list("keyterm") == ["Drakonia", "Thalric"]
    assert "keywords" not in seen[0].url.params
    assert seen[1].url.params.get_list("keywords") == ["Drakonia", "Thalric"]
    assert "keyterm" not in seen[1].url.params


async def test_nova_2_truncates_the_glossary_to_its_documented_ceiling() -> None:
    """Legacy keywords are capped at 100 per request; over the cap Deepgram
    rejects rather than ignores, and a rejected request costs the utterance."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    glossary = Glossary(campaign_id="c1", terms=[f"term{i}" for i in range(150)])
    await _run(handler, model="nova-2", glossary=glossary)

    assert len(seen[0].url.params.get_list("keywords")) == 100


async def test_whisper_sends_no_biasing_field_at_all() -> None:
    """Whisper Cloud's feature table lists Keywords as unsupported, and keyterm
    prompting is Nova-3/Flux only, so there is nothing to send."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    await _run(handler, glossary=Glossary(campaign_id="c1", terms=["Drakonia"]))

    assert "keyterm" not in seen[0].url.params
    assert "keywords" not in seen[0].url.params


async def test_unset_model_omits_the_parameter() -> None:
    """No default model is pinned here: omitted, Deepgram applies its own."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply([]))

    await _run(handler, model=None)

    assert "model" not in seen[0].url.params


async def test_empty_transcript_yields_no_event() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_reply([], text=""))

    assert await _run(handler) == []


async def test_missing_alternatives_yield_no_event() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"channels": []}})

    assert await _run(handler) == []


async def test_a_streaming_base_url_is_not_handed_to_the_http_client() -> None:
    """For this kind base_url has meant the WebSocket endpoint since before a
    batch connector existed; a wss:// URL would fail every request."""
    config = _config()
    config.base_url = "wss://api.deepgram.com/v1/listen"
    backend = DeepgramBatchBackend(config, api_key="k")
    try:
        assert str(backend._client.base_url) == BASE_URL  # pyright: ignore[reportPrivateUsage]
    finally:
        await backend.aclose()
