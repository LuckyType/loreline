<script lang="ts">
/**
 * A titled section that folds away, header and all.
 *
 * The body is wrapped, but the wrapper is `display: contents` unless the
 * caller asks for something else - so by default the children stay direct
 * children of whatever laid the section out, gaps and all, and a caller that
 * needs the body to be a box of its own (one that takes the leftover height
 * and scrolls what does not fit, say) gets one by naming its classes.
 *
 * The header is a row rather than one big button, because a section's own
 * controls belong beside its title and a button inside a button is invalid
 * HTML that breaks the keyboard and the screen reader both. The toggle and the
 * `actions` snippet are therefore siblings: pressing an action cannot fold the
 * section, because the press never reaches the toggle at all, and no handler
 * has to stop a bubble to make that true. The toggle still grows into
 * everything the actions leave, so the header stays clickable wherever there
 * is no control in the way.
 *
 * The row wraps, which is the whole phone story. The toggle asks for 10rem
 * before it will share a line, so once the actions no longer fit beside that
 * they drop to a line of their own rather than squeezing the title to an
 * ellipsis or pushing the last button off a 320px screen.
 */

import { ChevronDown } from '@lucide/svelte'
import type { Snippet } from 'svelte'

let {
	title,
	meta = '',
	open = $bindable(true),
	bodyClass = 'contents',
	actions,
	children,
}: {
	title: string
	meta?: string
	open?: boolean
	/** What the body wrapper is, layout included. Defaults to laying out
	 *  nothing: the children fall through to the caller's own flow. */
	bodyClass?: string
	/** The section's own controls, drawn beside the toggle rather than inside
	 *  it. Shown folded or not, on purpose: a control that disappears with the
	 *  body is one nobody knows the section has. */
	actions?: Snippet
	children: Snippet
} = $props()
</script>

<div class="flex w-full shrink-0 flex-wrap items-center gap-x-2 gap-y-1">
	<button
		class="flex min-w-0 flex-1 basis-40 items-center gap-2 rounded-md text-left"
		aria-expanded={open}
		onclick={() => (open = !open)}
	>
		<ChevronDown
			class="size-4 shrink-0 text-muted-foreground transition-transform {open ? '' : '-rotate-90'}"
		/>
		<h3 class="m-0 font-medium">{title}</h3>
		{#if meta}
			<!-- Truncated rather than left to push: the meta is the least important
			     thing on the line and the only one that grows without bound, so it
			     is what gives way when the actions want the room. -->
			<span class="min-w-0 truncate text-xs text-muted-foreground">{meta}</span>
		{/if}
	</button>
	{#if actions}
		<div class="ml-auto flex flex-wrap items-center justify-end gap-2">
			{@render actions()}
		</div>
	{/if}
</div>
{#if open}
	<div class={bodyClass}>
		{@render children()}
	</div>
{/if}
