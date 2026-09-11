<script lang="ts">
/**
 * Diarize a transcript: work out who was speaking, and relabel the segments.
 *
 * Diarization runs over the whole session recording and rewrites the speaker
 * labels of one version, so it needs the version it will change and a session
 * with stored audio to run over - the caller only opens this when there is
 * one.
 *
 * A dialog rather than a row of controls under the transcript, for the reason
 * every other action here moved: a mode dropdown, an endpoint, two speaker
 * counts and a button are five controls that wrapped into a second line on a
 * phone and pushed the rename button off the end of the first. The section's
 * header now carries a single Diarize button, and this is what it opens.
 *
 * The component stays mounted while the dialog is shut, deliberately: an
 * endpoint typed once is still there on the next open, and a session whose
 * first run needed a hand-typed address usually needs it again.
 *
 * A blank endpoint is a real choice, and the dialog says what it means. The
 * chain is the one the server walks: what is typed here, else the session's
 * own endpoint (sent as the request's), else the default saved under
 * Settings (filled in by the server when the request names none). The hint
 * under the field names the link that will actually be used, so nobody has to
 * open Settings to find out where a run went. With nothing at any link the
 * run could only fail, so the button says so and refuses instead of queuing
 * it: the server would answer 400 either way, but a disabled button with the
 * reason on it is a better place to learn that than a banner after the press.
 */

import { actionSetup } from '$lib/actionSetup.svelte'
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
import Dropdown from '$lib/Dropdown.svelte'
import { versionLabel } from '$lib/stores'
import type { DiarizationModeKind } from '$lib/wire'

let {
	open = $bindable(false),
	sessionId,
	version,
	sessionEndpoint,
	onqueued,
	onerror,
}: {
	open?: boolean
	sessionId: string
	/** The version to relabel: 'original', or a re-transcription's job id.
	 *  Whatever the page is showing, which is the one the reader was looking at
	 *  when they pressed the button. */
	version: string
	/** The endpoint the session was captured with, the middle link of the
	 *  fallback chain: what is typed here wins, this is what stands in for it,
	 *  and null lets the server fall back to its own configuration. */
	sessionEndpoint?: string | null
	/** A diarization has been queued: the caller refetches the job list.
	 *  Awaited, so the new row is on screen before the dialog closes. */
	onqueued?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

let kind = $state<DiarizationModeKind>('remote')
let endpoint = $state('')
let minSpeakers = $state('')
let maxSpeakers = $state('')
let busy = $state(false)

/** Where a blank field sends the run, and which link of the chain that is.
 *  Null when there is nothing at either link. */
const fallback = $derived.by(() => {
	if (sessionEndpoint) return { endpoint: sessionEndpoint, from: "the session's own endpoint" }
	const stored = actionSetup.defaults.diar_endpoint
	if (stored) return { endpoint: stored, from: 'the default saved under Settings' }
	return null
})
const NO_ENDPOINT = 'No diarization endpoint: type one here, or save a default under Settings.'
// Only the remote diarizer needs an address; the OpenAI mode has the
// provider row's.
const noEndpoint = $derived(kind === 'remote' && !endpoint.trim() && !fallback)

async function diarizeSession() {
	if (noEndpoint) {
		onerror?.(NO_ENDPOINT)
		return
	}
	busy = true
	onerror?.('')
	try {
		await api.enqueueReprocess({
			session_id: sessionId,
			operation: 'diarize',
			target: version,
			diarization: {
				mode: kind,
				// Only the remote diarizer has an address to send: the OpenAI mode
				// talks to the provider row's own endpoint. Null is the server's
				// cue to use the stored default (see `fallback`).
				endpoint: kind === 'remote' ? endpoint.trim() || sessionEndpoint || null : null,
				// Blank means "no opinion", which is not the same as zero: the
				// server picks the count itself when neither bound is given.
				min_speakers: minSpeakers ? Number(minSpeakers) : null,
				max_speakers: maxSpeakers ? Number(maxSpeakers) : null,
			},
		})
		await onqueued?.()
		// Only on success: a run that failed to queue leaves the dialog up with
		// the settings still in it, next to the banner saying why.
		open = false
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'diarize failed')
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="sm:max-w-md">
		<DialogHeader>
			<DialogTitle>Diarize transcript</DialogTitle>
			<!-- Said before the run, not after it: this relabels a transcript that
			     may already carry a diarization somebody was happy with, and the
			     previous one does not survive. -->
			<DialogDescription>
				Diarizes the full session audio and relabels transcript
				<code>{versionLabel(version)}</code>, the version on screen. Re-running replaces its
				previous diarization. It runs in the background - you can close this page.
			</DialogDescription>
		</DialogHeader>

		<div class="flex flex-col gap-4">
			<div class="flex flex-col gap-2">
				<Label for="diarize-mode">Diarizer</Label>
				<Dropdown
					id="diarize-mode"
					bind:value={kind}
					options={[
            { value: 'remote', label: 'sherpa-onnx' },
            { value: 'openai', label: 'gpt-4o-transcribe-diarize' }
          ]}
				/>
			</div>

			{#if kind === 'remote'}
				<div class="flex flex-col gap-2">
					<Label for="diarize-endpoint">Endpoint</Label>
					<Input
						id="diarize-endpoint"
						placeholder="http://…:8001"
						aria-describedby="diarize-endpoint-hint"
						bind:value={endpoint}
					/>
					<!-- Which endpoint a blank field means, by name: the promise the
					     field makes is only worth something if it says what it will
					     do. -->
					<span
						id="diarize-endpoint-hint"
						class={['text-xs', noEndpoint ? 'text-destructive' : 'text-muted-foreground']}
					>
						{#if endpoint.trim()}
							Runs at the endpoint typed above.
						{:else if fallback}
							Blank uses {fallback.from}: <code>{fallback.endpoint}</code>
						{:else}
							{NO_ENDPOINT}
						{/if}
					</span>
				</div>
			{/if}

			<div class="flex gap-3">
				<div class="flex flex-1 flex-col gap-2">
					<Label for="diarize-min">Min speakers</Label>
					<Input
						id="diarize-min"
						type="number"
						min="1"
						placeholder="min"
						bind:value={minSpeakers}
					/>
				</div>
				<div class="flex flex-1 flex-col gap-2">
					<Label for="diarize-max">Max speakers</Label>
					<Input
						id="diarize-max"
						type="number"
						min="1"
						placeholder="max"
						bind:value={maxSpeakers}
					/>
				</div>
			</div>
			<span class="-mt-2 text-xs text-muted-foreground">
				Both optional: left blank, the diarizer decides how many voices it heard.
			</span>
		</div>

		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<!-- Disabled with nothing to run against; the hint under the field is
			     where the reason is, since a disabled button shows no tooltip. -->
			<Button onclick={diarizeSession} disabled={busy || noEndpoint}>
				{busy ? 'Queuing…' : 'Diarize'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
