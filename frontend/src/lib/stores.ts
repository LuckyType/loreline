import { writable } from 'svelte/store'
import type { Health } from './types'

export const health = writable<Health | null>(null)
export const authed = writable<boolean>(true)

/** Live-feed WebSocket state (set by the Dashboard, surfaced in the header health bubble). */
export const transcriptWs = writable<boolean>(false)
export const logsWs = writable<boolean>(false)

/** Deterministic speaker color from a label, for transcript rendering. */
export function speakerColor(speaker: string | null): string {
	if (!speaker) return '#94a3b8'
	let hash = 0
	for (let i = 0; i < speaker.length; i++) hash = (hash * 31 + speaker.charCodeAt(i)) % 360
	return `hsl(${hash}, 60%, 55%)`
}

export function formatTime(seconds: number): string {
	const s = Math.max(0, Math.floor(seconds))
	const m = Math.floor(s / 60)
	const sec = String(s % 60).padStart(2, '0')
	return `${m}:${sec}`
}
