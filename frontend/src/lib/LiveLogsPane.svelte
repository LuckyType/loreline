<script lang="ts">
/**
 * The Dashboard's live log pane: the running capture's lines as they are
 * written, with a filter, wrapping and a follow toggle.
 *
 * It carries the running capture only, so it is empty by design between
 * sessions and says so - every finished run keeps its own log behind "Show
 * logs" on the session page, which is where a whole session's log belongs.
 * This one is capped: it is a tail, not an archive.
 *
 * The pane docks under the Transcript rather than sitting beside it, so it
 * owns its own height. The whole title bar is the resize handle and a tap on
 * it folds the pane down to that bar: a thin strip between the two cards would
 * be a 6px target nobody can hit with a finger, while the header row is a big
 * one for a mouse and a thumb alike. It is driven by pointer events, which
 * both of them raise, so there is one code path rather than a mouse one and a
 * touch one.
 *
 * Folding is a layout change and nothing more: the socket stays open and the
 * buffer keeps filling behind the closed bar, which is what the line count
 * beside the title is there to show.
 */

import { ArrowDownToLine, ChevronDown, Filter, Trash2, WrapText } from '@lucide/svelte'
import { Button } from '$lib/components/ui/button'
import { Card } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { confirm } from '$lib/confirm.svelte'
import { LiveFeed } from '$lib/liveFeed.svelte'
import LogLine from '$lib/LogLine.svelte'
import { health, logsWs } from '$lib/stores'
import { cn } from '$lib/utils'

let {
	maxHeight = 0,
}: {
	/** The tallest this pane may be dragged, in px: what the dock has left once
	 *  the Transcript keeps its minimum. Zero means the dock has not been
	 *  measured yet, so there is no ceiling to apply. */
	maxHeight?: number
} = $props()

let filter = $state('')
let filterOpen = $state(false)
let following = $state(true)
let wrap = $state(false)

// Dock height and fold state, kept across visits (best effort - private
// windows etc.), the way the sidebar keeps its own fold.
const DOCK_KEY = 'loreline.logs-dock'
/** What the pane opens to before anyone has dragged it. */
const DEFAULT_HEIGHT = 180
/** A pane shorter than its bar plus this shows too little log to be worth the
 *  room it takes, so a drag that ends there folds it away instead. */
const MIN_BODY_HEIGHT = 48
/** Pointer travel that tells a resize from a tap, in px. */
const DRAG_THRESHOLD = 6

interface Dock {
	/** The remembered open height. A fold leaves it alone, so unfolding lands
	 *  back where the user left it. */
	height: number
	collapsed: boolean
}

function loadDock(): Dock {
	const fallback: Dock = { height: DEFAULT_HEIGHT, collapsed: false }
	try {
		const raw = localStorage.getItem(DOCK_KEY)
		if (!raw) return fallback
		const stored = { ...fallback, ...JSON.parse(raw) }
		return {
			// Anything but a real number is a corrupted entry, not a height.
			height: Number.isFinite(stored.height) ? stored.height : DEFAULT_HEIGHT,
			collapsed: stored.collapsed === true,
		}
	} catch {
		return fallback
	}
}

let dock = $state(loadDock())

function saveDock() {
	try {
		localStorage.setItem(DOCK_KEY, JSON.stringify(dock))
	} catch {
		/* best effort */
	}
}

/** Measured rather than assumed: the bar is as tall as its own buttons, and
 *  that is exactly what the pane folds down to. */
let headerHeight = $state(48)
/** The live height while a drag is in flight, so the pane tracks the pointer
 *  1:1 without persisting every pixel it passes through. */
let dragHeight = $state<number | null>(null)
let dragStartY = 0
let dragStartHeight = 0
let dragMoved = false

const dragging = $derived(dragHeight !== null)
/** The shortest an open pane is allowed to be. */
const minHeight = $derived(headerHeight + MIN_BODY_HEIGHT)
const maxAllowed = $derived(Math.max(minHeight, maxHeight || Number.POSITIVE_INFINITY))
/** What the card is right now: the drag in progress, else the resting state. */
const paneHeight = $derived(
	dragHeight ??
		(dock.collapsed ? headerHeight : Math.min(Math.max(dock.height, minHeight), maxAllowed)),
)
/** Whether the log body shows. Read off the height rather than the stored
 *  flag, so a drag closes it as it passes the bar rather than on release. */
const bodyOpen = $derived(paneHeight > headerHeight + 2)

function startDrag(e: PointerEvent & { currentTarget: HTMLElement }) {
	// The bar's own controls keep their taps: the filter box and every toggle
	// on it would otherwise be swallowed by the drag that starts here. The
	// cluster is named rather than the buttons alone because a *disabled*
	// button (Clear, with nothing to clear) is not the target of its own
	// clicks - the browser hands those to whatever is behind it, which is this
	// bar, so folding the pane was the one thing a dead button still did.
	if (e.target instanceof Element && e.target.closest('button, input, [data-bar-controls]')) return
	dragStartY = e.clientY
	dragStartHeight = paneHeight
	dragMoved = false
	dragHeight = paneHeight
	// Capture, so a finger that slides off the bar keeps resizing it.
	e.currentTarget.setPointerCapture(e.pointerId)
}

