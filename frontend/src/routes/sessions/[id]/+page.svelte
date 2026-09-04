<script lang="ts">
import { onMount } from 'svelte'
import { page } from '$app/state'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { reasoningEffortsFor } from '$lib/capabilities.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
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
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import Foldable from '$lib/Foldable.svelte'
import GenerateVideoDialog from '$lib/GenerateVideoDialog.svelte'
import { jsonFrame, LiveFeed } from '$lib/liveFeed.svelte'
import { modelInfoFor } from '$lib/modelCatalog.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import SessionHeader from '$lib/SessionHeader.svelte'
import { diarizerLabel, inFlight, providerName } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import TranscriptVersions from '$lib/TranscriptVersions.svelte'
import type {
	DiarizationModeKind,
	ReprocessJob,
	SessionDetail,
	TranscriptEvent,
	VideoJob,
} from '$lib/wire'

let detail = $state<SessionDetail | null>(null)
let jobs = $state<ReprocessJob[]>([])
let error = $state('')
let rpDiarKind = $state<DiarizationModeKind>('remote')
let rpDiarEndpoint = $state('')
let rpDiarMin = $state('')
let rpDiarMax = $state('')
let rpBusy = $state(false)

const id = $derived(page.params.id ?? '')
const hasAudio = $derived(!!detail?.session.audio_path)

/** Every card reports what went wrong here: one banner, at the top. */
function setError(message: string) {
	error = message
}

// Fold state of the page's sections, kept across visits (best effort).
const SECTIONS_KEY = 'loreline.session-sections'

function loadSections(): { table: boolean; transcript: boolean; summary: boolean } {
	const fallback = { table: true, transcript: true, summary: true }
	try {
		const raw = localStorage.getItem(SECTIONS_KEY)
		return raw ? { ...fallback, ...JSON.parse(raw) } : fallback
	} catch {
		return fallback
	}
}

let sections = $state(loadSections())

$effect(() => {
	const serialized = JSON.stringify(sections)
	try {
		localStorage.setItem(SECTIONS_KEY, serialized)
	} catch {
		/* best effort */
	}
})

// --- transcript versions ---
// A session's transcript exists in versions: the original live capture plus
// one per re-transcription job. The table lists them all; clicking a row
// selects one and everything below (details band, transcript, diarize
// target) follows it.
let selectedVersion = $state('original')

/** The selected version's segments, live.
 *
 * A running job publishes every segment it persists, so the version fills up
 * as it is written instead of appearing all at once when the job ends. The
 * event's `source` names the version that produced it, which is what keeps a
 * re-transcription's text out of the original and out of the other versions,
 * and the feed is filtered to this session, so it carries this session's runs
 * (and its live capture) and nothing else. */
const versionFeed = new LiveFeed<TranscriptEvent>({
	path: () => `/ws/transcript?session_id=${encodeURIComponent(id)}`,
	parse: jsonFrame,
	accept: (event, held) =>
		event.source === `reprocess:${selectedVersion}` &&
		// selectVersion's fetch and this socket can overlap by a segment.
		!held.some((e) => e.start_ts === event.start_ts && e.text === event.text),
})

const shownEvents = $derived(
	selectedVersion === 'original' ? (detail?.transcript ?? []) : versionFeed.items,
)

// The diarize job whose relabeling the selected version currently shows.
const diarizeJob = $derived(
	jobs
		.filter(
			(j) =>
				j.operation === 'diarize' &&
				(j.target ?? 'original') === selectedVersion &&
				j.status === 'done' &&
				j.segments_added > 0,
		)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0],
)

async function selectVersion(version: string) {
	selectedVersion = version
	try {
		versionFeed.items = version === 'original' ? [] : await api.getTranscriptVersion(id, version)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load transcript version'
	}
}

const selectedJob = $derived(
	jobs.find((j) => j.operation === 'transcribe' && j.id === selectedVersion),
)
const selectedProviderName = $derived(
	selectedVersion === 'original'
		? providerName(detail?.session.primary_provider, actionSetup.providers)
		: providerName(selectedJob?.provider_id, actionSetup.providers),
)
const selectedModel = $derived(selectedVersion === 'original' ? '-' : (selectedJob?.model ?? '-'))

