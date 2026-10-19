<#
.SYNOPSIS
    Build the Windows installer of Find Your Job With AI from a committed revision.

.DESCRIPTION
    1. Exports the committed tree of -Revision (default HEAD) with "git archive": nothing
       untracked or ignored (data/, .env, *.local.yaml, logs/) can reach the package.
    2. Builds the dashboard (npm ci + npm run build) in that export.
    3. Stages the application: code, default config, prebuilt dashboard, the synthetic
       evaluation fixtures the self-check uses, docs, and a private Python runtime
       copied from a python.org CPython 3.12 x64 installation, with the pinned runtime
       dependencies (packaging/windows/requirements.txt) installed into it.
    4. Checks the stage (no secrets, databases or personal files; imports work from
       outside any checkout) and writes build-info.json.
    5. Compiles packaging/windows/FindYourJob.iss with Inno Setup 6 (ISCC.exe).

    Output: dist/windows/FindYourJobWithAI-Setup-<version>.exe (+ .sha256).

    Prerequisites (build machine only; the installed app needs none of them):
      * git, Node.js 22 LTS (or 20.19+) with npm
      * CPython 3.12 x64 from python.org (the "py" launcher finds it, or pass -PythonHome)
      * Inno Setup 6 (ISCC.exe on PATH, in its default folder, or pass -Iscc)
      * network access to PyPI and the npm registry

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -StageOnly
#>
[CmdletBinding()]
param(
    [string]$Revision = "HEAD",
    [string]$PythonHome = "",
    [string]$Iscc = "",
    [string]$BuildDir = "",
    [string]$OutputDir = "",
    # Stage and check the application without compiling the installer (no Inno Setup needed).
    [switch]$StageOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $BuildDir) { $BuildDir = Join-Path $RepoRoot "build\windows" }
if (-not $OutputDir) { $OutputDir = Join-Path $RepoRoot "dist\windows" }

function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Fail([string]$Text) { Write-Host "error: $Text" -ForegroundColor Red; exit 1 }
function Invoke-Checked([string]$Exe, [string[]]$Arguments, [string]$What) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)" }
}

# -- prerequisites ------------------------------------------------------------------------------

Step "Checking prerequisites"
foreach ($tool in "git", "npm", "tar") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool is not installed or not on PATH" }
}
if (-not $PythonHome) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { Fail "no 'py' launcher: install CPython 3.12 x64 from python.org or pass -PythonHome" }
    $PythonHome = (& py -3.12 -c "import sys; print(sys.base_prefix)").Trim()
    if ($LASTEXITCODE -ne 0) { Fail "CPython 3.12 not found by 'py -3.12': install it or pass -PythonHome" }
}
$BasePython = Join-Path $PythonHome "python.exe"
if (-not (Test-Path $BasePython)) { Fail "no python.exe in $PythonHome" }
$PyInfo = (& $BasePython -c "import sys, platform; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]} {platform.architecture()[0]} {sys.prefix == sys.base_prefix}')").Trim().Split(" ")
if ($PyInfo[0] -notlike "3.12.*") { Fail "$BasePython is Python $($PyInfo[0]); the package needs 3.12" }
if ($PyInfo[1] -ne "64bit") { Fail "$BasePython is $($PyInfo[1]); the package needs 64-bit Python" }
if ($PyInfo[2] -ne "True") { Fail "$PythonHome is a virtual environment; pass the base installation (sys.base_prefix)" }
if (-not $StageOnly) {
    if (-not $Iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $Iscc = $cmd.Source }
        foreach ($candidate in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                                 "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                                 "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
            if (-not $Iscc -and (Test-Path $candidate)) { $Iscc = $candidate }
        }
    }
    if (-not $Iscc -or -not (Test-Path $Iscc)) {
        Fail "Inno Setup 6 (ISCC.exe) not found: install it from https://jrsoftware.org/isinfo.php, pass -Iscc, or use -StageOnly"
    }
}

