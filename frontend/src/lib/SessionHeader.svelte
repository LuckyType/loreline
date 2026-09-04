<script lang="ts">
/**
 * What a session is, in one band: when it ran, how long for, and the ways out
 * of it.
 *
 * The export menu is hand-rolled rather than a Dropdown because it is not a
 * picker: nothing stays selected, each item is a download, and the audio entry
 * only exists when there is a recording to hand over.
 */

import { ChevronDown } from '@lucide/svelte'
import { api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import { fmtWhen } from '$lib/stores'
import type { ExportFormat } from '$lib/types'
import type { Session } from '$lib/wire'

let { sessionId, session }: { sessionId: string; session: Session } = $props()

const formats: ExportFormat[] = ['txt', 'md', 'srt', 'vtt', 'json']
const formatLabels: Record<ExportFormat, string> = {
	txt: 'Text (.txt)',
	md: 'Markdown (.md)',
	srt: 'Subtitles (.srt)',
	vtt: 'Subtitles (.vtt)',
	json: 'JSON (.json)',
}

const hasAudio = $derived(!!session.audio_path)

let exportOpen = $state(false)

function exportAs(fmt: ExportFormat) {
	exportOpen = false
	window.location.href = api.exportUrl(sessionId, fmt)
}

const durationText = $derived.by(() => {
	if (!session.ended_at) return ''
	const secs = Math.max(0, Math.floor(session.ended_at - session.started_at))
	const h = Math.floor(secs / 3600)
	const m = Math.floor((secs % 3600) / 60)
	const sec = String(secs % 60).padStart(2, '0')
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
})
</script>

<CardContent class="flex flex-wrap items-center justify-between gap-3">
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
					class="absolute top-full left-0 z-30 mt-1.5 flex w-44 flex-col rounded-lg border bg-popover p-1 shadow-lg"
				>
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
							class="mt-1 rounded border-t px-3 py-1.5 pt-2 text-left hover:bg-accent"
							onclick={() => {
								exportOpen = false
								window.location.href = api.audioUrl(sessionId)
							}}
						>
							Audio (.wav)
						</button>
					{/if}
				</div>
			{/if}
		</div>
	</div>
	<a class="text-primary text-sm hover:underline" href="/sessions">← Back</a>
</CardContent>
