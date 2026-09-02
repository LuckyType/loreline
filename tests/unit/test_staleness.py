"""The comparison half of the staleness checks, with no network anywhere.

Two things are pinned here. The first is the arithmetic: what counts as a
retired model, a passed sunset date, a fact the vendor now contradicts. The
second, and the one worth more, is everything the check must NOT say. It may
not report drift from a vendor that never answered, from a page of a paginated
catalogue it never fetched, or from a field the vendor does not publish. A
staleness check that cries wolf is worse than none, because the next real
finding gets ignored along with the rest.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from loreline.capabilities import config as shipped_config
from loreline.capability_config import CapabilityConfig
from loreline.models import Interaction, ProviderKind
from loreline.staleness import FailOn, Severity, should_fail
from loreline.staleness.catalog import (
    CatalogProbe,
    CatalogStatus,
    VendorModel,
    VendorReasoning,
    VendorVideo,
)
from loreline.staleness.compare import compare
from loreline.staleness.deprecation import check_dates
from loreline.staleness.report import Code, Finding, StalenessReport, order, render

TODAY = date(2026, 9, 2)
ENDPOINT = "https://example.invalid/models"


# The undated, offered model every synthetic provider needs: the schema wants
# exactly one default per offered interaction and refuses a dated one.
CURRENT_MODEL: dict[str, object] = {
    "id": "some-model",
    "interactions": ["transcribe"],
    "default": True,
    "transcribe": {"batch": True},
}


def _provider(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "label": "Test",
        "key_url": "https://example.invalid/keys",
        "interactions": ["transcribe"],
        "models": [dict(CURRENT_MODEL)],
    }
    spec.update(overrides)
    return spec


def _config(providers: dict[str, object] | None = None) -> CapabilityConfig:
    """A config covering every ProviderKind, since the loader insists on that."""
    full: dict[str, object] = {kind.value: _provider() for kind in ProviderKind}
    full.update(providers or {})
    return CapabilityConfig.model_validate({"version": 1, "providers": full})


def _probe(
    *models: VendorModel,
    interaction: Interaction = Interaction.SUMMARIZE,
    kind: ProviderKind = ProviderKind.OPENROUTER,
    status: CatalogStatus = CatalogStatus.OK,
    partial: bool = False,
) -> CatalogProbe:
    return CatalogProbe(kind, interaction, ENDPOINT, status, "test", tuple(models), partial=partial)


# --------------------------------------------------------------------------
# Sunset dates: the half that needs no network, cannot be wrong about a vendor,
# and therefore is the one that gates CI. Every case here is driven by a fixed
# reference date and a synthetic config: a test whose result changes tomorrow,
# or the day somebody retires a real model, is worse than no test.
# --------------------------------------------------------------------------


def _dated_config(deprecated: str | None, *, hidden: bool = False) -> CapabilityConfig:
    """A provider with one dated model beside a current default.

    The pairing is forced by the schema, which refuses a default that is hidden
    or retiring: adding a sunset date to the marked model is exactly the moment
    someone has to choose the next one.
    """
    dated: dict[str, object] = {
        "id": "old-model",
        "interactions": ["transcribe"],
        "transcribe": {"batch": True},
        "deprecated": deprecated,
        "hidden": hidden,
    }
    return _config({"openai": _provider(models=[dated, dict(CURRENT_MODEL)])})


def _dated_findings(config: CapabilityConfig) -> list[Finding]:
    return [f for f in check_dates(config, today=TODAY) if f.model == "old-model"]


def _in_days(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("days_away", "severity"),
    [
        # Already gone, and still being offered: the worst case there is.
        (-60, Severity.ERROR),
        (-1, Severity.ERROR),
        # The day itself. Still offered, so still a failure.
        (0, Severity.ERROR),
        (1, Severity.ERROR),
        # Last day inside the failure band: "less than 7" fails.
        (6, Severity.ERROR),
        # Exactly seven is not less than seven, so it warns instead.
        (7, Severity.WARNING),
        (29, Severity.WARNING),
        # Exactly thirty is not less than thirty: nothing at all.
        (30, None),
        (177, None),
    ],
)
def test_the_two_thresholds_decide_the_severity(days_away: int, severity: Severity | None) -> None:
    """The gap between the bands is the feature.

    Under 30 days a model still works for another month, so it warns and CI
    stays green. Under 7 days it is about to stop answering, and a red build is
    what forces somebody to choose the successor at the last responsible
    moment rather than a player discovering it mid-session.
    """
    findings = _dated_findings(_dated_config(_in_days(days_away)))
    assert [f.severity for f in findings] == ([severity] if severity else [])


def test_a_model_with_no_date_at_all_is_never_reported() -> None:
    assert _dated_findings(_dated_config(None)) == []


def test_a_config_with_nothing_near_retirement_is_clean() -> None:
    """The state the file is meant to be in most of the time. It has to be a
    silent pass, not an awkward empty report or a spurious failure."""
    report = StalenessReport(tuple(check_dates(_config(), today=TODAY)))
    assert report.findings == ()
    assert should_fail(report, FailOn.ERROR) is False
    assert "no recorded sunset date is due or imminent" in render(report)


def test_a_failing_finding_says_everything_a_red_build_needs() -> None:
    """Nobody reading a broken pipeline at 9am should have to open the yaml to
    find out what to do: the line names the provider, the model, the date, the
    days left, that it is still offered, and the fix."""
    line = _dated_findings(_dated_config(_in_days(3)))[0].line()
    assert line.startswith("ERROR")
    assert "openai old-model" in line
    assert f"retires in 3 days ({_in_days(3)})" in line
    assert "still offered" in line
    assert "fails the build" in line


def test_a_past_due_model_counts_the_days_since() -> None:
    message = _dated_findings(_dated_config(_in_days(-12)))[0].message
    assert message.startswith(f"retired 12 days ago ({_in_days(-12)})")


def test_the_day_itself_reads_as_today() -> None:
    assert "retires today" in _dated_findings(_dated_config(_in_days(0)))[0].message


@pytest.mark.parametrize("days_away", [-60, 0, 3, 29])
def test_a_hidden_model_never_fails_the_build(days_away: int) -> None:
    """Decided deliberately: a hidden entry is described but offered by no
    picker, so its retirement cannot break anyone's session, and failing a
    build over one would force churn with no user visible upside. It is still
    reported, because a retired hidden model is dead weight to delete."""
    findings = _dated_findings(_dated_config(_in_days(days_away), hidden=True))
    assert [f.severity for f in findings] == [Severity.INFO]
    assert "hidden" in findings[0].message


def test_a_date_nobody_can_parse_is_an_error() -> None:
    """The schema takes this field as a free string, so a typo lands here
    rather than at load time, and a date nothing can read is a date nobody will
    ever act on."""
    findings = _dated_findings(_dated_config("soon"))
    assert [f.code for f in findings] == [Code.DEPRECATION_UNPARSEABLE]
    assert findings[0].severity is Severity.ERROR


def test_the_dates_do_not_depend_on_the_clock() -> None:
    """Same file plus same date gives the same answer. This check gates CI, so
    it must be exact rather than fail soft: fail soft is for a vendor that
    might be down, and there is no vendor here."""
    config = _dated_config(_in_days(3))
    assert check_dates(config, today=TODAY) == check_dates(config, today=TODAY)
    assert check_dates(config, today=TODAY + timedelta(days=60)) != check_dates(config, today=TODAY)


def test_the_shipped_file_records_only_parseable_dates() -> None:
    """The one guard here that reads the real file, and deliberately the only
    thing it asserts.

    Whether a date has *passed* is the CI check's question, answered against
    the day it runs; a unit test that decided it would start failing on a day
    nobody touched the repo. That a recorded date is a date at all holds
    forever, and holds just as well when no model carries one.
    """
    for kind, spec in shipped_config().providers.items():
        for model in spec.models:
            if model.deprecated:
                assert date.fromisoformat(model.deprecated), f"{kind.value}/{model.id}"


# --------------------------------------------------------------------------
# The cliff that cannot happen
# --------------------------------------------------------------------------


def test_a_provider_cannot_offer_a_fully_dated_interaction() -> None:
    """Why there is no "this kind will have nothing left" finding.

    Several models retiring on the same day looks like it needs its own report,
    but the schema has already made it impossible: every interaction a provider
    offers models for must mark exactly one default, and a default may not
    carry a sunset date. So at least one undated offered model always survives,
    and a file where they are all dated does not load at all - which is a
    louder failure than any staleness finding, and an earlier one.
    """
    dated = [
        {
            "id": f"old-{n}",
            "interactions": ["transcribe"],
            "transcribe": {"batch": True},
            "deprecated": _in_days(200),
        }
        for n in range(2)
    ]
    with pytest.raises(ValidationError, match="must mark exactly one transcribe model"):
        _config({"openai": _provider(models=dated)})


# --------------------------------------------------------------------------
# Summarization: a curated shortlist against OpenRouter's chat catalogue.
# --------------------------------------------------------------------------

# A current model to carry `default: true`, since the schema refuses a default
# that is hidden or dated and refuses a provider that offers models with none.
# Its vendor row matches exactly, so it contributes no findings of its own.
ANCHOR = "vendor/anchor"


def _chat_model(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "id": "vendor/chat-1",
        "interactions": ["summarize"],
        "llm": {
            "reasoning": {"supported": True, "efforts": ["high", "low"]},
            "context_length": 100,
            "max_output_tokens": 50,
            "temperature": True,
        },
    }
    model.update(overrides)
    return model


def _chat_config(**overrides: object) -> CapabilityConfig:
    return _config(
        {
            "openrouter": _provider(
                interactions=["summarize"],
                catalog_endpoint={"summarize": ENDPOINT},
                models=[_chat_model(**overrides), _chat_model(id=ANCHOR, default=True)],
            )
        }
    )


def _vendor_chat(**overrides: object) -> VendorModel:
    base: dict[str, object] = {
        "id": "vendor/chat-1",
        "context_length": 100,
        "max_output_tokens": 50,
        "temperature": True,
        "reasoning": VendorReasoning(efforts=("high", "low"), mandatory=False),
        # As the real OpenRouter chat parser sets it: this catalogue speaks
        # about reasoning for every row, so a missing block is a denial.
        "publishes_reasoning": True,
    }
    base.update(overrides)
    return VendorModel(**base)  # type: ignore[arg-type]


def _chat_probe(*models: VendorModel, partial: bool = False) -> CatalogProbe:
    """A catalogue carrying the anchor model plus whatever the test needs."""
    return _probe(_vendor_chat(id=ANCHOR), *models, partial=partial)


def _chat_findings(config: CapabilityConfig, probe: CatalogProbe) -> list[Finding]:
    return compare(config, [probe], today=TODAY)


def test_a_curated_model_the_vendor_dropped_is_an_error() -> None:
    findings = _chat_findings(_chat_config(), _chat_probe())
    assert [(f.code, f.model) for f in findings] == [(Code.MODEL_RETIRED, "vendor/chat-1")]
    assert findings[0].severity is Severity.ERROR


def test_a_vendor_that_did_not_answer_produces_nothing_at_all() -> None:
    """The hard requirement as a test: an unreachable vendor is silence, never
    a claim that every model it serves has been retired."""
    for status in (
        CatalogStatus.UNREACHABLE,
        CatalogStatus.UNREADABLE,
        CatalogStatus.NO_CREDENTIALS,
        CatalogStatus.NO_CATALOGUE,
    ):
        assert _chat_findings(_chat_config(), _probe(status=status)) == []


def test_absence_from_a_partial_answer_is_not_absence() -> None:
    """One page of a paginated catalogue proves a model exists, never that it
    does not."""
    assert _chat_findings(_chat_config(), _chat_probe(partial=True)) == []


@pytest.mark.parametrize(
    ("field", "value", "fact"),
    [
        ("context_length", 200, "llm.context_length"),
        ("max_output_tokens", 999, "llm.max_output_tokens"),
        ("temperature", False, "llm.temperature"),
    ],
)
def test_a_contradicted_llm_fact_warns(field: str, value: object, fact: str) -> None:
    findings = _chat_findings(_chat_config(), _chat_probe(_vendor_chat(**{field: value})))
    assert [(f.fact, f.severity) for f in findings] == [(fact, Severity.WARNING)]
    assert findings[0].vendor == str(value)


def test_a_fact_the_vendor_does_not_publish_is_never_a_contradiction() -> None:
    """OpenAI's /models carries an id and a shutdown date and nothing else. A
    check that read those absences as zeroes, or as denials, would report every
    OpenAI model as wrong on every field."""
    silent = VendorModel(id="vendor/chat-1")
    assert _chat_findings(_chat_config(), _chat_probe(silent)) == []


def test_reasoning_efforts_are_compared_as_a_set() -> None:
    """The vendor orders its effort list its way and the yaml orders it the way
    it reads best. An ordering difference is not drift."""
    reordered = VendorReasoning(efforts=("low", "high"), mandatory=False)
    assert _chat_findings(_chat_config(), _chat_probe(_vendor_chat(reasoning=reordered))) == []


def test_a_changed_effort_vocabulary_warns() -> None:
    changed = VendorReasoning(efforts=("high", "low", "none"), mandatory=False)
    findings = _chat_findings(_chat_config(), _chat_probe(_vendor_chat(reasoning=changed)))
    assert [f.fact for f in findings] == ["llm.reasoning.efforts"]


def test_a_newly_mandatory_reasoning_model_warns() -> None:
    """Worth catching: a model that flips to mandatory rejects effort "none",
    so a picker still offering it produces failed requests."""
    mandatory = VendorReasoning(efforts=("high", "low"), mandatory=True)
    findings = _chat_findings(_chat_config(), _chat_probe(_vendor_chat(reasoning=mandatory)))
    assert [f.fact for f in findings] == ["llm.reasoning.mandatory"]


def test_reasoning_the_vendor_no_longer_publishes_warns_once() -> None:
    """No reasoning block *in a catalogue that publishes them* is the vendor
    saying this model does not reason. That is one finding about support, not
    three about its details."""
    findings = _chat_findings(_chat_config(), _chat_probe(_vendor_chat(reasoning=None)))
    assert [f.fact for f in findings] == ["llm.reasoning.supported"]


def test_a_model_with_no_discrete_efforts_matches_an_empty_list() -> None:
    """``reasoning: {supported: true, efforts: []}`` is a real state, not an
    oversight: the model reasons but exposes no effort levels."""
    config = _chat_config(
        llm={
            "reasoning": {"supported": True, "efforts": []},
            "context_length": 100,
            "max_output_tokens": 50,
            "temperature": True,
        }
    )
    silent_efforts = _vendor_chat(reasoning=VendorReasoning(efforts=(), mandatory=False))
    assert _chat_findings(config, _chat_probe(silent_efforts)) == []


def test_uncurated_chat_models_are_not_reported() -> None:
    """The summarize shortlist is a manual review of OpenRouter's usage
    rankings, whose dataset endpoint needs a key. Reporting the other four
    hundred chat models as candidates every run would bury every real
    finding."""
    probe = _chat_probe(_vendor_chat(), VendorModel(id="vendor/chat-2"))
    assert _chat_findings(_chat_config(), probe) == []


# --------------------------------------------------------------------------
# Vendor-published retirement dates
# --------------------------------------------------------------------------


def test_a_vendor_date_the_yaml_does_not_record_warns() -> None:
    probe = _chat_probe(_vendor_chat(retires_on=date(2026, 9, 20)))
    findings = _chat_findings(_chat_config(), probe)
    assert [f.code for f in findings] == [Code.DEPRECATION_UNRECORDED]
    assert findings[0].vendor == "2026-09-20"


def test_a_far_future_vendor_date_is_a_sentinel_and_is_ignored() -> None:
    """OpenRouter carries 2098-12-31 on models with no real sunset. Treating a
    sentinel as an announcement would file a finding on each of them."""
    probe = _chat_probe(_vendor_chat(retires_on=date(2098, 12, 31)))
    assert _chat_findings(_chat_config(), probe) == []


def test_a_vendor_date_that_disagrees_with_the_yaml_warns() -> None:
    probe = _chat_probe(_vendor_chat(retires_on=date(2026, 11, 1)))
    findings = _chat_findings(_chat_config(deprecated="2026-12-01"), probe)
    assert [f.code for f in findings] == [Code.DEPRECATION_MISMATCH]
    assert (findings[0].curated, findings[0].vendor) == ("2026-12-01", "2026-11-01")


def test_a_matching_vendor_date_says_nothing() -> None:
    probe = _chat_probe(_vendor_chat(retires_on=date(2026, 11, 1)))
    assert _chat_findings(_chat_config(deprecated="2026-11-01"), probe) == []


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------


def _video_config() -> CapabilityConfig:
    return _config(
        {
            "openrouter": _provider(
                interactions=["video"],
                catalog_endpoint={"video": "https://example.invalid/videos/models"},
                models=[
                    {
                        "id": "vendor/video-1",
                        "interactions": ["video"],
                        "default": True,
                        "video": {
                            "durations": [4, 8],
                            "resolutions": ["720p"],
                            "aspect_ratios": ["16:9"],
                            "audio": True,
                            "image_input": True,
                        },
                    }
                ],
            )
        }
    )


def _vendor_video(**overrides: object) -> VendorModel:
    base: dict[str, object] = {
        "durations": (4, 8),
        "resolutions": ("720p",),
        "aspect_ratios": ("16:9",),
        "audio": True,
        "image_input": True,
    }
    base.update(overrides)
    return VendorModel(id="vendor/video-1", video=VendorVideo(**base))  # type: ignore[arg-type]


def test_video_parameters_are_compared_against_the_catalogue() -> None:
    probe = _probe(_vendor_video(durations=(4, 8, 12), audio=False), interaction=Interaction.VIDEO)
    findings = compare(_video_config(), [probe], today=TODAY)
    assert sorted(f.fact or "" for f in findings) == ["video.audio", "video.durations"]
    durations = next(f for f in findings if f.fact == "video.durations")
    # Sorted numerically, not by repr: [4, 8, 12] rather than [12, 4, 8].
    assert durations.vendor == "[4, 8, 12]"


def test_a_video_field_published_as_null_is_left_alone() -> None:
    """``generate_audio: null`` means the vendor says nothing, which the modal
    renders as silence rather than as "no audio". Flattening it to false here
    would file a finding against every model that has one."""
    probe = _probe(_vendor_video(audio=None, image_input=None), interaction=Interaction.VIDEO)
    assert compare(_video_config(), [probe], today=TODAY) == []


# --------------------------------------------------------------------------
# Transcription: the trap
# --------------------------------------------------------------------------


def _stt_config() -> CapabilityConfig:
    return _config(
        {
            "openrouter": _provider(
                interactions=["transcribe"],
                catalog_endpoint={"transcribe": ENDPOINT},
                models=[
                    {
                        "id": "vendor/stt-1",
                        "interactions": ["transcribe"],
                        "default": True,
                        "transcribe": {
                            "batch": True,
                            "inline_diarization": True,
                            "word_timestamps": True,
                            "glossary": {"supported": True, "field": "prompt"},
                        },
                    }
                ],
            )
        }
    )


def test_transcription_facts_are_never_derived_from_the_catalogue() -> None:
    """Verified against the live API on 2026-09-02: the transcription catalogue
    (``?output_modalities=transcription``) carries a supported_parameters list
    and it is the generic *chat* one. Every STT row advertises temperature,
    top_k and top_p, none of which the transcription endpoint accepts. A row
    arriving with chat metadata must therefore produce no transcribe findings,
    only existence and dates.
    """
    chat_metadata = VendorModel(
        id="vendor/stt-1",
        context_length=8192,
        max_output_tokens=4096,
        temperature=True,
        reasoning=VendorReasoning(efforts=("high",), mandatory=False),
    )
    probe = _probe(chat_metadata, interaction=Interaction.TRANSCRIBE)
    assert compare(_stt_config(), [probe], today=TODAY) == []


def test_uncurated_transcription_models_are_offered_for_review() -> None:
    """Unlike the chat catalogue, this one is already scoped to transcription,
    so what is left over is a short list of genuine candidates."""
    probe = _probe(
        VendorModel(id="vendor/stt-1"),
        VendorModel(id="vendor/stt-2"),
        interaction=Interaction.TRANSCRIBE,
    )
    findings = compare(_stt_config(), [probe], today=TODAY)
    assert [(f.model, f.severity, f.code) for f in findings] == [
        ("vendor/stt-2", Severity.INFO, Code.MODEL_UNCURATED)
    ]


# --------------------------------------------------------------------------
# Reporting and the failure policy
# --------------------------------------------------------------------------


def _finding(severity: Severity) -> Finding:
    return Finding(
        severity=severity, code=Code.MODEL_RETIRED, kind=ProviderKind.OPENAI, message="x"
    )


def test_findings_are_ordered_worst_first() -> None:
    ordered = order([_finding(Severity.INFO), _finding(Severity.ERROR), _finding(Severity.WARNING)])
    assert [f.severity for f in ordered] == [Severity.ERROR, Severity.WARNING, Severity.INFO]


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [(FailOn.ERROR, False), (FailOn.WARNING, True), (FailOn.INFO, True), (FailOn.NEVER, False)],
)
def test_the_exit_code_follows_the_threshold(threshold: FailOn, expected: bool) -> None:
    report = StalenessReport((_finding(Severity.WARNING),))
    assert should_fail(report, threshold) is expected


def test_an_unreachable_vendor_can_never_fail_the_build() -> None:
    """Even at the strictest threshold. A probe that failed is a status, not a
    finding, so there is nothing for --fail-on to catch."""
    report = StalenessReport((), (_probe(status=CatalogStatus.UNREACHABLE),))
    assert should_fail(report, FailOn.INFO) is False


def test_the_report_says_what_was_not_checked() -> None:
    """ "No findings" means nothing without knowing who answered. A report that
    could not tell "the vendor agrees" from "we never asked" would be the false
    clean bill of health this whole feature exists to avoid."""
    report = StalenessReport((), (_probe(status=CatalogStatus.NO_CREDENTIALS),))
    rendered = render(report)
    assert "skip openrouter/summarize" in rendered
    assert "no drift found in what was checked" in rendered
