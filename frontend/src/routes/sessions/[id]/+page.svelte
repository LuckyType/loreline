<script lang="ts">
import { onMount, onDestroy } from 'svelte'
import { page } from '$app/stores'
import { api, ApiError } from '$lib/api'
import { confirm } from '$lib/confirm.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import TranscriptList from '$lib/TranscriptList.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '$lib/components/ui/card'
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
import Dropdown from '$lib/Dropdown.svelte'
import { Badge } from '$lib/components/ui/badge'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import type {
	ActionDefaults,
	DiarizationModeKind,
	ExportFormat,
	ProviderConfig,
	ReprocessJob,
	SessionDetail,
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
const hasAudio = $derived(!!detail?.session.audio_path)
const rpSelectedProvider = $derived(providers.find((p) => p.id === rpProvider))

// distinct speaker labels detected in the transcript (drives the rename button)
const speakers = $derived(
	detail
		? [...new Set(detail.transcript.map((e) => e.speaker).filter((s): s is string => !!s))]
		: [],
)

let renameOpen = $state(false)
let nameForm = $state<Record<string, string>>({})

// --- summarize ---
const llmProviders = $derived(providers.filter((p) => p.kind === 'openai_chat'))
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
	if (!sumProvider) sumProvider = llmProviders[0]?.id ?? ''
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

<div class="flex items-center justify-between">
	<h1>Session</h1>
	<a class="text-primary text-sm hover:underline" href="/sessions">← Back</a>
</div>

{#if error}
	<p class="mt-2 text-sm text-destructive">{error}</p>
{/if}

{#if !detail}
	<p class="text-muted-foreground">Loading…</p>
{:else}
	<Card class="mt-4">
		<CardContent class="flex flex-wrap gap-8 py-4">
			<span
				><span class="text-muted-foreground">Status:</span>
				<Badge variant="outline" class="ml-1">{detail.session.status}</Badge></span
			>
			<span
				><span class="text-muted-foreground">Primary:</span>
				{detail.session.primary_provider ?? '-'}</span
			>
			<span
				><span class="text-muted-foreground">Diarization:</span>
				{detail.session.diarization.mode}</span
			>
			<span><span class="text-muted-foreground">Segments:</span> {detail.transcript.length}</span>
		</CardContent>
	</Card>

	<Card class="mt-4">
		<CardHeader><CardTitle>Export</CardTitle></CardHeader>
		<CardContent class="flex flex-wrap gap-2">
			{#each formats as fmt (fmt)}
				<Button
					variant="outline"
					size="sm"
					onclick={() => (window.location.href = api.exportUrl(id, fmt))}
				>
					.{fmt}
				</Button>
			{/each}
			{#if hasAudio}
				<Button
					variant="outline"
					size="sm"
					onclick={() => (window.location.href = api.audioUrl(id))}
				>
					audio .wav
				</Button>
			{/if}
		</CardContent>
	</Card>

	<Card class="mt-4">
		<CardContent class="flex items-center justify-between py-4">
			<h3 class="m-0">Summary</h3>
			<Button variant="outline" onclick={openSummarize} disabled={llmProviders.length === 0}>
				{detail.session.summary ? 'Re-summarize' : 'Summarize'}
			</Button>
		</CardContent>
		{#if detail.session.summary}
			<CardContent class="whitespace-pre-wrap leading-relaxed"
				>{detail.session.summary}</CardContent
			>
		{:else if llmProviders.length === 0}
			<CardContent class="text-sm text-muted-foreground">
				Add an LLM provider (OpenAI-compatible chat) in Settings to enable summaries.
			</CardContent>
		{:else}
			<CardContent class="text-sm text-muted-foreground">Not summarized yet.</CardContent>
		{/if}
	</Card>

	<Card class="mt-4">
		<CardHeader><CardTitle>Re-process</CardTitle></CardHeader>
		<CardContent>
			{#if hasAudio}
				<div class="flex flex-wrap items-center gap-2">
					<Dropdown
						class="max-w-52"
						bind:value={rpProvider}
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
				<div class="mt-2 flex flex-wrap items-center gap-2">
					<Dropdown
						class="max-w-72"
						bind:value={rpDiarKind}
						options={[
              { value: 'remote', label: 'sherpa-onnx (pyannote-seg-3.0 + 3dspeaker-eres2net)' },
              { value: 'openai', label: 'gpt-4o-transcribe-diarize' }
            ]}
					/>
					{#if rpDiarKind === 'remote'}
						<Input
							class="max-w-56"
							placeholder="endpoint (http://…:8001)"
							bind:value={rpDiarEndpoint}
						/>
					{/if}
					<Input
						class="max-w-32"
						type="number"
						min="1"
						placeholder="min speakers"
						bind:value={rpDiarMin}
					/>
					<Input
						class="max-w-32"
						type="number"
						min="1"
						placeholder="max speakers"
						bind:value={rpDiarMax}
					/>
					<Button variant="outline" onclick={diarizeSession} disabled={rpBusy}>
						Diarize whole session
					</Button>
				</div>
				<p class="mt-1 text-xs text-muted-foreground">
					Diarizes the full session audio once for stable speaker labels across the recording
					(source
					<code>diarize</code>). The OpenAI model uses your saved OpenAI provider key (or the
					<code>OPENAI_API_KEY</code>
					env var); sherpa-onnx uses your self-hosted endpoint.
				</p>
			{:else}
				<p class="text-muted-foreground">No stored audio for this session.</p>
			{/if}
			{#if jobs.length > 0}
				<Table class="mt-3">
					<TableHeader>
						<TableRow>
							<TableHead>Provider</TableHead><TableHead>Status</TableHead
							><TableHead>Segments</TableHead><TableHead>Error</TableHead>
						</TableRow>
					</TableHeader>
					<TableBody>
						{#each jobs as j (j.id)}
							<TableRow>
								<TableCell>{j.provider_id}</TableCell>
								<TableCell>
									<Badge
										variant={j.status === 'error'
                      ? 'destructive'
                      : j.status === 'done'
                        ? 'secondary'
                        : 'outline'}
									>
										{j.status}
									</Badge>
								</TableCell>
								<TableCell>{j.segments_added}</TableCell>
								<TableCell class="text-destructive">{j.error ?? ''}</TableCell>
							</TableRow>
						{/each}
					</TableBody>
				</Table>
			{/if}
		</CardContent>
	</Card>

	<Card class="mt-4">
		<CardContent class="flex items-center justify-between py-4">
			<h3 class="m-0">Transcript</h3>
			<Button variant="outline" onclick={openRename} disabled={speakers.length === 0}>
				Rename speakers
			</Button>
		</CardContent>
		<TranscriptList events={detail.transcript} names={detail.session.speaker_names} showSource />
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
