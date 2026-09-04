/**
 * The shapes the backend actually serves, named.
 *
 * Every type here is derived from `api.generated.d.ts`, which is generated
 * from `openapi.json`, which FastAPI writes out of the pydantic models
 * themselves (`uv run loreline openapi`). Nothing in this file describes the
 * wire: it only gives the generated shapes the names the pages use, so a page
 * imports `ProviderConfig` rather than `components['schemas']['ProviderView']`.
 *
 * That is the whole point of the indirection. A field added, removed or
 * retyped in `src/loreline/` reaches the pages through here and breaks
 * `npm run check`, instead of reaching them at runtime as an undefined.
 *
 * Regenerate with `npm run gen:api` after any schema change; `npm run check`
 * and the pre-commit hook both refuse a document that is out of date.
 *
 * Frontend-only types - unions the wire spells as plain strings, display
 * helpers, anything with no server-side counterpart - live in `./types`.
 */

import type { components } from './api.generated'

type Schemas = components['schemas']

/**
 * A payload with every field present.
 *
 * The document marks a field optional when it has a server-side default, which
 * is a statement about requests only: pydantic serializes every field of a
 * response, defaults and nulls included, so a response never omits one. Read
 * straight, the generated types would claim `favorite_models` might be missing
 * from a provider row that always carries it, and every reader would need a
 * `?? []` for a case that cannot happen.
 *
 * The two halves are told apart here rather than in the generator (which emits
 * one type per schema, whichever direction it travels): a request keeps the
 * document's own optionality, and a response is named through this, which
 * drops the optional marker - and with it the implicit `undefined` - at every
 * depth. `| null`, a value the server really does send, is left as it is.
 *
 * It also types a form draft that fills in every field, which is the same
 * claim made about a request the page is about to send.
 */
export type Complete<T> = T extends (infer Item)[]
	? Complete<Item>[]
	: T extends object
		? { [K in keyof T]-?: Complete<T[K]> }
		: T

// --- enumerations -----------------------------------------------------------
//
// StrEnums and Literals on the Python side, string unions here.

export type ProviderKind = Schemas['ProviderKind']
export type Interaction = Schemas['Interaction']
export type DiarizationModeKind = Schemas['DiarizationMode']
export type SessionStatusKind = Schemas['SessionStatus']
export type JobStatusKind = Schemas['JobStatus']
export type AuthScheme = Schemas['AuthScheme']
export type AlertLevelKind = Schemas['AlertLevel']

/**
 * How far a provider got when the Test button asked it a cheap question.
 *
 * Not a boolean, because "healthy" is at least three separate facts: the
 * endpoint answers, the credential is accepted, and the vendor is not
 * currently refusing. A provider with an invalid key and one whose base URL is
 * a typo are opposite fixes.
 *
 * - `healthy`      answered and accepted the credential
 * - `degraded`     answered but is rate limiting or erroring; the key is fine
 * - `unauthorized` reached it; the key is missing, wrong, or lacks access
 * - `unreachable`  no answer, or nothing that API lives at this URL
 * - `unknown`      the probe ran and decided nothing; explicitly not a failure
 */
export type HealthStatus = Schemas['HealthStatus']

/** Inline in the document (the enum is only ever used through a channel), so
 *  it is named off the field rather than off a schema of its own. */
export type AlertChannelKind = Schemas['AlertChannelView']['type']
export type Hosting = Schemas['ProviderSpec']['hosting']
/** 'optional' is a self-hosted server that may or may not check a key. */
export type AuthKind = Schemas['ProviderSpec']['auth']
export type LanguageSupport = Schemas['TranscribeCapabilities']['languages']

// --- capability config (GET /api/capabilities) ------------------------------
//
// The wire shape of src/loreline/capabilities.yaml: which provider+model
// combinations exist and what each can do. The fetched data lives in
// $lib/capabilities.svelte, which also holds every helper that reads it.

