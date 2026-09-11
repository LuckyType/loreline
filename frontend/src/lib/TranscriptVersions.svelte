<script lang="ts">
/**
 * Every transcript a session has: the live capture ("original") plus one row
 * per re-transcription, with what produced each and how far a running one has
 * got.
 *
 * Clicking a row selects that version, and everything below on the page
 * follows it, which is why a row is only clickable when there is something to
 * show: a running job counts, because it publishes each segment as it writes
 * it, but a finished one that wrote nothing does not. Only re-transcriptions
 * can be deleted - the original is the capture itself and nothing can produce
 * it again.
 *
 * That live view is what the last column is built around. Watching a
 * re-transcription fill up is how a GM judges a model, so while a job is in
 * flight the row offers Cancel where Delete will be, and once it stops -
 * cancelled or otherwise - Delete takes the slot back.
 *
 * Starting another one is a button in the header rather than a form under the
 * table (see ReprocessPanel): four controls at the foot of a section that
 * scrolls meant scrolling to reach the one button anybody came for, and on a
 * phone they wrapped into a paragraph of widgets. With no stored audio there
 * is nothing to replay, so the button is disabled with that reason instead of
 * opening a dialog that could not do anything. Below the `sm` breakpoint the
 * button is a plus and nothing else: two words plus the section's title do not
 * fit on a 320px header, and it opens a dialog that names itself anyway.
 *
 * Open, the section takes an equal share of the card's leftover height and
 * scrolls inside it, so a session with a dozen re-transcriptions still leaves
 * room for the transcript below it.
 */

import { Plus } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import { confirm } from '$lib/confirm.svelte'
import Foldable from '$lib/Foldable.svelte'
import ReprocessPanel from '$lib/ReprocessPanel.svelte'
import SessionLogsDialog from '$lib/SessionLogsDialog.svelte'
import { diarizerLabel, fmtWhen, inFlight, inlineDiarizationLabel, providerName } from '$lib/stores'
import { cn } from '$lib/utils'
import type { DiarizationConfig, ReprocessJob, SessionDetail } from '$lib/wire'

