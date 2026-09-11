"""Turn an uploaded recording into a stored session.

A GM's own recording of a session - a phone memo, a Discord rip, a handheld
recorder's card - becomes a session row indistinguishable from a captured one:
the same continuous 16 kHz mono WAV, the same utterance index sidecar beside
it, the same ``completed`` status. Nothing downstream is told where the audio
came from, which is the whole design (see ``docs/adr/0008``): re-transcribe,
diarize, rename speakers, summarize, export, merge, delete and video all work
on an import because none of them can tell.

Three steps, in this order for a reason. The upload lands on disk (never in
memory: a four hour recording is hundreds of MB), it is decoded into the
capture format, and only then is a row written - so a decode that fails leaves
neither a session nor a WAV behind, and a client that hangs up mid-upload
leaves nothing at all.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import IO, TYPE_CHECKING

from loreline.audio.decode import decode_recording, discard
from loreline.audio.vad import default_detector
from loreline.logging import get_logger
from loreline.models import Session, SessionOrigin, SessionStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from loreline.audio.chunker import SpeechDetector
    from loreline.persistence import AudioStore, SessionRepository
    from loreline.settings import Settings

log = get_logger(__name__)


class UploadTooLargeError(ValueError):
    """Raised when an upload runs past the configured size ceiling."""


class IndexingUnavailableError(RuntimeError):
    """Raised when the VAD that cuts a recording into utterances is missing.

    Its own failure rather than a warning, unlike the startup sweep's: that one
    leaves an existing recording unindexed and retries next boot, while an
    import with no index would create a session that can never be transcribed
    at all. Refusing it keeps the upload the GM still has on their phone as the
    only copy that matters.
    """


def _open_write(path: Path) -> IO[bytes]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("wb")


async def receive_upload(chunks: AsyncIterator[bytes], dest: Path, *, limit: int) -> int:
    """Write an upload to ``dest`` chunk by chunk; return how many bytes.

    Counted as it goes and refused the moment the count passes ``limit``, so a
    caller sending more than the ceiling stops costing disk at the ceiling
    rather than at whatever it decided to send. The writes go through a thread
    because a recording is large enough that blocking the event loop on its
    page cache flushes would stall every other request for the length of the
    upload.
    """
    size = 0
    out = await asyncio.to_thread(_open_write, dest)
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > limit:
                msg = f"this recording is larger than the {limit // (1024 * 1024)} MB import limit"
                raise UploadTooLargeError(msg)
            await asyncio.to_thread(out.write, chunk)
    finally:
        await asyncio.to_thread(out.close)
    return size


async def import_recording(
    chunks: AsyncIterator[bytes],
    *,
    name: str,
    started_at: float | None,
    campaign_id: str | None,
    sessions: SessionRepository,
    audio_store: AudioStore,
    settings: Settings,
    detector_factory: Callable[[int], SpeechDetector] | None = None,
) -> Session:
    """Store an uploaded recording as a completed session and return its row.

    ``started_at`` is wall-clock epoch seconds, defaulting to now. The row's
    ``started_mono`` is 0, which is what makes the recording's own clock the
    session clock: a re-transcription rebases every event by ``started_mono``
    (see ``ReprocessManager._drive``), so with 0 a segment's timestamp is its
    position in the WAV - exactly what the index sidecar built below records,
    and what the player and the exports read.

    Raises ``UploadTooLargeError``, ``DecodeError`` (including its ffmpeg and
    duration subclasses) or ``IndexingUnavailableError``; none of them leaves a
    session row, a WAV or a temp file behind.
    """
    session_id = uuid.uuid4().hex
    upload_path = settings.imports_dir / f"{session_id}.upload"
    decoded_path = settings.imports_dir / f"{session_id}.decoded.wav"
    began = time.monotonic()
    try:
        size_bytes = await receive_upload(chunks, upload_path, limit=settings.import_max_bytes)
        decoded = await decode_recording(
            upload_path,
            decoded_path,
            size_bytes=size_bytes,
            max_seconds=settings.import_max_seconds,
        )
    finally:
        # The source is of no further use the moment it is decoded, and of no
        # use at all if it was not: gone on every path, including a cancelled
        # request whose upload never finished.
        discard(upload_path)

    try:
        utterances = await _adopt(
            decoded_path,
            session_id,
            audio_store=audio_store,
            detector_factory=detector_factory or default_detector,
        )
    except BaseException:
        discard(decoded_path)
        audio_store.delete(session_id)
        raise

    began_at = started_at if started_at is not None else time.time()
    session = Session(
        id=session_id,
        # Completed on arrival: nothing is going to be appended to this
        # recording, which is the one thing every reader of `status` cares
        # about (the history list, the merge guard, the player).
        status=SessionStatus.COMPLETED,
        started_at=began_at,
        started_mono=0.0,
        ended_at=began_at + decoded.duration_s,
        campaign_id=campaign_id,
        # No provider heard this audio. A transcription of it names its own
        # provider on its job row, which is where that fact belongs for an
        # import exactly as it does for a re-transcription of a capture.
        primary_provider=None,
        audio_path=str(audio_store.wav_path(session_id)),
        origin=SessionOrigin.IMPORT,
        import_name=name,
    )
    await sessions.create(session)
    log.info(
        "session.imported",
        session_id=session_id,
        name=name,
        bytes=size_bytes,
        duration_s=round(decoded.duration_s, 1),
        decoder=decoded.decoder,
        utterances=utterances,
        elapsed_s=round(time.monotonic() - began, 1),
    )
    return session


async def _adopt(
    decoded_path: Path,
    session_id: str,
    *,
    audio_store: AudioStore,
    detector_factory: Callable[[int], SpeechDetector],
) -> int:
    """Move the decoded WAV into the audio store and index it.

    The move is a rename, which is atomic and instant because the import
    scratch space and the audio store are both under the data dir; copying
    hundreds of MB a second time would be the slowest thing in the request.

    The index is then rebuilt by the same code and the same detector the
    startup sweep uses on an orphaned recording, with ``base_ts=0`` because
    audio position zero is session start for an import. That is what ADR 0008
    means by an import getting no transcription pipeline of its own: this is
    the only bespoke step, and it is a call to something that already existed.
    """
    await asyncio.to_thread(_move_into_place, decoded_path, audio_store.wav_path(session_id))
    try:
        # CPU-bound (VAD inference over the whole recording), so off the loop.
        # No `should_abort`, so the count is never None.
        count = await asyncio.to_thread(
            audio_store.rebuild_index,
            session_id,
            detector_factory=detector_factory,
            base_ts=0.0,
        )
    except ImportError as exc:
        # The one failure worth its own sentence: the detector could not be
        # built at all because the optional `audio` extra is absent. Anything
        # else that goes wrong here is a real fault and stays one.
        msg = (
            "this server cannot split a recording into utterances: the speech detector "
            "is unavailable (the optional `audio` extra is not installed)."
        )
        raise IndexingUnavailableError(msg) from exc
    return count or 0


def _move_into_place(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
