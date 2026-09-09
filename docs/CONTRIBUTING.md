# Contributing

## Getting set up

Loreline is a Python 3.12 FastAPI backend with a SvelteKit frontend.
[`uv`](https://docs.astral.sh/uv/) manages the Python side.

```bash
uv sync                      # base deps + dev group
uv run loreline version
uv run loreline run --reload # http://127.0.0.1:8000
curl http://127.0.0.1:8000/api/system/livez
```

Audio capture needs the native extra, and PortAudio has to be present on the
host:

```bash
uv sync --extra audio        # sounddevice / silero-vad
uv run loreline devices      # list input devices
```

The frontend lives in `frontend/`:

```bash
cd frontend
npm install
npm run dev                  # http://localhost:5173, proxies the API to :8000
```

The backend serves the web UI at `/` only when `frontend/build/` exists, so run
`npm run build` to test the production-style single-origin setup.

## Checks

Everything CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv run loreline check-capabilities --offline

cd frontend && npm run lint && npm run check && npm run build
```

`uv run prek install` wires the git hooks up. prek is a drop-in Rust
reimplementation of pre-commit and reads the same `.pre-commit-config.yaml`;
`uv run prek run --all-files` runs every hook over the whole tree. The pyright
hook ignores the staged file list and type-checks the whole configured include
set, `src`, `tests` and `mocks`, in strict mode, so a change under `tests/` or
`mocks/` fails the commit exactly as one under `src/` does. Two secret scanners
also run on every commit, gitleaks over the staged diff and trufflehog over the
same range with live verification.

> Tip: run uv with `--frozen`, or `export UV_FROZEN=1`, so it never rewrites
> `uv.lock`. A user-global `exclude-newer` in `~/.config/uv/uv.toml` otherwise
> makes every `uv run` re-resolve and dirty the lock. CI already runs
> `uv sync --frozen`. When you intentionally change dependencies, run `uv lock`.

## Cutting a release

Releases are cut by hand, from a clean `main`, whenever there is something
worth deploying. Nothing in CI does it: the tag is a deliberate act, and a
version number only means anything if somebody decided it.

[commit-and-tag-version](https://github.com/absolute-version/commit-and-tag-version)
does the mechanical part. It reads the conventional commit subjects since the
last `v*` tag, writes `CHANGELOG.md`, bumps the version everywhere it is
written down, commits that as `chore(release): <version>` and tags it
`v<version>`. The `package.json` in the repository root exists for no other
reason than to install that tool: Loreline is not an npm package, nothing is
built or published from there, and the version of record is `pyproject.toml`.

```bash
npm install                  # once, and again whenever the tool is updated
npm run release:dry          # says what it would do, writes nothing
npm run release              # bump, changelog, commit, tag
```

Four files carry the version, and all four are bumped together and land in the
release commit: `pyproject.toml`, `frontend/package.json`,
`frontend/package-lock.json` and `uv.lock`. The last of those is not a nicety.
`uv.lock` keeps its own copy of loreline's version, `uv sync --frozen` refuses
to run when the two disagree, and `uv sync --frozen` is what the Dockerfile
does, so a release that skipped it would commit and tag perfectly cleanly and
then fail every image build from that tag onwards. `uv lock --check` runs as a
post-bump hook so that cannot happen quietly.

Four, and there is no fifth. The number the running app reports about itself,
`loreline.__version__`, is not written down anywhere: it is read out of the
installed distribution's metadata, which the build backend fills from
`pyproject.toml`, so bumping the file of record is what moves it. That is worth
saying out loud, because it was a hardcoded constant in
`src/loreline/__init__.py` until shortly after v0.2.0 was cut, the release
tooling had no idea the constant existed, and the image shipped from that tag
reported `v0.2.0` under Settings > Client while the header beside the app name
still read `0.1.0`. Putting a literal back there is how that returns.

`frontend/openapi.json` is not a fifth one either, and deliberately so. Its
`info.version` describes the API the document specifies, not the build that
serves it, so it is pinned to a constant in `src/loreline/web/app.py`
(`OPENAPI_DOCUMENT_VERSION`) rather than tracking releases. Letting the release
version in would mean every bump invalidated a committed generated file that
pre-commit and CI both diff on every push. The comment on that constant has the
full argument.

### What drives the bump

`fix:` is a patch and `feat:` would ordinarily be a minor. Those two are also
the only types that reach the changelog. `refactor:`, `docs:`, `test:`,
`chore:`, `style:`, `build:` and this repo's own `merge:` are hidden, not
because they are not real work but because somebody comparing two deployed
builds is not trying to learn that a component got extracted.
`.versionrc.cjs` holds the mapping and the argument for it.

> Tip, and this is the part worth remembering: while the version is below
> 1.0.0, commit-and-tag-version switches the preset into its 0.x mode by itself
> and shifts every bump down a step. Features come out as a patch, and only a
> `BREAKING CHANGE` reaches a minor. No setting in `.versionrc.cjs` turns that
> off, it is hardcoded in the tool. So a release that added features has to say
> so out loud:
>
> ```bash
> npm run release -- --release-as minor
> ```
>
> v0.2.0 was cut exactly that way: 0.1.0 plus 59 features would otherwise have
> proposed 0.1.1. A release that only fixes things needs no flag.

The standing rule, so this is not argued out again at every release: while the
version is below 1.0.0, a release that contains any `feat:` is cut with
`--release-as minor`, and a release that contains only `fix:` is cut with no
flag at all. That keeps the middle digit meaning "features landed" and the last
digit meaning "fixes only", which is exactly what the changelog's two sections
already claim, and it means the number can be read without going back to the
commits. A `BREAKING CHANGE` needs no flag either, because the same 0.x mode
already promotes it to a minor by itself. None of this survives 1.0.0: once the
version is past it the clamp stops applying, `feat:` becomes a minor on its
own, and plain `npm run release` is correct again.

### Publishing the tag

The release stops at a local commit and tag on purpose, so there is a moment to
read the changelog back before any of it becomes permanent. Nothing reaches
anyone else until the tag is pushed, and the tag has to be pushed explicitly:

```bash
git push --follow-tags origin main
```

That push is what moves the Revision line under Settings > Client. The image
bakes in `git describe --tags --always` at build time, so a build cut from a
pushed tag reports `v0.2.0` exactly, and one cut a few commits later reports
something like `v0.2.0-7-gabc1234`. Before v0.2.0 existed the nearest reachable
tag was `pre-streaming-realtime-stt-20260908`, a safety marker left before a
large merge, and the line read `pre-streaming-realtime-stt-20260908-108-g48f132d`,
which told nobody anything. Push the release commit without `--follow-tags` and
it stays that uninformative, because the tag the build describes against never
left the machine that made it.

## Layout

| Path | What |
|---|---|
| `src/loreline/audio/` | capture, VAD chunking, WAV writing, device enumeration |
| `src/loreline/stt/` | connectors, the registry and the primary/fallback router |
| `src/loreline/capabilities.yaml` | every model fact and every vendor surface |
| `src/loreline/capabilities.py` | the accessors that read that file |
| `src/loreline/catalog.py` | the one vendor catalogue reader |
| `src/loreline/health_probe.py` | the one provider health probe |
| `src/loreline/session/` | session lifecycle and orchestration |
| `src/loreline/persistence/` | SQLite repositories |
| `src/loreline/web/` | FastAPI app, routes, auth |
| `frontend/` | SvelteKit SPA |
| `services/diarization/` | self-hosted sherpa-onnx diarization service |
| `deploy/` | install/update scripts, systemd units |
| `mocks/` | mock provider servers for offline testing |

## Wire types

`frontend/src/lib/wire.ts` names every request and response shape the pages
use. Each one derives from `frontend/src/lib/api.generated.d.ts`, which is
generated from `frontend/openapi.json`, FastAPI's own description of the API
(`uv run loreline openapi`). Nothing about the wire is written by hand any
more; `frontend/src/lib/types.ts` keeps only what the document cannot say.

After changing `src/loreline/web/schemas.py`, `src/loreline/models.py` or a
route signature, run `cd frontend && npm run gen:api` and commit both generated
files with the change. Two checks catch a miss. The `openapi-current`
pre-commit hook compares the committed document against the live one and,
where `frontend/node_modules` is installed, the types against the document.
CI does the same in halves: the backend job checks the document, the frontend
job's `npm run check` checks the types. The fix for either is that one command.

One thing in the document is not generated from the routes: `info.version` is a
constant, not the release version, so a document reading `"version": "1"` under
a 0.2.x build is current rather than stale. "Cutting a release" above says why.

## How a transcription request travels

A route in `src/loreline/web/routes/` takes the provider row and the model the
caller named. `stt.registry.create_backend(config, secrets, model)` resolves the
kind and the model's transport to one connector, and resolves that model's
`TranscribeCapabilities` once, so the transport lookup, the connector and the
glossary policy can never disagree about which model is running. `SttRouter`
then hands that connector one utterance at a time, falls over to the fallback
provider when it fails, and hands the events to the diarizer.

Where the connector also has the streaming shape (`is_streaming`),
`SessionManager` picks `StreamPath` instead: frames go straight from capture
to the connector, which decides its own turns and yields interim and final
events over one long-held connection, reconnecting a bounded number of times
before failing over to the next streaming provider, then to `SttRouter` where
the fallback is call-shaped instead. Diarization goes through the same
`merge_diarization` either way, but beside the stream rather than in front of
it: a turn publishes unlabelled the moment it closes and is republished with
speakers once the diarizer answers. See `docs/adr/0006`.

The connector itself holds no addresses. `capabilities.surface_for` returns the
URL and the auth scheme the yaml declares for that interaction and transport,
with the provider row's `base_url` applied where the surface says it may be. The
same accessor answers for the LLM client, the video client, the catalogue reader
and the diarizer.

Five decisions shape all of this and each has an ADR under `docs/adr/`: the
connector base and what composes it, vendor surfaces living in the yaml, one
catalogue reader behind every model list, one health probe behind every "does
this key work" question, and one utterance per transcription call with the
model's capabilities resolved before the connector is built. Read those before moving a fact out of the yaml
and into code. `CONTEXT.md` has the vocabulary.

## Adding an STT backend

1. Add the vendor to `src/loreline/capabilities.yaml`: a provider block with its
   `surfaces` (a `url` and an `auth` per interaction and transport, a `catalog`
   surface, a `health` path or frame), its `interactions`, and its curated
   models with what each supports.
2. Implement `Connector` or `HttpConnector` from `src/loreline/stt/base.py`.
   You supply `prepare` and `transcribe_one`; the base owns the
   `TranscriptEvent` and the speaker rule. The `STTBackend` contract is
   `aclose()` and `transcribe()`, which takes one utterance and answers with
   one event or None. There is no `health()`: probing is
   `loreline.health_probe`'s job, driven by the surface in the yaml.
3. Register the factory with `@register(ProviderKind.YOURS)`, adding
   `realtime=True` for a streaming connector. One kind can register both. The
   factory receives the config, the secret store, the model and that model's
   resolved `TranscribeCapabilities` (None when nobody curated it), which is
   where a glossary ceiling or a declared feature conflict is read from. See
   any file in `src/loreline/stt/backends/`.
4. Add the kind to `ProviderKind` in `src/loreline/models.py`, and its one line
   of display copy to `PRESENTATION` in
   `frontend/src/routes/settings/providers/+page.svelte`. Everything else the
   wizard shows comes from the served capability config.
5. Cover it with a test that injects a fake client rather than hitting the
   network. `tests/integration/test_gemini.py` is a good template.

Backends are imported lazily so provider SDKs stay optional: the module has to
import cleanly even when its SDK is not installed.

## Notes

- Provider credentials live in `data/secrets.json` with mode `0600`, never in
  the database. A `LORELINE_SECRET_<NAME>` environment variable overrides the
  stored value.
- Deployment specifics that cost real time to discover, such as Bluetooth audio
  in containers and installer pitfalls, are collected in
  [`DEPLOYMENT-NOTES.md`](./DEPLOYMENT-NOTES.md).
