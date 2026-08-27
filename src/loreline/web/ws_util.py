"""Shared helper for WS routes that only ever push server-driven events.

A handler that just does ``async for item in stream: await ws.send_text(...)``
never calls ``ws.receive()``, so it never observes the ASGI
``websocket.disconnect`` message the server enqueues for a connection on
graceful shutdown (see e.g. ``uvicorn.protocols.websockets.*.shutdown``) -
sending a close frame to the client is not the same as anything waking up a
handler blocked on an unrelated internal queue with nothing to deliver.
Left alone, any client with the socket open (the Dashboard, most commonly)
wedges the whole process in "Waiting for background tasks to complete" until
something external (systemd's stop timeout) kills it outright.

``stream_until_disconnected`` races each "wait for the next item" against a
background watch for that disconnect message (client-initiated close counts
too), so the loop ends promptly either way instead of leaning on the
producer side ever finishing on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator

from fastapi import WebSocket


async def _watch_disconnect(ws: WebSocket) -> None:
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return


async def stream_until_disconnected[T](
    ws: WebSocket, source: AsyncIterator[T]
) -> AsyncGenerator[T, None]:
    """Yield items from ``source`` until the client disconnects or the server does."""
    disconnect = asyncio.ensure_future(_watch_disconnect(ws))
    try:
        while True:
            next_item = asyncio.ensure_future(source.__anext__())
            done, _pending = await asyncio.wait(
                {next_item, disconnect}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect in done:
                next_item.cancel()
                with contextlib.suppress(BaseException):
                    await next_item
                return
            try:
                yield next_item.result()
            except StopAsyncIteration:
                return
    finally:
        disconnect.cancel()
        with contextlib.suppress(BaseException):
            await disconnect
