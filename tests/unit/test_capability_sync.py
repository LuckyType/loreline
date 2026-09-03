"""Regenerating capabilities.yaml without destroying it.

Every test here runs against the real shipped file, because the property under
test is a property of that file: it is a third comments, and those comments are
the vendor doc citations and the reasons values are deliberately conservative.
A synthetic fixture would prove the splice works on a document nobody has to
maintain, which is not the risk.

The load-bearing test is the first one. A regenerator that agrees with the
vendors must leave the file byte-identical, not merely equivalent: anything
that re-renders the document would pass an "it still parses to the same data"
assertion while having deleted every comment in it.

No network anywhere. :func:`plan` takes probes, so the vendor answers are built
by hand here, which also lets the drift be exactly one field.
"""

from __future__ import annotations

import yaml

from loreline.capabilities import config as shipped_config
from loreline.capability_config import CONFIG_PATH, CapabilityConfig
from loreline.models import Interaction, ProviderKind
from loreline.staleness.catalog import (
    CatalogProbe,
    CatalogStatus,
    VendorModel,
    VendorReasoning,
    VendorVideo,
)
from loreline.staleness.sync import (
    DERIVABLE_FACTS,
    SyncPlan,
    SyncRefusedError,
    plan,
    render,
    verify,
)
from loreline.staleness.yaml_edit import (
    ById,
    SpliceRefusedError,
    YamlDocument,
    render_scalar,
    render_sequence,
)

# The end-to-end drift case the read half reported against the live API: the
# yaml pins 32768 where OpenRouter published 182520.
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b"
NEMOTRON_PINNED = 32768
NEMOTRON_PUBLISHED = 182520

ENDPOINT = "https://openrouter.ai/api/v1/models"


def _source() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def _agreeing_models(config: CapabilityConfig, interaction: Interaction) -> list[VendorModel]:
    """A catalogue that says exactly what the curated file already says.

    Built from the file rather than written out, so the fixture cannot drift
    from it and so "no changes" means the derivable fields really do round
    trip, field by field, rather than that the test forgot to look at some.
    """
    spec = config.provider(ProviderKind.OPENROUTER)
    assert spec is not None
    models: list[VendorModel] = []
    for model in spec.models:
        if interaction not in model.interactions:
            continue
        if interaction is Interaction.SUMMARIZE and model.llm is not None:
            caps = model.llm
            reasoning = caps.reasoning
            models.append(
                VendorModel(
                    id=model.id,
                    context_length=caps.context_length,
                    max_output_tokens=caps.max_output_tokens,
                    temperature=caps.temperature,
                    reasoning=(
                        VendorReasoning(tuple(reasoning.efforts), reasoning.mandatory)
                        if reasoning.supported
                        else None
                    ),
                    publishes_reasoning=True,
                )
            )
        elif interaction is Interaction.VIDEO and model.video is not None:
            video = model.video
            models.append(
                VendorModel(
                    id=model.id,
                    video=VendorVideo(
                        durations=tuple(video.durations),
                        resolutions=tuple(video.resolutions),
                        aspect_ratios=tuple(video.aspect_ratios),
                        audio=video.audio,
                        image_input=video.image_input,
                    ),
                )
            )
        else:
            models.append(VendorModel(id=model.id))
    return models


def _probes(config: CapabilityConfig, *, replace: VendorModel | None = None) -> list[CatalogProbe]:
    """The three OpenRouter catalogues, agreeing, with one row optionally swapped."""
    probes: list[CatalogProbe] = []
    for interaction in (Interaction.SUMMARIZE, Interaction.TRANSCRIBE, Interaction.VIDEO):
        models = _agreeing_models(config, interaction)
        if replace is not None:
            models = [replace if m.id == replace.id else m for m in models]
        probes.append(
            CatalogProbe(
                ProviderKind.OPENROUTER,
                interaction,
                ENDPOINT,
                CatalogStatus.OK,
                f"{len(models)} models",
                tuple(models),
            )
        )
    return probes


