<script lang="ts">
/**
 * The session's summary, and what can be made from it.
 *
 * A summary is a recap, and a video is generated from that recap, so both
 * triggers live here and both are disabled with the reason when the thing they
 * need is missing: an LLM provider, or a summary to work from. They sit in the
 * section's header rather than under the recap, because a control buried at
 * the foot of a body that scrolls is one you have to go looking for - and the
 * header is drawn whether the section is folded or not. They read in the order
 * they are used: summarize, then make a video of it, then watch what came out.
 *
 * Below the `sm` breakpoint only the first of them keeps a place on the
 * header, and only as a bare icon with its words in `aria-label` and `title`.
 * Three labelled buttons never fitted beside the section's title on a phone,
 * and the previous answer - shrink all three to icons - only traded that for a
 * row of unlabelled squares. So summarizing stays, because it is the thing
 * this section is for, and the two that make and show a video move into an
 * overflow menu behind a `…`. That menu is a real one (see the vendored
 * dropdown-menu, which the session header's Export menu is built on too): it
 * opens on tap, on Enter and on the arrow keys, and it closes on Escape or a
 * press anywhere else.
 *
 * The video count follows them onto that trigger. It is the whole reason the
 * Videos button exists - the complaint that moved the players in here was that
 * a finished generation was impossible to notice - and burying it behind an
 * unlabelled `…` would put that complaint straight back. With no generations
 * there is no number, and no Videos entry either: there would be nothing to
 * open.
 *
 * An entry the session cannot use yet says why on its own second line rather
 * than in a `title`, because the phone this menu exists for has no hover to
 * show a tooltip to. It is `aria-describedby` for the entry as well, so the
 * reason is the same sentence however it is reached. What that costs is that
 * the arrow keys skip a disabled entry, so a keyboard user walking the menu
 * steps over the explanation rather than hearing it; below `sm` there is
 * rarely a keyboard to walk it with, and the sentence is on the screen either
 * way.
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
 * (see SessionVideosDialog), reached from the button at `sm` and up and from
 * the overflow menu below it, carrying the count either way so nobody has to
 * scroll to the end of a recap to find out one exists.
 *
 * Open, the section takes an equal share of the card's leftover height and
 * scrolls inside it, so a long recap pushes neither the transcript nor the
 * player off the screen.
 */

import { Clapperboard, Ellipsis, Film, Sparkles } from '@lucide/svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { CardContent } from '$lib/components/ui/card'
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '$lib/components/ui/dropdown-menu'
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
const summarizeLabel = $derived(session.summary ? 'Re-summarize' : 'Summarize')

// The overflow menu's reason line needs a stable id to be pointed at, and this
// card is drawn once per page but the component is not a singleton.
const uid = $props.id()
const videoReasonId = `${uid}-video-reason`

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

/** Why a video cannot be generated right now, or '' when one can.
 *
 * Said once and read three times - by the button's `disabled`, by its `title`
 * and by the line under the overflow menu's entry - because the three used to
 * be three copies of the same nested ternary, and a reason that disagrees with
 * the state that produced it is worse than no reason at all. */
const videoBlocked = $derived(
	videoProviders.length === 0
		? 'Add an OpenRouter provider in Settings'
		: !session.summary
			? 'Summarize the session first'
			: '',
)

/** What became of this session's generations, in the fewest words that still
 *  distinguish "three to watch" from "three that failed" - '' when none of the
 *  statuses below matched, which is the caller's cue to say nothing. */
const videosStatus = $derived.by(() => {
	const done = videoJobs.filter((j) => j.status === 'done').length
	const running = videoJobs.filter((j) => j.status === 'queued' || j.status === 'running').length
	const failed = videoJobs.filter((j) => j.status === 'error').length
	const parts: string[] = []
	if (done) parts.push(`${done} to watch`)
	if (running) parts.push(`${running} generating`)
	if (failed) parts.push(`${failed} failed`)
	return parts.join(', ')
})

/** What the `…` says it is, spoken rather than drawn. The glyph alone names
 *  nothing, and the number beside it is the one thing on this header worth
 *  interrupting a reader for, so it is in the label and not only on the face. */
const overflowLabel = $derived(
	videoJobs.length
		? `More actions, ${videoJobs.length} video${videoJobs.length === 1 ? '' : 's'}`
		: 'More actions',
)

