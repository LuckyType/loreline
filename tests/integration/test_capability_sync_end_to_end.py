"""The sync command end to end, from an HTTP response to a rewritten file.

The planner's own rules live in tests/unit/test_capability_sync.py; what is
pinned here is the wiring around them: that the fetch really is the read half's
fetch, that a vendor which fails to answer produces no edits at all, and that
the write path refuses to leave an invalid file behind.

Every response is served by an ``httpx.MockTransport``, and every write lands
in a tmp_path copy of the real file. Nothing here touches a vendor or the
checked-in capabilities.yaml.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from loreline.capabilities import config as shipped_config
from loreline.capability_config import CONFIG_PATH
from loreline.models import ProviderKind
from loreline.staleness.catalog import ClientFactory
from loreline.staleness.sync import SyncRefusedError, run_sync, write

NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"
NEMOTRON_PUBLISHED = 182520

# One chat row and one video row, both trimmed to the fields the sync reads.
# The other curated models being absent is deliberate: that is a "vendor no
# longer lists it" finding, which this command must not act on.
_CHAT_BODY: dict[str, object] = {
    "data": [
        {
            "id": NEMOTRON,
            "context_length": 262144,
            "top_provider": {"max_completion_tokens": NEMOTRON_PUBLISHED},
            "supported_parameters": ["temperature", "reasoning", "max_tokens"],
            "reasoning": {"mandatory": False, "supported_efforts": ["high", "medium"]},
            "expiration_date": None,
        }
    ]
}

_VIDEO_BODY: dict[str, object] = {
    "data": [
        {
            "id": "runway/gen-4.5",
            "supported_durations": [2, 3, 4, 5, 6, 7, 8, 9, 10],
            "supported_resolutions": ["720p"],
            "supported_aspect_ratios": ["16:9", "9:16"],
            "supported_frame_images": ["first_frame"],
            "generate_audio": False,
        }
    ]
}


@pytest.fixture(autouse=True)
def _no_ambient_credentials(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer's exported keys must not change what these tests fetch."""
    for name in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ASSEMBLYAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _serving() -> httpx.MockTransport:
    """OpenRouter's three catalogues, routed by the address the config names."""

    def handle(request: httpx.Request) -> httpx.Response:
        if "videos/models" in str(request.url):
            return httpx.Response(200, json=_VIDEO_BODY)
        return httpx.Response(200, json=_CHAT_BODY)

    return httpx.MockTransport(handle)


def _factory(transport: httpx.MockTransport) -> ClientFactory:
    return lambda: httpx.AsyncClient(transport=transport)


async def test_a_published_value_reaches_the_file_through_the_real_fetch() -> None:
    plan = await run_sync(client_factory=_factory(_serving()))
    assert [(c.model, c.fact, c.after) for c in plan.changes] == [
        (NEMOTRON, "llm.max_output_tokens", str(NEMOTRON_PUBLISHED))
    ]
    assert plan.dirty


async def test_writing_lands_on_disk_and_still_validates(tmp_path: Path) -> None:
    target = tmp_path / "capabilities.yaml"
    target.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    plan = await run_sync(client_factory=_factory(_serving()))

    config = write(plan, target)

    model = next(m for m in config.providers[ProviderKind.OPENROUTER].models if m.id == NEMOTRON)
    assert model.llm is not None
    assert model.llm.max_output_tokens == NEMOTRON_PUBLISHED
    written = target.read_text(encoding="utf-8")
    assert f"max_output_tokens: {NEMOTRON_PUBLISHED}" in written
    # The checked-in file is untouched, and the written one is the original
    # plus that single value.
    original = CONFIG_PATH.read_text(encoding="utf-8")
    assert original != written
    assert len(original.splitlines()) == len(written.splitlines())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="upstream unavailable"),
        httpx.Response(429, text="slow down"),
        httpx.Response(200, text="<html>maintenance</html>"),
    ],
)
async def test_a_vendor_that_did_not_answer_writes_nothing(response: httpx.Response) -> None:
    """The failure that must never be read as "the vendor dropped this value".

    A 503, a rate limit and a body that is not the catalogue at all are three
    different accidents with one correct outcome: the file is left exactly as
    it is, and the run says so rather than reporting a clean sync.
    """
    transport = httpx.MockTransport(lambda _r: response)
    plan = await run_sync(client_factory=_factory(transport))
    assert plan.changes == ()
    assert not plan.dirty
    assert plan.answered == ()


async def test_a_connection_failure_writes_nothing() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    plan = await run_sync(client_factory=_factory(httpx.MockTransport(refuse)))
    assert plan.changes == ()
    assert not plan.dirty


async def test_the_write_is_refused_rather_than_leaving_an_invalid_file(
    tmp_path: Path,
) -> None:
    """Verification runs before the write, so a rejected result never lands.

    The file used here has had its one video default removed, which the schema
    refuses ("exactly one default per offered interaction"). The sync itself is
    innocent; the point is that it will not hand a broken file to disk however
    it got broken.
    """
    source = CONFIG_PATH.read_text(encoding="utf-8")
    broken = tmp_path / "capabilities.yaml"
    # Drop the video default marker, which the loader insists on.
    without_default = source.replace(
        '      - id: google/veo-3.1-fast\n        label: "Veo 3.1 Fast"\n',
        '      - id: google/veo-3.1-fast\n        label: "Veo 3.1 Fast"\n        hidden: true\n',
        1,
    )
    assert without_default != source
    broken.write_text(without_default, encoding="utf-8")

    plan = await run_sync(client_factory=_factory(_serving()), source_path=broken)
    with pytest.raises(SyncRefusedError, match="no longer validates"):
        write(plan, broken)
    assert broken.read_text(encoding="utf-8") == without_default


async def test_the_shipped_config_is_what_gets_planned_against() -> None:
    """A sanity check on the wiring: no probe, no plan, and the config is the
    real one rather than a fixture the test happens to agree with."""
    plan = await run_sync(config=shipped_config(), client_factory=_factory(_serving()))
    assert {p.kind for p in plan.answered} == {ProviderKind.OPENROUTER}
