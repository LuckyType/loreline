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
 *    list simply is not offered, and is not sent.
 */

import { ApiError, api } from '$lib/api'
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
import { Textarea } from '$lib/components/ui/textarea'
import type { ActionDefaults, ProviderConfig, VideoJob, VideoModelInfo } from '$lib/types'

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
let modelId = $state('')
let prompt = $state('')
let duration = $state<number | null>(null)
let resolution = $state('')
let aspectRatio = $state('')
let generateAudio = $state(false)
let models = $state<VideoModelInfo[]>([])
let modelsFor = $state('')
let loadingModels = $state(false)
let busy = $state(false)
let error = $state('')

const model = $derived(models.find((m) => m.id === modelId))
const durations = $derived(model?.supported_durations ?? [])
const resolutions = $derived(model?.supported_resolutions ?? [])
const aspectRatios = $derived(model?.supported_aspect_ratios ?? [])

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

$effect(() => {
	const pid = providerId
	if (!open || !pid || modelsFor === pid || loadingModels) return
	loadingModels = true
	api
		.videoModels(pid)
		.then((loaded) => {
			models = loaded
			modelsFor = pid
			if (!loaded.some((m) => m.id === modelId)) {
				const preferred = defaults?.video_model
				modelId =
					(preferred && loaded.some((m) => m.id === preferred) ? preferred : '') ||
					loaded[0]?.id ||
					''
			}
		})
		.catch(() => {
			models = []
			modelsFor = pid
		})
		.finally(() => {
			loadingModels = false
		})
})

// Whenever the model changes, drop any parameter it does not offer and fall
// back to its own first supported value. Carrying "1080p" over to a model that
// only does 720p would fail the request at submit time.
$effect(() => {
	const m = model
	if (!m) return
	if (duration !== null && !(m.supported_durations ?? []).includes(duration)) duration = null
	if (duration === null && m.supported_durations?.length) duration = m.supported_durations[0]
	if (resolution && !(m.supported_resolutions ?? []).includes(resolution)) resolution = ''
	if (!resolution && m.supported_resolutions?.length) resolution = m.supported_resolutions[0]
	if (aspectRatio && !(m.supported_aspect_ratios ?? []).includes(aspectRatio)) aspectRatio = ''
	if (!aspectRatio && m.supported_aspect_ratios?.length) aspectRatio = m.supported_aspect_ratios[0]
	if (!m.generate_audio) generateAudio = false
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
						options={models.map((m) => ({ value: m.id, label: m.name || m.id }))}
						placeholder={loadingModels ? 'Loading models…' : 'Select model…'}
					/>
					{#if !loadingModels && models.length === 0 && modelsFor}
						<span class="text-xs text-muted-foreground">
							No video models available - check the provider's API key.
						</span>
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
					{#if model.generate_audio}
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
