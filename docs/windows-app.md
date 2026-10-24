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
| Backend (FastAPI, `src/api`) | API, pipeline runs, scanners, scheduler; serves the dashboard | yes | starts it on `127.0.0.1` at a free port chosen at start (`FYJ_PORT` fixes one); connects only to the backend that returns this instance's token |
| Dashboard (`frontend/dist`) | the web UI, built once with Vite | yes | rebuilds it when `frontend/` sources are newer than the build (needs Node.js and `frontend/node_modules`) |
| SQLite (`data/jobs.db`, `data/esco.db`) | all application data | yes | nothing: the backend opens and migrates it; the launcher never touches data |
| LM Studio / Ollama | local LLM, only for AI-enabled runs (`use_ai`) | no | never starts it; reports whether it answers at `PHASE2_LLM_ENDPOINT` and warns if that address is not local |

**Ownership rule.** The launcher stops only processes it started itself. Every child it
starts is placed in a Windows *job object* with kill-on-close before its first
instruction runs, so if the launcher crashes or its window is closed, its children —
and anything they spawned — end with it. A backend that was already running (the
startup tasks below, a terminal, another tool) is reused and shown as "external", and is
never stopped. A second double-click while the app runs just opens the dashboard again.

## Commands

```bat
FindYourJob.cmd                  :: same as "start"
FindYourJob.cmd tray             :: the installed app's mode: no console, an icon in the
                                 ::   notification area; closing the dashboard keeps scans
                                 ::   running, Quit from the icon stops everything
FindYourJob.cmd autostart on     :: installed app only: start it (tray) when you sign in;
                                 ::   "off", "status"
FindYourJob.cmd start --window browser   :: default browser instead of an app window
FindYourJob.cmd start --port 8100        :: a fixed backend port (or FYJ_PORT in .env);
                                 ::   default: a free port at each start (dev: 8000)
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
| `logs\launcher\backend.log`, `vite.log`, `frontend-build.log` | raw output of each child the launcher started (one previous generation kept past 5 MB) |
| `logs\backend\backend-<date>.log` | the backend's own application log (rotating) |

`logs/` is git-ignored. The launcher never prints or logs the content of `.env`.

## Running at logon instead

`scripts\install_startup.py` registers a per-user Task Scheduler task
(`FindYourJobWithAI_Backend`) that starts the backend in the background at logon, without
the launcher. It still works, and the launcher detects and reuses what it started. Remove
it with `scripts\uninstall_startup.py`. Nothing is registered unless you run that script
yourself. n8n support was removed in 2.0: if you once registered its logon task, remove it
with `scripts\uninstall_n8n_startup.py`.

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
- The stop request is a named event in your logon session; only your own processes can
  send it, and it can only stop the app.

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| `[XX] packages ... missing` | run `.venv\Scripts\python -m pip install -e .` |
| `[XX] dashboard is not built` | install Node.js 22 LTS (or 20.19+), then `cd frontend && npm ci && npm run build` |
| `port 8100 is already used by another program` | only with a fixed port (`FYJ_PORT`, `--port`): stop that program, choose another port, or remove `FYJ_PORT` to let the launcher pick a free one |
| `port ... is used by another copy of Find Your Job With AI` | a backend this launcher did not start (another install, a terminal, the startup task) answers on the fixed port without this instance's token: the launcher never connects to it. Stop it, or remove `FYJ_PORT` |
| `backend exited during startup` | the printed log lines say why (often a broken `.env` value or a locked database); full output in `logs\launcher\backend.log` |
| `backend did not become ready within 180s` | startup on a large database can be slow: `--timeout 600` |
| `PHASE2_LLM_ENDPOINT points to a non-local address` | AI-enabled runs refuse that host and run without AI; point it at LM Studio/Ollama on this machine, or set `FYJ_ALLOW_REMOTE_LLM=1` for a server you control |
