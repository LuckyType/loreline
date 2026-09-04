<script lang="ts">
/**
 * Give each detected speaker a display name.
 *
 * The form is seeded once per opening, from the labels the shown version
 * actually carries and whatever names the session already stores. It is not
 * bound to either: a version arriving under an open dialog must not wipe an
 * edit in progress.
 */

import { untrack } from 'svelte'
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
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'

let {
	open = $bindable(false),
	sessionId,
	speakers,
	names = {},
	onsaved,
	onerror,
}: {
	open?: boolean
	sessionId: string
	/** The distinct speaker labels in the shown transcript. */
	speakers: string[]
	/** The names already stored for them. */
	names?: Record<string, string>
	/** Names were saved: the caller refetches the session. Awaited, so the
	 *  dialog only closes once the transcript behind it has caught up. */
	onsaved?: () => Promise<void> | void
	/** What went wrong. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

let nameForm = $state<Record<string, string>>({})

$effect(() => {
	if (!open) return
	untrack(() => {
		nameForm = Object.fromEntries(speakers.map((s) => [s, names[s] ?? '']))
	})
})

async function saveNames() {
	const wanted: Record<string, string> = {}
	for (const [label, name] of Object.entries(nameForm)) {
		if (name.trim()) wanted[label] = name.trim()
	}
	try {
		await api.setSpeakerNames(sessionId, wanted)
		await onsaved?.()
		open = false
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'rename failed')
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Rename speakers</DialogTitle>
			<DialogDescription>
				Set a display name for each detected speaker; blank keeps the original label. Applied in the
				transcript and exports.
			</DialogDescription>
		</DialogHeader>
		<div class="flex flex-col gap-3">
			{#each speakers as s, i (s)}
				<div class="flex flex-col gap-2">
					<Label for="speaker-name-{i}">{s}</Label>
					<Input id="speaker-name-{i}" bind:value={nameForm[s]} placeholder={s} />
				</div>
			{/each}
		</div>
		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<Button onclick={saveNames}>Save names</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
