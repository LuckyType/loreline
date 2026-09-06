"""Shared pieces of the two xAI connectors.

xAI serves one speech-to-text engine over two transports that differ only in
how the audio arrives and how the parameters are spelled onto the request:
``wss://api.x.ai/v1/stt`` streams it with the configuration in the query string
(see ``xai.py``) and ``POST https://api.x.ai/v1/stt`` posts a whole file as
``multipart/form-data`` (see ``xai_batch.py``). Same parameter *names* on both
sides, same ``words[]`` payload underneath. This module holds that overlap so
the two connectors cannot drift on the parts that are genuinely one thing: the
glossary field and its per-term ceiling, the diarization request, and how a
word becomes a :class:`~loreline.models.Word`.

The naming is close enough to Deepgram's that ``_deepgram.py`` is the module
this one is shaped after: ``keyterm`` repeated once per term, ``diarize`` as a
boolean, an integer ``speaker`` on every word. What differs is that there is no
``model`` to send at all, on either transport, and that xAI states the glossary
ceiling in terms rather than tokens, so it can be enforced here.

NOTE ON THE MISSING MODEL PARAMETER. Neither endpoint documents one, and the
REST reference lists every field of both without it. So none is sent, and the
model id the registry resolves is used for nothing but the capability lookup
the registry has already done. See the grok-stt-1.0 entry in capabilities.yaml
for why the file names a model this code never puts on the wire.

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text
      https://docs.x.ai/developers/rest-api-reference/inference/voice
"""

from __future__ import annotations

from loreline.capability_config import TranscribeCapabilities
from loreline.models import Word
from loreline.stt.backends._ws import as_list, as_obj_dict, get_float, get_str
from loreline.stt.base import glossary_support, glossary_terms_for

# The request field for keyword biasing on a model nobody has annotated. xAI
# documents exactly one engine and exactly one spelling for it, so unlike
# Deepgram (whose generations disagree, keyterm against keywords) this fallback
# can only ever be right.
_DEFAULT_GLOSSARY_FIELD = "keyterm"

# Per-term character ceiling for a model the yaml does not annotate. The
# documented limit is 50 characters per key term; a longer one is dropped rather
# than truncated, because half a proper noun biases towards the wrong word.
_DEFAULT_MAX_TERM_CHARS = 50


def stt_params(
    *,
    caps: TranscribeCapabilities | None,
    language: str,
    terms: list[str],
    realtime: bool,
) -> list[tuple[str, str]]:
    """The parameters both ``/v1/stt`` transports take, as name/value pairs.

    Pairs rather than a mapping because the glossary is repeated: ``keyterm``
    appears once per term on the socket's query string and once per term as a
    multipart field, and a mapping cannot hold that.

    Diarization is requested unconditionally, matching every other connector
    here: the backend always asks for speakers and the router decides whether to
    use them (see stt/router.py's DiarizationMode.INLINE branch).

    No ``model``: neither transport has such a parameter. ``format`` (inverse
    text normalization, so "twenty twenty six" is written 2026) is likewise left
    at the vendor's default of false on both, because a transcript that a
    diarizer relabels and a GM reads should carry what was said.
    """
    params: list[tuple[str, str]] = []
    if language:
        # A BCP-47 hint. Omitted when the row names none, which is the vendor's
        # own auto-detection across its 25+ languages rather than a guess here.
        params.append(("language", language))
    params.append(("diarize", "true"))
    params.extend(glossary_params(caps, terms, realtime=realtime))
    return params


def glossary_params(
    caps: TranscribeCapabilities | None, terms: list[str], *, realtime: bool
) -> list[tuple[str, str]]:
    """Glossary terms as repeated ``keyterm`` parameters, within both ceilings.

    Two limits, and they are not the same kind of limit. The count (100 terms)
    is the shared policy in :func:`loreline.stt.base.glossary_terms_for`, which
    trims head-first because glossary order is priority order. The per-term
    length (50 characters) is enforced here, by dropping the term rather than
    cutting it: a truncated proper noun is a term that biases recognition
    towards something nobody said, which is worse than not biasing at all.
    """
    support = glossary_support(caps)
    field = support.field if support and support.field else _DEFAULT_GLOSSARY_FIELD
    max_chars = (
        support.max_term_chars if support and support.max_term_chars else _DEFAULT_MAX_TERM_CHARS
    )
    allowed = glossary_terms_for(caps, terms, realtime=realtime)
    return [(field, term) for term in allowed if len(term) <= max_chars]


def parse_words(payload: dict[str, object], *, offset: float) -> list[Word]:
    """A ``words[]`` array into words on the session clock.

    Identical on both transports: the batch response and the socket's
    ``transcript.partial`` / ``transcript.done`` events all carry the same
    entries of ``text``/``start``/``end``, plus an integer ``speaker`` when
    ``diarize=true``. Times are relative to the audio submitted, so ``offset``
    shifts them onto the session clock.

    No confidence: xAI publishes none per word, so the field stays None rather
    than being filled with a number this vendor never sent.
    """
    words: list[Word] = []
    for raw_word in as_list(payload.get("words")):
        word_map = as_obj_dict(raw_word)
        text = get_str(word_map, "text")
        if not text:
            continue
        words.append(
            Word(
                text=text,
                start=get_float(word_map, "start") + offset,
                end=get_float(word_map, "end") + offset,
                speaker=speaker_label(word_map.get("speaker")),
            )
        )
    return words


def speaker_label(raw: object) -> str | None:
    """xAI's integer speaker index as this app's label, or None when absent.

    Absent is the normal state for a response to a request that did not ask for
    diarization, and for a word the service could not attribute. Spelled the
    same way as Deepgram's ("Speaker 0"), because the labels end up side by side
    in one transcript view.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return f"Speaker {int(raw)}"
    if isinstance(raw, str) and raw:
        return f"Speaker {raw}"
    return None
