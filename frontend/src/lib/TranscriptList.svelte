<script lang="ts">
/**
 * A transcript's segments: when each was said, who said it, what it says.
 *
 * Shared by the session page and the Dashboard's live pane, so the things
 * only one of them can do arrive as optional callbacks: without `onseek` a
 * timestamp is plain text, without `onrename` a speaker is plain text too.
 * Neither place offers an affordance it has no way to honour - the live pane
 * has no player to seek and no stored session to rename speakers in.
 *
 * Filtering stays with the caller, which owns the count in its own header;
 * `query` comes back down only to mark the hits where they were found.
 */

import { Input } from '$lib/components/ui/input'
import { formatTime, sourceLabel, speakerColor } from '$lib/stores'
import { highlight } from '$lib/transcriptSearch'
import { cn } from '$lib/utils'
import type { ProviderConfig, TranscriptEvent } from '$lib/wire'

let {
	events,
	names = {},
	showSource = false,
	providers = [],
	query = '',
	dimInterim = false,
	emptyText = 'No transcript segments.',
	class: className = 'max-h-[calc(100vh-240px)] overflow-auto',
	element = $bindable(null),
	onseek,
	onrename,
}: {
	events: TranscriptEvent[]
	names?: Record<string, string>
	showSource?: boolean
	providers?: ProviderConfig[]
	/** What the caller filtered by, marked in the text it matched. */
	query?: string
	/** Dim segments still marked interim: the live feed carries those, a
	 *  stored transcript does not. */
	dimInterim?: boolean
	/** Shown in place of the list when there is nothing to show. */
	emptyText?: string
	class?: string
	/** The scrolling box, for a caller that follows the tail. */
	element?: HTMLElement | null
	/** Set to make each timestamp play the session audio from that point. */
	onseek?: (seconds: number) => void
	/** Set to make a speaker's name editable in place. A blank name clears it,
	 *  which is the rename dialog's rule too. */
	onrename?: (label: string, name: string) => Promise<void> | void
} = $props()

function displaySpeaker(label: string): string {
	return names[label] ?? label
}

// Which row's speaker is being edited, and the name being typed into it. By
// row rather than by label because one label appears on dozens of segments,
// and only the one that was clicked should become a field.
let editingRow = $state<number | null>(null)
let draft = $state('')

function startEdit(row: number, label: string) {
	editingRow = row
	draft = names[label] ?? ''
}

/** Closes the field before saving, so the blur that unmounting it fires finds
 *  nothing left to commit and cannot send the same rename twice. */
async function commitEdit(label: string) {
	if (editingRow === null) return
	editingRow = null
	await onrename?.(label, draft)
}

function editKeydown(e: KeyboardEvent, label: string) {
	if (e.key === 'Enter') {
		e.preventDefault()
		void commitEdit(label)
	} else if (e.key === 'Escape') {
		e.preventDefault()
		editingRow = null
	}
}
</script>

{#snippet marked(text: string)}
	{#each highlight(text, query) as part, i (i)}
		{#if part.hit}
			<mark class="rounded-xs bg-amber-300/70 text-inherit dark:bg-amber-400/40">{part.text}</mark>
		{:else}
			{part.text}
		{/if}
	{/each}
{/snippet}

<div bind:this={element} class={className}>
	{#each events as ev, i (i)}
		{@const speaker = ev.speaker}
		<div class="flex items-baseline gap-2.5 py-1">
			{#if onseek}
				<button
					type="button"
					class="shrink-0 cursor-pointer text-xs tabular-nums text-muted-foreground hover:text-primary hover:underline"
					title="Play the recording from here"
					onclick={() => onseek?.(ev.start_ts)}
				>
					{formatTime(ev.start_ts)}
				</button>
			{:else}
				<span class="shrink-0 text-xs tabular-nums text-muted-foreground"
					>{formatTime(ev.start_ts)}</span
				>
			{/if}
			{#if speaker}
				{#if onrename && editingRow === i}
					<Input
						class="h-6 w-36 shrink-0 px-1.5 py-0 text-sm"
						aria-label="Rename {speaker}"
						placeholder={speaker}
						autofocus
						bind:value={draft}
						onkeydown={(e) => editKeydown(e, speaker)}
						onblur={() => commitEdit(speaker)}
					/>
				{:else if onrename}
					<button
						type="button"
						class="shrink-0 cursor-pointer text-sm font-semibold hover:underline"
						style="color: {speakerColor(speaker)}"
						title="Rename this speaker everywhere in the transcript"
						onclick={() => startEdit(i, speaker)}
					>
						{@render marked(displaySpeaker(speaker))}
					</button>
				{:else}
					<span class="shrink-0 text-sm font-semibold" style="color: {speakerColor(speaker)}"
						>{@render marked(displaySpeaker(speaker))}</span
					>
				{/if}
			{/if}
			{#if showSource}
				<span class="shrink-0 rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
					>{sourceLabel(ev.source, providers)}</span
				>
			{/if}
			<span class={cn('min-w-0', dimInterim && !ev.is_final && 'opacity-60 italic')}
				>{@render marked(ev.text)}</span
			>
		</div>
	{/each}
	{#if events.length === 0}
		<p class="text-muted-foreground">{emptyText}</p>
	{/if}
</div>
