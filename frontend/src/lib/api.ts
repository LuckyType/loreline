import { goto } from '$app/navigation'
import { loginUrlWithNext } from './loginRedirect'
import { authed } from './stores'
import type { ExportFormat, ImportTranscribeOptions } from './types'
import type {
	ActionDefaults,
	AlertChannel,
	AlertChannelWrite,
	AlertTestResult,
	AutostartState,
	Campaign,
	CampaignDocument,
	CampaignEntities,
	CampaignSummary,
	CampaignWrite,
	CapabilityConfig,
	DeviceSetting,
	DiarizerProbe,
	GenerateRequest,
	Glossary,
	Health,
	ImportedSession,
	InputDevice,
	ModelInfo,
	OkResponse,
	ProviderConfig,
	ProviderCreate,
	ProviderModelsRequest,
	PreviouslyOnRequest,
	ProviderTestResult,
	ReprocessJob,
	ReprocessRequest,
	RevisionResponse,
	SearchResults,
	ServiceLogs,
	ServiceState,
	Session,
	SessionDetail,
	SessionDocument,
	SessionExtraction,
	StartSessionRequest,
	SummarizeRequest,
	SummarizeResult,
	TranscriptEvent,
	UpdateResult,
	VersionLogs,
	VideoGenerateRequest,
	VideoJob,
	VideoModelInfo,
} from './wire'

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
		// Carry the page that was being asked for, so signing in finishes the
		// journey instead of dumping the visitor on the Dashboard.
		if (location.pathname !== '/login') {
			void goto(loginUrlWithNext(location.pathname + location.search))
		}
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

/** What one upload reports while it is in flight. */
export interface UploadProgress {
	sent: number
	total: number
}

/** POST a multipart body, reporting how much of it has gone out.
 *
 * XMLHttpRequest rather than fetch, for one reason: an upload progress bar.
 * `fetch` exposes no equivalent of `xhr.upload.onprogress`, and a recording is
 * large enough that a button which merely says "Importing…" for four minutes
 * reads as a hang. Everything else matches `request` above - the same
 * same-origin cookie, the same 401 bounce to the login form, the same
 * `ApiError` carrying the server's own sentence.
 */
function upload<T>(
	path: string,
	body: FormData,
	options: { onProgress?: (progress: UploadProgress) => void; signal?: AbortSignal } = {},
): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const xhr = new XMLHttpRequest()
		xhr.open('POST', path)
		xhr.upload.onprogress = (event) => {
			if (event.lengthComputable) options.onProgress?.({ sent: event.loaded, total: event.total })
		}
		xhr.onload = () => {
			if (xhr.status === 401) {
				authed.set(false)
				if (location.pathname !== '/login') {
					void goto(loginUrlWithNext(location.pathname + location.search))
				}
			}
			if (xhr.status >= 200 && xhr.status < 300) {
				resolve(JSON.parse(xhr.responseText) as T)
				return
			}
			let detail = xhr.statusText
			try {
				const parsed = JSON.parse(xhr.responseText) as { detail?: string }
				if (parsed.detail) detail = parsed.detail
			} catch {
				/* non-JSON error body */
			}
			reject(new ApiError(xhr.status, detail))
		}
		// Status 0 is what the browser reports for a dropped connection and for
		// an abort alike; neither has a server sentence to quote.
		xhr.onerror = () => reject(new ApiError(0, 'the upload could not reach the server'))
		xhr.onabort = () => reject(new ApiError(0, 'the upload was cancelled'))
		options.signal?.addEventListener('abort', () => xhr.abort())
		xhr.send(body)
	})
}

