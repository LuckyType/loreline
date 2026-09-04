"""Validation rules for capabilities.yaml.

These tests are about the *schema*, not the data: each one pins a way a
hand-edited capability entry can be wrong in a manner that would otherwise show
up as a silently missing UI control rather than an error.
"""

from __future__ import annotations

import itertools
import textwrap
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from loreline.capability_config import (
    AuthScheme,
    CapabilityConfig,
    ModelSpec,
    ProviderSpec,
    Surface,
    TranscribeCapabilities,
    Transport,
    load,
)
from loreline.models import Interaction, ProviderKind
from loreline.stt import registry
from loreline.stt.backends import load as load_backends


def _transcriber(**overrides: object) -> dict[str, object]:
    # Marked default because a provider that offers models must mark exactly
    # one per interaction; a helper that produced an unmarked model would fail
    # every spec built from it for a reason unrelated to what is under test.
    model: dict[str, object] = {
        "id": "some-model",
        "interactions": ["transcribe"],
        "default": True,
        "transcribe": {"realtime": False, "batch": True},
    }
    model.update(overrides)
    return model


def _surface(**overrides: object) -> dict[str, object]:
    surface: dict[str, object] = {"url": "https://vendor.invalid/v1", "auth": "bearer"}
    surface.update(overrides)
    return surface


def _surfaces_for(spec: dict[str, object]) -> dict[str, object]:
    """Surfaces matching whatever a spec declares.

    Derived so that a test about some other rule does not fail on the surface
    validators, which insist on an address for every interaction and every
    transport a model claims.
    """
    interactions = cast("list[str]", spec.get("interactions", []))
    surfaces: dict[str, object] = {}
    if "transcribe" in interactions:
        realtime = batch = False
        entries = cast("list[dict[str, object]]", spec.get("models", []))
        entries = [*entries, *cast("list[dict[str, object]]", spec.get("model_patterns", []))]
        for entry in entries:
            caps = cast("dict[str, object]", entry.get("transcribe") or {})
            realtime = realtime or bool(caps.get("realtime", False))
            batch = batch or bool(caps.get("batch", True))
        transcribe: dict[str, object] = {}
        if realtime:
            transcribe["realtime"] = _surface(url="wss://vendor.invalid/listen")
        if batch or not realtime:
            transcribe["batch"] = _surface()
        surfaces["transcribe"] = transcribe
    for other in ("summarize", "video"):
        if other in interactions:
            surfaces[other] = _surface()
    return surfaces


def _provider(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "label": "Test",
        "key_url": "https://example.invalid/keys",
        "interactions": ["transcribe"],
        "models": [_transcriber()],
    }
    spec.update(overrides)
    spec.setdefault("surfaces", _surfaces_for(spec))
    return spec


def _config(providers: dict[str, object] | None = None) -> dict[str, object]:
    """A config covering every ProviderKind, since the loader insists on that."""
    full: dict[str, object] = {kind.value: _provider() for kind in ProviderKind}
    full.update(providers or {})
    return {"version": 1, "providers": full}


def test_minimal_config_validates() -> None:
    config = CapabilityConfig.model_validate(_config())
    assert config.provider(ProviderKind.DEEPGRAM) is not None


def test_unknown_key_is_rejected() -> None:
    """A typo must fail loudly, not read as an absent capability."""
    with pytest.raises(ValidationError, match=r"realtimee|extra"):
        ProviderSpec.model_validate(
            _provider(models=[_transcriber(transcribe={"realtimee": True})])
        )


def test_glossary_supported_requires_a_field_name() -> None:
    with pytest.raises(ValidationError, match="request field name"):
        ProviderSpec.model_validate(
            _provider(models=[_transcriber(transcribe={"glossary": {"supported": True}})])
        )


def _summarizer(llm: dict[str, object]) -> dict[str, object]:
    return {"id": "m", "interactions": ["summarize"], "default": True, "llm": llm}


