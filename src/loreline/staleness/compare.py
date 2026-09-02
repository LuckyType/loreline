"""Curated file versus vendor catalogue: what actually differs.

The middle third of the check. It takes what the yaml says (via the typed
config) and what a vendor said (via :mod:`loreline.staleness.catalog`) and
produces findings. It performs no I/O, which is what makes it testable against
a hand-written catalogue and reusable by the sync script that will later
regenerate the derivable fields instead of hand editing them.

WHAT IS DERIVABLE, AND WHAT IS NOT
==================================

The boundary is the whole design. Claiming a hand-annotated fact is derivable
is how this check would start producing confident wrong answers, so it is
written down here rather than left to whoever edits next.

Derived, and checked (the vendor publishes the fact itself):

* existence: is this id still in the catalogue at all.
* the vendor's own retirement date: OpenRouter ``expiration_date``, OpenAI
  ``shutdown_date``, against the yaml's ``deprecated``.
* ``llm.context_length`` <- ``context_length``.
* ``llm.max_output_tokens`` <- ``top_provider.max_completion_tokens``.
* ``llm.temperature`` <- whether ``temperature`` is in ``supported_parameters``.
* ``llm.reasoning.{supported, efforts, mandatory}`` <- the ``reasoning`` block
  (``supported_efforts``, ``mandatory``).
* the whole ``video:`` block except the prompt limits <- ``supported_durations``,
  ``supported_resolutions``, ``supported_aspect_ratios``,
  ``supported_frame_images``, ``generate_audio``.

Hand-annotated, and deliberately never checked against a catalogue:

* ``transcribe.realtime`` / ``transcribe.batch``. These are properties of the
  *endpoint*, not of the model: the same Deepgram model streams over the
  websocket and posts over REST, and OpenRouter exposes only the posting half.
  No catalogue publishes it.
* ``transcribe.inline_diarization``, ``glossary.*``, ``word_timestamps``,
  ``languages``. Each is the intersection of what the vendor offers and what
  this repo's connector extracts, which no vendor knows.
* ``live_capture``, the labels, and the curation itself.
* ``llm.system_prompt``: OpenRouter publishes no per-model flag for it.
* ``video.prompt_max_chars`` / ``prompt_max_tokens``: documented in prose, not
  in the catalogue.

THE TRAP, verified on 2026-09-02: the transcription catalogue
(``/api/v1/models?output_modalities=transcription``) carries a
``supported_parameters`` list, and it is the generic *chat* one - every STT row
returns ``temperature``, ``top_k``, ``top_p``, ``frequency_penalty`` and so on,
none of which the transcription endpoint accepts. Nothing under ``transcribe:``
may ever be driven from it. Transcription models are therefore checked for
existence and dates only.

Also known and not attempted: OpenRouter's rankings dataset, which is where the
summarize shortlist comes from, returns 401 unauthenticated. The shortlist
stays a periodic manual review, which is why new chat models are not reported
as candidates below.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import cast

from loreline.capability_config import CapabilityConfig, ModelSpec, ProviderSpec
from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import CatalogProbe, VendorModel
from loreline.staleness.deprecation import WARN_HORIZON_DAYS
from loreline.staleness.report import Code, Finding, Severity

# Provider and interaction pairs where "the vendor lists a model we do not
# curate" is a useful signal. It needs two things to be true at once: the
# catalogue must be scoped to the interaction (so the leftovers are candidates
# rather than every image and embedding model the vendor sells), and our
# curation must be trying to be complete.
#
# Excluded, each for its own reason:
#   openrouter/summarize - the shortlist is a manual review of the usage
#     rankings, whose dataset endpoint needs a key. 400+ chat models would be
#     reported as candidates every run.
#   openai/*            - one unscoped /models list mixing chat, image, audio
#     and embedding models, with no field that tells them apart.
#   gemini/transcribe   - same problem: /v1beta/models is every Gemini model.
_REPORT_CANDIDATES: frozenset[tuple[ProviderKind, Interaction]] = frozenset(
    {
        (ProviderKind.OPENROUTER, Interaction.TRANSCRIBE),
        (ProviderKind.OPENROUTER, Interaction.VIDEO),
        (ProviderKind.DEEPGRAM, Interaction.TRANSCRIBE),
    }
)

# A summary of the paragraph above, printed under the report so a reader of CI
# output knows what silence means.
NOT_CHECKED_NOTE = """\
Deliberately not checked against any catalogue: transcribe.realtime/batch,
inline_diarization, glossary.*, word_timestamps, languages, live_capture,
llm.system_prompt, the video prompt limits, the labels, and which models are
curated at all. No vendor publishes those, and the transcription catalogue's
supported_parameters is the generic chat list, so it must not drive any
transcribe fact. New chat models are not reported as candidates either: the
summarize shortlist comes from OpenRouter's rankings dataset, which needs a
key, and stays a periodic manual review."""


def compare(
    config: CapabilityConfig,
    probes: Sequence[CatalogProbe],
    *,
    today: date | None = None,
    horizon_days: int = WARN_HORIZON_DAYS,
) -> list[Finding]:
    """Findings from every probe that produced a usable answer.

    A probe that failed contributes nothing, by design: an unreachable vendor
    is silence, never a claim that its models disappeared.
    """
    now = today or date.today()
    horizon = now + timedelta(days=horizon_days)
    findings: list[Finding] = []
    for probe in probes:
        spec = config.provider(probe.kind)
        if spec is None or not probe.usable:
            continue
        findings.extend(_compare_probe(spec, probe, now=now, horizon=horizon))
    return findings


def _curated(spec: ProviderSpec, interaction: Interaction) -> list[ModelSpec]:
    """Curated entries for an interaction, hidden ones included.

    Hidden models are compared too - they are described in full and one day
    someone will unhide them, so drift in their facts matters - but every
    finding about one is downgraded, since nothing offers it today.
    """
    return [m for m in spec.models if interaction in m.interactions]


def _severity(model: ModelSpec, offered: Severity) -> Severity:
    return offered if not model.hidden else Severity.INFO


def _compare_probe(
    spec: ProviderSpec, probe: CatalogProbe, *, now: date, horizon: date
) -> list[Finding]:
    findings: list[Finding] = []
    curated = _curated(spec, probe.interaction)
    for model in curated:
        vendor = probe.find(model.id)
        if vendor is None:
            if not probe.partial:
                # A partial answer is one page of several, so absence from it
                # is not absence from the catalogue.
                findings.append(_retired(probe, model))
            continue
        findings.extend(_compare_dates(probe, model, vendor, now=now, horizon=horizon))
        findings.extend(_compare_facts(probe, model, vendor))
    findings.extend(_candidates(probe, curated))
    return findings


def _retired(probe: CatalogProbe, model: ModelSpec) -> Finding:
    return Finding(
        severity=_severity(model, Severity.ERROR),
        code=Code.MODEL_RETIRED,
        kind=probe.kind,
        interaction=probe.interaction,
        model=model.id,
        curated="offered" if not model.hidden else "hidden",
        vendor="absent",
        message="curated but the vendor no longer lists it",
    )


def _candidates(probe: CatalogProbe, curated: Iterable[ModelSpec]) -> list[Finding]:
    """Models the vendor lists that nobody has curated.

    Always INFO. An uncurated model is *unknown*, never unsupported, and the
    app already offers it when strict filtering is off, so this is a review
    queue and never a failure.
    """
    if (probe.kind, probe.interaction) not in _REPORT_CANDIDATES:
        return []
    known = {m.id for m in curated}
    return [
        Finding(
            severity=Severity.INFO,
            code=Code.MODEL_UNCURATED,
            kind=probe.kind,
            interaction=probe.interaction,
            model=vendor.id,
            vendor="listed",
            message="listed by the vendor and not curated here",
        )
        for vendor in probe.models
        if vendor.id not in known
    ]


def _compare_dates(
    probe: CatalogProbe, model: ModelSpec, vendor: VendorModel, *, now: date, horizon: date
) -> list[Finding]:
    """The vendor's published sunset against the yaml's ``deprecated``.

    Far-future dates are ignored rather than reported: OpenRouter carries
    2098-12-31 on models with no real sunset, and treating a sentinel as an
    announcement would file a finding on every one of them. A date inside the
    horizon, or already past, is the only kind worth a human's attention.
    """
    if vendor.retires_on is None:
        return []
    if model.deprecated:
        curated_date = _iso(model.deprecated)
        if curated_date == vendor.retires_on:
            return []
        return [
            Finding(
                severity=Severity.WARNING,
                code=Code.DEPRECATION_MISMATCH,
                kind=probe.kind,
                interaction=probe.interaction,
                model=model.id,
                fact="deprecated",
                curated=model.deprecated,
                vendor=vendor.retires_on.isoformat(),
                message=(
                    f"retirement date differs: yaml says {model.deprecated}, "
                    f"vendor says {vendor.retires_on.isoformat()}"
                ),
            )
        ]
    if vendor.retires_on > horizon:
        return []
    return [
        Finding(
            severity=_severity(model, Severity.WARNING),
            code=Code.DEPRECATION_UNRECORDED,
            kind=probe.kind,
            interaction=probe.interaction,
            model=model.id,
            fact="deprecated",
            curated="null",
            vendor=vendor.retires_on.isoformat(),
            message=(
                f"vendor publishes a retirement date ({vendor.retires_on.isoformat()}"
                f", {'passed' if vendor.retires_on <= now else 'upcoming'}) "
                "that the yaml does not record"
            ),
        )
    ]


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        # Reported separately by the offline date check; not this one's job.
        return None


def _compare_facts(probe: CatalogProbe, model: ModelSpec, vendor: VendorModel) -> list[Finding]:
    if probe.interaction is Interaction.SUMMARIZE:
        return _compare_llm(probe, model, vendor)
    if probe.interaction is Interaction.VIDEO:
        return _compare_video(probe, model, vendor)
    # Transcription: existence and dates only. See the trap in the module
    # docstring - the STT catalogue's supported_parameters is the chat list.
    return []


def _mismatch(
    probe: CatalogProbe,
    model: ModelSpec,
    fact: str,
    curated: object,
    vendor: object,
) -> Finding:
    return Finding(
        severity=_severity(model, Severity.WARNING),
        code=Code.FACT_MISMATCH,
        kind=probe.kind,
        interaction=probe.interaction,
        model=model.id,
        fact=fact,
        curated=_show(curated),
        vendor=_show(vendor),
        message=f"{fact}: yaml says {_show(curated)}, vendor publishes {_show(vendor)}",
    )


def _sort_key(value: object) -> tuple[int, float, str]:
    """Numbers numerically, everything else alphabetically.

    Sorting a duration list by its repr would print [10, 4, 5], which reads as
    a bug in the tool rather than as the drift it is reporting.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, str(value))


