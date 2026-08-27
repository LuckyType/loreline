<script lang="ts">
import { onMount } from 'svelte'
import { api, ApiError } from '$lib/api'
import { confirm } from '$lib/confirm.svelte'
import { Cloud, Pencil, Plus, Server, Trash2 } from '@lucide/svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '$lib/components/ui/card'
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
import { Badge } from '$lib/components/ui/badge'
import { Checkbox } from '$lib/components/ui/checkbox'
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '$lib/components/ui/table'
import type {
	ActionDefaults,
	ProviderConfig,
	ProviderCreate,
	ProviderKind,
	ProtocolKind,
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
		kind: 'google',
		label: 'Google STT v2',
		hosting: 'cloud',
		protocol: 'grpc',
		baseUrl: { label: 'GCP project id', placeholder: 'my-gcp-project' },
		apiKey: {
			label: 'API key or service-account JSON (blank = ADC)',
			url: 'https://aistudio.google.com/api-keys',
		},
		note: 'gRPC streaming · diarization.',
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
		kind: 'openai_chat',
		label: 'Ollama / LM Studio / vLLM',
		hosting: 'selfhosted',
		protocol: 'http_batch',
		baseUrl: { label: 'Base URL', placeholder: 'http://localhost:11434/v1' },
		apiKey: { label: 'API key (optional)' },
		note: 'OpenAI-compatible chat for summaries.',
	},
]

const blank = (): ProviderCreate => ({
	name: '',
	kind: 'openai_compat',
	protocol: 'http_batch',
	base_url: '',
	model: '',
	favorite_models: [],
	sample_rate: 16000,
	language: 'de',
	enabled: true,
	api_key: '',
})

let providers = $state<ProviderConfig[]>([])
let editing = $state<string | null>(null)
let message = $state('')
let testResults = $state<Record<string, 'testing' | 'healthy' | 'down'>>({})
let form = $state<ProviderCreate>(blank())
let availableModels = $state<string[]>([])
let modelsLoading = $state(false)
let modelFilter = $state('')
let step = $state(1)
let hosting = $state<Hosting>('cloud')
let selected = $state<ProviderMeta | null>(null)
let wizardOpen = $state(false)

let defaults = $state<ActionDefaults>({
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_model: '',
})
let defaultsMsg = $state('')
let sttSrc = $state('')
let llmSrc = $state('')
const sttProviders = $derived(providers.filter((p) => p.kind !== 'openai_chat'))
const llmProviders = $derived(providers.filter((p) => p.kind === 'openai_chat'))
const sttSrcProvider = $derived(providers.find((p) => p.id === sttSrc))
const llmSrcProvider = $derived(providers.find((p) => p.id === llmSrc))

const filteredModels = $derived(
	modelFilter
		? availableModels.filter((m) => m.toLowerCase().includes(modelFilter.toLowerCase()))
		: availableModels,
)

$effect(() => {
	if (!sttSrc && sttProviders.length) sttSrc = sttProviders[0].id
	if (!llmSrc && llmProviders.length) llmSrc = llmProviders[0].id
})

async function load() {
	providers = await api.listProviders()
}

async function loadDefaults() {
	try {
		defaults = await api.getDefaults()
	} catch {
		/* keep blanks */
	}
}

async function saveDefaults() {
	try {
		defaults = await api.setDefaults(defaults)
		defaultsMsg = 'Saved'
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
			const present = availableModels
			form.favorite_models = (form.favorite_models ?? []).filter((m) => present.includes(m))
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
	<CardContent class="flex items-center justify-between py-4">
		<h2 class="m-0">Providers</h2>
		<div class="flex gap-1">
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
		</div>
	</CardContent>
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
		<CardDescription>
			Pre-selected first in the per-session pickers. Pick a provider to browse its models.
		</CardDescription>
	</CardHeader>
	<CardContent class="flex flex-col gap-4">
		<div class="flex flex-col gap-2">
			<Label for="def-stt-src">Transcription model</Label>
			<div class="flex items-stretch gap-2">
				<Dropdown
					id="def-stt-src"
					class="max-w-48"
					bind:value={sttSrc}
					options={sttProviders.map((p) => ({ value: p.id, label: p.name }))}
				/>
				<div class="flex-1">
					<ModelPicker
						provider={sttSrcProvider}
						bind:value={defaults.stt_model}
						autoseed={false}
						onpick={saveDefaults}
					/>
				</div>
			</div>
		</div>
		<div class="flex flex-col gap-2">
			<Label for="def-diar">Diarization</Label>
			<Dropdown
				id="def-diar"
				class="max-w-48"
				bind:value={defaults.diar_mode}
				options={[
          { value: '', label: 'No default' },
          { value: 'none', label: 'None' },
          { value: 'inline', label: 'Inline (from STT)' },
          { value: 'remote', label: 'Remote service' }
        ]}
				onpick={() => void saveDefaults()}
			/>
		</div>
		{#if defaults.diar_mode === 'remote'}
			<div class="flex flex-col gap-2">
				<Label for="def-diar-endpoint">Diarization endpoint</Label>
				<Input
					id="def-diar-endpoint"
					class="max-w-48"
					bind:value={defaults.diar_endpoint}
					placeholder="http://diarization:8001"
					onblur={saveDefaults}
				/>
			</div>
		{/if}
		<div class="flex flex-col gap-2">
			<Label for="def-llm-src">Summary model</Label>
			{#if llmProviders.length}
				<div class="flex items-stretch gap-2">
					<Dropdown
						id="def-llm-src"
						class="max-w-48"
						bind:value={llmSrc}
						options={llmProviders.map((p) => ({ value: p.id, label: p.name }))}
					/>
					<div class="flex-1">
						<ModelPicker
							provider={llmSrcProvider}
							bind:value={defaults.summarize_model}
							autoseed={false}
							onpick={saveDefaults}
						/>
					</div>
				</div>
			{:else}
				<p class="m-0 text-sm text-muted-foreground">
					Add an LLM provider (OpenAI-compatible chat) to set a summary default.
				</p>
			{/if}
		</div>
		{#if defaultsMsg}
			<span class="text-sm text-muted-foreground">{defaultsMsg}</span>
		{/if}
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
							{#each filteredModels as m (m)}
								<label class="flex items-center gap-2 px-1 py-0.5">
									<Checkbox
										checked={(form.favorite_models ?? []).includes(m)}
										onCheckedChange={() => toggleFavorite(m)}
									/>
									<span>{m}</span>
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
