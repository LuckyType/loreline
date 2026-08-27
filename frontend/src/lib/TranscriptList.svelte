<script lang="ts">
import { speakerColor, formatTime } from '$lib/stores'
import type { TranscriptEvent } from '$lib/types'

let {
	events,
	names = {},
	showSource = false,
}: {
	events: TranscriptEvent[]
	names?: Record<string, string>
	showSource?: boolean
} = $props()

function displaySpeaker(label: string): string {
	return names[label] ?? label
}
</script>

<div class="max-h-[calc(100vh-240px)] overflow-auto">
	{#each events as ev, i (i)}
		<div class="flex items-baseline gap-2.5 py-1">
			<span class="shrink-0 text-xs tabular-nums text-muted-foreground"
				>{formatTime(ev.start_ts)}</span
			>
			{#if ev.speaker}
				<span class="shrink-0 text-sm font-semibold" style="color: {speakerColor(ev.speaker)}">
					{displaySpeaker(ev.speaker)}
				</span>
			{/if}
			{#if showSource}
				<span class="shrink-0 text-xs text-muted-foreground">{ev.source}</span>
			{/if}
			<span class="min-w-0">{ev.text}</span>
		</div>
	{/each}
	{#if events.length === 0}
		<p class="text-muted-foreground">No transcript segments.</p>
	{/if}
</div>
