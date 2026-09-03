<script lang="ts">
import {
	AlignLeft,
	Clapperboard,
	Filter,
	Cloud,
	Mic,
	Pencil,
	Plus,
	Server,
	Trash2,
	Users,
} from '@lucide/svelte'
import { onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Badge } from '$lib/components/ui/badge'
import { Button } from '$lib/components/ui/button'
import {
	Card,
	CardAction,
	CardContent,
	CardDescription,
	CardHeader,
	CardTitle,
} from '$lib/components/ui/card'
import { Checkbox } from '$lib/components/ui/checkbox'
import { Switch } from '$lib/components/ui/switch'
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
import { Textarea } from '$lib/components/ui/textarea'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import { hintFor, priceTitle } from '$lib/modelInfo'
import ModelPicker from '$lib/ModelPicker.svelte'
import {
	capabilities,
	deprecationFor,
	inlineDiarizationFor,
	interactionsFor,
	isHiddenModel,
	reasoningEffortsFor,
	requiresBaseUrl,
} from '$lib/capabilities.svelte'
import {
	providersFor,
	type ActionDefaults,
	type AuthKind,
	type Hosting,
	type ProtocolKind,
	capabilityBadges,
	type HealthStatus,
	type ModelInfo,
	type OpenRouterRouting,
	type ProviderConfig,
	type ProviderCreate,
	type ProviderKind,
} from '$lib/types'

/**
 * Copy the capability config does not carry, and only that: which transport
 * this kind's connector speaks, what its base-URL box should suggest, and the
 * one-line pitch in the wizard's list. Label, hosting, key URL and base URL
 * come from /api/capabilities - keeping a second copy of those here is what
 * let the two drift apart in the first place.
 */
interface ProviderPresentation {
	protocol: ProtocolKind
	/** Only for a kind the operator has to point somewhere. */
	baseUrlPlaceholder?: string
	note: string
}

// Also the wizard's running order, which stays put whether or not the config
// loaded.
const PRESENTATION: Record<ProviderKind, ProviderPresentation> = {
	deepgram: { protocol: 'ws', note: 'Streaming WS · inline diarization.' },
	assemblyai: { protocol: 'ws', note: 'Streaming WS · inline diarization.' },
	openai: { protocol: 'ws', note: 'Realtime transcription and session summaries.' },
	gemini: { protocol: 'http_batch', note: 'API key · diarization · word timestamps.' },
	openrouter: {
		protocol: 'http_batch',
		note: 'One key for many vendors. Transcription, summaries and video.',
	},
	openai_compat: {
		protocol: 'http_batch',
		baseUrlPlaceholder: 'http://localhost:8000/v1',
		note: 'Speaches, whisper.cpp, Ollama, LM Studio, vLLM. Transcription and/or summaries.',
	},
}

/** One row of the wizard's provider list: the served facts, joined to the copy. */
interface ProviderChoice {
	kind: ProviderKind
	label: string
	/** Null when the config never loaded - such a row shows under both hosting
	 *  steps rather than disappearing from the wizard entirely. */
	hosting: Hosting | null
	auth: AuthKind
	keyUrl: string | null
	protocol: ProtocolKind
	baseUrlPlaceholder: string | null
	note: string
}

const catalog = $derived(
	(Object.keys(PRESENTATION) as ProviderKind[]).map((kind): ProviderChoice => {
		const spec = capabilities.provider(kind)
		const copy = PRESENTATION[kind]
		// A surface the config leaves without an address is one only the operator can supply.
		const needsBaseUrl = spec ? requiresBaseUrl(spec) : !!copy.baseUrlPlaceholder
		return {
			kind,
			label: spec?.label ?? kind,
			hosting: spec?.hosting ?? null,
			auth: spec?.auth ?? 'optional',
			keyUrl: spec?.key_url ?? null,
			protocol: copy.protocol,
			baseUrlPlaceholder: needsBaseUrl ? (copy.baseUrlPlaceholder ?? '') : null,
			note: copy.note,
		}
	}),
)

/** OpenRouter's own defaults - see OpenRouterRouting in $lib/types. The
 *  backend drops any field still sitting here, so this is also "send nothing". */
const blankRouting = (): OpenRouterRouting => ({
	sort: null,
	data_collection: 'allow',
	zdr: false,
})

