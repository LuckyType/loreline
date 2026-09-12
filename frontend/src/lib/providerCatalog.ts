/**
 * The vendor list two wizards offer, and the one-line pitch beside each name.
 *
 * There are two places a GM picks a provider out of a list: Settings >
 * Providers, and the first-run wizard's provider step. They ask the same
 * question and must show the same vendors in the same order with the same
 * copy, so the list lives here rather than in either of them. A second copy is
 * how the two would end up disagreeing about which vendors exist the first
 * time one is added.
 *
 * Only what `/api/capabilities` does not carry is written here. Label, hosting,
 * key URL and whether a base URL is required all come off the served config,
 * because keeping a second copy of those is exactly what let them drift apart
 * before.
 */

import { capabilities, requiresBaseUrl } from '$lib/capabilities.svelte'
import type { AuthKind, Hosting, ProviderKind } from '$lib/wire'

interface ProviderPresentation {
	/** Only for a kind the operator has to point somewhere. */
	baseUrlPlaceholder?: string
	note: string
}

// Also the running order, which stays put whether or not the config loaded.
const PRESENTATION: Record<ProviderKind, ProviderPresentation> = {
	deepgram: { note: 'Streaming WS · inline diarization.' },
	assemblyai: { note: 'Streaming WS · inline diarization.' },
	openai: { note: 'Realtime transcription and session summaries.' },
	gemini: { note: 'API key · diarization · word timestamps.' },
	openrouter: { note: 'One key for many vendors. Transcription, summaries and video.' },
	xai: { note: 'Streaming WS · inline diarization. Summaries and Grok Imagine video.' },
	openai_compat: {
		baseUrlPlaceholder: 'http://localhost:8000/v1',
		note: 'Speaches, whisper.cpp, Ollama, LM Studio, vLLM. Transcription and/or summaries.',
	},
}

/** One row of a provider list: the served facts, joined to the copy above. */
export interface ProviderChoice {
	kind: ProviderKind
	label: string
	/** Null when the config never loaded - such a row shows under both hosting
	 *  steps rather than disappearing from the wizard entirely. */
	hosting: Hosting | null
	auth: AuthKind
	keyUrl: string | null
	baseUrlPlaceholder: string | null
	note: string
}

/**
 * Every offerable provider, in presentation order.
 *
 * A plain function rather than a stored value, so a caller wraps it in its own
 * `$derived` and the list picks up the real labels and key links the moment
 * `/api/capabilities` answers - which may well be after a wizard is already
 * open.
 */
export function providerCatalog(): ProviderChoice[] {
	return (Object.keys(PRESENTATION) as ProviderKind[]).map((kind): ProviderChoice => {
		const spec = capabilities.provider(kind)
		const copy = PRESENTATION[kind]
		// A surface the config leaves without an address is one only the operator can supply.
		const needsBaseUrl = spec ? requiresBaseUrl(spec) : !!copy.baseUrlPlaceholder
		return {
			kind,
			label: spec?.label ?? kind,
			hosting: spec?.hosting ?? null,
			auth: spec?.auth ?? 'optional',
			keyUrl: spec?.key_url ?? null,
			baseUrlPlaceholder: needsBaseUrl ? (copy.baseUrlPlaceholder ?? '') : null,
			note: copy.note,
		}
	})
}

/** The rows to show under one hosting answer. A row whose hosting is unknown
 *  shows under both, rather than being unreachable until the config loads. */
export function choicesForHosting(choices: ProviderChoice[], hosting: Hosting): ProviderChoice[] {
	return choices.filter((c) => c.hosting === null || c.hosting === hosting)
}

/** What this kind's API key field should be called. 'optional' is a
 *  self-hosted endpoint that may or may not check a key. */
export function apiKeyLabel(choice: ProviderChoice | undefined, editing: boolean): string {
	const base = choice?.auth === 'optional' ? 'API key (optional)' : 'API key'
	return editing ? `${base} - blank = keep current` : base
}
