<script lang="ts">
/**
 * Summarize a session with an LLM.
 *
 * The provider and model are seeded from the stored summarize default and
 * overridden by a pick, the same rule every other picker follows. Reasoning
 * effort is the one control that comes and goes: only the levels this model
 * accepts are offered, and a model that reasons without exposing levels gets
 * no selector at all rather than a dead one.
 */

import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { reasoningEffortsFor } from '$lib/capabilities.svelte'
import { Button } from '$lib/components/ui/button'
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
import { modelInfoFor } from '$lib/modelCatalog.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'

let {
	open = $bindable(false),
	sessionId,
	speakers,
	onsummarized,
}: {
	open?: boolean
	sessionId: string
	/** The distinct speaker labels in the shown transcript: with none, the
	 *  summary cannot say who said what, and the dialog warns about it. */
	speakers: string[]
	/** A summary was stored: the caller refetches the session. Awaited, so the
	 *  dialog only closes once the summary behind it is on screen. */
	onsummarized?: () => Promise<void> | void
} = $props()

const llmProviders = $derived(actionSetup.providersFor('summarize'))
// Seeded, not stored, same rule as every other picker: the saved default while
// it still summarizes, else the first row that does.
let sumProvider = $derived(actionSetup.preferredProvider('summarize')?.id ?? '')
let sumEffort = $state('')
let sumBusy = $state(false)
let sumError = $state('')

// Each opening starts clean: a failure from the last attempt is not news about
// this one.
$effect(() => {
	if (open) sumError = ''
})

const selectedLlm = $derived(llmProviders.find((p) => p.id === sumProvider))
// The summarize default as a pair: the model half only counts while its
// provider half is the one selected.
const sumDefault = $derived(actionSetup.pairedDefault('summarize', selectedLlm))
let sumModel = $derived(actionSetup.preferredModelFor('summarize', selectedLlm))
// The picked model's catalogue entry; the fallback for a model the capability
// config does not annotate. Read from the shared catalogue rather than
// reported by the picker, which the dialog unmounts on close: the entry must
// outlive the picker or the effort selector vanishes on reopen.
const sumModelInfo = $derived(modelInfoFor(selectedLlm, 'summarize', '', sumModel))
// The levels this model actually accepts, in config order. Empty means no
// dropdown at all: either it does not reason, or it reasons without exposing
// levels, and an empty selector would be a dead control in both cases.
const sumEfforts = $derived(
	reasoningEffortsFor(selectedLlm?.kind, sumModel, sumModelInfo?.supports_reasoning),
)

// A level carried over from another model that does not offer it would fail
// the request. An empty list means the selector is hidden and nothing is sent,
// so leave the pick alone rather than forgetting it on the way past.
$effect(() => {
	if (sumEfforts.length && sumEffort && !sumEfforts.includes(sumEffort)) sumEffort = ''
})

async function runSummarize() {
	if (!sumProvider || !sumModel) return
	sumBusy = true
	sumError = ''
	try {
		await api.summarizeSession(sessionId, {
			provider_id: sumProvider,
			model: sumModel,
			reasoning_effort: sumEfforts.length ? sumEffort || null : null,
		})
		await onsummarized?.()
		open = false
	} catch (err) {
		sumError = err instanceof ApiError ? err.message : 'summarize failed'
	} finally {
		sumBusy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Summarize session</DialogTitle>
			{#if speakers.length === 0}
				<DialogDescription class="text-destructive">
					No diarized speakers - the summary won't distinguish who said what.
				</DialogDescription>
			{/if}
		</DialogHeader>
		<div class="flex flex-col gap-2">
			<Label for="summarize-provider">LLM provider</Label>
			<Dropdown
				id="summarize-provider"
				bind:value={sumProvider}
				defaultValue={actionSetup.defaults.summarize_provider}
				options={llmProviders.map((p) => ({ value: p.id, label: p.name }))}
				placeholder="LLM provider"
			/>
		</div>
		<div class="mt-3 flex flex-col gap-2">
			<Label for="summarize-model">Model</Label>
			<ModelPicker
				id="summarize-model"
				provider={selectedLlm}
				bind:value={sumModel}
				defaultModel={sumDefault}
				interaction="summarize"
			/>
		</div>
		{#if sumEfforts.length}
			<div class="mt-3 flex flex-col gap-2">
				<Label for="summarize-effort">Reasoning effort</Label>
				<Dropdown
					id="summarize-effort"
					bind:value={sumEffort}
					defaultValue={actionSetup.defaults.summarize_reasoning_effort}
					options={[
						{ value: '', label: "Model's default" },
						...sumEfforts.map((e) => ({ value: e, label: e })),
					]}
				/>
				<span class="text-xs text-muted-foreground">
					Higher effort means a slower, more expensive, usually better summary.
				</span>
			</div>
		{/if}
		{#if sumError}
			<p class="mt-2 text-sm text-destructive">{sumError}</p>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<!-- Same rule as re-processing: the model is chosen here or nowhere. -->
			<Button
				onclick={runSummarize}
				disabled={sumBusy || !sumProvider || !sumModel}
				title={sumProvider && !sumModel ? 'Pick a model to summarize with.' : ''}
			>
				{sumBusy ? 'Summarizing…' : 'Summarize'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
