<script lang="ts">
import { ChevronDown } from '@lucide/svelte'
import type { Snippet } from 'svelte'

let {
	title,
	meta = '',
	open = $bindable(true),
	children,
}: {
	title: string
	meta?: string
	open?: boolean
	children: Snippet
} = $props()
</script>

<button
	class="flex w-full items-center gap-2 rounded-md text-left"
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
	{@render children()}
{/if}
