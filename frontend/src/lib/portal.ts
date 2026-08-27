import type { Action } from 'svelte/action'

/**
 * Moves the node to `document.body` (or another target) on mount, restoring
 * nothing on destroy - used to escape `overflow: hidden` ancestors (e.g. Card)
 * for absolutely-positioned popups.
 */
export const portal: Action<HTMLElement, string | HTMLElement | undefined> = (
	node,
	target = 'body',
) => {
	const el = typeof target === 'string' ? document.querySelector(target) : target
	if (!el) throw new Error(`portal target not found: ${String(target)}`)
	el.appendChild(node)
	return {
		destroy() {
			node.parentNode?.removeChild(node)
		},
	}
}
