<script lang="ts">
import { Dialog as DialogPrimitive } from 'bits-ui'
import XIcon from '@lucide/svelte/icons/x'
import { Button } from '$lib/components/ui/button/index.js'
import { cn, type WithoutChildrenOrChild } from '$lib/utils.js'
import * as Dialog from './index.js'
import DialogPortal from './dialog-portal.svelte'
import type { Snippet } from 'svelte'
import type { ComponentProps } from 'svelte'

let {
	ref = $bindable(null),
	class: className,
	portalProps,
	children,
	showCloseButton = true,
	onInteractOutside,
	...restProps
}: WithoutChildrenOrChild<DialogPrimitive.ContentProps> & {
	portalProps?: WithoutChildrenOrChild<ComponentProps<typeof DialogPortal>>
	children: Snippet
	showCloseButton?: boolean
} = $props()

// Our custom Dropdown portals its option list to <body>, outside this dialog's
// own DOM subtree - bits-ui's outside-interaction check only looks at DOM
// containment, so picking an option reads as an outside click and closes the
// dialog before the pick even registers. Anything inside a portaled dropdown
// popover (marked with data-dropdown-popover) is never an "outside" dialog
// interaction; everything else still gets the caller's own handler, if any.
function handleInteractOutside(e: PointerEvent) {
	if (e.target instanceof Element && e.target.closest('[data-dropdown-popover]')) {
		e.preventDefault()
		return
	}
	onInteractOutside?.(e)
}
</script>

<DialogPortal {...portalProps}>
	<Dialog.Overlay />
	<DialogPrimitive.Content
		bind:ref
		data-slot="dialog-content"
		class={cn(
      'grid max-w-[calc(100%-2rem)] gap-6 rounded-xl bg-popover p-6 text-sm text-popover-foreground ring-1 ring-foreground/10 duration-100 sm:max-w-md data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 fixed top-1/2 left-1/2 z-50 w-full -translate-x-1/2 -translate-y-1/2 outline-none',
      className
    )}
		onInteractOutside={handleInteractOutside}
		{...restProps}
	>
		{@render children?.()}
		{#if showCloseButton}
			<DialogPrimitive.Close data-slot="dialog-close">
				{#snippet child({ props })}
					<Button variant="ghost" class="absolute top-4 right-4" size="icon-sm" {...props}>
						<XIcon />
						<span class="sr-only">Close</span>
					</Button>
				{/snippet}
			</DialogPrimitive.Close>
		{/if}
	</DialogPrimitive.Content>
</DialogPortal>