/** What the Videos button says on hover. The count on its face is every
 *  generation this session has, which is the number that makes them
 *  discoverable; it cannot say which of them is watchable now, still running
 *  or dead, and those are three different reasons to open the dialog. A status
 *  nothing above covers leaves `videosStatus` empty, and falling back to the
 *  button's own label is what stops the tooltip from being blank. */
const videosTitle = $derived(videosStatus || `Videos (${videoJobs.length})`)

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
			<!-- The one control that stays on the header at every width, because
			     summarizing is what this section is for. Its label is still hidden
			     below `sm`, where it is one icon beside the `…` rather than one of
			     three identical squares, and `aria-label` carries the words the eye
			     no longer gets. -->
			<Button
				variant="outline"
				size="sm"
				onclick={openSummarize}
				disabled={llmProviders.length === 0}
				aria-label={summarizeLabel}
				title={llmProviders.length === 0
					? 'Add an LLM provider (OpenAI-compatible chat) in Settings'
					: summarizeLabel}
			>
				<Sparkles />
				<span class="hidden sm:inline">{summarizeLabel}</span>
			</Button>
			<!-- These two are gone below `sm`, not shrunk: the overflow menu under
			     them is the same two entries with their labels intact, which is a
			     better trade on a phone than two more anonymous icons. Labelled here,
			     so no `aria-label` is needed to repeat what the button already
			     says. -->
			<Button
				variant="outline"
				size="sm"
				class="max-sm:hidden"
				onclick={() => (videoOpen = true)}
				disabled={!!videoBlocked}
				title={videoBlocked || 'Generate video'}
			>
				<Clapperboard />
				Generate video
			</Button>
			{#if videoJobs.length}
				<Button
					variant="outline"
					size="sm"
					class="max-sm:hidden"
					onclick={() => (videosOpen = true)}
					title={videosTitle}
				>
					<Film />
					Videos ({videoJobs.length})
				</Button>
			{/if}
			<DropdownMenu>
				<DropdownMenuTrigger>
					{#snippet child({ props })}
						<!-- The count rides the trigger, because a `…` on its own admits
						     to nothing behind it and the number is the only part of this
						     header worth noticing from across a room. `title` is left off
						     on purpose: this button exists only below `sm`, where there is
						     no pointer to hover it with, so a tooltip would be a string
						     nobody can ever read. -->
						<Button
							{...props}
							variant="outline"
							size="sm"
							class="sm:hidden"
							aria-label={overflowLabel}
						>
							<Ellipsis />
							{#if videoJobs.length}
								<span>{videoJobs.length}</span>
							{/if}
						</Button>
					{/snippet}
				</DropdownMenuTrigger>
				<!-- Aligned to its own end so it hangs under the right edge of the
				     header rather than off the side of a 320px screen, and as wide as
				     the export menu in the session header above, which is the other
				     menu on this page. -->
				<DropdownMenuContent align="end" class="w-56">
					<DropdownMenuItem
						class={['flex-col items-start gap-0.5', videoBlocked && 'data-disabled:opacity-100']}
						disabled={!!videoBlocked}
						aria-describedby={videoBlocked ? videoReasonId : undefined}
						onSelect={() => (videoOpen = true)}
					>
						<span class={['flex items-center gap-2', videoBlocked && 'text-muted-foreground']}>
							<Clapperboard />
							Generate video
						</span>
						{#if videoBlocked}
							<!-- The reason in text rather than in a `title`, and left at
							     full opacity while the label above it dims: an explanation
							     faded to half is an explanation nobody reads, which is the
							     same failure as not having one. -->
							<span id={videoReasonId} class="text-xs text-muted-foreground">{videoBlocked}</span>
						{/if}
					</DropdownMenuItem>
					{#if videoJobs.length}
						<DropdownMenuItem
							class="flex-col items-start gap-0.5"
							onSelect={() => (videosOpen = true)}
						>
							<span class="flex items-center gap-2">
								<Film />
								Videos ({videoJobs.length})
							</span>
							{#if videosStatus}
								<!-- What the button's tooltip says at `sm` and up, in the one
								     place a phone can actually read it. -->
								<span class="text-xs text-muted-foreground">{videosStatus}</span>
							{/if}
						</DropdownMenuItem>
					{/if}
				</DropdownMenuContent>
			</DropdownMenu>
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
