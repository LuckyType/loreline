import { writable } from 'svelte/store'
import type { Health, ProviderConfig, ReprocessJob, TranscriptEvent } from './wire'
import type { ConnectionStatus } from './ws'

export const health = writable<Health | null>(null)
export const authed = writable<boolean>(true)

/** Live-feed WebSocket state (set by the Dashboard, surfaced in the header health bubble). */
export const transcriptWs = writable<ConnectionStatus>('offline')
export const logsWs = writable<ConnectionStatus>('offline')

/** Deterministic speaker color from a label, for transcript rendering. */
export function speakerColor(speaker: string | null): string {
	if (!speaker) return '#94a3b8'
	let hash = 0
	for (let i = 0; i < speaker.length; i++) hash = (hash * 31 + speaker.charCodeAt(i)) % 360
	return `hsl(${hash}, 60%, 55%)`
}

/** One moment on the session clock, spelled for a person. */
export function fmtWhen(ts: number): string {
	return new Date(ts * 1000).toLocaleString()
}

/** Whether a re-processing job is still going: shared by the version list,
 *  which counts up while it runs, and the page's poll, which stops when the
 *  last one drains. */
export function inFlight(job: ReprocessJob): boolean {
	return job.status === 'queued' || job.status === 'running'
}

/** What the diarizer a job used is called: named in the version list, and
 *  again next to the transcript that job relabeled. */
export function diarizerLabel(job: ReprocessJob): string {
	if (job.diarization.mode === 'openai') return 'OpenAI · gpt-4o-transcribe-diarize'
	if (job.diarization.mode === 'remote')
		return `sherpa-onnx${job.diarization.endpoint ? ` · ${job.diarization.endpoint}` : ''}`
	return job.diarization.mode
}

export function formatTime(seconds: number): string {
	const s = Math.max(0, Math.floor(seconds))
	const m = Math.floor(s / 60)
	const sec = String(s % 60).padStart(2, '0')
	return `${m}:${sec}`
}

// A stored WAV this short has captured nothing: an errored session's bare
// ~44-byte header decodes to exactly 0 s. The small margin above zero is only
// for rounding - any real utterance clears it easily.
const EMPTY_AUDIO_MAX_S = 0.05

/** What an empty recording is called, wherever one is mentioned. */
export const EMPTY_AUDIO_NOTE = 'This session has no captured audio - the recording is empty.'

/** Whether a session's stored recording is a header with no audio behind it.
 *  One check, shared: the export menu labels its entry from it, and the player
 *  says so instead of offering controls that would play nothing. */
export function audioIsEmpty(durationS: number | null | undefined): boolean {
	return durationS != null && durationS <= EMPTY_AUDIO_MAX_S
}

// Transcript segments and reprocess jobs store a provider *id*, which is a
// 32-char hex string - unreadable in a table or next to a transcript line.
// These resolve it to the provider's name, keeping the id only as a fallback
// for providers that have since been deleted.
const DIARIZE_SOURCE = 'diarize'
const REPROCESS_PREFIX = 'reprocess:'
/** `TranscriptEvent.source` of the marker a streaming connector leaves where a
 *  dead connection swallowed audio - see loreline.models.GAP_SOURCE. */
export const GAP_SOURCE = 'gap'

export function providerName(id: string | null | undefined, providers: ProviderConfig[]): string {
	if (!id) return '-'
	const match = providers.find((p) => p.id === id)
	if (match) return match.name
	// Deleted provider: a short prefix still lets you correlate with the logs.
	return `${id.slice(0, 8)}…`
}

/** How a feed knows two events are one turn being revised.
 *
 * A streaming connector publishes a turn as a growing interim and then as a
 * final, all carrying one `turn_id`; the utterance path publishes each segment
 * once, settled, with none. Null here means "its own item", so a pane keyed on
 * this behaves exactly as it did before turns existed.
 *
 * The source is part of the key because a session's versions share the table:
 * a re-run's copy of a turn is a different row from the original's. */
export function turnKey(event: TranscriptEvent): string | null {
	return event.turn_id ? `${event.source}:${event.turn_id}` : null
}

// A transcript segment's `source` is a provider id, `gap`, a `diarize:<version>`
// tag, or `reprocess:<job id>` - see loreline.models.
export function sourceLabel(source: string, providers: ProviderConfig[]): string {
	if (source === GAP_SOURCE) return 'Lost audio'
	if (source === DIARIZE_SOURCE || source.startsWith(`${DIARIZE_SOURCE}:`)) return 'Diarization'
	if (source.startsWith(REPROCESS_PREFIX)) {
		const suffix = source.slice(REPROCESS_PREFIX.length)
		// Legacy rows referenced the provider; current rows reference the job.
		const provider = providers.find((p) => p.id === suffix)
		return provider ? `${provider.name} (re-run)` : `re-run ${suffix.slice(0, 8)}`
	}
	return providerName(source, providers)
}
