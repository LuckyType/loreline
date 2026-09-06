<script lang="ts">
/**
 * The selected transcript: what produced it, what it says, and the two things
 * that can still be done to it.
 *
 * Diarization runs over the whole session audio and relabels this version, so
 * it belongs next to the version it will change rather than next to the job
 * list. Renaming speakers needs speakers to rename, which only a diarized
 * version has, so the button says so by being disabled. The dialog is not the
 * only way in: a name clicked in the transcript below edits in place, which
 * ends at the same endpoint with the same whole-map shape.
 *
 * The search box filters here rather than inside the list, because the two
 * counts on this bar have to agree with what the list is showing.
 *
 * Open, the section takes an equal share of whatever height the card has left
 * and scrolls the segments inside it; closed, it is just its own header. The
 * bar above the segments stays put either way, so the list is the only thing
 * that moves under the playhead.
 */

import { Filter } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import Dropdown from '$lib/Dropdown.svelte'
import Foldable from '$lib/Foldable.svelte'
import { Input } from '$lib/components/ui/input'
import RenameSpeakersDialog from '$lib/RenameSpeakersDialog.svelte'
import { diarizerLabel, providerName } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import { matchesQuery } from '$lib/transcriptSearch'
import { cn } from '$lib/utils'
import type { DiarizationModeKind, ReprocessJob, SessionDetail, TranscriptEvent } from '$lib/wire'

let {
	sessionId,
	detail,
	jobs,
	version,
	events,
	loading = false,
	speakers,
	open = $bindable(true),
	activeStart = null,
	revealNonce = 0,
	onqueued,
	onerror,
	onrenamed,
	onseek,
}: {
	sessionId: string
	detail: SessionDetail
	jobs: ReprocessJob[]
	/** The version being shown: 'original', or a re-transcription's job id. */
	version: string
	events: TranscriptEvent[]
	/** True while `version`'s own transcript fetch is still in flight, so
	 *  `events` may still hold the previous version's segments. The list is
	 *  hidden behind a loading message instead of showing them. */
	loading?: boolean
	/** The distinct speaker labels in `events`. */
	speakers: string[]
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** The segment the player is inside, by its `start_ts`, marked in the list
	 *  and kept on screen as playback runs. */
	activeStart?: number | null
	/** Bumped by the page on a seek the user made by hand. */
	revealNonce?: number
	/** A diarization has been queued: the caller refetches the job list. */
	onqueued?: () => Promise<void> | void
	/** Names were saved: the caller refetches the session. */
	onrenamed?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
	/** Play the session audio from a segment's start. Set only when the page
	 *  has a player to seek, so timestamps stay plain text otherwise. */
	onseek?: (seconds: number) => void
} = $props()

const hasAudio = $derived(!!detail.session.audio_path)

let diarKind = $state<DiarizationModeKind>('remote')
let diarEndpoint = $state('')
let diarMin = $state('')
let diarMax = $state('')
let busy = $state(false)
let renameOpen = $state(false)
let filter = $state('')
let filterOpen = $state(false)

const shownEvents = $derived(
	filter ? events.filter((e) => matchesQuery(e, detail.session.speaker_names, filter)) : events,
)
// "12/340" while a search is on, a plain total otherwise. Both places that
// count segments read this, so the fold header and the bar cannot disagree
// about how much of the transcript is actually on screen.
const segmentsLabel = $derived(
	filter ? `${shownEvents.length}/${events.length}` : `${events.length}`,
)

function toggleFilter() {
	filterOpen = !filterOpen
	if (!filterOpen) filter = ''
}

function onFilterKeydown(e: KeyboardEvent) {
	if (e.key !== 'Escape') return
	e.preventDefault()
	toggleFilter()
}

/** The rename dialog's save, reached by clicking a name instead of opening a
 *  form: the same endpoint, the same whole-map body, and the same rule that a
 *  blank name means the raw label. The map is sent complete because the
 *  endpoint replaces it, so a partial one would drop every other name. */
async function renameSpeaker(label: string, name: string) {
	const stored = detail.session.speaker_names
	const trimmed = name.trim()
	if (trimmed === (stored[label] ?? '')) return // clicked away without a change
	const wanted = { ...stored }
	if (trimmed) wanted[label] = trimmed
	else delete wanted[label]
	onerror?.('')
	try {
		await api.setSpeakerNames(sessionId, wanted)
		await onrenamed?.()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'rename failed')
	}
}

