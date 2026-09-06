"""Tests for services/diarization/app.py's own /healthz readiness gate.

This is the small, separate sherpa-onnx-backed diarization service bundled in
this repo and run as its own Docker container (see ``services/diarization``),
not the mock at ``mocks/diarization.py`` that ``test_diarization.py`` in this
same directory uses to exercise ``RemoteDiarizer`` end to end.

sherpa-onnx is a native wheel and, like numpy, is never installed for these
tests (kept out of base CI on purpose, see ``services/diarization/app.py``'s
own module docstring), so the "not ready" cases below run the service's real
``_load_pipeline`` and let its own import failure prove the 503, while the
"loaded" case stubs it to check the readiness gate itself rather than
sherpa-onnx's model loading.
"""

from __future__ import annotations

import httpx
import pytest

import services.diarization.app as diarization_app


def _transport() -> httpx.ASGITransport:
    """A transport onto a fresh service app, so no test shares another's cache."""
    return httpx.ASGITransport(app=diarization_app.create_app())


async def test_healthz_503_before_models_are_ready() -> None:
    """The bug this guards: /healthz used to answer 200 in this exact state.

    No monkeypatching: DIAR_SEGMENTATION_MODEL/DIAR_EMBEDDING_MODEL are unset
    in the test process and sherpa-onnx is not installed, so this drives the
    service's real ``_load_pipeline`` failure, the same one a misconfigured
    deployment hits.
    """
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert "detail" in response.json()


async def test_healthz_matches_diarize_failure_shape() -> None:
    """/healthz's 503 must read exactly like /diarize's documented one.

    Before this fix only /diarize could ever answer 503; now both endpoints
    share the same readiness check, so a failure looks identical either way.
    """
    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        healthz_response = await client.get("/healthz")
        diarize_response = await client.post(
            "/diarize", files={"file": ("audio.wav", b"", "audio/wav")}
        )

    assert healthz_response.status_code == 503
    assert diarize_response.status_code == 503
    assert healthz_response.json() == diarize_response.json()


async def test_healthz_503_reports_the_unconfigured_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact real-world incident: models never configured on a live box.

    Stubs ``_load_pipeline`` to raise precisely what it raises for unset env
    vars, independent of whether sherpa-onnx happens to be installed, so this
    documents and locks in the specific failure this bug report described.
    """

    def unconfigured(num_clusters: int = -1) -> tuple[object, object]:
        _ = num_clusters
        msg = "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
        raise RuntimeError(msg)

    monkeypatch.setattr(diarization_app, "_load_pipeline", unconfigured)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "DIAR_SEGMENTATION_MODEL and DIAR_EMBEDDING_MODEL must be set"
    }


async def test_healthz_ok_once_models_load_and_caches_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once ready, /healthz reports it, and does not reload on every poll."""
    calls: list[int] = []

    def fake_load_pipeline(num_clusters: int = -1) -> tuple[object, object]:
        calls.append(num_clusters)
        return object(), object()

    monkeypatch.setattr(diarization_app, "_load_pipeline", fake_load_pipeline)

    async with httpx.AsyncClient(transport=_transport(), base_url="http://diar") as client:
        first = await client.get("/healthz")
        second = await client.get("/healthz")

    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    assert second.status_code == 200
    assert calls == [-1]  # loaded once; the second poll reused the cached pipeline
