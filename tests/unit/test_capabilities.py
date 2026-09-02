"""Which provider+model combinations are offered for which interaction.

The regression these guard is concrete: the pickers used to offer everything a
provider's ``/models`` returned, so a GM could pick ``dall-e-3`` to transcribe
with and only discover the mistake when the job failed.
"""

from __future__ import annotations

import pytest

from loreline.capabilities import (
    INTERACTIONS_BY_KIND,
    config,
    default_diarizing_model,
    default_model,
    filter_models,
    interactions_for,
    is_realtime_model,
    kinds_for,
    kinds_with_inline_diarization,
    supports,
    supports_batch,
    supports_inline_diarization,
    supports_live_capture,
    supports_realtime,
)
from loreline.capability_config import ModelSpec
from loreline.models import Interaction, ModelInfo, ProviderKind


class TestCapabilityTable:
    def test_every_provider_kind_declares_its_interactions(self) -> None:
        """A kind missing from the table is offered for nothing at all, which
        would silently remove it from the UI - so the table must be total."""
        assert set(INTERACTIONS_BY_KIND) == set(ProviderKind)
        assert all(INTERACTIONS_BY_KIND[k] for k in ProviderKind), "a kind declares no interaction"

    def test_a_kind_may_serve_several_interactions(self) -> None:
        """One provider entry per vendor, not one per role: OpenAI and
        OpenRouter both transcribe and summarize from a single stored config.
        What keeps the pickers meaningful is that each is scoped by interaction,
        not that the kinds are disjoint."""
        both = kinds_for(Interaction.TRANSCRIBE) & kinds_for(Interaction.SUMMARIZE)
        assert both == {
            ProviderKind.OPENAI,
            ProviderKind.OPENAI_COMPAT,
            ProviderKind.OPENROUTER,
            ProviderKind.GEMINI,
        }

    def test_only_openrouter_generates_video(self) -> None:
        assert kinds_for(Interaction.VIDEO) == {ProviderKind.OPENROUTER}

    def test_openrouter_is_a_single_kind_doing_everything(self) -> None:
        """One entry, three abilities. Its chat, transcription and video
        catalogues are disjoint, which is handled by scoping each picker to an
        interaction rather than by splitting the vendor into several kinds."""
        assert interactions_for(ProviderKind.OPENROUTER) == {
            Interaction.TRANSCRIBE,
            Interaction.SUMMARIZE,
            Interaction.VIDEO,
        }

    def test_a_kind_does_not_claim_what_it_cannot_do(self) -> None:
        assert not supports(ProviderKind.DEEPGRAM, Interaction.VIDEO)
        assert not supports(ProviderKind.DEEPGRAM, Interaction.SUMMARIZE)
        assert not supports(ProviderKind.OPENAI, Interaction.VIDEO)


class TestDefaultModel:
    """The one default left: what a connector names when nobody chose.

    Every action route now requires a model, so this answers for the health
    probe alone (POST /providers/{id}/test), whose websocket kinds name a model
    in the handshake.
    """

    def test_the_default_is_scoped_to_the_interaction(self) -> None:
        """The regression in one assertion. OpenRouter serves three
        interactions from one provider row, and its catalogues are disjoint, so
        a per-kind default was guaranteed wrong for two of them - the previous
        attempt ("first non-hidden model of any interaction") handed a chat
        model to a transcription provider."""
        transcribe = default_model(ProviderKind.OPENROUTER, Interaction.TRANSCRIBE)
        summarize = default_model(ProviderKind.OPENROUTER, Interaction.SUMMARIZE)
        assert transcribe != summarize
        spec = config().providers[ProviderKind.OPENROUTER]
        assert transcribe in {m.id for m in spec.models_for(Interaction.TRANSCRIBE)}
        assert summarize in {m.id for m in spec.models_for(Interaction.SUMMARIZE)}

    def test_a_kind_with_no_curated_catalogue_has_no_default(self) -> None:
        """The self-hosted kind. Its connector then names no model at all, which
        is the only honest answer for a server whose models nobody has seen -
        the constant it replaced pinned whisper-1 onto every such server."""
        assert default_model(ProviderKind.OPENAI_COMPAT, Interaction.TRANSCRIBE) is None

    def test_an_unknown_interaction_for_a_kind_has_no_default(self) -> None:
        """Deepgram transcribes and nothing else, so there is no chat model to
        fall back to and asking must not invent one."""
        assert default_model(ProviderKind.DEEPGRAM, Interaction.SUMMARIZE) is None

    def test_the_transcription_default_decides_the_transport(self) -> None:
        """The default and the connector lookup read the same value, so a probe
        cannot open a Realtime session while the model that would run posts."""
        chosen = default_model(ProviderKind.OPENAI, Interaction.TRANSCRIBE)
        assert chosen == "gpt-transcribe"
        assert not is_realtime_model(ProviderKind.OPENAI, chosen)


