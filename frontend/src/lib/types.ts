import { capabilities, hasRealtimeTranscription } from './capabilities.svelte'

export type ProviderKind =
	| 'deepgram'
	| 'openai'
	| 'openai_compat'
	| 'assemblyai'
	| 'gemini'
	| 'openrouter'

/** What a provider is being asked to do. Providers are not interchangeable
 *  across these - mirrors `Interaction` in src/loreline/models.py. */
export type Interaction = 'transcribe' | 'summarize' | 'video'

// --- capability config (GET /api/capabilities) ------------------------------
//
// The wire shape of src/loreline/capabilities.yaml, which is the single source
// of truth for which provider+model combinations exist and what each can do.
// These are types only: the fetched data lives in $lib/capabilities.svelte,
// and the helper functions at the bottom of this file read it from there.

export type Hosting = 'cloud' | 'selfhosted'

/** 'optional' is a self-hosted server that may or may not check a key. */
export type AuthKind = 'api_key' | 'optional' | 'none'

export type LanguageSupport = 'single' | 'multi' | 'codeswitch'

/** The transcription toggles a conflict group may name. */
export type ConflictFeature = 'glossary' | 'inline_diarization' | 'word_timestamps'

/** Whether keyword biasing exists, and what the vendor calls the field.
 *  `supported: false` is what disables the "Use glossary" checkbox instead of
 *  leaving a control that silently does nothing. */
export interface GlossarySupport {
	supported: boolean
	field: string | null
	/** Term count ceiling; null where the vendor documents none. */
	max_terms: number | null
	/** Token budget across the whole list. */
	max_tokens: number | null
	/** Per-term character limit. */
	max_term_chars: number | null
	/** Override applying only to the streaming transport. */
	max_terms_realtime: number | null
}

export interface TranscribeCapabilities {
	/** Streams within an utterance - also what gates a model for live capture. */
	realtime: boolean
	batch: boolean
	inline_diarization: boolean
	glossary: GlossarySupport
	word_timestamps: boolean
	languages: LanguageSupport
	language_codes: string[]
	/** Groups of features that may not be enabled together: Gemini rejects a
	 *  request outright when a custom vocabulary arrives alongside diarization
	 *  or word timestamps. */
	conflicts: ConflictFeature[][]
}

export interface ReasoningSupport {
	supported: boolean
	/** The model refuses to have reasoning turned off, so never offer 'none'. */
	mandatory: boolean
	/** Empty with `supported` true means the model reasons but exposes no
	 *  levels - show no dropdown rather than an empty one. */
	efforts: string[]
}

export interface LlmCapabilities {
	reasoning: ReasoningSupport
	context_length: number | null
	max_output_tokens: number | null
	system_prompt: boolean
	temperature: boolean
}

export interface VideoCapabilities {
	/** Seconds. */
	durations: number[]
	resolutions: string[]
	aspect_ratios: string[]
	/** Null where the vendor publishes no answer, which is not "no audio". */
	audio: boolean | null
	image_input: boolean
	prompt_max_chars: number | null
	prompt_max_tokens: number | null
}

/** One curated model. A capability block is null for an interaction it does
 *  not serve. */
export interface ModelSpec {
	id: string
	label: string | null
	interactions: Interaction[]
	/** Present but not offered: the release gate for a connector that is
	 *  written but unverified. No picker may ever list one. */
	hidden: boolean
	/** The model this config vouches for on this kind, for each interaction it
	 *  declares, when a connector must name one and nobody chose. Server-side
	 *  only: the pickers here seed from the action default and then the
	 *  provider's favourites, and deliberately do not fall back to this - a
	 *  default that is right for a health probe is not a recommendation. */
	default: boolean
	/** Vendor-announced sunset, ISO date. Warned about, never hidden. */
	deprecated: string | null
	transcribe: TranscribeCapabilities | null
	llm: LlmCapabilities | null
	video: VideoCapabilities | null
}

/** Capabilities for models matched by glob - what a self-hosted endpoint's
 *  catalogue gets, since nobody can enumerate it. */
export interface ModelPattern {
	match: string
	interactions: Interaction[]
	transcribe: TranscribeCapabilities | null
	llm: LlmCapabilities | null
	video: VideoCapabilities | null
}

export type ModelAnnotation = ModelSpec | ModelPattern

