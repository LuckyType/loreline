<script lang="ts">
import { onMount } from 'svelte'
import { api, ApiError } from '$lib/api'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Label } from '$lib/components/ui/label'
import { Textarea } from '$lib/components/ui/textarea'

let defaultText = $state('')
let defaultMsg = $state('')

async function saveDefault() {
	const terms = defaultText
		.split('\n')
		.map((t) => t.trim())
		.filter(Boolean)
	try {
		await api.putDefaultGlossary(terms)
		defaultMsg = 'Saved'
	} catch (err) {
		defaultMsg = err instanceof ApiError ? err.message : 'save failed'
	}
}

onMount(async () => {
	try {
		defaultText = (await api.getDefaultGlossary()).terms.join('\n')
	} catch {
		/* ignore */
	}
})
</script>

<Card>
	<CardHeader>
		<CardTitle>Glossary</CardTitle>
		<CardDescription>Default word list applied to every session.</CardDescription>
	</CardHeader>
	<CardContent>
		<div class="flex flex-col gap-2">
			<Label for="defwords">Terms</Label>
			<Textarea
				id="defwords"
				rows={12}
				bind:value={defaultText}
				onblur={saveDefault}
				placeholder="One spell / character / place name per line"
			/>
		</div>
		{#if defaultMsg}
			<span class="mt-2 text-sm text-muted-foreground">{defaultMsg}</span>
		{/if}
	</CardContent>
</Card>
