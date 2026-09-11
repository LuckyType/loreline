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
 * Both live in the section's header, next to the search, and both open a
 * dialog. The controls used to sit in a bar above the segments, where five
 * diarizer settings and two buttons wrapped onto a second line on a phone and
 * scrolled out of reach on a long transcript; a header is drawn whether the
 * section is folded or not. The search toggle was the last thing left in that
 * bar and joined them there, because one funnel floating over the transcript
 * was a whole band of chrome spent on a single icon.
 *
 * What produced this version is in the header too, as the title's own caption:
 * the provider, the model, whether it has been diarized and how many segments
 * it holds. That is a caption rather than a bar because it is read once, on
 * arrival, and never acted on - and read from the header it is legible with
 * the section folded, which is exactly when "which transcript is this?" is the
 * question being asked.
 *
 * "Diarized" is answered from the rows, not only from the jobs. A diarize pass
 * is one way labels get onto a version; the other is the transcription itself,
 * when it ran with a diarizer or the vendor labelled speakers on its own, and
 * a caption that only knew about passes printed "Not diarized" over lines that
 * plainly named who spoke. A version with labelled rows and no pass says the
 * labels came inline, and names the diarizer its own run was configured with.
 *
 * The search box filters here rather than inside the list, because the count
 * in the header has to agree with what the list is showing. The box itself is
 * the only part of it that is not in the header: it appears as a row above the
 * segments while the search is on and there is no row at all while it is off,
 * so nothing is spent on a band that is empty most of the time. Turning the
 * search on therefore unfolds the section as well - the toggle is drawn folded
 * and the box is not, and a search you cannot see or type into is not a search.
 *
 * Open, the section takes an equal share of whatever height the card has left
 * and scrolls the segments inside it; closed, it is just its own header. The
 * header and the search box both stay put while it is open, so the list is the
 * only thing that moves under the playhead.
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
import {
	diarizerLabel,
	GAP_SOURCE,
	inlineDiarizationLabel,
	providerName,
	versionLabel,
} from '$lib/stores'
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
	ontranscribe,
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
	/** Open the New transcription dialog. Set only while the session has no
	 *  transcript at all and a recording it could have one from - an import
	 *  before its first run, or a capture whose STT never produced a line. The
	 *  page decides that, because it is the one that can see both. */
	ontranscribe?: () => void
} = $props()

const hasAudio = $derived(!!detail.session.audio_path)

let diarizeOpen = $state(false)
let renameOpen = $state(false)
let filter = $state('')
let filterOpen = $state(false)
// The toggle itself, so Escape out of the box can hand focus back to the
// control that opened it rather than dropping it on the document.
let filterButton = $state<HTMLElement | null>(null)
const uid = $props.id()
const filterId = `${uid}-filter`

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

/** Put the search away and take the filter with it: a box that is gone while
 *  a query is still narrowing the list is a transcript with segments missing
 *  and nothing on screen saying why. */
function closeFilter(refocus: boolean) {
	filterOpen = false
	filter = ''
	if (refocus) filterButton?.focus()
}

function toggleFilter() {
	if (filterOpen) {
		// The pointer or the keyboard is already on the toggle here, so there is
		// nothing to hand focus back to.
		closeFilter(false)
		return
	}
	filterOpen = true
	// The box lives in the body and the toggle lives in the header, which is
	// drawn folded too: opening the search on a folded section would otherwise
	// put the cursor in a box nobody can see.
	open = true
}

function onFilterKeydown(e: KeyboardEvent) {
	if (e.key !== 'Escape') return
	e.preventDefault()
	closeFilter(true)
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
// What the shown version's own run was set to diarize with: the capture's
// config for the original, the job's for a re-transcription. Undefined only
// while the job list has not landed yet.
const ownRun = $derived(version === 'original' ? detail.session : selectedJob)
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
			{:else if loading}
				<!-- `speakers` still describes the version being left, so nothing
				     about this one can be claimed until its rows are here. -->
				<span class="ml-3 text-muted-foreground">Diarized …</span>
			{:else if speakers.length > 0}
				<span class="ml-3"
					><span class="text-muted-foreground">Diarized</span>
					{ownRun ? inlineDiarizationLabel(ownRun) : 'inline'}</span
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
			<!-- Icons, because their words would be the second and third label on a
			     header that has to fit at 320px, and both are recognisable enough
			     without them. Renaming needs speakers to rename, which only a
			     diarized transcript has, so with none the button says so by being
			     disabled and by saying why on hover. -->
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
			<!-- A disclosure rather than a toggle, because what it does is reveal
			     the box below: `aria-expanded` says so, and the outline variant
			     already draws an expanded button filled, which is the state this
			     needs. The green edge on top of that is the louder signal, for the
			     case that matters most - a search left on, quietly hiding most of
			     the transcript. -->
			<Button
				variant="outline"
				size="icon-sm"
				bind:ref={filterButton}
				class={cn(filterOpen && 'border-emerald-500')}
				aria-expanded={filterOpen}
				aria-controls={filterOpen ? filterId : undefined}
				title="Search this transcript by what was said or who said it"
				aria-label="Search transcript"
				onclick={toggleFilter}
			>
				<Filter />
			</Button>
		{/snippet}

		<!-- The last of the control bar that used to sit here, and only while it
		     has something to hold: the box is its own row, so switching the search
		     off leaves the list flush against the header rather than under a band
		     of empty chrome. Full width on a phone, where a 10rem box is four
		     words, and back to a box beside the segments once there is room. -->
		{#if filterOpen}
			<Input
				id={filterId}
				class="w-full shrink-0 sm:w-64"
				placeholder="search…"
				aria-label="Search transcript"
				bind:value={filter}
				autofocus
				onkeydown={onFilterKeydown}
			/>
		{/if}
		{#if loading}
			<p class="text-muted-foreground">Loading transcript…</p>
		{:else if ontranscribe && events.length === 0}
			<!-- An empty list under a search box reads as "nothing matched"; this
			     is the other emptiness, the one with a way out of it. The button
			     opens the same dialog the Transcriptions header does. -->
			<div class="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 text-center">
				<p class="m-0 text-muted-foreground">
					Nothing has been transcribed from this recording yet.
				</p>
				<Button variant="outline" size="sm" onclick={ontranscribe}>Transcribe</Button>
			</div>
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
