<script lang="ts">
/**
 * The Dashboard's live log pane: the running capture's lines as they are
 * written, with a filter, wrapping and a follow toggle.
 *
 * It carries the running capture only, so it is empty by design between
 * sessions and says so - every finished run keeps its own log behind "Show
 * logs" on the session page, which is where a whole session's log belongs.
 * This one is capped: it is a tail, not an archive.
 */

import { ArrowDownToLine, Filter, Trash2, WrapText } from '@lucide/svelte'
import { Button } from '$lib/components/ui/button'
import { Card } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { confirm } from '$lib/confirm.svelte'
import { LiveFeed } from '$lib/liveFeed.svelte'
import LogLine from '$lib/LogLine.svelte'
import { health, logsWs } from '$lib/stores'
import { cn } from '$lib/utils'

let filter = $state('')
let filterOpen = $state(false)
let following = $state(true)
let wrap = $state(false)

const feed = new LiveFeed<string>({
	path: () => '/ws/logs',
	// A log frame is one line, already formatted by the server.
	parse: (frame) => frame,
	cap: 1000,
	follow: () => following,
	onstatus: (open) => logsWs.set(open),
})

const capturing = $derived($health?.capture_status === 'capturing')

const shownLogs = $derived(
	filter ? feed.items.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : feed.items,
)

function toggleFilter() {
	filterOpen = !filterOpen
	if (!filterOpen) filter = ''
}

async function clear() {
	if (feed.items.length && !(await confirm('Clear the log view?'))) return
	feed.clear()
}
</script>

<Card class="flex min-h-0 flex-col py-4">
	<div class="flex items-center justify-between gap-2 px-4 pb-2">
		<h3 class="m-0">Logs</h3>
		<div class="flex items-center gap-1">
			{#if filterOpen}
				<Input class="w-33" placeholder="filter…" bind:value={filter} autofocus />
			{/if}
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn(
              'opacity-55 hover:opacity-100',
              filterOpen && 'border-emerald-500 opacity-100'
            )}
				title="Filter logs"
				aria-label="Filter logs"
				onclick={toggleFilter}
			>
				<Filter />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn('opacity-55 hover:opacity-100', wrap && 'border-emerald-500 opacity-100')}
				title="Wrap lines"
				aria-label="Wrap lines"
				onclick={() => (wrap = !wrap)}
			>
				<WrapText />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn(
              'opacity-55 hover:opacity-100',
              following && 'border-emerald-500 opacity-100'
            )}
				title="Follow"
				aria-label="Follow"
				onclick={() => (following = !following)}
			>
				<ArrowDownToLine />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class="opacity-55 hover:opacity-100"
				title="Clear logs"
				aria-label="Clear logs"
				onclick={clear}
			>
				<Trash2 />
			</Button>
		</div>
	</div>
	<div
		class="m-0 min-h-0 flex-1 overflow-auto px-4 font-mono text-xs leading-relaxed"
		bind:this={feed.element}
	>
		{#each shownLogs as line, i (i)}
			<LogLine {line} {wrap} />
		{/each}
		{#if shownLogs.length === 0}
			<!-- This panel carries the running capture's lines only, so it is
				     empty by design between sessions. Say so rather than letting
				     it read as a broken feed: every finished run keeps its own log
				     under Show logs on the session page. -->
			<span class="text-muted-foreground">
				{capturing
						? 'No log lines yet.'
						: 'Logs appear here while a session is recording. A finished session keeps its own logs on its page.'}
			</span>
		{/if}
	</div>
</Card>