// distinct speaker labels in the shown version (drives the rename button)
const speakers = $derived([
	...new Set(shownEvents.map((e) => e.speaker).filter((s): s is string => !!s)),
])

let renameOpen = $state(false)
let nameForm = $state<Record<string, string>>({})

// --- summarize ---
const llmProviders = $derived(actionSetup.providersFor('summarize'))
let summarizeOpen = $state(false)
// Seeded, not stored, the same rule every picker follows: the saved default
// while it still summarizes, else the first row that does.
let sumProvider = $derived(actionSetup.preferredProvider('summarize')?.id ?? '')
let sumEffort = $state('')
let sumBusy = $state(false)
let sumError = $state('')
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

// --- video generation ---
// Only OpenRouter can generate video (see supports_video in
// src/loreline/video/client.py); every other provider kind is filtered out
// rather than offered and rejected at submit time.
const videoProviders = $derived(actionSetup.providersFor('video'))
let videoOpen = $state(false)
let videoJobs = $state<VideoJob[]>([])

const videoRunning = $derived(
	videoJobs.some((j) => j.status === 'queued' || j.status === 'running'),
)

/** A generation takes minutes, so this polls only while something is actually
 *  in flight and stops as soon as the queue drains. Hanging the interval off an
 *  effect means the same teardown covers all three ways it should stop: the
 *  queue draining, the flag flipping, and the page unmounting. There is no
 *  timer left to clear by hand, and none left running behind a dead page. */
$effect(() => {
	if (!videoRunning) return
	const timer = setInterval(refreshVideoJobs, 5000)
	return () => clearInterval(timer)
})

async function refreshVideoJobs() {
	videoJobs = await api.listVideoJobs(id)
}

async function deleteVideo(jobId: string) {
	if (!(await confirm('Delete this video and its file?'))) return
	await api.deleteVideoJob(jobId)
	await refreshVideoJobs()
}

async function refreshJobs() {
	jobs = await api.listReprocess(id)
}

/** A finished run may have rewritten the selected version's rows, so the queue
 *  draining refetches them. */
async function reloadAfterJobs() {
	detail = await api.getSession(id)
	if (selectedVersion !== 'original') {
		versionFeed.items = await api.getTranscriptVersion(id, selectedVersion)
	}
}

const jobsRunning = $derived(jobs.some(inFlight))

// Whether the last run of the effect below saw a job in flight, which is what
// makes the refetch fire on the falling edge only. A plain variable and not
// $state on purpose: the effect writes it, and reactive state written inside an
// effect would schedule that effect to run itself again.
let jobsWereRunning = false

// The reprocess poll, the video poll's twin: keyed on whether anything is in
// flight, torn down by the effect on the falling edge and on unmount alike.
$effect(() => {
	if (!jobsRunning) {
		if (jobsWereRunning) {
			jobsWereRunning = false
			void reloadAfterJobs()
		}
		return
	}
	jobsWereRunning = true
	const timer = setInterval(refreshJobs, 1500)
	return () => clearInterval(timer)
})

async function diarizeSession() {
	rpBusy = true
	error = ''
	try {
		await api.enqueueReprocess({
			session_id: id,
			operation: 'diarize',
			target: selectedVersion,
			diarization: {
				mode: rpDiarKind,
				endpoint:
					rpDiarKind === 'remote'
						? rpDiarEndpoint || detail?.session.diarization.endpoint || null
						: null,
				min_speakers: rpDiarMin ? Number(rpDiarMin) : null,
				max_speakers: rpDiarMax ? Number(rpDiarMax) : null,
			},
		})
		await refreshJobs()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'diarize failed'
	} finally {
		rpBusy = false
	}
}

function openRename() {
	const names = detail?.session.speaker_names ?? {}
	nameForm = Object.fromEntries(speakers.map((s) => [s, names[s] ?? '']))
	renameOpen = true
}

async function saveNames() {
	const names: Record<string, string> = {}
	for (const [label, name] of Object.entries(nameForm)) {
		if (name.trim()) names[label] = name.trim()
	}
	try {
		await api.setSpeakerNames(id, names)
		detail = await api.getSession(id) // refresh speaker_names + transcript
		renameOpen = false
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'rename failed'
	}
}