const blank = (): ProviderCreate => ({
	name: '',
	kind: 'openai_compat',
	protocol: 'http_batch',
	base_url: '',
	favorite_models: [],
	sample_rate: 16000,
	language: 'de',
	routing: blankRouting(),
	enabled: true,
	api_key: '',
})

let providers = $state<ProviderConfig[]>([])
let editing = $state<string | null>(null)
let message = $state('')
/**
 * Per-provider outcome of the Test button.
 *
 * `testing` is the only client-side state; the rest are HealthStatus straight
 * off the wire, so the page never re-derives a verdict the backend already
 * graded. `detail` is the vendor's own message ("API key not valid.") and is
 * shown as the badge's tooltip: it is what turns "something is wrong" into
 * "fix this field".
 */
type TestState = { status: HealthStatus | 'testing'; detail?: string | null }

let testResults = $state<Record<string, TestState>>({})

/**
 * Badge styling per state. Two failures, and they need opposite fixes: a
 * rejected key is not a wrong base URL, and rendering both as "down" is what
 * this table used to do. `degraded` is amber rather than red because the
 * credential is provably fine, and `unknown` stays grey because an
 * inconclusive probe must not read as a broken provider.
 */
const TEST_BADGE: Record<
	TestState['status'],
	{ label: string; variant: 'secondary' | 'destructive' | 'outline'; dot: string }
> = {
	testing: { label: 'testing…', variant: 'secondary', dot: 'bg-amber-500' },
	healthy: { label: 'healthy', variant: 'secondary', dot: 'bg-emerald-500' },
	degraded: { label: 'degraded', variant: 'secondary', dot: 'bg-amber-500' },
	unauthorized: { label: 'auth failed', variant: 'destructive', dot: 'bg-red-500' },
	unreachable: { label: 'unreachable', variant: 'destructive', dot: 'bg-red-500' },
	unknown: { label: 'unknown', variant: 'outline', dot: 'bg-muted-foreground' },
}
let form = $state<ProviderCreate>(blank())
let availableModels = $state<ModelInfo[]>([])
let modelsLoading = $state(false)
let modelFilter = $state('')
let step = $state(1)
let hosting = $state<Hosting>('cloud')
let selectedKind = $state<ProviderKind | null>(null)
let wizardOpen = $state(false)
// Derived rather than captured, so a wizard opened before /api/capabilities
// answered picks up the real label and key link the moment it arrives.
const selected = $derived(catalog.find((c) => c.kind === selectedKind))

const blankDefaults = (): ActionDefaults => ({
	stt_provider: '',
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_provider: '',
	summarize_model: '',
	summarize_prompt: '',
	summarize_reasoning_effort: '',
	video_provider: '',
	video_model: '',
	strict_model_filtering: true,
})
let defaults = $state<ActionDefaults>(blankDefaults())
// Last persisted state - drives the "default" tags in the pickers, so they
// reflect what is saved, not the (possibly unsaved) current selection.
let savedDefaults = $state<ActionDefaults>(blankDefaults())
let defaultsMsg = $state('')
// This default is pre-selected when *starting or re-processing* a session (the
// card below says so), and re-processing replays stored audio - the
// live-capture exclusion has no business narrowing it. Offering only
// live-capable providers here made the batch/re-process-only ones (OpenRouter)
// impossible to set as the transcription default at all. The live pickers on
// the dashboard apply the live-capture rule themselves, and ignore a stored
// default that cannot drive a capture.
const sttProviders = $derived(providersFor(providers, 'transcribe'))
const llmProviders = $derived(providersFor(providers, 'summarize'))
const videoProviders = $derived(providersFor(providers, 'video'))
const sttSrcProvider = $derived(providers.find((p) => p.id === defaults.stt_provider))
const llmSrcProvider = $derived(providers.find((p) => p.id === defaults.summarize_provider))
const videoSrcProvider = $derived(providers.find((p) => p.id === defaults.video_provider))
// The name field's placeholder is a real default, not a hint: leaving it blank
// names the provider after its type ("OpenRouter"), which is what most people
// want for their first one. Save stays enabled accordingly.
const effectiveName = $derived(form.name.trim() || selected?.label || '')
const wizardChoices = $derived(catalog.filter((c) => c.hosting === null || c.hosting === hosting))
// 'optional' is a self-hosted endpoint that may or may not check a key.
const apiKeyLabel = $derived(
	`${selected?.auth === 'optional' ? 'API key (optional)' : 'API key'}${
		editing ? ' - blank = keep current' : ''
	}`,
)
// Set by the summary model picker when the chosen model advertises reasoning.
let llmModelInfo = $state<ModelInfo | undefined>(undefined)
// Set by the STT model picker: inline diarization only yields speakers for
// some provider+model pairs, so the default must not be settable otherwise.
let sttModelInfo = $state<ModelInfo | undefined>(undefined)
const inlineDiarizationAvailable = $derived(
	inlineDiarizationFor(sttSrcProvider?.kind, defaults.stt_model, sttModelInfo?.inline_diarization),
)
// The effort levels this model accepts, in config order; empty means offer no
// dropdown at all rather than an empty one.
const llmEfforts = $derived(
	reasoningEffortsFor(
		llmSrcProvider?.kind,
		defaults.summarize_model,
		llmModelInfo?.supports_reasoning,
	),
)

