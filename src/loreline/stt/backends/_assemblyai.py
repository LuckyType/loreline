"""Shared pieces of the two AssemblyAI connectors.

The two AssemblyAI APIs look nothing alike from the outside - one is a
WebSocket session (``assemblyai.py``), the other an upload, a job and a poll
(``assemblyai_batch.py``) - but they agree on the parts that would silently rot
if written twice: the per-word payload (``text``/``start``/``end``/
``confidence``/``speaker``, times in milliseconds on both), and the glossary
field, whose ceiling is the same ``keyterms_prompt`` capped differently per
transport (1000 terms async, 100 streaming, for the same model). The two hosts
and the bare-key ``Authorization`` header are the surfaces in capabilities.yaml.

Docs: https://www.assemblyai.com/docs/api-reference/overview
"""

from __future__ import annotations

from loreline.capability_config import TranscribeCapabilities
from loreline.models import Word
from loreline.stt.backends._ws import as_list, as_obj_dict, get_float, get_str
from loreline.stt.base import glossary_terms_for

_MS_PER_S = 1000.0


def glossary_for(
    caps: TranscribeCapabilities | None, terms: list[str], *, realtime: bool
) -> list[str]:
    """Glossary terms for ``keyterms_prompt``, capped for this transport.

    Streaming rejects a request carrying more than 100 terms outright, and a
    rejected session costs the whole utterance, so the cap is enforced here
    rather than discovered at the vendor. The numbers live in capabilities.yaml
    per model, including the async/streaming split on universal-3-5-pro.
    https://www.assemblyai.com/docs/streaming/prompting-and-keyterms
    https://www.assemblyai.com/docs/pre-recorded-audio/universal-3-5-pro/prompting
    """
    return glossary_terms_for(caps, terms, realtime=realtime)


def parse_words(raw_words: object, *, offset: float) -> list[Word]:
    """A ``words[]`` array into words on the session clock.

    Identical on both transports: a streaming Turn message and an async
    transcript both carry ``text``, ``start``, ``end``, ``confidence`` and, with
    speaker labels on, ``speaker`` ("A", "B", …; "PENDING" while the streaming
    model still has too little audio to attribute a word). Times are in
    milliseconds relative to the audio submitted, so they are converted and
    shifted onto the session clock.
    """
    words: list[Word] = []
    for raw_word in as_list(raw_words):
        word_map = as_obj_dict(raw_word)
        if not word_map:
            continue
        speaker_raw = word_map.get("speaker")
        speaker = f"Speaker {speaker_raw}" if speaker_raw is not None else None
        words.append(
            Word(
                text=get_str(word_map, "text"),
                start=get_float(word_map, "start") / _MS_PER_S + offset,
                end=get_float(word_map, "end") / _MS_PER_S + offset,
                confidence=get_float(word_map, "confidence") or None,
                speaker=speaker,
            )
        )
    return words