let {
	sessionId,
	detail,
	jobs,
	selected,
	open = $bindable(true),
	onselect,
	onchanged,
	onerror,
}: {
	sessionId: string
	detail: SessionDetail
	jobs: ReprocessJob[]
	/** The version the page is showing. */
	selected: string
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** A row was clicked, or a deleted version had to be swapped out. */
	onselect?: (version: string) => Promise<void> | void
	/** The job rows changed under us: the caller refetches them. */
	onchanged?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

const hasAudio = $derived(!!detail.session.audio_path)

// Said in two places for one reason: it is the header button's disabled
// reason, and a tooltip is not a thing a phone can show, so the body says it
// too on the one kind of session where it applies.
const NO_AUDIO_NOTE =
	'No stored audio for this session - re-processing and diarization are unavailable.'

let reprocessOpen = $state(false)
let logsOpen = $state(false)
let logsVersion = $state('original')
// Which "Show logs" button opened the dialog. One dialog serves the whole
// table, so nothing about it knows which row asked; without this, closing it
// dropped focus on the "Transcriptions" fold header, one Space away from
// collapsing the table the reader was working in.
let logsTrigger = $state<HTMLElement | null>(null)

// Job ids whose Cancel has been pressed and whose row has not settled yet. A
// re-transcription stops at the end of the utterance it is on, so the row goes
// on saying "running" for a moment after the press; without this the button
// would sit there looking untouched and invite a second press. Ids are never
// removed on success on purpose: the button they belong to is gone as soon as
// the row leaves flight, whichever state it lands in.
let cancelling = $state<string[]>([])

/** Show the log lines one version was produced by, remembering the control
 *  that asked so focus can go back to it. */
function showLogs(version: string, trigger: EventTarget | null) {
	logsTrigger = trigger instanceof HTMLElement ? trigger : null
	logsVersion = version
	logsOpen = true
}

const transcribeJobs = $derived(
	jobs.filter((j) => j.operation === 'transcribe').sort((a, b) => a.created_at - b.created_at),
)

// Whether the versions on screen were cut on different boundaries: the
// original streamed (its rows carry a turn_id, one per vendor turn) while
// every re-transcription always runs over the stored recording cut at the
// local VAD's utterance boundaries instead (docs/adr/0006). Same audio, a
// different segmentation - shown once, near the table, when there is a
// re-transcription to actually compare it against.
const versionsDifferInShape = $derived(
	transcribeJobs.length > 0 && detail.transcript.some((e) => e.turn_id),
)

/** When a job failed, as best the row can say. `finished_at` is the moment
 *  that matters; a job that died before it was ever marked finished still has
 *  the moment it was queued. */
function failedAt(job: ReprocessJob): number {
	return job.finished_at ?? job.started_at ?? job.created_at
}

/** What a failed job says for itself.
 *
 * A run can fail without storing a message, and an empty message must not read
 * as "no failure": naming the operation is the least that still tells the
 * reader which button to look at. The logs for that version have the rest,
 * which is what the Show logs button on the same row is for. */
function failureText(job: ReprocessJob): string {
	return job.error?.trim() || `the ${job.operation} run failed without recording a reason`
}

// The most recent failure, surfaced under the table (there is no error
// column). Selected on `status === 'error'` rather than on there being an
// error string: filtering on the message meant the newest *chatty* failure
// won, so a fixed-and-superseded error sat on screen for minutes while a newer
// run failed silently behind it. Ordered by when each failed, not when each
// was created, and stamped with that time below - a failure with no date on it
// reads as current however old it is. A cancelled run is deliberately not one:
// the GM stopped it themselves, nothing broke, and reporting their own decision
// back to them as the session's last failure would be both wrong and alarming.
const lastFailure = $derived(
	jobs.filter((j) => j.status === 'error').sort((a, b) => failedAt(b) - failedAt(a))[0],
)
/** That failure as the one sentence the table shows: which operation, when,
 *  and what it said. Assembled here rather than in the markup so the timestamp
 *  cannot drift away from the message it belongs to. */
const lastFailureText = $derived.by(() => {
	if (!lastFailure) return ''
	const when = fmtWhen(failedAt(lastFailure))
	return `Last failed job (${lastFailure.operation}, ${when}): ${failureText(lastFailure)}`
})

/** Whether a row can be opened. A running job counts: it publishes each
 *  segment as it is written, so its version is worth watching while it fills
 *  up. A finished one with nothing in it is not (there is nothing to show).
 *
 *  A cancelled run counts on exactly the same terms as a done one, and that is
 *  the point of the whole feature: the GM stopped it in order to read what it
 *  had produced and decide whether to keep it. Those rows are still there, so
 *  the row stays clickable until the version is deleted. */
function selectable(job: ReprocessJob): boolean {
	return (
		inFlight(job) ||
		((job.status === 'done' || job.status === 'cancelled') && job.segments_added > 0)
	)
}

/** Why a done or cancelled row isn't clickable, when that's why. Both carry a
 *  badge that promises something to look at, so without this the only visible
 *  difference from a row with content is a missing hover style - easy to read
 *  as broken rather than as "nothing to show". They need separate wording
 *  because they are different facts: one pass ran to the end and produced
 *  nothing, the other was stopped before it produced anything.
 *  Queued/running/error rows need no note: their badge already explains why
 *  there's nothing to open. */
function unselectableReason(job: ReprocessJob): string | undefined {
	if (selectable(job)) return undefined
	if (job.status === 'done')
		return 'This pass produced no segments, so there is nothing to show for it.'
	if (job.status === 'cancelled')
		return 'This run was cancelled before it wrote anything, so there is nothing to show for it.'
	return undefined
}

/** The variant a job's status badge wears.
 *
 * 'cancelled' is deliberately not destructive. It is terminal like 'done', but
 * it is neither a success to mark nor a failure to warn about: painting it red
 * would file a run the GM chose to end next to the ones that broke, which is
 * the exact confusion the separate status exists to prevent. It gets the same
 * neutral outline as queued and running. */
function statusVariant(job: ReprocessJob): 'destructive' | 'secondary' | 'outline' {
	if (job.status === 'error') return 'destructive'
	if (job.status === 'done') return 'secondary'
	return 'outline'
}

/** What hovering a status badge explains. A failure says what went wrong; a
 *  cancelled run says what became of the work, because "cancelled" alone reads
 *  like the version was thrown away and it was not. */
function statusTitle(job: ReprocessJob): string | undefined {
	if (job.status === 'error') return failureText(job)
	if (job.status === 'cancelled')
		return 'Stopped on request. The segments it had already written are kept.'
	return job.error ?? undefined
}

/** Stop a running or queued job, keeping every segment it has already written.
 *
 * No confirmation dialog, and nobody should add one. Cancelling destroys
 * nothing: the rows the run produced stay, the version stays readable, and it
 * can still be deleted afterwards (which is the usual next click). The entire
 * value of this button is stopping something quickly, and a modal in front of
 * it would spend the seconds the GM pressed it to save.
 *
 * A 409 means the run finished between the poll that drew this button and the
 * press, which is a race the page can genuinely lose and not something the GM
 * did wrong - so it is swallowed rather than shown, and the refetch below lets
 * the row settle into whatever it really ended as. */
async function cancelJob(job: ReprocessJob) {
	cancelling.push(job.id)
	onerror?.('')
	try {
		await api.cancelReprocess(job.id)
	} catch (err) {
		if (!(err instanceof ApiError && err.status === 409)) {
			// Anything else and the job is still going, so give the button back.
			cancelling = cancelling.filter((id) => id !== job.id)
			onerror?.(err instanceof ApiError ? err.message : 'cancel failed')
		}
	}
	await onchanged?.()
}

/** Delete one re-transcription version, its diarization, and its job rows.
 *
 * Only re-transcriptions are deletable: the original is the live capture and
 * nothing can produce it again, so it has no button (and the server refuses it
 * regardless). A version that is on screen is swapped back to the original,
 * which is the one version guaranteed to still be there. */
async function deleteVersion(job: ReprocessJob) {
	const segments = job.segments_added === 1 ? '1 segment' : `${job.segments_added} segments`
	if (
		!(await confirm({
			title: 'Delete transcription',
			description: `Delete version ${job.id.slice(0, 8)} and its ${segments}? Its diarization goes with it; the audio and the other versions are untouched.`,
			confirmLabel: 'Delete',
			destructive: true,
		}))
	)
		return
	try {
		await api.deleteTranscriptVersion(sessionId, job.id)
		if (selected === job.id) await onselect?.('original')
		await onchanged?.()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'delete failed')
	}
}

