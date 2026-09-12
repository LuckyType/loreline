# Loreline STT

Loreline captures a tabletop session's audio, turns it into a transcript through a
speech-to-text vendor, and re-processes stored audio later. This is that domain's language
as the code uses it. Detail lives in `capabilities.yaml`, `stt/base.py` and `docs/adr/`.

## Providers and models

**ProviderKind**: A vendor the app can talk to: Deepgram, OpenAI, AssemblyAI, Gemini,
OpenRouter, or any self-hosted OpenAI-compatible server.

**Interaction**: What a provider is being asked to do, one of transcribe, summarize or
generate video. Models are never interchangeable across these.

**Transport**: How audio reaches a model. Realtime is a socket that answers while audio
is still going out, batch is one request per utterance. A model may serve one or both.
It follows the chosen model, never the provider row. A model serving both names its
preference, and that preference is written for a live capture, so re-processing a stored
recording takes the batch transport instead wherever the model has one. Realtime became
literally true with the streaming shape below; before that a realtime connector still only
ever saw audio a local VAD had finished cutting.
_Avoid_: protocol (was a stored enum on a ProviderConfig that nothing read)

**ProviderConfig**: One stored provider row a GM configured: a kind, a credential
reference, an optional base URL, a language and a shortlist of favourite models.
Nothing about what the kind can do lives on the row; that is the yaml's.

