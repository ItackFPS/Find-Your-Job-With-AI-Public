# Windows installer

`FindYourJobWithAI-Setup-<version>.exe` installs the app for your Windows account, with
its own private Python, the prebuilt dashboard, a desktop icon and Start Menu entries. You
need no Python, Node.js, Git or terminal, and no administrator rights.

Running from a source checkout (`FindYourJob.cmd` in a clone, see the
[README](../README.md#quick-start-windows)) still works; this page is about the installed app.

## Install

1. Download `FindYourJobWithAI-Setup-<version>.exe` and `….exe.sha256` from the
   [latest release](https://github.com/ItackFPS/Find-Your-Job-With-AI-Public/releases/latest).
   In PowerShell, `Get-FileHash .\FindYourJobWithAI-Setup-<version>.exe` must print the hash in
   the `.sha256` file. Then run the `.exe`. Windows SmartScreen may warn that the
   publisher is unknown (the installer is not code-signed): **More info → Run anyway**
   once the hash matches.
2. Keep or untick **Create a desktop shortcut**, then **Install**.
3. Leave **Start Find Your Job With AI now** ticked to open it at the end.

| What | Where |
|---|---|
| Application and your data | `%LOCALAPPDATA%\Programs\Find Your Job With AI` (one folder, see below) |
| Start Menu | **Find Your Job With AI**, **Stop Find Your Job With AI**, **… logs**, **… self-check**, **Uninstall …** |
| Desktop | **Find Your Job With AI** (optional) |
| Disk | about 300 MB installed (62 MB download), plus ~470 MB for the embedding model on the first run |

Requirements: Windows 10 1809 or later / Windows 11, 64-bit. Nothing else.

## First run

Double-click **Find Your Job With AI**. The app starts in the notification area (an
icon next to the clock, no console window): it checks the installation, starts the
backend on `127.0.0.1` (this computer only), waits until it answers, then opens the
dashboard in an app window (Edge or Chrome) or your browser. The first start takes up to a
minute or two; later starts take seconds. The first time, a notification explains that
the app keeps running in the notification area.

**Closing the dashboard window does not stop the app**: the backend, its schedules and any
LinkedIn or HelloWork scan keep running. Reopen the dashboard from the icon (double-click,
or right-click › **Open**). Right-click the icon for **Start with Windows** (off by
default; it starts the app in the notification area when you sign in), **Open logs
folder** and **Quit**. A notification says when a job search finishes. If the backend
stops unexpectedly it is started again once, and a notification says so.

**Nothing is collected until you ask.** Opening the app starts no scan and no pipeline run;
the scanners are manual in the shipped settings and nothing runs at logon.

Then:

1. **Profile** page: your target roles, skills, locations, languages. It is saved to
   `config\profile.local.yaml`.
2. **Sources.** These work without any account: RemoteOK, Jobicy, We Work Remotely and
   EURES (pipeline run), HelloWork and LinkedIn (**Scan now** on their dashboard cards).
   Two sources need credentials and are *skipped* until you add them:

   | Source | What to set in `.env` | Where to get it |
   |---|---|---|
   | France Travail | `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | free application at <https://developer.francetravail.fr> |
   | Indeed | `INDEED_API_KEY` | see the comment in `.env.example` |

   To add them: in the install folder, copy `.env.example` to `.env`, fill in only those
   lines, then stop and start the app. A run's **Sources** table shows each source as
   *completed*, *partial*, *failed* or *skipped*, with the reason for a skip.
3. **Run pipeline** on the Dashboard (or **Scan now** on a scanner card). The first run
   downloads the multilingual embedding model (~470 MB) from Hugging Face once.

Local AI (LM Studio / Ollama) is off. The app never sends job data to a cloud AI service;
see the README's [Local AI](../README.md#local-ai) section to opt in to a local model.

## Stop, restart

- **Stop:** right-click the notification-area icon › **Quit**, or use **Stop Find Your
  Job With AI** in the Start Menu. Closing the dashboard window does not stop it. A running LinkedIn or HelloWork scan stops at its next
  checkpoint and can be resumed from its card later. A pipeline run still in progress is
  abandoned: the next start marks it *failed* (stale), and you run it again. SQLite keeps
  everything already written.
- **Restart:** the same shortcut again. Launching while the app already runs only opens the
  dashboard again; it never starts a second backend. Launching while it is still stopping
  waits until it has stopped, then starts it.
- If port 8000 is taken by another program, `logs\launcher\launcher.log` says so. Set another port: add
  `FYJ_PORT=8010` to `.env`.

## Self-check and the Lab

Start Menu → **Find Your Job With AI self-check** replays the current strategy on the
bundled synthetic benchmark (50 labelled jobs), compares it with its recorded baseline and
prints the result; the report then appears on the **Lab** page. It reports safety
(unsafe rejections) and eligibility quality separately; ranking quality is `not_measured`
until reviewed relevance labels exist. Nothing it finds changes your strategy: only a
human approval from the command line (`python -m src.lab approve`, see
[ADR 0011](adr/0011-experiment-lab.md)) can promote one.

## Logs

Start Menu → **Find Your Job With AI logs**, or the `logs` folder of the install folder:

| File | Contents |
|---|---|
| `logs\launcher\launcher.log` | what the launcher checked, started and stopped |
| `logs\launcher\backend.log` | the backend's console output (startup errors land here) |
| `logs\backend\backend-<date>.log` | the application log |

Logs contain no credentials. They do contain job titles and your search activity: look
through them before sharing.

## Your data, upgrades, uninstall

The install folder holds both the application and your data:

| Path | Owner | On upgrade | On uninstall |
|---|---|---|---|
| `src\`, `runtime\`, `frontend\dist\`, `scripts\`, `docs\` | application | replaced | removed |
| `config\*.yaml` (defaults) | you, once installed | **kept as you left them** (only missing files are added) | kept |
| `config\*.local.yaml` (profile, notifications) | you | never touched | kept |
| `data\` (jobs, decisions, feedback, lab) | you | never touched | kept |
| `.env`, `logs\` | you | never touched | kept |

- **Upgrade:** run the newer installer. It stops the running app through its launcher
  first, replaces the application files and keeps everything else. The database is
  upgraded in place when the new version starts. Back up `data\` first if you want a way
  back (copy the folder while the app is stopped).
- **Uninstall:** Settings → Apps → **Find Your Job With AI** → Uninstall (or the Start
  Menu entry). The application files and shortcuts are removed; you are then asked whether
  to delete your data too (default **No**). A silent uninstall always keeps the data. To
  remove everything later, delete the install folder.
- The embedding model lives in your Hugging Face cache (`%USERPROFILE%\.cache\huggingface`)
  and is shared with other tools; delete it yourself if you want the space back.

## Troubleshooting

| Symptom | What to do |
|---|---|
| The console shows `[XX] packages` or `[XX] dashboard` | the installation is damaged: run the installer again (data is kept) |
| `port 8000 is already used by another program` | close that program, or set `FYJ_PORT` in `.env` |
| The window closes at once | run **Find Your Job With AI** from the Start Menu; if it still closes, open `logs\launcher\launcher.log` |
| `backend exited during startup` | the last log lines are printed; the full output is `logs\launcher\backend.log` |
| A source shows *failed* | its error is listed under the run (e.g. a site answering HTTP 403 blocks automated access); the other sources are unaffected |
| SmartScreen blocks the installer | **More info → Run anyway**, after checking the SHA-256 |

## Building the installer

On a Windows build machine with Git, Node.js 22 LTS, CPython 3.12 x64 from python.org
and [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```bat
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

It packages the **committed** revision (`-Revision`, default `HEAD`; uncommitted changes
are ignored), builds the dashboard, copies the Python 3.12 base installation into
`runtime\` (minus tests, IDLE and Tk) with a `._pth` file that isolates it from
`PYTHONPATH` and the user's site-packages, installs `packaging\windows\requirements.txt`,
refuses any `.env`, database, log or `data\` file in the stage, checks that the backend
imports from outside any checkout, and compiles `packaging\windows\FindYourJob.iss`.
Output: `dist\windows\FindYourJobWithAI-Setup-<version>.exe` and its `.sha256`. The
version comes from `pyproject.toml`; `build-info.json` in the install folder records the
version, commit and Python version.

`-StageOnly` stops before Inno Setup (`build\windows\stage`).

### Acceptance test

```bat
powershell -ExecutionPolicy Bypass -File packaging\windows\acceptance.ps1 -Installer dist\windows\FindYourJobWithAI-Setup-<version>.exe
```

Installs silently into a test folder under `%TEMP%` (refuses to run if the app is already
installed for your account), then checks the shortcuts, a first launch on loopback with no
scan started, a second launch reusing the app, stop and restart with settings kept, an
upgrade while running that keeps data and edited config, and an uninstall that keeps user
data. It uses port 8765 and starts no scan. `-Stage build\windows\stage` checks a stage
without the setup program.

### Updating the pinned dependencies

`packaging\windows\requirements.txt` pins the runtime set (the project's dependencies
plus the `linkedin` extra). After changing `pyproject.toml`, regenerate it in a fresh
virtual environment of CPython 3.12 x64 (install the dependencies with
`--only-binary=:all:`, then `pip freeze`, dropping pip and wheel), keep the header, and
run `pip-audit -r packaging\windows\requirements.txt --no-deps --disable-pip`. CI runs
that audit on every pull request (`Dependency audit`), and
`tests/test_windows_package.py` checks that every declared dependency is pinned.

## Limitations

- The installer and launcher are not code-signed (SmartScreen warning).
- The bundled Python is the build machine's CPython 3.12; rebuild with the latest 3.12
  patch release to pick up its security fixes.
- Application files and user data share one folder; the uninstaller keeps the data, but
  "repair" means reinstalling over it.
- The installer does not set up the lab agent's sandbox. Nothing in the app starts a lab agent;
  if you run one, use the AppContainer sandbox (from the install folder:
  `runtime\python.exe -m src.lab sandbox grant --apply`, then `sandbox run -- python -m src.lab …`;
  see [docs/development.md](development.md#sandboxed-lab-agent-windows-appcontainer-no-administrator)).
