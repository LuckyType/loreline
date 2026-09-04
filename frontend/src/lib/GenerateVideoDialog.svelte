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
 * 2. The parameter controls are built from the chosen model. Video models
 *    differ in which durations, resolutions and aspect ratios they accept
 *    (some accept no duration at all), and a model handed a parameter it does
 *    not support rejects the whole request - so anything the model does not
 *    list simply is not offered, and is not sent. The vouched-for answer is
 *    the model's video block in capabilities.yaml; the vendor catalogue's own
 *    lists are the fallback for a model that file does not annotate.
 */

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
import type { ActionDefaults, ProviderConfig, VideoJob } from '$lib/types'

let {
	open = $bindable(false),
	sessionId,
	providers,
	summary = '',
	defaults,
	onqueued,
}: {
	open?: boolean
	sessionId: string
	/** Video-capable providers only - the caller filters, since it already has them. */
	providers: ProviderConfig[]
	/** Seeds the prompt on first open. */
	summary?: string
	/** Stored per-action defaults (Settings → Video), used to preselect. */
	defaults?: ActionDefaults
	onqueued?: (job: VideoJob) => void
} = $props()

let providerId = $state('')
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
	const preferred = defaults?.video_model
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

// Re-seed the prompt and pick a provider each time the dialog opens, but never
// while it is open - that would wipe an edit in progress.
$effect(() => {
	if (!open) return
	if (!prompt) prompt = summary
	// The saved default wins, falling back to the first available provider.
	if (!providerId) {
		const preferred = defaults?.video_provider
		providerId =
			(preferred && providers.some((p) => p.id === preferred) ? preferred : '') ||
			providers[0]?.id ||
			''
	}
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
	<DialogContent class="sm:max-w-lg">
		<DialogHeader>
			<DialogTitle>Generate video</DialogTitle>
			<DialogDescription>
				Starts a video generation from the prompt below. It runs in the background for a few minutes
				- you can close this page.
			</DialogDescription>
		</DialogHeader>

		{#if providers.length === 0}
			<p class="text-sm text-muted-foreground">
				Add an OpenRouter provider in Settings to generate video.
			</p>
		{:else}
			<div class="flex flex-col gap-4">
				<div class="flex flex-col gap-2">
					<Label>Provider</Label>
					<Dropdown
						bind:value={providerId}
						options={providers.map((p) => ({ value: p.id, label: p.name }))}
						placeholder="Provider"
					/>
				</div>

				<div class="flex flex-col gap-2">
					<Label>Model</Label>
					<Dropdown
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
						<span class="text-xs text-amber-500">{sunset}</span>
					{/if}
				</div>

				<div class="flex flex-col gap-2">
					<Label for="video-prompt">Prompt</Label>
					<Textarea id="video-prompt" rows={8} bind:value={prompt} />
					<span class="text-xs text-muted-foreground">
						Seeded from the session summary - edit freely before generating.
					</span>
				</div>

				{#if model}
					<div class="grid grid-cols-2 gap-3">
						{#if durations.length}
							<div class="flex flex-col gap-2">
								<Label>Length</Label>
								<Dropdown
									value={duration === null ? '' : String(duration)}
									onpick={(v) => (duration = v ? Number(v) : null)}
									options={durations.map((d) => ({ value: String(d), label: `${d}s` }))}
								/>
							</div>
						{/if}
						{#if resolutions.length}
							<div class="flex flex-col gap-2">
								<Label>Resolution</Label>
								<Dropdown
									bind:value={resolution}
									options={resolutions.map((r) => ({ value: r, label: r }))}
								/>
							</div>
						{/if}
						{#if aspectRatios.length}
							<div class="flex flex-col gap-2">
								<Label>Aspect ratio</Label>
								<Dropdown
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

		{#if error}
			<p class="mt-2 text-sm text-destructive">{error}</p>
		{/if}

		<DialogFooter>
			<Button variant="outline" onclick={() => (open = false)}>Cancel</Button>
			<Button onclick={submit} disabled={busy || !providerId || !modelId || !prompt.trim()}>
				{busy ? 'Starting…' : 'Generate'}
			</Button>
		</DialogFooter>
	</DialogContent>
</Dialog>
