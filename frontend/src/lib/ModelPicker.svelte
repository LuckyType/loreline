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
	disabled = false,
	id = undefined,
	interaction = 'transcribe',
	refreshToken = '',
	onpick = undefined,
}: {
	provider: ProviderConfig | undefined
	/** Seeding is the caller's job (see preferredModel): bind this to an
	 *  overridable derived and a pick overrides it until the provider changes. */
	value?: string
	/** The stored default for this picker's interaction, already attributed to
	 *  `provider` by the caller. Action defaults are stored as provider/model
	 *  pairs (stt_provider + stt_model and so on, see ActionDefaults), and the
	 *  model half is only meaningful while the provider half names this row,
	 *  so pass '' otherwise. Gets the "default" tag and leads the list. */
	defaultModel?: string
	disabled?: boolean
	id?: string
	/** Scopes the model list - a transcription picker must never offer a chat
	 *  or image model. See src/loreline/capabilities.py. */
	interaction?: Interaction
	/** Invalidates the cached list when it changes; see RefreshToken. */
	refreshToken?: RefreshToken
	onpick?: (model: string) => void
} = $props()

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

// Offered without consulting `fetched`, and that is the point: `fetched` is
// empty until the list is lazily loaded, so a default resting on it would
// appear, and move the selection, the moment the user opened the list.
const defaultForThisProvider = $derived(
	provider && defaultModel ? withoutHidden(kind, [defaultModel]) : [],
)

// A retiring model keeps its place in the list - a GM mid-campaign should not
// lose it - but says so, in the row and again under the trigger once picked.
const options = $derived(
	[...new Set([...defaultForThisProvider, ...favorites, ...fetched].filter(Boolean))].map((m) =>
		optionFor(kind, m, detail.get(m)),
	),
)

const selectedSunset = $derived(deprecationNote(kind, value))

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
		{onpick}
	/>
	{#if selectedSunset}
		<span class="text-xs text-amber-500">{selectedSunset}</span>
	{/if}
</div>
