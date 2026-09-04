# Contributing

## Getting set up

Loreline is a Python 3.12 FastAPI backend with a SvelteKit frontend.
[`uv`](https://docs.astral.sh/uv/) manages the Python side.

```bash
uv sync                      # base deps + dev group
uv run loreline version
uv run loreline run --reload # http://127.0.0.1:8000
curl http://127.0.0.1:8000/api/system/healthz
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

## How a transcription request travels

A route in `src/loreline/web/routes/` takes the provider row and the model the
caller named. `stt.registry.create_backend(config, secrets, model)` resolves the
kind and the model's transport to one connector, so the transport lookup and the
connector can never disagree about which model is running. `SttRouter` then
feeds utterances to that connector, falls over to the fallback provider when it
fails, and hands the events to the diarizer.

The connector itself holds no addresses. `capabilities.surface_for` returns the
URL and the auth scheme the yaml declares for that interaction and transport,
with the provider row's `base_url` applied where the surface says it may be. The
same accessor answers for the LLM client, the video client, the catalogue reader
and the diarizer.

Four decisions shape all of this and each has an ADR under `docs/adr/`: the
connector base and what composes it, vendor surfaces living in the yaml, one
catalogue reader behind every model list, and one health probe behind every
"does this key work" question. Read those before moving a fact out of the yaml
and into code. `CONTEXT.md` has the vocabulary.

## Adding an STT backend

1. Add the vendor to `src/loreline/capabilities.yaml`: a provider block with its
   `surfaces` (a `url` and an `auth` per interaction and transport, a `catalog`
   surface, a `health` path or frame), its `interactions`, and its curated
   models with what each supports.
2. Implement `Connector` or `HttpConnector` from `src/loreline/stt/base.py`.
   You supply `prepare` and `transcribe_one`; the base owns the per-utterance
   loop, the `TranscriptEvent` and the speaker rule. The `STTBackend` contract
   is `transcribe()` and `aclose()`. There is no `health()`: probing is
   `loreline.health_probe`'s job, driven by the surface in the yaml.
3. Register the factory with `@register(ProviderKind.YOURS)`, adding
   `realtime=True` for a streaming connector. One kind can register both. See
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
