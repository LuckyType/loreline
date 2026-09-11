<script lang="ts">
/**
 * One session: its transcript versions, the transcript itself, its summary,
 * and the recording they all describe, playing in a bar of its own beneath
 * them.
 *
 * The page owns only what more than one card reads - the session, its job
 * rows, its generated videos, which version is selected, and where the
 * playhead is - plus the things that follow the whole page rather than any one
 * card: the polls that run while a job is in flight, and the socket a running
 * job publishes to. Each card below owns its own controls, its own dialog and
 * its own teardown.
 *
 * The video jobs are here for exactly that reason: the header's export menu
 * offers the finished ones and the summary card lists and deletes them, and
 * two cards fetching the same list on their own timers would disagree about
 * how many there are.
 *
 * The page is sized to the window rather than to its contents, so the player
 * stays reachable without scrolling. It is the last row of a column that ends
 * at the bottom of the screen and it takes only the height it needs, which
 * leaves the card the rest: the session header is a fixed band at the top of
 * that card, and the sections under it share what is left, each scrolling its
 * own body. The player is docked by flow rather than by `position: fixed`, so
 * unlike a fixed bar it can cover neither the card's last line nor the sidebar
 * beside it, and a session with no recording simply ends the column early.
 * Which segment is being spoken is worked out here, once, because both the
 * timeline's dots and the transcript's highlight are answers to it.
 *
 * A search result links here with `?v=<version>&t=<start_ts>`: the version to
 * select and the line to look at. Handled once, on arrival, by selecting that
 * version and putting the playhead on the line, which is what the transcript's
 * highlight and its scroll-into-view already follow. It is deliberately not an
 * effect on the URL: the deep link is where the reader came in, not a state the
 * page has to keep matching while they move around inside it.
 *
 * The refreshes the page runs on its own - the job poll, the video poll, the
 * reloads after a card changed something - report their failures in the same
 * banner the cards use, through `refresh`. They used to be bare awaits, so a
 * refetch that failed left the page quietly stale, and a card whose action
 * had succeeded reported the refetch's failure as its own. One message, kept
 * until the next refresh succeeds: the job poll runs every 1.5 s, and a
 * banner that changed on every tick would be noise, not news.
 */

import { onMount } from 'svelte'
import { page } from '$app/state'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { campaigns } from '$lib/campaigns.svelte'
import { Card } from '$lib/components/ui/card'
import { jsonFrame, LiveFeed } from '$lib/liveFeed.svelte'
import SessionHeader from '$lib/SessionHeader.svelte'
import SessionPlayer from '$lib/SessionPlayer.svelte'
import SessionSummary from '$lib/SessionSummary.svelte'
import { inFlight, turnKey } from '$lib/stores'
import TranscriptPanel from '$lib/TranscriptPanel.svelte'
import TranscriptVersions from '$lib/TranscriptVersions.svelte'
import type { ReprocessJob, SessionDetail, TranscriptEvent, VideoJob } from '$lib/wire'

let detail = $state<SessionDetail | null>(null)
let jobs = $state<ReprocessJob[]>([])
// The New transcription dialog's state, owned here because two cards open it:
// the Transcriptions header's button, and the empty state the transcript card
// shows a session that has nothing to read yet.
let reprocessOpen = $state(false)
let videoJobs = $state<VideoJob[]>([])
let error = $state('')
// The last background refresh that failed, or ''. Its own slot rather than
// `error`, so a success can clear it without clearing what a card reported.
let refreshError = $state('')

const id = $derived(page.params.id ?? '')

// The player's element, borrowed so a transcript timestamp can drive it. It
// stays null when the player decided there was nothing worth playing (no
// recording, or an empty one), and that is what keeps the transcript from
// offering to seek a player that is not there: one emptiness check, made
// once, in the card that owns the element.
let audioEl = $state<HTMLAudioElement | null>(null)

