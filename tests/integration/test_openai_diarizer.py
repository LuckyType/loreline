"""Tests for the OpenAI batch diarizer.

Which model it runs is not written here, or in the connector: it is the OpenAI
transcription model capabilities.yaml records as returning speaker labels.
"""

from __future__ import annotations

import httpx
import pytest

from loreline.capabilities import config, default_diarizing_model
from loreline.diarization import openai_diarizer as diarizer_module
from loreline.diarization.openai_diarizer import OpenAIDiarizer
from loreline.diarization.provider import create_diarizer
from loreline.models import DiarizationConfig, DiarizationMode, Interaction, ProviderKind


async def test_openai_diarizer_parses_diarized_json() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"speaker": "A", "start": 0.0, "end": 1.5, "text": "hi"},
                    {"speaker": "B", "start": 1.5, "end": 3.0, "text": "yo"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    diarizer = OpenAIDiarizer(client=client)
    segments = await diarizer.diarize(b"RIFF\x00\x00fake-wav", sample_rate=16000)
    await diarizer.aclose()

    assert [s.speaker for s in segments] == ["Speaker A", "Speaker B"]
    assert segments[0].start == 0.0
    assert segments[1].end == 3.0
    assert len(requests) == 1  # one batch call over the whole session
    assert requests[0].url.path.endswith("/audio/transcriptions")


def test_create_diarizer_openai_mode() -> None:
    diarizer = create_diarizer(DiarizationConfig(mode=DiarizationMode.OPENAI))
    assert isinstance(diarizer, OpenAIDiarizer)


async def test_the_model_comes_from_the_capability_config() -> None:
    """Not from a constant here, which is how the old one went stale.

    It is deliberately *not* the kind's transcription default either: that one
    is picked for transcription and returns no speakers at all, so a
    diarization pass would silently produce an unlabelled timeline.
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode("utf-8", errors="replace"))
        return httpx.Response(200, json={"segments": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    diarizer = OpenAIDiarizer(client=client)
    await diarizer.diarize(b"RIFF\x00\x00fake-wav")
    await diarizer.aclose()

    chosen = default_diarizing_model(ProviderKind.OPENAI)
    assert chosen is not None
    assert f'name="model"\r\n\r\n{chosen}' in sent[0]

    # And the file really does say that model returns speakers.
    spec = config().providers[ProviderKind.OPENAI]
    entry = next(m for m in spec.models_for(Interaction.TRANSCRIBE) if m.id == chosen)
    assert entry.transcribe is not None
    assert entry.transcribe.inline_diarization


def test_no_diarizing_model_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every model that returns speakers has been removed from the file.

    Posting a request with no model would surface as OpenAI complaining about a
    missing field, which points at the wrong file entirely - so this says which
    file has nothing left to offer, and what the alternative is.
    """

    def no_diarizer(_kind: ProviderKind) -> str | None:
        return None

    monkeypatch.setattr(diarizer_module, "default_diarizing_model", no_diarizer)
    with pytest.raises(ValueError, match="no OpenAI model that returns speaker labels"):
        OpenAIDiarizer()
