# Contributing

## Getting set up

Loreline is a Python (FastAPI) backend with a SvelteKit frontend.
[`uv`](https://docs.astral.sh/uv/) manages the Python side.

```bash
uv sync                      # base deps + dev group
uv run loreline version
uv run loreline run --reload # http://127.0.0.1:8000
curl http://127.0.0.1:8000/api/system/healthz
```

Audio capture needs the native extra (PortAudio must be present on the host):

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

The backend only serves the web UI at `/` when `frontend/build/` exists - run
`npm run build` if you want to test the production-style single-origin setup.

## Checks

Everything CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest

cd frontend && npm run lint && npm run check && npm run build
```

`uv run prek install` wires the git hooks up. prek is a drop-in Rust
reimplementation of pre-commit and reads the same
`.pre-commit-config.yaml`; `uv run prek run --all-files` runs every hook over
the whole tree.

> **Tip:** run uv with `--frozen` (or `export UV_FROZEN=1`) so it never rewrites
> `uv.lock`. A user-global `exclude-newer` in `~/.config/uv/uv.toml` otherwise makes
> every `uv run` re-resolve and dirty the lock. CI already runs `uv sync --frozen`.
> When you intentionally change dependencies, run `uv lock` explicitly.

## Layout

| Path | What |
|---|---|
| `src/loreline/audio/` | capture, VAD chunking, WAV writing, device enumeration |
| `src/loreline/stt/` | STT backends + the primary/fallback router |
| `src/loreline/session/` | session lifecycle and orchestration |
| `src/loreline/persistence/` | SQLite repositories |
| `src/loreline/web/` | FastAPI app, routes, auth |
| `frontend/` | SvelteKit SPA |
| `services/diarization/` | self-hosted sherpa-onnx diarization service |
| `deploy/` | install/update scripts, systemd units |
| `mocks/` | mock provider servers for offline testing |

## Adding an STT backend

1. Implement the `STTBackend` protocol in `src/loreline/stt/base.py` - `transcribe()`,
   `health()`, `aclose()`.
2. Register it with `@register(ProviderKind.YOURS)` (see any file in
   `src/loreline/stt/backends/` for the shape).
3. Add the kind to `ProviderKind` in `src/loreline/models.py` and to the provider
   catalogue in `frontend/src/routes/settings/providers/+page.svelte`.
4. Cover it with a test that injects a fake client rather than hitting the network -
   `tests/integration/test_google.py` is a good template.

Backends are imported lazily, so provider SDKs stay optional: the module must import
cleanly even when its SDK isn't installed.

## Notes

- Provider credentials live in `data/secrets.json` (`0600`), never in the database.
  Environment variables (`LORELINE_SECRET_<NAME>`) override stored values.
- Deployment specifics that cost real time to discover - Bluetooth audio in containers,
  installer pitfalls, network gotchas - are collected in
  [`DEPLOYMENT-NOTES.md`](./DEPLOYMENT-NOTES.md).
