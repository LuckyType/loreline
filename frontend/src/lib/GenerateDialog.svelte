<script lang="ts">
/**
 * Ask a model for a text: a summary, a recap, an extraction, a "previously on".
 *
 * One dialog for all four because the choice in front of the GM is the same
 * every time - which LLM provider, which model, how hard it should think - and
 * only the verb and the destination differ. It was the summarize dialog alone;
 * copying it three times is how the reasoning-effort rule below (offer only the
 * levels this model accepts, and forget a level the next model cannot take)
 * ends up correct in one of four places.
 *
 * The provider and model are seeded from the stored summarize default and
 * overridden by a pick, the same rule every other picker follows. Reasoning
 * effort is the one control that comes and goes: only the levels this model
 * accepts are offered, and a model that reasons without exposing levels gets
 * no selector at all rather than a dead one.
 *
 * What it reads is said before the pickers. For the three session kinds that
 * is the transcript version the page is showing: a session has one of each of
 * these but several transcripts, and summarizing used to read the live capture
 * no matter which re-transcription was on screen, silently. The campaign kind
 * reads recaps instead, and says so.
 *
 * Running it is the caller's: this owns the choice, not the request, so the
 * page that knows which session or campaign it is about is the one that sends
 * it and the one that stores what came back.
 */

import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError } from '$lib/api'
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
import { versionLabel } from '$lib/stores'
import type { GenerateKind } from '$lib/types'
import type { GenerateRequest } from '$lib/wire'

let {
	open = $bindable(false),
	kind,
	speakers = [],
	version = '',
	onrun,
}: {
	open?: boolean
	/** Which text is being asked for. Decides the wording and nothing else. */
	kind: GenerateKind
	/** The distinct speaker labels in the shown transcript: with none, a recap
	 *  cannot say who said what, and the dialog warns about it. Empty for the
	 *  campaign kind, which reads recaps rather than a transcript. */
	speakers?: string[]
	/** The transcript version to read: 'original', or a re-transcription's job
	 *  id. Whatever the page is showing, which is the one the reader was looking
	 *  at when they pressed the button. '' for the campaign kind. */
	version?: string
	/** Send the request and store what comes back. Awaited, so the dialog only
	 *  closes once what it produced is on screen; anything it throws is shown
	 *  here rather than in the page's banner, because this is where the button
	 *  that caused it is. */
	onrun: (body: GenerateRequest) => Promise<void>
} = $props()

/** What this dialog calls itself, per kind: the heading, the button, and the
 *  present participle the button wears while it is running. One table, so a
 *  kind cannot be titled "Summarize session" and act on a recap. */
const WORDING: Record<GenerateKind, { title: string; verb: string; running: string }> = {
	summary: { title: 'Summarize session', verb: 'Summarize', running: 'Summarizing…' },
	recap: { title: 'Write recap', verb: 'Write recap', running: 'Writing…' },
	extraction: { title: 'Extract names', verb: 'Extract', running: 'Extracting…' },
	'previously-on': { title: 'Write "previously on"', verb: 'Write', running: 'Writing…' },
}

const wording = $derived(WORDING[kind])
const readsATranscript = $derived(kind !== 'previously-on')

const llmProviders = $derived(actionSetup.providersFor('summarize'))
// Seeded, not stored, same rule as every other picker: the saved default while
// it still summarizes, else the first row that does.
let provider = $derived(actionSetup.preferredProvider('summarize')?.id ?? '')
let effort = $state('')
let busy = $state(false)
let error = $state('')

// Each opening starts clean: a failure from the last attempt is not news about
// this one.
$effect(() => {
	if (open) error = ''
})

const selected = $derived(llmProviders.find((p) => p.id === provider))
// The summarize default as a pair: the model half only counts while its
// provider half is the one selected.
const pairedDefault = $derived(actionSetup.pairedDefault('summarize', selected))
let model = $derived(actionSetup.preferredModelFor('summarize', selected))
// The picked model's catalogue entry; the fallback for a model the capability
// config does not annotate. Read from the shared catalogue rather than
// reported by the picker, which the dialog unmounts on close: the entry must
// outlive the picker or the effort selector vanishes on reopen.
const modelInfo = $derived(modelInfoFor(selected, 'summarize', '', model))
// The levels this model actually accepts, in config order. Empty means no
// dropdown at all: either it does not reason, or it reasons without exposing
// levels, and an empty selector would be a dead control in both cases.
const efforts = $derived(reasoningEffortsFor(selected?.kind, model, modelInfo?.supports_reasoning))

// A level carried over from another model that does not offer it would fail
// the request. An empty list means the selector is hidden and nothing is sent,
// so leave the pick alone rather than forgetting it on the way past.
$effect(() => {
	if (efforts.length && effort && !efforts.includes(effort)) effort = ''
})

const uid = $props.id()

async function run() {
	if (!provider || !model) return
	busy = true
	error = ''
	try {
		await onrun({
			provider_id: provider,
			model,
			reasoning_effort: efforts.length ? effort || null : null,
			// The endpoint spells "the live capture" as null rather than as the
			// string 'original', so say it the way the contract says it.
			version: readsATranscript && version !== 'original' ? version : null,
		})
		open = false
	} catch (err) {
		error = err instanceof ApiError ? err.message : `${wording.verb.toLowerCase()} failed`
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>{wording.title}</DialogTitle>
			<!-- Named at the point of action, not after the fact: on a session with
			     five transcripts this is the only thing that says which one the
			     text will be about. -->
			<DialogDescription>
				{#if readsATranscript}
					Reads transcript <code>{versionLabel(version || 'original')}</code>, the version on
					screen.
				{:else}
					Reads the campaign's most recent recaps, falling back to summaries.
				{/if}
			</DialogDescription>
			{#if readsATranscript && speakers.length === 0}
				<DialogDescription class="text-destructive">
					No diarized speakers - the text won't distinguish who said what.
				</DialogDescription>
			{/if}
		</DialogHeader>
		<div class="flex flex-col gap-2">
			<Label for="{uid}-provider">LLM provider</Label>
			<Dropdown
				id="{uid}-provider"
				bind:value={provider}
				defaultValue={actionSetup.defaults.summarize_provider}
				options={llmProviders.map((p) => ({ value: p.id, label: p.name }))}
				placeholder="LLM provider"
			/>
		</div>
		<div class="mt-3 flex flex-col gap-2">
			<Label for="{uid}-model">Model</Label>
			<ModelPicker
				id="{uid}-model"
				provider={selected}
				bind:value={model}
				defaultModel={pairedDefault}
				interaction="summarize"
			/>
		</div>
		{#if efforts.length}
			<div class="mt-3 flex flex-col gap-2">
				<Label for="{uid}-effort">Reasoning effort</Label>
				<Dropdown
					id="{uid}-effort"
					bind:value={effort}
					defaultValue={actionSetup.defaults.summarize_reasoning_effort}
					options={[
						{ value: '', label: "Model's default" },
						...efforts.map((e) => ({ value: e, label: e })),
					]}
				/>
				<span class="text-xs text-muted-foreground">
					Higher effort means a slower, more expensive, usually better answer.
				</span>
			</div>
		{/if}
		{#if error}
			<p class="mt-2 text-sm text-destructive">{error}</p>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<!-- Same rule as re-processing: the model is chosen here or nowhere. -->
			<Button
				onclick={run}
				disabled={busy || !provider || !model}
				title={provider && !model ? 'Pick a model to run this with.' : ''}
			>
				{busy ? wording.running : wording.verb}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