class TestDiarizingModel:
    """Which model answers "give me speakers", when the caller names none."""

    def test_every_kind_that_can_diarize_offers_a_default(self) -> None:
        """The guard the resolution rule leans on.

        The rule is order-free by construction: the interaction default when it
        diarizes, else the single model that does. That leaves exactly one
        undefined case - several diarizing models with the default not among
        them - and this fails there, because picking by list position is what
        the whole marker exists to avoid. The fix at that point is a human
        decision, not a code change.
        """
        for kind, spec in config().providers.items():
            diarizers = [
                m.id
                for m in spec.models_for(Interaction.TRANSCRIBE)
                if m.transcribe and m.transcribe.inline_diarization
            ]
            chosen = default_diarizing_model(kind)
            if not diarizers:
                assert chosen is None, f"{kind.value} offers no diarizing model"
                continue
            assert chosen in diarizers, (
                f"{kind.value} has {len(diarizers)} diarizing models and no way to pick one"
            )

    def test_it_is_not_just_the_transcription_default(self) -> None:
        """On OpenAI the two genuinely differ: the transcription default
        (gpt-transcribe) returns no speakers, so a diarization pass that
        inherited it would produce an unlabelled timeline and no error."""
        assert default_model(ProviderKind.OPENAI, Interaction.TRANSCRIBE) == "gpt-transcribe"
        assert default_diarizing_model(ProviderKind.OPENAI) == "gpt-4o-transcribe-diarize"
        assert not supports_inline_diarization(ProviderKind.OPENAI, "gpt-transcribe")


class TestLiveCapture:
    def test_openrouter_stt_is_reprocess_only(self) -> None:
        """OpenRouter transcription is a single request/response upload with no
        streaming mode, so it must never be picked for a live session."""
        assert supports(ProviderKind.OPENROUTER, Interaction.TRANSCRIBE)
        assert not supports_live_capture(ProviderKind.OPENROUTER)

    @pytest.mark.parametrize(
        "kind",
        [
            ProviderKind.DEEPGRAM,
            ProviderKind.ASSEMBLYAI,
            ProviderKind.OPENAI,
            ProviderKind.OPENAI_COMPAT,
            ProviderKind.GEMINI,
        ],
    )
    def test_every_other_stt_kind_still_drives_live_capture(self, kind: ProviderKind) -> None:
        assert supports_live_capture(kind)

    def test_realtime_and_batch_are_reported_per_kind(self) -> None:
        """What the UI badges read. Realtime means the connector streams within
        an utterance; batch means one round trip per utterance. Both drive a
        live session - only OpenRouter is barred from that."""
        assert supports_realtime(ProviderKind.DEEPGRAM)
        assert supports_realtime(ProviderKind.OPENAI)
        assert supports_batch(ProviderKind.OPENAI_COMPAT)
        assert supports_batch(ProviderKind.OPENROUTER)
        assert not supports_realtime(ProviderKind.OPENROUTER)

    def test_a_kind_may_offer_both_transports(self) -> None:
        """These are not complements: OpenAI streams gpt-live-transcribe and
        posts whisper-1 from the same kind, and which connector runs is decided
        per model (see is_realtime_model)."""
        assert supports_realtime(ProviderKind.OPENAI)
        assert supports_batch(ProviderKind.OPENAI)