export const api = {
	// --- auth ---
	login: (password: string) =>
		request<OkResponse>('/api/auth/login', {
			method: 'POST',
			body: JSON.stringify({ password }),
		}),
	logout: () => request<OkResponse>('/api/auth/logout', { method: 'POST' }),

	// --- system ---
	health: () => request<Health>('/api/system/healthz'),
	/** Probe whatever endpoint the caller names, on demand - unlike the
	 *  healthz snapshot, which only ever probes the stored default. */
	probeDiarizerEndpoint: (endpoint: string) =>
		request<DiarizerProbe>(`/api/system/diarizer/probe?endpoint=${encodeURIComponent(endpoint)}`),
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
		request<OkResponse>(`/api/system/alerts/channels/${id}`, {
			method: 'DELETE',
		}),
	testAlertChannel: (id: string) =>
		request<AlertTestResult>(`/api/system/alerts/channels/${id}/test`, {
			method: 'POST',
		}),

	// --- audio ---
	listDevices: () => request<InputDevice[]>('/api/audio/devices'),
	getInputDevice: () => request<DeviceSetting>('/api/audio/device'),
	setInputDevice: (device: string | null) =>
		request<OkResponse>('/api/audio/device', {
			method: 'PUT',
			body: JSON.stringify({ device }),
		}),

	// --- capabilities ---
	// The whole of src/loreline/capabilities.yaml: which provider+model
	// combinations exist and what each can do. Unauthenticated and unchanging
	// for the life of the process, so $lib/capabilities.svelte fetches it once
	// and shares it. Do not call this directly - use loadCapabilities().
	capabilities: () => request<CapabilityConfig>('/api/capabilities'),

	// --- providers ---
	listProviders: () => request<ProviderConfig[]>('/api/providers'),
	providerModels: (body: ProviderModelsRequest) =>
		request<ModelInfo[]>('/api/providers/models', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	createProvider: (body: ProviderCreate) =>
		request<ProviderConfig>('/api/providers', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	updateProvider: (id: string, body: ProviderCreate) =>
		request<ProviderConfig>(`/api/providers/${id}`, {
			method: 'PUT',
			body: JSON.stringify(body),
		}),
	deleteProvider: (id: string) => request<OkResponse>(`/api/providers/${id}`, { method: 'DELETE' }),
	setProviderSecret: (id: string, value: string) =>
		request<OkResponse>(`/api/providers/${id}/secret`, {
			method: 'POST',
			body: JSON.stringify({ value }),
		}),
	testProvider: (id: string) =>
		request<ProviderTestResult>(`/api/providers/${id}/test`, {
			method: 'POST',
		}),

	// --- glossary ---
	getGlossary: (campaign: string) => request<Glossary>(`/api/glossary/${campaign}`),
	putGlossary: (campaign: string, terms: string[]) =>
		request<Glossary>(`/api/glossary/${campaign}`, {
			method: 'PUT',
			body: JSON.stringify({ terms }),
		}),
	getDefaultGlossary: () => request<Glossary>('/api/glossary'),
	putDefaultGlossary: (terms: string[]) =>
		request<Glossary>('/api/glossary', {
			method: 'PUT',
			body: JSON.stringify({ terms }),
		}),

	// --- campaigns ---
	// A campaign is a row now, not the free string `sessions.campaign_id` used
	// to hold: it has a name, a glossary, a recap prompt and the documents its
	// sessions accumulate. See docs/adr/0009.
	listCampaigns: () => request<CampaignSummary[]>('/api/campaigns'),
	getCampaign: (id: string) => request<Campaign>(`/api/campaigns/${id}`),
	createCampaign: (body: CampaignWrite) =>
		request<Campaign>('/api/campaigns', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	updateCampaign: (id: string, body: CampaignWrite) =>
		request<Campaign>(`/api/campaigns/${id}`, {
			method: 'PUT',
			body: JSON.stringify(body),
		}),
	/** Delete the campaign. Its sessions are kept, unassigned - say so wherever
	 *  this is offered, because it is the one thing about it anyone fears. */
	deleteCampaign: (id: string) => request<OkResponse>(`/api/campaigns/${id}`, { method: 'DELETE' }),
	/** The campaign's sessions, oldest first: the order they were played in. */
	campaignSessions: (id: string) => request<Session[]>(`/api/campaigns/${id}/sessions`),
	campaignDocuments: (id: string, kind?: string) =>
		request<SessionDocument[]>(
			`/api/campaigns/${id}/documents${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`,
		),
	campaignEntities: (id: string) => request<CampaignEntities>(`/api/campaigns/${id}/entities`),
	addToCampaignGlossary: (id: string, terms: string[]) =>
		request<Glossary>(`/api/campaigns/${id}/glossary/add`, {
			method: 'POST',
			body: JSON.stringify({ terms }),
		}),
	getPreviouslyOn: (id: string) =>
		request<CampaignDocument | null>(`/api/campaigns/${id}/previously-on`),
	writePreviouslyOn: (id: string, body: PreviouslyOnRequest) =>
		request<CampaignDocument>(`/api/campaigns/${id}/previously-on`, {
			method: 'POST',
			body: JSON.stringify(body),
		}),

	// --- search ---
	/** Find a transcript line, everywhere or inside one campaign. `indexed`
	 *  comes back false on a SQLite build with no FTS5, where the hits are real
	 *  and the ranking is not. */
	search: (q: string, campaignId?: string, limit = 50) =>
		request<SearchResults>(
			`/api/search?q=${encodeURIComponent(q)}&limit=${limit}` +
				(campaignId ? `&campaign_id=${encodeURIComponent(campaignId)}` : ''),
		),

	// --- sessions ---
	startSession: (body: StartSessionRequest) =>
		request<Session>('/api/session/start', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	stopSession: () => request<Session>('/api/session/stop', { method: 'POST' }),
	listSessions: () => request<Session[]>('/api/session'),
	getSession: (id: string) => request<SessionDetail>(`/api/session/${id}`),
	getTranscriptVersion: (id: string, version: string) =>
		request<TranscriptEvent[]>(
			`/api/session/${id}/transcript?version=${encodeURIComponent(version)}`,
		),
	/** Delete one re-transcription version (segments + job rows). The server
	 *  refuses 'original', which is the live capture and cannot be remade. */
	deleteTranscriptVersion: (id: string, version: string) =>
		request<OkResponse>(`/api/session/${id}/transcript?version=${encodeURIComponent(version)}`, {
			method: 'DELETE',
		}),
	/** The log lines one transcript version was produced by. The live capture
	 *  is version 'original'; every re-transcription is its job id. */
	getVersionLogs: (id: string, version: string) =>
		request<VersionLogs>(`/api/session/${id}/logs?version=${encodeURIComponent(version)}`),
	setSpeakerNames: (id: string, names: Record<string, string>) =>
		request<OkResponse>(`/api/session/${id}/speakers`, {
			method: 'PUT',
			body: JSON.stringify({ names }),
		}),
	summarizeSession: (id: string, body: SummarizeRequest) =>
		request<SummarizeResult>(`/api/session/${id}/summarize`, {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	/** Put the session in a campaign, or take it out of one (null). */
	setSessionCampaign: (id: string, campaignId: string | null) =>
		request<Session>(`/api/session/${id}/campaign`, {
			method: 'PUT',
			body: JSON.stringify({ campaign_id: campaignId }),
		}),
	/** The player-facing account of the session. A different text for a
	 *  different reader than the summary, stored beside it as a document. */
	recapSession: (id: string, body: GenerateRequest) =>
		request<SessionDocument>(`/api/session/${id}/recap`, {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	/** The names the session used, as structured data. Stored as a document
	 *  too; what comes back is the parsed copy. */
	extractSession: (id: string, body: GenerateRequest) =>
		request<SessionExtraction>(`/api/session/${id}/extract`, {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	/** Download one version's transcript. The version is explicit because the
	 *  page can be showing a re-transcription while the header's Export menu
	 *  sits above it: the server defaults to 'original', so leaving it off is
	 *  how a download quietly disagreed with what was on screen. */
	exportUrl: (id: string, fmt: ExportFormat, version: string) =>
		`/api/session/${id}/export?fmt=${fmt}&version=${encodeURIComponent(version)}`,
	audioUrl: (id: string) => `/api/session/${id}/audio`,
	deleteSessions: (ids: string[]) =>
		request<OkResponse>('/api/session/delete', {
			method: 'POST',
			body: JSON.stringify({ ids }),
		}),
	mergeSessions: (ids: string[]) =>
		request<Session>('/api/session/merge', { method: 'POST', body: JSON.stringify({ ids }) }),
	/** Import a recording made elsewhere as a session.
	 *
	 *  `startedAt` is epoch seconds (the server defaults to now). `transcribe`
	 *  starts the first transcription in the same request and travels as a JSON
	 *  string, because the body is multipart and the block holds an object -
	 *  see ImportTranscribeOptions. */
	importRecording: (
		file: File,
		options: {
			startedAt?: number
			campaignId?: string | null
			transcribe?: ImportTranscribeOptions | null
			onProgress?: (progress: UploadProgress) => void
			signal?: AbortSignal
		} = {},
	) => {
		const body = new FormData()
		body.append('file', file, file.name)
		if (options.startedAt !== undefined) body.append('started_at', String(options.startedAt))
		if (options.campaignId) body.append('campaign_id', options.campaignId)
		if (options.transcribe) body.append('transcribe', JSON.stringify(options.transcribe))
		return upload<ImportedSession>('/api/session/import', body, options)
	},

	// --- video generation ---
	// Generation is asynchronous upstream (minutes), so enqueue returns a
	// queued job and the caller polls listVideoJobs/getVideoJob.
	videoModels: (providerId: string) =>
		request<VideoModelInfo[]>(`/api/video/models?provider_id=${encodeURIComponent(providerId)}`),
	enqueueVideo: (body: VideoGenerateRequest) =>
		request<VideoJob>('/api/video', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	getVideoJob: (jobId: string) => request<VideoJob>(`/api/video/${jobId}`),
	listVideoJobs: (sessionId: string) =>
		request<VideoJob[]>(`/api/video?session_id=${encodeURIComponent(sessionId)}`),
	deleteVideoJob: (jobId: string) =>
		request<OkResponse>(`/api/video/${jobId}`, { method: 'DELETE' }),
	/** Playback/download URL for a finished job (served from local storage). */
	videoContentUrl: (jobId: string) => `/api/video/${jobId}/content`,

	// --- reprocess ---
	enqueueReprocess: (body: ReprocessRequest) =>
		request<ReprocessJob>('/api/reprocess', {
			method: 'POST',
			body: JSON.stringify(body),
		}),
	getReprocess: (jobId: string) => request<ReprocessJob>(`/api/reprocess/${jobId}`),
	listReprocess: (sessionId: string) =>
		request<ReprocessJob[]>(`/api/reprocess?session_id=${sessionId}`),
	/** Stop a queued or running job, keeping what it has already written. The
	 *  job comes back as it stands, which for a re-transcription can still be
	 *  'running': it stops at the end of the utterance it is on, and the
	 *  caller's poll picks up the settled row. 409 when it already finished. */
	cancelReprocess: (jobId: string) =>
		request<ReprocessJob>(`/api/reprocess/${jobId}/cancel`, { method: 'POST' }),

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
