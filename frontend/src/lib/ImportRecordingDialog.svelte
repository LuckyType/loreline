<script lang="ts">
/**
 * "Import recording": a session recorded somewhere else, stored here.
 *
 * This is the dialog for a GM with no box and no microphone at the table -
 * they recorded the evening on a phone, and what comes out the other end is a
 * session indistinguishable from a captured one (see docs/adr/0008). So the
 * form asks for exactly three things and no more: which file, when the session
 * was, and whether to start transcribing it now.
 *
 * The date is seeded from the file's own modification time rather than from
 * now, because that is the closest thing a recording carries to when the
 * evening happened, and it is right far more often than "now" is - an import
 * is usually done days later, and a session filed under the wrong date is
 * wrong in the history list forever.
 *
 * "Transcribe now" reveals the same provider and model pickers the New
 * transcription dialog uses, and for the same reason: transcribing an import
 * *is* an ordinary re-processing job. It is optional because the recording is
 * worth storing on its own - a GM who has not decided which model to spend on
 * yet can upload tonight and choose later, from the session page.
 *
 * The upload goes through XMLHttpRequest (see `api.importRecording`) purely so
 * this can draw a progress bar: a four hour recording is hundreds of MB, and a
 * button that says nothing for four minutes reads as a hang. The bar reaches
 * the end before the request does, because the server still has to decode and
 * index the audio after the last byte lands - which is what the line under it
 * says while that happens.
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
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import { Switch } from '$lib/components/ui/switch'
import Dropdown from '$lib/Dropdown.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import type { ImportedSession } from '$lib/wire'

let {
	open = $bindable(false),
	campaignId = null,
	onimported,
}: {
	open?: boolean
	/** The campaign the new session belongs to, sent verbatim. No picker here:
	 *  the field exists so one can be added above this without touching the
	 *  request. */
	campaignId?: string | null
	/** The import succeeded: the caller decides where to go next. */
	onimported?: (result: ImportedSession) => Promise<void> | void
} = $props()

// What the file types an upload may offer. Deliberately wider than the
// extensions: a phone names its recordings in its own way, and the server
// decides what it can actually decode.
const ACCEPT = 'audio/*,.m4a,.mp3,.ogg,.opus,.webm,.flac,.wav'

let fileInput = $state<HTMLInputElement | null>(null)
let file = $state<File | null>(null)
/** The session's start, as a `datetime-local` value in the browser's own zone. */
let when = $state('')
let transcribeNow = $state(false)
let busy = $state(false)
let sent = $state(0)
let total = $state(0)
let error = $state('')

// Held as the GM's standing choice rather than as the resulting flag, exactly
// as the New transcription dialog holds it: "off because I said so" and "off
// because this model cannot take one" are different facts.
let glossaryPick = $state<boolean | null>(null)

/** An import replays stored audio, so every transcribe-capable provider
 *  qualifies - including the batch-only ones live capture excludes. */
const providers = $derived(actionSetup.providersFor('transcribe'))
// Seeded, not stored: a pick overrides these until the provider changes.
let provider = $derived(actionSetup.preferredProvider('transcribe')?.id ?? '')
const selectedProvider = $derived(actionSetup.provider(provider))
const storedDefault = $derived(actionSetup.pairedDefault('transcribe', selectedProvider))
let model = $derived(actionSetup.preferredModelFor('transcribe', selectedProvider))
const glossaryBlocked = $derived(featureBlockedReason(selectedProvider?.kind, model, 'glossary'))
const glossaryWarning = $derived(glossaryDropsWarning(selectedProvider?.kind, model))
const useGlossary = $derived(glossaryBlocked ? false : (glossaryPick ?? true))

const percent = $derived(total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0)
/** What the bar is waiting on. The upload finishing is not the import
 *  finishing: the server still decodes the audio and runs the VAD over it,
 *  which on a long recording is the slower half. */
const stage = $derived(
	!busy ? '' : total > 0 && sent >= total ? 'Decoding and indexing…' : 'Uploading…',
)
const ready = $derived(!!file && !busy && (!transcribeNow || (!!provider && !!model)))

function pad(value: number): string {
	return String(value).padStart(2, '0')
}

