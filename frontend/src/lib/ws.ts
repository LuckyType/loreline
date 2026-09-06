/**
 * Auto-reconnecting WebSocket helper for live transcript / log streams.
 *
 * A dropped connection retries forever, with exponential backoff capped at
 * MAX_RETRY_DELAY_MS and jitter mixed into each attempt so that every open
 * tab does not hammer a dead backend on the same fixed interval, in lockstep.
 * A successful connection resets the backoff, so a brief blip recovers as
 * fast as the very first retry would.
 */
export interface LiveSocket {
	close: () => void
}

/** What `connect` reports through `onStatus`: a normal connection, a dropped
 *  one that is backing off before its next try, or one that has stopped
 *  trying (closed deliberately through the returned `LiveSocket`). */
export type ConnectionStatus = 'connected' | 'reconnecting' | 'offline'

/** The delay before the first retry, before backoff has grown it. */
export const INITIAL_RETRY_DELAY_MS = 1000
/** The longest a retry ever waits, no matter how many attempts have failed. */
export const MAX_RETRY_DELAY_MS = 30000

/**
 * Equal-jitter exponential backoff: the target delay doubles with `attempt`
 * up to MAX_RETRY_DELAY_MS, and the actual delay returned is a random point
 * in the top half of that target, `[target / 2, target]`. That keeps the
 * schedule close to the intended curve while still spreading out clients that
 * all failed at the same instant. `random` is injectable so the schedule is
 * testable without mocking `Math.random`.
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
	const target = Math.min(MAX_RETRY_DELAY_MS, INITIAL_RETRY_DELAY_MS * 2 ** Math.max(0, attempt))
	return Math.round(target / 2 + random() * (target / 2))
}

export function connect(
	path: string,
	onMessage: (data: string) => void,
	onStatus?: (status: ConnectionStatus) => void,
): LiveSocket {
	let socket: WebSocket | null = null
	let closed = false
	let retry: ReturnType<typeof setTimeout> | null = null
	let attempt = 0

	const url = () => {
		const proto = location.protocol === 'https:' ? 'wss' : 'ws'
		return `${proto}://${location.host}${path}`
	}

	const open = () => {
		if (closed) return
		socket = new WebSocket(url())
		socket.onopen = () => {
			attempt = 0
			onStatus?.('connected')
		}
		socket.onmessage = (ev) => onMessage(String(ev.data))
		socket.onclose = () => {
			// A deliberate close (below) already reported 'offline' and does not
			// want a retry sneaking in behind it.
			if (closed) return
			onStatus?.('reconnecting')
			retry = setTimeout(open, backoffDelay(attempt))
			attempt++
		}
		socket.onerror = () => socket?.close()
	}

	open()

	return {
		close: () => {
			closed = true
			if (retry) clearTimeout(retry)
			socket?.close()
			onStatus?.('offline')
		},
	}
}