def test_reasoning_without_effort_values_is_allowed() -> None:
    """MiniMax M3 and MiMo-V2.5 reason but publish no effort levels.

    Reasoning-capable and offers-an-effort-selector are different facts, so an
    empty list is meaningful: the UI hides the dropdown instead of showing an
    empty one.
    """
    spec = ProviderSpec.model_validate(
        _provider(
            interactions=["summarize"],
            models=[_summarizer({"reasoning": {"supported": True}})],
        )
    )
    llm = spec.models[0].llm
    assert llm is not None
    assert llm.reasoning.supported is True
    assert llm.reasoning.selectable_efforts() == []


def test_efforts_without_support_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"reasoning\.supported is false"):
        ProviderSpec.model_validate(
            _provider(
                interactions=["summarize"],
                models=[_summarizer({"reasoning": {"efforts": ["high"]}})],
            )
        )


def test_mandatory_reasoning_drops_the_none_effort() -> None:
    """GLM 5.3 and Gemini 3.7 Flash reject effort "none" outright."""
    spec = ProviderSpec.model_validate(
        _provider(
            interactions=["summarize"],
            models=[
                _summarizer(
                    {
                        "reasoning": {
                            "supported": True,
                            "mandatory": True,
                            "efforts": ["low", "high", "max"],
                        }
                    }
                )
            ],
        )
    )
    llm = spec.models[0].llm
    assert llm is not None
    assert llm.reasoning.selectable_efforts() == ["low", "high", "max"]


def test_mandatory_reasoning_cannot_also_list_none() -> None:
    with pytest.raises(ValidationError, match="contradicts an effort"):
        ProviderSpec.model_validate(
            _provider(
                interactions=["summarize"],
                models=[
                    _summarizer(
                        {
                            "reasoning": {
                                "supported": True,
                                "mandatory": True,
                                "efforts": ["none", "high"],
                            }
                        }
                    )
                ],
            )
        )


def test_transcription_model_needs_a_transport() -> None:
    with pytest.raises(ValidationError, match="realtime or batch"):
        ProviderSpec.model_validate(
            _provider(models=[_transcriber(transcribe={"realtime": False, "batch": False})])
        )


def test_conflicts_must_name_real_features() -> None:
    """A typo here would silently stop guarding a rejected combination."""
    with pytest.raises(ValidationError, match="unknown feature"):
        ProviderSpec.model_validate(
            _provider(
                models=[_transcriber(transcribe={"conflicts": [["glossary", "diarisation"]]})]
            )
        )


def test_conflicts_reports_the_features_blocked_by_one_toggle() -> None:
    """Gemini rejects custom_vocabulary sent with diarization or timestamps."""
    spec = ProviderSpec.model_validate(
        _provider(
            models=[
                _transcriber(
                    transcribe={
                        "inline_diarization": True,
                        "word_timestamps": True,
                        "glossary": {"supported": True, "field": "custom_vocabulary"},
                        "conflicts": [
                            ["glossary", "inline_diarization"],
                            ["glossary", "word_timestamps"],
                        ],
                    }
                )
            ]
        )
    )
    caps = spec.models[0].transcribe
    assert caps is not None
    assert caps.conflicts_with("glossary") == frozenset({"inline_diarization", "word_timestamps"})
    assert caps.conflicts_with("inline_diarization") == frozenset({"glossary"})
    assert caps.conflicts_with("word_timestamps") == frozenset({"glossary"})


def _gemini_shaped() -> TranscribeCapabilities:
    """A model with both of Gemini's declared conflicts, and all three features."""
    spec = ProviderSpec.model_validate(
        _provider(
            models=[
                _transcriber(
                    transcribe={
                        "inline_diarization": True,
                        "word_timestamps": True,
                        "glossary": {"supported": True, "field": "custom_vocabulary"},
                        "conflicts": [
                            ["glossary", "inline_diarization"],
                            ["glossary", "word_timestamps"],
                        ],
                    }
                )
            ]
        )
    )
    caps = spec.models[0].transcribe
    assert caps is not None
    return caps


def test_resolve_conflicts_keeps_the_glossary_and_drops_the_rest() -> None:
    """The decided policy: the terms are what the GM switched the toggle on for."""
    caps = _gemini_shaped()
    kept = caps.resolve_conflicts({"glossary", "inline_diarization", "word_timestamps"})
    assert kept == frozenset({"glossary"})


