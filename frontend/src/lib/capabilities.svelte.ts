/**
 * The browser's view of src/loreline/capabilities.yaml, served whole by
 * GET /api/capabilities.
 *
 * Every "can this provider do that" fact used to be written down twice: once
 * in the backend and once by hand in $lib/types. The two had already drifted,
 * so the tables are gone and this module is the only place the frontend learns
 * what a provider or a model can do.
 *
 * Two rules run through all of it:
 *
 * 1. Fail soft. A missing config, an unreachable endpoint or a model nobody
 *    has annotated yet all mean "unknown", never "unsupported". Losing the
 *    file must not strand an operator mid-campaign, so an unknown answer
 *    leaves the control on rather than hiding it. The one exception is
 *    `hidden`, which is a deliberate release gate and only ever comes from a
 *    config we actually loaded.
 * 2. A declared `false` is believed. That is the entire point: the glossary
 *    checkbox on an OpenRouter model is a silent no-op, and only this data
 *    knows it.
 */

import { api } from './api'
import type {
	CapabilityConfig,
	ConflictFeature,
	Interaction,
	LlmCapabilities,
	ModelAnnotation,
	ModelSpec,
	ProviderKind,
	ProviderSpec,
	TranscribeCapabilities,
	VideoCapabilities,
} from './types'

/** Every interaction, for the soft fallback when a kind is unknown. */
const ALL_INTERACTIONS: Interaction[] = ['transcribe', 'summarize', 'video']

/**
 * Last-resort effort ladder, in picker order, for a model this config does not
 * annotate but the live catalogue reports as reasoning-capable - a model too
 * new for the file, or a self-hosted one nobody could enumerate. Annotated
 * models list their own accepted values and never reach this.
 */
export const REASONING_EFFORTS = [
	'none',
	'minimal',
	'low',
	'medium',
	'high',
	'xhigh',
	'max',
] as const

class CapabilityStore {
	/** Null until the first successful fetch, and after a failed one. Readers
	 *  must treat null as "unknown", never as "nothing is supported". */
	config = $state<CapabilityConfig | null>(null)
	/** Non-empty when the fetch failed. Surfaced as a banner, never as a block. */
	error = $state('')
	#inflight: Promise<void> | null = null

	/** Fetch once per page load and share the result. */
	load(): Promise<void> {
		if (this.config) return Promise.resolve()
		if (this.#inflight) return this.#inflight
		this.#inflight = api
			.capabilities()
			.then((config) => {
				this.config = config
				this.error = ''
			})
			.catch(() => {
				this.error =
					'Capability data could not be loaded, so every provider and model option is being ' +
					'offered. Some of them may not work.'
			})
			.finally(() => {
				this.#inflight = null
			})
		return this.#inflight
	}

	/** Re-fetch after a failure (the banner's retry). */
	reload(): Promise<void> {
		this.config = null
		return this.load()
	}

	provider(kind: ProviderKind | undefined): ProviderSpec | undefined {
		if (!kind) return undefined
		return this.config?.providers[kind]
	}
}

export const capabilities = new CapabilityStore()

/** Whether a kind's connectors have no address of their own, so the provider
 *  row must carry one: true for the self-hosted kind and nothing else. */
export function requiresBaseUrl(spec: ProviderSpec): boolean {
	const { transcribe, summarize, video } = spec.surfaces
	const surfaces = [transcribe?.realtime, transcribe?.batch, summarize, video]
	return surfaces.some((s) => s != null && s.url === null)
}

/** Kick off the one shared fetch; safe to call from anywhere, any number of times. */
export function loadCapabilities(): Promise<void> {
	return capabilities.load()
}

// --- model lookup -----------------------------------------------------------

const globCache = new Map<string, RegExp>()

/** fnmatch-style glob, matching the backend's ModelPattern.matches (which
 *  lowercases both sides). Only `*` and `?` are treated as wildcards. */
function globToRegExp(pattern: string): RegExp {
	const cached = globCache.get(pattern)
	if (cached) return cached
	const body = pattern
		.replace(/[.+^${}()|[\]\\]/g, '\\$&')
		.replace(/\*/g, '.*')
		.replace(/\?/g, '.')
	const re = new RegExp(`^${body}$`)
	globCache.set(pattern, re)
	return re
}

/**
 * The capability entry for one model: an exact spec, else the first matching
 * glob pattern, else undefined for a model nobody has annotated.
 *
 * Patterns are ordered most specific first in the config, so first match wins.
 */
export function annotationFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): ModelAnnotation | undefined {
	const spec = capabilities.provider(kind)
	if (!spec || !modelId) return undefined
	const exact = spec.models.find((m) => m.id === modelId)
	if (exact) return exact
	const id = modelId.toLowerCase()
	return spec.model_patterns.find((p) => globToRegExp(p.match.toLowerCase()).test(id))
}

