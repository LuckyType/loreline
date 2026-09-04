/**
 * Everything a page needs before it can offer a provider for an action: the
 * provider rows, the stored action defaults, and the capability config that
 * says which rows may be offered for what.
 *
 * Each page used to fetch the first two itself, keep its own blank defaults
 * literal, and seed its picker in an onMount or an effect, racing the one
 * capability fetch the layout starts and nobody awaits. A snapshot that landed
 * before the config gated nothing, so a seed could name a row the picker then
 * refused to list. One store loads all three together, and the pages read
 * pure derivations from it: a seed is a `$derived` over this store, so it can
 * only ever be computed from a gated list.
 *
 * Deduped like the capability store: one fetch per browser session, shared,
 * and `reload` for the settings page after it changes a row. The stored
 * defaults change only through `saveDefaults`, which keeps them current
 * without a second round trip.
 */

import { api } from './api'
import {
	capabilities,
	loadCapabilities,
	preferredModel,
	supportsInteraction,
} from './capabilities.svelte'
import type { ActionDefaults, Interaction, ProviderConfig, ProviderKind } from './types'

/** What a page is picking a provider for: one of the wire interactions, or a
 *  live capture, which is transcription narrowed to the rows that can stream a
 *  session (OpenRouter transcribes stored audio only). */
export type Action = Interaction | 'capture'

/** The wire type leaves some fields optional; every reader here gets all of
 *  them, so no picker needs a `?? ''` and no page needs its own blank literal. */
export type CompleteDefaults = Required<ActionDefaults>

/** Fill in every field. Also the one place a legacy stored "" diarization
 *  mode becomes the explicit "none" it means, so a picker shows one spelling
 *  of "no diarization for new sessions" rather than two options. */
export function completeDefaults(raw?: Partial<ActionDefaults> | null): CompleteDefaults {
	return {
		stt_provider: raw?.stt_provider ?? '',
		stt_model: raw?.stt_model ?? '',
		diar_mode: raw?.diar_mode || 'none',
		diar_endpoint: raw?.diar_endpoint ?? '',
		summarize_provider: raw?.summarize_provider ?? '',
		summarize_model: raw?.summarize_model ?? '',
		summarize_prompt: raw?.summarize_prompt ?? '',
		summarize_reasoning_effort: raw?.summarize_reasoning_effort ?? '',
		video_provider: raw?.video_provider ?? '',
		video_model: raw?.video_model ?? '',
		strict_model_filtering: raw?.strict_model_filtering ?? true,
	}
}

type ProviderKey = 'stt_provider' | 'summarize_provider' | 'video_provider'
type ModelKey = 'stt_model' | 'summarize_model' | 'video_model'

/** Action defaults are stored as provider/model pairs; this is which pair
 *  each action reads. A live capture is a transcription, so it shares the pair. */
const PAIR: Record<Action, [provider: ProviderKey, model: ModelKey]> = {
	capture: ['stt_provider', 'stt_model'],
	transcribe: ['stt_provider', 'stt_model'],
	summarize: ['summarize_provider', 'summarize_model'],
	video: ['video_provider', 'video_model'],
}

/** True for a row that can drive a live capture: transcribe-capable, and not
 *  a kind the config marks as stored-audio only. Permissive without a config,
 *  like every gate: offering too much beats hiding a row an operator needs. */
function supportsCapture(p: { kind: ProviderKind }): boolean {
	return (
		supportsInteraction(p, 'transcribe') && capabilities.provider(p.kind)?.live_capture !== false
	)
}

class ActionSetupStore {
	/** Empty until the first successful fetch. Replaced whole, never mutated. */
	providers = $state.raw<ProviderConfig[]>([])
	/** Always complete: blank until loaded, then whatever the server stores. */
	defaults = $state.raw<CompleteDefaults>(completeDefaults())
	/** Non-empty when the provider list could not be fetched. The defaults
	 *  failing is not an error: a fresh install has none. */
	error = $state('')
	#loaded = false
	#inflight: Promise<void> | null = null

	/** Fetch once per page load and share the result. Resolves either way:
	 *  a failure is recorded in `error`, not thrown. */
	load(): Promise<void> {
		if (this.#loaded) return Promise.resolve()
		if (this.#inflight) return this.#inflight
		this.#inflight = this.#fetch().finally(() => {
			this.#inflight = null
		})
		return this.#inflight
	}

	/** Re-fetch after a page changed the rows (added, edited, removed one).
	 *  The current snapshot stays until the new one lands, so nothing seeded
	 *  from it flickers to empty in between. */
	reload(): Promise<void> {
		this.#loaded = false
		return this.load()
	}

	/** Persist a new set of defaults and adopt what the server stored. Throws
	 *  on failure, since the page has the message slot for it. */
	async saveDefaults(body: ActionDefaults): Promise<void> {
		this.defaults = completeDefaults(await api.setDefaults(body))
	}

	async #fetch(): Promise<void> {
		// All three together: a provider list read before the config landed
		// would gate nothing, and a seed computed from it could name a row the
		// picker then refuses to list. The capability fetch is the layout's
		// shared one, so this joins it rather than starting another.
		const [providers, defaults] = await Promise.all([
			api.listProviders().then(
				(rows) => {
					this.error = ''
					return rows
				},
				(err: unknown) => {
					this.error = err instanceof Error ? err.message : 'failed to load providers'
					return null
				},
			),
			api.getDefaults().catch(() => null),
			loadCapabilities(),
		])
		if (providers) this.providers = providers
		this.defaults = completeDefaults(defaults)
		this.#loaded = !!providers
	}

	provider(id: string | null | undefined): ProviderConfig | undefined {
		if (!id) return undefined
		return this.providers.find((p) => p.id === id)
	}

	/** The rows offerable for an action, in stored order. */
	providersFor(action: Action): ProviderConfig[] {
		if (action === 'capture') return this.providers.filter(supportsCapture)
		return this.providers.filter((p) => supportsInteraction(p, action))
	}

	/** Which row a picker starts on: the stored default while it is still
	 *  offerable, else `fallback` (a page's own second choice, such as the row
	 *  that captured the session) while that is, else the first offerable row,
	 *  else undefined. A default naming a row that has since been deleted or
	 *  lost the ability is skipped, never selected into a dead id. */
	preferredProvider(action: Action, fallback?: string | null): ProviderConfig | undefined {
		const rows = this.providersFor(action)
		const stored = this.defaults[PAIR[action][0]]
		return (
			(stored ? rows.find((p) => p.id === stored) : undefined) ??
			(fallback ? rows.find((p) => p.id === fallback) : undefined) ??
			rows[0]
		)
	}

	/** The model half of an action's stored default, but only while `provider`
	 *  is its provider half: the pair is one choice, and the model half means
	 *  nothing to any other row. '' otherwise. */
	pairedDefault(action: Action, provider: ProviderConfig | undefined): string {
		const [providerKey, modelKey] = PAIR[action]
		if (!provider || provider.id !== this.defaults[providerKey]) return ''
		return this.defaults[modelKey]
	}

	/** Which model a picker starts on for this action on `provider` (the
	 *  preferred row when none is given): `preferredModel` fed the paired
	 *  default, so the rule is stated once. Video models are not favourites,
	 *  so the generate dialog seeds from its catalogue instead. */
	preferredModelFor(
		action: Action,
		provider: ProviderConfig | undefined = this.preferredProvider(action),
	): string {
		return preferredModel(provider, this.pairedDefault(action, provider))
	}
}

export const actionSetup = new ActionSetupStore()
