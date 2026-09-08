"""Unit tests for the AssemblyAI streaming query string's language parameter.

The bug this pins the fix for: the connector used to send a plain
``language=<code>``, which Universal-Streaming v3 does not define as a
parameter at all, so it was silently dropped and the vendor was never told
what to expect. The real parameter is ``language_codes``, a JSON array
encoded as a single query value (``?language_codes=["de"]``), and it is
documented for ``universal-3-5-pro`` only.
https://www.assemblyai.com/docs/api-reference/streaming-api/streaming-api

Both connector shapes build their query string through the one ``_params``
method, so these tests exercise ``prepare`` (what the utterance shape opens
its socket with) and ``_params(..., streaming=True)`` (what ``open_stream``
sends) side by side and pin that they cannot disagree.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode, urlparse

from loreline.models import ProviderConfig, ProviderKind
from loreline.stt.backends.assemblyai import AssemblyAIBackend

_KEY = "test-key"


def _config(language: str) -> ProviderConfig:
    return ProviderConfig(
        id="aai", name="AssemblyAI", kind=ProviderKind.ASSEMBLYAI, language=language
    )


def _query(backend: AssemblyAIBackend, *, streaming: bool) -> dict[str, list[str]]:
    """The parsed query string either shape would open its socket with."""
    if streaming:
        params = backend._params(None, streaming=True)  # pyright: ignore[reportPrivateUsage]
        return parse_qs(urlencode(params))
    return parse_qs(urlparse(backend.prepare(None)).query)


def test_one_language_becomes_a_json_array_in_the_query_string() -> None:
    backend = AssemblyAIBackend(_config("de"), api_key=_KEY)

    for streaming in (False, True):
        query = _query(backend, streaming=streaming)
        # The exact wire encoding: a JSON array, not a bare code or a CSV list.
        assert query["language_codes"] == ['["de"]']
        assert json.loads(query["language_codes"][0]) == ["de"]


def test_no_language_omits_the_parameter_on_both_shapes() -> None:
    backend = AssemblyAIBackend(_config(""), api_key=_KEY)

    for streaming in (False, True):
        assert "language_codes" not in _query(backend, streaming=streaming)


def test_the_old_plain_language_parameter_is_gone() -> None:
    """This was the bug: v3 has no `language` parameter and ignored it."""
    backend = AssemblyAIBackend(_config("de"), api_key=_KEY)

    for streaming in (False, True):
        assert "language" not in _query(backend, streaming=streaming)


def test_the_utterance_and_streaming_shapes_cannot_disagree() -> None:
    """One query builder backs both `prepare` and `open_stream`."""
    backend = AssemblyAIBackend(_config("de"), api_key=_KEY, model="universal-3-5-pro")

    assert (
        _query(backend, streaming=False)["language_codes"]
        == _query(backend, streaming=True)["language_codes"]
    )


def test_language_codes_is_sent_for_the_default_and_the_pro_model() -> None:
    """No model chosen inherits the endpoint's own default, universal-3-5-pro."""
    for model in (None, "universal-3-5-pro"):
        backend = AssemblyAIBackend(_config("de"), api_key=_KEY, model=model)
        assert "language_codes" in _query(backend, streaming=True)


def test_language_codes_is_withheld_from_models_that_do_not_document_it() -> None:
    """universal-streaming-english is English only; universal-streaming-
    multilingual auto code-switches its own six languages. Neither documents
    `language_codes`, so nothing is sent rather than risking a validation
    error for a key that model does not accept."""
    for model in ("universal-streaming-english", "universal-streaming-multilingual"):
        backend = AssemblyAIBackend(_config("de"), api_key=_KEY, model=model)
        assert "language_codes" not in _query(backend, streaming=True)