def _nemotron(config: CapabilityConfig, *, max_output_tokens: int) -> VendorModel:
    agreeing = next(m for m in _agreeing_models(config, Interaction.SUMMARIZE) if m.id == NEMOTRON)
    assert agreeing.max_output_tokens == NEMOTRON_PINNED, (
        "the shipped yaml no longer pins the value this test is about"
    )
    return VendorModel(
        id=agreeing.id,
        context_length=agreeing.context_length,
        max_output_tokens=max_output_tokens,
        temperature=agreeing.temperature,
        reasoning=agreeing.reasoning,
        publishes_reasoning=True,
    )


def _comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


# --------------------------------------------------------------------------
# The property the whole design exists for
# --------------------------------------------------------------------------


def test_a_run_with_no_drift_leaves_the_file_byte_identical() -> None:
    """The strongest form of "comments are preserved" there is.

    A round-trip through a YAML loader and dumper would pass every structural
    assertion in this file and fail this one, having silently deleted ~450
    lines of vendor doc citations and reordered the rest.
    """
    config = shipped_config()
    source = _source()
    result = plan(config, _probes(config), source)
    assert result.changes == ()
    assert result.manual == ()
    assert result.updated == source
    assert not result.dirty


def test_the_nemotron_case_changes_that_one_value_and_nothing_else() -> None:
    config = shipped_config()
    source = _source()
    result = plan(
        config,
        _probes(config, replace=_nemotron(config, max_output_tokens=NEMOTRON_PUBLISHED)),
        source,
    )

    assert [c.fact for c in result.changes] == ["llm.max_output_tokens"]
    change = result.changes[0]
    assert change.model == NEMOTRON
    assert (change.before, change.after) == (str(NEMOTRON_PINNED), str(NEMOTRON_PUBLISHED))
    assert result.manual == ()

    before = source.splitlines()
    after = result.updated.splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1
    assert before[differing[0]].strip() == f"max_output_tokens: {NEMOTRON_PINNED}"
    assert after[differing[0]].strip() == f"max_output_tokens: {NEMOTRON_PUBLISHED}"


def test_the_nemotron_case_keeps_every_comment_in_the_file() -> None:
    config = shipped_config()
    source = _source()
    result = plan(
        config,
        _probes(config, replace=_nemotron(config, max_output_tokens=NEMOTRON_PUBLISHED)),
        source,
    )
    assert _comment_lines(result.updated) == _comment_lines(source)
    # Including the one sitting immediately above the value that moved, which
    # is exactly the kind a re-render loses.
    assert "# API also sets reasoning.supports_max_tokens=true" in result.updated


def test_the_rewritten_file_still_loads_and_validates() -> None:
    config = shipped_config()
    result = plan(
        config,
        _probes(config, replace=_nemotron(config, max_output_tokens=NEMOTRON_PUBLISHED)),
        _source(),
    )
    reloaded = verify(result)
    model = next(
        m for m in (reloaded.providers[ProviderKind.OPENROUTER].models) if m.id == NEMOTRON
    )
    assert model.llm is not None
    assert model.llm.max_output_tokens == NEMOTRON_PUBLISHED


# --------------------------------------------------------------------------
# Scope: what the planner refuses to touch
# --------------------------------------------------------------------------


def test_no_transcribe_field_is_derivable() -> None:
    """The trap the read half documents, restated as a test.

    The transcription catalogue publishes a supported_parameters list and it is
    the generic chat one, so every STT row advertises temperature, top_k and
    top_p. Nothing under transcribe may ever be written from a catalogue, and
    the fact allowlist is where that is enforced.
    """
    assert not any(fact.startswith("transcribe") for fact in DERIVABLE_FACTS)
    hand_annotated = {
        "llm.system_prompt",
        "video.prompt_max_chars",
        "video.prompt_max_tokens",
        "deprecated",
        "hidden",
        "default",
        "label",
    }
    assert DERIVABLE_FACTS & hand_annotated == set()