// A level saved against another model, that this one does not accept, would
// fail every summary started from the default. Only ever narrowed against a
// list we actually have: an empty one means "no levels known", and clearing a
// saved setting on that basis would lose it for nothing.
$effect(() => {
	if (
		llmEfforts.length &&
		defaults.summarize_reasoning_effort &&
		!llmEfforts.includes(defaults.summarize_reasoning_effort)
	) {
		defaults.summarize_reasoning_effort = ''
	}
})

// Never leave a stored "inline" default pointing at a model that cannot serve
// it - the session-start guard would reject every session using it.
$effect(() => {
	if (defaults.diar_mode === 'inline' && sttModelInfo && !inlineDiarizationAvailable) {
		defaults.diar_mode = 'none'
	}
})

// Hidden models are held back from every picker, favourites included: the
// flag is the release gate for a connector nobody has verified yet.
const offeredModels = $derived(availableModels.filter((m) => !isHiddenModel(form.kind, m.id)))
const filteredModels = $derived(
	modelFilter
		? offeredModels.filter((m) => m.id.toLowerCase().includes(modelFilter.toLowerCase()))
		: offeredModels,
)

$effect(() => {
	// Pre-fill empty pickers so the model list is browsable; nothing is
	// persisted until the explicit Save.
	if (!defaults.stt_provider && sttProviders.length) defaults.stt_provider = sttProviders[0].id
	if (!defaults.summarize_provider && llmProviders.length) {
		defaults.summarize_provider = llmProviders[0].id
	}
	if (!defaults.video_provider && videoProviders.length) {
		defaults.video_provider = videoProviders[0].id
	}
})

async function load() {
	providers = await api.listProviders()
}

async function loadDefaults() {
	try {
		defaults = await api.getDefaults()
		// Legacy stored "" and an explicit "none" mean the same thing (no
		// diarization for new sessions) - show one spelling, not two options.
		if (!defaults.diar_mode) defaults.diar_mode = 'none'
		savedDefaults = { ...defaults }
	} catch {
		/* keep blanks */
	}
}

async function saveDefaults() {
	try {
		defaults = await api.setDefaults(defaults)
		if (!defaults.diar_mode) defaults.diar_mode = 'none'
		savedDefaults = { ...defaults }
		defaultsMsg = 'Saved'
		setTimeout(() => (defaultsMsg = ''), 2500)
	} catch (err) {
		defaultsMsg = err instanceof ApiError ? err.message : 'save failed'
	}
}