// Where the playhead is, and how many times the user has put it somewhere by
// hand. The second is what tells the transcript that a seek landing inside the
// segment it is already showing is still a request to look at that segment.
let currentTime = $state(0)
let seekNonce = $state(0)

/** Play from a segment's start. `start_ts` is already on the session clock,
 *  which is the WAV's own timeline, so it needs no conversion. */
function seekAudio(seconds: number) {
	if (!audioEl) return
	audioEl.currentTime = seconds
	seekNonce++
	// Started by a click, so autoplay policy allows it; a browser that still
	// declines leaves the player parked at the new position, which is fine.
	audioEl.play().catch(() => {})
}

/** Every card reports what went wrong here: one banner, at the top. */
function setError(message: string) {
	error = message
}

// Fold state of the page's sections, kept across visits (best effort).
const SECTIONS_KEY = 'loreline.session-sections'

// Only the version list is worth opening unasked: it is short, and it is what
// says which transcript everything else would be about. The transcript and the
// summary are both long enough to be a wall of text on arrival, so a first
// visit gets them folded and every visit after that gets them however they
// were left.
function loadSections(): { table: boolean; transcript: boolean; summary: boolean } {
	const fallback = { table: true, transcript: false, summary: false }
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

// A session's transcript exists in versions: the original live capture plus
// one per re-transcription job. The version list shows them all; selecting one
// is what everything below (the transcript, the diarize target) follows.
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
		// selectVersion's fetch and this socket can overlap by a segment. A
		// segment carrying a turn is exempt: an arriving revision of a turn
		// already held is the point, and `key` below puts it in place.
		(event.turn_id != null ||
			!held.some((e) => e.start_ts === event.start_ts && e.text === event.text)),
	key: turnKey,
})

const shownEvents = $derived(
	selectedVersion === 'original' ? (detail?.transcript ?? []) : versionFeed.items,
)

/** Whether this session has nothing to read and a recording it could read
 *  from: an import before its first transcription, and a capture whose STT
 *  never produced a line. The transcript card turns that into the one button
 *  that changes it rather than an empty list with no way out. */
const nothingTranscribed = $derived(
	!!detail &&
		!!detail.session.audio_path &&
		detail.transcript.length === 0 &&
		!jobs.some((j) => j.operation === 'transcribe'),
)

// distinct speaker labels in the shown version (drives the rename button)
const speakers = $derived([
	...new Set(shownEvents.map((e) => e.speaker).filter((s): s is string => !!s)),
])

/** The segment being spoken, named by its `start_ts` - which the transcript
 *  and the timeline both already have, so neither needs a position or an id
 *  the other could disagree about.
 *
 * It is the last segment to have started rather than the one strictly
 * containing the playhead, so the silences between segments keep showing the
 * line last spoken instead of blanking out. Inside a segment the two rules
 * give the same answer. */
const activeStart = $derived.by(() => {
	let found: number | null = null
	for (const ev of shownEvents) {
		if (ev.start_ts <= currentTime && (found === null || ev.start_ts > found)) found = ev.start_ts
	}
	return found
})

// Bumped on every call, so a fetch that is still in flight when the user
// switches versions again - even back to the version it started from - can
// tell it is no longer the most recent request and must not write its
// result. A plain variable, not $state, for the same reason as
// jobsWereRunning below: it is only ever compared against after an await,
// never read reactively.
let versionRequestToken = 0

// True only while selectVersion's own fetch for a non-original version is in
// flight. Switching to 'original' needs no fetch, so it never sets this; the
// background refetch in reloadAfterJobs below does not either, since that is
// a catch-up for the version already on screen, not a user-initiated switch.
let versionLoading = $state(false)

async function selectVersion(version: string) {
	selectedVersion = version
	const token = ++versionRequestToken
	if (version === 'original') {
		versionFeed.items = []
		versionLoading = false
		return
	}
	versionLoading = true
	try {
		const items = await api.getTranscriptVersion(id, version)
		if (token === versionRequestToken) versionFeed.items = items
	} catch (err) {
		if (token === versionRequestToken) {
			error = err instanceof ApiError ? err.message : 'failed to load transcript version'
		}
	} finally {
		if (token === versionRequestToken) versionLoading = false
	}
}

