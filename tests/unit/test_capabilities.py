"""Which provider+model combinations are offered for which interaction.

The regression these guard is concrete: the pickers used to offer everything a
provider's ``/models`` returned, so a GM could pick ``dall-e-3`` to transcribe
with and only discover the mistake when the job failed.
"""

from __future__ import annotations

import pytest

from loreline.capabilities import (
    INTERACTIONS_BY_KIND,
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
        """Configs stored before per-model resolution carry no model; they must
        keep running exactly the connector they always got."""
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