def test_resolve_conflicts_does_not_depend_on_the_callers_order() -> None:
    """Precedence, not set iteration order, decides which half survives."""
    caps = _gemini_shaped()
    for order in itertools.permutations(["glossary", "inline_diarization", "word_timestamps"]):
        assert caps.resolve_conflicts(order) == frozenset({"glossary"})


def test_resolve_conflicts_keeps_everything_without_the_glossary() -> None:
    """Nothing is dropped for its own sake: the two only clash with the glossary."""
    caps = _gemini_shaped()
    requested = {"inline_diarization", "word_timestamps"}
    assert caps.resolve_conflicts(requested) == frozenset(requested)


def test_resolve_conflicts_is_a_no_op_for_a_model_declaring_none() -> None:
    """Every model but Gemini's batch transcriber, today."""
    spec = ProviderSpec.model_validate(
        _provider(
            models=[
                _transcriber(
                    transcribe={
                        "inline_diarization": True,
                        "word_timestamps": True,
                        "glossary": {"supported": True, "field": "keyterm"},
                    }
                )
            ]
        )
    )
    caps = spec.models[0].transcribe
    assert caps is not None
    requested = {"glossary", "inline_diarization", "word_timestamps"}
    assert caps.resolve_conflicts(requested) == frozenset(requested)


# Kinds whose connector runs every request through a FeatureConflictGuard. A
# conflict declared for a kind that is not on this list is a rule nobody
# enforces, which is exactly the bug this list exists to stop coming back: the
# yaml said the pair was illegal for months while the connector kept sending
# it, and every Gemini re-process with a glossary died on a 400.
_CONFLICT_ENFORCING_KINDS = {ProviderKind.GEMINI}


def test_every_declared_conflict_has_a_connector_that_enforces_it() -> None:
    config = load()
    declaring = {
        kind
        for kind, spec in config.providers.items()
        for model in spec.models
        if model.transcribe and model.transcribe.conflicts
    }
    unenforced = sorted(k.value for k in declaring - _CONFLICT_ENFORCING_KINDS)
    assert not unenforced, (
        f"capabilities.yaml declares conflicts for {', '.join(unenforced)}, whose connector "
        "does not apply a FeatureConflictGuard: wire it up, then add the kind here"
    )


def test_declared_interaction_without_its_block_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no transcribe block"):
        ProviderSpec.model_validate(_provider(models=[{"id": "m", "interactions": ["transcribe"]}]))


def test_stale_block_without_its_interaction_is_rejected() -> None:
    """Removing an interaction must not leave its parameters behind."""
    with pytest.raises(ValidationError, match="does not declare transcribe"):
        ProviderSpec.model_validate(
            _provider(
                interactions=["transcribe", "summarize"],
                models=[
                    {
                        "id": "m",
                        "interactions": ["summarize"],
                        "llm": {},
                        "transcribe": {"batch": True},
                    }
                ],
            )
        )


def test_model_cannot_exceed_its_provider_interactions() -> None:
    with pytest.raises(ValidationError, match="does not offer"):
        ProviderSpec.model_validate(
            _provider(
                interactions=["transcribe"],
                models=[
                    {
                        "id": "m",
                        "interactions": ["transcribe", "video"],
                        "transcribe": {"batch": True},
                        "video": {},
                    }
                ],
            )
        )


def test_two_defaults_for_one_interaction_are_rejected() -> None:
    """Two would put the answer back where the marker took it from: list order."""
    with pytest.raises(ValidationError, match="exactly one transcribe model as default"):
        ProviderSpec.model_validate(_provider(models=[_transcriber(id="a"), _transcriber(id="b")]))


def test_offered_models_without_a_default_are_rejected() -> None:
    """None leaves the kind with no house transport to route by."""
    with pytest.raises(ValidationError, match="exactly one transcribe model as default"):
        ProviderSpec.model_validate(_provider(models=[_transcriber(default=False)]))


