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
 *
 * The rows themselves are the session page's list component, so a segment
 * looks the same wherever it is read and the search below only had to be
 * written once. What the page can do to a stored session - seek its audio,
 * rename its speakers - the list simply is not given here.
 */

import { ArrowDownToLine, Filter, Trash2 } from '@lucide/svelte'
import { api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Card } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { confirm } from '$lib/confirm.svelte'
import { jsonFrame, LiveFeed } from '$lib/liveFeed.svelte'
import { transcriptWs, turnKey } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import { matchesQuery } from '$lib/transcriptSearch'
import { cn } from '$lib/utils'
import type { TranscriptEvent } from '$lib/wire'

let autoscroll = $state(true)
let filter = $state('')
let filterOpen = $state(false)

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
	// A streaming connector revises one turn several times before it settles;
	// keyed by turn, each revision lands on the same line instead of adding one.
	key: turnKey,
	follow: () => autoscroll,
	onstatus: (open) => transcriptWs.set(open),
})

// The live capture has no stored speaker names to search by, so the labels the
// backend sent are all there is to match on.
const shownEvents = $derived(
	filter ? feed.items.filter((ev) => matchesQuery(ev, {}, filter)) : feed.items,
)

function toggleFilter() {
	filterOpen = !filterOpen
	if (!filterOpen) filter = ''
}

function onFilterKeydown(e: KeyboardEvent) {
	if (e.key !== 'Escape') return
	e.preventDefault()
	toggleFilter()
}

async function clear() {
	if (feed.items.length && !(await confirm('Clear the transcript view?'))) return
	feed.clear()
}
</script>

<Card class="flex min-h-0 flex-1 flex-col py-4">
	<div class="flex items-center justify-between gap-2 px-4 pb-2">
		<h3 class="m-0">Transcript</h3>
		<div class="flex items-center gap-1">
			{#if filterOpen}
				<Input
					class="w-33"
					placeholder="search…"
					bind:value={filter}
					autofocus
					onkeydown={onFilterKeydown}
				/>
			{/if}
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn('opacity-55 hover:opacity-100', filterOpen && 'border-emerald-500 opacity-100')}
				title="Search the transcript by what was said or who said it"
				aria-label="Search transcript"
				onclick={toggleFilter}
			>
				<Filter />
			</Button>
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
				disabled={feed.items.length === 0}
				onclick={clear}
			>
				<Trash2 />
			</Button>
		</div>
	</div>
	<TranscriptList
		events={shownEvents}
		query={filter}
		dimInterim
		emptyText={filter ? 'No segments match the search.' : 'Waiting for transcript events…'}
		class="min-h-0 flex-1 overflow-auto px-4"
		bind:element={feed.element}
	/>
</Card>
