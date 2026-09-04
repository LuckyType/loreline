"""The diarizer factory: one owner of construction and of the OpenAI key."""

from __future__ import annotations

import pytest

from loreline.diarization import openai_diarizer
from loreline.diarization.base import NoopDiarizer
from loreline.diarization.provider import OPENAI_KEY_ENV, DiarizerFactory, resolve_openai_key
from loreline.models import DiarizationConfig, DiarizationMode, ProviderConfig, ProviderKind


class FakeRows:
    """The provider list the factory reads, without a database."""

    def __init__(self, *rows: ProviderConfig) -> None:
        self._rows = list(rows)

    async def list(self) -> list[ProviderConfig]:
        return self._rows


class FakeSecrets:
    def __init__(self, **values: str) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


def _row(kind: ProviderKind, auth_ref: str | None = None) -> ProviderConfig:
    return ProviderConfig(id=kind.value, name=kind.value, kind=kind, auth_ref=auth_ref)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Records the key every OpenAI diarizer is constructed with."""
    keys: list[str | None] = []

    class Recording(NoopDiarizer):
        def __init__(self, *, api_key: str | None = None, **_: object) -> None:
            keys.append(api_key)

    monkeypatch.setattr(openai_diarizer, "OpenAIDiarizer", Recording)
    return keys


def test_a_configured_openai_row_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OPENAI_KEY_ENV, "sk-from-env")
    rows = [_row(ProviderKind.DEEPGRAM, "dg"), _row(ProviderKind.OPENAI, "oai")]
    assert resolve_openai_key(rows, FakeSecrets(dg="sk-dg", oai="sk-from-store")) == "sk-from-store"


def test_the_environment_answers_when_no_row_has_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPENAI_KEY_ENV, "sk-from-env")
    # A row of the wrong kind, an OpenAI row with nothing stored under its
    # reference, and one with no reference at all: none of them counts.
    rows = [
        _row(ProviderKind.DEEPGRAM, "dg"),
        _row(ProviderKind.OPENAI, "oai"),
        _row(ProviderKind.OPENAI),
    ]
    assert resolve_openai_key(rows, FakeSecrets(dg="sk-dg")) == "sk-from-env"


def test_no_key_anywhere_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPENAI_KEY_ENV, raising=False)
    assert resolve_openai_key([_row(ProviderKind.OPENAI, "oai")], FakeSecrets()) is None


async def test_the_factory_hands_the_resolved_key_to_the_openai_diarizer(
    monkeypatch: pytest.MonkeyPatch, built: list[str | None]
) -> None:
    monkeypatch.setenv(OPENAI_KEY_ENV, "sk-from-env")
    factory = DiarizerFactory(
        FakeRows(_row(ProviderKind.OPENAI, "oai")), FakeSecrets(oai="sk-from-store")
    )
    await factory(DiarizationConfig(mode=DiarizationMode.OPENAI))
    assert built == ["sk-from-store"]


async def test_the_factory_builds_every_other_mode_without_a_key(
    built: list[str | None],
) -> None:
    factory = DiarizerFactory(FakeRows(), FakeSecrets())
    for mode in (DiarizationMode.NONE, DiarizationMode.INLINE):
        assert isinstance(await factory(DiarizationConfig(mode=mode)), NoopDiarizer)
    with pytest.raises(ValueError, match="requires an endpoint"):
        await factory(DiarizationConfig(mode=DiarizationMode.REMOTE))
    assert built == []
