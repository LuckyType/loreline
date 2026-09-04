<script lang="ts">
/**
 * "New transcription": run the stored audio through a provider again.
 *
 * Re-processing replays a recording, so it accepts every transcribe-capable
 * provider, including the ones live capture excludes, and it needs a model:
 * the provider row carries none, so there is nothing for the server to fall
 * back to. With no stored audio there is nothing to replay, and the panel says
 * that instead of offering controls that cannot work.
 */

import { TriangleAlert } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { featureBlockedReason, glossaryDropsWarning } from '$lib/capabilities.svelte'
import { Button } from '$lib/components/ui/button'
import { Checkbox } from '$lib/components/ui/checkbox'
import Dropdown from '$lib/Dropdown.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'

let {
	sessionId,
	hasAudio,
	capturedWith,
	onqueued,
	onerror,
}: {
	sessionId: string
	hasAudio: boolean
	/** The provider the session was captured with, the seed's last resort. */
	capturedWith?: string | null
	/** A run has been queued: the caller refetches the job list. Awaited, so
	 *  the button stays busy until the new row is on screen. */
	onqueued?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

// On by default: re-processing always fed the campaign glossary to the
// provider, and turning it off is the deliberate choice.
let useGlossary = $state(true)
let busy = $state(false)

/** Re-processing replays stored audio, so it accepts every transcribe-capable
 *  provider - including the ones excluded from live capture. */
const providers = $derived(actionSetup.providersFor('transcribe'))

/** Which provider the re-process row comes up on. Seeded, not stored: a pick
 * overrides it.
 *
 * The stored transcription default wins: Settings promises it is pre-selected
 * "when starting or re-processing a session", and it is the only way to say
 * "re-run my sessions on the batch provider". Re-running whatever captured the
 * session is the fallback, not the rule - a capture provider is by definition
 * one that can drive a live session, so preferring it buried the batch
 * providers behind a manual switch every single time. A default naming a
 * provider that has since been deleted (or lost its transcribe ability) is
 * ignored rather than selected into a dead id. */
let provider = $derived(actionSetup.preferredProvider('transcribe', capturedWith)?.id ?? '')
const selectedProvider = $derived(actionSetup.provider(provider))
// The stored transcription default is a provider/model pair: its model half
// only counts while its provider half is the one selected.
const storedDefault = $derived(actionSetup.pairedDefault('transcribe', selectedProvider))
// Seeded, not stored: a pick overrides this until the provider changes, and a
// provider switch starts over (see preferredModel).
let model = $derived(actionSetup.preferredModelFor('transcribe', selectedProvider))
// Not every model can take a glossary: OpenRouter's transcription API accepts
// a prompt field and ignores it, so the checkbox there was a silent no-op.
// Disabled with the reason, rather than left to do nothing.
const glossaryBlocked = $derived(featureBlockedReason(selectedProvider?.kind, model, 'glossary'))
// A model that takes a glossary but refuses to combine it with word
// timestamps is a different case: usable, and the backend keeps the terms and
// drops the timestamps. It costs speaker attribution quality, so the toggle
// carries a warning instead of being greyed out. This is the panel the
// original report came from: every utterance of a Gemini re-process failed
// with a 400 and the GM had no way to see why beforehand.
const glossaryWarning = $derived(glossaryDropsWarning(selectedProvider?.kind, model))

$effect(() => {
	if (useGlossary && glossaryBlocked) useGlossary = false
})

async function reprocess() {
	if (!provider || !model) return
	busy = true
	onerror?.('')
	try {
		await api.enqueueReprocess({
			session_id: sessionId,
			provider_id: provider,
			model,
			use_glossary: useGlossary,
		})
		await onqueued?.()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'reprocess failed')
	} finally {
		busy = false
	}
}
</script>

{#if hasAudio}
	<div class="flex flex-wrap items-center justify-end gap-2">
		<span class="text-muted-foreground">New transcription</span>
		<Dropdown
			class="max-w-52"
			bind:value={provider}
			defaultValue={actionSetup.defaults.stt_provider}
			options={providers.map((p) => ({ value: p.id, label: p.name }))}
			placeholder="Provider"
		/>
		<ModelPicker
			provider={selectedProvider}
			bind:value={model}
			defaultModel={storedDefault}
			interaction="transcribe"
		/>
		<label
			class="flex items-center gap-2"
			title={glossaryBlocked ||
				glossaryWarning ||
				"Sends the campaign's terms to the provider as keyterms or a prompt."}
		>
			<Checkbox
				checked={useGlossary}
				disabled={!!glossaryBlocked}
				onCheckedChange={(v) => (useGlossary = v === true)}
			/>
			<span class={glossaryBlocked ? 'text-muted-foreground' : ''}>Use glossary</span>
			{#if glossaryWarning && !glossaryBlocked}
				<TriangleAlert
					class="size-3.5 shrink-0 text-amber-500"
					aria-label="Diarization quality warning"
				/>
			{/if}
		</label>
		<!-- A model is required: the provider row carries none, so
		     there is nothing for the server to fall back to. -->
		<Button
			variant="outline"
			onclick={reprocess}
			disabled={busy || !provider || !model}
			title={provider && !model ? 'Pick a model to re-process with.' : ''}
		>
			{busy ? 'Queuing…' : 'Re-process audio'}
		</Button>
	</div>
	<!-- The icon alone is a tooltip, and the row is too narrow for the
	     sentence: spelled out here so the trade is readable before the
	     job is queued, not after the version comes back unlabelled. -->
	{#if useGlossary && glossaryWarning}
		<p class="text-right text-xs text-amber-500">{glossaryWarning}</p>
	{/if}
{:else}
	<p class="text-muted-foreground">
		No stored audio for this session - re-processing and diarization are unavailable.
	</p>
{/if}
