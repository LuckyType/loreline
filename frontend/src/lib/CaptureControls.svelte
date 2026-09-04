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
import type { DiarizationModeKind } from '$lib/wire'
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

onMount(() => {
	// Providers, defaults and the capability gate: every seed above is derived
	// from the store, so nothing here has to wait for it.
	void actionSetup.load()
})
</script>

<Card class="shrink-0 py-4">
	<CardContent class="px-4">
		{#if capturing}
			<div class="flex items-center justify-between">
				<span class="flex items-center gap-2">
					<span class="size-2 rounded-full bg-emerald-500"></span>
					<strong>Recording</strong>
					<span class="text-muted-foreground">
						{elapsed.seconds === null ? '-' : formatTime(elapsed.seconds)}
						· {actionSetup.providers.length} providers
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
		{#if error || actionSetup.error}
			<p class="mt-2 text-sm text-destructive">{error || actionSetup.error}</p>
		{/if}
	</CardContent>
</Card>
