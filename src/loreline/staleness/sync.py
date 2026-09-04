"""Regenerating the machine-derivable fields of capabilities.yaml.

The write half of the staleness feature, and the deliberate mirror of the read
half: :mod:`loreline.staleness.check` reports where the curated file and the
vendors disagree, and this module resolves the subset of those disagreements
where the vendor is authoritative, by rewriting the file.

It reuses both earlier halves rather than restating them.
:mod:`loreline.catalog` does the fetching, which is why there is no
second HTTP client here, and :mod:`loreline.staleness.compare` decides what
counts as drift, which is why there is no second definition of "differs" here
either. This module reads the *typed* vendor value behind each mismatch
finding, works out where in the source that value is written, and splices.
Keeping the drift rule in one place is what stops the checker and the sync
script from ever disagreeing about whether a field is stale.

WHAT IS WRITTEN
===============

Exactly the fields a vendor catalogue genuinely publishes, and only for models
that are already curated:

* ``llm.context_length``, ``llm.max_output_tokens``, ``llm.temperature`` and
  the ``llm.reasoning`` block, from OpenRouter's ``GET /api/v1/models``.
* ``video.durations``, ``video.resolutions``, ``video.aspect_ratios``,
  ``video.audio`` and ``video.image_input``, from ``GET /api/v1/videos/models``.

WHAT IS NEVER WRITTEN, AND WHY THE LIST MATTERS MORE THAN THE ONE ABOVE
=======================================================================

Everything under ``transcribe:``, all of ``glossary.*``, ``word_timestamps``,
``inline_diarization``, ``languages``, ``live_capture``, ``system_prompt``, the
video prompt limits, the ``label``s, ``hidden``, ``deprecated``, the
``default: true`` markers, and which models are listed at all. Those are hand
annotations: the vendor catalogues either do not carry them, or carry something
that looks like them and is not.

The specific trap, verified on 2026-09-02 and restated here because this is the
module that could act on it: the transcription catalogue
(``/api/v1/models?output_modalities=transcription``) carries a
``supported_parameters`` list and it is the generic *chat* one. Every STT row
advertises ``temperature``, ``top_k``, ``top_p`` and friends, none of which the
transcription endpoint accepts. No ``transcribe:`` fact may ever be derived
from it, which is why the fact allowlist below names no transcribe field and
why :func:`_yaml_path` cannot address one.

Adding or removing a model is likewise a human decision. The vendor listing a
model nobody curated is a review queue, not an instruction, and the read half
already reports it.

THREE SAFETY PROPERTIES
=======================

* Dry run by default. Nothing is written until the caller asks for it, so a
  first run cannot rewrite anyone's file.
* Fail soft. A vendor that did not answer contributes no findings, therefore no
  changes; a fetch failure can never be read as "the vendor stopped publishing
  this" and blank a field. A run where no catalogue answered at all writes
  nothing even with the write flag set.
* Comment preserving, provably. Edits are byte-span splices over the original
  text (see :mod:`loreline.staleness.yaml_edit`), so a run with no drift
  returns the input unchanged rather than a re-render that happens to agree.
  Anything that cannot be spliced safely is reported for a human instead of
  guessed at, and the result is schema-validated before it reaches disk.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path as FsPath
from typing import cast

import yaml

from loreline import capabilities
from loreline.capability_config import CONFIG_PATH, CapabilityConfig, ModelSpec
from loreline.catalog import (
    DEFAULT_TIMEOUT_S,
    CatalogProbe,
    ClientFactory,
    VendorModel,
)
from loreline.models import Interaction, ProviderKind
from loreline.staleness.check import gather_probes
from loreline.staleness.compare import compare
from loreline.staleness.report import Code, Finding, probe_lines
from loreline.staleness.yaml_edit import (
    ById,
    Path,
    SpliceRefusedError,
    YamlDocument,
    render_mapping,
    render_scalar,
    render_sequence,
    show_path,
)

# A short version of the paragraph above, printed under a sync run so whoever
# reads the output knows what the script left alone rather than found clean.
NEVER_WRITTEN_NOTE = """\
Never written by this command, whatever a vendor says: everything under
transcribe (the STT catalogue's supported_parameters is the generic chat list
and must not drive a transcribe fact), glossary.*, word_timestamps,
inline_diarization, languages, live_capture, llm.system_prompt, the video
prompt limits, the labels, hidden, deprecated, the default markers, and which
models are curated at all. Those are hand annotations, and adding or dropping a
model is a curation decision - run check-capabilities for those."""

# Scalar facts, mapped to the value the vendor published for them. A fact
# missing from here (or from _LIST_FACTS / _REASONING) is one this command
# will not write, whatever the checker reports about it.
_SCALAR_FACTS: dict[str, Callable[[VendorModel], object | None]] = {
    "llm.context_length": lambda v: v.context_length,
    "llm.max_output_tokens": lambda v: v.max_output_tokens,
    "llm.temperature": lambda v: v.temperature,
    "video.audio": lambda v: v.video.audio if v.video else None,
    "video.image_input": lambda v: v.video.image_input if v.video else None,
}

_LIST_FACTS: dict[str, Callable[[VendorModel], Sequence[object] | None]] = {
    "video.durations": lambda v: v.video.durations if v.video else None,
    "video.resolutions": lambda v: v.video.resolutions if v.video else None,
    "video.aspect_ratios": lambda v: v.video.aspect_ratios if v.video else None,
}

# Reasoning is written as one unit rather than leaf by leaf, because its leaves
# constrain each other: the schema rejects a non-empty `efforts` beside
# `supported: false`, and `mandatory: true` beside an effort of "none". Writing
# `supported` alone could therefore leave the file invalid, and the file's
# convention of omitting `mandatory` when it is false means the key to write
# often does not exist as a line to replace. Both problems disappear when the
# whole inline mapping is re-rendered from the vendor's statement.
_REASONING_FACT = "llm.reasoning"
_REASONING_PREFIX = f"{_REASONING_FACT}."

DERIVABLE_FACTS: frozenset[str] = frozenset({*_SCALAR_FACTS, *_LIST_FACTS, _REASONING_FACT})


@dataclass(frozen=True, slots=True)
class Change:
    """One value this run would rewrite, with both sides as they read."""

    kind: ProviderKind
    interaction: Interaction
    model: str
    fact: str
    path: Path
    before: str
    after: str

    def line(self) -> str:
        return (
            f"{self.kind.value}/{self.interaction.value} {self.model} "
            f"{self.fact}: {self.before} -> {self.after}"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.kind.value,
            "interaction": self.interaction.value,
            "model": self.model,
            "fact": self.fact,
            "path": show_path(self.path),
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class Manual:
    """A drifted field this run refuses to write, and the reason.

    Always reported, never skipped silently. A field the script cannot reach
    surgically is still drift somebody has to resolve, and a sync run that
    quietly did nothing about it would be worse than one that never claimed to
    handle it.
    """

    kind: ProviderKind
    interaction: Interaction
    model: str
    fact: str
    wanted: str
    reason: str

    def line(self) -> str:
        return (
            f"{self.kind.value}/{self.interaction.value} {self.model} "
            f"{self.fact} -> {self.wanted}: {self.reason}"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.kind.value,
            "interaction": self.interaction.value,
            "model": self.model,
            "fact": self.fact,
            "wanted": self.wanted,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What a run would do to the file, without having done it."""

    original: str
    updated: str
    changes: tuple[Change, ...] = ()
    manual: tuple[Manual, ...] = ()
    probes: tuple[CatalogProbe, ...] = ()

    @property
    def dirty(self) -> bool:
        return self.updated != self.original

    @property
    def answered(self) -> tuple[CatalogProbe, ...]:
        return tuple(p for p in self.probes if p.usable)

    def diff(self, name: str = "capabilities.yaml") -> str:
        """A unified diff of exactly what would change."""
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.updated.splitlines(keepends=True),
                fromfile=name,
                tofile=f"{name} (synced)",
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "changes": [c.as_dict() for c in self.changes],
            "manual": [m.as_dict() for m in self.manual],
            "probes": [
                {
                    "provider": p.kind.value,
                    "interaction": p.interaction.value,
                    "status": p.status.value,
                    "detail": p.detail,
                }
                for p in self.probes
            ],
            "diff": self.diff(),
        }


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def _yaml_path(kind: ProviderKind, model_id: str, fact: str) -> Path:
    """Where in the source a fact for one curated model is written.

    The model is addressed by its id rather than by its position in the list,
    so reordering the curated models cannot make this write onto the wrong one.
    """
    return ("providers", kind.value, "models", ById(model_id), *fact.split("."))


