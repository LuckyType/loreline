<script lang="ts">
/**
 * The log lines one transcript version was produced by.
 *
 * Fetched on demand rather than with the version list: a whole session's log
 * is far larger than the row it belongs to, and nobody reads it until
 * something about that version looks wrong.
 *
 * One instance serves the whole table, so where focus goes on close is not
 * something this dialog can work out for itself - see `trigger`.
 *
 * "Loading…" is keyed on the fetch being in flight, not on the list being
 * empty. The stored log is split into lines and the blank ones dropped, so a
 * log that exists and holds nothing readable used to leave the list empty and
 * the placeholder saying "Loading…" for as long as the dialog stayed open; a
 * fetch that has come back with nothing to show now says that instead.
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
import { versionLabel } from '$lib/stores'

let {
	open = $bindable(false),
	sessionId,
	version,
	trigger = null,
}: {
	open?: boolean
	sessionId: string
	/** Which version's log to show: 'original', or a job id. */
	version: string
	/** The control that opened this, so Escape hands focus back to it.
	 *
	 * The dialog is rendered once for the whole version table and driven by a
	 * pair of variables, so no element "owns" it and the focus that comes back
	 * on close is not the row's button but the section's fold header - one
	 * Space from collapsing the table the reader was looking at. The row that
	 * opened it is the only thing that knows which button that was, so it says
	 * so. Null means whatever the dialog would do on its own. */
	trigger?: HTMLElement | null
} = $props()

let lines = $state<string[]>([])
let error = $state('')
let loading = $state(false)
// Bumped per fetch, so one that is still in flight when the version changes
// under the dialog cannot land its lines, or clear `loading`, after the newer
// one. A plain variable: only ever compared after an await, never rendered.
let loadToken = 0

// Opening is what fetches, and a version changing under an open dialog
// refetches: the row that asked is the only thing that knows which log is
// wanted, and it says so by setting both at once.
$effect(() => {
	if (open) void load(version)
})

async function load(wanted: string) {
	const token = ++loadToken
	lines = []
	error = ''
	loading = true
	try {
		const stored = await api.getVersionLogs(sessionId, wanted)
		if (token !== loadToken) return
		lines = stored.logs.split('\n').filter((line) => line !== '')
	} catch (err) {
		if (token !== loadToken) return
		error =
			err instanceof ApiError && err.status === 404
				? 'No logs were stored for this version.'
				: err instanceof ApiError
					? err.message
					: 'failed to load logs'
	} finally {
		if (token === loadToken) loading = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent
		class="sm:max-w-3xl"
		onCloseAutoFocus={(e) => {
			// A row deleted while its log was open has no button left to focus, so
			// let the default restoration have it.
			if (!trigger?.isConnected) return
			e.preventDefault()
			trigger.focus()
		}}
	>
		<DialogHeader>
			<DialogTitle>Logs · {versionLabel(version)}</DialogTitle>
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
				{#if loading}
					<span class="text-muted-foreground">Loading…</span>
				{:else if lines.length === 0}
					<span class="text-muted-foreground">The stored log has no lines to show.</span>
				{/if}
			</div>
		{/if}
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Close</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