async function openSummarize() {
	if (
		speakers.length === 0 &&
		!(await confirm('This session has no diarized speakers. Summarize anyway?'))
	)
		return
	sumError = ''
	summarizeOpen = true
}

async function runSummarize() {
	if (!sumProvider || !sumModel) return
	sumBusy = true
	sumError = ''
	try {
		await api.summarizeSession(id, {
			provider_id: sumProvider,
			model: sumModel,
			reasoning_effort: sumEfforts.length ? sumEffort || null : null,
		})
		detail = await api.getSession(id) // refresh to show the stored summary
		summarizeOpen = false
	} catch (err) {
		sumError = err instanceof ApiError ? err.message : 'summarize failed'
	} finally {
		sumBusy = false
	}
}

// Nothing here outlives the awaits: it only assigns state, so `async` is safe.
// Everything that had to be torn down (the socket, the two intervals) belongs
// to an effect above, exactly because a teardown returned from an async onMount
// is never registered.
onMount(async () => {
	// Providers, defaults and the capability gate: the seeds above are derived
	// from the store, so nothing here has to wait for it.
	void actionSetup.load()
	try {
		detail = await api.getSession(id)
		await refreshJobs()
		await refreshVideoJobs()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load'
	}
})
</script>

{#if error || actionSetup.error}
	<p class="mb-2 text-sm text-destructive">{error || actionSetup.error}</p>
{/if}

{#if !detail}
	<p class="text-muted-foreground">Loading…</p>
{:else}
	<Card>
		<SessionHeader sessionId={id} session={detail.session} />

		<div class="border-t"></div>

		<TranscriptVersions
			sessionId={id}
			{detail}
			{jobs}
			selected={selectedVersion}
			bind:open={sections.table}
			onselect={selectVersion}
			onchanged={refreshJobs}
			onerror={setError}
		/>

		<div class="border-t"></div>

		<CardContent class="flex flex-col gap-3">
			<Foldable
				title="Transcript"
				meta="{selectedVersion === 'original'
					? 'original'
					: selectedVersion.slice(0, 8)} · {shownEvents.length} segments"
				bind:open={sections.transcript}
			>
				<div
					class="flex flex-wrap items-center justify-between gap-3 rounded-md bg-accent/40 px-3 py-2.5"
				>
					<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
						<span
							>Transcript
							<code
								>{selectedVersion === 'original' ? 'original' : selectedVersion.slice(0, 8)}</code
							></span
						>
						<span><span class="text-muted-foreground">Provider</span> {selectedProviderName}</span>
						<span><span class="text-muted-foreground">Model</span> {selectedModel}</span>
						{#if diarizeJob}
							<span
								><span class="text-muted-foreground">Diarized with</span>
								{diarizerLabel(diarizeJob)}</span
							>
						{:else}
							<span class="text-muted-foreground">Not diarized</span>
						{/if}
						<span><span class="text-muted-foreground">Segments</span> {shownEvents.length}</span>
					</div>
					<div class="flex flex-wrap items-center gap-2">
						{#if hasAudio}
							<Dropdown
								class="max-w-52"
								bind:value={rpDiarKind}
								options={[
                { value: 'remote', label: 'sherpa-onnx' },
                { value: 'openai', label: 'gpt-4o-transcribe-diarize' }
              ]}
							/>
							{#if rpDiarKind === 'remote'}
								<Input class="max-w-48" placeholder="http://…:8001" bind:value={rpDiarEndpoint} />
							{/if}
							<Input
								class="max-w-20"
								type="number"
								min="1"
								placeholder="min"
								bind:value={rpDiarMin}
							/>
							<Input
								class="max-w-20"
								type="number"
								min="1"
								placeholder="max"
								bind:value={rpDiarMax}
							/>
							<Button
								variant="outline"
								size="sm"
								onclick={diarizeSession}
								disabled={rpBusy}
								title="Diarizes the full session audio and relabels the selected transcription; re-running replaces its previous diarization."
							>
								Diarize
							</Button>
							<div class="h-5 w-px bg-border"></div>
						{/if}
						<Button
							variant="outline"
							size="sm"
							onclick={openRename}
							disabled={speakers.length === 0}
						>
							Rename speakers
						</Button>
					</div>
				</div>
				<TranscriptList
					events={shownEvents}
					names={detail.session.speaker_names}
					providers={actionSetup.providers}
					showSource={selectedVersion === 'original'}
				/>
			</Foldable>
		</CardContent>

		<div class="border-t"></div>

		<CardContent class="flex flex-col gap-2">
			<Foldable
				title="Summary"
				meta={detail.session.summary && detail.session.summary_model
					? `${providerName(detail.session.summary_provider, actionSetup.providers)} · ${detail.session.summary_model}`
					: ''}
				bind:open={sections.summary}
			>
				{#if detail.session.summary}
					<p class="m-0 leading-relaxed whitespace-pre-wrap">{detail.session.summary}</p>
				{:else if llmProviders.length === 0}
					<p class="m-0 text-muted-foreground">
						Add an LLM provider (OpenAI-compatible chat) in Settings to enable summaries.
					</p>
				{:else}
					<p class="m-0 text-muted-foreground">Not summarized yet.</p>
				{/if}
				<div class="flex justify-end gap-2">
					<Button
						variant="outline"
						size="sm"
						onclick={() => (videoOpen = true)}
						disabled={videoProviders.length === 0 || !detail.session.summary}
						title={videoProviders.length === 0
							? 'Add an OpenRouter provider in Settings'
							: !detail.session.summary
								? 'Summarize the session first'
								: ''}
					>
						Generate video
					</Button>
					<Button
						variant="outline"
						size="sm"
						onclick={openSummarize}
						disabled={llmProviders.length === 0}
					>
						{detail.session.summary ? 'Re-summarize' : 'Summarize'}
					</Button>
				</div>

				{#if videoJobs.length}
					<div class="mt-3 flex flex-col gap-3 border-t pt-3">
						{#each videoJobs as job (job.id)}
							<div class="flex flex-col gap-2">
								<div class="flex items-center justify-between gap-2">
									<span class="min-w-0 truncate text-xs text-muted-foreground">
										{job.model}{job.duration ? ` · ${job.duration}s` : ''}
										{job.resolution
											? ` · ${job.resolution}`
											: ''}
									</span>
									<span class="flex shrink-0 items-center gap-2">
										{#if job.status === 'queued' || job.status === 'running'}
											<span class="text-xs text-muted-foreground">Generating…</span>
										{:else if job.status === 'error'}
											<span class="text-xs text-destructive">{job.error ?? 'failed'}</span>
										{/if}
										<Button variant="ghost" size="sm" onclick={() => deleteVideo(job.id)}>
											Delete
										</Button>
									</span>
								</div>
								{#if job.status === 'done'}
									<!-- svelte-ignore a11y_media_has_caption -->
									<video
										class="w-full rounded-md border"
										controls
										preload="metadata"
										src={api.videoContentUrl(job.id)}
									></video>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</Foldable>
		</CardContent>
	</Card>
{/if}

<GenerateVideoDialog
	bind:open={videoOpen}
	sessionId={id}
	summary={detail?.session.summary ?? ''}
	onqueued={refreshVideoJobs}
/>
<Dialog bind:open={renameOpen}>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Rename speakers</DialogTitle>
			<DialogDescription>
				Set a display name for each detected speaker; blank keeps the original label. Applied in the
				transcript and exports.
			</DialogDescription>
		</DialogHeader>
		<div class="flex flex-col gap-3">
			{#each speakers as s, i (s)}
				<div class="flex flex-col gap-2">
					<Label for="speaker-name-{i}">{s}</Label>
					<Input id="speaker-name-{i}" bind:value={nameForm[s]} placeholder={s} />
				</div>
			{/each}
		</div>
		<DialogFooter>
			<Button variant="outline" onclick={() => (renameOpen = false)}>Cancel</Button>
			<Button onclick={saveNames}>Save names</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>

<Dialog bind:open={summarizeOpen}>
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
			<Button variant="outline" onclick={() => (summarizeOpen = false)}>Cancel</Button>
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