def _curated_model(config: CapabilityConfig, kind: ProviderKind, model_id: str) -> ModelSpec | None:
    spec = config.provider(kind)
    if spec is None:
        return None
    return next((m for m in spec.models if m.id == model_id), None)


def _order_like_the_file(curated: Sequence[object], vendor: Sequence[object]) -> list[object]:
    """The vendor's values, arranged the way the file already arranges them.

    The comparison treats these lists as sets, so a rewrite only happens when
    the membership changed; the order is then ours to choose and the smallest
    honest diff is the one that keeps what is already there. Numbers are the
    exception: every numeric list in the file is ascending, and durations read
    as a bug otherwise.
    """
    if vendor and all(isinstance(v, int) and not isinstance(v, bool) for v in vendor):
        return sorted(vendor, key=lambda v: int(str(v)))
    kept = [c for c in curated if c in vendor]
    return [*kept, *(v for v in vendor if v not in kept)]


def _reasoning_pairs(model: ModelSpec, vendor: VendorModel) -> list[tuple[str, object]]:
    """The vendor's whole reasoning statement, in the file's own key order.

    ``mandatory`` is emitted only when true, matching the file, where the key
    is present on exactly the models that require reasoning and absent
    elsewhere rather than spelled out as false everywhere.
    """
    published = vendor.reasoning
    curated = model.llm.reasoning if model.llm else None
    pairs: list[tuple[str, object]] = [("supported", published is not None)]
    if published is not None and published.mandatory:
        pairs.append(("mandatory", True))
    efforts = _order_like_the_file(
        curated.efforts if curated else (), published.efforts if published else ()
    )
    pairs.append(("efforts", efforts))
    return pairs