class TestRealtimeModelResolution:
    """is_realtime_model picks the connector for kinds with both transports, so
    it has to answer for any model string a config can carry."""

    def test_openai_models_split_by_transport(self) -> None:
        assert is_realtime_model(ProviderKind.OPENAI, "gpt-live-transcribe")
        assert is_realtime_model(ProviderKind.OPENAI, "gpt-realtime-whisper")
        assert not is_realtime_model(ProviderKind.OPENAI, "whisper-1")
        assert not is_realtime_model(ProviderKind.OPENAI, "gpt-transcribe")
        assert not is_realtime_model(ProviderKind.OPENAI, "gpt-4o-transcribe")

    def test_gemini_live_variant_is_streaming(self) -> None:
        assert is_realtime_model(ProviderKind.GEMINI, "gemini-3.5-transcribe-live")
        assert not is_realtime_model(ProviderKind.GEMINI, "gemini-3.5-transcribe")

    def test_streaming_only_kinds_stream_whatever_the_model(self) -> None:
        """Deepgram and AssemblyAI offer nothing but streaming models here (the
        curated lists exclude Deepgram's batch-only hosted Whisper), so an
        uncurated model still rides the streaming connector."""
        assert is_realtime_model(ProviderKind.DEEPGRAM, "nova-3")
        assert is_realtime_model(ProviderKind.DEEPGRAM, "some-future-model")
        assert is_realtime_model(ProviderKind.ASSEMBLYAI, None)

    def test_an_unset_model_keeps_the_kinds_historical_connector(self) -> None:
        """None is answered against the kind alone. In practice only a kind
        that curates no catalogue reaches this, since create_backend resolves
        the declared default first, but the answer must stay defined: a lookup
        that raised here would break the health probe rather than the pick."""
        assert is_realtime_model(ProviderKind.OPENAI, None)
        assert not is_realtime_model(ProviderKind.GEMINI, None)

    def test_a_new_model_naming_its_transport_is_recognised(self) -> None:
        """The curated sets rot; both vendors put the transport in the name, so
        an uncurated "live"/"realtime" model on a mixed kind routes to the
        streaming connector rather than failing on the batch endpoint."""
        assert is_realtime_model(ProviderKind.OPENAI, "gpt-live-transcribe-2")

    def test_batch_only_kinds_never_stream(self) -> None:
        """The name markers apply only to kinds with a streaming connector to
        route to: a self-hosted model with "live" in its name still posts."""
        assert not is_realtime_model(ProviderKind.OPENAI_COMPAT, "whisper-live-v3")
        assert not is_realtime_model(ProviderKind.OPENROUTER, "x-ai/grok-stt-1.0")


def _models(*ids: str) -> list[ModelInfo]:
    return [ModelInfo(id=i) for i in ids]


