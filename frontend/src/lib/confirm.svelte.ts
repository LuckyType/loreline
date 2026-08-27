/**
 * Promise-based replacement for window.confirm(), backed by a single
 * <ConfirmDialog> mounted once in the root layout. Call confirm(...) from
 * anywhere; it opens that dialog and resolves once the user picks.
 */

export type ConfirmOptions = {
	title?: string
	description: string
	confirmLabel?: string
	cancelLabel?: string
	/** Styles the confirm button as destructive (deletions, irreversible actions). */
	destructive?: boolean
}

class ConfirmState {
	open = $state(false)
	options = $state<ConfirmOptions>({ description: '' })
	#resolve: ((value: boolean) => void) | null = null

	request(opts: ConfirmOptions | string): Promise<boolean> {
		this.options = typeof opts === 'string' ? { description: opts } : opts
		this.open = true
		return new Promise((resolve) => {
			this.#resolve = resolve
		})
	}

	/** Settle the pending promise; a no-op if already settled (e.g. Escape then a stray click). */
	settle(value: boolean) {
		this.open = false
		this.#resolve?.(value)
		this.#resolve = null
	}
}

export const confirmState = new ConfirmState()

/** Ask the user to confirm; resolves true/false. Pass a string for a plain message. */
export function confirm(opts: ConfirmOptions | string): Promise<boolean> {
	return confirmState.request(opts)
}