def _mismatches(config: CapabilityConfig, probes: Sequence[CatalogProbe]) -> list[Finding]:
    """Fact-level drift, as the read half already defines it.

    Findings about a retired model, an uncurated one or a deprecation date are
    dropped here: every one of them is a curation decision, and none is
    something this command may act on.
    """
    return [f for f in compare(config, probes) if f.code is Code.FACT_MISMATCH]


def _vendor_for(
    probes: Sequence[CatalogProbe], finding: Finding
) -> tuple[CatalogProbe, VendorModel] | None:
    for probe in probes:
        if probe.kind is not finding.kind or probe.interaction is not finding.interaction:
            continue
        if not probe.usable or finding.model is None:
            continue
        vendor = probe.find(finding.model)
        if vendor is not None:
            return probe, vendor
    return None


def plan(
    config: CapabilityConfig,
    probes: Sequence[CatalogProbe],
    source: str,
) -> SyncPlan:
    """Work out every edit, apply none of them to disk.

    The returned plan carries the rewritten text so the caller can diff it,
    validate it and decide whether to write it, all without a file having been
    touched.
    """
    document = YamlDocument(source)
    changes: list[Change] = []
    manual: list[Manual] = []
    # Reasoning drift arrives as up to three separate findings (supported,
    # efforts, mandatory) for one model, and is written once, so the models
    # already handled are remembered rather than re-edited.
    reasoned: set[tuple[ProviderKind, str]] = set()

    for finding in _mismatches(config, probes):
        fact = finding.fact or ""
        if fact.startswith(_REASONING_PREFIX):
            fact = _REASONING_FACT
        if fact not in DERIVABLE_FACTS or finding.model is None:
            # A fact the checker compares and this command does not own. There
            # are none today, and if one is added to the checker tomorrow it
            # must stay a human's decision until it is added here too.
            manual.append(_unowned(finding))
            continue
        found = _vendor_for(probes, finding)
        model = _curated_model(config, finding.kind, finding.model)
        if found is None or model is None:  # pragma: no cover - compare found both
            continue
        if fact == _REASONING_FACT:
            key = (finding.kind, finding.model)
            if key in reasoned:
                continue
            reasoned.add(key)
        outcome = _stage(document, finding, fact, model, found[1])
        if isinstance(outcome, Change):
            changes.append(outcome)
        else:
            manual.append(outcome)

    return SyncPlan(
        original=source,
        updated=document.text,
        changes=tuple(changes),
        manual=tuple(manual),
        probes=tuple(probes),
    )


