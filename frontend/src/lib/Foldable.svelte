<script lang="ts">
/**
 * A titled section that folds away, header and all.
 *
 * The body is wrapped, but the wrapper is `display: contents` unless the
 * caller asks for something else - so by default the children stay direct
 * children of whatever laid the section out, gaps and all, and a caller that
 * needs the body to be a box of its own (one that takes the leftover height
 * and scrolls what does not fit, say) gets one by naming its classes.
 */

import { ChevronDown } from '@lucide/svelte'
import type { Snippet } from 'svelte'

let {
	title,
	meta = '',
	open = $bindable(true),
	bodyClass = 'contents',
	children,
}: {
	title: string
	meta?: string
	open?: boolean
	/** What the body wrapper is, layout included. Defaults to laying out
	 *  nothing: the children fall through to the caller's own flow. */
	bodyClass?: string
	children: Snippet
} = $props()
</script>

<button
	class="flex w-full shrink-0 items-center gap-2 rounded-md text-left"
	aria-expanded={open}
	onclick={() => (open = !open)}
>
	<ChevronDown
		class="size-4 shrink-0 text-muted-foreground transition-transform {open ? '' : '-rotate-90'}"
	/>
	<h3 class="m-0 font-medium">{title}</h3>
	{#if meta}
		<span class="text-xs text-muted-foreground">{meta}</span>
	{/if}
</button>
{#if open}
	<div class={bodyClass}>
		{@render children()}
	</div>
{/if}
