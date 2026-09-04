<script lang="ts">
/**
 * One session: its transcript versions, the transcript itself, its summary.
 *
 * The page owns only what more than one card reads - the session, its job
 * rows, and which version is selected - plus the two things that follow the
 * whole page rather than any one card: the poll that runs while a job is in
 * flight, and the socket a running job publishes to. Each card below owns its
 * own controls, its own dialog and its own teardown.
 */

import { onMount } from 'svelte'
import { page } from '$app/state'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Card } from '$lib/components/ui/card'
import { jsonFrame, LiveFeed } from '$lib/liveFeed.svelte'
import SessionHeader from '$lib/SessionHeader.svelte'
import SessionSummary from '$lib/SessionSummary.svelte'
import { inFlight } from '$lib/stores'
import TranscriptPanel from '$lib/TranscriptPanel.svelte'
import TranscriptVersions from '$lib/TranscriptVersions.svelte'
import type { ReprocessJob, SessionDetail, TranscriptEvent } from '$lib/wire'

let detail = $state<SessionDetail | null>(null)
let jobs = $state<ReprocessJob[]>([])
let error = $state('')

const id = $derived(page.params.id ?? '')

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
		// selectVersion's fetch and this socket can overlap by a segment.
		!held.some((e) => e.start_ts === event.start_ts && e.text === event.text),
})

const shownEvents = $derived(
	selectedVersion === 'original' ? (detail?.transcript ?? []) : versionFeed.items,
)

// distinct speaker labels in the shown version (drives the rename button)
const speakers = $derived([
	...new Set(shownEvents.map((e) => e.speaker).filter((s): s is string => !!s)),
])

async function selectVersion(version: string) {
	selectedVersion = version
	try {
		versionFeed.items = version === 'original' ? [] : await api.getTranscriptVersion(id, version)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load transcript version'
	}
}

async function refreshJobs() {
	jobs = await api.listReprocess(id)
}

async function reloadDetail() {
	detail = await api.getSession(id)
}

/** A finished run may have rewritten the selected version's rows, so the queue
 *  draining refetches them. */
async function reloadAfterJobs() {
	await reloadDetail()
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

// Nothing here outlives the awaits: it only assigns state, so `async` is safe.
// Everything that had to be torn down (the socket, the poll) belongs to an
// effect above, exactly because a teardown returned from an async onMount is
// never registered.
onMount(async () => {
	// Providers, defaults and the capability gate: the seeds in the cards below
	// are derived from the store, so nothing here has to wait for it.
	void actionSetup.load()
	try {
		detail = await api.getSession(id)
		await refreshJobs()
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

		<TranscriptPanel
			sessionId={id}
			{detail}
			{jobs}
			version={selectedVersion}
			events={shownEvents}
			{speakers}
			bind:open={sections.transcript}
			onqueued={refreshJobs}
			onrenamed={reloadDetail}
			onerror={setError}
		/>

		<div class="border-t"></div>

		<SessionSummary
			sessionId={id}
			session={detail.session}
			{speakers}
			bind:open={sections.summary}
			onsummarized={reloadDetail}
			onerror={setError}
		/>
	</Card>
{/if}