def _unowned(finding: Finding) -> Manual:
    return Manual(
        kind=finding.kind,
        interaction=finding.interaction or Interaction.SUMMARIZE,
        model=finding.model or "?",
        fact=finding.fact or "?",
        wanted=finding.vendor or "?",
        reason="hand-annotated field, this command does not derive it",
    )


def _stage(
    document: YamlDocument,
    finding: Finding,
    fact: str,
    model: ModelSpec,
    vendor: VendorModel,
) -> Change | Manual:
    """Render one fact and stage the splice, or explain why it cannot be."""
    path = _yaml_path(finding.kind, model.id, fact)
    located = document.locate(path)
    if fact == _REASONING_FACT:
        rendered = render_mapping(_reasoning_pairs(model, vendor))
    elif fact in _LIST_FACTS:
        values = _LIST_FACTS[fact](vendor)
        if values is None:  # pragma: no cover - compare skips an unpublished list
            return _unowned(finding)
        curated_list = _curated_sequence(model, fact)
        rendered = render_sequence(
            _order_like_the_file(curated_list, values),
            like=located.node if located else None,
        )
    else:
        value = _SCALAR_FACTS[fact](vendor)
        if value is None:  # pragma: no cover - compare skips an unpublished scalar
            return _unowned(finding)
        rendered = render_scalar(value)

    if located is None:
        # No line to replace. Inserting one is a judgement about the comments
        # around it (the file omits a key precisely where a comment explains
        # the omission), so it goes to a human with the value spelled out.
        return _manual(finding, fact, rendered, "the file does not carry this key; add it by hand")
    try:
        document.replace(located, rendered)
    except SpliceRefusedError as exc:
        return _manual(finding, fact, rendered, str(exc))
    return Change(
        kind=finding.kind,
        interaction=finding.interaction or Interaction.SUMMARIZE,
        model=model.id,
        fact=fact,
        path=path,
        before=located.source,
        after=rendered,
    )


def _curated_sequence(model: ModelSpec, fact: str) -> Sequence[object]:
    video = model.video
    if video is None:  # pragma: no cover - compare only reaches this with a block
        return ()
    return {
        "video.durations": video.durations,
        "video.resolutions": video.resolutions,
        "video.aspect_ratios": video.aspect_ratios,
    }.get(fact, [])


def _manual(finding: Finding, fact: str, rendered: str, reason: str) -> Manual:
    return Manual(
        kind=finding.kind,
        interaction=finding.interaction or Interaction.SUMMARIZE,
        model=finding.model or "?",
        fact=fact,
        wanted=rendered,
        reason=reason,
    )