export interface ProviderSpec {
	label: string
	hosting: Hosting
	auth: AuthKind
	key_url: string | null
	/** Null means the operator must supply one (self-hosted). */
	base_url: string | null
	/** A string when one endpoint serves every interaction, a mapping when the
	 *  vendor splits its catalogue, null when the config is the catalogue. */
	catalog_endpoint: string | Partial<Record<Interaction, string>> | null
	/** False for a provider allowed for stored audio but never a live session. */
	live_capture: boolean
	interactions: Interaction[]
	models: ModelSpec[]
	model_patterns: ModelPattern[]
}

export interface CapabilityConfig {
	version: number
	/** Substrings that mark a model id as a transcription model. */
	transcribe_name_markers: string[]
	realtime_name_markers: string[]
	providers: Partial<Record<ProviderKind, ProviderSpec>>
}

// --- capability helpers -----------------------------------------------------
//
// Thin wrappers over the fetched config. Every one of them answers "unknown"
// permissively: if the config never arrived, the UI offers everything and says
// so, rather than hiding controls an operator needs.

/** The capability badges shown for a provider, in a stable order. */
export function capabilityBadges(p: { kind: ProviderKind }): string[] {
	// Badges describe rather than gate, so with no config they say nothing
	// instead of guessing. Nothing is hidden by an empty list; the controls
	// themselves stay permissive.
	if (!capabilities.config) return []
	const badges: string[] = []
	if (supportsInteraction(p, 'transcribe')) {
		badges.push(hasRealtimeTranscription(p.kind) ? 'Realtime' : 'Batch')
	}
	if (supportsInteraction(p, 'summarize')) badges.push('Summarizing')
	if (supportsInteraction(p, 'video')) badges.push('Video')
	return badges
}

export function supportsInteraction(p: { kind: ProviderKind }, interaction: Interaction): boolean {
	const spec = capabilities.provider(p.kind)
	// No config (or a kind it has never heard of): allow it. The backend still
	// refuses a combination it cannot serve, and an error beats a provider that
	// silently vanished from every picker.
	if (!spec) return true
	return spec.interactions.includes(interaction)
}

/** Providers offerable for one interaction. */
export function providersFor<T extends { kind: ProviderKind }>(
	providers: T[],
	interaction: Interaction,
): T[] {
	return providers.filter((p) => supportsInteraction(p, interaction))
}

/** Providers that can drive a *live* capture (excludes re-process-only STT -
 *  OpenRouter's transcription has no streaming mode). */
export function liveSttProviders<T extends { kind: ProviderKind }>(providers: T[]): T[] {
	return providersFor(providers, 'transcribe').filter(
		(p) => capabilities.provider(p.kind)?.live_capture !== false,
	)
}

/** True for a provider that summarizes - negate it to select the STT ones. */
export function isLlmProvider(p: { kind: ProviderKind }): boolean {
	return supportsInteraction(p, 'summarize')
}

export type ProtocolKind = 'ws' | 'grpc' | 'http_sse' | 'http_batch'

export type DiarizationModeKind = 'inline' | 'remote' | 'openai' | 'none'

export interface ProviderCaps {
	streaming: boolean
	inline_diarization: boolean
	vocab_param: string | null
}

/** A model's price, in USD per **million** tokens (the backend scales
 *  OpenRouter's per-token figures - see ModelPrice in src/loreline/models.py). */
export interface ModelPrice {
	/** Input. */
	prompt: number | null
	/** Output. */
	completion: number | null
	/** Set only on a tier that applies above this prompt length. */
	min_prompt_tokens: number | null
}

/** One entry in a provider's model list. Only `id` is ever guaranteed -
 *  curated catalogs and plain OpenAI `/models` rows carry nothing else. */
export interface ModelInfo {
	id: string
	context_length: number | null
	/** Whether the model works with a streaming connector - a property of the
	 *  model AND the transport. Null where the provider draws no distinction. */
	realtime?: boolean | null
	/** Whether "Inline (from STT)" yields real speakers for this model. The
	 *  diarization pickers hide that mode when false. */
	inline_diarization?: boolean
	/** Whether the model accepts a reasoning-effort setting. */
	supports_reasoning?: boolean
	pricing: ModelPrice | null
	/** Prices that take over above a prompt-length threshold; usually empty. */
	price_tiers: ModelPrice[]
}

/**
 * OpenRouter provider-routing preferences (OpenRouter kind only) - OpenRouter
 * fans one model id out across upstream providers that differ in price, speed
 * and data policy. Mirrors `OpenRouterRouting` in src/loreline/models.py; the
 * backend sends only the fields moved off their default.
 */
export interface OpenRouterRouting {
	/** null = OpenRouter's own balanced default; 'price' = cheapest first. */
	sort: 'price' | 'throughput' | 'latency' | null
	/** 'deny' excludes providers that may store or train on the prompt. */
	data_collection: 'allow' | 'deny'
	/** Restrict to endpoints under a Zero Data Retention agreement. */
	zdr: boolean
}

