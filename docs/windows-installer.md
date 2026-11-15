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
2. **Sources.** These work without any account: the France Travail website, EURES, RemoteOK,
   Jobicy and We Work Remotely. HelloWork, LinkedIn and Indeed are unofficial: they run only if
   you choose them. A run's **Sources** table shows each source as *completed*, *partial*,
   *failed* or *skipped*, with the reason for a skip.

   Advanced: the France Travail API is restricted and not required. If you already hold access,
   put `FRANCE_TRAVAIL_CLIENT_ID` and `FRANCE_TRAVAIL_CLIENT_SECRET` in `.env` in the install
   folder and restart the app.

   Searches are built from your profile, so any line of work can be searched. The remote
   boards (RemoteOK, Jobicy, We Work Remotely) list mostly digital roles.
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
- The backend listens on `127.0.0.1` at a free port chosen at each start, so another program on
  port 8000 no longer matters. `FYJ_PORT=8010` in `.env` fixes a port instead.

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
| `config\*.yaml` (defaults) | you, once installed | a file you never edited moves to the new default; **a file you edited is kept**, and gains only the entries the new version needs (the daily search, France Travail web) | kept |
| `config\*.local.yaml` (profile, notifications) | you | never touched | kept |
| `data\` (jobs, decisions, feedback, lab) | you | never touched | kept |
| `.env`, `logs\` | you | never touched | kept |

- **Upgrade:** run the newer installer. It stops the running app through its launcher
  first, replaces the application files and keeps everything else. When the new version
  first starts, it copies the database to `data\backups\jobs-upgrade-<version>-<time>.db`
  and every configuration file it changes to `data\backups\config-<time>\`, then upgrades
  the database and the configuration in place.
- **Restore the database from before an upgrade:** stop the app. In `data\`, delete
  `jobs.db`, `jobs.db-wal` and `jobs.db-shm`, then copy
  `data\backups\jobs-upgrade-<version>-<time>.db` to `data\jobs.db`. Start the app: it
  makes a new backup and upgrades the restored database again. Anything found or changed
  since that upgrade is lost. To undo configuration changes too, copy the files from
  `data\backups\config-<time>\` back to `config\`.
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
| `port 8010 is already used by another program` | only with `FYJ_PORT` set: close that program, or remove `FYJ_PORT` from `.env` |
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
`--only-binary=:all:`, then `pip freeze`, dropping pip and wheel), keep the header, run
`python packaging\windows\hash_requirements.py` to add each pinned file's SHA-256 from PyPI
(the build installs with `--require-hashes`: a file that does not match fails the build), and
run `pip-audit -r packaging\windows\requirements.txt --no-deps --disable-pip`. CI runs
that audit on every pull request (`Dependency audit`), and
`tests/test_windows_package.py` checks that every declared dependency is pinned and hashed.

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