def test_a_model_the_vendor_no_longer_lists_is_not_removed() -> None:
    """Dropping a model is curation, which the checker reports and this does not do."""
    config = shipped_config()
    source = _source()
    probes = _probes(config)
    thinned = [
        CatalogProbe(
            p.kind,
            p.interaction,
            p.endpoint,
            p.status,
            p.detail,
            tuple(m for m in p.models if m.id != NEMOTRON),
        )
        for p in probes
    ]
    result = plan(config, thinned, source)
    assert result.updated == source
    assert NEMOTRON in result.updated


def test_a_model_the_vendor_lists_and_nobody_curated_is_not_added() -> None:
    config = shipped_config()
    source = _source()
    probes = _probes(config)
    fattened = [
        CatalogProbe(
            p.kind,
            p.interaction,
            p.endpoint,
            p.status,
            p.detail,
            (*p.models, VendorModel(id="acme/brand-new-model", context_length=42)),
        )
        for p in probes
    ]
    result = plan(config, fattened, source)
    assert result.updated == source
    assert "acme/brand-new-model" not in result.updated


def test_a_vendor_that_never_answered_changes_nothing() -> None:
    """Absence of evidence, never evidence of absence.

    An unreachable catalogue must not blank the fields it would otherwise have
    confirmed, which is the failure mode that would quietly empty half the
    file the first time OpenRouter had an outage.
    """
    config = shipped_config()
    source = _source()
    dead = [
        CatalogProbe(
            ProviderKind.OPENROUTER,
            interaction,
            ENDPOINT,
            CatalogStatus.UNREACHABLE,
            "could not check: ConnectError",
        )
        for interaction in (Interaction.SUMMARIZE, Interaction.TRANSCRIBE, Interaction.VIDEO)
    ]
    result = plan(config, dead, source)
    assert result.changes == ()
    assert result.updated == source
    assert "no catalogue answered, so nothing was derived" in render(result)


# --------------------------------------------------------------------------
# Reasoning, which is written as a unit
# --------------------------------------------------------------------------


