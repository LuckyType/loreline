"""Validation rules for capabilities.yaml.

These tests are about the *schema*, not the data: each one pins a way a
hand-edited capability entry can be wrong in a manner that would otherwise show
up as a silently missing UI control rather than an error.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from loreline.capability_config import CapabilityConfig, ModelSpec, ProviderSpec, load
from loreline.models import Interaction, ProviderKind
from loreline.stt import registry
from loreline.stt.backends import load as load_backends


def _transcriber(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "id": "some-model",
        "interactions": ["transcribe"],
        "transcribe": {"realtime": False, "batch": True},
    }
    model.update(overrides)
    return model


def _provider(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "label": "Test",
        "key_url": "https://example.invalid/keys",
        "interactions": ["transcribe"],
        "models": [_transcriber()],
    }
    spec.update(overrides)
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
    return {"id": "m", "interactions": ["summarize"], "llm": llm}


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
    brain: dict[str, object] = {"id": "brain", "interactions": ["summarize"], "llm": {}}
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
                interactions: [transcribe]
                models:
                  - id: m
                    interactions: [transcribe]
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
    registered". It has happened before: Deepgram's hosted Whisper and
    AssemblyAI's Universal-2 are batch only while this repo has just their
    streaming connectors, and the vosk kind was offered for a while with no
    connector at all (removed in migration v15). A model qualifies if at least
    ONE of its transports has a backend, because the streaming connectors also
    serve re-processing.
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


def test_summarize_and_video_providers_have_a_client_that_can_serve_them() -> None:
    """Declaring an interaction with no connector behind it is a silent trap.

    Unlike transcription there is no registry to consult, so the invariant is
    stated against what the client modules actually know how to reach. The
    summarizer speaks OpenAI-compatible chat and routes every kind except
    OpenRouter to OpenAI's own base URL (loreline/llm.py), and the video client
    speaks OpenRouter's /videos API (loreline/video/client.py). A kind outside
    these sets would be offered in the picker and fail at request time, which
    is what happened when Gemini's researched chat and Veo models were first
    added. Whoever writes those connectors updates this test with them.
    """
    cfg = load()

    def kinds(interaction: Interaction) -> set[str]:
        return {k.value for k, p in cfg.providers.items() if interaction in p.interactions}

    assert kinds(Interaction.SUMMARIZE) <= {"openai", "openai_compat", "openrouter"}
    assert kinds(Interaction.VIDEO) <= {"openrouter"}
