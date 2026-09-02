<script lang="ts">
import { api } from '$lib/api'
import Dropdown from '$lib/Dropdown.svelte'
import { hintFor, priceTitle } from '$lib/modelInfo'
import type { Interaction, ModelInfo, ProviderConfig } from '$lib/types'

let {
	provider,
	value = $bindable(''),
	defaultModel = '',
	disabled = false,
	id = undefined,
	autoseed = true,
	interaction = 'transcribe',
	refreshToken = '',
	onselect = undefined,
	onpick = undefined,
}: {
	provider: ProviderConfig | undefined
	value?: string
	defaultModel?: string
	disabled?: boolean
	id?: string
	autoseed?: boolean
	/** Scopes the model list - a transcription picker must never offer a chat
	 *  or image model. See src/loreline/capabilities.py. */
	interaction?: Interaction
	/** Any value that should invalidate the cached list when it changes - the
	 *  server applies the "only show compatible models" setting, so a picker
	 *  loaded before that toggle flipped would otherwise stay stale. */
	refreshToken?: unknown
	/** Fires with the selected model's full metadata whenever it resolves -
	 *  lets a caller offer model-dependent controls (e.g. reasoning effort)
	 *  without fetching the list a second time. */
	onselect?: (model: ModelInfo | undefined) => void
	onpick?: (model: string) => void
} = $props()

let all = $state<ModelInfo[]>([])
let loadedFor = $state('')
let loading = $state(false)
let seededFor = $state('')
let seededValue = $state('')

// `all` is fetched per provider and must not leak across a provider switch:
// it's only valid while it matches the provider it was loaded for. The
// stored default (defaultModel) is global - it can name a model belonging to
// a different provider entirely - so only offer it when this provider
// actually has it.
const cacheKey = $derived(`${provider?.id ?? ''}:${interaction}:${String(refreshToken)}`)
const loaded = $derived(provider && loadedFor === cacheKey ? all : [])
const fetched = $derived(loaded.map((m) => m.id))
// Price/context hints are only known for models the live list actually
// returned - a favourite or stored default that predates the fetch (or that
// the provider no longer serves) simply renders without one.
const detail = $derived(new Map(loaded.map((m) => [m.id, m])))
const favorites = $derived(provider?.favorite_models ?? [])

// Report the selected model's metadata upward once the list has resolved.
$effect(() => {
	onselect?.(detail.get(value))
})
const defaultForThisProvider = $derived(
	defaultModel && (favorites.includes(defaultModel) || fetched.includes(defaultModel))
		? [defaultModel]
		: [],
)

const options = $derived(
	[
		...new Set(
			[...defaultForThisProvider, ...favorites, ...fetched].filter((m): m is string => !!m),
		),
	].map((m) => ({
		value: m,
		label: m,
		hint: hintFor(detail.get(m)),
		title: priceTitle(detail.get(m)),
	})),
)

$effect(() => {
	if (!autoseed) return
	const pid = provider?.id ?? ''
	if (!pid) return
	// Same rule as `options`: never seed a model the selected provider can't
	// serve. Falls through to its favourites, then its own stored model.
	const prefs = [...defaultForThisProvider, ...favorites, provider?.model ?? ''].filter(Boolean)
	const want = prefs[0] ?? ''
	const userOverrode = !!value && value !== seededValue
	if (pid !== seededFor || !userOverrode) {
		seededFor = pid
		seededValue = want
		if (value !== want) value = want
	}
})

async function loadAll() {
	const key = cacheKey
	if (!provider || loading || loadedFor === key) return
	loading = true
	try {
		// Video models live on their own endpoint with their own capability
		// metadata (durations, resolutions); the generate dialog consumes that
		// shape directly, while this picker only needs the ids.
		all =
			interaction === 'video'
				? (await api.videoModels(provider.id)).map((m) => ({
						id: m.id,
						context_length: null,
						pricing: null,
						price_tiers: [],
					}))
				: await api.providerModels({
						kind: provider.kind,
						interaction,
						base_url: provider.base_url,
						provider_id: provider.id,
					})
		loadedFor = key
	} catch {
		all = []
	} finally {
		loading = false
	}
}
</script>

<Dropdown
	{id}
	{disabled}
	{loading}
	bind:value
	{options}
	{favorites}
	defaultValue={defaultForThisProvider[0] ?? ''}
	filterable
	placeholder="Select model…"
	onopen={loadAll}
	{onpick}
/>