function moveDrag(e: PointerEvent) {
	if (dragHeight === null) return
	// The pane grows upwards, against the pointer's own direction of travel.
	const dy = dragStartY - e.clientY
	if (Math.abs(dy) > DRAG_THRESHOLD) dragMoved = true
	if (!dragMoved) return
	dragHeight = Math.min(Math.max(dragStartHeight + dy, headerHeight), maxAllowed)
}

function endDrag() {
	if (dragHeight === null) return
	const height = dragHeight
	dragHeight = null
	// Nothing moved, so that was a tap on the bar rather than a resize.
	if (!dragMoved) {
		toggleCollapsed()
		return
	}
	if (height < minHeight) dock.collapsed = true
	else {
		dock.collapsed = false
		dock.height = height
	}
	saveDock()
}

/** A gesture the browser took over (a system swipe, say): drop the drag and
 *  leave the pane where it rested. */
function cancelDrag() {
	dragHeight = null
}

function toggleCollapsed() {
	dock.collapsed = !dock.collapsed
	saveDock()
}

/** Wrap a control on the bar so its click can never read as a tap on the bar
 *  behind it. */
function headerAction(run: () => void) {
	return (e: Event) => {
		e.stopPropagation()
		run()
	}
}

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

// A folded body is display:none, and the browser drops a hidden box's scroll
// position, so a pane that follows the tail would come back parked on its
// oldest line. Put it back on the newest one instead.
$effect(() => {
	if (!bodyOpen || !following) return
	const el = feed.element
	el?.scrollTo(0, el.scrollHeight)
})

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
	if (feed.items.length && !(await confirm('Clear the log view?'))) return
	feed.clear()
}
</script>

<Card
	class={cn(
		'flex min-h-0 flex-col gap-0 py-0',
		dragging ? 'transition-none' : 'transition-[flex-basis] duration-150 ease-out',
	)}
	style="flex: 0 0 {paneHeight}px"
>
	<!--
		The bar carries a heading and five buttons, so it can take no widget role
		of its own without hiding them from a screen reader. Dragging it is a
		pointer-only enhancement: the chevron below is the keyboard-reachable way
		to fold and unfold, and nothing else on the bar is drag-only.
	-->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		bind:clientHeight={headerHeight}
		class={[
			'group/dockbar relative flex shrink-0 cursor-ns-resize touch-none items-center justify-between gap-2 px-4 pt-2.5 pb-1.5 select-none',
			dragging && 'bg-accent/40',
		]}
		title="Drag to resize the logs, tap to fold them away"
		onpointerdown={startDrag}
		onpointermove={moveDrag}
		onpointerup={endDrag}
		onpointercancel={cancelDrag}
	>
		<!-- The grip: what says the whole bar can be dragged. -->
		<span
			class={[
				'pointer-events-none absolute top-1.5 left-1/2 h-[3px] w-8 -translate-x-1/2 rounded-full transition-colors',
				dragging ? 'bg-primary' : 'bg-border group-hover/dockbar:bg-primary',
			]}
		></span>
		<h3 class="m-0 flex items-baseline gap-2">
			Logs
			<!-- Kept in the bar so a folded pane still shows its feed climbing. -->
			<span class="text-xs font-normal tabular-nums text-muted-foreground">{shownLogs.length}</span>
		</h3>
		<div class="flex items-center gap-1" data-bar-controls>
			{#if filterOpen}
				<Input
					class="w-33"
					placeholder="filter…"
					bind:value={filter}
					autofocus
					onkeydown={onFilterKeydown}
				/>
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
				onclick={headerAction(toggleFilter)}
			>
				<Filter />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class={cn('opacity-55 hover:opacity-100', wrap && 'border-emerald-500 opacity-100')}
				title="Wrap lines"
				aria-label="Wrap lines"
				onclick={headerAction(() => (wrap = !wrap))}
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
				onclick={headerAction(() => (following = !following))}
			>
				<ArrowDownToLine />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class="opacity-55 hover:opacity-100"
				title="Clear logs"
				aria-label="Clear logs"
				disabled={feed.items.length === 0}
				onclick={headerAction(clear)}
			>
				<Trash2 />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class="opacity-55 hover:opacity-100"
				title={dock.collapsed ? 'Show logs' : 'Hide logs'}
				aria-label={dock.collapsed ? 'Show logs' : 'Hide logs'}
				aria-expanded={!dock.collapsed}
				onclick={headerAction(toggleCollapsed)}
			>
				<ChevronDown class={cn('transition-transform', dock.collapsed && 'rotate-180')} />
			</Button>
		</div>
	</div>
	<div
		class={[
			'm-0 min-h-0 flex-1 overflow-auto px-4 pb-3 font-mono text-xs leading-relaxed',
			!bodyOpen && 'hidden',
		]}
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
