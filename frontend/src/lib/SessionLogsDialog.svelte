<script lang="ts">
/**
 * The log lines one transcript version was produced by.
 *
 * Fetched on demand rather than with the version list: a whole session's log
 * is far larger than the row it belongs to, and nobody reads it until
 * something about that version looks wrong.
 */

import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import LogLine from '$lib/LogLine.svelte'

let {
	open = $bindable(false),
	sessionId,
	version,
}: {
	open?: boolean
	sessionId: string
	/** Which version's log to show: 'original', or a job id. */
	version: string
} = $props()

let lines = $state<string[]>([])
let error = $state('')

// Opening is what fetches, and a version changing under an open dialog
// refetches: the row that asked is the only thing that knows which log is
// wanted, and it says so by setting both at once.
$effect(() => {
	if (open) void load(version)
})

async function load(wanted: string) {
	lines = []
	error = ''
	try {
		const stored = await api.getVersionLogs(sessionId, wanted)
		lines = stored.logs.split('\n').filter((line) => line !== '')
	} catch (err) {
		error =
			err instanceof ApiError && err.status === 404
				? 'No logs were stored for this version.'
				: err instanceof ApiError
					? err.message
					: 'failed to load logs'
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-3xl">
		<DialogHeader>
			<DialogTitle>
				Logs · {version === 'original' ? 'original' : version.slice(0, 8)}
			</DialogTitle>
			<DialogDescription>
				What this version was produced by, kept per version: the live view on the Dashboard only
				holds the last few hundred lines of the running capture.
			</DialogDescription>
		</DialogHeader>
		{#if error}
			<p class="m-0 text-sm text-muted-foreground">{error}</p>
		{:else}
			<div
				class="max-h-[60vh] overflow-auto rounded-md bg-accent/40 px-3 py-2 font-mono text-xs leading-relaxed"
			>
				{#each lines as line, i (i)}
					<LogLine {line} wrap={false} />
				{/each}
				{#if lines.length === 0}
					<span class="text-muted-foreground">Loading…</span>
				{/if}
			</div>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Close</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
