"""Shared pieces of the two Deepgram connectors.

Deepgram serves the same models over two transports that differ only in how the
audio arrives: ``wss://api.deepgram.com/v1/listen`` streams it (see
``deepgram.py``) and ``POST https://api.deepgram.com/v1/listen`` posts a whole
file (see ``deepgram_batch.py``). Same query parameters, same auth header, same
``alternatives[]`` payload underneath. This module holds that overlap so the two
connectors cannot drift on the parts that are genuinely one thing, most of all
the glossary field, which differs per model rather than per transport.

Docs: https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded.md
      https://developers.deepgram.com/reference/speech-to-text-api/listen-streaming
"""

from __future__ import annotations

from loreline.models import ProviderKind, Word
from loreline.stt.backends._ws import as_list, as_obj_dict, get_float, get_str
from loreline.stt.base import capped_terms, glossary_support

# Deepgram authenticates with its own scheme, not Bearer.
# https://developers.deepgram.com/docs/authenticating
_AUTH_SCHEME = "Token"
# Field for a model nobody has annotated. Keyterm prompting is the current
# generation's biasing parameter (Nova-3 and Flux); legacy `keywords` belongs to
# Nova-2 and older, and capabilities.yaml says so per model. Sending the wrong
# one is a 400, so the guess only applies where we have no annotation at all.
# https://developers.deepgram.com/docs/keyterm
_DEFAULT_GLOSSARY_FIELD = "keyterm"


def auth_headers(api_key: str | None) -> dict[str, str]:
    """Deepgram's ``Authorization: Token <key>`` header, or none when unkeyed."""
    return {"Authorization": f"{_AUTH_SCHEME} {api_key}"} if api_key else {}


def listen_params(
    *, model: str | None, language: str, terms: list[str], realtime: bool
) -> list[tuple[str, str]]:
    """Query parameters both ``/v1/listen`` transports take.

    Diarization is requested unconditionally, matching every other connector
    here: the backend always asks for speakers and the router decides whether to
    use them (see stt/router.py's DiarizationMode.INLINE branch). ``diarize`` is
    kept rather than the newer ``diarize_model``, which the docs prefer: the two
    may not be sent together, ``diarize=true`` still routes to the v1 diarizer on
    both transports, and v1 is the only version streaming accepts, so one
    parameter keeps the two connectors identical here.
    https://developers.deepgram.com/docs/diarization
    """
    params: list[tuple[str, str]] = []
    if model:
        params.append(("model", model))
    if language:
        params.append(("language", language))
    params.extend([("diarize", "true"), ("punctuate", "true")])
    params.extend(glossary_params(model, terms, realtime=realtime))
    return params


def glossary_params(
    model: str | None, terms: list[str], *, realtime: bool
) -> list[tuple[str, str]]:
    """Glossary terms as repeated query parameters, under this model's field.

    Both fields are repeated rather than comma-joined (``keyterm=a&keyterm=b``).
    A model documented as taking no biasing at all - Deepgram's hosted Whisper,
    whose feature table lists Keywords as unsupported - sends nothing rather than
    a parameter the endpoint ignores or rejects.
    https://developers.deepgram.com/docs/keyterm
    https://developers.deepgram.com/docs/keywords
    https://developers.deepgram.com/docs/deepgram-whisper-cloud
    """
    support = glossary_support(ProviderKind.DEEPGRAM, model)
    if support is not None and not support.supported:
        return []
    field = support.field if support and support.field else _DEFAULT_GLOSSARY_FIELD
    return [(field, term) for term in capped_terms(terms, support, realtime=realtime)]


def parse_alternative(alternative: dict[str, object], *, offset: float) -> tuple[str, list[Word]]:
    """One ``alternatives[]`` entry into its transcript and words.

    Identical on both transports: streaming puts the alternative under
    ``channel``, batch under ``results.channels[]``, but the entry itself carries
    the same ``transcript`` plus ``words[]`` of ``word``/``punctuated_word``/
    ``start``/``end``/``confidence``/``speaker``. Word times are relative to the
    audio submitted, so they are shifted onto the session clock by ``offset``.
    """
    transcript = get_str(alternative, "transcript")
    words: list[Word] = []
    for raw_word in as_list(alternative.get("words")):
        word_map = as_obj_dict(raw_word)
        if not word_map:
            continue
        speaker_raw = word_map.get("speaker")
        speaker = f"Speaker {speaker_raw}" if speaker_raw is not None else None
        words.append(
            Word(
                text=get_str(word_map, "punctuated_word") or get_str(word_map, "word"),
                start=get_float(word_map, "start") + offset,
                end=get_float(word_map, "end") + offset,
                confidence=get_float(word_map, "confidence") or None,
                speaker=speaker,
            )
        )
    return transcript, words