class TestModelFiltering:
    def test_openai_transcription_picker_drops_non_audio_models(self) -> None:
        """The actual reported bug: OpenAI's /models mixes chat, image and TTS
        models in with whisper, and all of them were offered for transcription."""
        listed = _models("gpt-4o", "dall-e-3", "tts-1", "whisper-1", "gpt-4o-transcribe")
        kept = filter_models(listed, kind=ProviderKind.OPENAI, interaction=Interaction.TRANSCRIBE)
        assert [m.id for m in kept] == ["whisper-1", "gpt-4o-transcribe"]

    def test_self_hosted_naming_is_recognised(self) -> None:
        listed = _models("Systran/faster-whisper-large-v3", "nvidia/parakeet-tdt-0.6b-v3", "llama3")
        kept = filter_models(
            listed, kind=ProviderKind.OPENAI_COMPAT, interaction=Interaction.TRANSCRIBE
        )
        assert [m.id for m in kept] == [
            "Systran/faster-whisper-large-v3",
            "nvidia/parakeet-tdt-0.6b-v3",
        ]

    def test_a_filter_that_would_empty_the_list_is_discarded(self) -> None:
        """A self-hosted server may name its models in a way these markers have
        never seen. Showing one extra model beats stranding an operator with an
        empty picker and no way to pick the model they installed."""
        listed = _models("my-custom-build-v2", "another-one")
        kept = filter_models(
            listed, kind=ProviderKind.OPENAI_COMPAT, interaction=Interaction.TRANSCRIBE
        )
        assert [m.id for m in kept] == ["my-custom-build-v2", "another-one"]

    def test_openrouter_lists_pass_through_untouched(self) -> None:
        """OpenRouter is fetched from a modality-scoped endpoint, so the list is
        already correct - name matching would only corrupt it."""
        listed = _models("openai/whisper-large-v3-turbo", "nvidia/nemotron-3.5-asr-streaming")
        kept = filter_models(
            listed, kind=ProviderKind.OPENROUTER, interaction=Interaction.TRANSCRIBE
        )
        assert [m.id for m in kept] == [m.id for m in listed]

    def test_summarize_and_video_are_never_name_filtered(self) -> None:
        listed = _models("anthropic/claude-sonnet-4.5", "openai/gpt-4o")
        for interaction in (Interaction.SUMMARIZE, Interaction.VIDEO):
            kept = filter_models(listed, kind=ProviderKind.OPENROUTER, interaction=interaction)
            assert [m.id for m in kept] == [m.id for m in listed]


class TestStrictToggle:
    """`strict=False` is the escape hatch behind the settings toggle: the name
    markers are a hand-maintained guess, and a model released tomorrow will not
    match them."""

    def test_disabling_strict_shows_everything_the_endpoint_offers(self) -> None:
        listed = _models("gpt-4o", "dall-e-3", "whisper-1", "some-brand-new-asr-model-2027")
        kept = filter_models(
            listed,
            kind=ProviderKind.OPENAI,
            interaction=Interaction.TRANSCRIBE,
            strict=False,
        )
        assert [m.id for m in kept] == [m.id for m in listed]

    def test_strict_is_the_default(self) -> None:
        """Callers that don't opt out get the safe behaviour."""
        listed = _models("gpt-4o", "whisper-1")
        assert [
            m.id
            for m in filter_models(
                listed, kind=ProviderKind.OPENAI, interaction=Interaction.TRANSCRIBE
            )
        ] == ["whisper-1"]

    def test_the_toggle_never_unscopes_provider_sourced_lists(self) -> None:
        """Turning it off must not resurrect the original bug for OpenRouter:
        its transcription list comes from a modality-scoped endpoint, so it is
        already correct and a new model there appears automatically."""
        listed = _models("openai/whisper-large-v3-turbo")
        for strict in (True, False):
            kept = filter_models(
                listed,
                kind=ProviderKind.OPENROUTER,
                interaction=Interaction.TRANSCRIBE,
                strict=strict,
            )
            assert [m.id for m in kept] == ["openai/whisper-large-v3-turbo"]


