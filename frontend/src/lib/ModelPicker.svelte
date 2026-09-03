<script lang="ts">
import { api } from '$lib/api'
import { deprecationFor, deprecationNote, withoutHidden } from '$lib/capabilities.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import { hintFor, priceTitle } from '$lib/modelInfo'
import type { Interaction, ModelInfo, ProviderConfig } from '$lib/types'

let {
	provider,
	value = $bindable(''),
	defaultModel = '',
	defaultProvider = undefined,
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
	/** The provider the stored default belongs to. Action defaults are stored
	 *  as provider/model pairs (stt_provider + stt_model and so on, see
	 *  ActionDefaults), so passing the paired provider lets this picker offer
	 *  the default straight away instead of waiting for the list to confirm
	 *  it. Pass the pair matching this picker's `interaction`: that is what
	 *  keeps a transcribe picker from ever being handed a chat model. */
	defaultProvider?: string
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
// Seeding bookkeeping. Deliberately plain variables rather than $state:
// nothing outside the seeding effect reads them, and keeping them inert stops
// the effect from re-running on its own writes.
let seededFor = ''
let picked = false

// `all` is fetched per provider and must not leak across a provider switch:
// it's only valid while it matches the provider it was loaded for.
const cacheKey = $derived(`${provider?.id ?? ''}:${interaction}:${String(refreshToken)}`)
const loaded = $derived(provider && loadedFor === cacheKey ? all : [])
const kind = $derived(provider?.kind)
// A hidden model is one whose connector is written but unverified against the
// real API (see capabilities.yaml). It must never reach a picker - listing it
// is exactly what the flag exists to prevent - so every source of ids here is
// filtered, the live catalogue and the stored favourites alike.
const fetched = $derived(
	withoutHidden(
		kind,
		loaded.map((m) => m.id),
	),
)
// Price/context hints are only known for models the live list actually
// returned - a favourite or stored default that predates the fetch (or that
// the provider no longer serves) simply renders without one.
const detail = $derived(new Map(loaded.map((m) => [m.id, m])))
const favorites = $derived(withoutHidden(kind, provider?.favorite_models ?? []))

// Report the selected model's metadata upward once the list has resolved.
$effect(() => {
	onselect?.(detail.get(value))
})
// The stored default is global: it can name a model belonging to a different
// provider entirely, so it only earns a place here once it can be attributed
// to this one. A caller that passes `defaultProvider` has said so outright;
// otherwise the provider's own favourites are the only evidence available.
// Neither test consults `fetched`, and that is the point: `fetched` is empty
// until the list is lazily loaded, so a default resting on it would appear -
// and move the seeded selection - the moment the user opened the list.
const defaultForThisProvider = $derived(
	defaultModel &&
		(defaultProvider !== undefined
			? defaultProvider === provider?.id
			: favorites.includes(defaultModel))
		? withoutHidden(kind, [defaultModel])
		: [],
)

// A retiring model keeps its place in the list - a GM mid-campaign should not
// lose it - but says so, in the row and again under the trigger once picked.
const options = $derived(
	[
		...new Set(
			[...defaultForThisProvider, ...favorites, ...fetched].filter((m): m is string => !!m),
		),
	].map((m) => {
		const sunset = deprecationFor(kind, m)
		return {
			value: m,
			label: m,
			hint: [hintFor(detail.get(m)), sunset ? `retiring ${sunset}` : '']
				.filter(Boolean)
				.join(' · '),
			title: [priceTitle(detail.get(m)), deprecationNote(kind, m)].filter(Boolean).join(' '),
		}
	}),
)

const selectedSunset = $derived(deprecationNote(kind, value))

// Seeding an empty picker is a convenience; changing a selection the user made
// is a trap, so `picked` is recorded from the dropdown's own pick event rather
// than inferred from the value. Inferring it missed the user who picked
// exactly what was seeded, and could not tell a deliberate choice from a
// seed that a later-arriving input wanted to revise.
$effect(() => {
	if (!autoseed) return
	const pid = provider?.id ?? ''
	if (!pid) return
	// Same rule as `options`: never seed a model the selected provider can't
	// serve. The action default first, then the provider's favourites, and
	// nothing after: the row used to carry a `model` that was consulted last,
	// which made a single stored value shadow the per-action default for every
	// interaction that provider serves.
	const prefs = withoutHidden(kind, [...defaultForThisProvider, ...favorites]).filter(Boolean)
	const want = prefs[0] ?? ''
	// A provider switch starts over: the previous provider's pick cannot be
	// served here, so it is dropped along with the seed it replaced.
	if (pid !== seededFor) {
		seededFor = pid
		picked = false
	}
	if (picked) return
	if (value !== want) value = want
})

function pick(model: string) {
	picked = true
	onpick?.(model)
}

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

<div class="flex flex-col gap-1">
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
		onpick={pick}
	/>
	{#if selectedSunset}
		<span class="text-xs text-amber-500">{selectedSunset}</span>
	{/if}
</div>
