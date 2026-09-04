<script lang="ts">
import { deprecationNote, withoutHidden } from '$lib/capabilities.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import { modelCatalog, type RefreshToken } from '$lib/modelCatalog.svelte'
import { optionFor } from '$lib/modelInfo'
import type { Interaction, ProviderConfig } from '$lib/types'

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
	/** Invalidates the cached list when it changes; see RefreshToken. */
	refreshToken?: RefreshToken
	onpick?: (model: string) => void
} = $props()

// Seeding bookkeeping. Deliberately plain variables rather than $state:
// nothing outside the seeding effect reads them, and keeping them inert stops
// the effect from re-running on its own writes.
let seededFor = ''
let picked = false

const kind = $derived(provider?.kind)
// This picker is a view over the shared catalogue, which answers per provider
// row, interaction and refresh token: a previous provider's list never shows
// under the next one, and a picker that unmounts forgets nothing. Hidden
// models are already dropped there; the favourites are filtered here.
const loaded = $derived(provider ? modelCatalog.list(provider, interaction, refreshToken) : [])
const loading = $derived(
	provider ? modelCatalog.loading(provider, interaction, refreshToken) : false,
)
const fetched = $derived(loaded.map((m) => m.id))
// Price/context hints are only known for models the live list actually
// returned - a favourite or stored default that predates the fetch (or that
// the provider no longer serves) simply renders without one.
const detail = $derived(new Map(loaded.map((m) => [m.id, m])))
const favorites = $derived(withoutHidden(kind, provider?.favorite_models ?? []))

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
	[...new Set([...defaultForThisProvider, ...favorites, ...fetched].filter(Boolean))].map((m) =>
		optionFor(kind, m, detail.get(m)),
	),
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

// The list is loaded lazily, on first open: some catalogues (OpenRouter's) are
// large, and most pickers are never opened.
function load() {
	if (provider) modelCatalog.load(provider, interaction, refreshToken)
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
		onopen={load}
		onpick={pick}
	/>
	{#if selectedSunset}
		<span class="text-xs text-amber-500">{selectedSunset}</span>
	{/if}
</div>
