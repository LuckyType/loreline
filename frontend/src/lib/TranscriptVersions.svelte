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
 */

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
import { diarizerLabel, fmtWhen, inFlight, providerName } from '$lib/stores'
import type { ReprocessJob, SessionDetail } from '$lib/wire'

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

let logsOpen = $state(false)
let logsVersion = $state('original')

/** Show the log lines one version was produced by. */
function showLogs(version: string) {
	logsVersion = version
	logsOpen = true
}

const transcribeJobs = $derived(
	jobs.filter((j) => j.operation === 'transcribe').sort((a, b) => a.created_at - b.created_at),
)

// The most recent failure, surfaced under the table (there is no error column).
const lastJobError = $derived(
	jobs.filter((j) => j.error).sort((a, b) => b.created_at - a.created_at)[0],
)

/** Whether a row can be opened. A running job counts: it publishes each
 *  segment as it is written, so its version is worth watching while it fills
 *  up. A finished one with nothing in it is not (there is nothing to show). */
function selectable(job: ReprocessJob): boolean {
	return inFlight(job) || (job.status === 'done' && job.segments_added > 0)
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

// What the table's Diarization column says about one version.
function diarizeInfo(version: string): string {
	const targeting = jobs.filter((j) => j.operation === 'diarize' && j.target === version)
	// A running pass counts the segments it has relabeled so far, for the same
	// reason a running transcription does: something has to move.
	const running = targeting.find(inFlight)
	if (running)
		return running.segments_added > 0 ? `diarizing… ${running.segments_added}` : 'diarizing…'
	const done = targeting
		.filter((j) => j.status === 'done' && j.segments_added > 0)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0]
	return done ? diarizerLabel(done) : '-'
}

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

<CardContent class="flex flex-col gap-3">
	<Foldable
		title="Transcriptions"
		meta="{transcribeJobs.length + 1} version{transcribeJobs.length === 0 ? '' : 's'}"
		bind:open
	>
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
					<TableCell><code>original</code></TableCell>
					<TableCell
						>{providerName(detail.session.primary_provider, actionSetup.providers)}</TableCell
					>
					<TableCell class="text-muted-foreground">-</TableCell>
					<TableCell>{diarizeInfo('original')}</TableCell>
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
								showLogs('original')
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
						onclick={() => selectable(j) && onselect?.(j.id)}
					>
						<TableCell><code>{j.id.slice(0, 8)}</code></TableCell>
						<TableCell>{providerName(j.provider_id, actionSetup.providers)}</TableCell>
						<TableCell>{j.model ?? '-'}</TableCell>
						<TableCell>{diarizeInfo(j.id)}</TableCell>
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
						<TableCell>
							<div class="flex gap-1">
								<Button
									variant="ghost"
									size="sm"
									title="The log lines this transcription was produced by"
									onclick={(e: MouseEvent) => {
										e.stopPropagation() // the row click selects the version
										showLogs(j.id)
									}}
								>
									Show logs
								</Button>
								<Button
									variant="ghost"
									size="sm"
									disabled={inFlight(j)}
									title={inFlight(j)
										? 'Wait for the job to finish'
										: 'Delete this transcription and its diarization'}
									onclick={(e: MouseEvent) => {
										e.stopPropagation() // the row click selects the version
										void deleteVersion(j)
									}}
								>
									Delete
								</Button>
							</div>
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
		<ReprocessPanel
			{sessionId}
			{hasAudio}
			capturedWith={detail.session.primary_provider}
			onqueued={onchanged}
			{onerror}
		/>
	</Foldable>
</CardContent>

<SessionLogsDialog bind:open={logsOpen} {sessionId} version={logsVersion} />
