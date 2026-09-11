<script lang="ts">
/**
 * The names a session (or a whole campaign) turned out to contain, grouped,
 * with the ones worth biasing recognition towards tickable.
 *
 * One component for both because they are one list at two scopes: a session's
 * extraction, and every session's extraction merged by name. The campaign's
 * entries carry the sessions they came up in and the session's do not, which
 * is the only difference the reader sees.
 *
 * The checkboxes exist for one destination: the campaign glossary. A name the
 * model heard well enough to extract is exactly the name the *next* session's
 * provider should be told about, and typing forty of them back in by hand is
 * how a glossary stays empty. Nothing is ticked by default - the list holds
 * every passing innkeeper, and a glossary spent on those is a glossary that
 * has no room for the ones that matter.
 */

import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Checkbox } from '$lib/components/ui/checkbox'
import type { MergedEntity } from '$lib/wire'

let {
	groups,
	campaignId = null,
	onadded,
}: {
	/** The groups to show, in the order they should read. A group with no
	 *  entries is dropped rather than printed empty: "Factions: none" is a
	 *  line about the prompt, not about the session. */
	groups: { label: string; entries: MergedEntity[] }[]
	/** Where "Add to glossary" would write. Null (a session in no campaign)
	 *  disables the button and says why: a glossary belongs to a campaign. */
	campaignId?: string | null
	/** Terms were appended; the caller re-reads the glossary it is showing. */
	onadded?: (terms: string[]) => void
} = $props()

let picked = $state<Record<string, boolean>>({})
let busy = $state(false)
let error = $state('')
let message = $state('')

const shown = $derived(groups.filter((group) => group.entries.length > 0))
// Keyed by name rather than by group and index: the same name in two groups is
// one term in a glossary, and a list that shifts under a re-fetch must not
// move somebody's ticks onto different names.
const pickedNames = $derived(Object.keys(picked).filter((name) => picked[name]))

async function addToGlossary() {
	if (!campaignId || pickedNames.length === 0) return
	busy = true
	error = ''
	message = ''
	try {
		const glossary = await api.addToCampaignGlossary(campaignId, pickedNames)
		message = `Glossary now holds ${glossary.terms.length} ${
			glossary.terms.length === 1 ? 'term' : 'terms'
		}.`
		picked = {}
		onadded?.(glossary.terms)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not add the terms'
	} finally {
		busy = false
	}
}
</script>

{#if shown.length === 0}
	<p class="m-0 text-muted-foreground">Nothing extracted yet.</p>
{:else}
	<div class="flex flex-col gap-4">
		{#each shown as group (group.label)}
			<div class="flex flex-col gap-1">
				<h4 class="text-xs font-medium tracking-widest text-muted-foreground uppercase">
					{group.label}
				</h4>
				{#each group.entries as entry (entry.name)}
					<label class="flex items-start gap-2 text-sm">
						<Checkbox
							checked={picked[entry.name] ?? false}
							onCheckedChange={(v) => (picked = { ...picked, [entry.name]: v === true })}
							aria-label="Add {entry.name} to the glossary"
						/>
						<span class="min-w-0">
							<span class="font-medium">{entry.name}</span>
							{#if entry.kind}
								<span class="ml-1 rounded-full border px-1.5 text-xs text-muted-foreground">
									{entry.kind}
								</span>
							{/if}
							{#if entry.notes}
								<span class="ml-1 text-muted-foreground">{entry.notes}</span>
							{/if}
							{#if entry.session_ids.length > 1}
								<!-- Only worth saying when it is more than one: on a single
								     session's list every entry came from that session. -->
								<span class="ml-1 text-xs text-muted-foreground">
									({entry.session_ids.length}
									sessions)
								</span>
							{/if}
						</span>
					</label>
				{/each}
			</div>
		{/each}
	</div>
	<div class="mt-3 flex flex-wrap items-center gap-2 border-t border-dashed pt-3">
		<Button
			size="sm"
			variant="outline"
			onclick={addToGlossary}
			disabled={busy || !campaignId || pickedNames.length === 0}
			title={campaignId
				? 'Send the ticked names to this campaign’s glossary.'
				: 'A glossary belongs to a campaign - put this session in one first.'}
		>
			{busy ? 'Adding…' : `Add ${pickedNames.length} to glossary`}
		</Button>
		{#if error}
			<span class="text-sm text-destructive">{error}</span>
		{:else if message}
			<span class="text-sm text-muted-foreground">{message}</span>
		{/if}
	</div>
{/if}