async function loadModels() {
	modelsLoading = true
	try {
		// Favourites are one flat list shared by every picker, while a provider
		// can now serve several interactions at once (an OpenRouter entry does
		// transcription, summaries and video). Loading only one interaction's
		// catalogue is what made this button look broken: adding OpenRouter for
		// summaries offered the 19 transcription models and none of the chat
		// ones. Load each interaction the kind actually supports and merge,
		// de-duplicated, so a favourite can be picked for any of its roles.
		const catalogues = await Promise.all(
			interactionsFor(form.kind).map((interaction) =>
				api
					.providerModels({
						kind: form.kind,
						interaction,
						base_url: form.base_url || null,
						api_key: form.api_key || null,
						provider_id: editing,
					})
					// One unreachable catalogue must not lose the others.
					.catch(() => []),
			),
		)
		const seen = new Set<string>()
		// Hidden models are dropped here as well as in the list below, so one can
		// never survive as a stored favourite either.
		availableModels = catalogues
			.flat()
			.filter((m) => !seen.has(m.id) && seen.add(m.id) && !isHiddenModel(form.kind, m.id))
		if (availableModels.length) {
			const present = new Set(availableModels.map((m) => m.id))
			form.favorite_models = (form.favorite_models ?? []).filter((m) => present.has(m))
		}
	} catch {
		availableModels = []
	} finally {
		modelsLoading = false
	}
}

function toggleFavorite(model: string) {
	const favs = form.favorite_models ?? []
	form.favorite_models = favs.includes(model) ? favs.filter((m) => m !== model) : [...favs, model]
}

async function save() {
	message = ''
	try {
		const body: ProviderCreate = {
			...form,
			name: effectiveName,
			base_url: form.base_url || null,
			// Routing is an OpenRouter body extension - never store it on the
			// seven STT kinds or on a plain OpenAI-compatible endpoint.
			routing: form.kind === 'openrouter' ? form.routing : null,
			api_key: form.api_key || null,
		}
		if (editing) await api.updateProvider(editing, body)
		else await api.createProvider(body)
		resetWizard()
		await load()
	} catch (err) {
		message = err instanceof ApiError ? err.message : 'save failed'
	}
}

function openWizard() {
	editing = null
	selectedKind = null
	form = blank()
	availableModels = []
	modelFilter = ''
	step = 1
	wizardOpen = true
}

function pickHosting(h: Hosting) {
	hosting = h
	step = 2
}

function pickProvider(meta: ProviderChoice) {
	selectedKind = meta.kind
	// No model is seeded because a provider row no longer holds one: it serves
	// several interactions at once, so any single seed would be the wrong
	// answer for every picker but one. Each interaction-scoped picker chooses
	// per request instead, and capabilities.yaml carries the one default a
	// connector still needs when nobody chose (the health probe).
	form = { ...blank(), kind: meta.kind, protocol: meta.protocol }
	step = 3
}

function resetWizard() {
	editing = null
	selectedKind = null
	form = blank()
	step = 1
	wizardOpen = false
}

function edit(p: ProviderConfig) {
	editing = p.id
	selectedKind = p.kind
	form = {
		name: p.name,
		kind: p.kind,
		protocol: p.protocol,
		base_url: p.base_url ?? '',
		favorite_models: [...p.favorite_models],
		sample_rate: p.sample_rate,
		language: p.language,
		// A provider saved before routing existed (or any STT kind) has none.
		routing: p.routing ? { ...p.routing } : blankRouting(),
		enabled: p.enabled,
		api_key: '',
	}
	availableModels = []
	modelFilter = ''
	step = 3
	wizardOpen = true
}

async function testOne(id: string) {
	testResults = { ...testResults, [id]: { status: 'testing' } }
	try {
		const r = await api.testProvider(id)
		testResults = { ...testResults, [id]: { status: r.status, detail: r.detail } }
	} catch (e) {
		// Our own API did not answer, which says nothing about the provider.
		// Reporting it as unreachable would blame the wrong endpoint.
		testResults = {
			...testResults,
			[id]: { status: 'unknown', detail: e instanceof Error ? e.message : null },
		}
	}
}

async function testAll() {
	await Promise.all(providers.map((p) => testOne(p.id)))
}

async function remove(id: string) {
	if (
		!(await confirm({
			description: 'Delete this provider? This also removes its stored key.',
			destructive: true,
		}))
	)
		return
	await api.deleteProvider(id)
	if (editing === id) resetWizard()
	await load()
}

onMount(async () => {
	await load()
	await loadDefaults()
})
</script>