/** An epoch in milliseconds as the local `datetime-local` string an input takes. */
function toLocalInput(ms: number): string {
	const at = new Date(ms)
	return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`
}

/** That string back as epoch seconds, or undefined when it is blank or
 *  nonsense - in which case the server dates the session now, which is what
 *  its own default does. */
function startedAt(local: string): number | undefined {
	const ms = new Date(local).getTime()
	return Number.isNaN(ms) ? undefined : ms / 1000
}

function pickFile(event: Event & { currentTarget: HTMLInputElement }) {
	file = event.currentTarget.files?.[0] ?? null
	error = ''
	if (file) when = toLocalInput(file.lastModified)
}

/** Put the form back to empty, so the next open is not the last import's.
 *  The element's own value has to be cleared by hand: a file input keeps its
 *  selection, and `file` alone going null would leave the name on screen. */
function reset() {
	file = null
	when = ''
	sent = 0
	total = 0
	if (fileInput) fileInput.value = ''
}

async function submit() {
	if (!file || busy) return
	busy = true
	error = ''
	sent = 0
	total = file.size
	try {
		const result = await api.importRecording(file, {
			startedAt: startedAt(when),
			campaignId,
			transcribe: transcribeNow
				? { provider_id: provider, model, use_glossary: useGlossary }
				: null,
			onProgress: (progress) => {
				sent = progress.sent
				total = progress.total
			},
		})
		// Only on success: a refused upload leaves the dialog up with the file
		// still chosen, next to the sentence saying why.
		open = false
		reset()
		await onimported?.(result)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'the import failed'
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Import recording</DialogTitle>
			<DialogDescription>
				Stores a recording you made elsewhere as a session. Everything else works on it the same
				way: transcribe, diarize, rename speakers, summarize, export.
			</DialogDescription>
		</DialogHeader>

		<div class="flex flex-col gap-4">
			<div class="flex flex-col gap-2">
				<Label for="import-file">Recording</Label>
				<Input
					id="import-file"
					type="file"
					accept={ACCEPT}
					bind:ref={fileInput}
					disabled={busy}
					onchange={pickFile}
				/>
				<p class="m-0 text-xs text-muted-foreground">
					m4a, mp3, ogg, opus, webm, flac or wav. Everything but wav needs ffmpeg on the server.
				</p>
			</div>

			<div class="flex flex-col gap-2">
				<Label for="import-when">Session date</Label>
				<Input id="import-when" type="datetime-local" bind:value={when} disabled={busy} />
				<p class="m-0 text-xs text-muted-foreground">
					Taken from the file, which is usually when it was recorded. Left blank, the session is
					dated now.
				</p>
			</div>

			<label class="flex items-center gap-2" title="Starts the first transcription right away.">
				<Switch bind:checked={transcribeNow} disabled={busy} />
				<span>Transcribe now</span>
			</label>

			{#if transcribeNow}
				<div class="flex flex-col gap-2">
					<Label for="import-provider">Provider</Label>
					<Dropdown
						id="import-provider"
						bind:value={provider}
						defaultValue={actionSetup.defaults.stt_provider}
						options={providers.map((p) => ({ value: p.id, label: p.name }))}
						placeholder="Provider"
					/>
				</div>

				<div class="flex flex-col gap-2">
					<Label for="import-model">Model</Label>
					<ModelPicker
						id="import-model"
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
								class="size-3.5 shrink-0 text-amber-700 dark:text-amber-500"
								aria-label="Diarization quality warning"
							/>
						{/if}
					</label>
					{#if useGlossary && glossaryWarning}
						<p class="m-0 text-xs text-amber-700 dark:text-amber-500">{glossaryWarning}</p>
					{/if}
				</div>
			{/if}

			{#if busy}
				<div class="flex flex-col gap-1">
					<div
						class="h-2 w-full overflow-hidden rounded-full bg-muted"
						role="progressbar"
						aria-label="Upload progress"
						aria-valuenow={percent}
						aria-valuemin={0}
						aria-valuemax={100}
					>
						<div class="h-full bg-primary transition-[width]" style:width="{percent}%"></div>
					</div>
					<p class="m-0 text-xs text-muted-foreground">{stage} {percent}%</p>
				</div>
			{/if}

			{#if error}
				<p class="m-0 text-sm text-destructive">{error}</p>
			{/if}
		</div>

		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)} disabled={busy}>Cancel</Button>
			<Button
				onclick={submit}
				disabled={!ready}
				title={transcribeNow && provider && !model ? 'Pick a model to transcribe with.' : ''}
			>
				{busy ? 'Importing…' : 'Import'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
