<script lang="ts">
import { onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
import { Label } from '$lib/components/ui/label'
import { Textarea } from '$lib/components/ui/textarea'
import { confirm } from '$lib/confirm.svelte'

let defaultText = $state('')
let defaultMsg = $state('')
/**
 * Whether the stored list has been read, and what it was.
 *
 * Saving is gated on the read. The textarea starts empty and saves on blur,
 * so one failed load (the app restarting for an update, say) followed by a
 * click into the box and out again used to post an empty list over the
 * stored one and erase every term. Until a load succeeds the box is disabled
 * and nothing is posted; the failure shows above it with a retry.
 */
let loaded = $state(false)
let loadError = $state('')
let storedTerms = $state<string[]>([])

async function load() {
	loadError = ''
	try {
		storedTerms = (await api.getDefaultGlossary()).terms
		defaultText = storedTerms.join('\n')
		loaded = true
	} catch (err) {
		loadError = `Could not load the glossary: ${
			err instanceof ApiError ? err.message : 'the request failed'
		}`
	}
}

async function saveDefault() {
	if (!loaded) return
	const terms = defaultText
		.split('\n')
		.map((t) => t.trim())
		.filter(Boolean)
	// An empty box over a stored list is almost never meant: a select-all
	// and a stray keystroke look exactly like it. Ask, and on no put the stored
	// list back, so the box is not left empty to ask again on the next blur.
	if (terms.length === 0 && storedTerms.length > 0) {
		const count = storedTerms.length
		const cleared = await confirm({
			title: 'Clear the glossary?',
			description: `This removes all ${count} stored ${count === 1 ? 'term' : 'terms'} from every session.`,
			confirmLabel: 'Clear',
			destructive: true,
		})
		if (!cleared) {
			defaultText = storedTerms.join('\n')
			return
		}
	}
	try {
		storedTerms = (await api.putDefaultGlossary(terms)).terms
		defaultMsg = 'Saved'
		setTimeout(() => (defaultMsg = ''), 2500)
	} catch (err) {
		defaultMsg = err instanceof ApiError ? err.message : 'save failed'
	}
}

onMount(load)
</script>

<Card>
	<CardHeader>
		<CardTitle>Glossary</CardTitle>
		<CardDescription>Default word list applied to every session.</CardDescription>
	</CardHeader>
	<CardContent>
		{#if loadError}
			<!-- An empty box reads as an empty list unless the failed load says
			     so. Same banner and retry as the providers page. -->
			<div
				class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
			>
				<span>{loadError}</span>
				<button class="underline underline-offset-2" onclick={load}>Retry</button>
			</div>
		{/if}
		<div class="flex flex-col gap-2">
			<Label for="defwords">Terms</Label>
			<Textarea
				id="defwords"
				rows={12}
				bind:value={defaultText}
				onblur={saveDefault}
				disabled={!loaded}
				placeholder="One spell / character / place name per line"
			/>
		</div>
		{#if defaultMsg}
			<span class="mt-2 text-sm text-muted-foreground">{defaultMsg}</span>
		{/if}
	</CardContent>
</Card>