{#if message}
	<p class="mb-4 text-sm text-muted-foreground">{message}</p>
{/if}

<Card>
	<CardHeader>
		<CardTitle>Providers</CardTitle>
		<CardDescription>
			Speech-to-text and LLM endpoints for transcription, diarization and summaries.
		</CardDescription>
		<CardAction class="flex gap-1">
			<Button variant="outline" size="sm" onclick={testAll} disabled={providers.length === 0}>
				Test all
			</Button>
			<Button
				variant="outline"
				size="icon-sm"
				title="Add provider"
				aria-label="Add provider"
				onclick={openWizard}
			>
				<Plus />
			</Button>
		</CardAction>
	</CardHeader>
	<CardContent class="pt-0">
		<Table>
			<TableHeader>
				<TableRow>
					<TableHead>Name</TableHead><TableHead>Supports</TableHead><TableHead>Endpoint</TableHead
					><TableHead>API key</TableHead><TableHead>Status</TableHead><TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each providers as p (p.id)}
					<TableRow>
						<TableCell>{p.name}</TableCell>
						<TableCell>
							<span class="flex flex-wrap gap-1">
								{#each capabilityBadges(p) as badge (badge)}
									<Badge variant="secondary">{badge}</Badge>
								{/each}
							</span>
						</TableCell>
						<TableCell class="text-muted-foreground">{p.base_url ?? 'default'}</TableCell>
						<TableCell
							><code class="text-muted-foreground">{p.secret_hint ?? '- none -'}</code></TableCell
						>
						<TableCell>
							{@const result = testResults[p.id]}
							{#if result}
								{@const badge = TEST_BADGE[result.status]}
								<!-- The tooltip carries the vendor's own message, which is the
								     difference between "unauthorized" and "API key not valid." -->
								<Badge variant={badge.variant} class="gap-1.5" title={result.detail ?? undefined}
									><span class="size-2 rounded-full {badge.dot}"></span>{badge.label}</Badge
								>
							{:else}
								<Badge variant="outline" class="gap-1.5" title="never tested"
									><span class="size-2 rounded-full bg-muted-foreground"></span>not tested</Badge
								>
							{/if}
						</TableCell>
						<TableCell>
							<div class="flex gap-1">
								<Button variant="outline" size="sm" onclick={() => testOne(p.id)}>Test</Button>
								<Button
									variant="ghost"
									size="icon-sm"
									title="Edit"
									aria-label="Edit"
									onclick={() => edit(p)}
								>
									<Pencil />
								</Button>
								<Button
									variant="ghost"
									size="icon-sm"
									title="Delete"
									aria-label="Delete"
									onclick={() => remove(p.id)}
								>
									<Trash2 />
								</Button>
							</div>
						</TableCell>
					</TableRow>
				{/each}
				{#if providers.length === 0}
					<TableRow>
						<TableCell colspan={6} class="text-muted-foreground"
							>No providers yet - click + to add one.</TableCell
						>
					</TableRow>
				{/if}
			</TableBody>
		</Table>
	</CardContent>
</Card>

<Card class="mt-4">
	<CardHeader>
		<CardTitle>Defaults</CardTitle>
		<CardDescription>Pre-selected when starting or re-processing a session.</CardDescription>
	</CardHeader>
	<CardContent class="flex flex-col gap-4">
		<div class="grid gap-3 md:grid-cols-3">
			<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
				<div class="flex items-center gap-2 font-medium">
					<Mic class="size-4" />
					Transcription
				</div>
				<Label class="text-xs text-muted-foreground" for="def-stt-src">Provider</Label>
				<Dropdown
					id="def-stt-src"
					bind:value={defaults.stt_provider}
					defaultValue={savedDefaults.stt_provider ?? ''}
					options={sttProviders.map((p) => ({ value: p.id, label: p.name }))}
				/>
				<Label class="text-xs text-muted-foreground" for="def-stt-model">Model</Label>
				<ModelPicker
					id="def-stt-model"
					refreshToken={defaults.strict_model_filtering}
					interaction="transcribe"
					provider={sttSrcProvider}
					onselect={(m) => (sttModelInfo = m)}
					bind:value={defaults.stt_model}
					defaultModel={savedDefaults.stt_model}
					defaultProvider={savedDefaults.stt_provider ?? ''}
					autoseed={false}
				/>
			</div>

			<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
				<div class="flex items-center gap-2 font-medium">
					<Users class="size-4" />
					Diarization
				</div>
				<Label class="text-xs text-muted-foreground" for="def-diar">Mode</Label>
				<Dropdown
					id="def-diar"
					bind:value={defaults.diar_mode}
					defaultValue={savedDefaults.diar_mode}
					options={[
						{ value: 'none', label: 'None' },
						...(inlineDiarizationAvailable
							? [{ value: 'inline', label: 'Inline (from STT)' }]
							: []),
						{ value: 'remote', label: 'Remote service' },
					]}
				/>
				{#if defaults.diar_mode === 'remote'}
					<Label class="text-xs text-muted-foreground" for="def-diar-endpoint">Endpoint</Label>
					<Input
						id="def-diar-endpoint"
						bind:value={defaults.diar_endpoint}
						placeholder="http://diarization:8001"
					/>
				{/if}
			</div>

			<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
				<div class="flex items-center gap-2 font-medium">
					<AlignLeft class="size-4" />
					Summary
				</div>
				{#if llmProviders.length}
					<Label class="text-xs text-muted-foreground" for="def-llm-src">Provider</Label>
					<Dropdown
						id="def-llm-src"
						bind:value={defaults.summarize_provider}
						defaultValue={savedDefaults.summarize_provider ?? ''}
						options={llmProviders.map((p) => ({ value: p.id, label: p.name }))}
					/>
					<Label class="text-xs text-muted-foreground" for="def-llm-model">Model</Label>
					<ModelPicker
						id="def-llm-model"
						refreshToken={defaults.strict_model_filtering}
						interaction="summarize"
						provider={llmSrcProvider}
						bind:value={defaults.summarize_model}
						defaultModel={savedDefaults.summarize_model}
						defaultProvider={savedDefaults.summarize_provider ?? ''}
						autoseed={false}
						onselect={(m) => (llmModelInfo = m)}
					/>
					{#if llmEfforts.length}
						<Label class="text-xs text-muted-foreground" for="def-llm-effort">
							Reasoning effort
						</Label>
						<Dropdown
							id="def-llm-effort"
							bind:value={defaults.summarize_reasoning_effort}
							defaultValue={savedDefaults.summarize_reasoning_effort ?? ''}
							options={[
								{ value: '', label: "Model's default" },
								...llmEfforts.map((e) => ({ value: e, label: e })),
							]}
						/>
					{/if}
				{:else}
					<p class="m-0 text-sm text-muted-foreground">
						Add an LLM provider (OpenAI-compatible chat) to set a summary default.
					</p>
				{/if}
			</div>

			<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
				<div class="flex items-center gap-2 font-medium">
					<Clapperboard class="size-4" />
					Video
				</div>
				{#if videoProviders.length}
					<Label class="text-xs text-muted-foreground" for="def-video-src">Provider</Label>
					<Dropdown
						id="def-video-src"
						bind:value={defaults.video_provider}
						defaultValue={savedDefaults.video_provider ?? ''}
						options={videoProviders.map((p) => ({ value: p.id, label: p.name }))}
					/>
					<Label class="text-xs text-muted-foreground" for="def-video-model">Model</Label>
					<ModelPicker
						id="def-video-model"
						refreshToken={defaults.strict_model_filtering}
						interaction="video"
						provider={videoSrcProvider}
						bind:value={defaults.video_model}
						defaultModel={savedDefaults.video_model}
						defaultProvider={savedDefaults.video_provider ?? ''}
						autoseed={false}
					/>
				{:else}
					<p class="m-0 text-sm text-muted-foreground">
						Add an OpenRouter provider to set a video default.
					</p>
				{/if}
			</div>
		</div>

		<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
			<div class="flex items-center gap-2 font-medium">
				<AlignLeft class="size-4" />
				Summary system prompt
			</div>
			<Textarea
				id="def-llm-prompt"
				rows={5}
				bind:value={defaults.summarize_prompt}
				placeholder="Instructions the summary model receives before the transcript"
			/>
			<p class="m-0 text-xs text-muted-foreground">
				Sent as the system message with every summary. Clear it and save to restore the built-in
				default.
			</p>
		</div>

		<div class="flex flex-col gap-2.5 rounded-lg border p-3.5">
			<div class="flex items-center justify-between gap-4">
				<div class="flex flex-col gap-0.5">
					<span class="flex items-center gap-2 font-medium">
						<Filter class="size-4" />
						Only show compatible models
					</span>
					<span class="text-xs text-muted-foreground">
						Hides models that can't do the job you're picking for - an OpenAI endpoint lists image
						and speech models alongside the transcription ones. Turn it off to see everything a
						provider offers, for a model too new to be recognised or a self-hosted server with its
						own naming.
					</span>
				</div>
				<Switch
					checked={defaults.strict_model_filtering !== false}
					onCheckedChange={(v) => (defaults.strict_model_filtering = v)}
					aria-label="Only show compatible models"
				/>
			</div>
		</div>

		<div class="flex items-center justify-end gap-3">
			{#if defaultsMsg}
				<span class="text-xs text-muted-foreground">{defaultsMsg}</span>
			{/if}
			<Button onclick={saveDefaults}>Save defaults</Button>
		</div>
	</CardContent>
</Card>

<Dialog bind:open={wizardOpen}>
	<DialogContent class="sm:max-w-lg">
		<DialogHeader>
			<DialogTitle>{editing ? 'Edit provider' : 'Add provider'}</DialogTitle>
		</DialogHeader>

		{#if step === 1}
			<DialogDescription>Step 1 - where does this provider run?</DialogDescription>
			<div class="flex gap-2">
				<Button onclick={() => pickHosting('cloud')}>
					<Cloud data-icon="inline-start" />
					Cloud provider
				</Button>
				<Button onclick={() => pickHosting('selfhosted')}>
					<Server data-icon="inline-start" />
					Self-hosted
				</Button>
			</div>
			<p class="m-0 text-xs text-muted-foreground">
				Cloud needs an API key; self-hosted points at a URL on your network.
			</p>
		{:else if step === 2}
			<div class="flex items-center justify-between">
				<span class="text-muted-foreground"
					>Step 2 - {hosting === 'cloud' ? 'cloud' : 'self-hosted'} providers</span
				>
				<Button variant="outline" size="sm" onclick={() => (step = 1)}>← Back</Button>
			</div>
			<div class="mt-2 flex flex-col gap-1.5">
				{#each wizardChoices as meta (meta.kind)}
					<Button
						variant="outline"
						class="h-auto flex-col items-start gap-0.5 py-2"
						onclick={() => pickProvider(meta)}
					>
						<strong>{meta.label}</strong>
						<span class="text-xs font-normal text-muted-foreground">{meta.note}</span>
					</Button>
				{/each}
			</div>
		{:else if selected}
			{@const sel = selected}
			<div class="flex items-center justify-between">
				<span>
					<strong>{sel.label}</strong>
					<span class="text-muted-foreground">· {sel.protocol}</span>
				</span>
				{#if !editing}
					<Button variant="outline" size="sm" onclick={() => (step = 2)}>← Back</Button>
				{/if}
			</div>
			<div class="mt-2 flex flex-col gap-4">
				<div class="flex flex-col gap-2">
					<Label for="name">Name</Label>
					<Input id="name" bind:value={form.name} placeholder={sel.label} />
				</div>
				{#if sel.baseUrlPlaceholder !== null}
					<div class="flex flex-col gap-2">
						<Label for="url">Base URL</Label>
						<Input id="url" bind:value={form.base_url} placeholder={sel.baseUrlPlaceholder} />
					</div>
				{/if}
				<div class="flex flex-col gap-2">
					<Label for="lang">Language</Label>
					<Input id="lang" bind:value={form.language} placeholder="de" />
				</div>
				<div class="flex flex-col gap-2">
					<div class="flex items-center justify-between">
						<span>Favorite models ({form.favorite_models?.length ?? 0})</span>
						<Button variant="outline" size="sm" onclick={loadModels} disabled={modelsLoading}>
							{modelsLoading ? 'Loading…' : 'Load models'}
						</Button>
					</div>
					{#if availableModels.length}
						<Input placeholder="filter…" bind:value={modelFilter} />
						<div class="max-h-45 overflow-auto rounded-md border p-1.5">
							{#each filteredModels as m (m.id)}
								{@const sunset = deprecationFor(form.kind, m.id)}
								<label class="flex items-center gap-2 px-1 py-0.5" title={priceTitle(m)}>
									<Checkbox
										checked={(form.favorite_models ?? []).includes(m.id)}
										onCheckedChange={() => toggleFavorite(m.id)}
									/>
									<span class="min-w-0 flex-1 truncate">{m.id}</span>
									{#if sunset}
										<span
											class="shrink-0 text-xs whitespace-nowrap text-amber-500"
											title="The vendor is retiring this model on {sunset}."
										>
											retiring {sunset}
										</span>
									{/if}
									{#if hintFor(m)}
										<span class="shrink-0 text-xs whitespace-nowrap text-muted-foreground">
											{hintFor(m)}
										</span>
									{/if}
								</label>
							{/each}
							{#if filteredModels.length === 0}
								<span class="text-xs text-muted-foreground">No models match.</span>
							{/if}
						</div>
					{:else if (form.favorite_models ?? []).length}
						<div class="flex flex-wrap gap-1">
							{#each form.favorite_models ?? [] as m (m)}
								<Badge variant="secondary">{m}</Badge>
							{/each}
						</div>
					{/if}
					<span class="text-xs text-muted-foreground">
						Pick the models you'll choose between per session (live list, or curated for
						Deepgram/AssemblyAI).
					</span>
				</div>
				{#if sel.auth !== 'none'}
					<div class="flex flex-col gap-2">
						<div class="flex items-center justify-between">
							<Label for="apikey">{apiKeyLabel}</Label>
							{#if sel.keyUrl}
								<a
									href={sel.keyUrl}
									target="_blank"
									rel="noreferrer"
									class="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
								>
									Get an API key ↗
								</a>
							{/if}
						</div>
						<Input
							id="apikey"
							type="password"
							autocomplete="off"
							bind:value={form.api_key}
							placeholder={editing ? '•••• unchanged' : ''}
						/>
					</div>
				{/if}
				{#if form.kind === 'openrouter' && form.routing}
					{@const routing = form.routing}
					<div class="flex flex-col gap-3 rounded-md border p-3">
						<div class="flex flex-col gap-0.5">
							<strong class="text-sm">Provider routing</strong>
							<span class="text-xs text-muted-foreground">
								OpenRouter serves one model from several upstream providers that differ in price and
								data policy. These pick between them.
							</span>
						</div>
						<div class="flex flex-col gap-2">
							<Label for="or-sort">Prefer</Label>
							<Dropdown
								id="or-sort"
								value={routing.sort ?? ''}
								onpick={(v) => (routing.sort = (v || null) as OpenRouterRouting['sort'])}
								options={[
									{ value: '', label: 'Balanced (OpenRouter default)' },
									{ value: 'price', label: 'Cheapest' },
									{ value: 'throughput', label: 'Highest throughput' },
									{ value: 'latency', label: 'Lowest latency' },
								]}
							/>
						</div>
						<label class="flex items-start gap-2">
							<Checkbox
								checked={routing.data_collection === 'deny'}
								onCheckedChange={(v) => (routing.data_collection = v ? 'deny' : 'allow')}
							/>
							<span class="flex flex-col gap-0.5">
								<span class="text-sm">No data collection</span>
								<span class="text-xs text-muted-foreground">
									Skip providers that may store or train on the transcript.
								</span>
							</span>
						</label>
						<label class="flex items-start gap-2">
							<Checkbox checked={routing.zdr} onCheckedChange={(v) => (routing.zdr = v === true)} />
							<span class="flex flex-col gap-0.5">
								<span class="text-sm">Zero Data Retention only</span>
								<span class="text-xs text-muted-foreground">
									Stricter: only endpoints under a ZDR agreement.
								</span>
							</span>
						</label>
						{#if routing.data_collection === 'deny' || routing.zdr}
							<span class="text-xs text-muted-foreground">
								Note: a model with no provider meeting these rules fails the request rather than
								falling back - worth a Test after saving.
							</span>
						{/if}
					</div>
				{/if}
			</div>
			<DialogFooter>
				<Button variant="outline" onclick={resetWizard}>Cancel</Button>
				<Button onclick={save} disabled={!effectiveName}>
					{editing ? 'Save changes' : 'Add provider'}
				</Button>
			</DialogFooter>
		{/if}
	</DialogContent>
</Dialog>
