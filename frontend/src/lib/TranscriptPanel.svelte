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
 * Both live in the section's header, and both open a dialog. The controls used
 * to sit in a bar above the segments, where five diarizer settings and two
 * buttons wrapped onto a second line on a phone and scrolled out of reach on a
 * long transcript; a header is drawn whether the section is folded or not, and
 * two buttons fit on it at 320px.
 *
 * What produced this version is in the header too, as the title's own caption:
 * the provider, the model, whether it has been diarized and how many segments
 * it holds. That is a caption rather than a bar because it is read once, on
 * arrival, and never acted on - and read from the header it is legible with
 * the section folded, which is exactly when "which transcript is this?" is the
 * question being asked.
 *
 * The search box filters here rather than inside the list, because the count
 * in the header has to agree with what the list is showing.
 *
 * Open, the section takes an equal share of whatever height the card has left
 * and scrolls the segments inside it; closed, it is just its own header. The
 * search stays put either way, so the list is the only thing that moves under
 * the playhead.
 */

import { Filter, Users } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import DiarizeDialog from '$lib/DiarizeDialog.svelte'
import Foldable from '$lib/Foldable.svelte'
import { Input } from '$lib/components/ui/input'
import RenameSpeakersDialog from '$lib/RenameSpeakersDialog.svelte'
import { diarizerLabel, GAP_SOURCE, providerName, versionLabel } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import { matchesQuery } from '$lib/transcriptSearch'
import { cn } from '$lib/utils'
import type { ReprocessJob, SessionDetail, TranscriptEvent } from '$lib/wire'

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
	dimInterim = false,
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
	/** Dim segments still marked interim: `events` can hold one whenever the
	 *  version shown is the live capture in progress, since the API returns
	 *  those deliberately rather than waiting for them to settle. */
	dimInterim?: boolean
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

let diarizeOpen = $state(false)
let renameOpen = $state(false)
let filter = $state('')
let filterOpen = $state(false)

const shownEvents = $derived(
	filter ? events.filter((e) => matchesQuery(e, detail.session.speaker_names, filter)) : events,
)
/** How many of a list are an actual spoken segment - a gap marker is audio
 *  nobody transcribed, not one, and must not inflate the count. */
function segmentCount(list: TranscriptEvent[]): number {
	return list.filter((e) => e.source !== GAP_SOURCE).length
}
// "12/340" while a search is on, a plain total otherwise, so the caption in
// the header says how much of the transcript is actually on screen.
const segmentsLabel = $derived(
	filter ? `${segmentCount(shownEvents)}/${segmentCount(events)}` : `${segmentCount(events)}`,
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
</script>

<CardContent class={cn('flex flex-col gap-3', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable title="Transcript" bind:open bodyClass="flex min-h-0 flex-1 flex-col gap-3">
		<!-- One line, truncated by the header rather than wrapped: every item on
		     it is a fact about the version, in the order they are asked for, and
		     the ones at the end are the ones it costs least to have to widen the
		     window for. -->
		{#snippet metaContent()}
			<code>{versionLabel(version)}</code>
			<span class="ml-3"
				><span class="text-muted-foreground">Provider</span> {selectedProviderName}</span
			>
			<span class="ml-3"><span class="text-muted-foreground">Model</span> {selectedModel}</span>
			{#if diarizeJob}
				<span class="ml-3"
					><span class="text-muted-foreground">Diarized with</span>
					{diarizerLabel(diarizeJob)}</span
				>
			{:else}
				<span class="ml-3 text-muted-foreground">Not diarized</span>
			{/if}
			<span class="ml-3"
				><span class="text-muted-foreground">Segments</span>
				{loading ? '…' : segmentsLabel}</span
			>
		{/snippet}

		{#snippet actions()}
			{#if hasAudio}
				<Button
					variant="outline"
					size="sm"
					onclick={() => (diarizeOpen = true)}
					title="Diarizes the full session audio and relabels the selected transcription; re-running replaces its previous diarization."
				>
					Diarize
				</Button>
			{/if}
			<!-- An icon, because the words next to it would be the third label on a
			     header that has to fit at 320px. Renaming needs speakers to rename,
			     which only a diarized transcript has, so with none the button says
			     so by being disabled and by saying why on hover. -->
			<Button
				variant="outline"
				size="icon-sm"
				onclick={() => (renameOpen = true)}
				disabled={speakers.length === 0}
				aria-label="Rename speakers"
				title={speakers.length === 0
					? 'Rename speakers - diarize the transcript first, there are no speakers to name yet'
					: 'Rename speakers'}
			>
				<Users />
			</Button>
		{/snippet}

		<!-- All that is left of the control bar that used to sit here: the rest
		     either moved into the header or behind a dialog. The search stays with
		     the list, because it is the one control whose effect is in the body. -->
		<div class="flex shrink-0 flex-wrap items-center gap-2">
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
				{dimInterim}
				emptyText={filter ? 'No segments match the search.' : 'No transcript segments.'}
				{activeStart}
				{revealNonce}
				{onseek}
				onrename={renameSpeaker}
			/>
		{/if}
	</Foldable>
</CardContent>

<DiarizeDialog
	bind:open={diarizeOpen}
	{sessionId}
	{version}
	sessionEndpoint={detail.session.diarization.endpoint}
	{onqueued}
	{onerror}
/>

<RenameSpeakersDialog
	bind:open={renameOpen}
	{sessionId}
	{speakers}
	names={detail.session.speaker_names}
	onsaved={onrenamed}
	{onerror}
/>
