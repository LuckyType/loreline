"""Startup recovery for orphaned session audio.

A recording whose process died uncleanly (crash, power loss, ``kill -9``) can
leave its continuous WAV on disk without the utterance-index sidecar that
re-transcription needs - the WAV itself stays valid because its header is
patched on every write. This sweep finds those orphans at startup and rebuilds
each missing sidecar in the background by re-running VAD over the stored audio,
so an interrupted session becomes re-processable again without manual surgery.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from loreline.logging import get_logger
from loreline.monitoring.alerts import AlertLevel

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from loreline.audio.chunker import SpeechDetector
    from loreline.monitoring import AlertManager
    from loreline.persistence import AudioStore, SessionRepository

log = get_logger(__name__)


def _default_detector(sample_rate: int) -> SpeechDetector:
    """The same silero VAD live capture uses (optional ``audio`` extra)."""
    from loreline.audio.vad import SileroVad  # noqa: PLC0415 - optional native dep

    return SileroVad(sample_rate=sample_rate).is_speech


async def recover_orphaned_indexes(
    *,
    audio_store: AudioStore,
    sessions: SessionRepository,
    alerter: AlertManager | None,
    abort: threading.Event,
    active_session_id: Callable[[], str | None] = lambda: None,
    detector_factory: Callable[[int], SpeechDetector] | None = None,
) -> int:
    """Rebuild missing index sidecars for stored WAVs; return how many.

    Runs the per-file VAD pass in a worker thread (it is CPU-bound and can take
    minutes for a multi-hour recording), checking ``abort`` per audio frame so
    shutdown isn't held up - an aborted rebuild writes nothing and is retried
    on the next startup. WAVs without a session row (stray files) and the
    active session's own recording are left alone.
    """
    orphans = audio_store.orphaned_wavs()
    if not orphans:
        return 0
    if detector_factory is None:
        try:
            # Probe once, off the loop (importing torch + loading the ONNX
            # model blocks for seconds), so a missing audio extra downgrades
            # to one warning instead of a stack trace per orphan.
            await asyncio.to_thread(_default_detector, 16000)
        except Exception as exc:
            log.warning("audio.recovery.vad_unavailable", error=str(exc), orphans=len(orphans))
            return 0
        detector_factory = _default_detector

    recovered = 0
    for session_id in orphans:
        if abort.is_set():
            break
        session = await sessions.get(session_id)
        if session is None or session_id == active_session_id():
            continue
        try:
            count = await asyncio.to_thread(
                audio_store.rebuild_index,
                session_id,
                detector_factory=detector_factory,
                base_ts=session.started_mono,
                should_abort=abort.is_set,
            )
        except Exception:
            log.exception("audio.recovery.failed", session_id=session_id)
            continue
        if count is None:  # aborted mid-file (shutdown)
            break
        recovered += 1
        log.info("audio.recovery.index_rebuilt", session_id=session_id, utterances=count)
        if alerter is not None:
            with contextlib.suppress(Exception):  # best-effort, like the manager's alerts
                await alerter.send(
                    "Session audio recovered",
                    f"Rebuilt the utterance index for interrupted session {session_id} "
                    f"({count} utterances) - it can be re-transcribed now.",
                    level=AlertLevel.INFO,
                )
    return recovered
