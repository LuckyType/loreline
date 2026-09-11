<script lang="ts">
/**
 * What has been made of this session, and what else can be.
 *
 * Three texts live here, and they are three because they are for three
 * readers. The summary is the GM's index of what happened. The recap is what
 * the table is told during the week, written for the players and rendered with
 * a Copy button because it is meant to be pasted somewhere else. The
 * extraction is the names the session used, which is not prose at all: it is
 * what the campaign's glossary and its cast list are built from. A video is
 * generated from the summary, which is why the video controls are here too.
 *
 * Every trigger except Summarize lives in one overflow menu, at every width.
 * Five labelled buttons never fitted beside the section's title, and the
 * previous answer - some of them as buttons above `sm` and the same ones
 * repeated in a menu below it - meant two places to keep in step and two
 * chances for an entry to be enabled in one and disabled in the other. The
 * menu is a real one (see the vendored dropdown-menu, which the session
 * header's Export menu is built on too): it opens on tap, on Enter and on the
 * arrow keys, and it closes on Escape or a press anywhere else.
 *
 * The video count rides the menu's trigger. It is the whole reason the Videos
 * entry exists - the complaint that moved the players in here was that a
 * finished generation was impossible to notice - and burying it behind an
 * unlabelled glyph would put that complaint straight back.
 *
 * An entry the session cannot use yet says why on its own second line rather
 * than in a `title`, because the phone this menu exists for has no hover to
 * show a tooltip to. It is `aria-describedby` for the entry as well, so the
 * reason is the same sentence however it is reached. What that costs is that
 * the arrow keys skip a disabled entry, so a keyboard user walking the menu
 * steps over the explanation rather than hearing it; the sentence is on the
 * screen either way.
 *
 * A session has one of each of these but several transcripts, so each says
 * which version it was read from, and the dialog behind each trigger says
 * which version the next one would read, before it is written. Texts stored
 * before that was recorded say so rather than guessing at 'original'.
 *
 * Open, the section takes an equal share of the card's leftover height and
 * scrolls inside it, so a long recap pushes neither the transcript nor the
 * player off the screen.
 */

import { Clapperboard, Copy, Ellipsis, FileText, Film, ScanSearch, Sparkles } from '@lucide/svelte'
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
import ExtractedNames from '$lib/ExtractedNames.svelte'
import Foldable from '$lib/Foldable.svelte'
import GenerateDialog from '$lib/GenerateDialog.svelte'
import GenerateVideoDialog from '$lib/GenerateVideoDialog.svelte'
import Markdown from '$lib/Markdown.svelte'
import SessionVideosDialog from '$lib/SessionVideosDialog.svelte'
import { providerName, versionLabel } from '$lib/stores'
import type { GenerateKind } from '$lib/types'
import { cn } from '$lib/utils'
import type {
	GenerateRequest,
	MergedEntity,
	Session,
	SessionDocument,
	SessionExtraction,
	VideoJob,
} from '$lib/wire'

let {
	sessionId,
	session,
	documents,
	speakers,
	version,
	videoJobs,
	open = $bindable(true),
	ongenerated,
	onvideoschanged,
	onerror,
}: {
	sessionId: string
	session: Session
	/** The recap and the extraction, as the session detail serves them. */
	documents: SessionDocument[]
	/** The distinct speaker labels in the shown transcript. */
	speakers: string[]
	/** The transcript version the page is showing: what a new text would be
	 *  read from, which is not necessarily what the stored one was. */
	version: string
	/** Every video generated from this session. The page owns the list and the
	 *  poll behind it, because the export menu in the header reads it too. */
	videoJobs: VideoJob[]
	/** Fold state, kept by the page across visits. */
	open?: boolean
	/** A summary, a recap or an extraction was stored: the caller refetches the
	 *  session, which is what carries all three. */
	ongenerated?: () => Promise<void> | void
	/** A generation was queued or deleted: the caller refetches the job list.
	 *  Awaited, so the count on the trigger is right before anything reads it. */
	onvideoschanged?: () => Promise<void> | void
	/** What went wrong. The page owns the banner. */
	onerror?: (message: string) => void
} = $props()

const llmProviders = $derived(actionSetup.providersFor('summarize'))
const noLlm = $derived(
	llmProviders.length === 0 ? 'Add an LLM provider (OpenAI-compatible chat) in Settings' : '',
)
const summarizeLabel = $derived(session.summary ? 'Re-summarize' : 'Summarize')

// Which text the dialog is open for, and whether it is. One kind rather than
// one flag per kind: exactly one of them can be open at a time, and three
// booleans is three ways for two of them to be.
let generateKind = $state<GenerateKind>('summary')
let generateOpen = $state(false)

const recap = $derived(documents.find((d) => d.kind === 'recap'))
const extractionDocument = $derived(documents.find((d) => d.kind === 'extraction'))

