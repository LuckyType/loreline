<script lang="ts">
/**
 * "New transcription": run the stored audio through a provider again.
 *
 * Re-processing replays a recording, so it accepts every transcribe-capable
 * provider, including the ones live capture excludes, and it needs a model:
 * the provider row carries none, so there is nothing for the server to fall
 * back to.
 *
 * A dialog rather than a row at the foot of the version table, because the row
 * never fitted: a provider dropdown, a model picker, a glossary checkbox and a
 * button are four controls that wrap into a mess on a phone, and putting them
 * under a table meant scrolling a scrolling section to reach the one button
 * anybody came for. The button is in the section's header now and this is what
 * it opens. With no stored audio there is nothing to replay, so the caller
 * disables that button with the reason and this never opens at all.
 *
 * The component stays mounted while the dialog is shut, which is deliberate:
 * the provider and model picked last time are still picked on the next open,
 * and only the seeds below decide what a first open shows.
 */

import { TriangleAlert } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { featureBlockedReason, glossaryDropsWarning } from '$lib/capabilities.svelte'
import { Button } from '$lib/components/ui/button'
import { Checkbox } from '$lib/components/ui/checkbox'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import { Label } from '$lib/components/ui/label'
import Dropdown from '$lib/Dropdown.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'

let {
	open = $bindable(false),
	sessionId,
	capturedWith,
	onqueued,
	onerror,
}: {
	open?: boolean
	sessionId: string
	/** The provider the session was captured with, the seed's last resort. */
	capturedWith?: string | null
	/** A run has been queued: the caller refetches the job list. Awaited, so
	 *  the button stays busy until the new row is on screen. */
	onqueued?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

// On by default: re-processing always fed the campaign glossary to the
// provider, and turning it off is the deliberate choice. What is held is that
// choice, not the resulting flag - null while the GM has no opinion yet - so
// that "off because I said so" and "off because this model cannot take one"
// stay two different things. See useGlossary below.
let glossaryPick = $state<boolean | null>(null)
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
// Derived rather than cleared by an effect: a model that cannot receive a
// glossary suppresses it for exactly as long as it is selected, and picking a
// model that can take one again restores the standing intent. The effect that
// used to do this only ever turned the toggle off, so passing through such a
// model left the box unticked on the next model that could take the terms,
// with the row claiming a deliberate choice nobody had made.
const useGlossary = $derived(glossaryBlocked ? false : (glossaryPick ?? true))

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
		// Only on success: a run that failed to queue leaves the dialog up with
		// the picks still in it, next to the banner saying why.
		open = false
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'reprocess failed')
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>New transcription</DialogTitle>
			<DialogDescription>
				Runs this session's recording through a provider again and files the result as another
				version. It runs in the background - you can close this page.
			</DialogDescription>
		</DialogHeader>

		<div class="flex flex-col gap-4">
			<div class="flex flex-col gap-2">
				<Label for="reprocess-provider">Provider</Label>
				<Dropdown
					id="reprocess-provider"
					bind:value={provider}
					defaultValue={actionSetup.defaults.stt_provider}
					options={providers.map((p) => ({ value: p.id, label: p.name }))}
					placeholder="Provider"
				/>
			</div>

			<div class="flex flex-col gap-2">
				<Label for="reprocess-model">Model</Label>
				<ModelPicker
					id="reprocess-model"
					provider={selectedProvider}
					bind:value={model}
					defaultModel={storedDefault}
					interaction="transcribe"
				/>
			</div>

			<div class="flex flex-col gap-2">
				<label
					class="flex items-center gap-2"
					title={glossaryBlocked ||
						glossaryWarning ||
						"Sends the campaign's terms to the provider as keyterms or a prompt."}
				>
					<Checkbox
						checked={useGlossary}
						disabled={!!glossaryBlocked}
						onCheckedChange={(v) => (glossaryPick = v === true)}
					/>
					<span class={glossaryBlocked ? 'text-muted-foreground' : ''}>Use glossary</span>
					{#if glossaryWarning && !glossaryBlocked}
						<TriangleAlert
							class="size-3.5 shrink-0 text-amber-500"
							aria-label="Diarization quality warning"
						/>
					{/if}
				</label>
				<!-- The icon alone is a tooltip: spelled out here so the trade is
				     readable before the job is queued, not after the version comes
				     back unlabelled. -->
				{#if useGlossary && glossaryWarning}
					<p class="m-0 text-xs text-amber-500">{glossaryWarning}</p>
				{/if}
			</div>
		</div>

		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<!-- A model is required: the provider row carries none, so there is
			     nothing for the server to fall back to. -->
			<Button
				onclick={reprocess}
				disabled={busy || !provider || !model}
				title={provider && !model ? 'Pick a model to re-process with.' : ''}
			>
				{busy ? 'Queuing…' : 'Re-process audio'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