export interface ProviderConfig {
	id: string
	name: string
	kind: ProviderKind
	base_url: string | null
	auth_ref: string | null
	protocol: ProtocolKind
	/** No `model`: a row serves every interaction its kind declares, so one
	 *  stored model cannot be right for all of them. Each picker chooses per
	 *  request; `favorite_models` is the row's shortlist, not its choice. */
	favorite_models: string[]
	sample_rate: number
	language: string
	capabilities: ProviderCaps
	routing?: OpenRouterRouting | null
	enabled: boolean
	secret_set?: boolean
	secret_hint?: string | null
}

export interface ProviderCreate {
	name: string
	kind: ProviderKind
	protocol: ProtocolKind
	base_url?: string | null
	favorite_models?: string[]
	sample_rate?: number
	language?: string
	routing?: OpenRouterRouting | null
	enabled?: boolean
	api_key?: string | null
}

export interface ProviderModelsRequest {
	kind: ProviderKind
	/** Scopes the returned models to what can actually serve this interaction. */
	interaction?: Interaction
	base_url?: string | null
	api_key?: string | null
	provider_id?: string | null
}

export interface DiarizationConfig {
	mode: DiarizationModeKind
	endpoint: string | null
	min_speakers: number | null
	max_speakers: number | null
}

export interface Word {
	text: string
	start: number
	end: number
	confidence: number | null
	speaker: string | null
}

export interface TranscriptEvent {
	session_id: string
	source: string
	text: string
	words: Word[]
	speaker: string | null
	start_ts: number
	end_ts: number
	is_final: boolean
}

export interface Glossary {
	campaign_id: string
	terms: string[]
}

export type SessionStatusKind = 'idle' | 'capturing' | 'stopping' | 'completed' | 'error'

export interface Session {
	id: string
	status: SessionStatusKind
	started_at: number
	started_mono: number
	ended_at: number | null
	campaign_id: string | null
	primary_provider: string | null
	fallback_provider: string | null
	diarization: DiarizationConfig
	audio_path: string | null
	speaker_names: Record<string, string>
	summary: string | null
	summary_provider?: string | null
	summary_model?: string | null
}

export interface SummarizeRequest {
	provider_id: string
	/** Required by the API, and recorded as the summary's model. */
	model: string
	/** Only meaningful for a model whose ModelInfo.supports_reasoning is true. */
	reasoning_effort?: string | null
}

export interface SummarizeResult {
	summary: string
}

export interface ActionDefaults {
	stt_provider?: string
	summarize_provider?: string
	stt_model: string
	diar_mode: string
	diar_endpoint: string
	summarize_model: string
	/** Summary system prompt; the server serves the built-in default when unset. */
	summarize_prompt?: string
	summarize_reasoning_effort?: string
	video_provider?: string
	video_model?: string
	/** Hide models that don't look capable of the interaction being picked for.
	 *  On by default; turn it off to see everything an endpoint offers. */
	strict_model_filtering?: boolean
}

export interface SessionDetail {
	session: Session
	transcript: TranscriptEvent[]
}

export interface Health {
	status: string
	version: string
	uptime_seconds: number
	capture_status: SessionStatusKind
	active_session_id?: string | null
	disk_free_bytes?: number
	disk_total_bytes?: number
	alerts_enabled?: boolean
	diarizer_endpoint?: string | null
	diarizer_reachable?: boolean | null
	/** Epoch seconds live transcription started failing; null when healthy/idle. */
	stt_degraded_since?: number | null
	/** Vendor's own reason live transcription stopped for good; null while any provider works. */
	stt_error?: string | null
}

export interface InputDevice {
	index: number
	name: string
	channels: number
	default_samplerate: number
}

export interface AudioLevel {
	peak?: number
	rms?: number
	error?: string
}

export interface DeviceSetting {
	device: string | null
}

export interface StartSessionRequest {
	primary_provider: string
	fallback_provider?: string | null
	campaign_id?: string | null
	device?: number | string | null
	/** Required by the API: nothing else decides which model transcribes. */
	model: string
	/** Required as soon as a fallback provider is named - it has its own model
	 *  list, so the primary's choice means nothing to it. */
	fallback_model?: string | null
	diarization?: DiarizationConfig
	use_glossary?: boolean
}

export type JobStatusKind = 'queued' | 'running' | 'done' | 'error'