def test_a_provider_that_curates_nothing_needs_no_default() -> None:
    """The self-hosted kind's catalogue is whatever the operator installed, so
    there is no model here to vouch for and its connector needs none."""
    spec = ProviderSpec.model_validate(
        _provider(
            hosting="selfhosted",
            auth="optional",
            key_url=None,
            models=[],
            model_patterns=[
                {"match": "*", "interactions": ["transcribe"], "transcribe": {"batch": True}}
            ],
        )
    )
    assert spec.default_model(Interaction.TRANSCRIBE) is None


def test_a_hidden_model_cannot_be_the_default() -> None:
    """Hidden means its connector is unverified - exactly what must not run
    when nobody chose."""
    with pytest.raises(ValidationError, match="hidden and cannot be a default"):
        ProviderSpec.model_validate(_provider(models=[_transcriber(hidden=True)]))


def test_a_retiring_model_cannot_be_the_default() -> None:
    """The failure mode this whole marker exists for: the connectors' hardcoded
    defaults named whisper-1 and nova-2 long after both were superseded. Dating
    the marked model now fails the file, which forces the successor to be
    picked in the same edit."""
    with pytest.raises(ValidationError, match="cannot be a default"):
        ProviderSpec.model_validate(_provider(models=[_transcriber(deprecated="2027-02-26")]))


def test_shipped_config_marks_one_default_per_offered_interaction() -> None:
    """The guard on the real file, kind by kind and interaction by interaction.

    A default is per (kind, interaction) because a single provider row serves
    several: OpenRouter transcribes, summarizes and generates video, and the
    bug this replaced was a "default" that meant the first model of *any*
    interaction, which handed a chat model to a transcription provider.
    """
    for kind, provider in load().providers.items():
        for interaction in provider.interactions:
            offered = provider.models_for(interaction)
            chosen = provider.default_model(interaction)
            if not offered:
                assert chosen is None, f"{kind.value} curates no {interaction.value} models"
                continue
            assert chosen in {m.id for m in offered}, (
                f"{kind.value}/{interaction.value} has no usable default"
            )


def test_cloud_key_provider_needs_a_key_url() -> None:
    """The setup wizard links this; a blank link is a dead end for the user."""
    with pytest.raises(ValidationError, match="key_url"):
        ProviderSpec.model_validate(_provider(key_url=None))


def test_self_hosted_provider_needs_no_key_url() -> None:
    spec = ProviderSpec.model_validate(
        _provider(hosting="selfhosted", auth="optional", key_url=None)
    )
    assert spec.key_url is None


def test_every_provider_kind_must_be_described() -> None:
    partial = {k.value: _provider() for k in ProviderKind if k is not ProviderKind.GEMINI}
    with pytest.raises(ValidationError, match="no entry for provider kind"):
        CapabilityConfig.model_validate({"version": 1, "providers": partial})


def test_find_prefers_exact_id_then_pattern() -> None:
    spec = ProviderSpec.model_validate(
        _provider(
            models=[_transcriber(id="exact-model")],
            model_patterns=[
                {
                    "match": "*whisper*",
                    "interactions": ["transcribe"],
                    "transcribe": {"batch": True, "word_timestamps": True},
                }
            ],
        )
    )
    exact = spec.find("exact-model")
    assert isinstance(exact, ModelSpec)
    assert exact.id == "exact-model"
    matched = spec.find("Systran/faster-whisper-large-v3")
    assert matched is not None and matched.transcribe is not None
    assert matched.transcribe.word_timestamps is True
    # An unannotated model is unknown, never "unsupported".
    assert spec.find("nvidia/parakeet-tdt") is None


def test_models_for_scopes_by_interaction() -> None:
    brain: dict[str, object] = {
        "id": "brain",
        "interactions": ["summarize"],
        "default": True,
        "llm": {},
    }
    spec = ProviderSpec.model_validate(
        _provider(
            interactions=["transcribe", "summarize"],
            models=[_transcriber(id="ears"), brain],
        )
    )
    assert [m.id for m in spec.models_for(Interaction.TRANSCRIBE)] == ["ears"]
    assert [m.id for m in spec.models_for(Interaction.SUMMARIZE)] == ["brain"]


