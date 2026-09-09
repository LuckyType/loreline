<script lang="ts">
/**
 * What a session is, in one band: when it ran, how long for, and the ways out
 * of it.
 *
 * The export menu is hand-rolled rather than a Dropdown because it is not a
 * picker: nothing stays selected, each item is a download, and the audio entry
 * only exists when there is a recording to hand over - and says so plainly
 * when that "recording" is a bare WAV header with nothing in it (a session
 * that errored before capturing any audio still writes one).
 *
 * It exports the version the page is showing, and says which one at the top of
 * the menu. Both halves matter: the export used to be the capture no matter
 * what was selected, and because nothing named the version, a file holding the
 * wrong 683 lines looked exactly like a file holding the right 1346.
 *
 * The same recording is played rather than downloaded by the player docked at
 * the foot of the card, which reads that emptiness off the same shared check.
 *
 * A generated video is an export too, and the last one anybody thinks to look
 * for: a session can hold several, so each entry names what made it apart from
 * the others. They come from the page rather than from a fetch of this card's
 * own, because the summary card is already polling that same list.
 *
 * The way back out leads the band rather than ending it, as an icon button in
 * the corner every app puts one in - a text link with a literal arrow in it
 * sat at the far right, which is where a phone user's thumb finds it but not
 * where anybody's eye looks for it. It is still an `<a href>` wearing the
 * button's classes, because it navigates: a `<button>` with a handler would
 * cost the middle-click, the long-press and the "open in new tab" that a link
 * gets for free.
 *
 * Below `sm` the date and duration drop to a line of their own. Four things
 * cannot share a 320px row, and of the four it is the one nobody clicks and
 * the one that reads perfectly well on a line by itself, so it is the one that
 * gets sent down there. Export is pushed to the far end by the space between,
 * which puts a control at each edge of the band and matches the sections
 * below, whose own actions right-align the same way.
 */

import { ArrowLeft, ChevronDown } from '@lucide/svelte'
import { api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import { audioIsEmpty, EMPTY_AUDIO_NOTE, fmtDuration, fmtWhen, versionLabel } from '$lib/stores'
import type { ExportFormat } from '$lib/types'
import type { Session, VideoJob } from '$lib/wire'

let {
	sessionId,
	session,
	audioDurationS,
	version,
	videoJobs = [],
}: {
	sessionId: string
	session: Session
	audioDurationS: number | null
	/** The transcript version the page is showing: 'original', or a
	 *  re-transcription's job id. What Export writes. */
	version: string
	/** Every video generated from this session, finished or not. The page owns
	 *  the list; the menu below offers the ones with a file behind them. */
	videoJobs?: VideoJob[]
} = $props()

const formats: ExportFormat[] = ['txt', 'md', 'srt', 'vtt', 'json']
const formatLabels: Record<ExportFormat, string> = {
	txt: 'Text (.txt)',
	md: 'Markdown (.md)',
	srt: 'Subtitles (.srt)',
	vtt: 'Subtitles (.vtt)',
	json: 'JSON (.json)',
}

const hasAudio = $derived(!!session.audio_path)
const audioEmpty = $derived(audioIsEmpty(audioDurationS))

// Only a generation with a file behind it. A queued, running or failed job has
// nothing to hand over, and /api/video/{id}/content answers 409 or 404 for it -
// an entry that can only fail is worse than no entry.
const videos = $derived(videoJobs.filter((j) => j.status === 'done' && j.video_path))

/** What one video entry says about itself, so two of them are tellable apart.
 *
 * A session can carry several generations and they differ in exactly the three
 * things the job records: which model made it, how long it runs and how big it
 * is. The vendor prefix is dropped ('google/veo-3' is 'veo-3' here) because the
 * part that varies between two entries is never the vendor, and this menu is
 * narrow. Two runs of the same model at the same settings would still read the
 * same, which is what the number on the line above is for. */
function videoDetail(job: VideoJob): string {
	const parts = [job.model.split('/').pop() || job.model]
	if (job.duration) parts.push(`${job.duration}s`)
	if (job.resolution) parts.push(job.resolution)
	return parts.join(' · ')
}

let exportOpen = $state(false)

function exportAs(fmt: ExportFormat) {
	exportOpen = false
	window.location.href = api.exportUrl(sessionId, fmt, version)
}

/** Hand over the .mp4. Navigating downloads rather than replacing the page:
 *  the route serves the file with a filename attached (see get_video_content),
 *  so the browser treats it as an attachment. The players in the summary's
 *  video dialog read the same URL, which a media element fetches rather than
 *  navigates to, so playback is unaffected either way. */
function exportVideo(jobId: string) {
	exportOpen = false
	window.location.href = api.videoContentUrl(jobId)
}

const durationText = $derived(fmtDuration(session.started_at, session.ended_at))
</script>

<CardContent class="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1">
	<Button href="/sessions" variant="ghost" size="icon-sm" aria-label="Back" title="Back">
		<ArrowLeft />
	</Button>
	<h1 class="m-0 text-base font-semibold">Session</h1>
	<Badge variant="outline">{session.status}</Badge>
	<!-- `basis-full` is what drops this under the title on a phone, and `order`
	     is what keeps Export up on the line it left rather than stranded below
	     a full-width line it cannot share. Both are `max-sm` only: there is
	     room for all four on one row from `sm` up, in the order they read. -->
	<span class="text-muted-foreground max-sm:order-1 max-sm:basis-full">
		{fmtWhen(session.started_at)}{durationText ? ` · ${durationText}` : ''}
	</span>
	<div class="relative ml-auto">
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
				class="absolute top-full left-0 z-30 mt-1.5 flex w-56 flex-col rounded-lg border bg-popover p-1 shadow-lg"
			>
				<!-- Which transcript is about to be written, named before the
				     formats rather than after the download. The audio entry below
				     is deliberately outside this claim: there is one recording,
				     and every version describes that same one. -->
				<span class="px-3 py-1.5 text-xs text-muted-foreground">
					Transcript <code>{versionLabel(version)}</code>
				</span>
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
						class="mt-1 rounded border-t px-3 py-1.5 pt-2 text-left {audioEmpty
							? 'cursor-not-allowed text-muted-foreground'
							: 'hover:bg-accent'}"
						disabled={audioEmpty}
						title={audioEmpty ? EMPTY_AUDIO_NOTE : undefined}
						onclick={() => {
							exportOpen = false
							window.location.href = api.audioUrl(sessionId)
						}}
					>
						{audioEmpty ? 'Audio (empty)' : 'Audio (.wav)'}
					</button>
				{/if}
				<!-- Numbered, oldest first, because the number is the only part
				     guaranteed to differ; the muted line under it says which model
				     and at what settings, truncated rather than wrapped so one long
				     model id cannot stretch the menu. Like the audio entry, these
				     are outside the "Transcript <version>" claim above: a video is
				     made from the summary, not from a transcript. -->
				{#each videos as job, i (job.id)}
					<button
						class={[
							'flex w-full flex-col items-start rounded px-3 py-1.5 text-left hover:bg-accent',
							// The group's own rule, drawn once above the first entry.
							i === 0 && 'mt-1 border-t pt-2',
						]}
						onclick={() => exportVideo(job.id)}
					>
						<span>Video {i + 1} (.mp4)</span>
						<span class="max-w-full truncate text-xs text-muted-foreground">
							{videoDetail(job)}
						</span>
					</button>
				{/each}
			</div>
		{/if}
	</div>
</CardContent>
