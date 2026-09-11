<script lang="ts">
/**
 * One glossary, edited in a box: a campaign's terms.
 *
 * A glossary belongs to a campaign, and the campaign page is the only page
 * that edits one. The other scope this component can read, the always-on
 * `_default` list, is still served by the API and still merged into every
 * session, and this is still the editor for it - there is just no page
 * pointing at it any more, because a list of names with no campaign attached
 * is a list nobody can say which table it is for.
 *
 * Saving is gated on a successful read. The box starts empty and saves on
 * blur, so one failed load (the app restarting for an update, say) followed by
 * a click into the box and out again would post an empty list over the stored
 * one and erase every term. Until a load succeeds the box is disabled and
 * nothing is posted; the failure shows above it with a retry.
 *
 * The clear confirm is the other half of the same worry, for the case where
 * the load worked: an empty box over a stored list is almost never meant, and
 * a select-all with a stray keystroke looks exactly like it.
 */

import { ApiError, api } from '$lib/api'
import { Label } from '$lib/components/ui/label'
import { Textarea } from '$lib/components/ui/textarea'
import { confirm } from '$lib/confirm.svelte'

let {
	campaignId = null,
	placeholder = 'One spell / character / place name per line',
	rows = 12,
	refreshToken = 0,
	onsaved,
}: {
	/** The campaign whose list this is. Null reads and writes the always-on
	 *  `_default` list instead, which no page currently asks for. */
	campaignId?: string | null
	placeholder?: string
	rows?: number
	/** Bumped by the page when something else wrote to this glossary - the
	 *  "Add to glossary" button on the campaign's extracted names. The box
	 *  would otherwise keep showing the list as it was before the append, and
	 *  the next blur would save that stale copy back over it. */
	refreshToken?: number
	/** The stored list changed, with what it now is. */
	onsaved?: (terms: string[]) => void
} = $props()

let text = $state('')
let message = $state('')
let loaded = $state(false)
let loadError = $state('')
let storedTerms = $state<string[]>([])

const uid = $props.id()
const fieldId = `${uid}-terms`

async function load() {
	loadError = ''
	try {
		const glossary = campaignId ? await api.getGlossary(campaignId) : await api.getDefaultGlossary()
		storedTerms = glossary.terms
		text = storedTerms.join('\n')
		loaded = true
	} catch (err) {
		loadError = `Could not load the glossary: ${
			err instanceof ApiError ? err.message : 'the request failed'
		}`
	}
}

// Reading a different glossary is a different list, so the box has to follow
// the id it is pointed at - and re-read when the page says somebody else wrote
// to it. An effect and not an onMount, because both of those can change while
// this component stays mounted.
$effect(() => {
	void campaignId
	void refreshToken
	void load()
})

async function save() {
	if (!loaded) return
	const terms = text
		.split('\n')
		.map((t) => t.trim())
		.filter(Boolean)
	if (terms.length === 0 && storedTerms.length > 0) {
		const count = storedTerms.length
		const cleared = await confirm({
			title: 'Clear the glossary?',
			description: `This removes all ${count} stored ${count === 1 ? 'term' : 'terms'} from ${
				campaignId ? 'this campaign' : 'every session'
			}.`,
			confirmLabel: 'Clear',
			destructive: true,
		})
		if (!cleared) {
			// Put the stored list back, so the box is not left empty to ask the
			// same question again on the next blur.
			text = storedTerms.join('\n')
			return
		}
	}
	try {
		const saved = campaignId
			? await api.putGlossary(campaignId, terms)
			: await api.putDefaultGlossary(terms)
		storedTerms = saved.terms
		message = 'Saved'
		setTimeout(() => (message = ''), 2500)
		onsaved?.(saved.terms)
	} catch (err) {
		message = err instanceof ApiError ? err.message : 'save failed'
	}
}
</script>

{#if loadError}
	<!-- An empty box reads as an empty list unless the failed load says so.
	     Same banner and retry as the providers page. -->
	<div
		class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
	>
		<span>{loadError}</span>
		<button class="underline underline-offset-2" onclick={load}>Retry</button>
	</div>
{/if}
<div class="flex flex-col gap-2">
	<Label for={fieldId}>Terms</Label>
	<Textarea id={fieldId} {rows} bind:value={text} onblur={save} disabled={!loaded} {placeholder} />
</div>
{#if message}
	<span class="mt-2 text-sm text-muted-foreground">{message}</span>
{/if}
