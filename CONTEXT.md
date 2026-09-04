# Loreline STT

Loreline captures a tabletop session's audio, turns it into a transcript
through a cloud speech-to-text vendor, and re-processes stored audio later.
This is that domain's language as the code uses it. Detail lives in
`src/loreline/capabilities.yaml` (what each model can do) and the
`src/loreline/stt/base.py` docstring (how a connector is built); decisions
with a why behind them are in `docs/adr/`.

## Providers and models

**ProviderKind**: A vendor the app can talk to (Deepgram, OpenAI, AssemblyAI,
Gemini, OpenRouter, or any self-hosted OpenAI-compatible server).

**Interaction**: What a provider is being asked to do: transcribe, summarize
or generate video. Models are never interchangeable across interactions.

**Transport**: How audio reaches a model: realtime (a socket that answers
while audio is still going out) or batch (one request per utterance). A model
may serve one or both. It follows the chosen model, never the provider row.
_Avoid_: protocol (was a stored enum on a ProviderConfig that nothing read)

**ProviderConfig**: One stored provider row a GM configured: a kind, a
credential reference, an optional base URL, a language and a shortlist of
favourite models. Nothing about what the kind can do lives on the row; that
is the yaml's.

**Surface**: How to reach a vendor for one interaction over one transport:
the URL and the auth scheme, declared once in the yaml under the provider,
optionally overridable by the provider row's base URL. A request's surface
follows the model's chosen transport, never the vendor as a whole.
_Avoid_: endpoint constant, default base URL (each was one connector's copy)

**ModelSpec / TranscribeCapabilities**: One curated model and its transcription
surface: transports served, speakers, word timings, glossary ceiling.

**Connector**: The adapter for one kind over one transport, built on the
`Connector` base and satisfying the `STTBackend` contract.
_Avoid_: backend (kept only in class names and the contract), provider class

**Health probe**: One question per provider row, "does this key work at
this surface", answered as a **HealthReport** (reachable, the credential
works, or the vendor's own words on why not) by one entry point,
`probe_provider`, never by building a connector. The surface asked is the
one the yaml declares for the kind, and the grading is the same behind the
settings badge, the remote diarizer and `/healthz`. Distinct from the
Catalogue probe below, which asks what a vendor lists.
_Avoid_: connector health, `health()` (each was one connector's copy)

**Catalogue probe**: One vendor's answer to "what do you list right now" for
one interaction, read once from its catalog surface, fail-soft, with an
explicit status (ok, no catalogue, no credentials, unreachable, unreadable)
so silence is never mistaken for absence. The model pickers, the video list
and the staleness gate are projections of that one answer. A different
question from the health probe, which asks whether a provider row works.
_Avoid_: live fetch, vendor list (each was one projection's own reader)

## Browser

**Model catalogue**: One store per browser session answering "which models does
this provider row offer for this interaction", deduped per provider row,
interaction and refresh token. The pickers are views over it, not owners of it.

**Preferred model**: The pure rule for which model a picker starts on: the
action default when it belongs to this provider row, else the first favourite,
hidden models excluded. A user's pick overrides it until the provider changes.

## Audio and transcript

**Utterance**: One voiced stretch of session audio, cut by the VAD chunker,
with its start and end on the session clock.

**Word**: One recognized word with timing on the session clock and, when the
vendor attributes it, a speaker label.

**Transcription**: What a connector gets back for one utterance: the text and
whatever words came with it. Not yet an event.

**TranscriptEvent**: One final transcript segment for one utterance, tagged
with the source that produced it. Its speaker is the speaker of the first word
that carries one, else none; this is the one speaker rule for every connector.

**Transcript version**: One full pass over a session's audio, the live capture
("original") or one re-processing job. Diarization relabels one into a copy.

**Glossary**: A campaign's list of names and terms, in priority order, sent to
a model to bias recognition. Trimmed to the model's ceiling, head first.
_Avoid_: prompt, vocabulary, keyterms (each is one vendor's wire name for it)

**SttRouter**: Runs a session's utterances through a primary connector, fails
over to a fallback, and applies diarization.

**Diarizer**: The adapter that turns words or audio into speaker segments for
one DiarizationMode (inline from the STT's labels, a remote sherpa-onnx
service, OpenAI's batch model, or none). One factory, `DiarizerFactory`,
owns construction and credential precedence: a configured OpenAI row's stored
key, else the environment. A live session and a reprocess job get the same
diarizer from the same factory.
_Avoid_: diarization provider (the class name it keeps in `DiarizationProvider`)
