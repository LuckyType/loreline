<script lang="ts">
import { AlignLeft, Cloud, Mic, Pencil, Plus, Server, Trash2, Users } from '@lucide/svelte'
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
	isLlmProvider,
	type ActionDefaults,
	type ProtocolKind,
	type ModelInfo,
	type OpenRouterRouting,
	type ProviderConfig,
	type ProviderCreate,
	type ProviderKind,
} from '$lib/types'

type Hosting = 'cloud' | 'selfhosted'

interface ProviderMeta {
	kind: ProviderKind
	label: string
	hosting: Hosting
	protocol: ProtocolKind
	baseUrl?: { label: string; placeholder: string }
	apiKey?: { label: string; url?: string }
	defaultModel?: string
	note: string
}

const CATALOG: ProviderMeta[] = [
	{
		kind: 'deepgram',
		label: 'Deepgram',
		hosting: 'cloud',
		protocol: 'ws',
		apiKey: { label: 'API key', url: 'https://console.deepgram.com/' },
		defaultModel: 'nova-2',
		note: 'Streaming WS · inline diarization.',
	},
	{
		kind: 'assemblyai',
		label: 'AssemblyAI',
		hosting: 'cloud',
		protocol: 'ws',
		apiKey: { label: 'API key', url: 'https://www.assemblyai.com/dashboard/api-keys' },
		note: 'Streaming WS · inline diarization.',
	},
	{
		kind: 'openai',
		label: 'OpenAI Realtime',
		hosting: 'cloud',
		protocol: 'ws',
		apiKey: { label: 'API key', url: 'https://platform.openai.com/api-keys' },
		defaultModel: 'gpt-realtime-whisper',
		note: 'Streaming transcription.',
	},
	{
		kind: 'gemini',
		label: 'Google Gemini',
		hosting: 'cloud',
		protocol: 'http_batch',
		apiKey: { label: 'API key', url: 'https://aistudio.google.com/api-keys' },
		defaultModel: 'gemini-3.5-transcribe',
		note: 'API key · diarization · word timestamps.',
	},
	{
		kind: 'google',
		label: 'Google STT v2',
		hosting: 'cloud',
		protocol: 'grpc',
		baseUrl: { label: 'GCP project id', placeholder: 'my-gcp-project' },
		apiKey: {
			// Cloud STT v2 rejects API keys outright; only a service account
			// works here. An AI Studio key belongs on the Gemini entry above.
			label: 'Service-account JSON (blank = ADC)',
			url: 'https://console.cloud.google.com/iam-admin/serviceaccounts',
		},
		note: 'gRPC streaming · diarization · needs a service account.',
	},
	{
		kind: 'openai_compat',
		label: 'Speaches / OpenAI-compatible',
		hosting: 'selfhosted',
		protocol: 'http_batch',
		baseUrl: { label: 'Base URL', placeholder: 'http://localhost:8000/v1' },
		apiKey: { label: 'API key (optional)' },
		note: 'faster-whisper / whisper.cpp server.',
	},
	{
		kind: 'vosk',
		label: 'Vosk server',
		hosting: 'selfhosted',
		protocol: 'ws',
		baseUrl: { label: 'Base URL', placeholder: 'ws://localhost:2700' },
		note: 'Offline, lightweight, ARM-friendly.',
	},
	{
		kind: 'openai_chat',
		label: 'OpenAI (chat / LLM)',
		hosting: 'cloud',
		protocol: 'http_batch',
		apiKey: { label: 'API key', url: 'https://platform.openai.com/api-keys' },
		note: 'LLM for session summaries.',
	},
	{
		kind: 'openrouter',
		label: 'OpenRouter',
		hosting: 'cloud',
		protocol: 'http_batch',
		apiKey: { label: 'API key', url: 'https://openrouter.ai/settings/keys' },
		defaultModel: 'anthropic/claude-sonnet-4.5',
		note: 'One key for many vendors · "vendor/model" ids.',
	},
	{
		kind: 'openai_chat',
		label: 'Ollama / LM Studio / vLLM',
		hosting: 'selfhosted',
		protocol: 'http_batch',
		baseUrl: { label: 'Base URL', placeholder: 'http://localhost:11434/v1' },
		apiKey: { label: 'API key (optional)' },
		note: 'OpenAI-compatible chat for summaries.',
	},
]

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
	model: '',
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
let testResults = $state<Record<string, 'testing' | 'healthy' | 'down'>>({})
let form = $state<ProviderCreate>(blank())
let availableModels = $state<ModelInfo[]>([])
let modelsLoading = $state(false)
let modelFilter = $state('')
let step = $state(1)
let hosting = $state<Hosting>('cloud')
let selected = $state<ProviderMeta | null>(null)
let wizardOpen = $state(false)

const blankDefaults = (): ActionDefaults => ({
	stt_provider: '',
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_provider: '',
	summarize_model: '',
	summarize_prompt: '',
})
let defaults = $state<ActionDefaults>(blankDefaults())
// Last persisted state - drives the "default" tags in the pickers, so they
// reflect what is saved, not the (possibly unsaved) current selection.
let savedDefaults = $state<ActionDefaults>(blankDefaults())
let defaultsMsg = $state('')
const sttProviders = $derived(providers.filter((p) => !isLlmProvider(p)))
const llmProviders = $derived(providers.filter((p) => isLlmProvider(p)))
const sttSrcProvider = $derived(providers.find((p) => p.id === defaults.stt_provider))
const llmSrcProvider = $derived(providers.find((p) => p.id === defaults.summarize_provider))

