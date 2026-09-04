<script lang="ts">
/**
 * The Dashboard's live transcript: whatever the running capture has said.
 *
 * The socket only ever carries what is pushed after it opens, so leaving the
 * Dashboard and coming back would otherwise look like the session had lost
 * everything said before that point, though it is all persisted. The feed
 * seeds itself from the active session's stored transcript first, and caps
 * what it holds: this is a view of the last few hundred segments, not the
 * session page's full one.
 */

import { ArrowDownToLine, Trash2 } from '@lucide/svelte'
import { api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Card } from '$lib/components/ui/card'
import { confirm } from '$lib/confirm.svelte'
import { jsonFrame, LiveFeed } from '$lib/liveFeed.svelte'
import { formatTime, speakerColor, transcriptWs } from '$lib/stores'
import { cn } from '$lib/utils'
import type { TranscriptEvent } from '$lib/wire'

let autoscroll = $state(true)

/** The active session's stored transcript, if a session is running. Best
 *  effort: with no answer the pane simply starts empty and fills live. */
async function activeTranscript(): Promise<TranscriptEvent[]> {
	const h = await api.health()
	if (!h.active_session_id) return []
	const detail = await api.getSession(h.active_session_id)
	return detail.transcript
}

const feed = new LiveFeed<TranscriptEvent>({
	path: () => '/ws/transcript',
	parse: jsonFrame,
	seed: activeTranscript,
	cap: 500,
	follow: () => autoscroll,
	onstatus: (open) => transcriptWs.set(open),
})

async function clear() {
	if (feed.items.length && !(await confirm('Clear the transcript view?'))) return
	feed.clear()
}
</script>

<Card class="flex min-h-0 flex-col py-4">
	<div class="flex items-center justify-between gap-2 px-4 pb-2">
		<h3 class="m-0">Transcript</h3>
		<div class="flex gap-1">
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn(
              'opacity-55 hover:opacity-100',
              autoscroll && 'border-emerald-500 opacity-100'
            )}
				title="Auto-scroll"
				aria-label="Auto-scroll"
				onclick={() => (autoscroll = !autoscroll)}
			>
				<ArrowDownToLine />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class="opacity-55 hover:opacity-100"
				title="Clear transcript"
				aria-label="Clear transcript"
				onclick={clear}
			>
				<Trash2 />
			</Button>
		</div>
	</div>
	<div class="min-h-0 flex-1 overflow-auto px-4" bind:this={feed.element}>
		{#if feed.items.length === 0}
			<p class="text-muted-foreground">Waiting for transcript events…</p>
		{/if}
		{#each feed.items as ev, i (i)}
			<div class="flex items-baseline gap-2.5 py-1">
				<span class="text-xs tabular-nums text-muted-foreground">{formatTime(ev.start_ts)}</span>
				{#if ev.speaker}
					<span class="text-sm font-semibold" style="color: {speakerColor(ev.speaker)}"
						>{ev.speaker}</span
					>
				{/if}
				<span class={cn('min-w-0', !ev.is_final && 'opacity-60 italic')}>{ev.text}</span>
			</div>
		{/each}
	</div>
</Card>
