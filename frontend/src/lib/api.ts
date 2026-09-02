import { goto } from '$app/navigation'
import { authed } from './stores'
import type {
	ActionDefaults,
	AlertChannel,
	AlertChannelWrite,
	AlertTestResult,
	AutostartState,
	DeviceSetting,
	ExportFormat,
	Glossary,
	Health,
	InputDevice,
	ModelInfo,
	ProviderConfig,
	ProviderCreate,
	ProviderModelsRequest,
	ReprocessJob,
	ReprocessRequest,
	RevisionResponse,
	ServiceLogs,
	ServiceState,
	Session,
	SessionDetail,
	StartSessionRequest,
	SummarizeRequest,
	SummarizeResult,
	TranscriptEvent,
	UpdateResult,
	VideoGenerateRequest,
	VideoJob,
	VideoModelInfo,
} from './types'

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string,
	) {
		super(message)
		this.name = 'ApiError'
	}
}

const LOGIN_PATH = '/api/auth/login'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
	const res = await fetch(path, {
		credentials: 'same-origin',
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
		...init,
	})
	if (res.status === 401 && path !== LOGIN_PATH) {
		// Session cookie missing/expired: every authed page would otherwise throw
		// on its next call and render blank. Bounce to the login form instead
		// (skip this for the login call itself - that 401 just means "wrong
		// password" and the form shows it inline).
		authed.set(false)
		if (location.pathname !== '/login') void goto('/login')
	}
	if (!res.ok) {
		let detail = res.statusText
		try {
			const body = (await res.json()) as { detail?: string }
			if (body.detail) detail = body.detail
		} catch {
			/* non-JSON error body */
		}
		throw new ApiError(res.status, detail)
	}
	if (res.status === 204) return undefined as T
	return (await res.json()) as T
}

