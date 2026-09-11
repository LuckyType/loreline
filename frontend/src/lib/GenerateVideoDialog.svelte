<script lang="ts">
/**
 * "Generate video" - turns a session summary into a video prompt and submits
 * it to an OpenRouter video model.
 *
 * Two things shape this dialog:
 *
 * 1. The prompt is *seeded* from the summary, never bound to it. The GM edits
 *    what actually gets sent, and a summary is a recap, not a shot
 *    description - it almost always wants trimming before it is a good prompt.
 *    Which is why the box carries a character count and a Reset button: an
 *    eight-hour session's recap seeds several thousand characters, and there
 *    is no way to put it back once it has been cut about.
 * 2. The parameter controls are built from the chosen model. Video models
 *    differ in which durations, resolutions and aspect ratios they accept
 *    (some accept no duration at all), and a model handed a parameter it does
 *    not support rejects the whole request - so anything the model does not
 *    list simply is not offered, and is not sent. The vouched-for answer is
 *    the model's video block in capabilities.yaml; the vendor catalogue's own
 *    lists are the fallback for a model that file does not annotate.
 */

import { untrack } from 'svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
import { ApiError, api } from '$lib/api'
import { deprecationNote, videoCapsFor } from '$lib/capabilities.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import { Button } from '$lib/components/ui/button'
import { Checkbox } from '$lib/components/ui/checkbox'
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '$lib/components/ui/dialog'
import { Label } from '$lib/components/ui/label'
import { videoCatalog } from '$lib/modelCatalog.svelte'
import { Textarea } from '$lib/components/ui/textarea'
import type { VideoJob } from '$lib/wire'

let {
	open = $bindable(false),
	sessionId,
	summary = '',
	onqueued,
}: {
	open?: boolean
	sessionId: string
	/** Seeds the prompt on first open. */
	summary?: string
	onqueued?: (job: VideoJob) => void
} = $props()

// Video-capable rows only, from the shared setup store the page has loaded.
const providers = $derived(actionSetup.providersFor('video'))
// Seeded, not stored: the saved default (Settings, Video) while it still
// generates video, else the first row that does. A pick overrides it.
let providerId = $derived(actionSetup.preferredProvider('video')?.id ?? '')
let prompt = $state('')
let duration = $state<number | null>(null)
let resolution = $state('')
let aspectRatio = $state('')
let generateAudio = $state(false)
let busy = $state(false)
let error = $state('')

const provider = $derived(providers.find((p) => p.id === providerId))
const providerKind = $derived(provider?.kind)
// A view over the shared video catalogue, which has already dropped hidden
// models (a hidden model is one whose connector is unverified).
const offered = $derived(provider ? videoCatalog.list(provider, 'video', '') : [])
const loadingModels = $derived(provider ? videoCatalog.loading(provider, 'video', '') : false)
const modelsSettled = $derived(provider ? videoCatalog.settled(provider, 'video', '') : false)
// Seeded, not stored: the saved default when this provider lists it, else the
// first model offered. A pick overrides this until the list changes (a
// provider switch, or the list arriving), the same rule the other pickers
// get from preferredModel, minus favourites, which video rows do not carry.
let modelId = $derived.by(() => {
	const preferred = actionSetup.defaults.video_model
	if (preferred && offered.some((m) => m.id === preferred)) return preferred
	return offered[0]?.id ?? ''
})
const model = $derived(offered.find((m) => m.id === modelId))
const caps = $derived(videoCapsFor(providerKind, modelId))
const durations = $derived(
	caps?.durations.length ? caps.durations : (model?.supported_durations ?? []),
)
const resolutions = $derived(
	caps?.resolutions.length ? caps.resolutions : (model?.supported_resolutions ?? []),
)
const aspectRatios = $derived(
	caps?.aspect_ratios.length ? caps.aspect_ratios : (model?.supported_aspect_ratios ?? []),
)
// `audio: null` in the config means the vendor publishes no answer, which is
// not the same as "no audio" - fall back to the catalogue rather than promise
// silence.
const audioOffered = $derived(caps?.audio ?? model?.generate_audio === true)
const sunset = $derived(deprecationNote(providerKind, modelId))

// Long enough that a video model is being handed a chapter rather than a shot.
// Not a vendor limit (promptMax below is one, when the config knows one) -
// just the point past which the dialog's own advice is worth repeating.
const PROMPT_LONG_CHARS = 1200

