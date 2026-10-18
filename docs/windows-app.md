# Running the app on Windows

`FindYourJob.cmd`, at the repository root, is the one entry point. Double-click it (or
pin a shortcut, see below): it checks the installation, builds the dashboard when its
sources changed, starts the backend, opens the dashboard, and stops everything it
started when you press **Ctrl+C** or close its window.

The window is the application's control panel: it shows each component's state and
where the logs are. The dashboard opens in a chrome-less app window when Microsoft Edge
or Google Chrome is installed, otherwise in your default browser.

## What runs, and who manages it

| Component | Role | Required? | What the launcher does |
|---|---|---|---|
| Backend (FastAPI, `src/api`) | API, pipeline runs, scanners, scheduler; serves the dashboard | yes | starts it on `127.0.0.1:8000`, or reuses one that is already running |
| Dashboard (`frontend/dist`) | the web UI, built once with Vite | yes | rebuilds it when `frontend/` sources are newer than the build (needs Node.js and `frontend/node_modules`) |
| SQLite (`data/jobs.db`, `data/esco.db`) | all application data | yes | nothing: the backend opens and migrates it; the launcher never touches data |
| n8n | weekly e-mail digest only | no | starts it with `--n8n` if it is installed and not running, bound to `127.0.0.1` with telemetry off |
| LM Studio / Ollama | local LLM, only for AI-enabled runs (`use_ai`) | no | never starts it; reports whether it answers at `PHASE2_LLM_ENDPOINT` and warns if that address is not local |
| Discord | optional notifications | no | nothing: configured through `.env` |

**Ownership rule.** The launcher stops only processes it started itself. Every child it
starts is placed in a Windows *job object* with kill-on-close before its first
instruction runs, so if the launcher crashes or its window is closed, its children —
and anything they spawned — end with it. A backend or n8n that was already running (the
startup tasks below, a terminal, another tool) is reused and shown as "external", and is
never stopped. A second double-click while the app runs just opens the dashboard again.

## Commands

```bat
FindYourJob.cmd                  :: same as "start"
FindYourJob.cmd start --n8n      :: also start n8n for the weekly digest
FindYourJob.cmd start --window browser   :: default browser instead of an app window
FindYourJob.cmd start --port 8100        :: another backend port (or FYJ_PORT in .env)
FindYourJob.cmd status           :: what is running and who owns it; starts nothing
FindYourJob.cmd stop             :: ask the running launcher to stop what it started
FindYourJob.cmd dev              :: development: backend + Vite dev server (hot reload)
FindYourJob.cmd dev --reload     :: ... and restart the backend on code changes
FindYourJob.cmd shortcut         :: add a Start menu shortcut (--where desktop, --remove)
```

`python -m src.launcher ...` is the same thing from an activated virtual environment.

Exit codes: `0` success, `1` a component failed to start (the last lines of its log
are printed), `2` the installation is incomplete (each problem is listed with its fix).

## First-time setup

The launcher never installs anything. It tells you what is missing:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .
cd frontend && npm ci && npm run build
copy .env.example .env      :: optional; fill in only what you use
```

The first pipeline run downloads the sentence-embedding model (about 470 MB, once, from
the Hugging Face Hub).

## Logs

| File | Content |
|---|---|
| `logs\launcher\launcher.log` | what the launcher printed |
| `logs\launcher\backend.log`, `n8n.log`, `vite.log`, `frontend-build.log` | raw output of each child the launcher started (one previous generation kept past 5 MB) |
| `logs\backend\backend-<date>.log` | the backend's own application log (rotating) |

`logs/` is git-ignored. The launcher never prints or logs the content of `.env`.

## Running at logon instead

`scripts\install_startup.py` and `scripts\install_n8n_startup.py` register per-user Task
Scheduler tasks (`FindYourJobWithAI_Backend`, `FindYourJobWithAI_N8n`) that start the
backend and n8n in the background at logon, without the launcher. They still work, and
the launcher detects and reuses what they started. Remove them with
`scripts\uninstall_startup.py` / `scripts\uninstall_n8n_startup.py`. Nothing is
registered unless you run those scripts yourself.

## Installing, updating, uninstalling

There is no installer: the app runs from its folder (which may contain spaces).

- **Update:** stop the app, `git pull`, then `.venv\Scripts\python -m pip install -e .`
  if `pyproject.toml` changed and `cd frontend && npm ci` if `package-lock.json`
  changed. The launcher rebuilds the dashboard by itself.
- **Uninstall:** stop the app, remove the shortcut (`FindYourJob.cmd shortcut --remove`)
  and any startup task (scripts above), then delete the folder. Back up `data/` and your
  `config/*.local.yaml` and `.env` first if you want to keep them. The embedding model
  cache lives in `%USERPROFILE%\.cache\huggingface`.

## Security notes

- The backend binds to `127.0.0.1` only, and refuses requests whose `Host` is not this
  machine (DNS rebinding) and cross-site writes from web pages
  (`src/api/origin_guard.py`). It has no login: every process of every account on this
  machine can call it. `API_SAME_USER_ONLY=1` (in `.env`) restricts it to processes of
  your Windows account — recommended when another account (such as the isolated lab
  agent) uses this machine; the dev server cannot be used in that mode.
- n8n started by the launcher (or `scripts\Start_n8n.bat`) listens on `127.0.0.1`
  unless you set `N8N_LISTEN_ADDRESS` yourself.
- The stop request is a named event in your logon session; only your own processes can
  send it, and it can only stop the app.

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| `[XX] packages ... missing` | run `.venv\Scripts\python -m pip install -e .` |
| `[XX] dashboard is not built` | install Node.js 22 LTS (or 20.19+), then `cd frontend && npm ci && npm run build` |
| `port 8000 is already used by another program` | something that is not this app holds the port: stop it, or `FindYourJob.cmd --port 8100` |
| `backend exited during startup` | the printed log lines say why (often a broken `.env` value or a locked database); full output in `logs\launcher\backend.log` |
| `backend did not become ready within 180s` | startup on a large database can be slow: `--timeout 600` |
| n8n `not running` | optional; `FindYourJob.cmd start --n8n`, or install it with `npm install -g n8n` |
| `PHASE2_LLM_ENDPOINT points to a non-local address` | AI-enabled runs would send job text to that host; point it at LM Studio/Ollama on this machine |