def _reasoning_line(text: str, model_id: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- id: {model_id}")
    return next(line for line in lines[start : start + 12] if "reasoning:" in line).strip()


def test_a_new_effort_is_appended_and_the_curated_order_survives() -> None:
    """The lists are compared as sets, so the order is the file's to keep.

    Nemotron reads [high, medium], and the file's reasoning lists are hand
    ordered by strength. Rewriting them into the vendor's order would produce a
    diff on every model the day OpenRouter changes how it sorts its JSON.
    """
    config = shipped_config()
    source = _source()
    drifted = VendorModel(
        id=NEMOTRON,
        context_length=262144,
        max_output_tokens=NEMOTRON_PINNED,
        temperature=True,
        reasoning=VendorReasoning(efforts=("low", "medium", "high"), mandatory=False),
        publishes_reasoning=True,
    )
    result = plan(config, _probes(config, replace=drifted), source)
    assert [c.fact for c in result.changes] == ["llm.reasoning"]
    assert _reasoning_line(result.updated, NEMOTRON) == (
        "reasoning: {supported: true, efforts: [high, medium, low]}"
    )
    verify(result)


def test_a_model_that_starts_requiring_reasoning_gains_the_mandatory_key() -> None:
    """The key is not in the file for this model, so there is no line to patch.

    Writing the block as a unit is what makes that a rewrite rather than an
    insertion, and it is also what keeps `supported` and `efforts` consistent:
    patching `supported: false` onto a model with efforts listed would leave a
    file the schema rejects.
    """
    config = shipped_config()
    source = _source()
    assert "mandatory" not in _reasoning_line(source, NEMOTRON)
    drifted = VendorModel(
        id=NEMOTRON,
        context_length=262144,
        max_output_tokens=NEMOTRON_PINNED,
        temperature=True,
        reasoning=VendorReasoning(efforts=("high", "medium"), mandatory=True),
        publishes_reasoning=True,
    )
    result = plan(config, _probes(config, replace=drifted), source)
    assert [c.fact for c in result.changes] == ["llm.reasoning"]
    assert _reasoning_line(result.updated, NEMOTRON) == (
        "reasoning: {supported: true, mandatory: true, efforts: [high, medium]}"
    )
    verify(result)


def test_a_model_that_stops_reasoning_loses_its_efforts_in_the_same_edit() -> None:
    """One splice, or a file the loader refuses to read."""
    config = shipped_config()
    source = _source()
    drifted = VendorModel(
        id=NEMOTRON,
        context_length=262144,
        max_output_tokens=NEMOTRON_PINNED,
        temperature=True,
        reasoning=None,
        publishes_reasoning=True,
    )
    result = plan(config, _probes(config, replace=drifted), source)
    assert _reasoning_line(result.updated, NEMOTRON) == (
        "reasoning: {supported: false, efforts: []}"
    )
    verify(result)


# --------------------------------------------------------------------------
# Video, where the lists are quoted and the quoting has to survive
# --------------------------------------------------------------------------


def test_a_video_list_is_rewritten_in_the_quoting_the_file_uses() -> None:
    """`16:9` unquoted is a mapping, not a string, so this is correctness."""
    config = shipped_config()
    source = _source()
    spec = config.provider(ProviderKind.OPENROUTER)
    assert spec is not None
    model = next(m for m in spec.models if m.id == "runway/gen-4.5")
    assert model.video is not None
    drifted = VendorModel(
        id=model.id,
        video=VendorVideo(
            durations=(2, 3, 4, 5, 6, 7, 8, 9, 10, 12),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16", "1:1"),
            audio=model.video.audio,
            image_input=model.video.image_input,
        ),
    )
    result = plan(config, _probes(config, replace=drifted), source)
    facts = {c.fact: c.after for c in result.changes}
    assert facts["video.durations"] == "[2, 3, 4, 5, 6, 7, 8, 9, 10, 12]"
    assert facts["video.resolutions"] == '["720p", "1080p"]'
    assert facts["video.aspect_ratios"] == '["16:9", "9:16", "1:1"]'
    reloaded = verify(result)
    updated = next(
        m for m in reloaded.providers[ProviderKind.OPENROUTER].models if m.id == "runway/gen-4.5"
    )
    assert updated.video is not None
    assert updated.video.aspect_ratios == ["16:9", "9:16", "1:1"]


# --------------------------------------------------------------------------
# What it refuses, and says so about
# --------------------------------------------------------------------------


def test_a_key_the_file_does_not_carry_is_reported_rather_than_inserted() -> None:
    """Where to put a new line is a judgement about the comments around it.

    The file omits a key precisely where a comment above it explains the
    omission (grok-imagine-video-1.5 has no `audio` because generate_audio is
    null), so the script hands the value to a human instead of guessing at a
    position.
    """
    source = _source()
    trimmed = source.replace(f"          max_output_tokens: {NEMOTRON_PINNED}\n", "", 1)
    config = CapabilityConfig.model_validate(yaml.safe_load(trimmed))
    dropped = next(m for m in config.providers[ProviderKind.OPENROUTER].models if m.id == NEMOTRON)
    assert dropped.llm is not None and dropped.llm.max_output_tokens is None, (
        "the shipped yaml no longer pins this value uniquely; the test edit missed"
    )
    result = plan(
        config,
        _probes(config, replace=_nemotron_without_pin(config)),
        trimmed,
    )
    assert result.changes == ()
    assert [m.fact for m in result.manual] == ["llm.max_output_tokens"]
    assert result.manual[0].wanted == str(NEMOTRON_PUBLISHED)
    assert "does not carry this key" in result.manual[0].reason
    assert result.updated == trimmed


def _nemotron_without_pin(config: CapabilityConfig) -> VendorModel:
    agreeing = next(m for m in _agreeing_models(config, Interaction.SUMMARIZE) if m.id == NEMOTRON)
    return VendorModel(
        id=agreeing.id,
        context_length=agreeing.context_length,
        max_output_tokens=NEMOTRON_PUBLISHED,
        temperature=agreeing.temperature,
        reasoning=agreeing.reasoning,
        publishes_reasoning=True,
    )


def test_a_block_written_value_with_comments_inside_is_refused() -> None:
    """The Gemini reasoning blocks are written this way, with the 400 response
    that justifies each effort quoted inside them. Re-rendering the block would
    delete those, so the splice refuses and reports instead."""
    source = _source()
    block = (
        "          reasoning:\n"
        "            supported: true\n"
        "            # 400: rejects effort none outright.\n"
        "            efforts: [high, medium]\n"
    )
    rewritten = source.replace(
        "          reasoning: {supported: true, efforts: [high, medium]}\n", block, 1
    )
    assert rewritten != source
    config = CapabilityConfig.model_validate(yaml.safe_load(rewritten))
    drifted = VendorModel(
        id=NEMOTRON,
        context_length=262144,
        max_output_tokens=NEMOTRON_PINNED,
        temperature=True,
        reasoning=VendorReasoning(efforts=("high", "medium", "low"), mandatory=False),
        publishes_reasoning=True,
    )
    result = plan(config, _probes(config, replace=drifted), rewritten)
    assert result.changes == ()
    assert [m.fact for m in result.manual] == ["llm.reasoning"]
    assert "block" in result.manual[0].reason
    assert result.updated == rewritten
    assert "# 400: rejects effort none outright." in result.updated


def test_verify_refuses_a_result_that_does_not_parse_back() -> None:
    """The guard against a rendering bug, not against a vendor.

    A value spliced in as text has to come back out as the value that was
    meant; an aspect ratio that lost its quotes would parse as a mapping and
    empty a picker months later.
    """
    bad = SyncPlan(original="version: 1\n", updated="version: 1\nproviders: {}\n")
    try:
        verify(bad)
    except SyncRefusedError as exc:
        assert "line count" in str(exc)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("a line the splice did not make should be refused")


# --------------------------------------------------------------------------
# The editor itself, on documents small enough to read
# --------------------------------------------------------------------------


def test_a_trailing_comment_on_the_edited_line_survives() -> None:
    text = "a:\n  b: 32768   # models.list outputTokenLimit\n"
    document = YamlDocument(text)
    located = document.locate(("a", "b"))
    assert located is not None
    document.replace(located, "182520")
    assert document.text == "a:\n  b: 182520   # models.list outputTokenLimit\n"


def test_an_untouched_document_is_returned_unchanged() -> None:
    text = "# leading note\na:\n  b: 1\n"
    document = YamlDocument(text)
    assert document.text is text
    assert not document.edited


def test_a_model_is_addressed_by_id_not_by_position() -> None:
    text = "models:\n  - id: second\n    n: 1\n  - id: first\n    n: 2\n"
    document = YamlDocument(text)
    located = document.locate(("models", ById("first"), "n"))
    assert located is not None
    document.replace(located, "9")
    assert document.text == "models:\n  - id: second\n    n: 1\n  - id: first\n    n: 9\n"


def test_a_block_sequence_is_refused() -> None:
    """PyYAML's end mark for one runs past the last item into the following
    whitespace, so the span is not the value and splicing it would eat a line."""
    document = YamlDocument("a:\n  - 1\n  - 2\nb: 3\n")
    located = document.locate(("a",))
    assert located is not None
    try:
        document.replace(located, "[1, 2, 3]")
    except SpliceRefusedError as exc:
        assert "block" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a block sequence should be refused")


def test_scalars_render_the_way_the_file_writes_them() -> None:
    assert render_scalar(True) == "true"
    assert render_scalar(False) == "false"
    assert render_scalar(182520) == "182520"
    assert render_scalar("high") == "high"
    # A colon makes a bare scalar a mapping, so it has to be quoted.
    assert render_scalar("16:9") == '"16:9"'


def test_sequences_copy_the_quoting_of_the_one_they_replace() -> None:
    quoted = YamlDocument('a: ["720p", "1080p"]\n').locate(("a",))
    bare = YamlDocument("a: [high, low]\n").locate(("a",))
    assert quoted is not None and bare is not None
    assert render_sequence(["480p", "720p"], like=quoted.node) == '["480p", "720p"]'
    assert render_sequence(["high", "medium"], like=bare.node) == "[high, medium]"
