<script lang="ts">
import { onDestroy, onMount } from 'svelte'
import { api, ApiError } from '$lib/api'
import { confirm } from '$lib/confirm.svelte'
import { health, logsWs, speakerColor, formatTime, transcriptWs } from '$lib/stores'
import { connect, type LiveSocket } from '$lib/ws'
import { ArrowDownToLine, Filter, Trash2, WrapText } from '@lucide/svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import LogLine from '$lib/LogLine.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import Dropdown from '$lib/Dropdown.svelte'
import { cn } from '$lib/utils'
import type {
	ActionDefaults,
	ProviderConfig,
	DiarizationModeKind,
	TranscriptEvent,
} from '$lib/types'

// --- session controls ---
let providers = $state<ProviderConfig[]>([])
let primary = $state('')
let model = $state('')
let fallback = $state('')
let defaults = $state<ActionDefaults>({
	stt_model: '',
	diar_mode: '',
	diar_endpoint: '',
	summarize_model: '',
})

// STT providers only - an LLM (openai_chat) provider can't transcribe.
const sttProviders = $derived(providers.filter((p) => p.kind !== 'openai_chat'))
const primaryProvider = $derived(providers.find((p) => p.id === primary))
let diarMode = $state<DiarizationModeKind>('none')
let diarEndpoint = $state('')
let error = $state('')
let busy = $state(false)

const capturing = $derived($health?.capture_status === 'capturing')

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
		if (!primary && sttProviders.length) primary = sttProviders[0].id
	} catch (err) {
		error = err instanceof ApiError ? err.message : 'failed to load providers'
	}
	try {
		defaults = await api.getDefaults()
		if (defaults.diar_mode) diarMode = defaults.diar_mode as DiarizationModeKind
		if (defaults.diar_endpoint) diarEndpoint = defaults.diar_endpoint
	} catch {
		/* defaults are optional */
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
			diarization: {
				mode: diarMode,
				endpoint: diarMode === 'remote' ? diarEndpoint : null,
				min_speakers: null,
				max_speakers: null,
			},
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
							{Math.round($health?.uptime_seconds ?? 0)}s · {providers.length} providers
						</span>
					</span>
					<Button variant="destructive" onclick={stop} disabled={busy}>Stop session</Button>
				</div>
			{:else}
				<div class="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] items-end gap-3">
					<div class="flex flex-col gap-2">
						<Label for="primary">Primary provider</Label>
						<Dropdown
							id="primary"
							bind:value={primary}
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
					<div class="flex flex-col gap-2">
						<Label for="fallback">Fallback (optional)</Label>
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
					<div class="flex flex-col gap-2">
						<Label for="diar">Diarization</Label>
						<Dropdown
							id="diar"
							bind:value={diarMode}
							options={[
                { value: 'none', label: 'None' },
                { value: 'inline', label: 'Inline (from STT)' },
                { value: 'remote', label: 'Remote service' }
              ]}
						/>
					</div>
					{#if diarMode === 'remote'}
						<div class="flex flex-col gap-2">
							<Label for="ep">Diarization endpoint</Label>
							<Input id="ep" bind:value={diarEndpoint} placeholder="http://diarizer:8001" />
						</div>
					{/if}
					<div class="flex justify-end">
						<Button onclick={start} disabled={busy || !primary}>Start session</Button>
					</div>
				</div>
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
					<span class="text-muted-foreground">No log lines.</span>
				{/if}
			</div>
		</Card>
	</div>
</div>