def _show(value: object) -> str:
    """A stable, readable rendering of one side of a mismatch."""
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(cast("Iterable[object]", value), key=_sort_key)
        return "[" + ", ".join(str(v) for v in items) + "]"
    return str(value)


def _compare_llm(probe: CatalogProbe, model: ModelSpec, vendor: VendorModel) -> list[Finding]:
    caps = model.llm
    if caps is None:
        return []
    findings: list[Finding] = []
    if vendor.context_length is not None and caps.context_length != vendor.context_length:
        findings.append(
            _mismatch(
                probe, model, "llm.context_length", caps.context_length, vendor.context_length
            )
        )
    if vendor.max_output_tokens is not None and caps.max_output_tokens != vendor.max_output_tokens:
        findings.append(
            _mismatch(
                probe,
                model,
                "llm.max_output_tokens",
                caps.max_output_tokens,
                vendor.max_output_tokens,
            )
        )
    if vendor.temperature is not None and caps.temperature != vendor.temperature:
        findings.append(
            _mismatch(probe, model, "llm.temperature", caps.temperature, vendor.temperature)
        )
    findings.extend(_compare_reasoning(probe, model, vendor))
    return findings


def _compare_reasoning(probe: CatalogProbe, model: ModelSpec, vendor: VendorModel) -> list[Finding]:
    """Reasoning support, effort vocabulary and the mandatory flag.

    Efforts are compared as sets: the vendor's order is its own and the yaml
    lists them however they read best, so an ordering difference is not drift.
    """
    caps = model.llm
    if caps is None or not vendor.publishes_reasoning:
        # A catalogue that says nothing about reasoning cannot contradict what
        # the yaml records: OpenAI's /models has no such field for any model.
        return []
    supported = vendor.reasoning is not None
    if caps.reasoning.supported != supported:
        return [
            _mismatch(probe, model, "llm.reasoning.supported", caps.reasoning.supported, supported)
        ]
    if vendor.reasoning is None:
        return []
    findings: list[Finding] = []
    if set(caps.reasoning.efforts) != set(vendor.reasoning.efforts):
        findings.append(
            _mismatch(
                probe,
                model,
                "llm.reasoning.efforts",
                caps.reasoning.efforts,
                vendor.reasoning.efforts,
            )
        )
    if caps.reasoning.mandatory != vendor.reasoning.mandatory:
        findings.append(
            _mismatch(
                probe,
                model,
                "llm.reasoning.mandatory",
                caps.reasoning.mandatory,
                vendor.reasoning.mandatory,
            )
        )
    return findings


def _compare_video(probe: CatalogProbe, model: ModelSpec, vendor: VendorModel) -> list[Finding]:
    caps = model.video
    published = vendor.video
    if caps is None or published is None:
        return []
    findings: list[Finding] = []
    lists: tuple[tuple[str, Sequence[object], Sequence[object] | None], ...] = (
        ("video.durations", caps.durations, published.durations),
        ("video.resolutions", caps.resolutions, published.resolutions),
        ("video.aspect_ratios", caps.aspect_ratios, published.aspect_ratios),
    )
    for fact, curated, vendor_values in lists:
        if vendor_values is None:
            continue
        if set(curated) != set(vendor_values):
            findings.append(_mismatch(probe, model, fact, curated, vendor_values))
    if published.audio is not None and caps.audio != published.audio:
        findings.append(_mismatch(probe, model, "video.audio", caps.audio, published.audio))
    if published.image_input is not None and caps.image_input != published.image_input:
        findings.append(
            _mismatch(probe, model, "video.image_input", caps.image_input, published.image_input)
        )
    return findings
