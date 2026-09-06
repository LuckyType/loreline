<script lang="ts">
/**
 * The Dashboard's capture card: what a session will be started with, and,
 * once it is running, that it is running.
 *
 * Every seed here is a derivation over the shared setup store rather than
 * state of its own, so a pick overrides a stored default and a provider
 * switch starts the model over. The advanced half folds away, which is why
 * the summary line above it has to state what fallback, diarization and the
 * glossary are set to: folding something away must never hide a problem.
 */

import { ChevronDown, TriangleAlert } from '@lucide/svelte'
import { onMount } from 'svelte'
import { actionSetup } from '$lib/actionSetup.svelte'
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
import Dropdown from '$lib/Dropdown.svelte'
import { elapsedSince } from '$lib/elapsed.svelte'
import { modelInfoFor } from '$lib/modelCatalog.svelte'
import ModelPicker from '$lib/ModelPicker.svelte'
import { formatTime, health } from '$lib/stores'
import type { DiarizationModeKind, DiarizerProbe } from '$lib/wire'
import { cn } from '$lib/utils'

// Provider rows, stored defaults and the capability gate all come from one
// store, loaded together, so every seed below is computed from a gated list
// or not at all. Live capture only: LLM providers can't transcribe, and
// OpenRouter's STT has no streaming mode so it is re-processing-only.
const sttProviders = $derived(actionSetup.providersFor('capture'))
// Seeded, not stored: the configured default while it can drive a capture,
// else the first row that can. A pick overrides it.
let primary = $derived(actionSetup.preferredProvider('capture')?.id ?? '')
let fallback = $state('')
const primaryProvider = $derived(actionSetup.provider(primary))
const fallbackProvider = $derived(actionSetup.provider(fallback))
// The stored transcription default is a provider/model pair: its model half
// only counts while its provider half is the one selected.
const sttDefault = $derived(actionSetup.pairedDefault('capture', primaryProvider))
// Seeded, not stored: a pick in the picker overrides these until the provider
// changes, and a provider switch starts over (see preferredModel).
let model = $derived(actionSetup.preferredModelFor('capture', primaryProvider))
let fallbackModel = $derived(preferredModel(fallbackProvider, ''))
// The stored mode, seeded the same way: a pick overrides it.
let diarMode = $derived(actionSetup.defaults.diar_mode as DiarizationModeKind)
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
// Where the bundled sherpa-onnx diarizer answers on the compose network.
// Used as the actual default, not just placeholder text - it was previously
// only a placeholder, so you had to retype the value it was already showing.
const DEFAULT_DIAR_ENDPOINT = 'http://diarization:8001'
// The stored endpoint. Saved defaults can say "remote" without one: fill in
// the bundled service rather than showing an empty box next to a placeholder
// the user would have to copy out by hand. An edit overrides it.
let diarEndpoint = $derived(
	actionSetup.defaults.diar_endpoint ||
		(actionSetup.defaults.diar_mode === 'remote' ? DEFAULT_DIAR_ENDPOINT : ''),
)
// An on-demand probe of whatever diarEndpoint holds right now, not the stored
// default: $health's diarizer_* fields only ever grade defaults.diar_endpoint,
// so typing a different endpoint here used to get neither a warning when it's
// down nor credit when it's up. diarProbedEndpoint records which endpoint the
// verdict is about, so a slow or stale answer can never be shown against a
// value that isn't on screen any more - see diarProbeCurrent below.
let diarProbe = $state<DiarizerProbe | null>(null)
let diarProbedEndpoint = $state('')
const diarProbeCurrent = $derived(
	diarMode === 'remote' && diarProbedEndpoint === diarEndpoint.trim(),
)
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

// Debounced live probe of the remote diarization endpoint: fires shortly
// after diarEndpoint (or diarMode) settles rather than on every keystroke, so
// typing a URL doesn't hammer the server or the service behind it. Runs
// whenever remote diarization is selected, whether or not the advanced panel
// happens to be open - the collapsed summary needs the same live answer the
// panel does, since folding it away must never hide a problem.
const DIAR_PROBE_DEBOUNCE_MS = 500

$effect(() => {
	const endpoint = diarEndpoint.trim()
	if (diarMode !== 'remote' || !endpoint) {
		diarProbe = null
		diarProbedEndpoint = ''
		return
	}
	const timer = setTimeout(async () => {
		try {
			const result = await api.probeDiarizerEndpoint(endpoint)
			// Read the field again rather than closing over it: a slow answer
			// must not paint a verdict about an endpoint no longer shown.
			if (diarEndpoint.trim() === endpoint) {
				diarProbe = result
				diarProbedEndpoint = endpoint
			}
		} catch {
			// The probe call itself failing (our own backend hiccuping, an
			// expired session) says nothing about the endpoint, so this
			// clears to "unknown" rather than painting it red.
			if (diarEndpoint.trim() === endpoint) {
				diarProbe = null
				diarProbedEndpoint = ''
			}
		}
	}, DIAR_PROBE_DEBOUNCE_MS)
	return () => clearTimeout(timer)
})

