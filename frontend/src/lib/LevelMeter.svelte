<script lang="ts">
/**
 * A small horizontal peak meter: green under 60%, amber to 90%, red above.
 *
 * Shared by the pre-session mic test (Settings > Client) and the Dashboard's
 * live gain meter while a session records, so the two read as the same
 * instrument rather than two meters that happen to agree. Sizing is the
 * caller's choice via `class`; this only ever draws the track and the fill.
 */
import { cn } from '$lib/utils'

interface Props {
	/** 0.0-1.0 peak amplitude. */
	peak: number
	class?: string
}

let { peak, class: className }: Props = $props()

const color = $derived(peak > 0.9 ? 'bg-red-500' : peak > 0.6 ? 'bg-amber-500' : 'bg-emerald-500')
</script>

<div class={cn('h-2.5 overflow-hidden rounded-full bg-foreground/15', className)}>
	<div
		class={cn('h-full rounded-full transition-[width] duration-75', color)}
		style="width: {Math.min(100, Math.round(peak * 100))}%"
	></div>
</div>
