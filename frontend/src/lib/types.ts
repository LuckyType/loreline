/**
 * Types the browser owns.
 *
 * Everything the backend serves is named in `$lib/wire`, off the generated
 * OpenAPI document. What is left here is what the wire does not say: a
 * narrowing the document cannot express, a union the API only ever spells as a
 * query string, and the one display helper that reads capability data.
 */

import { capabilities, hasRealtimeTranscription, supportsInteraction } from './capabilities.svelte'
import type { ModelPattern, ModelSpec, ProviderKind } from './wire'

/**
 * The transcription toggles a conflict group may name.
 *
 * `TranscribeCapabilities.conflicts` is `list[list[str]]` on the wire, and the
 * backend validates the members against exactly these three (see
 * CONFLICT_PRECEDENCE in src/loreline/capability_config.py) rather than typing
 * them. The picker needs the union to label them, so it narrows what it reads.
 */
export type ConflictFeature = 'glossary' | 'inline_diarization' | 'word_timestamps'

const CONFLICT_FEATURES: readonly string[] = ['glossary', 'inline_diarization', 'word_timestamps']

/** Whether a conflict group's member is one this UI knows how to name. */
export function isConflictFeature(feature: string): feature is ConflictFeature {
	return CONFLICT_FEATURES.includes(feature)
}

/** A model's capability entry: one curated model, or the glob that covers a
 *  family of them. */
export type ModelAnnotation = ModelSpec | ModelPattern

/** Export formats `GET /api/session/{id}/export?fmt=` accepts. A query
 *  parameter with a plain string default, so the document does not enumerate
 *  them (see loreline.export). */
export type ExportFormat = 'txt' | 'md' | 'srt' | 'vtt' | 'json'

/** The capability badges shown for a provider, in a stable order. */
export function capabilityBadges(p: { kind: ProviderKind }): string[] {
	// Badges describe rather than gate, so with no config they say nothing
	// instead of guessing. Nothing is hidden by an empty list; the controls
	// themselves stay permissive.
	if (!capabilities.config) return []
	const badges: string[] = []
	if (supportsInteraction(p, 'transcribe')) {
		badges.push(hasRealtimeTranscription(p.kind) ? 'Realtime' : 'Batch')
	}
	if (supportsInteraction(p, 'summarize')) badges.push('Summarizing')
	if (supportsInteraction(p, 'video')) badges.push('Video')
	return badges
}
