"""Tests for the audio device-list + default-device routes."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from loreline.web.routes.audio import parse_device


async def test_list_devices(client: AsyncClient) -> None:
    resp = await client.get("/api/audio/devices")
    assert resp.status_code == 200
    body: list[dict[str, object]] = resp.json()
    assert isinstance(body, list)
    # Empty without the audio extra; otherwise each entry has the device shape.
    for device in body:
        assert {"index", "name", "channels", "default_samplerate"} <= set(device)


async def test_input_device_roundtrip(client: AsyncClient) -> None:
    # Nothing stored yet -> system default.
    assert (await client.get("/api/audio/device")).json() == {"device": None}

    assert (await client.put("/api/audio/device", json={"device": "2"})).status_code == 200
    assert (await client.get("/api/audio/device")).json() == {"device": "2"}

    # Clearing it returns to the system default.
    await client.put("/api/audio/device", json={"device": None})
    assert (await client.get("/api/audio/device")).json() == {"device": None}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("2", 2), ("-1", -1), ("BlackHole 2ch", "BlackHole 2ch")],
)
def test_parse_device(raw: str | None, expected: int | str | None) -> None:
    assert parse_device(raw) == expected
