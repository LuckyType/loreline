<script lang="ts">
/**
 * The session's summary, and what can be made from it.
 *
 * A summary is a recap, and a video is generated from that recap, so both
 * triggers live here and both are disabled with the reason when the thing they
 * need is missing: an LLM provider, or a summary to work from. They sit in the
 * section's header rather than under the recap, because a control buried at
 * the foot of a body that scrolls is one you have to go looking for - and the
 * header is drawn whether the section is folded or not.
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
 * Finished videos are not here at all: they are in a dialog off the header
 * (see SessionVideosDialog), whose trigger carries the count so nobody has to
 * scroll to the end of a recap to find out one exists.
 *
 * Open, the section takes an equal share of the card's leftover height and
 * scrolls inside it, so a long recap pushes neither the transcript nor the
 * player off the screen.
 */

import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import { confirm } from '$lib/confirm.svelte'
import Foldable from '$lib/Foldable.svelte'
import GenerateVideoDialog from '$lib/GenerateVideoDialog.svelte'
import Markdown from '$lib/Markdown.svelte'
import SessionVideosDialog from '$lib/SessionVideosDialog.svelte'
import { providerName, versionLabel } from '$lib/stores'
import SummarizeDialog from '$lib/SummarizeDialog.svelte'
import { cn } from '$lib/utils'
import type { Session, VideoJob } from '$lib/wire'

let {
	sessionId,
	session,
	speakers,
	version,
	videoJobs,
	open = $bindable(true),
	onsummarized,
	onvideoschanged,
	onerror,
}: {
	sessionId: string
	session: Session
	/** The distinct speaker labels in the shown transcript. */
	speakers: string[]
	/** The transcript version the page is showing: what a new summary would be
	 *  read from, which is not necessarily what the stored one was. */
	version: string
	/** Every video generated from this session. The page owns the list and the
	 *  poll behind it, because the export menu in the header reads it too. */
	videoJobs: VideoJob[]
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** A summary was stored: the caller refetches the session. */
	onsummarized?: () => Promise<void> | void
	/** A generation was queued or deleted: the caller refetches the job list.
	 *  Awaited, so the count on the trigger is right before anything reads it. */
	onvideoschanged?: () => Promise<void> | void
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
let videosOpen = $state(false)

/** What the Videos trigger says on hover. The count on its face is every
 *  generation this session has, which is the number that makes them
 *  discoverable; it cannot say which of them is watchable now, still running
 *  or dead, and those are three different reasons to open the dialog. */
const videosTitle = $derived.by(() => {
	const done = videoJobs.filter((j) => j.status === 'done').length
	const running = videoJobs.filter((j) => j.status === 'queued' || j.status === 'running').length
	const failed = videoJobs.filter((j) => j.status === 'error').length
	const parts: string[] = []
	if (done) parts.push(`${done} to watch`)
	if (running) parts.push(`${running} generating`)
	if (failed) parts.push(`${failed} failed`)
	return parts.join(', ')
})

/** Delete one generation and its file.
 *
 * Deleting the last one closes the dialog, because the trigger that opened it
 * is gone the moment the count reaches zero: leaving an empty dialog behind a
 * button that no longer exists is a dead end you can only escape by hand. Done
 * here rather than in an effect - it is the consequence of a press, not a
 * state to keep in sync. */
async function deleteVideo(jobId: string) {
	if (!(await confirm('Delete this video and its file?'))) return
	try {
		await api.deleteVideoJob(jobId)
		await onvideoschanged?.()
		if (videoJobs.length === 0) videosOpen = false
	} catch (err) {
		onerror?.(err instanceof ApiError ? err.message : 'delete failed')
	}
}
</script>

<CardContent class={cn('flex flex-col gap-2', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable
		title="Summary"
		meta={summaryMeta}
		bind:open
		bodyClass="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto"
	>
		{#snippet actions()}
			{#if videoJobs.length}
				<!-- The count is the whole point of this button. "Videos" on its own
				     says nothing about whether there is anything behind it, and the
				     complaint that moved the players in here was precisely that a
				     finished generation was impossible to notice. No generations, no
				     button: there would be nothing to open. -->
				<Button variant="outline" size="sm" onclick={() => (videosOpen = true)} title={videosTitle}>
					Videos ({videoJobs.length})
				</Button>
			{/if}
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
		{/snippet}

		{#if session.summary}
			<Markdown source={session.summary} />
		{:else if llmProviders.length === 0}
			<p class="m-0 text-muted-foreground">
				Add an LLM provider (OpenAI-compatible chat) in Settings to enable summaries.
			</p>
		{:else}
			<p class="m-0 text-muted-foreground">Not summarized yet.</p>
		{/if}
	</Foldable>
</CardContent>

<GenerateVideoDialog
	bind:open={videoOpen}
	{sessionId}
	summary={session.summary ?? ''}
	onqueued={onvideoschanged}
/>

<SessionVideosDialog bind:open={videosOpen} jobs={videoJobs} ondelete={deleteVideo} />

<SummarizeDialog bind:open={summarizeOpen} {sessionId} {speakers} {version} {onsummarized} />
