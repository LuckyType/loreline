<script lang="ts">
import { api } from '$lib/api'
import Dropdown from '$lib/Dropdown.svelte'
import { hintFor, priceTitle } from '$lib/modelInfo'
import type { ModelInfo, ProviderConfig } from '$lib/types'

let {
	provider,
	value = $bindable(''),
	defaultModel = '',
	disabled = false,
	id = undefined,
	autoseed = true,
	onpick = undefined,
}: {
	provider: ProviderConfig | undefined
	value?: string
	defaultModel?: string
	disabled?: boolean
	id?: string
	autoseed?: boolean
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
const loaded = $derived(provider && loadedFor === provider.id ? all : [])
const fetched = $derived(loaded.map((m) => m.id))
// Price/context hints are only known for models the live list actually
// returned - a favourite or stored default that predates the fetch (or that
// the provider no longer serves) simply renders without one.
const detail = $derived(new Map(loaded.map((m) => [m.id, m])))
const favorites = $derived(provider?.favorite_models ?? [])
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
	if (!provider || loading || loadedFor === provider.id) return
	loading = true
	try {
		all = await api.providerModels({
			kind: provider.kind,
			base_url: provider.base_url,
			provider_id: provider.id,
		})
		loadedFor = provider.id
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
