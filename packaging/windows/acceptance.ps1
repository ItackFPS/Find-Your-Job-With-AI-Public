<#
.SYNOPSIS
    End-to-end acceptance test of the Windows package: install, launch, stop, restart,
    upgrade, uninstall -- without a browser and without starting any job scan.

.DESCRIPTION
    With -Installer, runs the real setup program silently for the current user into a
    fresh test folder (the default install location is not touched), then checks:
      1. files, version, Start Menu and desktop shortcuts (target, working folder, icon);
      2. first launch through the installed launcher: backend healthy on 127.0.0.1 only,
         database created, NO pipeline run or scan started;
      3. a second launch reuses the running app (same backend process, no duplicate);
      4. a settings change through the API (profile.local.yaml) and a marker in a
         default config file;
      5. "stop" stops the backend cleanly; a restart keeps the data and starts no scan;
      6. an upgrade (the same installer again) while the app runs: it is stopped first,
         and the database, profile.local.yaml and the edited default config survive;
      7. silent uninstall: application files and shortcuts gone, user data kept.

    With -Stage (a build.ps1 -StageOnly folder), copies the stage into the test folder
    instead and runs steps 2-5: it checks the runtime and the launcher, not the setup
    program.

    Uses port 8765 by default so a development backend on 8000 is not disturbed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\windows\acceptance.ps1 -Installer dist\windows\FindYourJobWithAI-Setup-1.0.0.exe
