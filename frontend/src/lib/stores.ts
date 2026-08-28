import { writable } from 'svelte/store'
import type { Health, ProviderConfig } from './types'

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

// Transcript segments and reprocess jobs store a provider *id*, which is a
// 32-char hex string - unreadable in a table or next to a transcript line.
// These resolve it to the provider's name, keeping the id only as a fallback
// for providers that have since been deleted.
const DIARIZE_SOURCE = 'diarize'
const REPROCESS_PREFIX = 'reprocess:'

export function providerName(id: string | null | undefined, providers: ProviderConfig[]): string {
	if (!id) return '-'
	const match = providers.find((p) => p.id === id)
	if (match) return match.name
	// Deleted provider: a short prefix still lets you correlate with the logs.
	return `${id.slice(0, 8)}…`
}

// A transcript segment's `source` is a provider id, a `diarize:<version>` tag,
// or `reprocess:<job id>` - see loreline.models.
export function sourceLabel(source: string, providers: ProviderConfig[]): string {
	if (source === DIARIZE_SOURCE || source.startsWith(`${DIARIZE_SOURCE}:`)) return 'Diarization'
	if (source.startsWith(REPROCESS_PREFIX)) {
		const suffix = source.slice(REPROCESS_PREFIX.length)
		// Legacy rows referenced the provider; current rows reference the job.
		const provider = providers.find((p) => p.id === suffix)
		return provider ? `${provider.name} (re-run)` : `re-run ${suffix.slice(0, 8)}`
	}
	return providerName(source, providers)
}
