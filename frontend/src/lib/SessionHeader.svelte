<script lang="ts">
/**
 * What a session is, in one band: when it ran, how long for, and the ways out
 * of it.
 *
 * The export menu is a menu, not a Dropdown picker: nothing stays selected,
 * each item is a download, and the audio entry only exists when there is a
 * recording to hand over - and says so plainly when that "recording" is a bare
 * WAV header with nothing in it (a session that errored before capturing any
 * audio still writes one). It is the vendored bits-ui dropdown menu (the same
 * one the summary card's overflow uses) rather than a hand-rolled popover: the
 * hand-rolled one closed on a click outside and on nothing else, so Escape
 * left it open and focus never came back to the button, and a menu that
 * ignores Escape is one keyboard users cannot leave.
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
import { campaigns } from '$lib/campaigns.svelte'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '$lib/components/ui/dropdown-menu'
import SessionCampaignDialog from '$lib/SessionCampaignDialog.svelte'
import { audioIsEmpty, EMPTY_AUDIO_NOTE, fmtDuration, fmtWhen, versionLabel } from '$lib/stores'
import type { ExportFormat } from '$lib/types'
import type { Session, VideoJob } from '$lib/wire'

let {
	sessionId,
	session,
	audioDurationS,
	version,
	videoJobs = [],
	oncampaignchanged,
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
	/** The session moved to another campaign: the caller refetches it. */
	oncampaignchanged?: () => Promise<void> | void
} = $props()

let campaignOpen = $state(false)
// '' for a session in no campaign, and for one whose campaign has been
// deleted: the id behind the second is not something anyone can act on, and
// "None" is what both of them mean to a reader.
const campaignName = $derived(campaigns.name(session.campaign_id))

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

// The empty-audio entry's reason line needs a stable id to be pointed at.
const uid = $props.id()
const audioReasonId = `${uid}-audio-reason`

/** Every entry navigates rather than fetching: each route serves its file
 *  with a filename attached, so the browser treats it as an attachment and
 *  the page stays where it is. The menu closes itself on select. */
function exportAs(fmt: ExportFormat) {
	window.location.href = api.exportUrl(sessionId, fmt, version)
}

function exportAudio() {
	window.location.href = api.audioUrl(sessionId)
}

/** Hand over the .mp4 (see get_video_content). The players in the summary's
 *  video dialog read the same URL, which a media element fetches rather than
 *  navigates to, so playback is unaffected either way. */
function exportVideo(jobId: string) {
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
	<!-- Which campaign this belongs to, beside when it ran, because both are
	     facts about the session rather than actions on it. The name links to the
	     campaign; Change is what moves it, and is the only way to, since the
	     capture card can only answer the question as a session starts. -->
	<span class="flex items-center gap-1 text-muted-foreground max-sm:order-1">
		{#if campaignName}
			<a class="text-primary hover:underline" href="/campaigns/{session.campaign_id}">
				{campaignName}
			</a>
		{:else}
			No campaign
		{/if}
		<Button
			variant="ghost"
			size="sm"
			class="h-6 px-2 text-xs"
			onclick={() => (campaignOpen = true)}
		>
			Change
		</Button>
	</span>
	<!-- Nobody sat at the table for this one, and the file name is the only
	     name the recording ever had - so it belongs in the one band that says
	     what this session is, next to when it was. Truncated rather than
	     wrapped: a phone recording's name can be very long, and the date beside
	     it is what the band is for. -->
	{#if session.origin === 'import'}
		<span class="max-w-[16rem] truncate text-muted-foreground" title={session.import_name ?? ''}>
			Imported from {session.import_name ?? 'a file'}
		</span>
	{/if}
	<DropdownMenu>
		<DropdownMenuTrigger>
			{#snippet child({ props })}
				<Button {...props} variant="outline" size="sm" class="ml-auto">
					Export <ChevronDown class="size-4" />
				</Button>
			{/snippet}
		</DropdownMenuTrigger>
		<!-- Aligned to its own end, so it hangs under the right edge of the band
		     rather than off the side of a phone. -->
		<DropdownMenuContent align="end" class="w-56">
			<!-- Which transcript is about to be written, named before the formats
			     rather than after the download. The audio entry below is
			     deliberately outside this claim: there is one recording, and
			     every version describes that same one. -->
			<span class="px-2 py-1.5 text-xs text-muted-foreground">
				Transcript <code>{versionLabel(version)}</code>
			</span>
			{#each formats as fmt (fmt)}
				<DropdownMenuItem onSelect={() => exportAs(fmt)}>{formatLabels[fmt]}</DropdownMenuItem>
			{/each}
			{#if hasAudio}
				<!-- An empty recording says why on a line of its own rather than in
				     a `title`: a disabled item takes no pointer events, so a tooltip
				     would never show, and a phone has no hover to show it on. The
				     reason keeps full opacity while the label dims, for the same
				     reason the summary's overflow menu does it that way. -->
				<DropdownMenuItem
					class={[
						'mt-1 flex-col items-start gap-0.5 border-t pt-2',
						audioEmpty && 'data-disabled:opacity-100',
					]}
					disabled={audioEmpty}
					aria-describedby={audioEmpty ? audioReasonId : undefined}
					onSelect={exportAudio}
				>
					<span class={[audioEmpty && 'text-muted-foreground']}>
						{audioEmpty ? 'Audio (empty)' : 'Audio (.wav)'}
					</span>
					{#if audioEmpty}
						<span id={audioReasonId} class="text-xs text-muted-foreground">{EMPTY_AUDIO_NOTE}</span>
					{/if}
				</DropdownMenuItem>
			{/if}
			<!-- Numbered, oldest first, because the number is the only part
			     guaranteed to differ; the muted line under it says which model and
			     at what settings, truncated rather than wrapped so one long model
			     id cannot stretch the menu. Like the audio entry, these are outside
			     the "Transcript <version>" claim above: a video is made from the
			     summary, not from a transcript. -->
			{#each videos as job, i (job.id)}
				<DropdownMenuItem
					class={[
						'flex-col items-start gap-0.5',
						// The group's own rule, drawn once above the first entry.
						i === 0 && 'mt-1 border-t pt-2',
					]}
					onSelect={() => exportVideo(job.id)}
				>
					<span>Video {i + 1} (.mp4)</span>
					<span class="max-w-full truncate text-xs text-muted-foreground">
						{videoDetail(job)}
					</span>
				</DropdownMenuItem>
			{/each}
		</DropdownMenuContent>
	</DropdownMenu>
</CardContent>

<SessionCampaignDialog
	bind:open={campaignOpen}
	{sessionId}
	campaignId={session.campaign_id}
	onchanged={oncampaignchanged}
/>