function setDiarMode(mode: string) {
	diarMode = mode as DiarizationModeKind
	// Switching to remote with nothing configured: offer the bundled service
	// rather than an empty box the user has to fill from the placeholder.
	if (mode === 'remote' && !diarEndpoint.trim()) {
		diarEndpoint = actionSetup.defaults.diar_endpoint || DEFAULT_DIAR_ENDPOINT
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
	if (diarProbeCurrent && diarProbe && !diarProbe.reachable) {
		return 'Remote - service not answering'
	}
	return `Remote - ${diarEndpoint}`
})

// Split out so each half of the summary line can colour itself: a missing
// fallback model is not a diarization problem and must not paint one red.
const diarProblem = $derived(
	endpointMissing || (diarProbeCurrent && !!diarProbe && !diarProbe.reachable),
)

const advancedProblem = $derived(startBlocked || diarProblem)

const capturing = $derived($health?.capture_status === 'capturing')

// --- session elapsed time ---
// healthz's uptime_seconds is the app process's uptime, not the session's -
// after the first session of a process's lifetime the two drift apart for
// good. Fetch the active session's started_at instead and tick locally.
let sessionStartedAt = $state<number | null>(null) // epoch seconds

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

const elapsed = elapsedSince(() => sessionStartedAt)

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

// --- is audio actually arriving? ---
// "Capturing" only says the machinery started. A microphone that opens and
// then delivers nothing looks exactly the same from here, which is how a
// session can tick away for an evening over a recording that never grew. These
// two say what is really happening: how much audio has been written, and how
// long ago the last frame arrived. A device sends frames whether or not anyone
// is speaking, so an age that keeps climbing is the device, not the table.
const capturedSeconds = $derived(capturing ? ($health?.captured_seconds ?? null) : null)
const lastFrameAge = $derived(capturing ? ($health?.capture_last_frame_age ?? null) : null)
// Frames arrive every 20ms, and health reports the age at the moment it is
// asked, so anything past a couple of seconds is a stopped microphone rather
// than poll jitter.
const AUDIO_STALL_S = 3
const audioStalled = $derived(lastFrameAge !== null && lastFrameAge >= AUDIO_STALL_S)
const audioSummary = $derived.by(() => {
	if (audioStalled) return `no audio for ${Math.round(lastFrameAge ?? 0)}s`
	if (capturedSeconds === null) return 'waiting for audio'
	return `${formatTime(Math.floor(capturedSeconds))} recorded`
})

// --- a session that ended badly ---
// Stop answers with the finished session, and a capture that died answers
// nothing at all: the health poll simply stops saying "capturing". Either way
// the GM has to be told, because the card otherwise just resets to the start
// form as though the evening had gone fine.
let endedError = $state('')
let endedSessionId = $state('')
// Held while POST /api/session/stop is in flight. The backend drains the
// transcription queue first (up to half a minute on a slow provider), and the
// card must not look idle while that happens.
let stopping = $state(false)
// Plain locals, deliberately not $state: bookkeeping for the effect below, not
// anything that gets rendered.
let wasCapturing = false
let endingSessionId: string | null = null
let stoppedByUs = false

$effect(() => {
	const id = activeSessionId
	if (capturing) {
		wasCapturing = true
		endingSessionId = id
		return
	}
	if (!wasCapturing) return
	wasCapturing = false
	const ended = endingSessionId
	endingSessionId = null
	// Our own Stop reports its own outcome from the response it got, which is
	// both faster and surer than re-fetching the session.
	if (stoppedByUs) {
		stoppedByUs = false
		return
	}
	if (ended) void reportIfFailed(ended)
})

async function reportIfFailed(id: string) {
	try {
		const detail = await api.getSession(id)
		if (detail.session.status === 'error') showEnded(id)
	} catch {
		/* the session page still has the truth; nothing to gain from a second banner */
	}
}

function showEnded(id: string) {
	endedSessionId = id
	endedError =
		'The recording stopped before it was finished and the session ended with an error. ' +
		'Whatever was captured up to that point is saved.'
}

