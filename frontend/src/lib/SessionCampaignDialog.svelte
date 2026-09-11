<script lang="ts">
/**
 * Move a session into a campaign, or out of one.
 *
 * A dialog rather than an inline picker in the header band, because this is
 * rare and consequential: it decides which glossary the session's
 * re-transcriptions get, which campaign's recaps it joins and where its
 * extracted names land. The picker offers the campaigns and nothing else - a
 * campaign is made on the Dashboard as a session starts, or on the Campaigns
 * page, both of which are one click away and neither of which is here.
 */

import { ApiError, api } from '$lib/api'
import { campaigns } from '$lib/campaigns.svelte'
import { Button } from '$lib/components/ui/button'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import { Label } from '$lib/components/ui/label'
import Dropdown from '$lib/Dropdown.svelte'

let {
	open = $bindable(false),
	sessionId,
	campaignId,
	onchanged,
}: {
	open?: boolean
	sessionId: string
	/** The campaign the session is in now, or null. */
	campaignId: string | null
	/** Awaited, so the header behind the dialog already says the new name when
	 *  it closes. */
	onchanged?: () => Promise<void> | void
} = $props()

// Seeded from the session and overridden by a pick, the same rule every other
// picker follows: reopening the dialog after cancelling shows where the
// session actually is, not where it was nearly moved to.
let picked = $derived(campaignId ?? '')
let busy = $state(false)
let error = $state('')

const uid = $props.id()

$effect(() => {
	if (open) error = ''
})

async function save() {
	busy = true
	error = ''
	try {
		await api.setSessionCampaign(sessionId, picked || null)
		await onchanged?.()
		open = false
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not change the campaign'
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Campaign</DialogTitle>
			<DialogDescription>
				Which campaign this session belongs to. Its glossary biases re-transcriptions, and its
				recaps and extracted names collect on the campaign's page.
			</DialogDescription>
		</DialogHeader>
		<div class="flex flex-col gap-2">
			<Label for="{uid}-campaign">Campaign</Label>
			<Dropdown
				id="{uid}-campaign"
				bind:value={picked}
				options={campaigns.options()}
				placeholder={campaigns.ready ? 'No campaign' : 'Loading campaigns…'}
				loading={!campaigns.ready}
			/>
		</div>
		{#if error}
			<p class="mt-2 text-sm text-destructive">{error}</p>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<Button onclick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