/** Run one background refresh and account for it in the banner.
 *
 * A failure is shown once, named by what it was refreshing, and stays until
 * a refresh succeeds; a poll that keeps failing keeps the same message rather
 * than re-announcing it every tick. Nothing is thrown, so a card that awaited
 * the refresh after its own action succeeded is not told its action failed. */
async function refresh(what: string, work: () => Promise<void>) {
	try {
		await work()
		refreshError = ''
	} catch (err) {
		refreshError = `${what}: ${err instanceof ApiError ? err.message : 'request failed'}`
	}
}

/** Which version the page should be showing.
 *
 * The live capture, except on an import, which has none: its "original" holds
 * no rows and no run will ever fill it (see docs/adr/0008), so the newest
 * transcription that has something to show - or is filling up right now - is
 * what the page is about. */
function defaultVersion(current: SessionDetail, list: ReprocessJob[]): string {
	if (current.session.origin !== 'import') return 'original'
	const shown = list
		.filter((j) => j.operation === 'transcribe' && (inFlight(j) || j.segments_added > 0))
		.sort((a, b) => b.created_at - a.created_at)
	return shown[0]?.id ?? 'original'
}

async function refreshJobs() {
	await refresh('could not refresh the job list', async () => {
		jobs = await api.listReprocess(id)
	})
	// Only while nothing else is selected: a reader who chose a version is left
	// on it, and an import's first transcription takes the page the moment it
	// has a line in it.
	if (detail && selectedVersion === 'original') {
		const next = defaultVersion(detail, jobs)
		if (next !== 'original') await selectVersion(next)
	}
}

async function reloadDetail() {
	await refresh('could not reload the session', async () => {
		detail = await api.getSession(id)
	})
}

/** A finished run may have rewritten the selected version's rows, so the queue
 *  draining refetches them. Guarded by the same token as selectVersion: this
 *  fetch can still be in flight after the user has switched to a different
 *  version, and a slow refetch of the version they left must not clobber it. */
