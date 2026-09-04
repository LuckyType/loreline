/**
 * One socket-backed buffer for a page pane: a path, a history seed and a cap.
 *
 * Three panes want the same thing and each used to spell it out again: open a
 * socket, seed the buffer from what is already stored, keep the last N items,
 * scroll to the tail, and say whether the socket is up. The loops were the
 * same, the lifecycles were not - the Dashboard opened its sockets in an
 * onMount and carried an `unmounted` flag so a seed that resolved late would
 * not connect behind a page that was gone, while the session page had already
 * learned that an effect has no such gap. This owns the effect, so every pane
 * gets the lifecycle that works: the socket is opened and its teardown
 * registered in the same breath, and `path` is read reactively, so a route
 * param changing under a reused page component reconnects rather than leaving
 * the pane listening to the previous session.
 *
 * Reconnecting is not this module's business: `connect` already retries
 * forever behind the handle, and closing through that handle is what stops it.
 */

import { untrack } from 'svelte'
import { connect, type LiveSocket } from './ws'

export interface LiveFeedOptions<T> {
	/** Where to listen. Read reactively: a change reconnects. */
	path: () => string
	/** One frame to one item, or `null` to drop it (a malformed frame). */
	parse: (frame: string) => T | null
	/** The history to start from, fetched once before the socket opens so that
	 *  a slow answer can never overwrite a frame already delivered. Failure is
	 *  best effort: the socket still fills the pane. */
	seed?: () => Promise<T[]>
	/** Most items held; the oldest fall off the front. Unset holds them all. */
	cap?: number
	/** Whether to keep an item, given what the buffer already holds. The second
	 *  argument is what a feed whose history and socket can overlap by an item
	 *  needs to recognize the duplicate. */
	accept?: (item: T, held: readonly T[]) => boolean
	/** Whether an arriving item should scroll `element` to the tail. */
	follow?: () => boolean
	/** The socket's open state, for a status indicator. Called with `false` on
	 *  teardown, so a pane that goes away stops claiming a live socket. */
	onstatus?: (open: boolean) => void
}

/** A JSON `parse` for feeds whose frames are objects: a malformed frame is
 *  dropped rather than thrown, since one bad frame is not a dead feed. */
export function jsonFrame<T>(frame: string): T | null {
	try {
		return JSON.parse(frame) as T
	} catch {
		return null
	}
}

export class LiveFeed<T> {
	/** What the pane renders: the seeded history plus everything kept since. */
	items = $state<T[]>([])
	/** The scroll container, for a pane that follows the tail. Bind it with
	 *  `bind:this={feed.element}`; a pane that does not follow leaves it null. */
	element = $state<HTMLElement | null>(null)

	#options: LiveFeedOptions<T>

	/** Construct one at the top level of a component's `<script>`: the effect
	 *  it owns is what closes the socket when that component goes away. */
	constructor(options: LiveFeedOptions<T>) {
		this.#options = options
		$effect(() => {
			const path = options.path()
			let socket: LiveSocket | null = null
			let gone = false
			const open = () => {
				if (gone) return
				socket = connect(path, (frame) => this.#receive(frame), options.onstatus)
			}
			// Untracked: the seed is a request, not a dependency. Reading state
			// inside it would reconnect the socket every time that state moved.
			const history = untrack(() => options.seed?.())
			if (history) {
				history
					.then((items) => {
						if (!gone) this.items = this.#trim(items)
					})
					.catch(() => {
						/* best effort - the socket still delivers new items */
					})
					.finally(open)
			} else {
				open()
			}
			return () => {
				gone = true
				socket?.close()
				options.onstatus?.(false)
			}
		})
	}

	/** Drop everything held. The socket stays open, so the pane fills again. */
	clear() {
		this.items = []
	}

	#receive(frame: string) {
		const item = this.#options.parse(frame)
		if (item === null) return
		if (this.#options.accept && !this.#options.accept(item, this.items)) return
		this.items = this.#trim([...this.items, item])
		if (this.#options.follow?.()) {
			queueMicrotask(() => {
				const el = this.element
				el?.scrollTo(0, el.scrollHeight)
			})
		}
	}

	#trim(items: T[]): T[] {
		const cap = this.#options.cap
		return cap && items.length > cap ? items.slice(-cap) : items
	}
}
