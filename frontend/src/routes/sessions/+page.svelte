<script lang="ts">
import { onMount } from 'svelte'
import { goto } from '$app/navigation'
import { api, ApiError } from '$lib/api'
import { confirm } from '$lib/confirm.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
import { Checkbox } from '$lib/components/ui/checkbox'
import { Badge } from '$lib/components/ui/badge'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import type { Session } from '$lib/types'

let sessions = $state<Session[]>([])
let selected = $state<Record<string, boolean>>({})
let error = $state('')
let busy = $state(false)

const selectedIds = $derived(sessions.filter((s) => selected[s.id]).map((s) => s.id))
const allChecked = $derived(sessions.length > 0 && selectedIds.length === sessions.length)

function when(ts: number): string {
	return new Date(ts * 1000).toLocaleString()
}

async function reload() {
	try {
		sessions = await api.listSessions()
		// Every row's Checkbox binds to selected[s.id] directly; leaving a key
		// absent makes that undefined rather than false, which bits-ui's
		// stricter prop validation rejects outright (throws props_invalid_value
		// and the whole table fails to render). Pre-populate real booleans.
		selected = Object.fromEntries(sessions.map((s) => [s.id, false]))
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load sessions'
	}
}

function toggleAll() {
	const next = !allChecked
	selected = Object.fromEntries(sessions.map((s) => [s.id, next]))
}

async function deleteSelected() {
	if (selectedIds.length === 0) return
	const ok = await confirm({
		description: `Delete ${selectedIds.length} session(s)? This also removes their audio.`,
		destructive: true,
	})
	if (!ok) return
	busy = true
	error = ''
	try {
		await api.deleteSessions(selectedIds)
		await reload()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'delete failed'
	} finally {
		busy = false
	}
}

async function mergeSelected() {
	if (selectedIds.length < 2) return
	busy = true
	error = ''
	try {
		const merged = await api.mergeSessions(selectedIds)
		goto(`/sessions/${merged.id}`)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'merge failed'
		busy = false
	}
}

onMount(reload)
</script>

<h1>History</h1>
{#if error}
	<p class="mt-2 text-sm text-destructive">{error}</p>
{/if}

<Card class="mt-4">
	<CardContent>
		<div class="mb-2 flex items-center justify-between">
			<span class="text-muted-foreground">{selectedIds.length} selected</span>
			<div class="flex gap-2">
				<Button variant="outline" onclick={mergeSelected} disabled={busy || selectedIds.length < 2}>
					Merge selected
				</Button>
				<Button
					variant="destructive"
					onclick={deleteSelected}
					disabled={busy || selectedIds.length === 0}
				>
					Delete selected
				</Button>
			</div>
		</div>
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead class="w-10">
						<Checkbox checked={allChecked} onCheckedChange={toggleAll} aria-label="Select all" />
					</TableHead>
					<TableHead>Started</TableHead>
					<TableHead>Status</TableHead>
					<TableHead>Primary</TableHead>
					<TableHead>Campaign</TableHead>
					<TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each sessions as s (s.id)}
					<TableRow>
						<TableCell>
							<Checkbox bind:checked={selected[s.id]} aria-label="Select session" />
						</TableCell>
						<TableCell>{when(s.started_at)}</TableCell>
						<TableCell>
							<Badge
								variant={s.status === 'error'
                  ? 'destructive'
                  : s.status === 'completed'
                    ? 'secondary'
                    : 'outline'}
							>
								{s.status}
							</Badge>
						</TableCell>
						<TableCell class="text-muted-foreground">{s.primary_provider ?? '-'}</TableCell>
						<TableCell class="text-muted-foreground">{s.campaign_id ?? '-'}</TableCell>
						<TableCell
							><a class="text-primary hover:underline" href="/sessions/{s.id}">Open</a></TableCell
						>
					</TableRow>
				{/each}
				{#if sessions.length === 0}
					<TableRow>
						<TableCell colspan={6} class="text-muted-foreground">No sessions recorded.</TableCell>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</CardContent>
</Card>