async function start() {
	busy = true
	error = ''
	endedError = ''
	endedSessionId = ''
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
	stopping = true
	stoppedByUs = true
	error = ''
	endedError = ''
	const ending = activeSessionId
	try {
		const finished = await api.stopSession()
		// The response carries the finished session, so a stop that finalized as
		// an error says so here instead of resetting to the start form as if
		// nothing had happened.
		if (finished.status === 'error') showEnded(finished.id)
	} catch (err) {
		// 409 means the session had already ended itself (a dead microphone gets
		// finalized without waiting for Stop), so this is that outcome to
		// report, not a Stop that failed.
		if (err instanceof ApiError && err.status === 409 && ending) {
			await reportIfFailed(ending)
		} else {
			error = err instanceof ApiError ? err.message : 'failed to stop'
		}
	} finally {
		await refresh()
		busy = false
		stopping = false
	}
}

async function refresh() {
	health.set(await api.health())
}

onMount(() => {
	// Providers, defaults and the capability gate: every seed above is derived
	// from the store, so nothing here has to wait for it.
	void actionSetup.load()
})
</script>

<Card class="shrink-0 py-4">
	<CardContent class="px-4">
		{#if capturing || stopping}
			<div class="flex items-center justify-between">
				<span class="flex items-center gap-2">
					<span
						class={cn(
							'size-2 rounded-full',
							stopping ? 'bg-amber-500' : audioStalled ? 'bg-destructive' : 'bg-emerald-500',
						)}
					></span>
					<strong>{stopping ? 'Finalizing' : 'Recording'}</strong>
					<span class="text-muted-foreground">
						{elapsed.seconds === null ? '-' : formatTime(elapsed.seconds)}
						· {actionSetup.providers.length} providers
						{#if capturing}
							· <span class={audioStalled ? 'text-destructive' : ''}>{audioSummary}</span>
						{/if}
					</span>
				</span>
				<Button variant="destructive" onclick={stop} disabled={busy}>
					{stopping ? 'Finalizing…' : 'Stop session'}
				</Button>
			</div>
			{#if stopping}
				<p class="mt-2 border-t border-dashed pt-2 text-sm text-muted-foreground">
					Transcribing what is still queued and closing the recording. This can take up to half a
					minute; the audio is already on disk.
				</p>
			{:else if audioStalled}
				<p class="mt-2 border-t border-dashed pt-2 text-sm font-medium text-destructive">
					No audio has reached the recorder for {Math.round(lastFrameAge ?? 0)} seconds. A
					microphone sends frames even in a silent room, so this is the device rather than the table
					- stop the session, pick another input in Settings, and start again.
				</p>
			{:else if sttError}
				<p class="mt-2 border-t border-dashed pt-2 text-sm font-medium text-destructive">
					Live transcription stopped: {sttError} Audio is still being recorded, so the session can
					be re-transcribed once this is fixed.
				</p>
			{:else if sttDegradedAt}
				<p class="mt-2 border-t border-dashed pt-2 text-sm text-amber-500">
					Live transcription has been failing since {sttDegradedAt} - audio is still being recorded
					and the session can be re-transcribed later.
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
						defaultValue={actionSetup.defaults.stt_provider}
						options={sttProviders.map((p) => ({ value: p.id, label: p.name }))}
						placeholder={actionSetup.ready ? 'Select provider…' : 'Loading providers…'}
						loading={!actionSetup.ready}
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
							{:else if diarProbeCurrent && diarProbe && !diarProbe.reachable}
								<span class="text-xs text-amber-500">
									No diarization service answered at {diarEndpoint}.
									{#if diarProbe.detail}
										({diarProbe.detail})
									{/if}
								</span>
							{:else if diarProbeCurrent && diarProbe?.status === 'degraded'}
								<span class="text-xs text-amber-500">
									The diarization service at {diarEndpoint} answered but cannot serve right now.
									{#if diarProbe.detail}
										({diarProbe.detail})
									{/if}
								</span>
							{/if}
						</div>
					{/if}
				</div>
			{/if}
		{/if}
		<!-- Outside the capturing/idle split on purpose: this is about the session
		     that just ended, so it has to survive the card resetting to the start
		     form - which is precisely what used to hide it. -->
		{#if endedError}
			<p class="mt-2 border-t border-dashed pt-2 text-sm font-medium text-destructive">
				{endedError}
				<a class="underline underline-offset-2" href="/sessions/{endedSessionId}">
					Open the session
				</a>
				<button
					type="button"
					class="ml-2 underline underline-offset-2 text-muted-foreground"
					onclick={() => (endedError = '')}
				>
					Dismiss
				</button>
			</p>
		{/if}
		{#if error || actionSetup.error}
			<p class="mt-2 text-sm text-destructive">{error || actionSetup.error}</p>
		{/if}
	</CardContent>
</Card>