export interface ReprocessJob {
	id: string
	session_id: string
	provider_id: string
	operation: 'transcribe' | 'diarize'
	model?: string | null
	target?: string
	use_glossary?: boolean
	diarization: DiarizationConfig
	status: JobStatusKind
	created_at: number
	started_at?: number | null
	finished_at?: number | null
	/** Segments written so far, updated while the job runs (not only at the
	 *  end), so a refresh or a second browser sees the same number. It is not a
	 *  completion ratio: models segment the same audio differently. */
	segments_added: number
	error?: string | null
}

export interface ReprocessRequest {
	session_id: string
	provider_id?: string
	operation?: 'transcribe' | 'diarize'
	diarization?: DiarizationConfig
	/** Required by the API for a "transcribe" job, ignored for "diarize". */
	model?: string | null
	target?: string
	use_glossary?: boolean
}

/**
 * A video-generation model and the parameters it actually accepts.
 *
 * Every video model takes a different subset, so the generate dialog builds
 * its controls from this rather than offering one fixed set. A `null` list
 * means the model does not take that parameter at all - which is not the same
 * as an empty list, hence nullable rather than defaulting to [].
 */
export interface VideoModelInfo {
	id: string
	name: string
	description?: string | null
	/** Seconds. */
	supported_durations?: number[] | null
	supported_resolutions?: string[] | null
	supported_aspect_ratios?: string[] | null
	supported_sizes?: string[] | null
	generate_audio: boolean
	seed: boolean
}

/** One video generation, from enqueue to a playable file. Generation takes
 *  minutes, so this is polled while `status` is queued/running. */
export interface VideoJob {
	id: string
	session_id: string
	provider_id: string
	model: string
	prompt: string
	duration?: number | null
	resolution?: string | null
	aspect_ratio?: string | null
	generate_audio: boolean
	seed?: number | null
	status: JobStatusKind
	remote_id?: string | null
	video_path?: string | null
	created_at: number
	started_at?: number | null
	finished_at?: number | null
	error?: string | null
}

export interface VideoGenerateRequest {
	session_id: string
	provider_id: string
	model: string
	prompt: string
	duration?: number | null
	resolution?: string | null
	aspect_ratio?: string | null
	generate_audio?: boolean
	seed?: number | null
}

export type ExportFormat = 'txt' | 'md' | 'srt' | 'vtt' | 'json'

// --- system / monitoring (M11) ---

export type AlertLevelKind = 'info' | 'warning' | 'error'
export type AlertChannelKind = 'ntfy' | 'telegram' | 'webhook'

export interface AlertChannel {
	id: string
	type: AlertChannelKind
	enabled: boolean
	min_level: AlertLevelKind
	server: string
	topic: string | null
	chat_id: string | null
	url: string | null
	token_set: boolean
}

export interface AlertChannelWrite {
	type: AlertChannelKind
	enabled: boolean
	min_level: AlertLevelKind
	server?: string
	topic?: string | null
	chat_id?: string | null
	url?: string | null
	token?: string | null
}

export interface AlertTestResult {
	ok: boolean
}

/**
 * How far a provider got when the Test button asked it a cheap question.
 *
 * Mirrors loreline.health.HealthStatus. Not a boolean, because "healthy" is at
 * least three separate facts: the endpoint answers, the credential is
 * accepted, and the vendor is not currently refusing. This page used to render
 * a provider with a completely invalid key exactly like one whose base URL was
 * a typo, which are opposite fixes.
 *
 * - `healthy`      answered and accepted the credential
 * - `degraded`     answered but is rate limiting or erroring; the key is fine
 * - `unauthorized` reached it; the key is missing, wrong, or lacks access
 * - `unreachable`  no answer, or nothing that API lives at this URL
 * - `unknown`      the probe ran and decided nothing; explicitly not a failure
 */
export type HealthStatus = 'healthy' | 'degraded' | 'unauthorized' | 'unreachable' | 'unknown'

export interface ProviderTestResult {
	status: HealthStatus
	/** The vendor's own words, where it gave any. Shown as the badge tooltip. */
	detail: string | null
}

export interface UpdateResult {
	ok: boolean
	previous_commit: string | null
	new_commit: string | null
	returncode: number
	output: string
}

export interface AutostartState {
	enabled: boolean
}

export interface RevisionResponse {
	commit: string | null
}

export interface ServiceState {
	name: string
	container_id: string
	state: string
	status: string
	image: string
	controllable: boolean
}

export interface ServiceLogs {
	name: string
	logs: string
}

/** One transcript version's stored log file (see /api/session/{id}/logs). */
export interface VersionLogs {
	session_id: string
	version: string
	logs: string
}