/** The stored extraction, parsed, or null.
 *
 * A body that no longer fits the schema reads as "none" rather than throwing:
 * it was written by a model on a day the shape may have been different, and a
 * section that cannot render one old document must not take the summary and
 * the recap down with it. Extracting again replaces it. */
const extraction = $derived.by<SessionExtraction | null>(() => {
	if (!extractionDocument) return null
	try {
		return JSON.parse(extractionDocument.body) as SessionExtraction
	} catch {
		return null
	}
})

/** The extraction as the shared names list takes it: one group per kind, in
 *  the order a GM reads them. `session_ids` is empty because this is one
 *  session's own list - the campaign page is where an entry comes from
 *  several. */
const nameGroups = $derived.by(() => {
	const e = extraction
	if (!e) return []
	const plain = (name: string, kind: string, notes: string): MergedEntity => ({
		name,
		kind,
		notes,
		session_ids: [],
	})
	return [
		{
			label: 'Characters',
			entries: e.characters.map((c) => plain(c.name, c.kind, c.notes)),
		},
		{ label: 'Places', entries: e.places.map((p) => plain(p.name, '', p.notes)) },
		{ label: 'Items', entries: e.items.map((i) => plain(i.name, '', i.notes)) },
		{ label: 'Factions', entries: e.factions.map((f) => plain(f.name, '', f.notes)) },
		{ label: 'Quests', entries: e.quests.map((q) => plain(q.title, q.status, q.notes)) },
		{ label: 'Decisions', entries: e.decisions.map((d) => plain(d, '', '')) },
	]
})

// The overflow menu's reason lines need stable ids to be pointed at, and this
// card is drawn once per page but the component is not a singleton.
const uid = $props.id()
const videoReasonId = `${uid}-video-reason`
const llmReasonId = `${uid}-llm-reason`

/** One line about a stored text: who wrote it, with what, and from which
 *  transcript. One written before the version was recorded says exactly that -
 *  claiming 'original' would be a guess, and on a session with five versions
 *  it is the guess most likely to be wrong. */
function meta(providerId: string | null, model: string | null, readVersion: string | null): string {
	if (!model) return ''
	const from = readVersion ? `from ${versionLabel(readVersion)}` : 'version not recorded'
	return `${providerName(providerId, actionSetup.providers)} · ${model} · ${from}`
}

const summaryMeta = $derived(
	session.summary
		? meta(session.summary_provider, session.summary_model, session.summary_version)
		: '',
)

/** Open a dialog, after warning about an undiarized transcript once.
 *
 * The warning is about the two kinds that read the transcript as prose: with
 * no speakers the model cannot say who did what, which is most of what a
 * summary and a recap are for. */
async function openGenerate(kind: GenerateKind) {
	if (
		kind !== 'extraction' &&
		speakers.length === 0 &&
		!(await confirm('This session has no diarized speakers. Continue anyway?'))
	)
		return
	generateKind = kind
	generateOpen = true
}

/** Run the chosen generation and let the page refetch what it stored. */
async function run(body: GenerateRequest) {
	if (generateKind === 'summary') await api.summarizeSession(sessionId, body)
	else if (generateKind === 'recap') await api.recapSession(sessionId, body)
	else await api.extractSession(sessionId, body)
	await ongenerated?.()
}

let copied = $state(false)