/** What a version's own run says about its rows: whether they carry speaker
 *  labels, and what the run was set to diarize with. A job row carries both
 *  fields itself; the original's are assembled from the session and its rows. */
type OwnRun = { has_speakers: boolean; diarization: DiarizationConfig }

/**
 * What the table's Diarization column says about one version.
 *
 * A successful pass wins over a failed one however recent the failure, because
 * the labels on screen are that pass's and the cell describes what is showing.
 * With no pass, rows that carry labels of their own are the next thing
 * showing: they came with the transcription (see `inlineDiarizationLabel`),
 * and a cell that only knew about passes printed "-" over them. Only with
 * nothing to show is a failed attempt named here rather than only in the line
 * under the table: the failure belongs next to the run it happened to, and
 * this cell has the room. `note` carries the message as a tooltip, so the
 * cell stays one word wide whatever the vendor wrote.
 *
 * A cancelled diarize pass is named by none of these, and correctly so: it
 * leaves no relabeling behind at all (a half-written one would hide the rest
 * of the transcript, so the server drops it - see `_diarize_session`), which
 * means this cell describes whatever pass was showing before it, or nothing.
 */
function diarizeInfo(
	version: string,
	own: OwnRun,
): { text: string; failed: boolean; note?: string } {
	const targeting = jobs.filter((j) => j.operation === 'diarize' && j.target === version)
	// A running pass counts the segments it has relabeled so far, for the same
	// reason a running transcription does: something has to move.
	const running = targeting.find(inFlight)
	if (running) {
		const text = running.segments_added > 0 ? `diarizing… ${running.segments_added}` : 'diarizing…'
		return { text, failed: false }
	}
	const done = targeting
		.filter((j) => j.status === 'done' && j.segments_added > 0)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0]
	if (done) return { text: diarizerLabel(done), failed: false }
	if (own.has_speakers) return { text: inlineDiarizationLabel(own), failed: false }
	const failed = targeting
		.filter((j) => j.status === 'error')
		.sort((a, b) => failedAt(b) - failedAt(a))[0]
	if (failed) return { text: 'failed', failed: true, note: failureText(failed) }
	return { text: '-', failed: false }
}

