<script lang="ts">
import {
	ArrowDownToLine,
	ChevronDown,
	Filter,
	Trash2,
	TriangleAlert,
	WrapText,
} from '@lucide/svelte'
import { onDestroy, onMount } from 'svelte'
import { ApiError, api } from '$lib/api'
import {
	featureBlockedReason,
	glossaryDropsWarning,
	inlineDiarizationFor,
	preferredModel,
} from '$lib/capabilities.svelte'
import { Button } from '$lib/components/ui/button'
import { Card, CardContent } from '$lib/components/ui/card'
import { Checkbox } from '$lib/components/ui/checkbox'
import { Input } from '$lib/components/ui/input'
import { Label } from '$lib/components/ui/label'
import { confirm } from '$lib/confirm.svelte'
import Dropdown from '$lib/Dropdown.svelte'
import LogLine from '$lib/LogLine.svelte'
import { modelInfoFor } from '$lib/modelCatalog.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import { formatTime, health, logsWs, speakerColor, transcriptWs } from '$lib/stores'
import {
	liveSttProviders,
	type ActionDefaults,
	type DiarizationModeKind,
	type ProviderConfig,
	type TranscriptEvent,
} from '$lib/types'
import { cn } from '$lib/utils'
import { connect, type LiveSocket } from '$lib/ws'

// --- session controls ---
let providers = $state<ProviderConfig[]>([])
let primary = $state('')
let fallback = $state('')
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
// The stored transcription default is a provider/model pair: its model half
// only counts while its provider half is the one selected.
const sttDefault = $derived(primary === defaults.stt_provider ? defaults.stt_model : '')
// Seeded, not stored: a pick in the picker overrides these until the provider
// changes, and a provider switch starts over (see preferredModel).
let model = $derived(preferredModel(primaryProvider, sttDefault))
let fallbackModel = $derived(preferredModel(fallbackProvider, ''))
let diarMode = $state<DiarizationModeKind>('none')
// The picked model's catalogue entry, used only as the fallback for a model
// the capability config does not annotate. Read from the shared catalogue, so
// it is known as soon as the picker's list has loaded and stays known whether
// or not that picker is still mounted.
const primaryModelInfo = $derived(modelInfoFor(primaryProvider, 'transcribe', '', model))
const primaryKind = $derived(primaryProvider?.kind)
// Inline diarization only yields speakers for some provider+model pairs, so
// the option is offered only when the chosen model actually returns them.
const inlineAvailable = $derived(
	inlineDiarizationFor(primaryKind, model, primaryModelInfo?.inline_diarization),
)
let diarEndpoint = $state('')
// On by default: capture always fed the campaign glossary to the provider, and
// turning it off is the deliberate choice (hear the audio unbiased).
let useGlossary = $state(true)
// Some models cannot take a glossary at all (the field is simply ignored), in
// which case the checkbox is disabled and says why rather than being a silent
// no-op. No `active` features are passed: a model that refuses to combine the
// glossary with something else does not block it, it costs something, and the
// backend resolves that in the glossary's favour - see glossaryWarning.
const glossaryBlocked = $derived(featureBlockedReason(primaryKind, model, 'glossary'))
// The price of that resolution, stated where the GM decides: on a model that
// declares the conflict, switching the glossary on gives up word timestamps
// and so degrades speaker attribution in every diarization mode.
const glossaryWarning = $derived(glossaryDropsWarning(primaryKind, model))
// The mirror image: with the glossary on, a model that cannot combine the two
// keeps the inline option visible but greyed, so the reason is discoverable.
const inlineConflict = $derived(
	inlineAvailable
		? featureBlockedReason(
				primaryKind,
				model,
				'inline_diarization',
				useGlossary ? ['glossary'] : [],
			)
		: '',
)
let error = $state('')
let busy = $state(false)

// Where the bundled sherpa-onnx diarizer answers on the compose network.
// Used as the actual default, not just placeholder text - it was previously
// only a placeholder, so you had to retype the value it was already showing.
const DEFAULT_DIAR_ENDPOINT = 'http://diarization:8001'

