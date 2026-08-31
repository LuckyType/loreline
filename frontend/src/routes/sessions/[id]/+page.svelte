<script lang="ts">
import { ChevronDown } from '@lucide/svelte'
import { onDestroy, onMount } from 'svelte'
import { page } from '$app/stores'
import { ApiError, api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
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
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import Foldable from '$lib/Foldable.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import { providerName } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import {
	isLlmProvider,
	type ActionDefaults,
	type DiarizationModeKind,
	type ExportFormat,
	type ProviderConfig,
	type ReprocessJob,
	type SessionDetail,
	type TranscriptEvent,
} from '$lib/types'

let detail = $state<SessionDetail | null>(null)
let providers = $state<ProviderConfig[]>([])
let jobs = $state<ReprocessJob[]>([])
let error = $state('')
let rpProvider = $state('')
let rpModel = $state('')
let rpDiarKind = $state<DiarizationModeKind>('remote')
let rpDiarEndpoint = $state('')
let rpDiarMin = $state('')
let rpDiarMax = $state('')
let rpBusy = $state(false)
let poll: ReturnType<typeof setInterval> | undefined

const id = $derived($page.params.id ?? '')
const formats: ExportFormat[] = ['txt', 'md', 'srt', 'vtt', 'json']
const formatLabels: Record<ExportFormat, string> = {
	txt: 'Text (.txt)',
	md: 'Markdown (.md)',
	srt: 'Subtitles (.srt)',
	vtt: 'Subtitles (.vtt)',
	json: 'JSON (.json)',
}
const hasAudio = $derived(!!detail?.session.audio_path)
const rpSelectedProvider = $derived(providers.find((p) => p.id === rpProvider))

let exportOpen = $state(false)

function exportAs(fmt: ExportFormat) {
	exportOpen = false
	window.location.href = api.exportUrl(id, fmt)
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
let versionEvents = $state<TranscriptEvent[] | null>(null)

const transcribeJobs = $derived(
	jobs.filter((j) => j.operation === 'transcribe').sort((a, b) => a.created_at - b.created_at),
)

const shownEvents = $derived(
	selectedVersion === 'original' ? (detail?.transcript ?? []) : (versionEvents ?? []),
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

// The most recent failure, surfaced under the table (there is no error column).
const lastJobError = $derived(
	jobs.filter((j) => j.error).sort((a, b) => b.created_at - a.created_at)[0],
)

async function selectVersion(version: string) {
	selectedVersion = version
	try {
		versionEvents = version === 'original' ? null : await api.getTranscriptVersion(id, version)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load transcript version'
	}
}

function selectable(job: ReprocessJob): boolean {
	return job.status === 'done' && job.segments_added > 0
}

function diarizerLabel(job: ReprocessJob): string {
	if (job.diarization.mode === 'openai') return 'OpenAI · gpt-4o-transcribe-diarize'
	if (job.diarization.mode === 'remote')
		return `sherpa-onnx${job.diarization.endpoint ? ` · ${job.diarization.endpoint}` : ''}`
	return job.diarization.mode
}

// What the table's Diarization column says about one version.
function diarizeInfo(version: string): string {
	const targeting = jobs.filter(
		(j) => j.operation === 'diarize' && (j.target ?? 'original') === version,
	)
	if (targeting.some((j) => j.status === 'queued' || j.status === 'running')) return 'diarizing…'
	const done = targeting
		.filter((j) => j.status === 'done' && j.segments_added > 0)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0]
	return done ? diarizerLabel(done) : '-'
}

function fmtWhen(ts: number): string {
	return new Date(ts * 1000).toLocaleString()
}

const durationText = $derived.by(() => {
	const s = detail?.session
	if (!s?.ended_at) return ''
	const secs = Math.max(0, Math.floor(s.ended_at - s.started_at))
	const h = Math.floor(secs / 3600)
	const m = Math.floor((secs % 3600) / 60)
	const sec = String(secs % 60).padStart(2, '0')
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
})

const selectedJob = $derived(transcribeJobs.find((j) => j.id === selectedVersion))
const selectedProviderName = $derived(
	selectedVersion === 'original'
		? providerName(detail?.session.primary_provider, providers)
		: providerName(selectedJob?.provider_id, providers),
)
const selectedModel = $derived(selectedVersion === 'original' ? '-' : (selectedJob?.model ?? '-'))

// distinct speaker labels in the shown version (drives the rename button)
const speakers = $derived([
	...new Set(shownEvents.map((e) => e.speaker).filter((s): s is string => !!s)),
])

let renameOpen = $state(false)
let nameForm = $state<Record<string, string>>({})

// --- summarize ---
const llmProviders = $derived(providers.filter((p) => isLlmProvider(p)))
let summarizeOpen = $state(false)
let sumProvider = $state('')
let sumModel = $state('')
let sumBusy = $state(false)
let sumError = $state('')
let defaults = $state<ActionDefaults>({
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_model: '',
})
const selectedLlm = $derived(llmProviders.find((p) => p.id === sumProvider))

async function refreshJobs() {
	jobs = await api.listReprocess(id)
	if (!jobs.some((j) => j.status === 'queued' || j.status === 'running') && poll) {
		clearInterval(poll)
		poll = undefined
		detail = await api.getSession(id)
		// The finished job may have rewritten the selected version's rows.
		if (selectedVersion !== 'original') {
			versionEvents = await api.getTranscriptVersion(id, selectedVersion)
		}
	}
}

async function reprocess() {
	if (!rpProvider) return
	rpBusy = true
	error = ''
	try {
		await api.enqueueReprocess({ session_id: id, provider_id: rpProvider, model: rpModel || null })
		await refreshJobs()
		if (!poll) poll = setInterval(refreshJobs, 1500)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'reprocess failed'
	} finally {
		rpBusy = false
	}
}

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
		if (!poll) poll = setInterval(refreshJobs, 1500)
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
	if (!sumProvider) {
		const wanted = defaults.summarize_provider
		sumProvider =
			wanted && llmProviders.some((p) => p.id === wanted) ? wanted : (llmProviders[0]?.id ?? '')
	}
	summarizeOpen = true
}