class TestInlineDiarization:
    """ "Inline (from STT)" must only be offered where speakers actually come
    back - a mode that silently yields an unlabelled transcript is discovered
    after the session, when the live audio is gone."""

    def test_deepgram_nova_models_diarize(self) -> None:
        assert supports_inline_diarization(ProviderKind.DEEPGRAM, "nova-3")
        assert supports_inline_diarization(ProviderKind.DEEPGRAM, "nova-2-meeting")

    def test_deepgram_flux_does_not(self) -> None:
        """Deepgram's diarization docs list Nova/enhanced/base; Flux does not
        carry `diarize` among its supported parameters."""
        assert not supports_inline_diarization(ProviderKind.DEEPGRAM, "flux-general-en")
        assert not supports_inline_diarization(ProviderKind.DEEPGRAM, "flux-general-multi")

    def test_assemblyai_streaming_models_diarize(self) -> None:
        for model in (
            "universal-3-5-pro",
            "universal-streaming-english",
            "universal-streaming-multilingual",
        ):
            assert supports_inline_diarization(ProviderKind.ASSEMBLYAI, model)

    def test_gemini_transcribe_diarizes(self) -> None:
        assert supports_inline_diarization(ProviderKind.GEMINI, "gemini-3.5-transcribe")

    def test_gemini_live_does_not_diarize(self) -> None:
        """Google's Live API docs state plainly that speaker diarization is not
        supported in live streaming sessions, so the guard must refuse "Inline
        (from STT)" on the -live model rather than silently producing an
        unlabelled transcript."""
        assert not supports_inline_diarization(ProviderKind.GEMINI, "gemini-3.5-transcribe-live")

    def test_openrouter_surfaces_no_diarization_at_all(self) -> None:
        """Not even for the model whose description advertises it.

        x-ai/grok-stt-1.0's OpenRouter page says it "supports transcription
        with word-level timestamps, optional speaker diarization, and
        multichannel audio", and this repo believed it. Checked against the
        gateway on 2026-09-02: the transcription request schema has no
        diarization field, the model's supported_parameters are only
        max_tokens/temperature/top_p/seed/logprobs/top_logprobs/response_format,
        and the response body carries no speaker structure. Diarization is a
        native-provider feature the gateway does not pass through, so offering
        "Inline (from STT)" here produced an unlabelled transcript and no
        warning."""
        assert not supports_inline_diarization(ProviderKind.OPENROUTER, "x-ai/grok-stt-1.0")
        assert ProviderKind.OPENROUTER not in kinds_with_inline_diarization()

    def test_models_that_return_no_speakers_report_false(self) -> None:
        """Parsing speakers does not conjure them: Whisper produces none, and
        neither does OpenAI's realtime transcription model."""
        assert not supports_inline_diarization(ProviderKind.OPENROUTER, "openai/whisper-1")
        assert not supports_inline_diarization(ProviderKind.OPENAI_COMPAT, "whisper-1")
        assert not supports_inline_diarization(ProviderKind.OPENAI, "gpt-live-transcribe")

    def test_unknown_or_unset_model_is_false(self) -> None:
        """An uncurated model is exactly the case we cannot vouch for."""
        assert not supports_inline_diarization(ProviderKind.DEEPGRAM, None)
        assert not supports_inline_diarization(ProviderKind.DEEPGRAM, "some-future-model")


class TestGeminiSummarizeEfforts:
    """The reasoning levels Google's compatible endpoint actually accepted.

    Checked one request per model per level against the live API on 2026-09-02,
    not copied from the thinking-page table, which claims a uniform
    low/medium/high for the whole family and is wrong in both directions. These
    lists differ per model on purpose; a "tidy-up" that makes them uniform is
    the regression this test exists to catch.
    """

    def _efforts(self, model_id: str) -> list[str]:
        spec = config().provider(ProviderKind.GEMINI)
        assert spec is not None
        entry = spec.find(model_id)
        assert isinstance(entry, ModelSpec)
        assert entry.llm is not None
        return entry.llm.reasoning.selectable_efforts()

    def test_flash_takes_every_level_the_shim_offers(self) -> None:
        assert self._efforts("gemini-3.5-flash") == ["none", "minimal", "low", "medium", "high"]

    def test_3_8_flash_rejects_minimal(self) -> None:
        """400: "Thinking level MINIMAL is not supported for this model"."""
        assert "minimal" not in self._efforts("gemini-3.8-flash")

    @pytest.mark.parametrize("model_id", ["gemini-3.5-flash-lite", "gemini-3.1-pro-preview"])
    def test_thinking_only_models_never_offer_none(self, model_id: str) -> None:
        """3.1 Pro answers "Budget 0 is invalid. This model only works in
        thinking mode"; Flash-Lite refuses it with a bare invalid-argument."""
        assert "none" not in self._efforts(model_id)

    def test_openai_only_levels_are_offered_for_no_gemini_model(self) -> None:
        """ "Invalid reasoning_effort: xhigh. Valid values are: high, low,
        medium, minimal, none" - the shim validates against its own set before
        the model ever sees the request."""
        for model_id in ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-pro-preview"):
            assert not {"xhigh", "max"} & set(self._efforts(model_id))
