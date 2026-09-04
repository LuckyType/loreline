"""Verify a backend sends the provider's configured transcription language."""

from __future__ import annotations

import json

from websockets.asyncio.server import ServerConnection, serve

from loreline.audio.chunker import Utterance
from loreline.models import ProviderConfig, ProviderKind
from loreline.stt.backends.deepgram import DeepgramBackend


def _one() -> Utterance:
    return Utterance(pcm=b"\x01\x00" * 800, start=0.0, end=0.1)


async def test_deepgram_sends_config_language() -> None:
    seen: dict[str, str] = {}

    async def handler(ws: ServerConnection) -> None:
        if ws.request is not None:
            seen["path"] = ws.request.path
        async for message in ws:
            if isinstance(message, str):  # CloseStream control frame
                await ws.send(json.dumps({"type": "Metadata"}))
                return

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = ProviderConfig(
            id="dg",
            name="dg",
            kind=ProviderKind.DEEPGRAM,
            base_url=f"ws://127.0.0.1:{port}",
            language="en",
        )
        backend = DeepgramBackend(config, api_key="k")
        _ = await backend.transcribe(_one(), session_id="s")

    assert "language=en" in seen["path"]