// Validated in the UI rather than only server-side, so the message can sit
// under the field it's about instead of at the bottom of the card.
const endpointMissing = $derived(diarMode === 'remote' && !diarEndpoint.trim())

// A fallback provider needs its own model: it is a different vendor with its
// own list, so the primary's pick means nothing to it, and the API rejects the
// pair. Blank with no fallback selected is fine - the fallback is optional.
const fallbackModelMissing = $derived(!!fallback && !fallbackModel)

// Everything the advanced panel can get wrong, so the Start button and the
// collapsed summary agree about whether it is safe to press.
const startBlocked = $derived(endpointMissing || fallbackModelMissing)

// A stored default (or an earlier pick) of "inline" must not survive a switch
// to a model that returns no speakers - the backend would reject the start.
// The condition is "a model is chosen", not "its catalogue entry has arrived":
// the entry only lands once the picker's list has been opened, and until then
// the dropdown already refuses to offer inline, so waiting for it would leave
// the summary line claiming a mode the control below it no longer lists.
$effect(() => {
	if (diarMode === 'inline' && model && !inlineAvailable) diarMode = 'none'
})

// Switching to a model with no way to receive a glossary at all must not leave
// the toggle on: the terms would be dropped on the floor with the checkbox
// still ticked. This is only about that case now, not about a conflict - a
// model that refuses a combination still gets the glossary, and gives up the
// other feature instead.
$effect(() => {
	if (useGlossary && glossaryBlocked) useGlossary = false
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

const fallbackSummary = $derived.by(() => {
	if (!fallbackProvider) return 'None'
	// Named but modelless is a problem the collapsed panel must not hide: the
	// API rejects the pair, so this would otherwise read as configured while
	// the Start button stays greyed for no visible reason.
	if (!fallbackModel) return `${fallbackProvider.name} - model missing`
	return `${fallbackProvider.name} · ${fallbackModel}`
})

const diarSummary = $derived.by(() => {
	if (diarMode === 'none') return 'Off'
	if (diarMode === 'inline') return 'Inline (from STT)'
	if (endpointMissing) return 'Remote - endpoint missing'
	if ($health?.diarizer_endpoint && $health.diarizer_reachable === false) {
		return 'Remote - service not answering'
	}
	return `Remote - ${diarEndpoint}`
})

// Split out so each half of the summary line can colour itself: a missing
// fallback model is not a diarization problem and must not paint one red.
const diarProblem = $derived(
	endpointMissing ||
		(diarMode === 'remote' && !!$health?.diarizer_endpoint && $health.diarizer_reachable === false),
)

const advancedProblem = $derived(startBlocked || diarProblem)

const capturing = $derived($health?.capture_status === 'capturing')

// --- session elapsed time ---
// healthz's uptime_seconds is the app process's uptime, not the session's -
// after the first session of a process's lifetime the two drift apart for
// good. Fetch the active session's started_at instead and tick locally.
let sessionStartedAt = $state<number | null>(null) // epoch seconds
let nowMs = $state(Date.now())

// Pulled out as its own derived, and the effect below reads only this: the
// layout replaces the whole $health object on every poll, so an effect that
// touched the store directly would re-fetch the session (transcript and all)
// every few seconds for one integer. A session id is a string, and a derived
// whose value is unchanged does not propagate, so the fetch runs once per
// session instead of once per poll.
const activeSessionId = $derived(capturing ? ($health?.active_session_id ?? null) : null)

$effect(() => {
	const id = activeSessionId
	if (!id) {
		sessionStartedAt = null
		return
	}
	api
		.getSession(id)
		.then((detail) => {
			// Read the id again rather than closing over it: a slow response
			// for a session that has since ended must not clobber newer state.
			if (activeSessionId === detail.session.id) {
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

// Worse than degraded, and different in kind: every provider has failed in a
// way that repeats (no credits, rejected key), so nothing more will be
// transcribed this session. It carries the vendor's own sentence because that
// is the only part the GM can act on.
const sttError = $derived(capturing ? ($health?.stt_error ?? null) : null)

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
			model,
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
				{#if sttError}
					<p class="mt-2 border-t border-dashed pt-2 text-sm font-medium text-destructive">
						Live transcription stopped: {sttError} Audio is still being recorded, so the session can
						be re-transcribed once this is fixed.
					</p>
				{:else if sttDegradedAt}
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
							defaultModel={sttDefault}
						/>
					</div>
					<Button onclick={start} disabled={busy || !primary || !model || startBlocked}>
						Start session
					</Button>
					{#if !model && primary}
						<span class="text-xs text-muted-foreground">
							Pick a model to start - it is chosen per session, not stored on the provider.
						</span>
					{/if}
				</div>

				<div
					class="mt-3.5 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-dashed pt-3 text-xs"
				>
					<span
						class={cn('size-1.5 shrink-0 rounded-full', advancedProblem ? 'bg-destructive' : 'bg-emerald-500')}
					></span>
					<span class="text-muted-foreground">Fallback</span>
					<span class={fallbackModelMissing ? 'text-destructive' : 'text-foreground'}
						>{fallbackSummary}</span
					>
					<span class="text-muted-foreground">·</span>
					<span class="text-muted-foreground">Diarization</span>
					<span class={diarProblem ? 'text-destructive' : 'text-foreground'}>{diarSummary}</span>
					<span class="text-muted-foreground">·</span>
					<span class="text-muted-foreground">Glossary</span>
					<span class="text-foreground"
						>{useGlossary ? 'On' : glossaryBlocked ? 'Unsupported' : 'Off'}</span
					>
					<!-- The panel below is collapsed by default and the glossary is on by
					     default, so the folded summary is where most GMs will meet this. -->
					{#if useGlossary && glossaryWarning}
						<TriangleAlert class="size-3 shrink-0 text-amber-500" aria-label={glossaryWarning} />
					{/if}
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
										? [
												{
													value: 'inline',
													label: 'Inline (from STT)',
													disabled: !!inlineConflict,
													title: inlineConflict,
												},
											]
										: []),
									{ value: 'remote', label: 'Remote service' },
								]}
							/>
							{#if !inlineAvailable}
								<span class="text-xs text-muted-foreground">
									This model returns no speaker labels, so inline diarization isn't offered.
								</span>
							{:else if inlineConflict}
								<span class="text-xs text-muted-foreground">
									{inlineConflict}
									Turn the glossary off to use it.
								</span>
							{/if}
						</div>
						<div class="flex flex-col gap-2">
							<Label for="use-glossary">Glossary</Label>
							<label class="flex items-center gap-2" title={glossaryBlocked}>
								<Checkbox
									id="use-glossary"
									checked={useGlossary}
									disabled={!!glossaryBlocked}
									onCheckedChange={(v) => (useGlossary = v === true)}
								/>
								<span class={cn('text-sm', glossaryBlocked && 'text-muted-foreground')}>
									Use glossary
								</span>
								{#if glossaryWarning && !glossaryBlocked}
									<TriangleAlert
										class="size-3.5 shrink-0 text-amber-500"
										aria-label="Diarization quality warning"
									/>
								{/if}
							</label>
							<span
								class={cn(
									'text-xs',
									glossaryWarning && !glossaryBlocked ? 'text-amber-500' : 'text-muted-foreground',
								)}
							>
								{glossaryBlocked ||
									glossaryWarning ||
									"Sends the campaign's terms to the provider as keyterms or a prompt."}
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
										{#if $health.diarizer_detail}
											({$health.diarizer_detail})
										{/if}
									</span>
								{:else if $health?.diarizer_endpoint && $health.diarizer_status === 'degraded'}
									<span class="text-xs text-amber-500">
										The diarization service at {$health.diarizer_endpoint} answered but cannot serve
										right now.
										{#if $health.diarizer_detail}
											({$health.diarizer_detail})
										{/if}
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