/** True for an entry that names one model rather than a family of them. A
 *  catch-all glob knows far less than an exact id and is not worth believing
 *  when it says a capability is absent. */
function isExactSpec(entry: ModelAnnotation | undefined): entry is ModelSpec {
	return !!entry && 'id' in entry
}

export function transcribeCapsFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): TranscribeCapabilities | undefined {
	return annotationFor(kind, modelId)?.transcribe ?? undefined
}

export function llmCapsFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): LlmCapabilities | undefined {
	return annotationFor(kind, modelId)?.llm ?? undefined
}

export function videoCapsFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): VideoCapabilities | undefined {
	return annotationFor(kind, modelId)?.video ?? undefined
}

// --- provider level ---------------------------------------------------------

/** Interactions a kind serves. Unknown kinds get all of them: a picker that
 *  offers too much beats one that offers nothing. */
export function interactionsFor(kind: ProviderKind | undefined): Interaction[] {
	return capabilities.provider(kind)?.interactions ?? ALL_INTERACTIONS
}

/** True when any *offered* transcription model streams within an utterance.
 *  Hidden models grant nothing - that is what the flag is for - and glob
 *  patterns count, since a runtime-discovered catalogue lists no models. */
export function hasRealtimeTranscription(kind: ProviderKind | undefined): boolean {
	const spec = capabilities.provider(kind)
	if (!spec) return false
	const models = spec.models.some((m) => !m.hidden && m.transcribe?.realtime)
	return models || spec.model_patterns.some((p) => p.transcribe?.realtime)
}

// --- hidden and deprecated --------------------------------------------------

/** A model held back from every picker: written, not yet verified against the
 *  real API. Only ever true from a config we actually loaded. */
export function isHiddenModel(kind: ProviderKind | undefined, modelId: string): boolean {
	const spec = capabilities.provider(kind)
	return !!spec?.models.some((m) => m.id === modelId && m.hidden)
}

/** Drop hidden models from a picker's list. */
export function withoutHidden(kind: ProviderKind | undefined, ids: string[]): string[] {
	const spec = capabilities.provider(kind)
	if (!spec) return ids
	const hidden = new Set(spec.models.filter((m) => m.hidden).map((m) => m.id))
	return hidden.size ? ids.filter((id) => !hidden.has(id)) : ids
}

/** Vendor-announced sunset date, or null. The model stays selectable: a GM
 *  mid-campaign should not lose it the day the announcement lands. */
export function deprecationFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): string | null {
	const entry = annotationFor(kind, modelId)
	return isExactSpec(entry) ? entry.deprecated : null
}

/** Human line for a deprecated model, or '' for a current one. */
export function deprecationNote(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): string {
	const date = deprecationFor(kind, modelId)
	return date ? `The vendor is retiring this model on ${date}.` : ''
}

// --- per-model feature gating ----------------------------------------------

const FEATURE_LABELS: Record<ConflictFeature, string> = {
	glossary: 'a glossary',
	inline_diarization: 'inline diarization',
	word_timestamps: 'word timestamps',
}

/** Features this model cannot combine with `feature`. */
function conflictsWith(
	caps: TranscribeCapabilities | undefined,
	feature: ConflictFeature,
): ConflictFeature[] {
	if (!caps) return []
	const blocked = new Set<ConflictFeature>()
	for (const group of caps.conflicts) {
		if (group.includes(feature)) {
			for (const other of group) if (other !== feature) blocked.add(other)
		}
	}
	return [...blocked]
}

/**
 * Why `feature` cannot be used on this model right now, or '' when it can.
 *
 * `active` is whatever the user has already switched on: a conflict is about a
 * combination, so the second feature only greys out once the first is set.
 */
