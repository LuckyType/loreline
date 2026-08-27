<script lang="ts">
import { api } from '$lib/api'
import Dropdown from '$lib/Dropdown.svelte'
import type { ProviderConfig } from '$lib/types'

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

let all = $state<string[]>([])
let loadedFor = $state('')
let loading = $state(false)
let seededFor = $state('')
let seededValue = $state('')

const options = $derived(
	[
		...new Set(
			[defaultModel, ...(provider?.favorite_models ?? []), ...all].filter((m): m is string => !!m),
		),
	].map((m) => ({ value: m, label: m })),
)

$effect(() => {
	if (!autoseed) return
	const pid = provider?.id ?? ''
	if (!pid) return
	const prefs = [defaultModel, ...(provider?.favorite_models ?? []), provider?.model ?? ''].filter(
		Boolean,
	)
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
	favorites={provider?.favorite_models ?? []}
	defaultValue={defaultModel}
	filterable
	placeholder="Select model…"
	onopen={loadAll}
	{onpick}
/>