// The diarize job whose relabeling the selected version currently shows.
const diarizeJob = $derived(
	jobs
		.filter(
			(j) =>
				j.operation === 'diarize' &&
				(j.target ?? 'original') === version &&
				j.status === 'done' &&
				j.segments_added > 0,
		)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0],
)
const selectedJob = $derived(jobs.find((j) => j.operation === 'transcribe' && j.id === version))
const selectedProviderName = $derived(
	version === 'original'
		? providerName(detail.session.primary_provider, actionSetup.providers)
		: providerName(selectedJob?.provider_id, actionSetup.providers),
)
const selectedModel = $derived(version === 'original' ? '-' : (selectedJob?.model ?? '-'))

async function diarizeSession() {
	busy = true
	onerror?.('')
	try {
		await api.enqueueReprocess({
			session_id: sessionId,
			operation: 'diarize',
			target: version,
			diarization: {
				mode: diarKind,
				endpoint:
					diarKind === 'remote'
						? diarEndpoint || detail.session.diarization.endpoint || null
						: null,
				min_speakers: diarMin ? Number(diarMin) : null,
				max_speakers: diarMax ? Number(diarMax) : null,
			},
		})
		await onqueued?.()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'diarize failed')
	} finally {
		busy = false
	}
}
</script>

<CardContent class={cn('flex flex-col gap-3', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable
		title="Transcript"
		meta="{version === 'original'
			? 'original'
			: version.slice(0, 8)} · {loading ? 'loading…' : `${segmentsLabel} segments`}"
		bind:open
		bodyClass="flex min-h-0 flex-1 flex-col gap-3"
	>
		<div
			class="flex shrink-0 flex-wrap items-center justify-between gap-3 rounded-md bg-accent/40 px-3 py-2.5"
		>
			<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
				<span
					>Transcript
					<code>{version === 'original' ? 'original' : version.slice(0, 8)}</code></span
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
				<span
					><span class="text-muted-foreground">Segments</span>
					{loading
						? '…'
						: segmentsLabel}</span
				>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				{#if filterOpen}
					<Input
						class="w-40"
						placeholder="search…"
						bind:value={filter}
						autofocus
						onkeydown={onFilterKeydown}
					/>
				{/if}
				<Button
					variant="ghost"
					size="icon-sm"
					class={cn('opacity-55 hover:opacity-100', filterOpen && 'border-emerald-500 opacity-100')}
					title="Search this transcript by what was said or who said it"
					aria-label="Search transcript"
					onclick={toggleFilter}
				>
					<Filter />
				</Button>
				<div class="h-5 w-px bg-border"></div>
				{#if hasAudio}
					<Dropdown
						class="max-w-52"
						bind:value={diarKind}
						options={[
                { value: 'remote', label: 'sherpa-onnx' },
                { value: 'openai', label: 'gpt-4o-transcribe-diarize' }
              ]}
					/>
					{#if diarKind === 'remote'}
						<Input class="max-w-48" placeholder="http://…:8001" bind:value={diarEndpoint} />
					{/if}
					<Input class="max-w-20" type="number" min="1" placeholder="min" bind:value={diarMin} />
					<Input class="max-w-20" type="number" min="1" placeholder="max" bind:value={diarMax} />
					<Button
						variant="outline"
						size="sm"
						onclick={diarizeSession}
						disabled={busy}
						title="Diarizes the full session audio and relabels the selected transcription; re-running replaces its previous diarization."
					>
						Diarize
					</Button>
					<div class="h-5 w-px bg-border"></div>
				{/if}
				<Button
					variant="outline"
					size="sm"
					onclick={() => (renameOpen = true)}
					disabled={speakers.length === 0}
				>
					Rename speakers
				</Button>
			</div>
		</div>
		{#if loading}
			<p class="text-muted-foreground">Loading transcript…</p>
		{:else}
			<TranscriptList
				class="min-h-0 flex-1 overflow-auto"
				events={shownEvents}
				names={detail.session.speaker_names}
				providers={actionSetup.providers}
				showSource={version === 'original'}
				query={filter}
				emptyText={filter ? 'No segments match the search.' : 'No transcript segments.'}
				{activeStart}
				{revealNonce}
				{onseek}
				onrename={renameSpeaker}
			/>
		{/if}
	</Foldable>
</CardContent>

<RenameSpeakersDialog
	bind:open={renameOpen}
	{sessionId}
	{speakers}
	names={detail.session.speaker_names}
	onsaved={onrenamed}
	{onerror}
/>