def test_load_rejects_a_non_mapping_file(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping at the top level"):
        load(path)


def test_load_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.yaml"
    providers = "\n".join(
        textwrap.dedent(
            f"""\
              {kind.value}:
                label: {kind.value}
                key_url: https://example.invalid/keys
                surfaces:
                  transcribe:
                    batch: {{url: "https://example.invalid/v1", auth: bearer}}
                interactions: [transcribe]
                models:
                  - id: m
                    interactions: [transcribe]
                    default: true
                    transcribe: {{batch: true}}
            """
        ).rstrip()
        for kind in ProviderKind
    )
    path.write_text(
        f"version: 1\nproviders:\n{textwrap.indent(providers, '  ')}\n", encoding="utf-8"
    )
    assert load(path).version == 1


def test_shipped_config_is_valid() -> None:
    """The real file must always parse. This is the CI guard on hand edits."""
    config = load()
    assert config.providers


def test_every_offered_transcription_model_can_actually_be_routed() -> None:
    """No curated model may be unreachable by any registered connector.

    The failure this prevents is invisible in the UI: a model appears in the
    picker, the GM selects it, and the job dies with "no STT backend
    registered". It has happened before: the vosk kind was offered for a while
    with no connector at all (removed in migration v15), and Deepgram's hosted
    Whisper and AssemblyAI's Universal-2 had to stay out of this file entirely
    while their vendors' only connector here was the streaming one, because
    both models are batch only. They are described now that the batch
    connectors exist. A model qualifies if at least ONE of its transports has a
    backend, because the streaming connectors also serve re-processing.
    """
    load_backends()
    registered = registry.registered_transports()
    unroutable: list[str] = []
    for kind, provider in load().providers.items():
        for model in provider.models_for(Interaction.TRANSCRIBE):
            caps = model.transcribe
            assert caps is not None
            transports = {rt for rt, on in ((True, caps.realtime), (False, caps.batch)) if on}
            if not transports & {rt for k, rt in registered if k is kind}:
                unroutable.append(f"{kind.value}/{model.id}")
    assert not unroutable, f"offered but unroutable: {', '.join(unroutable)}"


def test_every_registered_connector_serves_a_described_model() -> None:
    """The inverse guard: a transport nothing uses is a connector nobody calls.

    Registering (kind, transport) pairs and describing models are two halves of
    one decision, and only the halves together make a model reachable. The
    other direction is caught above; this one catches a connector left behind
    after its models were dropped, or written for a transport this file never
    claims, which is dead code that still passes every other test.

    Hidden models count here, unlike above: hiding is a release gate on
    offering a model, not on the connector existing, and an unverified
    connector plus its hidden model is exactly the intended state (Gemini's
    Live API, Deepgram Whisper, AssemblyAI Universal-2). Patterns count too,
    because a kind whose catalogue is discovered at runtime lists no models at
    all and declares its transports by glob.
    """
    load_backends()
    unused: list[str] = []
    for kind, provider in load().providers.items():
        described: set[bool] = set()
        for entry in (*provider.models, *provider.model_patterns):
            caps = entry.transcribe
            if caps is None:
                continue
            described.update(rt for rt, on in ((True, caps.realtime), (False, caps.batch)) if on)
        for registered_kind, realtime in registry.registered_transports():
            if registered_kind is kind and realtime not in described:
                transport = "streaming" if realtime else "batch"
                unused.append(f"{kind.value} ({transport})")
    assert not unused, f"connector registered for a transport no model uses: {', '.join(unused)}"


def test_summarize_and_video_providers_have_a_client_that_can_serve_them() -> None:
    """Declaring an interaction with no connector behind it is a silent trap.

    Unlike transcription there is no registry to consult, so the invariant is
    stated against what the client modules actually know how to reach. The
    summarizer speaks OpenAI-compatible chat (loreline/llm.py) and the video
    client speaks OpenRouter's /videos API (loreline/video/client.py), which no
    other vendor serves. A kind outside these sets would be offered in the
    picker and fail at request time, which is what happened when Gemini's
    researched chat and Veo models were first added. Whoever writes those
    connectors updates this test with them.

    Membership alone is too weak for the summarize half: every cloud kind that
    is not OpenAI must also declare its own chat address, since a copy of
    OpenAI's would pass the schema and post a Gemini key to OpenAI. Gemini
    passes because its summarize surface is Google's OpenAI-compatible shim,
    a sibling path the native transcription base does not serve.
    """
    cfg = load()

    def kinds(interaction: Interaction) -> set[str]:
        return {k.value for k, p in cfg.providers.items() if interaction in p.interactions}

    assert kinds(Interaction.SUMMARIZE) <= {"openai", "openai_compat", "openrouter", "gemini"}
    assert kinds(Interaction.VIDEO) <= {"openrouter"}

    openai_chat = cfg.providers[ProviderKind.OPENAI].surface(Interaction.SUMMARIZE)
    assert openai_chat is not None
    for kind, provider in cfg.providers.items():
        # openai_compat is the operator-supplied case: its address comes from
        # the provider row, and the surface deliberately has none of its own.
        if Interaction.SUMMARIZE not in provider.interactions or kind in (
            ProviderKind.OPENAI,
            ProviderKind.OPENAI_COMPAT,
        ):
            continue
        chat = provider.surface(Interaction.SUMMARIZE)
        assert chat is not None
        assert chat.url != openai_chat.url, f"{kind.value} posts chat to OpenAI"
    gemini_chat = cfg.providers[ProviderKind.GEMINI].surface(Interaction.SUMMARIZE)
    assert gemini_chat is not None and gemini_chat.url is not None
    assert gemini_chat.url.endswith("/openai")


# --------------------------------------------------------------------------
# Surfaces: where a vendor is reached, per interaction and transport
# --------------------------------------------------------------------------


def test_declared_interaction_without_a_surface_is_rejected() -> None:
    """A connector for an interaction with no address has nowhere to go."""
    with pytest.raises(ValidationError, match=r"no surfaces\.summarize entry"):
        ProviderSpec.model_validate(
            _provider(
                interactions=["transcribe", "summarize"],
                models=[_transcriber()],
                surfaces={"transcribe": {"batch": _surface()}},
            )
        )


def test_stale_surface_without_its_interaction_is_rejected() -> None:
    """Dropping an interaction must not leave its address behind."""
    with pytest.raises(ValidationError, match=r"surfaces\.video entry but does not declare"):
        ProviderSpec.model_validate(
            _provider(surfaces={"transcribe": {"batch": _surface()}, "video": _surface()})
        )


def test_a_model_may_not_claim_a_transport_its_vendor_has_no_surface_for() -> None:
    """Routing a streaming model to a kind with no socket fails at request time."""
    with pytest.raises(ValidationError, match=r"no surfaces\.transcribe\.realtime"):
        ProviderSpec.model_validate(
            _provider(
                models=[_transcriber(transcribe={"realtime": True, "batch": False})],
                surfaces={"transcribe": {"batch": _surface()}},
            )
        )


def test_hidden_models_and_patterns_still_need_their_transport_surface() -> None:
    """Hiding gates the picker, not the connector: an explicit config routes."""
    with pytest.raises(ValidationError, match="'\\*live\\*' transcribes over realtime"):
        ProviderSpec.model_validate(
            _provider(
                model_patterns=[
                    {
                        "match": "*live*",
                        "interactions": ["transcribe"],
                        "transcribe": {"realtime": True, "batch": False},
                    }
                ],
                surfaces={"transcribe": {"batch": _surface()}},
            )
        )


def test_transcribe_surfaces_need_at_least_one_transport() -> None:
    with pytest.raises(ValidationError, match="at least one transport"):
        ProviderSpec.model_validate(_provider(surfaces={"transcribe": {}}))


def test_a_catalog_for_an_interaction_not_offered_is_rejected() -> None:
    with pytest.raises(ValidationError, match="catalog for video, which it does not offer"):
        ProviderSpec.model_validate(
            _provider(
                surfaces={
                    "transcribe": {"batch": _surface()},
                    "catalog": {"video": _surface(url="https://vendor.invalid/videos/models")},
                }
            )
        )


def test_catalog_is_optional_and_may_be_one_surface_or_one_per_interaction() -> None:
    none = ProviderSpec.model_validate(_provider())
    assert none.catalog(Interaction.TRANSCRIBE) is None
    shared = ProviderSpec.model_validate(
        _provider(
            interactions=["transcribe", "summarize"],
            surfaces={
                "transcribe": {"batch": _surface()},
                "summarize": _surface(),
                "catalog": _surface(url="https://vendor.invalid/models"),
            },
        )
    )
    for interaction in (Interaction.TRANSCRIBE, Interaction.SUMMARIZE):
        catalog = shared.catalog(interaction)
        assert catalog is not None and catalog.url == "https://vendor.invalid/models"
    # Not offered, so not catalogued either, whatever the single surface says.
    assert shared.catalog(Interaction.VIDEO) is None
    split = ProviderSpec.model_validate(
        _provider(
            interactions=["transcribe", "summarize"],
            surfaces={
                "transcribe": {"batch": _surface()},
                "summarize": _surface(),
                "catalog": {"summarize": _surface(url="https://vendor.invalid/chat/models")},
            },
        )
    )
    assert split.catalog(Interaction.TRANSCRIBE) is None
    chat = split.catalog(Interaction.SUMMARIZE)
    assert chat is not None and chat.url == "https://vendor.invalid/chat/models"


@pytest.mark.parametrize("url", [None, "{base_url}/models"])
def test_a_surface_without_an_address_of_its_own_must_be_overridable(url: str | None) -> None:
    """Null or a template means the operator supplies it; the file has to say so."""
    with pytest.raises(ValidationError, match="must be overridable"):
        Surface.model_validate({"url": url})
    assert Surface.model_validate({"url": url, "overridable": True}).resolve(None) is None


@pytest.mark.parametrize("url", ["api.vendor.invalid/v1", "/v1", "ftp://vendor.invalid"])
def test_a_surface_url_must_be_absolute(url: str) -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        Surface.model_validate({"url": url})


def test_a_health_path_is_relative_to_its_surface() -> None:
    with pytest.raises(ValidationError, match="relative to the surface url"):
        Surface.model_validate({"url": "https://vendor.invalid/v1", "health": "models"})


def test_a_bare_health_path_is_the_shorthand_for_a_probe_block() -> None:
    """The yaml writes ``health: /key``; what it means is a probe asking that path."""
    surface = Surface.model_validate({"url": "https://vendor.invalid/v1", "health": "/key"})
    assert surface.health is not None
    assert surface.health.path == "/key"
    assert surface.health.frame is None


def test_a_health_question_matches_the_surfaces_transport() -> None:
    """A socket is asked with a frame and an HTTP surface at a path; a block
    declaring the other is a mistake the loader should name, not something a
    probe should quietly ignore."""
    with pytest.raises(ValidationError, match="frame, not a path"):
        Surface.model_validate({"url": "wss://vendor.invalid/v1", "health": "/models"})
    with pytest.raises(ValidationError, match="path, not with a frame"):
        Surface.model_validate(
            {"url": "https://vendor.invalid/v1", "health": {"frame": {"type": "Hello"}}}
        )
    with pytest.raises(ValidationError, match="path or a frame"):
        Surface.model_validate({"url": "https://vendor.invalid/v1", "health": {}})


class TestOverride:
    """How a provider row's base_url reaches a surface, or does not."""

    def test_an_overridable_surface_takes_the_row_address(self) -> None:
        surface = Surface(url="https://vendor.invalid/v1", overridable=True)
        assert surface.resolve("https://eu.vendor.invalid/v1") == "https://eu.vendor.invalid/v1"
        assert surface.resolve(None) == "https://vendor.invalid/v1"

    def test_a_fixed_surface_ignores_the_row_address(self) -> None:
        surface = Surface(url="https://vendor.invalid/v1")
        assert surface.resolve("https://elsewhere.invalid") == "https://vendor.invalid/v1"

    def test_a_socket_address_is_dropped_by_an_http_surface(self) -> None:
        """For the kinds whose streaming connector shipped first, a stored
        base_url has always meant the socket, and an HTTP client handed a
        wss:// URL fails every request. The batch surface falls back to its
        own address, which is what such a row meant all along."""
        surface = Surface(url="https://vendor.invalid", overridable=True)
        assert surface.resolve("wss://vendor.invalid/v1/listen") == "https://vendor.invalid"

    def test_an_http_address_is_dropped_by_a_socket_surface(self) -> None:
        surface = Surface(url="wss://vendor.invalid/v1/listen", overridable=True)
        assert surface.resolve("https://vendor.invalid") == "wss://vendor.invalid/v1/listen"
        assert surface.resolve("ws://127.0.0.1:9") == "ws://127.0.0.1:9"

    def test_a_template_splices_the_row_address_in(self) -> None:
        """The self-hosted catalogue: the row's base already carries the
        version segment the connectors post to, so a trailing slash is the
        only thing trimmed."""
        surface = Surface(url="{base_url}/models", overridable=True)
        assert surface.resolve("http://speaches:8000/v1/") == "http://speaches:8000/v1/models"
        assert surface.resolve(None) is None

    def test_an_operator_surface_with_no_row_address_resolves_to_nothing(self) -> None:
        assert Surface(url=None, overridable=True).resolve(None) is None
        assert Surface(url=None, overridable=True).resolve("http://ollama:11434/v1") == (
            "http://ollama:11434/v1"
        )


class TestAuthScheme:
    """One spelling per scheme, rendered in one place."""

    @pytest.mark.parametrize(
        ("scheme", "headers"),
        [
            (AuthScheme.BEARER, {"Authorization": "Bearer k"}),
            (AuthScheme.TOKEN_HEADER, {"Authorization": "Token k"}),
            (AuthScheme.RAW_HEADER, {"Authorization": "k"}),
            (AuthScheme.GOOG_HEADER, {"x-goog-api-key": "k"}),
            (AuthScheme.QUERY_KEY, {}),
            (AuthScheme.NONE, {}),
        ],
    )
    def test_headers(self, scheme: AuthScheme, headers: dict[str, str]) -> None:
        assert scheme.headers("k") == headers
        assert scheme.headers(None) == {}

    def test_only_the_query_scheme_puts_the_key_in_the_url(self) -> None:
        assert AuthScheme.QUERY_KEY.query("k") == {"key": "k"}
        assert AuthScheme.QUERY_KEY.query(None) == {}
        assert AuthScheme.BEARER.query("k") == {}

    def test_a_surface_adds_its_fixed_headers_to_the_credential(self) -> None:
        surface = Surface(url="https://vendor.invalid", headers={"X-Title": "Loreline"})
        assert surface.request_headers("k") == {"Authorization": "Bearer k", "X-Title": "Loreline"}
        assert surface.request_headers(None) == {"X-Title": "Loreline"}


def test_shipped_surfaces_match_the_transport_they_serve() -> None:
    """A streaming surface is a socket, everything else is HTTP.

    The override rule keys on this (a wss:// row address goes to the socket
    surface and nowhere else), so a batch surface declared as wss:// would
    silently start swallowing the streaming connector's address.
    """
    for kind, provider in load().providers.items():
        transcribe = provider.surfaces.transcribe
        if transcribe is not None:
            if transcribe.realtime is not None:
                assert transcribe.realtime.socket, f"{kind.value} realtime is not a socket"
            if transcribe.batch is not None:
                assert not transcribe.batch.socket, f"{kind.value} batch is a socket"
        for interaction in (Interaction.SUMMARIZE, Interaction.VIDEO):
            surface = provider.surface(interaction)
            assert surface is None or not surface.socket, f"{kind.value} {interaction} is a socket"
        for interaction in provider.interactions:
            catalog = provider.catalog(interaction)
            assert catalog is None or not catalog.socket


def test_shipped_operator_surfaces_belong_to_the_self_hosted_kind_alone() -> None:
    """Only the kind the wizard asks a base URL for may leave its address to
    the operator; a cloud surface with no url would be a connector with
    nowhere to post and no field in the UI to fix it."""
    transports: tuple[Transport | None, ...] = ("realtime", "batch", None)
    for kind, provider in load().providers.items():
        for interaction in provider.interactions:
            for transport in transports:
                surface = provider.surface(interaction, transport)
                if surface is None:
                    continue
                operator = surface.resolve(None) is None
                assert operator == (kind is ProviderKind.OPENAI_COMPAT), (
                    f"{kind.value} {interaction.value} {transport}"
                )
