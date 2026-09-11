<script lang="ts">
/**
 * Every session recorded, newest work first.
 *
 * A merged session copies its oldest source's start, status, campaign and
 * primary provider, so on Started/Status/Primary/Campaign alone the merge and
 * the part it was built from are the same row twice and only opening both
 * tells them apart. Two things separate them here. `merged_from` names the
 * parts, so the merge says outright what it is. Duration says which row is the
 * long one, which is the question this table gets asked most and is worth a
 * column whether or not anything was ever merged.
 */

import { onMount } from 'svelte'
import { goto } from '$app/navigation'
import { actionSetup } from '$lib/actionSetup.svelte'
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
import ImportRecordingDialog from '$lib/ImportRecordingDialog.svelte'
import { fmtDuration, providerName } from '$lib/stores'
import type { ImportedSession, Session } from '$lib/wire'

let sessions = $state<Session[]>([])
let selected = $state<Record<string, boolean>>({})
let error = $state('')
let busy = $state(false)
let importOpen = $state(false)

const selectedIds = $derived(sessions.filter((s) => selected[s.id]).map((s) => s.id))
const allChecked = $derived(sessions.length > 0 && selectedIds.length === sessions.length)

function when(ts: number): string {
	return new Date(ts * 1000).toLocaleString()
}

/** "a" for one name, "a and b" for two, "a, b, and c" for more. */
function joinNames(names: string[]): string {
	if (names.length === 1) return `${names[0]}`
	if (names.length === 2) return `${names[0]} and ${names[1]}`
	return `${names.slice(0, -1).join(', ')}, and ${names[names.length - 1]}`
}

/** "1 video", "3 videos". */
function plural(n: number, one: string, many: string): string {
	return `${n} ${n === 1 ? one : many}`
}

/** What deleting these sessions takes with them, as the sentence the confirm says.
 *
 * The number of sessions alone undersold it. The audio was named and the rest
 * was not, and the rest is what cost something: a summary is a model run the
 * GM paid for, a video is minutes of somebody's GPU, and neither shows in this
 * table. Summaries are on the rows already; videos are counted from their own
 * rows because a session row does not carry the number. */
async function deleteDescription(ids: string[]): Promise<string> {
	const chosen = sessions.filter((s) => ids.includes(s.id))
	const summaries = chosen.filter((s) => s.summary).length
	const videos = (await Promise.all(ids.map((id) => api.listVideoJobs(id)))).reduce(
		(n, jobs) => n + jobs.length,
		0,
	)
	const one = ids.length === 1
	const goes = [one ? 'its audio' : 'their audio']
	if (summaries) goes.push(one ? 'its summary' : plural(summaries, 'summary', 'summaries'))
	if (videos) goes.push(plural(videos, 'video', 'videos'))
	return `Delete ${plural(ids.length, 'session', 'sessions')}? This also removes ${joinNames(goes)}.`
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
	// Only used to render provider ids as names, so its failure is not the
	// list's: the store records it, and providerName() already falls back to
	// a short id.
	void actionSetup.load()
}

function toggleAll() {
	const next = !allChecked
	selected = Object.fromEntries(sessions.map((s) => [s.id, next]))
}

async function deleteSelected() {
	if (selectedIds.length === 0) return
	busy = true
	error = ''
	try {
		// Counting what goes is a round trip, so it sits inside the same
		// try: a backend that cannot list videos cannot delete sessions either,
		// and the error line is where that belongs.
		const ok = await confirm({
			description: await deleteDescription(selectedIds),
			destructive: true,
		})
		if (!ok) return
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
	// Oldest to newest, matching merge_sessions' own sort - so the order named
	// here is the order the parts actually land in.
	const parts = [...sessions]
		.filter((s) => selected[s.id])
		.sort((a, b) => a.started_at - b.started_at)
	const ok = await confirm({
		description: `Merge ${joinNames(parts.map((s) => `session ${when(s.started_at)}`))} into a new session? The originals will be kept.`,
	})
	if (!ok) return
	busy = true
	error = ''
	try {
		const merged = await api.mergeSessions(parts.map((s) => s.id))
		goto(`/sessions/${merged.id}`)
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'merge failed'
		busy = false
	}
}

/** Straight to the new session: an import is done to look at it, and when the
 *  request also started a transcription there is already something filling up
 *  there to watch. */
function openImported(result: ImportedSession) {
	goto(`/sessions/${result.session.id}`)
}

onMount(reload)
</script>

<!-- No page heading: the left nav names this page and highlights it while you
     are on it, and it only ever hides because someone collapsed it deliberately.
     The gap the heading used to open up now hangs off the error instead of the
     card, so the table starts at the page padding whether or not there is an
     error to show. -->
{#if error}
	<p class="mb-4 text-sm text-destructive">{error}</p>
{/if}

<Card>
	<CardContent>
		<div class="mb-2 flex items-center justify-between">
			<span class="text-muted-foreground">{selectedIds.length} selected</span>
			<div class="flex gap-2">
				<!-- First in the group and not disabled by the selection, because it
				     is the one action here that makes a session rather than acting on
				     the ones already listed. -->
				<Button
					variant="outline"
					onclick={() => (importOpen = true)}
					title="Store a recording you made elsewhere as a session"
				>
					Import recording
				</Button>
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
					<TableHead>Duration</TableHead>
					<TableHead>Status</TableHead>
					<TableHead>Primary</TableHead>
					<TableHead>Campaign</TableHead>
					<TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each sessions as s (s.id)}
					<TableRow title={s.import_name ? `Imported from ${s.import_name}` : undefined}>
						<TableCell>
							<Checkbox bind:checked={selected[s.id]} aria-label="Select session" />
						</TableCell>
						<TableCell>
							<span class="flex flex-wrap items-center gap-2">
								{when(s.started_at)}
								{#if s.merged_from.length}
									<Badge
										variant="outline"
										title="Assembled from {s.merged_from.length} sessions, which are still here in their own right."
									>
										merged
									</Badge>
								{/if}
								<!-- An import has no live transcript and no capture provider, so
								     without this it reads as a session whose recording failed. -->
								{#if s.origin === 'import'}
									<Badge variant="outline" title="Recorded elsewhere and uploaded: {s.import_name}">
										imported
									</Badge>
								{/if}
							</span>
						</TableCell>
						<!-- A dash, not a blank: "nothing to say" has to look deliberate
						     next to the rows that do say something. -->
						<TableCell class="text-muted-foreground">
							{fmtDuration(s.started_at, s.ended_at) || '-'}
						</TableCell>
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
						<TableCell class="text-muted-foreground"
							>{providerName(s.primary_provider, actionSetup.providers)}</TableCell
						>
						<TableCell class="text-muted-foreground">{s.campaign_id ?? '-'}</TableCell>
						<TableCell
							><a class="text-primary hover:underline" href="/sessions/{s.id}">Open</a></TableCell
						>
					</TableRow>
				{/each}
				{#if sessions.length === 0}
					<TableRow>
						<TableCell colspan={7} class="text-muted-foreground">No sessions recorded.</TableCell>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</CardContent>
</Card>

<ImportRecordingDialog bind:open={importOpen} onimported={openImported} />
