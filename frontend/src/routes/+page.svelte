<script lang="ts">
import { ArrowDownToLine, ChevronDown, Filter, Trash2, WrapText } from '@lucide/svelte'
import { onDestroy, onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
import { Checkbox } from '$lib/components/ui/checkbox'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import LogLine from '$lib/LogLine.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import { formatTime, health, logsWs, speakerColor, transcriptWs } from '$lib/stores'
import {
	liveSttProviders,
	type ActionDefaults,
	type DiarizationModeKind,
	type ModelInfo,
	type ProviderConfig,
	type TranscriptEvent,
} from '$lib/types'
import { cn } from '$lib/utils'
import { connect, type LiveSocket } from '$lib/ws'

// --- session controls ---
let providers = $state<ProviderConfig[]>([])
let primary = $state('')
let model = $state('')
let fallback = $state('')
let fallbackModel = $state('')
let defaults = $state<ActionDefaults>({
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_model: '',
})

// Live capture only: LLM providers can't transcribe, and OpenRouter's STT has
// no streaming mode so it is re-processing-only (see $lib/types).
const sttProviders = $derived(liveSttProviders(providers))
const primaryProvider = $derived(providers.find((p) => p.id === primary))
const fallbackProvider = $derived(providers.find((p) => p.id === fallback))
let diarMode = $state<DiarizationModeKind>('none')
// Set by the primary model picker: inline diarization only yields speakers for
// some provider+model pairs (see src/loreline/capabilities.py), so the option
// is offered only when the chosen model actually returns them.
let primaryModelInfo = $state<ModelInfo | undefined>(undefined)
const inlineAvailable = $derived(primaryModelInfo?.inline_diarization === true)
let diarEndpoint = $state('')
// On by default: capture always fed the campaign glossary to the provider, and
// turning it off is the deliberate choice (hear the audio unbiased).
let useGlossary = $state(true)
let error = $state('')
let busy = $state(false)

// Where the bundled sherpa-onnx diarizer answers on the compose network.
// Used as the actual default, not just placeholder text - it was previously
// only a placeholder, so you had to retype the value it was already showing.
const DEFAULT_DIAR_ENDPOINT = 'http://diarization:8001'

// Validated in the UI rather than only server-side, so the message can sit
// under the field it's about instead of at the bottom of the card.
const endpointMissing = $derived(diarMode === 'remote' && !diarEndpoint.trim())

// A stored default (or an earlier pick) of "inline" must not survive a switch
// to a model that returns no speakers - the backend would reject the start.
$effect(() => {
	if (diarMode === 'inline' && primaryModelInfo && !inlineAvailable) diarMode = 'none'
})

function setDiarMode(mode: string) {
	diarMode = mode as DiarizationModeKind
	// Switching to remote with nothing configured: offer the bundled service
	// rather than an empty box the user has to fill from the placeholder.
	if (mode === 'remote' && !diarEndpoint.trim()) {
		diarEndpoint = defaults.diar_endpoint || DEFAULT_DIAR_ENDPOINT
	}
}

// Fallback and diarization are collapsed by default. The summary line has to
// carry enough that folding them away never hides a problem - so it states
// what each is set to, and turns red when something needs attention.
let advancedOpen = $state(false)

const fallbackSummary = $derived(
	fallbackProvider ? [fallbackProvider.name, fallbackModel].filter(Boolean).join(' · ') : 'None',
)

const diarSummary = $derived.by(() => {
	if (diarMode === 'none') return 'Off'
	if (diarMode === 'inline') return 'Inline (from STT)'
	if (endpointMissing) return 'Remote - endpoint missing'
	if ($health?.diarizer_endpoint && $health.diarizer_reachable === false) {
		return 'Remote - service not answering'
	}
	return `Remote - ${diarEndpoint}`
})

const advancedProblem = $derived(
	endpointMissing ||
		(diarMode === 'remote' && !!$health?.diarizer_endpoint && $health.diarizer_reachable === false),
)

const capturing = $derived($health?.capture_status === 'capturing')

// --- session elapsed time ---
// healthz's uptime_seconds is the app process's uptime, not the session's -
// after the first session of a process's lifetime the two drift apart for
// good. Fetch the active session's started_at instead and tick locally.
let sessionStartedAt = $state<number | null>(null) // epoch seconds
let nowMs = $state(Date.now())

$effect(() => {
	const id = capturing ? $health?.active_session_id : null
	if (!id) {
		sessionStartedAt = null
		return
	}
	api
		.getSession(id)
		.then((detail) => {
			if ($health?.active_session_id === detail.session.id) {
				sessionStartedAt = detail.session.started_at
			}
		})
		.catch(() => {
			/* the ticker just stays hidden until the next health poll retries */
		})
})

$effect(() => {
	if (!capturing) return
	const timer = setInterval(() => {
		nowMs = Date.now()
	}, 1000)
	return () => clearInterval(timer)
})

const sessionElapsed = $derived(sessionStartedAt === null ? null : nowMs / 1000 - sessionStartedAt)

// Live transcription can stop flowing while the recording itself is fine
// (backend outage, dead uplink). healthz reports when the router's failing
// streak began; surface it as a warning rather than letting the transcript
// pane just go silently quiet.
const sttDegradedAt = $derived.by(() => {
	const since = capturing ? $health?.stt_degraded_since : null
	if (!since) return null
	return new Date(since * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
})

// --- live transcript ---
let txEvents = $state<TranscriptEvent[]>([])
let txAutoscroll = $state(true)
let txSock: LiveSocket | null = null
let txContainer: HTMLDivElement

// --- logs ---
let logLines = $state<string[]>([])
let logFilter = $state('')
let logFilterOpen = $state(false)
let logFollow = $state(true)
let logWrap = $state(false)
let logSock: LiveSocket | null = null
let logContainer: HTMLDivElement

const shownLogs = $derived(
	logFilter ? logLines.filter((l) => l.toLowerCase().includes(logFilter.toLowerCase())) : logLines,
)

function toggleFilter() {
	logFilterOpen = !logFilterOpen
	if (!logFilterOpen) logFilter = ''
}

async function clearTranscript() {
	if (txEvents.length && !(await confirm('Clear the transcript view?'))) return
	txEvents = []
}

async function clearLogs() {
	if (logLines.length && !(await confirm('Clear the log view?'))) return
	logLines = []
}

async function load() {
	try {
		providers = await api.listProviders()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load providers'
	}
	try {
		defaults = await api.getDefaults()
		if (defaults.diar_mode) diarMode = defaults.diar_mode as DiarizationModeKind
		if (defaults.diar_endpoint) diarEndpoint = defaults.diar_endpoint
		// Saved defaults can say "remote" without an endpoint. Fill in the
		// bundled service rather than showing an empty box next to a
		// placeholder the user would have to copy out by hand.
		if (diarMode === 'remote' && !diarEndpoint.trim()) {
			diarEndpoint = DEFAULT_DIAR_ENDPOINT
		}
	} catch {
		/* defaults are optional */
	}
	// Seed the provider picker only after the defaults are known, so the
	// configured default provider wins over "first in the list".
	if (!primary) {
		const wanted = defaults.stt_provider
		primary =
			wanted && sttProviders.some((p) => p.id === wanted) ? wanted : (sttProviders[0]?.id ?? '')
	}
}

// The transcript view only ever holds events pushed since this component's
// own /ws/transcript connection opened - leaving the Dashboard and coming
// back reconnects it fresh, otherwise losing everything from before that
// point even though it's already persisted server-side. Seed from the
// active session's saved transcript before wiring up the live socket, so
// nothing appears lost.
async function loadActiveTranscript(): Promise<void> {
	try {
		const h = await api.health()
		if (h.active_session_id) {
			const detail = await api.getSession(h.active_session_id)
			txEvents = detail.transcript.slice(-499)
		}
	} catch {
		/* best effort - the live socket still populates new events either way */
	}
}

async function start() {
	busy = true
	error = ''
	try {
		await api.startSession({
			primary_provider: primary,
			fallback_provider: fallback || null,
			model: model || null,
			fallback_model: fallbackModel || null,
			diarization: {
				mode: diarMode,
				endpoint: diarMode === 'remote' ? diarEndpoint : null,
				min_speakers: null,
				max_speakers: null,
			},
			use_glossary: useGlossary,
		})
		await refresh()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to start'
	} finally {
		busy = false
	}
}

async function stop() {
	busy = true
	error = ''
	try {
		await api.stopSession()
		await refresh()
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to stop'
	} finally {
		busy = false
	}
}

async function refresh() {
	health.set(await api.health())
}

let unmounted = false

onMount(() => {
	load()
	logSock = connect(
		'/ws/logs',
		(data) => {
			logLines = [...logLines.slice(-999), data]
			if (logFollow) queueMicrotask(() => logContainer?.scrollTo(0, logContainer.scrollHeight))
		},
		(o) => logsWs.set(o),
	)
	// Seed history before opening the live socket - connecting first could
	// otherwise have loadActiveTranscript's snapshot overwrite an event the
	// socket already delivered while that fetch was still in flight.
	loadActiveTranscript().finally(() => {
		if (unmounted) return
		txSock = connect(
			'/ws/transcript',
			(data) => {
				try {
					const ev = JSON.parse(data) as TranscriptEvent
					txEvents = [...txEvents.slice(-499), ev]
					if (txAutoscroll) queueMicrotask(() => txContainer?.scrollTo(0, txContainer.scrollHeight))
				} catch {
					/* ignore malformed frame */
				}
			},
			(o) => transcriptWs.set(o),
		)
	})
})

onDestroy(() => {
	unmounted = true
	txSock?.close()
	logSock?.close()
	transcriptWs.set(false)
	logsWs.set(false)
})
</script>

<div class="flex h-[calc(100vh-104px)] flex-col gap-4">
	<Card class="shrink-0 py-4">
		<CardContent class="px-4">
			{#if capturing}
				<div class="flex items-center justify-between">
					<span class="flex items-center gap-2">
						<span class="size-2 rounded-full bg-emerald-500"></span>
						<strong>Recording</strong>
						<span class="text-muted-foreground">
							{sessionElapsed === null ? '-' : formatTime(sessionElapsed)}
							· {providers.length} providers
						</span>
					</span>
					<Button variant="destructive" onclick={stop} disabled={busy}>Stop session</Button>
				</div>
				{#if sttDegradedAt}
					<p class="mt-2 border-t border-dashed pt-2 text-sm text-amber-500">
						Live transcription has been failing since {sttDegradedAt} - audio is still being
						recorded and the session can be re-transcribed later.
					</p>
				{/if}
			{:else}
				<!-- Essentials inline; fallback + diarization fold away, but the summary
				     below always states what they're set to so nothing hides silently. -->
				<div class="grid grid-cols-[1fr_1fr_auto] items-end gap-3">
					<div class="flex flex-col gap-2">
						<Label for="primary">Transcription provider</Label>
						<Dropdown
							id="primary"
							bind:value={primary}
							defaultValue={defaults.stt_provider ?? ''}
							options={sttProviders.map((p) => ({ value: p.id, label: p.name }))}
							placeholder="Select provider…"
						/>
					</div>
					<div class="flex flex-col gap-2">
						<Label for="model">Model</Label>
						<ModelPicker
							id="model"
							provider={primaryProvider}
							bind:value={model}
							defaultModel={defaults.stt_model}
						/>
					</div>
					<Button onclick={start} disabled={busy || !primary || endpointMissing}>
						Start session
					</Button>
				</div>

				<div
					class="mt-3.5 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-dashed pt-3 text-xs"
				>
					<span
						class={cn('size-1.5 shrink-0 rounded-full', advancedProblem ? 'bg-destructive' : 'bg-emerald-500')}
					></span>
					<span class="text-muted-foreground">Fallback</span>
					<span class="text-foreground">{fallbackSummary}</span>
					<span class="text-muted-foreground">·</span>
					<span class="text-muted-foreground">Diarization</span>
					<span class={advancedProblem ? 'text-destructive' : 'text-foreground'}
						>{diarSummary}</span
					>
					<span class="text-muted-foreground">·</span>
					<span class="text-muted-foreground">Glossary</span>
					<span class="text-foreground">{useGlossary ? 'On' : 'Off'}</span>
					<Button
						variant="ghost"
						size="sm"
						class="ml-auto h-6 px-2 text-xs"
						aria-expanded={advancedOpen}
						onclick={() => (advancedOpen = !advancedOpen)}
					>
						{advancedOpen ? 'Done' : 'Edit'}
						<ChevronDown class={cn('size-3 transition-transform', advancedOpen && 'rotate-180')} />
					</Button>
				</div>

				{#if advancedOpen}
					<div class="mt-3 grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] items-start gap-3">
						<div class="flex flex-col gap-2">
							<Label for="fallback">Fallback provider</Label>
							<Dropdown
								id="fallback"
								bind:value={fallback}
								options={[
                  { value: '', label: 'None' },
                  ...sttProviders.map((p) => ({ value: p.id, label: p.name }))
                ]}
								placeholder="None"
							/>
						</div>
						{#if fallbackProvider}
							<div class="flex flex-col gap-2">
								<Label for="fallback-model">Fallback model</Label>
								<ModelPicker
									id="fallback-model"
									provider={fallbackProvider}
									bind:value={fallbackModel}
								/>
							</div>
						{/if}
						<div class="flex flex-col gap-2">
							<Label for="diar">Diarization</Label>
							<Dropdown
								id="diar"
								value={diarMode}
								onpick={setDiarMode}
								options={[
									{ value: 'none', label: 'None' },
									...(inlineAvailable
										? [{ value: 'inline', label: 'Inline (from STT)' }]
										: []),
									{ value: 'remote', label: 'Remote service' },
								]}
							/>
							{#if !inlineAvailable}
								<span class="text-xs text-muted-foreground">
									This model returns no speaker labels, so inline diarization isn't offered.
								</span>
							{/if}
						</div>
						<div class="flex flex-col gap-2">
							<Label for="use-glossary">Glossary</Label>
							<label class="flex items-center gap-2">
								<Checkbox
									id="use-glossary"
									checked={useGlossary}
									onCheckedChange={(v) => (useGlossary = v === true)}
								/>
								<span class="text-sm">Use glossary</span>
							</label>
							<span class="text-xs text-muted-foreground">
								Sends the campaign's terms to the provider as keyterms or a prompt.
							</span>
						</div>
						{#if diarMode === 'remote'}
							<div class="flex flex-col gap-2">
								<Label for="ep">Diarization endpoint</Label>
								<Input
									id="ep"
									bind:value={diarEndpoint}
									placeholder="http://diarization:8001"
									aria-invalid={endpointMissing || undefined}
								/>
								{#if endpointMissing}
									<span class="text-xs text-destructive">
										Required for remote diarization - the service's base URL.
									</span>
								{:else if $health?.diarizer_endpoint && $health.diarizer_reachable === false}
									<span class="text-xs text-amber-500">
										No diarization service answered at {$health.diarizer_endpoint}.
									</span>
								{/if}
							</div>
						{/if}
					</div>
				{/if}
			{/if}
			{#if error}
				<p class="mt-2 text-sm text-destructive">{error}</p>
			{/if}
		</CardContent>
	</Card>

	<div class="grid min-h-0 flex-1 grid-cols-[3fr_2fr] gap-4">
		<Card class="flex min-h-0 flex-col py-4">
			<div class="flex items-center justify-between gap-2 px-4 pb-2">
				<h3 class="m-0">Transcript</h3>
				<div class="flex gap-1">
					<Button
						variant="ghost"
						size="icon-sm"
						class={cn(
              'opacity-55 hover:opacity-100',
              txAutoscroll && 'border-emerald-500 opacity-100'
            )}
						title="Auto-scroll"
						aria-label="Auto-scroll"
						onclick={() => (txAutoscroll = !txAutoscroll)}
					>
						<ArrowDownToLine />
					</Button>
					<Button
						variant="ghost"
						size="icon-sm"
						class="opacity-55 hover:opacity-100"
						title="Clear transcript"
						aria-label="Clear transcript"
						onclick={clearTranscript}
					>
						<Trash2 />
					</Button>
				</div>
			</div>
			<div class="min-h-0 flex-1 overflow-auto px-4" bind:this={txContainer}>
				{#if txEvents.length === 0}
					<p class="text-muted-foreground">Waiting for transcript events…</p>
				{/if}
				{#each txEvents as ev, i (i)}
					<div class="flex items-baseline gap-2.5 py-1">
						<span class="text-xs tabular-nums text-muted-foreground"
							>{formatTime(ev.start_ts)}</span
						>
						{#if ev.speaker}
							<span class="text-sm font-semibold" style="color: {speakerColor(ev.speaker)}"
								>{ev.speaker}</span
							>
						{/if}
						<span class={cn('min-w-0', !ev.is_final && 'opacity-60 italic')}>{ev.text}</span>
					</div>
				{/each}
			</div>
		</Card>

		<Card class="flex min-h-0 flex-col py-4">
			<div class="flex items-center justify-between gap-2 px-4 pb-2">
				<h3 class="m-0">Logs</h3>
				<div class="flex items-center gap-1">
					{#if logFilterOpen}
						<Input class="w-33" placeholder="filter…" bind:value={logFilter} autofocus />
					{/if}
					<Button
						variant="ghost"
						size="icon-sm"
						class={cn(
              'opacity-55 hover:opacity-100',
              logFilterOpen && 'border-emerald-500 opacity-100'
            )}
						title="Filter logs"
						aria-label="Filter logs"
						onclick={toggleFilter}
					>
						<Filter />
					</Button>
					<Button
						variant="ghost"
						size="icon-sm"
						class={cn('opacity-55 hover:opacity-100', logWrap && 'border-emerald-500 opacity-100')}
						title="Wrap lines"
						aria-label="Wrap lines"
						onclick={() => (logWrap = !logWrap)}
					>
						<WrapText />
					</Button>
					<Button
						variant="ghost"
						size="icon-sm"
						class={cn(
              'opacity-55 hover:opacity-100',
              logFollow && 'border-emerald-500 opacity-100'
            )}
						title="Follow"
						aria-label="Follow"
						onclick={() => (logFollow = !logFollow)}
					>
						<ArrowDownToLine />
					</Button>
					<Button
						variant="ghost"
						size="icon-sm"
						class="opacity-55 hover:opacity-100"
						title="Clear logs"
						aria-label="Clear logs"
						onclick={clearLogs}
					>
						<Trash2 />
					</Button>
				</div>
			</div>
			<div
				class="m-0 min-h-0 flex-1 overflow-auto px-4 font-mono text-xs leading-relaxed"
				bind:this={logContainer}
			>
				{#each shownLogs as line, i (i)}
					<LogLine {line} wrap={logWrap} />
				{/each}
				{#if shownLogs.length === 0}
					<!-- This panel carries the running capture's lines only, so it is
					     empty by design between sessions. Say so rather than letting
					     it read as a broken feed: every finished run keeps its own log
					     under Show logs on the session page. -->
					<span class="text-muted-foreground">
						{capturing
							? 'No log lines yet.'
							: 'Logs appear here while a session is recording. A finished session keeps its own logs on its page.'}
					</span>
				{/if}
			</div>
		</Card>
	</div>
</div>