async function reloadAfterJobs() {
	await reloadDetail()
	if (selectedVersion !== 'original') {
		const token = versionRequestToken
		await refresh('could not reload the transcript', async () => {
			const items = await api.getTranscriptVersion(id, selectedVersion)
			if (token === versionRequestToken) versionFeed.items = items
		})
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

async function refreshVideoJobs() {
	await refresh('could not refresh the video list', async () => {
		videoJobs = await api.listVideoJobs(id)
	})
}

const videoRunning = $derived(
	videoJobs.some((j) => j.status === 'queued' || j.status === 'running'),
)

/** A generation takes minutes, so this polls only while something is actually
 *  in flight and stops as soon as the queue drains. Hanging the interval off an
 *  effect means the same teardown covers all three ways it should stop: the
 *  queue draining, the flag flipping, and the page unmounting. There is no
 *  timer left to clear by hand, and none left running behind a dead page.
 *
 *  Deliberately independent of whether the video dialog is open: a job that
 *  finishes while nobody is looking still has to be there, in the export menu
 *  and on the count, the moment anyone looks again. */
$effect(() => {
	if (!videoRunning) return
	const timer = setInterval(refreshVideoJobs, 5000)
	return () => clearInterval(timer)
})

// Nothing here outlives the awaits: it only assigns state, so `async` is safe.
// Everything that had to be torn down (the socket, the poll) belongs to an
// effect above, exactly because a teardown returned from an async onMount is
// never registered.
onMount(async () => {
	// Providers, defaults and the capability gate: the seeds in the cards below
	// are derived from the store, so nothing here has to wait for it.
	void actionSetup.load()
	// The campaign names the header shows and its Change dialog offers.
	void campaigns.load()
	try {
		detail = await api.getSession(id)
		await refreshJobs()
		await refreshVideoJobs()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load'
		return
	}
	await openDeepLink()
})

/** Follow a `?v=&t=` link in, once, after the session has loaded.
 *
 * Both halves are optional and mean different things: `v` is which transcript
 * to show, `t` is where in it to look. A search hit carries both; a link to a
 * moment in the capture carries only `t`. An unusable `t` is ignored rather
 * than reported - the transcript is on screen either way, and a banner about a
 * malformed query string helps nobody who did not type it. */
async function openDeepLink() {
	const wanted = page.url.searchParams.get('v')
	if (wanted) await selectVersion(wanted)
	const at = Number(page.url.searchParams.get('t'))
	if (!Number.isFinite(at) || at < 0) return
	// The transcript folds away by default on a first visit, and a link to a
	// line that arrives on a folded transcript looks exactly like a link that
	// did nothing. Arriving by deep link is the one case that overrides how
	// the reader last left this section.
	sections.transcript = true
	// The playhead is what the transcript highlights and the timeline points
	// at, so putting it on the line is the whole of "show me this line".
	currentTime = at
	if (audioEl) audioEl.currentTime = at
	seekNonce++
}
</script>

<!-- The window, less the header above and this page's own padding, exactly as
     the Dashboard sizes its dock. Everything below fits inside it or scrolls. -->
<div class="flex h-[calc(100vh-104px)] flex-col gap-2">
	{#if error || refreshError || actionSetup.error}
		<p class="m-0 shrink-0 text-sm text-destructive">
			{error || refreshError || actionSetup.error}
		</p>
	{/if}

	{#if !detail}
		<p class="text-muted-foreground">Loading…</p>
	{:else}
		<Card class="min-h-0 flex-1">
			<!-- The header exports, the summary card summarizes, and both act on
			     the version selected below rather than on the capture: the page
			     owns the selection, so it is the page that hands it to them. -->
			<SessionHeader
				sessionId={id}
				session={detail.session}
				audioDurationS={detail.audio_duration_s}
				version={selectedVersion}
				{videoJobs}
				oncampaignchanged={reloadDetail}
			/>

			<div class="shrink-0 border-t"></div>

			<TranscriptVersions
				sessionId={id}
				{detail}
				{jobs}
				selected={selectedVersion}
				bind:open={sections.table}
				bind:reprocessOpen
				onselect={selectVersion}
				onchanged={refreshJobs}
				onerror={setError}
			/>

			<div class="shrink-0 border-t"></div>

			<TranscriptPanel
				sessionId={id}
				{detail}
				{jobs}
				version={selectedVersion}
				events={shownEvents}
				loading={versionLoading}
				{speakers}
				bind:open={sections.transcript}
				{activeStart}
				revealNonce={seekNonce}
				dimInterim
				onqueued={refreshJobs}
				onrenamed={reloadDetail}
				onerror={setError}
				onseek={audioEl ? seekAudio : undefined}
				ontranscribe={nothingTranscribed ? () => (reprocessOpen = true) : undefined}
			/>

			<div class="shrink-0 border-t"></div>

			<SessionSummary
				sessionId={id}
				session={detail.session}
				documents={detail.documents}
				{speakers}
				version={selectedVersion}
				{videoJobs}
				bind:open={sections.summary}
				ongenerated={reloadDetail}
				onvideoschanged={refreshVideoJobs}
				onerror={setError}
			/>
		</Card>

		<!-- Outside the card, and last in the column: the recording belongs to no
		     one section, and a row that takes only the height it needs sits on the
		     bottom edge of the screen without being positioned there. It brings
		     its own surface, and a session with no recording renders nothing here
		     at all rather than an empty bar. -->
		<SessionPlayer
			sessionId={id}
			audioPath={detail.session.audio_path}
			audioDurationS={detail.audio_duration_s}
			segments={shownEvents}
			{activeStart}
			bind:audioEl
			bind:currentTime
			onseek={() => seekNonce++}
		/>
	{/if}
</div>
