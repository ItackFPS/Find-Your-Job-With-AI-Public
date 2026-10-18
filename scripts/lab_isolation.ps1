<#
.SYNOPSIS
  Plan, apply or audit the NTFS permissions that keep an isolated lab agent account out of production
  (ADR 0011, "Promotion authority"; docs/development.md, "Isolated lab agent").

.DESCRIPTION
  Run as the HUMAN owner of the checkout (not as the agent account). It never creates accounts, never
  changes passwords, never touches anything outside -Repo and -DenyRoot, and changes no permission unless
  -Mode Apply or Remove. Every change is an ACE for -Account alone; no other principal's access changes.

    Plan   (default) print what Apply would change.
    Apply  add the ACEs below (idempotent: present ones are skipped; on a failure it stops, says which
           step failed and exits 2 — re-run after fixing, or Remove).
    Audit  verify them, including what the ACEs alone cannot guarantee; exit 1 on any problem. It also
           lists what runs as you or another account (PATH, scheduled tasks of every principal but the
           agent's, service binaries, Startup, Run and RunOnce keys, git hooks and helpers, Claude Code
           and Codex hooks and MCP servers) in data\isolation-exec-paths.txt (its only write) and prints
           the isolation-check command that probes those places from the agent's account.
    Remove roll back: delete every explicit ACE for -Account on the paths Apply touches.

  -DenyRoot D:\ (elevated) adds the same deny on another root: on a drive where Authenticated Users may
  write, the agent could otherwise replace programs you run (a PATH entry, a scheduled task's script)
  and run code as you. The explicit allow on data\lab still wins there (it is nearer). Several roots:
  separate them with ';' (`-DenyRoot "D:\;C:\Games\SomeLauncher"`): `powershell -File` passes
  `a,b` as ONE string and refuses a repeated -DenyRoot.

  ACEs, for -Account only:
    -Repo         Deny  (OI)(CI) WD,AD,WEA,WA,D,DC,WDAC,WO   mask 0x000D0156: no write, delete, ACL or owner change
    -Repo\data\lab Allow (OI)(CI) Modify                      its only writable area (lab.db, strategies)
    secret files  Deny  ReadData (content; the ACL stays readable)  .env* (not .env.example), config\*.local.*

  Not icacls: icacls adds SYNCHRONIZE to every deny ACE, and denying SYNCHRONIZE also blocks opening
  files for READING, so an icacls deny would stop the agent from reading the code it must run.

  Audit also checks what a correct ACL on the root cannot: an object the account OWNS keeps an implicit
  WRITE_DAC whatever the deny says; an object with inheritance disabled never receives the deny; an
  explicit Allow on a child is evaluated before the inherited deny. The only real proof is still
  `python -m src.lab --lab-db data/lab/lab.db isolation-check --exec-path ...` run AS the account.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\lab_isolation.ps1 -Repo . -Mode Audit
#>
[CmdletBinding(PositionalBinding = $false)]     # a stray value must not bind to -Account or -Mode
param(
    [Parameter(Mandatory = $true)][string]$Repo,
    [string]$Account = 'fyj-lab',
    [ValidateSet('Plan', 'Apply', 'Audit', 'Remove')][string]$Mode = 'Plan',
    [string[]]$DenyRoot = @(),
    # tests only: let the account running the script stand in for the agent on a scratch tree
    [switch]$UnsafeCurrentAccountForTests
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$R = [System.Security.AccessControl.FileSystemRights]
$DenyMask = $R::WriteData -bor $R::AppendData -bor $R::WriteExtendedAttributes -bor $R::WriteAttributes -bor `
            $R::Delete -bor $R::DeleteSubdirectoriesAndFiles -bor $R::ChangePermissions -bor $R::TakeOwnership
$WriteLike = $DenyMask
$Inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$NoInherit = [System.Security.AccessControl.InheritanceFlags]::None
$NoProp = [System.Security.AccessControl.PropagationFlags]::None

$Repo = (Resolve-Path -LiteralPath $Repo).Path.TrimEnd('\')
$LabDir = Join-Path $Repo 'data\lab'

function Resolve-Sid([string]$name) {
    if ($name -match '^S-1-') { return New-Object System.Security.Principal.SecurityIdentifier($name) }
    try { return (New-Object System.Security.Principal.NTAccount($name)).Translate([System.Security.Principal.SecurityIdentifier]) }
    catch { throw "account '$name' does not exist: create it first (docs/development.md, Isolated lab agent)" }
}
$Sid = Resolve-Sid $Account
# Every ACE here denies -Account. Denying a principal YOU are, or belong to, would lock you (and, for Users,
# Authenticated Users or Everyone, every account) out of the repository or, with -DenyRoot, a whole drive.
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$broad = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545', 'S-1-5-4', 'S-1-2-0', 'S-1-2-1', 'S-1-5-113', 'S-1-5-114',
           'S-1-5-32-544', 'S-1-5-18', 'S-1-5-19', 'S-1-5-20', 'S-1-3-0', 'S-1-3-4')
$mine = @($me.Groups | ForEach-Object { $_.Value })
if ($broad -contains $Sid.Value -or $mine -contains $Sid.Value) {
    throw "-Account $Account ($($Sid.Value)) is a group you belong to or a built-in principal: refusing (it would deny you too)"
}
if ($Sid.Value -eq $me.User.Value -and -not $UnsafeCurrentAccountForTests) {
    throw "-Account is the account running this script: refusing (run it as the human owner, for the agent's account)"
}

function Get-SecretFiles {
    $files = @(Get-ChildItem -LiteralPath $Repo -Force -File -Filter '.env*' | Where-Object { $_.Name -ne '.env.example' })
    $cfg = Join-Path $Repo 'config'
    if (Test-Path -LiteralPath $cfg) { $files += @(Get-ChildItem -LiteralPath $cfg -Force -File -Filter '*.local.*') }
    return $files
}

function Add-Rule([string]$path, $rights, $inherit, [string]$type) {
    # the DACL only: Set-Acl would also rewrite the owner, which needs WRITE_OWNER
    $item = Get-Item -LiteralPath $path -Force
    $acl = $item.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($Sid, $rights, $inherit, $NoProp, $type)))
    $item.SetAccessControl($acl)
}

function Test-Rule([string]$path, $rights, $inherit, [string]$type) {
    foreach ($ace in (Get-Acl -LiteralPath $path).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($ace.IdentityReference.Value -eq $Sid.Value -and -not $ace.IsInherited -and "$($ace.AccessControlType)" -eq $type -and
            (([int]$ace.FileSystemRights -band [int]$rights) -eq [int]$rights) -and $ace.InheritanceFlags -eq $inherit) {
            return $true
        }
    }
    return $false
}

# the repository-wide deny goes last: everything before it is set while nothing is denied yet
$steps = @(
    @{ What = "Allow $Account Modify (inherited) on $LabDir"; Path = $LabDir; Rights = $R::Modify; Inherit = $Inherit; Type = 'Allow' }
)
foreach ($f in Get-SecretFiles) {
    $steps += @{ What = "Deny $Account ReadData on secret $($f.FullName)"; Path = $f.FullName; Rights = $R::ReadData; Inherit = $NoInherit; Type = 'Deny' }
}
# ';' separates roots; so does ',' before a drive letter (what `-DenyRoot D:\,E:\` arrives as under -File)
$DenyRoot = @($DenyRoot | ForEach-Object { $_ -split ';|,(?=\s*[A-Za-z]:\\)' } | ForEach-Object { $_.Trim() } |
              Where-Object { $_ } | ForEach-Object { (Resolve-Path -LiteralPath $_).Path } | Select-Object -Unique)
foreach ($root in $DenyRoot) {
    # the account's profile and temp live under the system drive: denying it there stops it from logging on
    if ($root.TrimEnd('\') -eq $env:SystemDrive -or $env:SystemRoot.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "-DenyRoot $root contains the system drive's Windows folder or is its root: refusing (deny the writable folders on it instead)"
    }
}
foreach ($root in @($Repo) + $DenyRoot) {
    $steps += @{ What = "Deny $Account write/delete/ACL/owner changes (mask 0x{0:X8}, inherited) on $root" -f [int]$DenyMask
                 Path = $root; Rights = $DenyMask; Inherit = $Inherit; Type = 'Deny' }
}

if ($Mode -eq 'Remove') {
    $paths = @($steps | ForEach-Object { $_.Path } | Select-Object -Unique)
    [array]::Reverse($paths)                        # the reverse of Apply: the wide denies come off first
    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $item = Get-Item -LiteralPath $path -Force
        $acl = $item.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
        $acl.PurgeAccessRules($Sid)                 # explicit ACEs for this SID only; inherited ones go with the parent
        $item.SetAccessControl($acl)
        "removed: every explicit ACE for $Account on $path"
    }
    exit 0
}

if ($Mode -ne 'Audit') {
    if ($Mode -eq 'Apply' -and -not (Test-Path -LiteralPath $LabDir)) {       # before any deny is in place
        New-Item -ItemType Directory -Force -Path $LabDir | Out-Null
        "done: created $LabDir"
    }
    foreach ($s in $steps) {
        if (Test-Path -LiteralPath $s.Path) { if (Test-Rule $s.Path $s.Rights $s.Inherit $s.Type) { "already: $($s.What)"; continue } }
        if ($Mode -eq 'Plan') { "would: $($s.What)"; continue }

        try { Add-Rule $s.Path $s.Rights $s.Inherit $s.Type }
        catch {
            "FAILED: $($s.What): $($_.Exception.Message)"
            'stopped: the steps above are applied, nothing after. Fix (e.g. run elevated for -DenyRoot) and re-run Apply, or run Remove.'
            exit 2
        }
        "done: $($s.What)"
    }
    if ($Mode -eq 'Plan') { 'nothing changed (Plan). Re-run with -Mode Apply, then -Mode Audit.' }
    exit 0
}

# ---- Audit ------------------------------------------------------------------------------------------------
$problems = New-Object System.Collections.Generic.List[string]
foreach ($s in $steps) {
    if (-not (Test-Path -LiteralPath $s.Path) -or -not (Test-Rule $s.Path $s.Rights $s.Inherit $s.Type)) { $problems.Add("missing: $($s.What)") }
}

# the SIDs whose Allow ACEs the account benefits from: itself, its local groups and the well-known ones
$member = @{ $Sid.Value = $true; 'S-1-1-0' = $true; 'S-1-5-11' = $true; 'S-1-5-32-545' = $true; 'S-1-5-4' = $true;
             'S-1-2-0' = $true; 'S-1-2-1' = $true; 'S-1-5-113' = $true; 'S-1-5-114' = $false }
$isAccount = $Account -notmatch '^S-1-'
if ($isAccount -and (Get-Command Get-LocalGroup -ErrorAction SilentlyContinue)) {
    foreach ($g in Get-LocalGroup) {
        $in = @(Get-LocalGroupMember -Group $g -ErrorAction SilentlyContinue | Where-Object { $_.SID.Value -eq $Sid.Value })
        if ($in.Count) {
            $member[$g.SID.Value] = $true
            if ($g.SID.Value -eq 'S-1-5-32-544') { $problems.Add("$Account is an Administrator: it can rewrite any ACL") }
        }
    }
}

$items = @(Get-Item -LiteralPath $Repo -Force) + @(Get-ChildItem -LiteralPath $Repo -Recurse -Force -ErrorAction SilentlyContinue)
$labPrefix = $LabDir + '\'
foreach ($item in $items) {
    $p = $item.FullName
    if ($p -eq $LabDir -or $p.StartsWith($labPrefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        # whoever can write (or create) a link's target outside the repository controls what is read through it
        $target = @($item.Target) | Select-Object -First 1
        $full = if ($target) { [IO.Path]::GetFullPath([IO.Path]::Combine((Split-Path -Parent $p), $target)) } else { '' }
        if (-not $full -or -not ($full -eq $Repo -or $full.StartsWith($Repo + '\', [StringComparison]::OrdinalIgnoreCase))) {
            $state = if (-not $full) { 'unknown target' } elseif (Test-Path -LiteralPath $full) { "-> $full" } else { "-> $full (missing: creatable by whoever may write its parent)" }
            $problems.Add("link leads outside the repository ${state}: $p")
        }
    }
    try { $acl = Get-Acl -LiteralPath $p } catch { $problems.Add("cannot read ACL: $p"); continue }
    $owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($member[$owner]) { $problems.Add("owned by $Account or one of its groups (implicit WRITE_DAC): $p") }
    if ($p -eq $Repo) { continue }
    # inheritance disabled: the deny does not reach it, so only its own ACEs decide (pytest creates
    # .pytest_cache owner-only that way: harmless, since none of them is the account's)
    $protected = $acl.AreAccessRulesProtected
    foreach ($ace in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ("$($ace.AccessControlType)" -ne 'Allow' -or -not ([int]$ace.FileSystemRights -band [int]$WriteLike)) { continue }
        if (-not $member[$ace.IdentityReference.Value]) { continue }
        if ($protected) { $problems.Add("inheritance disabled and $($ace.IdentityReference.Value) may write it, the deny does not reach it: $p") }
        elseif (-not $ace.IsInherited) { $problems.Add("explicit write Allow for $($ace.IdentityReference.Value) precedes the inherited deny: $p") }
    }
}

"audited $($items.Count) objects under $Repo for $Account ($($Sid.Value))"
# not a problem for $Account, but every other principal that may write the repository can change production
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trusted = @('S-1-5-18', 'S-1-5-32-544', 'S-1-3-0', $me)
foreach ($ace in (Get-Acl -LiteralPath $Repo).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
    $who = $ace.IdentityReference.Value
    if ("$($ace.AccessControlType)" -eq 'Allow' -and ([int]$ace.FileSystemRights -band [int]$WriteLike) -and $trusted -notcontains $who) {
        try { $name = $ace.IdentityReference.Translate([System.Security.Principal.NTAccount]).Value } catch { $name = '(unresolved)' }
        "NOTE: $name $who may also write $Repo ($($ace.FileSystemRights)$(if ($ace.IsInherited) { ', inherited' })): it can change production"
    }
}

# where YOU execute from: code planted in any of these that the agent can write runs as you. Only the agent
# account can tell what it may write, so they are probed by isolation-check, run as the agent.
function Get-PathCandidates([string]$text) {
    # %VAR% first (a task's "%ProgramFiles%\x.exe" has no drive letter until expanded); then quoted paths,
    # unquoted ones up to a program extension (Run keys often hold "C:\Program Files\x\y.exe -flag"
    # unquoted), and any other unquoted token
    $text = [Environment]::ExpandEnvironmentVariables($text)
    $ext = 'exe|com|bat|cmd|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|msc|py|pyw|dll|cpl|scr|jar|lnk'
    $pattern = '"([A-Za-z]:\\[^"]+)"|(?<![\w"])([A-Za-z]:\\[^"\r\n]*?\.(?:' + $ext + '))(?=$|[\s,"])|(?<!["\w])([A-Za-z]:\\[^\s"]+)'
    foreach ($m in [regex]::Matches($text, $pattern, 'IgnoreCase')) {
        foreach ($g in 1..3) { if ($m.Groups[$g].Success) { $m.Groups[$g].Value.TrimEnd('\'); break } }
    }
    # the extension match ends at the first program name; the token match catches what follows it
    foreach ($m in [regex]::Matches($text, '(?<![\w"])([A-Za-z]:\\[^\s"]+)')) { $m.Groups[1].Value.TrimEnd('\', ',') }
}
$exec = New-Object System.Collections.Generic.List[string]
foreach ($scope in 'Machine', 'User') {
    foreach ($d in ([Environment]::GetEnvironmentVariable('Path', $scope) -split ';')) {
        if ($d) { $exec.Add([Environment]::ExpandEnvironmentVariables($d).TrimEnd('\')) }
    }
}
# scheduled tasks of every principal but the agent's own: one running as you runs planted code as you, one
# running as SYSTEM (or any other account) hands the agent that account, which beats every ACL here
if (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue) {
    foreach ($t in Get-ScheduledTask -ErrorAction SilentlyContinue) {
        $uid = "$($t.Principal.UserId)"
        if ($uid -and ($uid -eq $Account -or $uid.EndsWith('\' + $Account))) { continue }
        foreach ($a in @($t.Actions)) {
            if (-not ($a.PSObject.Properties.Name -contains 'Execute')) { continue }
            foreach ($c in Get-PathCandidates("`"$($a.Execute)`" $($a.Arguments)")) { $exec.Add($c) }
        }
    }
}
# service binaries: services run as SYSTEM or service accounts, so the same holds
foreach ($svc in @(Get-CimInstance Win32_Service -ErrorAction SilentlyContinue)) {
    if ($svc.PathName) { foreach ($c in Get-PathCandidates("$($svc.PathName)")) { $exec.Add($c) } }
}
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('CommonStartup')) {
    if (-not $dir) { continue }
    $exec.Add($dir.TrimEnd('\'))                        # whoever may add a file there starts it at your logon
    foreach ($f in @(Get-ChildItem -LiteralPath $dir -File -Force -ErrorAction SilentlyContinue)) {
        if ($f.Name -eq 'desktop.ini') { continue }
        $exec.Add($f.FullName)                           # every file there is opened at logon, not only .lnk
        if ($f.Extension -ne '.lnk') { continue }
        $sc = $shell.CreateShortcut($f.FullName)
        foreach ($c in Get-PathCandidates("`"$($sc.TargetPath)`" $($sc.Arguments)")) { $exec.Add($c) }
    }
}
foreach ($key in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
                 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
                 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
                 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce') {
    $props = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    if ($props) {
        foreach ($pr in $props.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' }) {
            foreach ($c in Get-PathCandidates("$($pr.Value)")) { $exec.Add($c) }
        }
    }
}
# programs your tools start from their configuration: git (core.hooksPath, core.fsmonitor, editors,
# credential helpers: a hooks folder that does not exist yet is created by whoever reaches it first), and
# Claude Code / Codex hooks and MCP servers
if (Get-Command git -ErrorAction SilentlyContinue) {
    $gitConfig = @()
    try { $gitConfig = @(& git -C $Repo config --get-regexp '^(core\.(hookspath|fsmonitor|editor|sshcommand|pager)|credential\..*helper|sequence\.editor|diff\..*|merge\..*|filter\..*)$' 2>$null) }
    catch { $problems.Add("cannot read git configuration: $($_.Exception.Message)") }
    foreach ($line in $gitConfig) {
        $key, $value = "$line" -split ' ', 2
        if ($key -eq 'core.hookspath' -and $value) {           # a folder, spaces and all; relative = in the checkout
            $hooks = [Environment]::ExpandEnvironmentVariables($value.Trim().Trim('"'))
            if (-not [IO.Path]::IsPathRooted($hooks)) { $hooks = Join-Path $Repo $hooks }
            $exec.Add([IO.Path]::GetFullPath($hooks).TrimEnd('\'))
            continue
        }
        foreach ($c in Get-PathCandidates("$value")) { $exec.Add($c) }
    }
}
foreach ($cfg in (Join-Path $HOME '.claude\settings.json'), (Join-Path $HOME '.claude\settings.local.json'),
                 (Join-Path $HOME '.claude.json'), (Join-Path $HOME '.codex\config.toml')) {
    if (Test-Path -LiteralPath $cfg -PathType Leaf) {
        # only what they run ("command"/"args" of hooks and MCP servers), not every path they mention
        $text = (Get-Content -LiteralPath $cfg -Raw -ErrorAction SilentlyContinue) -replace '\\\\', '\'
        foreach ($m in [regex]::Matches("$text", '"?(?:command|args)"?\s*[:=]\s*(\[[^\]]*\]|"[^"]*")')) {
            foreach ($c in Get-PathCandidates($m.Groups[1].Value)) { $exec.Add($c) }
        }
    }
}
$exec = @($exec | Where-Object { $_ -and $_ -match '^[A-Za-z]:\\' } | Sort-Object -Unique)
"places executed as you or another account ($($exec.Count)): PATH directories, scheduled tasks, services, Startup items, Run keys, git and agent-tool config"
# a few hundred paths overflow a command line (cmd.exe: 8191 characters): the check reads them from a file
# in data\, which the agent may read but not write once Apply is done (the check fails if it can)
$listFile = Join-Path $Repo 'data\isolation-exec-paths.txt'
$args_ = ($exec | ForEach-Object { "--exec-path `"$_`"" }) -join ' '
try {
    [IO.File]::WriteAllLines($listFile, [string[]]$exec)
    "  listed in $listFile"
    $args_ = '--exec-paths-file data/isolation-exec-paths.txt'
} catch { "  (could not write $listFile, so the paths are on the command line: $($_.Exception.Message))" }
if ($problems.Count) { $problems | ForEach-Object { "PROBLEM: $_" } }
'Verify AS the agent account (runas), from the repository, with the API running (API_SAME_USER_ONLY=1) and no relay:'
"  python -m src.lab --lab-db data/lab/lab.db isolation-check $args_"
if ($problems.Count) { exit 1 }
'OK: ACLs as specified (isolation is proven only by the command above, run as the account).'
exit 0
