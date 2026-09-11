<script lang="ts">
/**
 * Every campaign, and how much is in it.
 *
 * The two numbers are the whole reason this is not just a list of names:
 * "which of these am I actually playing" is answered by how many sessions a
 * campaign has and when the last one was, and neither is on the campaign
 * itself.
 */

import { onMount } from 'svelte'
import { goto } from '$app/navigation'
import { ApiError } from '$lib/api'
import { campaigns } from '$lib/campaigns.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'

let creating = $state(false)
let name = $state('')
let busy = $state(false)
let error = $state('')

function when(ts: number | null): string {
	return ts ? new Date(ts * 1000).toLocaleDateString() : '-'
}

async function create() {
	const trimmed = name.trim()
	if (!trimmed) return
	busy = true
	error = ''
	try {
		const campaign = await campaigns.create(trimmed)
		name = ''
		creating = false
		// Straight into it: a campaign is made in order to put something in it,
		// and the list it was made from has nothing more to say about it.
		goto(`/campaigns/${campaign.id}`)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not create the campaign'
	} finally {
		busy = false
	}
}

onMount(() => {
	void campaigns.reload()
})
</script>

{#if error || campaigns.error}
	<p class="mb-4 text-sm text-destructive">{error || campaigns.error}</p>
{/if}

<Card>
	<CardContent>
		<div class="mb-2 flex flex-wrap items-center justify-between gap-2">
			<span class="text-muted-foreground">
				A campaign collects its sessions, its glossary and its recaps.
			</span>
			{#if creating}
				<div class="flex gap-2">
					<Input
						bind:value={name}
						placeholder="Campaign name"
						aria-label="Campaign name"
						onkeydown={(e: KeyboardEvent) => {
							if (e.key === 'Enter') create()
							if (e.key === 'Escape') creating = false
						}}
					/>
					<Button onclick={create} disabled={busy || !name.trim()}>
						{busy ? 'Creating…' : 'Create'}
					</Button>
					<Button variant="ghost" onclick={() => (creating = false)}>Cancel</Button>
				</div>
			{:else}
				<Button onclick={() => (creating = true)}>New campaign</Button>
			{/if}
		</div>
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead>Name</TableHead>
					<TableHead>Sessions</TableHead>
					<TableHead>Last session</TableHead>
					<TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each campaigns.rows as row (row.campaign.id)}
					<TableRow>
						<TableCell>
							<a class="text-primary hover:underline" href="/campaigns/{row.campaign.id}">
								{row.campaign.name}
							</a>
						</TableCell>
						<TableCell class="text-muted-foreground">{row.sessions}</TableCell>
						<TableCell class="text-muted-foreground">{when(row.last_session_at)}</TableCell>
						<TableCell class="text-muted-foreground">{row.campaign.notes}</TableCell>
					</TableRow>
				{/each}
				{#if campaigns.ready && campaigns.rows.length === 0}
					<TableRow>
						<TableCell colspan={4} class="text-muted-foreground">
							No campaigns yet. Make one, then start a session in it from the Dashboard.
						</TableCell>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</CardContent>
</Card>