async function runSummarize() {
	if (!sumProvider) return
	sumBusy = true
	sumError = ''
	try {
		await api.summarizeSession(id, { provider_id: sumProvider, model: sumModel || null })
		detail = await api.getSession(id) // refresh to show the stored summary
		summarizeOpen = false
	} catch (err) {
		sumError = err instanceof ApiError ? err.message : 'summarize failed'
	} finally {
		sumBusy = false
	}
}

onMount(async () => {
	try {
		detail = await api.getSession(id)
		providers = await api.listProviders()
		rpProvider = detail.session.primary_provider ?? providers[0]?.id ?? ''
		try {
			defaults = await api.getDefaults()
		} catch {
			/* defaults are optional */
		}
		await refreshJobs()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load'
	}
})

onDestroy(() => {
	if (poll) clearInterval(poll)
})
</script>

{#if error}
	<p class="mb-2 text-sm text-destructive">{error}</p>
{/if}

{#if !detail}
	<p class="text-muted-foreground">Loading…</p>
{:else}
	<Card>
		<CardContent class="flex flex-wrap items-center justify-between gap-3">
			<div class="flex flex-wrap items-center gap-3">
				<h1 class="m-0 text-base font-semibold">Session</h1>
				<Badge variant="outline">{detail.session.status}</Badge>
				<span class="text-muted-foreground">
					{fmtWhen(detail.session.started_at)}{durationText ? ` · ${durationText}` : ''}
				</span>
				<div class="relative">
					<Button variant="outline" size="sm" onclick={() => (exportOpen = !exportOpen)}>
						Export <ChevronDown class="size-4" />
					</Button>
					{#if exportOpen}
						<button
							class="fixed inset-0 z-20 cursor-default"
							aria-label="Close export menu"
							onclick={() => (exportOpen = false)}
						></button>
						<div
							class="absolute top-full left-0 z-30 mt-1.5 flex w-44 flex-col rounded-lg border bg-popover p-1 shadow-lg"
						>
							{#each formats as fmt (fmt)}
								<button
									class="rounded px-3 py-1.5 text-left hover:bg-accent"
									onclick={() => exportAs(fmt)}
								>
									{formatLabels[fmt]}
								</button>
							{/each}
							{#if hasAudio}
								<button
									class="mt-1 rounded border-t px-3 py-1.5 pt-2 text-left hover:bg-accent"
									onclick={() => {
										exportOpen = false
										window.location.href = api.audioUrl(id)
									}}
								>
									Audio (.wav)
								</button>
							{/if}
						</div>
					{/if}
				</div>
			</div>
			<a class="text-primary text-sm hover:underline" href="/sessions">← Back</a>
		</CardContent>

		<div class="border-t"></div>

		<CardContent class="flex flex-col gap-3">
			<Foldable
				title="Transcriptions"
				meta="{transcribeJobs.length + 1} version{transcribeJobs.length === 0 ? '' : 's'}"
				bind:open={sections.table}
			>
				<Table>
					<TableHeader>
						<TableRow>
							<TableHead>Transcript</TableHead><TableHead>Provider</TableHead
							><TableHead>Model</TableHead><TableHead>Diarization</TableHead
							><TableHead>Segments</TableHead><TableHead>Created</TableHead
							><TableHead>Status</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						<TableRow
							class="cursor-pointer hover:bg-accent/30 {selectedVersion === 'original'
              ? 'bg-accent/50 [box-shadow:inset_2px_0_0_var(--color-primary)]'
              : ''}"
							onclick={() => selectVersion('original')}
						>
							<TableCell><code>original</code></TableCell>
							<TableCell>{providerName(detail.session.primary_provider, providers)}</TableCell>
							<TableCell class="text-muted-foreground">-</TableCell>
							<TableCell>{diarizeInfo('original')}</TableCell>
							<TableCell>{detail.transcript.length}</TableCell>
							<TableCell class="text-muted-foreground"
								>{fmtWhen(detail.session.started_at)}</TableCell
							>
							<TableCell><Badge variant="outline">live</Badge></TableCell>
						</TableRow>
						{#each transcribeJobs as j (j.id)}
							<TableRow
								class="{selectable(j) ? 'cursor-pointer hover:bg-accent/30' : ''} {selectedVersion ===
							j.id
                ? 'bg-accent/50 [box-shadow:inset_2px_0_0_var(--color-primary)]'
                : ''}"
								onclick={() => selectable(j) && selectVersion(j.id)}
							>
								<TableCell><code>{j.id.slice(0, 8)}</code></TableCell>
								<TableCell>{providerName(j.provider_id, providers)}</TableCell>
								<TableCell>{j.model ?? '-'}</TableCell>
								<TableCell>{diarizeInfo(j.id)}</TableCell>
								<TableCell>{j.segments_added}</TableCell>
								<TableCell class="text-muted-foreground">{fmtWhen(j.created_at)}</TableCell>
								<TableCell>
									<Badge
										title={j.error ?? undefined}
										variant={j.status === 'error'
                    ? 'destructive'
                    : j.status === 'done'
                      ? 'secondary'
                      : 'outline'}
									>
										{j.status}
									</Badge>
								</TableCell>
							</TableRow>
						{/each}
					</TableBody>
				</Table>
				{#if lastJobError?.error}
					<p class="text-xs text-destructive">
						Last failed job ({lastJobError.operation}): {lastJobError.error}
					</p>
				{/if}
				{#if hasAudio}
					<div class="flex flex-wrap items-center justify-end gap-2">
						<span class="text-muted-foreground">New transcription</span>
						<Dropdown
							class="max-w-52"
							bind:value={rpProvider}
							defaultValue={defaults.stt_provider ?? ''}
							options={providers.map((p) => ({ value: p.id, label: p.name }))}
							placeholder="Provider"
						/>
						<ModelPicker
							provider={rpSelectedProvider}
							bind:value={rpModel}
							defaultModel={defaults.stt_model}
						/>
						<Button variant="outline" onclick={reprocess} disabled={rpBusy || !rpProvider}>
							{rpBusy ? 'Queuing…' : 'Re-process audio'}
						</Button>
					</div>
				{:else}
					<p class="text-muted-foreground">
						No stored audio for this session - re-processing and diarization are unavailable.
					</p>
				{/if}
			</Foldable>
		</CardContent>

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
					{providers}
					showSource={selectedVersion === 'original'}
				/>
			</Foldable>
		</CardContent>

		<div class="border-t"></div>

		<CardContent class="flex flex-col gap-2">
			<Foldable
				title="Summary"
				meta={detail.session.summary && detail.session.summary_model
					? `${providerName(detail.session.summary_provider, providers)} · ${detail.session.summary_model}`
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
				<div class="flex justify-end">
					<Button
						variant="outline"
						size="sm"
						onclick={openSummarize}
						disabled={llmProviders.length === 0}
					>
						{detail.session.summary ? 'Re-summarize' : 'Summarize'}
					</Button>
				</div>
			</Foldable>
		</CardContent>
	</Card>
{/if}

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
			{#each speakers as s (s)}
				<div class="flex flex-col gap-2">
					<Label>{s}</Label>
					<Input bind:value={nameForm[s]} placeholder={s} />
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
			<Label>LLM provider</Label>
			<Dropdown
				bind:value={sumProvider}
				defaultValue={defaults.summarize_provider ?? ''}
				options={llmProviders.map((p) => ({ value: p.id, label: p.name }))}
				placeholder="LLM provider"
			/>
		</div>
		<div class="mt-3 flex flex-col gap-2">
			<Label>Model</Label>
			<ModelPicker
				provider={selectedLlm}
				bind:value={sumModel}
				defaultModel={defaults.summarize_model}
			/>
		</div>
		{#if sumError}
			<p class="mt-2 text-sm text-destructive">{sumError}</p>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (summarizeOpen = false)}>Cancel</Button>
			<Button onclick={runSummarize} disabled={sumBusy || !sumProvider}>
				{sumBusy ? 'Summarizing…' : 'Summarize'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
