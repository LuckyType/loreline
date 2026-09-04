<script lang="ts">
/**
 * The selected transcript: what produced it, what it says, and the two things
 * that can still be done to it.
 *
 * Diarization runs over the whole session audio and relabels this version, so
 * it belongs next to the version it will change rather than next to the job
 * list. Renaming speakers needs speakers to rename, which only a diarized
 * version has, so the button says so by being disabled.
 */

import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import Dropdown from '$lib/Dropdown.svelte'
import Foldable from '$lib/Foldable.svelte'
import { Input } from '$lib/components/ui/input'
import RenameSpeakersDialog from '$lib/RenameSpeakersDialog.svelte'
import { diarizerLabel, providerName } from '$lib/stores'
import TranscriptList from '$lib/TranscriptList.svelte'
import type { DiarizationModeKind, ReprocessJob, SessionDetail, TranscriptEvent } from '$lib/wire'

let {
	sessionId,
	detail,
	jobs,
	version,
	events,
	speakers,
	open = $bindable(true),
	onqueued,
	onerror,
	onrenamed,
}: {
	sessionId: string
	detail: SessionDetail
	jobs: ReprocessJob[]
	/** The version being shown: 'original', or a re-transcription's job id. */
	version: string
	events: TranscriptEvent[]
	/** The distinct speaker labels in `events`. */
	speakers: string[]
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** A diarization has been queued: the caller refetches the job list. */
	onqueued?: () => Promise<void> | void
	/** Names were saved: the caller refetches the session. */
	onrenamed?: () => Promise<void> | void
	/** What went wrong, '' when an attempt starts. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

const hasAudio = $derived(!!detail.session.audio_path)

let diarKind = $state<DiarizationModeKind>('remote')
let diarEndpoint = $state('')
let diarMin = $state('')
let diarMax = $state('')
let busy = $state(false)
let renameOpen = $state(false)

// The diarize job whose relabeling the selected version currently shows.
const diarizeJob = $derived(
	jobs
		.filter(
			(j) =>
				j.operation === 'diarize' &&
				(j.target ?? 'original') === version &&
				j.status === 'done' &&
				j.segments_added > 0,
		)
		.sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))[0],
)
const selectedJob = $derived(jobs.find((j) => j.operation === 'transcribe' && j.id === version))
const selectedProviderName = $derived(
	version === 'original'
		? providerName(detail.session.primary_provider, actionSetup.providers)
		: providerName(selectedJob?.provider_id, actionSetup.providers),
)
const selectedModel = $derived(version === 'original' ? '-' : (selectedJob?.model ?? '-'))

async function diarizeSession() {
	busy = true
	onerror?.('')
	try {
		await api.enqueueReprocess({
			session_id: sessionId,
			operation: 'diarize',
			target: version,
			diarization: {
				mode: diarKind,
				endpoint:
					diarKind === 'remote'
						? diarEndpoint || detail.session.diarization.endpoint || null
						: null,
				min_speakers: diarMin ? Number(diarMin) : null,
				max_speakers: diarMax ? Number(diarMax) : null,
			},
		})
		await onqueued?.()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'diarize failed')
	} finally {
		busy = false
	}
}
</script>

<CardContent class="flex flex-col gap-3">
	<Foldable
		title="Transcript"
		meta="{version === 'original'
			? 'original'
			: version.slice(0, 8)} · {events.length} segments"
		bind:open
	>
		<div
			class="flex flex-wrap items-center justify-between gap-3 rounded-md bg-accent/40 px-3 py-2.5"
		>
			<div class="flex flex-wrap items-center gap-x-4 gap-y-1">
				<span
					>Transcript
					<code>{version === 'original' ? 'original' : version.slice(0, 8)}</code></span
				>
				<span><span class="text-muted-foreground">Provider</span> {selectedProviderName}</span>
				<span><span class="text-muted-foreground">Model</span> {selectedModel}</span>
				{#if diarizeJob}
					<span
						><span class="text-muted-foreground">Diarized with</span>
						{diarizerLabel(diarizeJob)}</span
					>
				{:else}
					<span class="text-muted-foreground">Not diarized</span>
				{/if}
				<span><span class="text-muted-foreground">Segments</span> {events.length}</span>
			</div>
			<div class="flex flex-wrap items-center gap-2">
				{#if hasAudio}
					<Dropdown
						class="max-w-52"
						bind:value={diarKind}
						options={[
                { value: 'remote', label: 'sherpa-onnx' },
                { value: 'openai', label: 'gpt-4o-transcribe-diarize' }
              ]}
					/>
					{#if diarKind === 'remote'}
						<Input class="max-w-48" placeholder="http://…:8001" bind:value={diarEndpoint} />
					{/if}
					<Input class="max-w-20" type="number" min="1" placeholder="min" bind:value={diarMin} />
					<Input class="max-w-20" type="number" min="1" placeholder="max" bind:value={diarMax} />
					<Button
						variant="outline"
						size="sm"
						onclick={diarizeSession}
						disabled={busy}
						title="Diarizes the full session audio and relabels the selected transcription; re-running replaces its previous diarization."
					>
						Diarize
					</Button>
					<div class="h-5 w-px bg-border"></div>
				{/if}
				<Button
					variant="outline"
					size="sm"
					onclick={() => (renameOpen = true)}
					disabled={speakers.length === 0}
				>
					Rename speakers
				</Button>
			</div>
		</div>
		<TranscriptList
			{events}
			names={detail.session.speaker_names}
			providers={actionSetup.providers}
			showSource={version === 'original'}
		/>
	</Foldable>
</CardContent>

<RenameSpeakersDialog
	bind:open={renameOpen}
	{sessionId}
	{speakers}
	names={detail.session.speaker_names}
	onsaved={onrenamed}
	{onerror}
/>
