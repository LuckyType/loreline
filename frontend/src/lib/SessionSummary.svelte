<script lang="ts">
/**
 * The session's summary, and what can be made from it.
 *
 * A summary is a recap, and a video is generated from that recap, so both
 * triggers live here and both are disabled with the reason when the thing they
 * need is missing: an LLM provider, or a summary to work from.
 *
 * A session has one summary but several transcripts, so the meta line says
 * which version this one was read from alongside the provider and model that
 * wrote it - and the dialog behind the button says which version the next one
 * would read, before it is written. Summaries stored before that was recorded
 * say so rather than guessing at 'original'.
 *
 * The recap itself is markdown: every current chat model answers "write a
 * summary" with headings and bullets, and the built-in prompt does not ask it
 * not to, so this renders it (see Markdown.svelte) instead of showing the
 * reader the literal `##`.
 *
 * Open, the section takes an equal share of the card's leftover height and
 * scrolls inside it, so a long recap and a stack of generated videos push
 * neither the transcript nor the player off the screen.
 */

import { onMount } from 'svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import { confirm } from '$lib/confirm.svelte'
import Foldable from '$lib/Foldable.svelte'
import GenerateVideoDialog from '$lib/GenerateVideoDialog.svelte'
import Markdown from '$lib/Markdown.svelte'
import { providerName, versionLabel } from '$lib/stores'
import SummarizeDialog from '$lib/SummarizeDialog.svelte'
import { cn } from '$lib/utils'
import type { Session, VideoJob } from '$lib/wire'

let {
	sessionId,
	session,
	speakers,
	version,
	open = $bindable(true),
	onsummarized,
	onerror,
}: {
	sessionId: string
	session: Session
	/** The distinct speaker labels in the shown transcript. */
	speakers: string[]
	/** The transcript version the page is showing: what a new summary would be
	 *  read from, which is not necessarily what the stored one was. */
	version: string
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** A summary was stored: the caller refetches the session. */
	onsummarized?: () => Promise<void> | void
	/** What went wrong. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

const llmProviders = $derived(actionSetup.providersFor('summarize'))
let summarizeOpen = $state(false)

/** The fold header's one line about the stored summary: who wrote it, with
 *  what, and from which transcript. A summary written before the version was
 *  recorded says exactly that - claiming 'original' would be a guess, and on a
 *  session with five versions it is the guess most likely to be wrong. */
const summaryMeta = $derived.by(() => {
	if (!session.summary || !session.summary_model) return ''
	const provider = providerName(session.summary_provider, actionSetup.providers)
	const from = session.summary_version
		? `from ${versionLabel(session.summary_version)}`
		: 'version not recorded'
	return `${provider} · ${session.summary_model} · ${from}`
})

async function openSummarize() {
	if (
		speakers.length === 0 &&
		!(await confirm('This session has no diarized speakers. Summarize anyway?'))
	)
		return
	summarizeOpen = true
}

// --- video generation ---
// Only OpenRouter can generate video (see supports_video in
// src/loreline/video/client.py); every other provider kind is filtered out
// rather than offered and rejected at submit time.
const videoProviders = $derived(actionSetup.providersFor('video'))
let videoOpen = $state(false)
let videoJobs = $state<VideoJob[]>([])

const videoRunning = $derived(
	videoJobs.some((j) => j.status === 'queued' || j.status === 'running'),
)

/** A generation takes minutes, so this polls only while something is actually
 *  in flight and stops as soon as the queue drains. Hanging the interval off an
 *  effect means the same teardown covers all three ways it should stop: the
 *  queue draining, the flag flipping, and the page unmounting. There is no
 *  timer left to clear by hand, and none left running behind a dead page. */
$effect(() => {
	if (!videoRunning) return
	const timer = setInterval(refreshVideoJobs, 5000)
	return () => clearInterval(timer)
})

async function refreshVideoJobs() {
	videoJobs = await api.listVideoJobs(sessionId)
}

async function deleteVideo(jobId: string) {
	if (!(await confirm('Delete this video and its file?'))) return
	await api.deleteVideoJob(jobId)
	await refreshVideoJobs()
}

onMount(async () => {
	try {
		await refreshVideoJobs()
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'failed to load')
	}
})
</script>

<CardContent class={cn('flex flex-col gap-2', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable
		title="Summary"
		meta={summaryMeta}
		bind:open
		bodyClass="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto"
	>
		{#if session.summary}
			<Markdown source={session.summary} />
		{:else if llmProviders.length === 0}
			<p class="m-0 text-muted-foreground">
				Add an LLM provider (OpenAI-compatible chat) in Settings to enable summaries.
			</p>
		{:else}
			<p class="m-0 text-muted-foreground">Not summarized yet.</p>
		{/if}
		<div class="flex justify-end gap-2">
			<Button
				variant="outline"
				size="sm"
				onclick={() => (videoOpen = true)}
				disabled={videoProviders.length === 0 || !session.summary}
				title={videoProviders.length === 0
					? 'Add an OpenRouter provider in Settings'
					: !session.summary
						? 'Summarize the session first'
						: ''}
			>
				Generate video
			</Button>
			<Button
				variant="outline"
				size="sm"
				onclick={openSummarize}
				disabled={llmProviders.length === 0}
			>
				{session.summary ? 'Re-summarize' : 'Summarize'}
			</Button>
		</div>

		{#if videoJobs.length}
			<div class="mt-3 flex flex-col gap-3 border-t pt-3">
				{#each videoJobs as job (job.id)}
					<div class="flex flex-col gap-2">
						<div class="flex items-center justify-between gap-2">
							<span class="min-w-0 truncate text-xs text-muted-foreground">
								{job.model}{job.duration ? ` · ${job.duration}s` : ''}
								{job.resolution
									? ` · ${job.resolution}`
									: ''}
							</span>
							<span class="flex shrink-0 items-center gap-2">
								{#if job.status === 'queued' || job.status === 'running'}
									<span class="text-xs text-muted-foreground">Generating…</span>
								{:else if job.status === 'error'}
									<span class="text-xs text-destructive">{job.error ?? 'failed'}</span>
								{/if}
								<Button variant="ghost" size="sm" onclick={() => deleteVideo(job.id)}>
									Delete
								</Button>
							</span>
						</div>
						{#if job.status === 'done'}
							<!-- svelte-ignore a11y_media_has_caption -->
							<video
								class="w-full rounded-md border"
								controls
								preload="metadata"
								src={api.videoContentUrl(job.id)}
							></video>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</Foldable>
</CardContent>

<GenerateVideoDialog
	bind:open={videoOpen}
	{sessionId}
	summary={session.summary ?? ''}
	onqueued={refreshVideoJobs}
/>

<SummarizeDialog bind:open={summarizeOpen} {sessionId} {speakers} {version} {onsummarized} />
