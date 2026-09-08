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
 */

import { ChevronDown } from '@lucide/svelte'
import { api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import { audioIsEmpty, EMPTY_AUDIO_NOTE, fmtDuration, fmtWhen, versionLabel } from '$lib/stores'
import type { ExportFormat } from '$lib/types'
import type { Session } from '$lib/wire'

let {
	sessionId,
	session,
	audioDurationS,
	version,
}: {
	sessionId: string
	session: Session
	audioDurationS: number | null
	/** The transcript version the page is showing: 'original', or a
	 *  re-transcription's job id. What Export writes. */
	version: string
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

let exportOpen = $state(false)

function exportAs(fmt: ExportFormat) {
	exportOpen = false
	window.location.href = api.exportUrl(sessionId, fmt, version)
}

const durationText = $derived(fmtDuration(session.started_at, session.ended_at))
</script>

<CardContent class="flex shrink-0 flex-wrap items-center justify-between gap-3">
	<div class="flex flex-wrap items-center gap-3">
		<h1 class="m-0 text-base font-semibold">Session</h1>
		<Badge variant="outline">{session.status}</Badge>
		<span class="text-muted-foreground">
			{fmtWhen(session.started_at)}{durationText ? ` · ${durationText}` : ''}
		</span>
		<div class="relative">
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
				</div>
			{/if}
		</div>
	</div>
	<a class="text-primary text-sm hover:underline" href="/sessions">← Back</a>
</CardContent>