export const api = {
	// --- auth ---
	login: (password: string) =>
		request<{ ok: boolean }>('/api/auth/login', {
			method: 'POST',
			body: JSON.stringify({ password }),
		}),
	logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

	// --- system ---
	health: () => request<Health>('/api/system/healthz'),
	revision: () => request<RevisionResponse>('/api/system/revision'),
	update: () => request<UpdateResult>('/api/system/update', { method: 'POST' }),
	rollback: (commit: string) =>
		request<UpdateResult>('/api/system/rollback', {
			method: 'POST',
			body: JSON.stringify({ commit }),
		}),
	getDefaults: () => request<ActionDefaults>('/api/system/defaults'),
	setDefaults: (body: ActionDefaults) =>
		request<ActionDefaults>('/api/system/defaults', {
			method: 'PUT',
			body: JSON.stringify(body),
		}),
	getAutostart: () => request<AutostartState>('/api/system/autostart'),
	setAutostart: (enabled: boolean) =>
		request<AutostartState>('/api/system/autostart', {
			method: 'PUT',
			body: JSON.stringify({ enabled }),
		}),
	listAlertChannels: () => request<AlertChannel[]>('/api/system/alerts/channels'),
	createAlertChannel: (body: AlertChannelWrite) =>
		request<AlertChannel>('/api/system/alerts/channels', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	updateAlertChannel: (id: string, body: AlertChannelWrite) =>
		request<AlertChannel>(`/api/system/alerts/channels/${id}`, {
			method: 'PUT',
			body: JSON.stringify(body),
		}),
	deleteAlertChannel: (id: string) =>
		request<{ ok: boolean }>(`/api/system/alerts/channels/${id}`, { method: 'DELETE' }),
	testAlertChannel: (id: string) =>
		request<AlertTestResult>(`/api/system/alerts/channels/${id}/test`, { method: 'POST' }),

	// --- audio ---
	listDevices: () => request<InputDevice[]>('/api/audio/devices'),
	getInputDevice: () => request<DeviceSetting>('/api/audio/device'),
	setInputDevice: (device: string | null) =>
		request<{ ok: boolean }>('/api/audio/device', {
			method: 'PUT',
			body: JSON.stringify({ device }),
		}),

	// --- providers ---
	listProviders: () => request<ProviderConfig[]>('/api/providers'),
	providerModels: (body: ProviderModelsRequest) =>
		request<ModelInfo[]>('/api/providers/models', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	createProvider: (body: ProviderCreate) =>
		request<ProviderConfig>('/api/providers', { method: 'POST', body: JSON.stringify(body) }),
	updateProvider: (id: string, body: ProviderCreate) =>
		request<ProviderConfig>(`/api/providers/${id}`, {
			method: 'PUT',
			body: JSON.stringify(body),
		}),
	deleteProvider: (id: string) =>
		request<{ ok: boolean }>(`/api/providers/${id}`, { method: 'DELETE' }),
	setProviderSecret: (id: string, value: string) =>
		request<{ ok: boolean }>(`/api/providers/${id}/secret`, {
			method: 'POST',
			body: JSON.stringify({ value }),
		}),
	testProvider: (id: string) =>
		request<{ healthy: boolean }>(`/api/providers/${id}/test`, { method: 'POST' }),

	// --- glossary ---
	getGlossary: (campaign: string) => request<Glossary>(`/api/glossary/${campaign}`),
	putGlossary: (campaign: string, terms: string[]) =>
		request<Glossary>(`/api/glossary/${campaign}`, {
			method: 'PUT',
			body: JSON.stringify({ terms }),
		}),
	getDefaultGlossary: () => request<Glossary>('/api/glossary'),
	putDefaultGlossary: (terms: string[]) =>
		request<Glossary>('/api/glossary', { method: 'PUT', body: JSON.stringify({ terms }) }),

	// --- sessions ---
	startSession: (body: StartSessionRequest) =>
		request<Session>('/api/session/start', { method: 'POST', body: JSON.stringify(body) }),
	stopSession: () => request<Session>('/api/session/stop', { method: 'POST' }),
	listSessions: () => request<Session[]>('/api/session'),
	getSession: (id: string) => request<SessionDetail>(`/api/session/${id}`),
	getTranscriptVersion: (id: string, version: string) =>
		request<TranscriptEvent[]>(
			`/api/session/${id}/transcript?version=${encodeURIComponent(version)}`,
		),
	setSpeakerNames: (id: string, names: Record<string, string>) =>
		request<{ ok: boolean }>(`/api/session/${id}/speakers`, {
			method: 'PUT',
			body: JSON.stringify({ names }),
		}),
	summarizeSession: (id: string, body: SummarizeRequest) =>
		request<SummarizeResult>(`/api/session/${id}/summarize`, {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	exportUrl: (id: string, fmt: ExportFormat) => `/api/session/${id}/export?fmt=${fmt}`,
	audioUrl: (id: string) => `/api/session/${id}/audio`,
	deleteSessions: (ids: string[]) =>
		request<{ ok: boolean }>('/api/session/delete', {
			method: 'POST',
			body: JSON.stringify({ ids }),
		}),
	mergeSessions: (ids: string[]) =>
		request<Session>('/api/session/merge', { method: 'POST', body: JSON.stringify({ ids }) }),

	// --- video generation ---
	// Generation is asynchronous upstream (minutes), so enqueue returns a
	// queued job and the caller polls listVideoJobs/getVideoJob.
	videoModels: (providerId: string) =>
		request<VideoModelInfo[]>(`/api/video/models?provider_id=${encodeURIComponent(providerId)}`),
	enqueueVideo: (body: VideoGenerateRequest) =>
		request<VideoJob>('/api/video', { method: 'POST', body: JSON.stringify(body) }),
	getVideoJob: (jobId: string) => request<VideoJob>(`/api/video/${jobId}`),
	listVideoJobs: (sessionId: string) =>
		request<VideoJob[]>(`/api/video?session_id=${encodeURIComponent(sessionId)}`),
	deleteVideoJob: (jobId: string) =>
		request<{ ok: boolean }>(`/api/video/${jobId}`, { method: 'DELETE' }),
	/** Playback/download URL for a finished job (served from local storage). */
	videoContentUrl: (jobId: string) => `/api/video/${jobId}/content`,

	// --- reprocess ---
	enqueueReprocess: (body: ReprocessRequest) =>
		request<ReprocessJob>('/api/reprocess', { method: 'POST', body: JSON.stringify(body) }),
	getReprocess: (jobId: string) => request<ReprocessJob>(`/api/reprocess/${jobId}`),
	listReprocess: (sessionId: string) =>
		request<ReprocessJob[]>(`/api/reprocess?session_id=${sessionId}`),

	// --- services (docker) ---
	listServices: () => request<ServiceState[]>('/api/system/services'),
	setServiceRunning: (name: string, running: boolean) =>
		request<ServiceState>(`/api/system/services/${name}`, {
			method: 'POST',
			body: JSON.stringify({ running }),
		}),
	serviceLogs: (name: string, tail = 200) =>
		request<ServiceLogs>(`/api/system/services/${name}/logs?tail=${tail}`),
}