// The original's own run, for the Diarization cell: the capture's config, and
// whether the rows it produced name a speaker. Those rows are here already
// (the page fetched them with the session), which is why the original needs
// no `has_speakers` of its own where a re-transcription's row carries one.
const originalRun = $derived<OwnRun>({
	has_speakers: detail.transcript.some((e) => !!e.speaker),
	diarization: detail.session.diarization,
})

// Status of the "original" row. That version is the live capture itself, so
// its state is the *session's*, not a job's: "live" is only true while the
// capture is actually running (see SessionStatus in src/loreline/models.py) -
// on a session that ended hours ago the badge read "live" forever, which is
// exactly the wrong thing to say about a finished recording. Same variants the
// session list uses, so the two badges agree at a glance.
const originalStatus = $derived.by(() => {
	const status = detail.session.status
	if (status === 'capturing' || status === 'stopping') {
		return { label: 'live', variant: 'outline' } as const
	}
	if (status === 'error') return { label: 'error', variant: 'destructive' } as const
	return { label: 'complete', variant: 'secondary' } as const
})
</script>

<CardContent class={cn('flex flex-col gap-3', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable
		title="Transcriptions"
		meta="{transcribeJobs.length + 1} version{transcribeJobs.length === 0 ? '' : 's'}"
		bind:open
		bodyClass="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
	>
		{#snippet actions()}
			<Button
				variant="outline"
				size="sm"
				onclick={() => (reprocessOpen = true)}
				disabled={!hasAudio}
				aria-label="New transcription"
				title={hasAudio
					? 'Run the stored recording through a provider again, as another version'
					: NO_AUDIO_NOTE}
			>
				<Plus />
				<span class="hidden sm:inline">New transcription</span>
			</Button>
		{/snippet}

		<Table>
			<TableHeader>
				<TableRow>
					<TableHead>Transcript</TableHead><TableHead>Provider</TableHead
					><TableHead>Model</TableHead><TableHead>Diarization</TableHead
					><TableHead>Segments</TableHead><TableHead>Created</TableHead><TableHead>Status</TableHead
					><TableHead class="w-0"></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				<TableRow
					class="cursor-pointer hover:bg-accent/30 {selected === 'original'
              ? 'bg-accent/50 [box-shadow:inset_2px_0_0_var(--color-primary)]'
              : ''}"
					onclick={() => onselect?.('original')}
				>
					{@const diar = diarizeInfo('original', originalRun)}
					<TableCell><code>original</code></TableCell>
					<TableCell
						>{providerName(detail.session.primary_provider, actionSetup.providers)}</TableCell
					>
					<TableCell class="text-muted-foreground">-</TableCell>
					<TableCell class={diar.failed ? 'text-destructive' : ''} title={diar.note}>
						{diar.text}
					</TableCell>
					<TableCell>{detail.transcript.length}</TableCell>
					<TableCell class="text-muted-foreground">{fmtWhen(detail.session.started_at)}</TableCell>
					<TableCell>
						<Badge variant={originalStatus.variant}>{originalStatus.label}</Badge>
					</TableCell>
					<!-- No delete for the original: it is the live capture, and unlike
					     every re-transcription there is no way to produce it again. Its
					     log is the one worth keeping most, for the same reason. -->
					<TableCell>
						<Button
							variant="ghost"
							size="sm"
							title="The log lines this capture was recorded and transcribed by"
							onclick={(e: MouseEvent) => {
								e.stopPropagation() // the row click selects the version
								showLogs('original', e.currentTarget)
							}}
						>
							Show logs
						</Button>
					</TableCell>
				</TableRow>
				{#each transcribeJobs as j (j.id)}
					<TableRow
						class="{selectable(j) ? 'cursor-pointer hover:bg-accent/30' : ''} {selected ===
					j.id
                ? 'bg-accent/50 [box-shadow:inset_2px_0_0_var(--color-primary)]'
                : ''}"
						title={unselectableReason(j)}
						onclick={() => selectable(j) && onselect?.(j.id)}
					>
						{@const diar = diarizeInfo(j.id, j)}
						<TableCell><code>{j.id.slice(0, 8)}</code></TableCell>
						<TableCell>{providerName(j.provider_id, actionSetup.providers)}</TableCell>
						<TableCell>{j.model ?? '-'}</TableCell>
						<TableCell class={diar.failed ? 'text-destructive' : ''} title={diar.note}>
							{diar.text}
						</TableCell>
						<TableCell>
							{#if inFlight(j)}
								<!-- Counts up as the run writes segments, next to the finished
								     versions' counts. Not a percentage on purpose: models split the
								     same audio differently, so there is no total to divide by, only
								     a rough feel for how far along this run is. -->
								<span
									class="text-muted-foreground"
									title="Segments written so far. Versions legitimately end on different counts, so this is a rough feel for progress, not a completion ratio."
								>
									{j.segments_added}
									so far…
								</span>
							{:else}
								{j.segments_added}
							{/if}
						</TableCell>
						<TableCell class="text-muted-foreground">{fmtWhen(j.created_at)}</TableCell>
						<TableCell>
							<Badge title={statusTitle(j)} variant={statusVariant(j)}>
								{j.status}
							</Badge>
						</TableCell>
						<TableCell>
							<div class="flex gap-1">
								<Button
									variant="ghost"
									size="sm"
									title="The log lines this transcription was produced by"
									onclick={(e: MouseEvent) => {
										e.stopPropagation() // the row click selects the version
										showLogs(j.id, e.currentTarget)
									}}
								>
									Show logs
								</Button>
								<!-- One slot, two buttons. While the run is in flight there is
								     nothing to delete yet, so Cancel takes the place the greyed
								     out Delete used to sit in; the moment the row settles -
								     cancelled, done or failed - Delete comes back, enabled,
								     because a cancelled version is as deletable as a finished
								     one. Watch the version fill up, stop it, delete it: three
								     presses in one place, which is the flow this is for. -->
								{#if inFlight(j)}
									<Button
										variant="ghost"
										size="sm"
										disabled={cancelling.includes(j.id)}
										title="Stop this run. What it has already written is kept, so you can read it and then delete it."
										onclick={(e: MouseEvent) => {
											e.stopPropagation() // the row click selects the version
											void cancelJob(j)
										}}
									>
										{cancelling.includes(j.id) ? 'Cancelling…' : 'Cancel'}
									</Button>
								{:else}
									<Button
										variant="ghost"
										size="sm"
										title="Delete this transcription and its diarization"
										onclick={(e: MouseEvent) => {
											e.stopPropagation() // the row click selects the version
											void deleteVersion(j)
										}}
									>
										Delete
									</Button>
								{/if}
							</div>
						</TableCell>
					</TableRow>
				{/each}
			</TableBody>
		</Table>
		{#if versionsDifferInShape}
			<p class="text-xs text-muted-foreground">
				The original was transcribed live in vendor turns; re-transcriptions are cut at the
				recording's utterance boundaries, so segments will not line up one to one between versions.
			</p>
		{/if}
		{#if lastFailureText}
			<p class="text-xs text-destructive">{lastFailureText}</p>
		{/if}
		{#if !hasAudio}
			<p class="m-0 text-muted-foreground">{NO_AUDIO_NOTE}</p>
		{/if}
	</Foldable>
</CardContent>

<SessionLogsDialog bind:open={logsOpen} {sessionId} version={logsVersion} trigger={logsTrigger} />

<ReprocessPanel
	bind:open={reprocessOpen}
	{sessionId}
	capturedWith={detail.session.primary_provider}
	onqueued={onchanged}
	{onerror}
/>