// What this model will accept, when anyone has published a number. Today every
// video entry in capabilities.yaml sets this to null on purpose - OpenRouter
// documents no prompt length limit for any of them - so in practice the count
// below is a plain count and the note is the guidance. The moment a vendor
// does publish one, the config carries it and the warning appears with it,
// which beats learning the limit from a failed background job.
const promptMax = $derived(caps?.prompt_max_chars ?? null)
const promptOverMax = $derived(promptMax !== null && prompt.length > promptMax)
const promptLong = $derived(promptMax === null && prompt.length > PROMPT_LONG_CHARS)
/** Whether the box still holds exactly what the summary seeded, which is when
 *  there is nothing for Reset to put back. */
const promptIsSummary = $derived(prompt === summary)

// Seed the prompt when the dialog opens, and only then.
//
// Two ways to get this wrong, and the obvious spelling hits both. Reading
// `prompt` inside the effect makes emptying the box refill it from under the
// cursor, because clearing it re-runs the very effect that seeds it: select
// all, delete, and the whole recap is back before the first keystroke, which
// is exactly the gesture someone writing their own prompt starts with.
// Testing the raw string rather than the trimmed one leaves a box holding a
// single space seeded forever, with Generate disabled on `!prompt.trim()` and
// no way back to the summary short of reloading the page.
//
// So: fire on the transition into open, read the prompt untracked, and treat
// blank-after-trimming as unseeded. Emptiness while open is a state the GM is
// passing through, not a request for the recap back; "Reset to summary" below
// is the deliberate way to ask for that.
let wasOpen = false
$effect(() => {
	const isOpen = open
	if (isOpen && !wasOpen) {
		untrack(() => {
			if (!prompt.trim()) prompt = summary
		})
	}
	wasOpen = isOpen
})

// The list is wanted the moment the dialog shows, not when the model dropdown
// opens: the parameter controls below are built from the chosen model.
$effect(() => {
	if (open && provider) videoCatalog.load(provider, 'video', '')
})

// Whenever the model changes, drop any parameter it does not offer and fall
// back to its own first supported value. Carrying "1080p" over to a model that
// only does 720p would fail the request at submit time.
$effect(() => {
	if (!model) return
	if (duration !== null && !durations.includes(duration)) duration = null
	if (duration === null && durations.length) duration = durations[0]
	if (resolution && !resolutions.includes(resolution)) resolution = ''
	if (!resolution && resolutions.length) resolution = resolutions[0]
	if (aspectRatio && !aspectRatios.includes(aspectRatio)) aspectRatio = ''
	if (!aspectRatio && aspectRatios.length) aspectRatio = aspectRatios[0]
	if (!audioOffered) generateAudio = false
})

async function submit() {
	error = ''
	busy = true
	try {
		const job = await api.enqueueVideo({
			session_id: sessionId,
			provider_id: providerId,
			model: modelId,
			prompt,
			// Only ever send what this model actually supports.
			duration: durations.length ? duration : null,
			resolution: resolutions.length ? resolution || null : null,
			aspect_ratio: aspectRatios.length ? aspectRatio || null : null,
			generate_audio: generateAudio,
		})
		onqueued?.(job)
		open = false
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'could not start generation'
	} finally {
		busy = false
	}
}
</script>

