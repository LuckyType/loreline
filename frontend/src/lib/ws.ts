/**
 * Auto-reconnecting WebSocket helper for live transcript / log streams.
 */
export interface LiveSocket {
	close: () => void
}

export function connect(
	path: string,
	onMessage: (data: string) => void,
	onStatus?: (open: boolean) => void,
): LiveSocket {
	let socket: WebSocket | null = null
	let closed = false
	let retry: ReturnType<typeof setTimeout> | null = null

	const url = () => {
		const proto = location.protocol === 'https:' ? 'wss' : 'ws'
		return `${proto}://${location.host}${path}`
	}

	const open = () => {
		if (closed) return
		socket = new WebSocket(url())
		socket.onopen = () => onStatus?.(true)
		socket.onmessage = (ev) => onMessage(String(ev.data))
		socket.onclose = () => {
			onStatus?.(false)
			if (!closed) retry = setTimeout(open, 1500)
		}
		socket.onerror = () => socket?.close()
	}

	open()

	return {
		close: () => {
			closed = true
			if (retry) clearTimeout(retry)
			socket?.close()
		},
	}
}