$Commit = (& git -C $RepoRoot rev-parse --verify "$Revision^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { Fail "unknown revision $Revision" }
$Dirty = [bool](& git -C $RepoRoot status --porcelain --untracked-files=no)
if ($Dirty -and $Revision -eq "HEAD") {
    Write-Host "note: the working tree has uncommitted changes; they are NOT packaged (building $Commit)" -ForegroundColor Yellow
}

# -- export ---------------------------------------------------------------------------------------

Step "Exporting $Commit"
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
$Export = Join-Path $BuildDir "export"
$Stage = Join-Path $BuildDir "stage"
New-Item -ItemType Directory -Force $Export, $Stage | Out-Null
$Tar = Join-Path $BuildDir "export.tar"
Invoke-Checked git @("-C", $RepoRoot, "archive", "--format=tar", "-o", $Tar, $Commit) "git archive"
Invoke-Checked tar @("-xf", $Tar, "-C", $Export) "extracting the export"
Remove-Item $Tar

$Version = (& $BasePython -c "import tomllib,sys; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $Export "pyproject.toml")).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) { Fail "cannot read the version from pyproject.toml" }
# Windows file versions are four numbers: 1.0.0rc1 -> 1.0.0.0
$Numbers = @([regex]::Matches($Version, "^\d+(\.\d+){0,3}").Value.Split(".")) + @("0", "0", "0", "0")
$VersionInfo = ($Numbers[0..3] -join ".")

# -- dashboard -------------------------------------------------------------------------------------

Step "Building the dashboard"
Push-Location (Join-Path $Export "frontend")
try {
    Invoke-Checked npm @("ci", "--no-audit", "--no-fund") "npm ci"
    Invoke-Checked npm @("run", "build") "npm run build"
} finally { Pop-Location }

# -- stage the application --------------------------------------------------------------------------

Step "Staging the application"
$Items = @("src", "config", "scripts", "docs", "FindYourJob.cmd", "README.md", "LICENSE", "SECURITY.md",
           ".env.example", "pyproject.toml")
foreach ($item in $Items) {
    Copy-Item -Recurse -Force (Join-Path $Export $item) (Join-Path $Stage $item)
}
New-Item -ItemType Directory -Force (Join-Path $Stage "frontend") | Out-Null
Copy-Item -Recurse -Force (Join-Path $Export "frontend\dist") (Join-Path $Stage "frontend\dist")
# The self-check's committed benchmark and its recorded baseline (src/evaluation/__main__.py).
$Fixtures = Join-Path $Export "tests\fixtures\evaluation"
if (Test-Path $Fixtures) {
    New-Item -ItemType Directory -Force (Join-Path $Stage "tests\fixtures") | Out-Null
    Copy-Item -Recurse -Force $Fixtures (Join-Path $Stage "tests\fixtures\evaluation")
}
Copy-Item -Force (Join-Path $PSScriptRoot "FindYourJob.ico") (Join-Path $Stage "FindYourJob.ico")

# -- private Python runtime ---------------------------------------------------------------------------

Step "Copying CPython $($PyInfo[0]) from $PythonHome"
$Runtime = Join-Path $Stage "runtime"
New-Item -ItemType Directory -Force $Runtime | Out-Null
Get-ChildItem $PythonHome -File | Where-Object { $_.Extension -in ".exe", ".dll", ".txt" } |
    Copy-Item -Destination $Runtime
Copy-Item -Recurse (Join-Path $PythonHome "DLLs") (Join-Path $Runtime "DLLs")
# robocopy: the standard library without site-packages, tests, IDLE and Tk
$RoboArgs = @((Join-Path $PythonHome "Lib"), (Join-Path $Runtime "Lib"), "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
              "/XD", "site-packages", "__pycache__", "test", "idlelib", "tkinter", "turtledemo")
& robocopy @RoboArgs | Out-Null
if ($LASTEXITCODE -ge 8) { Fail "copying the standard library failed (robocopy $LASTEXITCODE)" }
Get-ChildItem (Join-Path $Runtime "DLLs") -Include "_tkinter*", "tcl*", "tk*" -Recurse | Remove-Item -Force
New-Item -ItemType Directory -Force (Join-Path $Runtime "Lib\site-packages") | Out-Null
# A ._pth file makes this interpreter ignore PYTHONPATH/PYTHONHOME and the user's site-packages,
# and puts the application folder (..) on sys.path: the installed app never sees another Python.
$Pth = "python{0}{1}._pth" -f $PyInfo[0].Split(".")[0], $PyInfo[0].Split(".")[1]
Set-Content -Encoding ascii (Join-Path $Runtime $Pth) "Lib`r`nDLLs`r`n..`r`nimport site"
$RuntimePython = Join-Path $Runtime "python.exe"

Step "Installing the runtime dependencies (packaging/windows/requirements.txt)"
$Requirements = Join-Path $Export "packaging\windows\requirements.txt"
Invoke-Checked $RuntimePython @("-m", "ensurepip", "--default-pip") "ensurepip"
Invoke-Checked $RuntimePython @("-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                                "--only-binary=:all:", "-r", $Requirements) "pip install"
Invoke-Checked $RuntimePython @("-m", "pip", "check") "pip check"
# The installed app never installs packages: drop the installer tools (and their advisories).
Invoke-Checked $RuntimePython @("-m", "pip", "uninstall", "--disable-pip-version-check", "-y", "pip") "removing pip"
foreach ($leftover in "Scripts") {
    $p = Join-Path $Runtime $leftover
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

# -- checks -------------------------------------------------------------------------------------------

Step "Checking the stage"
$Forbidden = Get-ChildItem -Recurse -Force $Stage -File | Where-Object {
    $rel = $_.FullName.Substring($Stage.Length + 1)
    $_.Name -eq ".env" -or $_.Name -like ".env.*" -and $_.Name -ne ".env.example" -or
    $_.Name -like "*.local.*" -or $_.Extension -in ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".log" -or
    $rel -like "data\*" -or $rel -like "logs\*"
}
if ($Forbidden) { Fail ("personal or secret files in the stage:`n  " + ($Forbidden.FullName -join "`n  ")) }

# Import the backend from outside any checkout, with a hostile environment: the ._pth must win.
$Probe = Join-Path $BuildDir "probe"
New-Item -ItemType Directory -Force $Probe | Out-Null
Push-Location $Probe
try {
    $env:PYTHONPATH = "C:\does-not-exist"; $env:PYTHONHOME = "C:\does-not-exist"
    Invoke-Checked $RuntimePython @("-c", "import sys, src.api.app, src.launcher.core as c; from pathlib import Path; assert c.bundled_runtime(Path(c.project_root())), sys.prefix; print('backend imports ok from', c.project_root())") "import check"
} finally {
    Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue
    Pop-Location
    Remove-Item -Recurse -Force $Probe
}

$Packages = & $RuntimePython -c "import importlib.metadata as m; print('\n'.join(sorted(f'{d.metadata[\""Name\""]}=={d.version}' for d in m.distributions())))"
$Utf8 = New-Object System.Text.UTF8Encoding $false  # no BOM: Python reads these files
[IO.File]::WriteAllText((Join-Path $Stage "runtime-packages.txt"), ($Packages -join "`n") + "`n", $Utf8)
$Info = [ordered]@{
    app = "find-your-job-with-ai"; version = $Version; commit = $Commit; dirty_worktree_ignored = $Dirty
    python = $PyInfo[0]; built_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
[IO.File]::WriteAllText((Join-Path $Stage "build-info.json"), ($Info | ConvertTo-Json), $Utf8)
# The bundled interpreter never writes bytecode (._pth mode implies -B), so without these every
# start would compile the whole stack again (measured: 80 s cold, 20 s warm). checked-hash .pyc
# files stay valid whatever timestamps the installer gives the sources.
Step "Precompiling bytecode"
# -s: record paths relative to the stage, not this machine's checkout (Python reports the real
# location at run time anyway).
& $RuntimePython -m compileall -q -j 0 --invalidation-mode checked-hash -s $Stage (Join-Path $Runtime "Lib") (Join-Path $Stage "src") | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "note: some files did not compile (unused test or template files in packages); they run from source" -ForegroundColor Yellow }

Step "Checking that no staged file names this checkout"
Invoke-Checked $BasePython @((Join-Path $PSScriptRoot "find_build_path.py"), $Stage, $RepoRoot) "build path check"

if ($StageOnly) {
    Write-Host "Stage ready (not compiled): $Stage" -ForegroundColor Green
    exit 0
}

# -- installer ----------------------------------------------------------------------------------------

Step "Compiling the installer with $Iscc"
New-Item -ItemType Directory -Force $OutputDir | Out-Null
Invoke-Checked $Iscc @("/Q", "/DAppVersion=$Version", "/DVersionInfo=$VersionInfo", "/DStageDir=$Stage",
                       "/DOutputDir=$OutputDir", (Join-Path $PSScriptRoot "FindYourJob.iss")) "ISCC"
$Setup = Join-Path $OutputDir "FindYourJobWithAI-Setup-$Version.exe"
if (-not (Test-Path $Setup)) { Fail "ISCC reported success but $Setup is missing" }
$Hash = (Get-FileHash -Algorithm SHA256 $Setup).Hash.ToLower()
Set-Content -Encoding ascii "$Setup.sha256" "$Hash  $(Split-Path -Leaf $Setup)"
Write-Host ""
Write-Host "Installer: $Setup" -ForegroundColor Green
Write-Host "SHA-256:   $Hash"
Write-Host "Version:   $Version (commit $Commit)"