async def run_sync(
    *,
    config: CapabilityConfig | None = None,
    source_path: FsPath | None = None,
    request_timeout: float = DEFAULT_TIMEOUT_S,
    credentials: Mapping[ProviderKind, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> SyncPlan:
    """Ask every vendor, then plan the edits. Writes nothing.

    The fetching is :func:`loreline.staleness.check.gather_probes` unchanged,
    which is the whole reason the read half separated "what does the vendor
    say" from "how does that compare": one client, one set of endpoints, one
    fail-soft policy, shared by the checker and by this.
    """
    cfg = config or capabilities.config()
    path = source_path or CONFIG_PATH
    probes = await gather_probes(
        cfg,
        request_timeout=request_timeout,
        credentials=credentials,
        client_factory=client_factory,
    )
    return plan(cfg, probes, path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


class SyncRefusedError(Exception):
    """The rewritten file did not pass its own checks, so nothing was written."""


def verify(plan: SyncPlan) -> CapabilityConfig:
    """Prove the rewritten text is still the file it claims to be.

    Three questions, each guarding a different way a splice could go wrong:

    * does every value we rewrote parse back as the value we meant? A rendering
      bug (an unquoted ``16:9`` turning into a mapping) is caught here rather
      than by a picker offering nothing months later;
    * is the line count unchanged? Splicing a value can never add or remove a
      line, so a difference means a span covered more than its value;
    * does the whole document still satisfy the schema? Exactly one default per
      offered interaction, no default with a sunset date, no reasoning efforts
      beside ``supported: false``.
    """
    raw: object = yaml.safe_load(plan.updated)
    if not isinstance(raw, dict):
        raise SyncRefusedError("the rewritten file is not a YAML mapping")
    document = cast("dict[str, object]", raw)
    for change in plan.changes:
        actual = _at(document, change.path)
        expected = yaml.safe_load(change.after)
        if _comparable(actual) != _comparable(expected):
            raise SyncRefusedError(
                f"{show_path(change.path)} was rewritten as {change.after!r} "
                f"but parses back as {actual!r}"
            )
    before, after = plan.original.count("\n"), plan.updated.count("\n")
    if before != after:
        raise SyncRefusedError(f"line count changed from {before} to {after}; a splice overreached")
    try:
        return CapabilityConfig.model_validate(document)
    except Exception as exc:
        raise SyncRefusedError(f"the rewritten file no longer validates: {exc}") from exc


def _at(document: object, path: Path) -> object:
    """The value the reparsed file carries at one path, or None."""
    node: object = document
    for step in path:
        if isinstance(step, ById):
            if not isinstance(node, list):
                return None
            node = _entry_by_id(cast("list[object]", node), step.value)
            continue
        if not isinstance(node, dict):
            return None
        node = cast("dict[str, object]", node).get(step)
    return node


def _entry_by_id(entries: Sequence[object], model_id: str) -> object:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mapping = cast("dict[str, object]", entry)
        if mapping.get("id") == model_id:
            return mapping
    return None


def _comparable(value: object) -> object:
    """Lists compare as multisets: the file's order is ours to choose."""
    if isinstance(value, list):
        return sorted(str(v) for v in cast("list[object]", value))
    return value


def write(plan: SyncPlan, path: FsPath | None = None) -> CapabilityConfig:
    """Verify the rewritten file, then replace the real one.

    Verification happens before the write, not after, so a rejected result
    leaves the original on disk untouched rather than needing a rollback.
    """
    config = verify(plan)
    (path or CONFIG_PATH).write_text(plan.updated, encoding="utf-8")
    return config


# --------------------------------------------------------------------------
# Rendering the run
# --------------------------------------------------------------------------


def render(plan: SyncPlan, *, name: str = "capabilities.yaml") -> str:
    """The dry-run report: what would change, what would not, and what answered.

    The probe list is printed on every run, empty of findings or not, for the
    same reason the checker prints it: "no changes" means something completely
    different when every catalogue answered than when none did.
    """
    lines: list[str] = []
    if plan.changes:
        lines.append(f"== would rewrite ({len(plan.changes)})")
        lines.extend(f"   {c.line()}" for c in plan.changes)
        lines.append("")
    if plan.manual:
        lines.append(f"== needs a hand edit ({len(plan.manual)})")
        lines.extend(f"   {m.line()}" for m in plan.manual)
        lines.append("")
    if plan.dirty:
        lines.append(plan.diff(name).rstrip("\n"))
        lines.append("")
    lines.append(f"== checked ({len(plan.answered)} of {len(plan.probes)} catalogues)")
    lines.extend(probe_lines(plan.probes))
    if not plan.changes and not plan.manual:
        lines.append("")
        lines.append(
            "no catalogue answered, so nothing was derived"
            if not plan.answered
            else "every derivable field already matches what the vendors publish"
        )
    return "\n".join(lines)


__all__ = [
    "DERIVABLE_FACTS",
    "NEVER_WRITTEN_NOTE",
    "Change",
    "Manual",
    "SyncPlan",
    "SyncRefusedError",
    "plan",
    "render",
    "run_sync",
    "verify",
    "write",
]