export type GlossarySupport = Complete<Schemas['GlossarySupport']>
export type TranscribeCapabilities = Complete<Schemas['TranscribeCapabilities']>
export type ReasoningSupport = Complete<Schemas['ReasoningSupport']>
export type LlmCapabilities = Complete<Schemas['LlmCapabilities']>
export type VideoCapabilities = Complete<Schemas['VideoCapabilities']>
export type ModelSpec = Complete<Schemas['ModelSpec']>
export type ModelPattern = Complete<Schemas['ModelPattern']>
export type HealthProbe = Complete<Schemas['HealthProbe']>
export type Surface = Complete<Schemas['Surface']>
export type TranscribeSurfaces = Complete<Schemas['TranscribeSurfaces']>
export type Surfaces = Complete<Schemas['Surfaces']>
export type ProviderSpec = Complete<Schemas['ProviderSpec']>

/**
 * The one place a generated shape is narrowed rather than renamed.
 *
 * `providers` is a `dict[ProviderKind, ProviderSpec]` on the Python side, but
 * JSON Schema states the key type in `propertyNames`, which the generator
 * drops - leaving an index signature that promises a spec for every string.
 * Looking one up returns `ProviderSpec | undefined` in fact, and every reader
 * here is written for that, so the key type is put back by hand.
 */
export type CapabilityConfig = Omit<Complete<Schemas['CapabilityConfig']>, 'providers'> & {
	providers: Partial<Record<ProviderKind, ProviderSpec>>
}

// --- providers --------------------------------------------------------------

export type OpenRouterRouting = Complete<Schemas['OpenRouterRouting']>
export type ModelPrice = Complete<Schemas['ModelPrice']>
export type ModelInfo = Complete<Schemas['ModelInfo']>
/** A stored provider row plus the masked hint of its key (`ProviderView`). */
export type ProviderConfig = Complete<Schemas['ProviderView']>
export type ProviderTestResult = Complete<Schemas['TestResult']>
export type ProviderModelsRequest = Schemas['ProviderModelsRequest']
/** The create/update payload. Requests keep the document's own optionality:
 *  a field with a server-side default may be left out. */
export type ProviderCreate = Schemas['ProviderCreate']

// --- sessions and transcripts -----------------------------------------------

export type DiarizationConfig = Complete<Schemas['DiarizationConfig']>
export type Word = Complete<Schemas['Word']>
export type TranscriptEvent = Complete<Schemas['TranscriptEvent']>
export type Glossary = Complete<Schemas['Glossary']>
export type Session = Complete<Schemas['Session']>
export type SessionDetail = Complete<Schemas['SessionDetail']>
export type VersionLogs = Complete<Schemas['VersionLogs']>
export type StartSessionRequest = Schemas['StartSessionRequest']
export type SummarizeRequest = Schemas['SummarizeRequest']
export type SummarizeResult = Complete<Schemas['SummarizeResult']>
export type ReprocessJob = Complete<Schemas['ReprocessJob']>
export type ReprocessRequest = Schemas['ReprocessRequest']

// --- video ------------------------------------------------------------------

export type VideoModelInfo = Complete<Schemas['VideoModelInfo']>
export type VideoJob = Complete<Schemas['VideoJob']>
export type VideoGenerateRequest = Schemas['VideoGenerateRequest']

// --- system and monitoring --------------------------------------------------

export type Health = Complete<Schemas['HealthResponse']>
export type ActionDefaults = Complete<Schemas['ActionDefaults']>
export type InputDevice = Complete<Schemas['InputDevice']>
export type DeviceSetting = Complete<Schemas['DeviceSetting']>
export type UpdateResult = Complete<Schemas['UpdateResult']>
export type RevisionResponse = Complete<Schemas['RevisionResponse']>
export type AutostartState = Complete<Schemas['AutostartState']>
export type ServiceState = Complete<Schemas['ServiceState']>
export type ServiceLogs = Complete<Schemas['ServiceLogs']>
export type AlertChannel = Complete<Schemas['AlertChannelView']>
export type AlertChannelWrite = Schemas['AlertChannelWrite']
export type AlertTestResult = Complete<Schemas['AlertTestResult']>
/** Every acknowledgement-only route answers with this. */
export type OkResponse = Complete<Schemas['OkResponse']>
