export type ProviderKind =
	| 'deepgram'
	| 'openai'
	| 'openai_compat'
	| 'assemblyai'
	| 'google'
	| 'vosk'
	| 'openai_chat'

export type ProtocolKind = 'ws' | 'grpc' | 'http_sse' | 'http_batch'

export type DiarizationModeKind = 'inline' | 'remote' | 'openai' | 'none'

export interface ProviderCaps {
	streaming: boolean
	inline_diarization: boolean
	vocab_param: string | null
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
	enabled?: boolean
	api_key?: string | null
}

export interface ProviderModelsRequest {
	kind: ProviderKind
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
}

export interface SummarizeRequest {
	provider_id: string
	model?: string | null
}

export interface SummarizeResult {
	summary: string
}

export interface ActionDefaults {
	stt_model: string
	diar_mode: string
	diar_endpoint: string
	summarize_model: string
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
	diarization?: DiarizationConfig
}

export type JobStatusKind = 'queued' | 'running' | 'done' | 'error'

export interface ReprocessJob {
	id: string
	session_id: string
	provider_id: string
	operation: 'transcribe' | 'diarize'
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