**Surface**: How to reach a vendor for one interaction over one transport, the URL and
the auth scheme, declared once in the yaml under the provider and optionally overridable
by the row's base URL. It follows the model's chosen transport, never the whole vendor.
_Avoid_: endpoint constant, default base URL (each was one connector's copy)

**ModelSpec / TranscribeCapabilities**: One curated model and its transcription surface:
transports served, speakers, word timings, glossary ceiling.

**Connector**: The adapter for one kind over one transport, built on the `Connector` base
and satisfying the `STTBackend` contract: one utterance in, one transcript event or nothing
back. It is built with its model's capabilities already resolved, so it never asks the yaml
which model is running. A realtime connector may also have the **streaming shape** below;
having it is a fact about the class, never a field in the yaml.
_Avoid_: backend (kept only in class names and the contract), provider class

**Streaming shape**: The second thing a realtime connector can be: fed a session's raw PCM
frames with no boundary decided for it, translating its vendor's messages into turn signals
and nothing else. `StreamingConnector` is that half; `TranscriptStream` is the other half,
one per provider per session, owning the capture clock, interim throttling, resampling,
the liveness watchdog, reconnects and the gap marker. See `docs/adr/0006`.
_Avoid_: streaming backend, realtime mode (the shape is not a mode anything is in)

**Turn**: One stretch of speech a vendor's own endpointing decided on, which is what a
streaming connector produces instead of an Utterance. It is published several times, as a
growing interim and then as a final, all carrying one **turn id**, which is the key a
reader replaces by rather than appends after: the transcript table, both live feeds.
_Avoid_: segment id, utterance (a turn is nobody's utterance)

**Gap marker**: A transcript row saying the app lost this span of audio, left where a
streaming connection died. Its `source` is `gap`, which keeps it in the live views (the
browser is who it is for) and out of exports, summaries and diarize jobs, all of which read
through `final_rows`. A streaming loss is aligned to nothing, so unlike a dropped utterance
no re-process recovers exactly it, which is why it is a row and not a log line.

**Health probe**: One question per provider row, "does this key work at this surface",
answered as a **HealthReport** by `probe_provider`, never by building a connector. It asks
the surface the yaml declares, and grades alike for the badge, the diarizer and `/healthz`.
_Avoid_: connector health, `health()` (each was one connector's copy)

**Catalogue probe**: One vendor's answer to "what do you list right now" for one interaction,
read once from its catalog surface, fail-soft, with an explicit status, so silence is never
mistaken for absence. The pickers, the video list and the staleness gate project that answer.
_Avoid_: live fetch, vendor list (each was one projection's own reader)

## The instance itself

**Claimed / unclaimed / open**: The three states an installation can be in, and the one thing
the app must never have to guess. Claimed is a password in force, from `LORELINE_AUTH_PASSWORD`
or from the secret store, and is today's behaviour exactly. Unclaimed is `first_run_setup` on
with no password from either source: only the setup routes and `livez` answer, everything else
is 403, and the browser is sent to the wizard from anywhere. Open is `first_run_setup` off with
no password, which is the dev box and the test suite. An empty password has meant "auth off"
since the first commit, so a fresh deployment is not recognisable from it, which is why the
gate is its own setting rather than an inference. See `docs/adr/0011`.
_Avoid_: "no auth" (that is open and unclaimed at once, which is the confusion)

**Setup code**: The one secret standing between a brand new instance and whoever reaches it
first. Minted once while unclaimed, persisted through `SecretStore` so a restart shows the same
one, logged at every startup while unclaimed, and deleted on a successful claim. It appears in
that log line and nowhere else: never in `GET /api/setup/state`, never in an error body, never
in a log line after the claim. Grouped as `ABCD-EFGH` for reading aloud, and compared with the
case and the grouping folded away.
_Avoid_: pairing code, install token

**Claim**: The transition out of unclaimed: the setup code plus a password given twice, which
stores the password, retires the code and issues the session cookie, so the browser lands signed
in rather than at a login form. Refused with the login route's own backoff, keyed by
`client_address` so a reverse proxy does not make one bucket of the whole table. A typo here
locks the instance out, and the only way back is `LORELINE_AUTH_PASSWORD` on the host, which
always wins over the stored one.

**Wizard**: `/setup`, rendered bare like `/login`: the claim, then a provider, then which of the
three recording routes this deployment actually offers. Everything after the claim is skippable
and resumable, and every step is somewhere it already lived (Settings > Providers, the capture
card), so a GM who skips it all lands on a dashboard that works.

## Browser

**Action setup**: One store per browser session holding the provider rows, the stored action
defaults and the capability gate, loaded together. Every picker's seed is a derivation over it,
the stored row while still offerable, else the first offerable. `capture` is transcription
narrowed to live-capable rows.

**Model catalogue**: One store per browser session answering "which models does this
provider row offer for this interaction", deduped per provider row, interaction and
refresh token. The pickers are views over it, not owners of it.

**Preferred model**: The pure rule for which model a picker starts on: the action default when
it belongs to this row, else the first favourite, hidden models excluded. A user's pick wins.

**Live feed**: One socket-backed buffer for a page pane: a path, a history seed and a cap. The
Dashboard's transcript pane, its log pane and a session's re-processing feed are the same object
three times over, with reconnect handled underneath by ws.ts.

## Audio and transcript

**Capture source**: Where a session's frames come from, and the one thing a start request
decides about them: a sound card on this machine through PortAudio, or the **client
microphone** below. It is one protocol, "a stoppable source of timestamped PCM frames", so
everything past the capture factory - the capture loop, the stats, the level meter, the disk
watch, the WAV writer, StreamPath, SttRouter - is written against the frames and cannot tell
which one it is reading. See `docs/adr/0010`.
_Avoid_: capture backend, audio input (each named the device rather than the seam)

**Client microphone**: The browser as the capture source: `getUserMedia` in the tab, an
AudioWorklet turning Float32 into PCM16, and a socket carrying the frames to
`WS /ws/audio/capture`, where they are resampled and re-blocked into an ordinary capture.
Every frame is stamped on arrival at the server, never from the browser's clock, and the
one socket is refused to a second tab rather than swapped. It needs a secure context, which
is a deployment fact and not an application one. The tab is then the recording: closing it
ends the session a reconnect window later.
_Avoid_: browser recording (that is the import, ADR 0008), remote mic

**Capture pre-flight**: Opening and immediately releasing the chosen microphone while the
start request is still being answered, through the same calls the capture itself makes. A
device that refuses fails the request. The device is opened at whatever rate it serves and
resampled to the provider's rate, so "this mic only does 48 kHz" is no longer a refusal.
For a client microphone the same question is asked of the socket: is a browser attached, and
has it delivered audio in the last few seconds.

**Capture liveness**: How much audio a running session has received, and how long ago its
last frame arrived. A microphone delivers frames in silence too, so a climbing age is a
stopped device rather than a quiet table - which is the one thing "capturing" cannot say.
A client microphone reads the same either way, and a gap it comes back from is padded with
silence so the recording's byte offset stays the session clock.

**Utterance**: One voiced stretch of session audio, cut by the VAD chunker, with its
start and end on the session clock.

**Word**: One recognized word with timing on the session clock and, when the vendor
attributes it, a speaker label.

**Transcription**: What a connector gets back for one utterance, the text and whatever words
came with it. Not yet an event.

**TranscriptEvent**: One segment of one transcript version, tagged with the source that
produced it: a settled one per utterance from the utterance path, or one turn's current
state from the streaming path, final or interim. Its speaker is the speaker of the first
word that carries one, else none. That is the one speaker rule for every connector.

**Transcript version**: One full pass over a session's audio, the live capture
("original") or one re-processing job. Diarization relabels one into a copy.

**Import**: A recording made elsewhere, stored as a session rather than as a second
kind of thing: the same continuous WAV and utterance index a capture leaves behind, a
`completed` row, and `started_mono` of zero, so the recording's own clock is the session
clock. Nobody heard it live, so its "original" version is empty and no run will ever fill
it, and transcribing it is an ordinary re-processing job. `Session.origin` is the only
thing that says so, and only the browser reads it. See `docs/adr/0008`.
_Avoid_: upload (the request, not the session it makes)

**Glossary**: A campaign's list of names and terms, in priority order, sent to a model to
bias recognition. Trimmed to the model's ceiling, head first, so what a session actually
sends is the cast, then the campaign's terms, then the always-on `_default` list, deduped
case-insensitively. Edited on the campaign it belongs to; `_default` is still merged and
still served, and no page points at it.
_Avoid_: prompt, vocabulary, keyterms (each is one vendor's wire name for it)

**SttRouter**: Runs a session's utterances through a primary connector, fails over to a
fallback, applies diarization.

**StreamPath**: The other live path, taken when a session's primary connector has the
streaming shape: frames straight from capture to the connector, a fallback opened as a
second stream or, where the fallback is call-shaped, the rest of the session handed to
`SttRouter`. Both providers dead means what it always meant, keep recording and stop
transcribing. The VAD and the chunker keep running under both, for the WAV's utterance
index.

**Diarizer**: The adapter that turns words or audio into speaker segments for one
DiarizationMode: inline from the STT's labels, a remote sherpa-onnx service, OpenAI's batch
model, or none. One factory, `DiarizerFactory`, owns construction and the credential
precedence, a configured OpenAI row's stored key before the environment.
_Avoid_: diarization provider (the class name it keeps in `DiarizationProvider`)

**Speaker bank**: The voices one session has been heard to contain, kept by the remote
service under the `session_id` every call carries, so a label means the same person in the
first utterance and the hundredth. Without it each call clusters alone and calls whoever
spoke "Speaker 0". Dropped when the diarizer closes, and on an idle TTL besides. See
`docs/adr/0007`.

## Campaign memory

**Campaign**: What a session belongs to, and where the value of its transcript
collects: its sessions in order, its glossary, its search, and the documents below.
A row since the campaign layer landed; before that a free string on the session that
nothing could resolve. Deleting one unassigns its sessions and never deletes them.
See `docs/adr/0009`.

**Cast**: Who is at a campaign's table: a row per seat, the person and the character
they play, at least one of the two filled, in priority order. The one fact both halves of
the app want. It leads the glossary, because a character's name is the word a recognizer
has no prior for and the ceiling is spent from the head; and it is one line of instruction
in the summary, the recap, the extraction and the "previously on", which is what turns the
extraction's `pc`/`npc` split from a guess into a reading. No cast means no line and no
terms, so a campaign nobody fills in behaves exactly as it did before.
_Avoid_: party (a party is in the fiction, the cast is the room)

**Document**: One generated text about a session (`recap`, `extraction`) or about a
campaign (`previously_on`), with the provider, the model and, for a session, the
transcript version behind it. One row per subject and kind, so writing one replaces
the last. The summary still lives in its own columns on the session; folding it in
here is a noted follow-up.
_Avoid_: artifact, generation (the video jobs are the other thing called that)

**Recap**: The player-facing account of one session, past tense, in the transcript's
language, for people who were at the table. Not a **summary**, which is the GM's index
of what happened: the two are separate texts because they are for separate readers.
Its instructions are the campaign's, else the stored default, else the built-in.

**Extraction**: The structured names one session used - characters, places, items,
factions, quests, decisions - asked for as JSON and read leniently, retried once when
the answer cannot be parsed. `GET /api/campaigns/{id}/entities` merges every session's
by normalised name, newest notes winning, which is what makes the campaign's cast
readable in one list.

**Previously on**: The short text that opens the next session, written from the last
few sessions' recaps and falling back per session to their summaries.

**Search**: FTS5 over the transcript rows, external content so the rows stay the
single source, ranked by `bm25` with `snippet()` marking the hits. A build with no
FTS5 answers the same question with `LIKE`, newest first and unranked, and the answer
says which ran.