async function copyRecap() {
	if (!recap) return
	try {
		await navigator.clipboard.writeText(recap.body)
		copied = true
		setTimeout(() => (copied = false), 2000)
	} catch {
		// A browser that refuses the clipboard (an insecure origin, a denied
		// permission) is not a failure worth the page's banner: the text is on
		// screen and can be selected.
		onerror?.('The browser would not let the page write to the clipboard.')
	}
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
 * Said once and read twice - by the entry's `disabled` and by the line under
 * it - because the two used to be copies of the same nested ternary, and a
 * reason that disagrees with the state that produced it is worse than no
 * reason at all. */
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

/** What the glyph says it is, spoken rather than drawn. It names nothing on
 *  its own, and the number beside it is the one thing on this header worth
 *  interrupting a reader for, so it is in the label and not only on the face. */
const overflowLabel = $derived(
	videoJobs.length
		? `More actions, ${videoJobs.length} video${videoJobs.length === 1 ? '' : 's'}`
		: 'More actions',
)

/** Delete one generation and its file.
 *
 * Deleting the last one closes the dialog, because the entry that opened it is
 * gone the moment the count reaches zero: leaving an empty dialog behind a
 * control that no longer exists is a dead end you can only escape by hand.
 * Done here rather than in an effect - it is the consequence of a press, not a
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

{#snippet entry(
	label: string,
	icon: typeof Sparkles,
	blocked: string,
	reasonId: string,
	onSelect: () => void,
	detail = '',
)}
	{@const Icon = icon}
	<DropdownMenuItem
		class={['flex-col items-start gap-0.5', blocked && 'data-disabled:opacity-100']}
		disabled={!!blocked}
		aria-describedby={blocked ? reasonId : undefined}
		{onSelect}
	>
		<span class={['flex items-center gap-2', blocked && 'text-muted-foreground']}>
			<Icon />
			{label}
		</span>
		{#if blocked}
			<!-- The reason in text rather than in a `title`, and left at full
			     opacity while the label above it dims: an explanation faded to half
			     is an explanation nobody reads, which is the same failure as not
			     having one. -->
			<span id={reasonId} class="text-xs text-muted-foreground">{blocked}</span>
		{:else if detail}
			<span class="text-xs text-muted-foreground">{detail}</span>
		{/if}
	</DropdownMenuItem>
{/snippet}

<CardContent class={cn('flex flex-col gap-2', open ? 'min-h-0 flex-1' : 'shrink-0')}>
	<Foldable
		title="Summary"
		meta={summaryMeta}
		bind:open
		bodyClass="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto"
	>
		{#snippet actions()}
			<!-- The one control that stays on the header, because summarizing is
			     what this section is for. Its label is hidden below `sm`, where it
			     is one icon beside the menu, and `aria-label` carries the words the
			     eye no longer gets. -->
			<Button
				variant="outline"
				size="sm"
				onclick={() => openGenerate('summary')}
				disabled={!!noLlm}
				aria-label={summarizeLabel}
				title={noLlm || summarizeLabel}
			>
				<Sparkles />
				<span class="hidden sm:inline">{summarizeLabel}</span>
			</Button>
			<DropdownMenu>
				<DropdownMenuTrigger>
					{#snippet child({ props })}
						<Button {...props} variant="outline" size="sm" aria-label={overflowLabel}>
							<Ellipsis />
							{#if videoJobs.length}
								<span>{videoJobs.length}</span>
							{/if}
						</Button>
					{/snippet}
				</DropdownMenuTrigger>
				<!-- Aligned to its own end so it hangs under the right edge of the
				     header rather than off the side of a 320px screen, and as wide as
				     the export menu in the session header above. -->
				<DropdownMenuContent align="end" class="w-60">
					{@render entry(
						recap ? 'Rewrite recap' : 'Write recap',
						FileText,
						noLlm,
						llmReasonId,
						() => openGenerate('recap'),
						'For the players, in their language',
					)}
					{@render entry(
						extraction ? 'Extract names again' : 'Extract names',
						ScanSearch,
						noLlm,
						llmReasonId,
						() => openGenerate('extraction'),
						'Characters, places, quests',
					)}
					{@render entry(
						'Generate video',
						Clapperboard,
						videoBlocked,
						videoReasonId,
						() => (videoOpen = true),
					)}
					{#if videoJobs.length}
						{@render entry(
							`Videos (${videoJobs.length})`,
							Film,
							'',
							`${uid}-videos`,
							() => (videosOpen = true),
							videosStatus,
						)}
					{/if}
				</DropdownMenuContent>
			</DropdownMenu>
		{/snippet}

		<!-- The recap itself is markdown, and so is the summary: every current
		     chat model answers either prompt with headings and bullets, so these
		     render it (see Markdown.svelte) instead of showing the reader the
		     literal `##`. -->
		{#if session.summary}
			<Markdown source={session.summary} />
		{:else if noLlm}
			<p class="m-0 text-muted-foreground">
				Add an LLM provider (OpenAI-compatible chat) in Settings to enable summaries.
			</p>
		{:else}
			<p class="m-0 text-muted-foreground">Not summarized yet.</p>
		{/if}

		{#if recap}
			<div class="flex flex-col gap-1 border-t border-dashed pt-3">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<h3 class="m-0 text-sm font-medium">Recap</h3>
					<span class="flex items-center gap-2">
						<span class="text-xs text-muted-foreground">
							{meta(recap.provider_id, recap.model, recap.version)}
						</span>
						<!-- A recap is written to be pasted into a group chat, which is
						     the one thing "select the text on a phone" is worst at. -->
						<Button variant="ghost" size="sm" onclick={copyRecap} aria-label="Copy the recap">
							<Copy />
							{copied ? 'Copied' : 'Copy'}
						</Button>
					</span>
				</div>
				<Markdown source={recap.body} />
			</div>
		{/if}

		{#if extraction}
			<div class="flex flex-col gap-2 border-t border-dashed pt-3">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<h3 class="m-0 text-sm font-medium">Names</h3>
					<span class="text-xs text-muted-foreground">
						{meta(
							extractionDocument?.provider_id ?? null,
							extractionDocument?.model ?? null,
							extractionDocument?.version ?? null,
						)}
					</span>
				</div>
				<ExtractedNames groups={nameGroups} campaignId={session.campaign_id} />
			</div>
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

<!-- One dialog for all three, told which it is. The provider and model are
     seeded inside it from the stored summarize default, so it carries a pick
     from one run to the next exactly as the summarize dialog it grew out of
     did. -->
<GenerateDialog bind:open={generateOpen} kind={generateKind} {speakers} {version} onrun={run} />