<Dialog bind:open>
	<DialogContent class="max-h-[calc(100dvh-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-2xl">
		<DialogHeader>
			<DialogTitle>Generate video</DialogTitle>
			<DialogDescription>
				Starts a video generation from the prompt below. It runs in the background for a few minutes
				- you can close this page.
			</DialogDescription>
		</DialogHeader>

		<!-- Header and footer stay put, the middle scrolls. The prompt is seeded from
		     the session summary, which for a long session runs to several thousand
		     characters, and with nothing capping it the dialog simply grew past the
		     window until Generate was off screen below it. Capping the whole dialog
		     at the viewport and giving this one region the only overflow keeps the
		     title and both buttons reachable at any window height. The sliver of
		     horizontal slack is so a focus ring on a dropdown is not shaved off by
		     the edge of the scroll container. -->
		<div class="-mx-1 min-h-0 overflow-y-auto px-1">
			{#if providers.length === 0}
				<p class="text-sm text-muted-foreground">
					Add an OpenRouter provider in Settings to generate video.
				</p>
			{:else}
				<div class="flex flex-col gap-4">
					<div class="flex flex-col gap-2">
						<Label for="video-provider">Provider</Label>
						<Dropdown
							id="video-provider"
							bind:value={providerId}
							options={providers.map((p) => ({ value: p.id, label: p.name }))}
							placeholder="Provider"
						/>
					</div>

					<div class="flex flex-col gap-2">
						<Label for="video-model">Model</Label>
						<Dropdown
							id="video-model"
							bind:value={modelId}
							loading={loadingModels}
							filterable
							options={offered.map((m) => ({ value: m.id, label: m.name || m.id }))}
							placeholder={loadingModels ? 'Loading models…' : 'Select model…'}
						/>
						{#if !loadingModels && offered.length === 0 && modelsSettled}
							<span class="text-xs text-muted-foreground">
								No video models available - check the provider's API key.
							</span>
						{/if}
						{#if sunset}
							<span class="text-xs text-amber-700 dark:text-amber-500">{sunset}</span>
						{/if}
					</div>

					<div class="flex flex-col gap-2">
						<div class="flex items-center justify-between gap-2">
							<Label for="video-prompt">Prompt</Label>
							<!-- The one way back to the seed. Re-opening only re-seeds an empty
							     box, by design: it must not wipe an edit in progress. -->
							<Button
								variant="ghost"
								size="sm"
								onclick={() => (prompt = summary)}
								disabled={!summary || promptIsSummary}
								title={summary
									? 'Put the session summary back in the box, discarding your edits'
									: 'This session has no summary to reset to'}
							>
								Reset to summary
							</Button>
						</div>
						<!-- The base textarea sizes itself to its content, so a recap-length
						     seed would take whatever height the text wants and drag the dialog
						     up with it. The cap turns that growth into the textarea's own
						     scrollbar. The floor is there because content sizing ignores the
						     rows attribute, which would leave an empty box a couple of lines
						     tall; rows still governs in engines that do not do content sizing. -->
						<Textarea id="video-prompt" rows={8} class="max-h-64 min-h-40" bind:value={prompt} />
						<div class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
							<span class="text-xs text-muted-foreground">
								Seeded from the session summary - a short, shot-like description of one scene
								generates better than a whole recap.
							</span>
							<span
								class="shrink-0 text-xs {promptOverMax
									? 'text-destructive'
									: promptLong
										? 'text-amber-700 dark:text-amber-500'
										: 'text-muted-foreground'}"
							>
								{prompt.length}{promptMax === null ? '' : ` / ${promptMax}`}
								characters
							</span>
						</div>
						{#if promptOverMax}
							<span class="text-xs text-destructive">
								Longer than this model accepts ({promptMax}
								characters) - it will be rejected.
							</span>
						{:else if promptLong}
							<span class="text-xs text-amber-700 dark:text-amber-500">
								That is a whole recap. Video models take a scene, not a chapter, and some cap the
								prompt well below this length.
							</span>
						{/if}
					</div>

					{#if model}
						<div class="grid grid-cols-2 gap-3">
							{#if durations.length}
								<div class="flex flex-col gap-2">
									<Label for="video-duration">Length</Label>
									<Dropdown
										id="video-duration"
										value={duration === null ? '' : String(duration)}
										onpick={(v) => (duration = v ? Number(v) : null)}
										options={durations.map((d) => ({ value: String(d), label: `${d}s` }))}
									/>
								</div>
							{/if}
							{#if resolutions.length}
								<div class="flex flex-col gap-2">
									<Label for="video-resolution">Resolution</Label>
									<Dropdown
										id="video-resolution"
										bind:value={resolution}
										options={resolutions.map((r) => ({ value: r, label: r }))}
									/>
								</div>
							{/if}
							{#if aspectRatios.length}
								<div class="flex flex-col gap-2">
									<Label for="video-aspect-ratio">Aspect ratio</Label>
									<Dropdown
										id="video-aspect-ratio"
										bind:value={aspectRatio}
										options={aspectRatios.map((r) => ({ value: r, label: r }))}
									/>
								</div>
							{/if}
						</div>
						{#if audioOffered}
							<label class="flex items-center gap-2">
								<Checkbox
									checked={generateAudio}
									onCheckedChange={(v) => (generateAudio = v === true)}
								/>
								<span class="text-sm">Generate audio</span>
							</label>
						{/if}
					{/if}
				</div>
			{/if}
		</div>

		<!-- The error rides with the footer rather than sitting in the scroll region
		     above: a submission that failed has to be readable next to the button
		     that failed, not somewhere up the prompt's own scrollback. -->
		<div class="flex flex-col gap-4">
			{#if error}
				<p class="text-sm text-destructive">{error}</p>
			{/if}
			<DialogFooter>
				<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
				<Button onclick={submit} disabled={busy || !providerId || !modelId || !prompt.trim()}>
					{busy ? 'Starting…' : 'Generate'}
				</Button>
			</DialogFooter>
		</div>
	</DialogContent>
</Dialog>
