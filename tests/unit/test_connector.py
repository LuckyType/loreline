"""The Connector spine: the loop, the event, the speaker rule, the HTTP base.

Driven through small fake subclasses rather than any real connector, so what
is asserted here is exactly what every connector inherits and nothing that a
vendor's payload shape could hide. The per-connector suites under
tests/integration keep the vendor parsing and the wire format.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from loreline.audio.chunker import Utterance
from loreline.models import (
    Glossary,
    ProviderConfig,
    ProviderKind,
    TranscriptEvent,
    Word,
)
from loreline.secrets import SecretStore
from loreline.stt.base import (
    Connector,
    HttpConnector,
    STTBackend,
    Transcription,
    first_labelled_speaker,
    glossary_terms,
    secret_for,
)


def _config(auth_ref: str | None = None) -> ProviderConfig:
    return ProviderConfig(
        id="fake-1",
        name="Fake",
        kind=ProviderKind.OPENAI_COMPAT,
        auth_ref=auth_ref,
    )


def _word(text: str, speaker: str | None) -> Word:
    return Word(text=text, start=0.0, end=0.1, speaker=speaker)


class _FakeConnector(Connector[str]):
    """Answers each utterance with the next scripted result."""

    def __init__(self, config: ProviderConfig, results: list[Transcription | None]) -> None:
        super().__init__(config)
        self._results = list(results)
        self.prepared_calls = 0
        self.seen: list[tuple[Utterance, str]] = []

    def prepare(self, glossary: Glossary | None) -> str:
        self.prepared_calls += 1
        return ",".join(glossary_terms(glossary))

    async def transcribe_one(self, utterance: Utterance, prepared: str) -> Transcription | None:
        self.seen.append((utterance, prepared))
        return self._results.pop(0)


async def _run(
    backend: _FakeConnector,
    utterances: list[Utterance],
    glossary: Glossary | None = None,
) -> list[TranscriptEvent]:
    """Every event the connector produced for these utterances, one call each.

    One call per utterance is the contract, so a test that wants two of them
    makes two calls, exactly as the router does.
    """
    events: list[TranscriptEvent] = []
    for utterance in utterances:
        event = await backend.transcribe(utterance, session_id="s1", glossary=glossary)
        if event is not None:
            events.append(event)
    return events


# --- the loop and the event ---------------------------------------------


async def test_prepares_the_setup_this_call_hands_to_transcribe_one() -> None:
    """``prepare`` runs once per call, and its value is what the hook reads.

    One call per utterance, so one prepare per utterance: the base no longer
    loops, and a connector with something worth keeping between utterances
    keeps it on the instance instead.
    """
    first = Utterance(pcm=b"\x01\x00", start=1.0, end=1.5)
    second = Utterance(pcm=b"\x02\x00", start=2.0, end=2.5)
    backend = _FakeConnector(_config(), [Transcription("one"), Transcription("two")])

    events = await _run(backend, [first, second], Glossary(campaign_id="c1", terms=["a", "b"]))

    assert backend.prepared_calls == 2
    assert backend.seen == [(first, "a,b"), (second, "a,b")]
    assert [e.text for e in events] == ["one", "two"]


async def test_event_fields_come_from_the_config_the_utterance_and_the_result() -> None:
    words = [_word("hello", "Speaker 0"), _word("there", "Speaker 1")]
    backend = _FakeConnector(_config(), [Transcription("hello there", words)])

    events = await _run(backend, [Utterance(pcm=b"\x01\x00", start=10.0, end=10.5)])

    assert len(events) == 1
    event = events[0]
    assert event.session_id == "s1"
    assert event.source == "fake-1"
    assert event.text == "hello there"
    assert event.words == words
    assert event.speaker == "Speaker 0"
    assert event.start_ts == 10.0
    assert event.end_ts == 10.5
    assert event.is_final is True


async def test_none_and_empty_text_yield_no_event_at_all() -> None:
    results: list[Transcription | None] = [
        None,
        Transcription(""),
        Transcription("", [_word("x", "Speaker 0")]),
        Transcription("kept"),
    ]
    backend = _FakeConnector(_config(), results)
    utterances = [Utterance(pcm=b"\x01\x00", start=float(i), end=i + 0.5) for i in range(4)]

    events = await _run(backend, utterances)

    assert [e.text for e in events] == ["kept"]
    assert len(backend.seen) == 4  # a skipped utterance costs only its own event


async def test_a_connector_satisfies_the_backend_protocol_structurally() -> None:
    backend = _FakeConnector(_config(), [])
    assert isinstance(backend, STTBackend)
    await backend.aclose()  # the default is a no-op, not an abstract method


# --- the speaker rule ------------------------------------------------------


def test_speaker_is_the_first_labelled_word() -> None:
    words = [_word("um", None), _word("hello", "Speaker B"), _word("there", "Speaker A")]
    assert first_labelled_speaker(words) == "Speaker B"


def test_speaker_is_none_without_words_or_labels() -> None:
    assert first_labelled_speaker([]) is None
    assert first_labelled_speaker([_word("a", None), _word("b", "")]) is None


async def test_an_unlabelled_lead_in_does_not_hide_the_speaker_from_the_event() -> None:
    """The delta from the old first-word rule: a lead-in word the vendor left
    unattributed no longer turns a labelled utterance into a speakerless one."""
    words = [_word("um", None), _word("hello", "Speaker 1")]
    backend = _FakeConnector(_config(), [Transcription("um hello", words)])

    events = await _run(backend, [Utterance(pcm=b"\x01\x00", start=0.0, end=0.5)])

    assert events[0].speaker == "Speaker 1"


# --- HttpConnector: the error body and the client's lifetime ---------------


class _FakeHttpConnector(HttpConnector[None]):
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client=client, base_url="https://vendor.test/v1", timeout=5.0)

    def prepare(self, glossary: Glossary | None) -> None:
        return None

    async def transcribe_one(self, utterance: Utterance, prepared: None) -> Transcription | None:
        response = await self._client.post("/transcribe", content=utterance.pcm)
        self._raise_for_status(response)
        return Transcription(str(response.json()["text"]))


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://vendor.test/v1"
    )


async def test_a_failed_request_raises_with_the_vendor_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "keyterm is not supported"}})

    async with _mock_client(handler) as client:
        backend = _FakeHttpConnector(_config(), client=client)
        with pytest.raises(httpx.HTTPStatusError, match=r"400 from .*keyterm is not supported"):
            await _run_http(backend)


async def test_a_successful_request_passes_through() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "fine"})

    async with _mock_client(handler) as client:
        backend = _FakeHttpConnector(_config(), client=client)
        event = await _run_http(backend)
        assert event is not None and event.text == "fine"


async def _run_http(backend: _FakeHttpConnector) -> TranscriptEvent | None:
    utterance = Utterance(pcm=b"\x01\x00", start=0.0, end=0.5)
    return await backend.transcribe(utterance, session_id="s1")


async def test_an_injected_client_is_not_closed_by_the_connector() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "fine"})

    client = _mock_client(handler)
    backend = _FakeHttpConnector(_config(), client=client)
    assert backend._client is client  # pyright: ignore[reportPrivateUsage]

    await backend.aclose()

    assert not client.is_closed
    await client.aclose()


async def test_an_owned_client_is_closed_by_the_connector() -> None:
    backend = _FakeHttpConnector(_config())
    client = backend._client  # pyright: ignore[reportPrivateUsage]
    assert str(client.base_url).rstrip("/") == "https://vendor.test/v1"  # httpx adds the slash
    assert not client.is_closed

    await backend.aclose()

    assert client.is_closed


# --- secret_for --------------------------------------------------------------


def test_secret_for_reads_the_auth_ref_or_nothing(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets.json")
    secrets.set("vendor_key", "s3cret")

    assert secret_for(_config(auth_ref="vendor_key"), secrets) == "s3cret"
    assert secret_for(_config(auth_ref=None), secrets) is None