export function featureBlockedReason(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
	feature: ConflictFeature,
	active: ConflictFeature[] = [],
): string {
	const caps = transcribeCapsFor(kind, modelId)
	if (!caps) return ''
	if (feature === 'glossary' && !caps.glossary.supported) {
		return 'This model has no way to receive a glossary, so the terms would be ignored.'
	}
	if (feature === 'inline_diarization' && !caps.inline_diarization) {
		return 'This model returns no speaker labels.'
	}
	if (feature === 'word_timestamps' && !caps.word_timestamps) {
		return 'This model returns no word timestamps.'
	}
	const clash = conflictsWith(caps, feature).filter((other) => active.includes(other))
	if (clash.length) {
		const names = clash.map((c) => FEATURE_LABELS[c]).join(' or ')
		return `This model rejects requests that combine ${FEATURE_LABELS[feature]} with ${names}.`
	}
	return ''
}

/** What each feature's absence costs, in the order the warning names them. */
const DROPPED_LABELS: [ConflictFeature, string][] = [
	['word_timestamps', 'word timestamps'],
	['inline_diarization', 'speaker labels'],
	['glossary', 'the glossary'],
]

/**
 * What switching the glossary on costs on this model, or '' when it costs nothing.
 *
 * Distinct from `featureBlockedReason`, which answers "can this be used at
 * all" and disables the control. Here the glossary can be used, and wins: the
 * backend resolves a declared conflict in its favour and leaves the other
 * feature off the request (loreline.capability_config.CONFLICT_PRECEDENCE),
 * because the terms are what the GM turned the toggle on for. That is a real
 * cost to pay knowingly, not an error, so it is a warning beside the toggle
 * rather than a reason it is greyed out.
 *
 * Only models that declare the conflict say anything. A warning every model
 * carries is a warning nobody reads.
 */
export function glossaryDropsWarning(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
): string {
	const caps = transcribeCapsFor(kind, modelId)
	if (!caps?.glossary.supported) return ''
	// Only features this model would otherwise have delivered: a conflict with
	// a feature the model does not have anyway costs nothing.
	const has: Record<ConflictFeature, boolean> = {
		glossary: true,
		inline_diarization: caps.inline_diarization,
		word_timestamps: caps.word_timestamps,
	}
	const clash = conflictsWith(caps, 'glossary')
	const dropped = DROPPED_LABELS.filter(([f]) => clash.includes(f) && has[f])
	if (!dropped.length) return ''
	const names = dropped.map(([, label]) => label).join(' and ')
	// Word timestamps are what a diarizer aligns its speaker spans onto, so
	// without them a whole utterance takes one speaker label rather than a
	// label per word - and that is true of all three modes, since every one of
	// them merges onto the same words.
	const cost = dropped.some(([f]) => f === 'word_timestamps')
		? ' Speakers can then only be placed per utterance instead of per word, so diarization quality suffers in every mode: inline, remote and re-processing.'
		: ''
	return `This model rejects a glossary sent alongside ${names}, so turning it on drops ${dropped.length > 1 ? 'them' : 'it'}.${cost}`
}

/**
 * Whether "Inline (from STT)" yields real speakers for this model.
 *
 * `fallback` is the live catalogue's own answer (ModelInfo.inline_diarization,
 * itself capability-derived server-side), used for a model this config does
 * not annotate.
 */
export function inlineDiarizationFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
	fallback: boolean | undefined,
): boolean {
	const caps = transcribeCapsFor(kind, modelId)
	if (caps) return caps.inline_diarization
	return fallback === true
}

/**
 * Reasoning-effort levels to offer for a summarize model, in config order.
 *
 * An empty list means show no dropdown at all: either the model does not
 * reason, or it reasons without exposing levels - an empty selector would be
 * a dead control in both cases. "none" is dropped for a model that requires
 * reasoning, since the vendor rejects it.
 *
 * `fallbackSupportsReasoning` is the live catalogue's flag, used only where
 * this config has nothing exact to say.
 */
export function reasoningEffortsFor(
	kind: ProviderKind | undefined,
	modelId: string | null | undefined,
	fallbackSupportsReasoning: boolean | undefined,
): string[] {
	const entry = annotationFor(kind, modelId)
	const reasoning = entry?.llm?.reasoning
	if (reasoning?.supported) {
		return reasoning.mandatory
			? reasoning.efforts.filter((e) => e !== 'none')
			: [...reasoning.efforts]
	}
	// A glob pattern that says "no reasoning" is a family-wide guess and cannot
	// know about a model the operator installed last week, so the live
	// catalogue still gets a say. An exact entry is believed.
	if (isExactSpec(entry) && entry.llm) return []
	return fallbackSupportsReasoning ? [...REASONING_EFFORTS] : []
}
