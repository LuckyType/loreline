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
 *  last one drains.
 *
 *  'cancelled' is not in flight, and that is what re-enables Delete on a row
 *  the GM stopped: the row only reaches that status once the run has actually
 *  stopped writing, so there is nothing left to wait for. The list of live
 *  states is written out rather than derived from the terminal ones, because
 *  this is the predicate a poll runs on: a status nobody thought about must
 *  end the poll, not keep it going forever. */
export function inFlight(job: ReprocessJob): boolean {
	return job.status === 'queued' || job.status === 'running'
}

/**
 * What one transcript version is called, wherever the UI has to name it.
 *
 * The live capture is 'original'; every re-transcription is its job id, and a
 * 32-char hex string is unreadable in a sentence, so it is cut to the same
 * 8-char prefix the version table and the transcript's own info bar show. One
 * helper because the Export menu, the Summarize dialog and the log viewer all
 * have to say the same thing about the same version - a page that names a
 * version two different ways is how "export gave me the wrong transcript"
 * became hard to see in the first place.
 */
export function versionLabel(version: string): string {
	return version === 'original' ? 'original' : version.slice(0, 8)
}

/** What the diarizer a job used is called: named in the version list, and
 *  again next to the transcript that job relabeled. */
export function diarizerLabel(job: ReprocessJob): string {
	if (job.diarization.mode === 'openai') return 'OpenAI · gpt-4o-transcribe-diarize'
	if (job.diarization.mode === 'remote')
		return `sherpa-onnx${job.diarization.endpoint ? ` · ${job.diarization.endpoint}` : ''}`
	return job.diarization.mode
}

/**
 * How long something ran, as m:ss or h:mm:ss, from the two moments it ran
 * between.
 *
 * Empty when there is no end, which is three different rows: a capture still
 * going, one that died without being stopped, and a merged session, which was
 * assembled out of other sessions rather than recorded and so never had an end
 * of its own. Each caller decides what to print instead.
 */
export function fmtDuration(startedAt: number, endedAt: number | null): string {
	if (!endedAt) return ''
	const secs = Math.max(0, Math.floor(endedAt - startedAt))
	const h = Math.floor(secs / 3600)
	const m = Math.floor((secs % 3600) / 60)
	const sec = String(secs % 60).padStart(2, '0')
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
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
 * a re-run's copy of a turn is a different row from the original's. The
 * session id is part of it too: a vendor's own turn handle can be a small
 * per-session integer starting back at 0 (AssemblyAI does this), so without
 * it a new session's first turn would replace the previous session's row in
 * a pane that never clears between sessions. */
export function turnKey(event: TranscriptEvent): string | null {
	return event.turn_id ? `${event.session_id}:${event.source}:${event.turn_id}` : null
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