const filteredModels = $derived(
	modelFilter
		? availableModels.filter((m) => m.id.toLowerCase().includes(modelFilter.toLowerCase()))
		: availableModels,
)

$effect(() => {
	// Pre-fill empty pickers so the model list is browsable; nothing is
	// persisted until the explicit Save.
	if (!defaults.stt_provider && sttProviders.length) defaults.stt_provider = sttProviders[0].id
	if (!defaults.summarize_provider && llmProviders.length) {
		defaults.summarize_provider = llmProviders[0].id
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
		availableModels = await api.providerModels({
			kind: form.kind,
			base_url: form.base_url || null,
			api_key: form.api_key || null,
			provider_id: editing,
		})
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
			base_url: form.base_url || null,
			model: form.model || null,
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
	selected = null
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

function pickProvider(meta: ProviderMeta) {
	selected = meta
	form = { ...blank(), kind: meta.kind, protocol: meta.protocol, model: meta.defaultModel ?? '' }
	step = 3
}

function resetWizard() {
	editing = null
	selected = null
	form = blank()
	step = 1
	wizardOpen = false
}

function edit(p: ProviderConfig) {
	editing = p.id
	selected = CATALOG.find((m) => m.kind === p.kind) ?? null
	form = {
		name: p.name,
		kind: p.kind,
		protocol: p.protocol,
		base_url: p.base_url ?? '',
		model: p.model ?? '',
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
	testResults = { ...testResults, [id]: 'testing' }
	try {
		const r = await api.testProvider(id)
		testResults = { ...testResults, [id]: r.healthy ? 'healthy' : 'down' }
	} catch {
		testResults = { ...testResults, [id]: 'down' }
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
					<TableHead>Name</TableHead><TableHead>Kind</TableHead><TableHead>Endpoint</TableHead
					><TableHead>API key</TableHead><TableHead>Status</TableHead><TableHead></TableHead>
				</TableRow>
			</TableHeader>
			<TableBody>
				{#each providers as p (p.id)}
					<TableRow>
						<TableCell>{p.name}</TableCell>
						<TableCell>{p.kind}</TableCell>
						<TableCell class="text-muted-foreground">{p.base_url ?? 'default'}</TableCell>
						<TableCell
							><code class="text-muted-foreground">{p.secret_hint ?? '- none -'}</code></TableCell
						>
						<TableCell>
							{#if testResults[p.id] === 'healthy'}
								<Badge variant="secondary" class="gap-1.5"
									><span class="size-2 rounded-full bg-emerald-500"></span>healthy</Badge
								>
							{:else if testResults[p.id] === 'down'}
								<Badge variant="destructive" class="gap-1.5"
									><span class="size-2 rounded-full bg-red-500"></span>down</Badge
								>
							{:else if testResults[p.id] === 'testing'}
								<Badge variant="secondary" class="gap-1.5"
									><span class="size-2 rounded-full bg-amber-500"></span>testing…</Badge
								>
							{:else}
								<Badge variant="outline" class="gap-1.5"
									><span class="size-2 rounded-full bg-muted-foreground"></span>unknown</Badge
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
					provider={sttSrcProvider}
					bind:value={defaults.stt_model}
					defaultModel={savedDefaults.stt_model}
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
            { value: 'inline', label: 'Inline (from STT)' },
            { value: 'remote', label: 'Remote service' }
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
						provider={llmSrcProvider}
						bind:value={defaults.summarize_model}
						defaultModel={savedDefaults.summarize_model}
						autoseed={false}
					/>
				{:else}
					<p class="m-0 text-sm text-muted-foreground">
						Add an LLM provider (OpenAI-compatible chat) to set a summary default.
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
				{#each CATALOG.filter((m) => m.hosting === hosting) as meta, i (`${meta.kind}-${i}`)}
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
				{#if sel.baseUrl}
					<div class="flex flex-col gap-2">
						<Label for="url">{sel.baseUrl.label}</Label>
						<Input id="url" bind:value={form.base_url} placeholder={sel.baseUrl.placeholder} />
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
								<label class="flex items-center gap-2 px-1 py-0.5" title={priceTitle(m)}>
									<Checkbox
										checked={(form.favorite_models ?? []).includes(m.id)}
										onCheckedChange={() => toggleFavorite(m.id)}
									/>
									<span class="min-w-0 flex-1 truncate">{m.id}</span>
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
				{#if sel.apiKey}
					<div class="flex flex-col gap-2">
						<div class="flex items-center justify-between">
							<Label for="apikey"
								>{sel.apiKey.label}{editing ? ' - blank = keep current' : ''}</Label
							>
							{#if sel.apiKey.url}
								<a
									href={sel.apiKey.url}
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
				<Button onclick={save} disabled={!form.name}>
					{editing ? 'Save changes' : 'Add provider'}
				</Button>
			</DialogFooter>
		{/if}
	</DialogContent>
</Dialog>