#>
[CmdletBinding()]
param(
    [string]$Installer = "",
    [string]$Stage = "",
    [string]$TestDir = (Join-Path $env:TEMP "fyj-acceptance"),
    [int]$Port = 8765,
    [switch]$KeepTestDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:Failures = 0
function Pass([string]$Text) { Write-Host "  [ok] $Text" -ForegroundColor Green }
function Check([bool]$Condition, [string]$Text) {
    if ($Condition) { Pass $Text } else { Write-Host "  [XX] $Text" -ForegroundColor Red; $script:Failures++ }
}
function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

if (-not $Installer -and -not $Stage) { throw "pass -Installer <setup.exe> or -Stage <stage folder>" }
if ($Installer) { $Installer = (Resolve-Path $Installer).Path }
if ($Stage) { $Stage = (Resolve-Path $Stage).Path }
$Base = "http://127.0.0.1:$Port"
$env:FYJ_PORT = "$Port"   # read by the launcher (shell environment beats .env)

function Get-Json([string]$Path) {
    try { return Invoke-RestMethod -Uri "$Base$Path" -TimeoutSec 5 } catch { return $null }
}
function Wait-Health([int]$Seconds = 240) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $h = Get-Json "/api/health"
        if ($h -and $h.app -eq "find-your-job-with-ai") { return $h }
        Start-Sleep -Milliseconds 500
    }
    return $null
}
function Wait-Down([int]$Seconds = 90) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Json "/api/health")) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}
function Start-App([string]$Root) {
    # What the shortcut does (the notification-area app), minus the browser window.
    Start-Process -FilePath (Join-Path $Root "runtime\pythonw.exe") `
        -ArgumentList "-m", "src.launcher", "tray", "--window", "none" `
        -WorkingDirectory $Root | Out-Null
}
function Invoke-Launcher([string]$Root, [string[]]$Arguments) {
    $p = Start-Process -FilePath (Join-Path $Root "FindYourJob.cmd") -ArgumentList $Arguments `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru -Wait
    return $p.ExitCode
}
function Assert-NoScan([string]$When) {
    # raw body: PowerShell turns an empty JSON array into $null, and @($null) has one element
    $raw = (Invoke-WebRequest -Uri "$Base/api/pipeline/runs" -UseBasicParsing -TimeoutSec 10).Content.Trim()
    Check ($raw -eq "[]") "$When : no pipeline run was started (runs: $($raw.Substring(0, [Math]::Min(80, $raw.Length))))"
    foreach ($source in "linkedin", "hellowork") {
        $s = Get-Json "/api/$source/status"
        Check ($null -ne $s -and -not $s.is_scanning -and $null -eq $s.last_scan) "$When : no $source scan started (state $($s.state))"
    }
}
function Get-Shortcut([string]$Path) {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    return [pscustomobject]@{ Target = $lnk.TargetPath; Dir = $lnk.WorkingDirectory; Icon = $lnk.IconLocation; Args = $lnk.Arguments }
}
function Put-Profile([string]$Marker) {
    $p = Get-Json "/api/profile"
    $p.target_skills = @($p.target_skills) + @($Marker)
    $body = $p | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Method Put -Uri "$Base/api/profile" -Body $body -ContentType "application/json" -TimeoutSec 15 | Out-Null
}

$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F0D6C2A-5B7E-4C1B-9E55-2D3C4F9A7B10}_is1"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Find Your Job With AI"
$DesktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Find Your Job With AI.lnk"

if (Get-Json "/api/health") { throw "something already answers on $Base; stop it or pass -Port" }
if ($Installer -and (Test-Path $UninstallKey)) {
    throw "Find Your Job With AI is already installed for this account; uninstall it first (this test installs its own copy)"
}
if (Test-Path $TestDir) { Remove-Item -Recurse -Force $TestDir }
$App = Join-Path $TestDir "app"
New-Item -ItemType Directory -Force $TestDir | Out-Null
$Marker = "acceptance-marker-$(Get-Random)"

try {
    # -- 1. install -----------------------------------------------------------------------------
    if ($Installer) {
        Step "Installing silently into $App"
        $log = Join-Path $TestDir "install.log"
        $p = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", "/DIR=`"$App`"", "/TASKS=desktopicon", "/LOG=`"$log`"" -PassThru -Wait
        Check ($p.ExitCode -eq 0) "setup exited with 0 (got $($p.ExitCode))"
        Check (Test-Path $UninstallKey) "registered for this account (Apps & features)"
        $version = (Get-ItemProperty $UninstallKey).DisplayVersion
        $info = Get-Content -Raw (Join-Path $App "build-info.json") | ConvertFrom-Json
        Check ($version -eq $info.version) "installed version $version matches build-info.json"
        foreach ($lnk in (Join-Path $StartMenu "Find Your Job With AI.lnk"), $DesktopLnk) {
            Check (Test-Path $lnk) "shortcut exists: $lnk"
            if (Test-Path $lnk) {
                $s = Get-Shortcut $lnk
                Check ($s.Target -eq (Join-Path $App "runtime\pythonw.exe")) "  targets the bundled pythonw ($($s.Target))"
                Check ($s.Args -eq "-m src.launcher tray") "  starts the notification-area app ($($s.Args))"
                Check ($s.Dir -eq $App) "  starts in the install folder"
                Check ($s.Icon -like "$App\FindYourJob.ico*") "  uses the application icon"
            }
        }
        $stop = Join-Path $StartMenu "Stop Find Your Job With AI.lnk"
        Check ((Test-Path $stop) -and (Get-Shortcut $stop).Args -eq "stop") "Start Menu has a Stop shortcut"
    } else {
        Step "Copying the stage into $App (simulated install)"
        Copy-Item -Recurse $Stage $App
        New-Item -ItemType Directory -Force (Join-Path $App "data"), (Join-Path $App "logs") | Out-Null
    }
    foreach ($forbidden in ".env", "data\jobs.db", "config\profile.local.yaml") {
        Check (-not (Test-Path (Join-Path $App $forbidden))) "fresh install has no $forbidden"
    }
    Check (Test-Path (Join-Path $App "runtime\python.exe")) "private Python runtime installed"
    Check (-not (Test-Path (Join-Path $App ".venv"))) "no development virtual environment"

    # -- 2. first launch --------------------------------------------------------------------------
    Step "First launch"
    $t0 = Get-Date
    Start-App $App
    $h = Wait-Health
    Check ($null -ne $h) "backend healthy at $Base"
    if (-not $h) { throw "backend did not start; see $App\logs\launcher" }
    Write-Host ("     ready after {0:N0} s, version {1}, pid {2}" -f ((Get-Date) - $t0).TotalSeconds, $h.version, $h.pid)
    $listen = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    Check ($listen.Count -gt 0 -and -not ($listen | Where-Object { $_.LocalAddress -notin "127.0.0.1", "::1" })) "listens on loopback only ($(($listen.LocalAddress | Sort-Object -Unique) -join ', '))"
    Check (Test-Path (Join-Path $App "data\jobs.db")) "database created in the install folder"
    $exe = (Get-Process -Id $h.pid).Path
    Check ($exe -like "$App\runtime\*") "backend runs the bundled Python ($exe)"
    Assert-NoScan "first launch"
    $dash = Invoke-WebRequest -Uri "$Base/" -UseBasicParsing -TimeoutSec 10
    Check ($dash.StatusCode -eq 200 -and $dash.Content -match "<div id=`"root`"") "dashboard is served"

    # -- 3. second launch -------------------------------------------------------------------------
    Step "Second launch while running"
    $code = Invoke-Launcher $App @("--window", "none")
    Check ($code -eq 0) "second launch exits 0 (reuses the running app)"
    $h2 = Get-Json "/api/health"
    Check ($h2 -and $h2.pid -eq $h.pid) "same backend process (pid $($h.pid)), no duplicate"
    $backends = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -like "$App\*" -and $_.CommandLine -match "uvicorn" })
    Check ($backends.Count -eq 1) "exactly one backend process from this install ($($backends.Count))"

    # -- 4. a setting and a config edit -----------------------------------------------------------
    Step "Changing a setting"
    Put-Profile $Marker
    Check (Select-String -Quiet -SimpleMatch $Marker (Join-Path $App "config\profile.local.yaml")) "profile saved to config\profile.local.yaml"
    Add-Content -Encoding utf8 (Join-Path $App "config\retention.yaml") "# $Marker"

    # -- 5. stop and restart ----------------------------------------------------------------------
    Step "Stop"
    $code = Invoke-Launcher $App @("stop")
    Check ($code -eq 0) "stop exits 0"
    Check (Wait-Down) "backend stopped"
    Check (-not (Get-Process -Id $h.pid -ErrorAction SilentlyContinue)) "backend process exited (no orphan)"
    Step "Restart"
    Start-App $App
    $h3 = Wait-Health
    Check ($null -ne $h3) "backend healthy again"
    Check ((Get-Json "/api/profile").target_skills -contains $Marker) "setting kept across the restart"
    Assert-NoScan "restart"

    if ($Installer) {
        # -- 6. upgrade while running -------------------------------------------------------------
        Step "Upgrade (same installer) while the app runs"
        $p = Start-Process -FilePath $Installer -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", "/DIR=`"$App`"", "/TASKS=desktopicon", "/LOG=`"$(Join-Path $TestDir 'upgrade.log')`"" -PassThru -Wait
        Check ($p.ExitCode -eq 0) "upgrade exited with 0 (got $($p.ExitCode))"
        Check (-not (Get-Process -Id $h3.pid -ErrorAction SilentlyContinue)) "the running app was stopped first"
        Check (Test-Path (Join-Path $App "data\jobs.db")) "database kept"
        Check (Select-String -Quiet -SimpleMatch $Marker (Join-Path $App "config\profile.local.yaml")) "profile.local.yaml kept"
        Check (Select-String -Quiet -SimpleMatch $Marker (Join-Path $App "config\retention.yaml")) "edited default config kept"
        Start-App $App
        $h4 = Wait-Health
        Check ($null -ne $h4) "upgraded app starts"
        Check ((Get-Json "/api/profile").target_skills -contains $Marker) "setting kept across the upgrade"
        Assert-NoScan "after upgrade"

        # -- 7. uninstall ------------------------------------------------------------------------
        Step "Uninstall (silent)"
        $uninstaller = (Get-ItemProperty $UninstallKey).UninstallString.Trim('"')
        $p = Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -PassThru -Wait
        Check ($p.ExitCode -eq 0) "uninstaller exited with 0"
        # the uninstaller copies itself to %TEMP% and returns early; wait for it to finish
        $deadline = (Get-Date).AddSeconds(120)
        while ((Test-Path $UninstallKey) -and (Get-Date) -lt $deadline) { Start-Sleep 1 }
        Start-Sleep 3
        Check (Wait-Down 5) "app stopped by the uninstaller"
        Check (-not (Test-Path $UninstallKey)) "unregistered"
        foreach ($gone in "src", "runtime", "frontend", "FindYourJob.cmd") {
            Check (-not (Test-Path (Join-Path $App $gone))) "application file removed: $gone"
        }
        Check (-not (Test-Path $DesktopLnk)) "desktop shortcut removed"
        Check (-not (Test-Path $StartMenu)) "Start Menu folder removed"
        foreach ($kept in "data\jobs.db", "config\profile.local.yaml", "config\retention.yaml", "logs") {
            Check (Test-Path (Join-Path $App $kept)) "user data kept: $kept"
        }
    }
} finally {
    if (Get-Json "/api/health") {
        if (Test-Path (Join-Path $App "FindYourJob.cmd")) { Invoke-Launcher $App @("stop") | Out-Null }
        Wait-Down | Out-Null
    }
    Remove-Item Env:FYJ_PORT -ErrorAction SilentlyContinue
    if (-not $KeepTestDir -and (Test-Path $TestDir)) { Remove-Item -Recurse -Force $TestDir -ErrorAction SilentlyContinue }
}

Write-Host ""
if ($script:Failures -eq 0) { Write-Host "ACCEPTANCE PASSED" -ForegroundColor Green; exit 0 }
Write-Host "ACCEPTANCE FAILED: $script:Failures check(s)" -ForegroundColor Red
exit 1
