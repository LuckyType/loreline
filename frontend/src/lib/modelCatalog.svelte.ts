/**
 * The browser's model catalogue: which models a provider row offers for one
 * interaction.
 *
 * Every picker used to fetch and cache its own copy of that answer, with its
 * own dedupe, and the metadata for the picked model lived inside one picker
 * instance and had to be pushed upward through a callback. Now one store per
 * browser session answers the question, deduped per provider row, interaction
 * and refresh token, and the pickers are views over it: a picker that
 * unmounts forgets nothing, a slow catalogue for one provider never blocks the
 * next one, and a parent that needs one model's metadata reads it here.
 *
 * `load` is an action, never a query: it subscribes the caller to nothing, so
 * it is safe to call from an effect without that effect re-running (and
 * re-fetching) every time the store changes. The readers (`list`, `loading`,
 * `settled`) are what a derived tracks.
 */

import { untrack } from 'svelte'
import { SvelteMap, SvelteSet } from 'svelte/reactivity'
import { api } from './api'
import { withoutHiddenRows } from './capabilities.svelte'
import type { Interaction, ModelInfo, ProviderConfig, VideoModelInfo } from './wire'

/** Any value that should invalidate a cached list when it changes: the server
 *  applies the "only show compatible models" setting, so a list loaded before
 *  that toggle flipped would otherwise stay stale. */
export type RefreshToken = unknown

type Fetch<Row> = (provider: ProviderConfig, interaction: Interaction) => Promise<Row[]>

export class ModelCatalog<Row extends { id: string }> {
	#rows = new SvelteMap<string, Row[]>()
	/** Keys whose last attempt failed. Not cached as an empty list, so the next
	 *  `load` retries, but distinguishable from "never asked" for a message. */
	#failed = new SvelteSet<string>()
	#inflight = new SvelteMap<string, Promise<void>>()
	#fetch: Fetch<Row>

	/** `fetch` is the request behind the store; injected so the store can be
	 *  exercised without an api module or a mounted component. */
	constructor(fetch: Fetch<Row>) {
		this.#fetch = fetch
	}

	key(provider: ProviderConfig, interaction: Interaction, token: RefreshToken): string {
		return `${provider.id}:${interaction}:${String(token)}`
	}

	/** The rows this provider row offers, hidden models already dropped: a
	 *  hidden model is a connector nobody has verified, and listing it is
	 *  exactly what the flag exists to prevent. Empty until loaded. */
	list(provider: ProviderConfig, interaction: Interaction, token: RefreshToken): Row[] {
		const rows = this.#rows.get(this.key(provider, interaction, token)) ?? []
		return withoutHiddenRows(provider.kind, rows)
	}

	loading(provider: ProviderConfig, interaction: Interaction, token: RefreshToken): boolean {
		return this.#inflight.has(this.key(provider, interaction, token))
	}

	/** True once an answer has come back, even a failed one, so "no models"
	 *  can be told apart from "not asked yet". */
	settled(provider: ProviderConfig, interaction: Interaction, token: RefreshToken): boolean {
		const key = this.key(provider, interaction, token)
		return this.#rows.has(key) || this.#failed.has(key)
	}

	/** Fetch once per key and share the result; a second call while the first
	 *  is in flight joins it. Resolves either way: a failure is recorded, not
	 *  thrown, since every picker already treats it as an empty list. */
	load(provider: ProviderConfig, interaction: Interaction, token: RefreshToken): Promise<void> {
		return untrack(() => {
			const key = this.key(provider, interaction, token)
			if (this.#rows.has(key)) return Promise.resolve()
			const pending = this.#inflight.get(key)
			if (pending) return pending
			this.#failed.delete(key)
			const request = this.#fetch(provider, interaction)
				.then((rows) => {
					this.#rows.set(key, rows)
				})
				.catch(() => {
					this.#failed.add(key)
				})
				.finally(() => {
					this.#inflight.delete(key)
				})
			this.#inflight.set(key, request)
			return request
		})
	}
}

/** What the pickers list. Video models live on their own endpoint with their
 *  own metadata (durations, resolutions): the generate dialog consumes that
 *  shape through `videoCatalog`, while a picker only needs the ids. */
async function fetchModels(
	provider: ProviderConfig,
	interaction: Interaction,
): Promise<ModelInfo[]> {
	if (interaction === 'video') {
		// A video model described as a picker row. The transcription and LLM
		// fields are what the server would send for a model that has none of
		// them, spelled out rather than left off: the row has to be a whole
		// ModelInfo, and a picker reads these without checking which endpoint
		// the row came from.
		return (await api.videoModels(provider.id)).map((m) => ({
			id: m.id,
			context_length: null,
			realtime: null,
			inline_diarization: false,
			supports_reasoning: false,
			pricing: null,
			price_tiers: [],
		}))
	}
	return api.providerModels({
		kind: provider.kind,
		interaction,
		base_url: provider.base_url,
		provider_id: provider.id,
	})
}

export const modelCatalog = new ModelCatalog<ModelInfo>(fetchModels)

export const videoCatalog = new ModelCatalog<VideoModelInfo>((provider) =>
	api.videoModels(provider.id),
)

/** The catalogue entry for one picked model, or undefined until its list has
 *  loaded (or for a favourite the provider no longer serves). What a parent
 *  reads to offer model-dependent controls, such as reasoning effort. */
export function modelInfoFor(
	provider: ProviderConfig | undefined,
	interaction: Interaction,
	token: RefreshToken,
	id: string,
): ModelInfo | undefined {
	if (!provider || !id) return undefined
	return modelCatalog.list(provider, interaction, token).find((m) => m.id === id)
}
