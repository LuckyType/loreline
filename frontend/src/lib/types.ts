export type ProviderKind =
	| 'deepgram'
	| 'openai'
	| 'openai_compat'
	| 'assemblyai'
	| 'gemini'
	| 'vosk'
	| 'openai_chat'
	| 'openrouter'
	| 'openrouter_stt'

/** What a provider is being asked to do. Providers are not interchangeable
 *  across these - mirrors `Interaction` in src/loreline/models.py. */
export type Interaction = 'transcribe' | 'summarize' | 'video'

/**
 * Which interactions each provider kind can serve - the TS mirror of
 * `INTERACTIONS_BY_KIND` in src/loreline/capabilities.py. Keep the two in step:
 * the backend rejects a combination this table would offer, and the UI hides
 * one this table omits.
 */
export const INTERACTIONS_BY_KIND: Record<ProviderKind, Interaction[]> = {
	deepgram: ['transcribe'],
	openai: ['transcribe'],
	openai_compat: ['transcribe'],
	assemblyai: ['transcribe'],
	gemini: ['transcribe'],
	vosk: ['transcribe'],
	openai_chat: ['summarize'],
	openrouter: ['summarize', 'video'],
	openrouter_stt: ['transcribe'],
}

/** Kinds that transcribe stored audio but must never drive a live capture -
 *  OpenRouter's STT has no streaming mode. Mirrors `LIVE_CAPTURE_EXCLUDED`. */
export const LIVE_CAPTURE_EXCLUDED: ProviderKind[] = ['openrouter_stt']

export function supportsInteraction(p: { kind: ProviderKind }, interaction: Interaction): boolean {
	return (INTERACTIONS_BY_KIND[p.kind] ?? []).includes(interaction)
}

/** Providers offerable for one interaction. */
export function providersFor<T extends { kind: ProviderKind }>(
	providers: T[],
	interaction: Interaction,
): T[] {
	return providers.filter((p) => supportsInteraction(p, interaction))
}

/** Providers that can drive a *live* capture (excludes re-process-only STT). */
export function liveSttProviders<T extends { kind: ProviderKind }>(providers: T[]): T[] {
	return providersFor(providers, 'transcribe').filter(
		(p) => !LIVE_CAPTURE_EXCLUDED.includes(p.kind),
	)
}

/** Kinds that summarize (chat-completions); everything else transcribes. */
export const LLM_KINDS: ProviderKind[] = ['openai_chat', 'openrouter']

/** True for an LLM provider - negate it to select the STT ones. */
export function isLlmProvider(p: { kind: ProviderKind }): boolean {
	return LLM_KINDS.includes(p.kind)
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
/** Reasoning-effort levels, in picker order. Mirrors REASONING_EFFORTS in
 *  src/loreline/llm.py. */
export const REASONING_EFFORTS = [
	'none',
	'minimal',
	'low',
	'medium',
	'high',
	'xhigh',
	'max',
] as const

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
	model: string | null
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
	model?: string | null
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
	model?: string | null
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
	model?: string | null
	fallback_model?: string | null
	diarization?: DiarizationConfig
}

export type JobStatusKind = 'queued' | 'running' | 'done' | 'error'

export interface ReprocessJob {
	id: string
	session_id: string
	provider_id: string
	operation: 'transcribe' | 'diarize'
	model?: string | null
	target?: string
	diarization: DiarizationConfig
	status: JobStatusKind
	created_at: number
	started_at?: number | null
	finished_at?: number | null
	segments_added: number
	error?: string | null
}

export interface ReprocessRequest {
	session_id: string
	provider_id?: string
	operation?: 'transcribe' | 'diarize'
	diarization?: DiarizationConfig
	model?: string | null
	target?: string
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
