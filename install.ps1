# Gyrus Installer for Windows
# One command, one API key, done.
#
# Usage:
#   .\install.ps1                                    # first machine
#   .\install.ps1 -Clone github.com/you/gyrus-repo   # second machine (clone existing data)

param(
    [string]$Clone = $env:GYRUS_CLONE
)

$ErrorActionPreference = "Stop"

$GyrusDir = "$env:USERPROFILE\.gyrus"
$IngestScript = "$GyrusDir\ingest.py"
$StorageScript = "$GyrusDir\storage.py"
$StorageNotionScript = "$GyrusDir\storage_notion.py"
$EnvFile = "$GyrusDir\.env"
$LogFile = "$GyrusDir\ingest.log"
$UvPython = "3.12"

function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor White }
function Write-Ok($msg) { Write-Host "  " -NoNewline; Write-Host "OK" -ForegroundColor Green -NoNewline; Write-Host " $msg" }
function Write-Warn($msg) { Write-Host "  " -NoNewline; Write-Host "!" -ForegroundColor Yellow -NoNewline; Write-Host " $msg" }
function Write-Fail($msg) { Write-Host "  " -NoNewline; Write-Host "X" -ForegroundColor Red -NoNewline; Write-Host " $msg" }
function Write-Dim($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "Gyrus" -ForegroundColor White -NoNewline
Write-Host " - your AI tools' shared brain"
Write-Host "======================================="

# --- Step 1: uv (Python toolchain) ---
Write-Step "Step 1: Setting up Python runtime..."

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($Uv) {
    Write-Ok "uv found at $($Uv.Source)"
    $UvCmd = $Uv.Source
} else {
    Write-Dim "Installing uv (Python toolchain by Astral - manages Python for you)..."
    try {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" 2>$null
        # Refresh PATH
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
        $Uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($Uv) {
            $UvCmd = $Uv.Source
            Write-Ok "uv installed at $UvCmd"
        } else {
            Write-Fail "Could not install uv. Install manually: https://docs.astral.sh/uv/"
            exit 1
        }
    } catch {
        Write-Fail "Could not install uv. Install manually: https://docs.astral.sh/uv/"
        exit 1
    }
}

# Ensure Python is available via uv
Write-Dim "Ensuring Python $UvPython is available..."
& $UvCmd python install $UvPython 2>$null
Write-Ok "Python $UvPython ready (managed by uv - your system Python is untouched)"

# --- Step 2: Storage location ---
Write-Step "Step 2: Where should Gyrus store your knowledge base?"

$DefaultLoc = "$env:USERPROFILE\gyrus-local"
Write-Host ""
Write-Host "  [1] Default: " -NoNewline
Write-Host $DefaultLoc -ForegroundColor White -NoNewline
Write-Host " (recommended)"
Write-Host "  [2] Custom path"
Write-Host ""
Write-Dim "Cross-machine sync happens via GitHub (set up in step 3)."
Write-Dim "Don't use OneDrive / Dropbox / Google Drive — they cause silent hangs."
Write-Host ""
$StorageChoice = Read-Host "  Choice [1]"
if ([string]::IsNullOrWhiteSpace($StorageChoice)) { $StorageChoice = "1" }

$CustomDir = ""
if ($StorageChoice -eq "2") {
    $CustomDir = Read-Host "  Custom path"
} else {
    $CustomDir = $DefaultLoc
}

if (-not [string]::IsNullOrWhiteSpace($CustomDir)) {
    $CustomDir = $CustomDir -replace "^~", $env:USERPROFILE

    # Guard against cloud-sync paths
    $CloudProvider = ""
    if ($CustomDir -match "OneDrive") { $CloudProvider = "OneDrive" }
    elseif ($CustomDir -match "\\Dropbox(\\|$)") { $CloudProvider = "Dropbox" }
    elseif ($CustomDir -match "Google Drive|GoogleDrive") { $CloudProvider = "Google Drive" }
    elseif ($CustomDir -match "\\Box(\\|$)|Box Sync") { $CloudProvider = "Box" }
    elseif ($CustomDir -match "iCloudDrive|iCloud Drive") { $CloudProvider = "iCloud Drive" }

    if ($CloudProvider -ne "") {
        Write-Warn "That path is inside $CloudProvider."
        Write-Dim "$CloudProvider can lock/evict files and hang reads."
        Write-Dim "Cross-machine sync is handled by GitHub (next step) — you don't need $CloudProvider for that."
        $CloudConfirm = Read-Host "  Use it anyway? [y/N]"
        if ($CloudConfirm -notmatch "^[Yy]") {
            Write-Warn "Falling back to $DefaultLoc"
            $CustomDir = $DefaultLoc
        }
    }

    $GyrusDir = $CustomDir
    $IngestScript = "$GyrusDir\ingest.py"
    $StorageScript = "$GyrusDir\storage.py"
    $StorageNotionScript = "$GyrusDir\storage_notion.py"
    $EnvFile = "$GyrusDir\.env"
    $LogFile = "$GyrusDir\ingest.log"

    # Create junction from ~/.gyrus if we're using a different location
    $DefaultDir = "$env:USERPROFILE\.gyrus"
    if ($GyrusDir -ne $DefaultDir) {
        if (Test-Path $DefaultDir) {
            $item = Get-Item $DefaultDir -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                Remove-Item $DefaultDir -Force
            }
        }
        if (-not (Test-Path $DefaultDir)) {
            New-Item -ItemType Directory -Path $GyrusDir -Force | Out-Null
            cmd /c mklink /J "$DefaultDir" "$GyrusDir" | Out-Null
            Write-Ok "Junction created: ~/.gyrus -> $GyrusDir"
        }
    }
}

# --- Step 3: Install scripts ---
Write-Step "Step 3: Installing..."

New-Item -ItemType Directory -Path $GyrusDir -Force | Out-Null

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (Test-Path (Join-Path $ScriptDir "ingest.py")) {
    Copy-Item (Join-Path $ScriptDir "ingest.py") $IngestScript -Force
    Copy-Item (Join-Path $ScriptDir "storage.py") $StorageScript -Force
    if (Test-Path (Join-Path $ScriptDir "storage_notion.py")) {
        Copy-Item (Join-Path $ScriptDir "storage_notion.py") $StorageNotionScript -Force
    }
    Write-Ok "Installed to $GyrusDir"
} else {
    # Download from GitHub
    $RepoUrl = "https://raw.githubusercontent.com/prismindanalytics/gyrus/main"
    Invoke-WebRequest -Uri "$RepoUrl/ingest.py" -OutFile $IngestScript -UseBasicParsing
    Invoke-WebRequest -Uri "$RepoUrl/storage.py" -OutFile $StorageScript -UseBasicParsing
    Invoke-WebRequest -Uri "$RepoUrl/storage_notion.py" -OutFile $StorageNotionScript -UseBasicParsing
    try {
        Invoke-WebRequest -Uri "$RepoUrl/eval_prompts.py" -OutFile "$GyrusDir\eval_prompts.py" -UseBasicParsing
    } catch { }
    Write-Ok "Downloaded to $GyrusDir"
}

# Install gyrus.cmd wrapper so users have a `gyrus` command
$GyrusBinDir = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Path $GyrusBinDir -Force | Out-Null
$GyrusBin = "$GyrusBinDir\gyrus.cmd"
@"
@echo off
REM Gyrus CLI wrapper — https://gyrus.sh
setlocal
if "%GYRUS_HOME%"=="" set "GYRUS_HOME=%USERPROFILE%\.gyrus"
if "%UV_BIN%"=="" set "UV_BIN=uv"

set "SUBCMD=%~1"
if "%SUBCMD%"==""         goto :run
if /I "%SUBCMD%"=="init"   (shift & set "FLAG=--init" & goto :flag)
if /I "%SUBCMD%"=="sync"   (shift & set "FLAG=--sync" & goto :flag)
if /I "%SUBCMD%"=="update" (shift & set "FLAG=--update" & goto :flag)
if /I "%SUBCMD%"=="compare" (shift & set "FLAG=--compare-models" & goto :flag)
if /I "%SUBCMD%"=="digest" (shift & set "FLAG=--digest" & goto :flag)
if /I "%SUBCMD%"=="status" (shift & set "FLAG=--review-status" & goto :flag)
if /I "%SUBCMD%"=="doctor" (shift & set "FLAG=--doctor" & goto :flag)
if /I "%SUBCMD%"=="log"    (shift & set "FLAG=--show-log" & goto :flag)
if /I "%SUBCMD%"=="help"   goto :help
if /I "%SUBCMD%"=="-h"     goto :help
if /I "%SUBCMD%"=="--help" goto :help
goto :run

:flag
cd /d "%GYRUS_HOME%" && "%UV_BIN%" run --python 3.12 ingest.py %FLAG% %*
goto :end

:run
cd /d "%GYRUS_HOME%" && "%UV_BIN%" run --python 3.12 ingest.py %*
goto :end

:help
echo Usage: gyrus [command] [options]
echo.
echo Commands:
echo   (none)    Run ingestion
echo   init      First-time setup (storage, API key, GitHub, schedule)
echo   sync      Manually pull + push GitHub remote
echo   status    Review and set project statuses
echo   doctor    Diagnose ingest health
echo   digest    Generate activity digest
echo   compare   Benchmark models
echo   update    Update Gyrus code
echo   log       Show recent run history
echo.
echo Docs: https://gyrus.sh

:end
endlocal
"@ | Set-Content $GyrusBin -Encoding ASCII
Write-Ok "Installed 'gyrus' command to $GyrusBin"

# Ensure ~\.local\bin is in PATH
$UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($UserPath -notlike "*$GyrusBinDir*") {
    [Environment]::SetEnvironmentVariable("PATH", "$UserPath;$GyrusBinDir", "User")
    Write-Ok "Added $GyrusBinDir to your user PATH (restart terminal to use 'gyrus')"
}
$env:PATH = "$env:PATH;$GyrusBinDir"

# --- Step 4: Choose your model ---
Write-Step "Step 4: Choose your model"

$ConfigFile = "$GyrusDir\config.json"

# Probe common local-LLM endpoints
$LocalUrl = ""
$LocalName = ""
$LocalModels = @()
foreach ($probe in @(
    @{url="http://localhost:11434/v1"; name="Ollama"},
    @{url="http://localhost:1234/v1";  name="LM Studio"},
    @{url="http://localhost:8000/v1";  name="vLLM"},
    @{url="http://localhost:8080/v1";  name="llama.cpp"}
)) {
    try {
        $resp = Invoke-RestMethod -Uri "$($probe.url)/models" `
                                  -Headers @{Authorization="Bearer local"} `
                                  -TimeoutSec 2 -ErrorAction Stop
        $LocalUrl  = $probe.url
        $LocalName = $probe.name
        $LocalModels = @($resp.data | ForEach-Object { $_.id })
        break
    } catch { continue }
}

Write-Host ""
Write-Host "  Gyrus uses an LLM to extract thoughts from sessions and merge them"
Write-Host "  into project pages. Pick one:"
Write-Host ""
Write-Host "  [1] Cloud (Anthropic / OpenAI / Google)"
Write-Dim "• Best quality out of the box"
Write-Dim "• Requires an API key"
Write-Dim "• Typical cost: ~`$5-15/month"
Write-Host ""
if ($LocalModels.Count -gt 0) {
    Write-Host "  [2] Local " -NoNewline
    Write-Host "($LocalName detected, $($LocalModels.Count) model(s) available)" -ForegroundColor Green
} else {
    Write-Host "  [2] Local (Ollama — we'll guide setup)"
}
Write-Dim "• `$0/month, your data never leaves the machine"
Write-Dim "• Recommended: qwen3.5:9b (`<=16GB) or qwen3.6:35b-a3b (`>=24GB)"
Write-Host ""
$ModelMode = Read-Host "  Choice [1]"
if ([string]::IsNullOrWhiteSpace($ModelMode)) { $ModelMode = "1" }

if ($ModelMode -eq "2") {
    # --- Local path ---
    if ($LocalModels.Count -eq 0) {
        Write-Host ""
        Write-Warn "No local LLM server detected."
        Write-Dim "Install Ollama from https://ollama.com/download"
        Write-Dim "Then: ollama pull qwen3.5:9b  (or gemma4:e4b for small machines)"
        Write-Dim "Then: ollama serve"
        Write-Host ""
        $SkipNow = Read-Host "  Save placeholder config and finish Ollama setup later? [Y/n]"
        if ($SkipNow -notmatch "^[Nn]") {
            @'
{
  "extract_model": "local:qwen3.5:9b",
  "merge_model": "local:qwen3.5:9b",
  "local_base_url": "http://localhost:11434/v1"
}
'@ | Set-Content $ConfigFile -Encoding ASCII
            Write-Ok "Saved placeholder config. After installing Ollama, run: gyrus doctor"
        } else {
            Write-Warn "Falling back to cloud model setup."
            $ModelMode = "1"
        }
    } else {
        Write-Host ""
        Write-Host "  Available models"
        $ShowCount = [Math]::Min($LocalModels.Count, 15)
        for ($i = 0; $i -lt $ShowCount; $i++) {
            Write-Host ("    [{0,2}] {1}" -f ($i+1), $LocalModels[$i])
        }
        if ($LocalModels.Count -gt 15) {
            Write-Host ("         +{0} more" -f ($LocalModels.Count - 15))
        }
        Write-Host ""

        # Pick smart defaults
        $DefaultExtract = $LocalModels[0]
        foreach ($pref in @("qwen3.5:9b","gemma4:e4b","gemma4:e2b","qwen3:9b")) {
            if ($LocalModels -contains $pref) { $DefaultExtract = $pref; break }
        }
        $DefaultMerge = $DefaultExtract
        foreach ($pref in @("qwen3.6:35b-a3b","gemma4:26b","qwen3:32b")) {
            if ($LocalModels -contains $pref) { $DefaultMerge = $pref; break }
        }
        $DefaultExtractIdx = [Array]::IndexOf($LocalModels, $DefaultExtract) + 1
        $DefaultMergeIdx = [Array]::IndexOf($LocalModels, $DefaultMerge) + 1

        # Helper: accept number, name, or empty for default
        function Resolve-Choice($input, $default, $list) {
            if ([string]::IsNullOrWhiteSpace($input)) { return $default }
            if ($input -match '^\d+$') {
                $n = [int]$input
                if ($n -ge 1 -and $n -le $list.Count) { return $list[$n-1] }
            }
            return $input
        }

        Write-Dim "Enter a number, a model name, or press Enter for default."
        $ExtractInput = Read-Host "  Extract model [$DefaultExtractIdx] ($DefaultExtract)"
        $ExtractChoice = Resolve-Choice $ExtractInput $DefaultExtract $LocalModels
        $MergeInput = Read-Host "  Merge model   [$DefaultMergeIdx] ($DefaultMerge)"
        $MergeChoice = Resolve-Choice $MergeInput $DefaultMerge $LocalModels

        $configObj = @{
            extract_model = "local:$ExtractChoice"
            merge_model = "local:$MergeChoice"
            local_base_url = $LocalUrl
        }
        $configObj | ConvertTo-Json | Set-Content $ConfigFile -Encoding ASCII
        Write-Ok "Configured for local LLM: $ExtractChoice (extract), $MergeChoice (merge)"
        Write-Dim "No API key needed. Change models anytime with: gyrus models"
    }
}

if ($ModelMode -eq "1") {
    # --- Cloud path ---
    $NeedsKey = $true
    if (Test-Path $EnvFile) {
        $content = Get-Content $EnvFile -Raw
        if ($content -match "ANTHROPIC_API_KEY=" -and $content -notmatch "PASTE_YOUR") {
            Write-Ok "Key already configured"
            $NeedsKey = $false
        }
    }

    if ($NeedsKey) {
        Write-Host ""
        Write-Host "  Gyrus needs one key: an " -NoNewline
        Write-Host "Anthropic API key" -ForegroundColor White
        Write-Host "  Get one (free to start): " -NoNewline
        Write-Host "https://console.anthropic.com/settings/keys" -ForegroundColor White
        Write-Host ""
        $AnthroKey = Read-Host "  Anthropic API key"
        while ([string]::IsNullOrWhiteSpace($AnthroKey)) {
            Write-Warn "This is the only key required (or choose Local above)."
            $AnthroKey = Read-Host "  Anthropic API key"
        }

        "ANTHROPIC_API_KEY=$AnthroKey" | Set-Content $EnvFile -Encoding ASCII

        # Restrict permissions to current user only
        $acl = Get-Acl $EnvFile
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
            "FullControl", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl $EnvFile $acl
        Write-Ok "Saved to $EnvFile"
    }

    # Create default config for cloud mode
    if (-not (Test-Path $ConfigFile)) {
        @'
{
  "extract_model": "haiku",
  "merge_model": "sonnet"
}
'@ | Set-Content $ConfigFile -Encoding ASCII
        Write-Ok "Default config created (Haiku for extraction, Sonnet for merging)"
        Write-Dim "Change models anytime with: gyrus models  (or edit $ConfigFile)"
    }
}

# --- Step 4.5: GitHub sync ---
Write-Step "Step 4.5: Cross-machine sync via GitHub (recommended)"

Write-Host ""
Write-Dim "A private GitHub repo keeps your knowledge base in sync across all"
Write-Dim "your machines. Every ``gyrus`` run pulls + pushes automatically."
Write-Host ""

$GhCmd = Get-Command gh -ErrorAction SilentlyContinue
$GhOk = $false
if ($GhCmd) {
    $null = & gh auth status 2>&1
    if ($LASTEXITCODE -eq 0) { $GhOk = $true } else {
        Write-Warn "gh CLI is installed but not logged in. Run: gh auth login"
        Write-Dim "Then: gyrus init  (to set up GitHub sync later)"
    }
} else {
    Write-Warn "gh CLI not installed — skipping GitHub sync."
    Write-Dim "To enable later: winget install GitHub.cli ; gh auth login ; gyrus init"
}

if ($GhOk) {
    $GhAction = "skip"
    if (-not [string]::IsNullOrWhiteSpace($Clone)) {
        $GhAction = "clone"
        Write-Dim "Will clone from: $Clone"
    } else {
        Write-Host "  [1] Create new private repo (first machine)"
        Write-Host "  [2] Clone existing repo (second machine)"
        Write-Host "  [3] Skip"
        Write-Host ""
        $GhChoice = Read-Host "  Choice [1]"
        switch ($GhChoice) {
            "2" {
                $GhAction = "clone"
                $Clone = Read-Host "  Repo URL (e.g. github.com/you/gyrus-knowledge)"
            }
            "3" { $GhAction = "skip" }
            default { $GhAction = "create" }
        }
    }

    if ($GhAction -eq "create") {
        $RepoName = Read-Host "  Repo name [gyrus-knowledge]"
        if ([string]::IsNullOrWhiteSpace($RepoName)) { $RepoName = "gyrus-knowledge" }

        if (-not (Test-Path "$GyrusDir\.git")) {
            & git -C $GyrusDir init --initial-branch=main --quiet
            @"
# secrets
.env

# python
__pycache__/
*.pyc

# gyrus code (managed by ``gyrus update``, not sync)
ingest.py
storage.py
storage_notion.py
eval_prompts.py
model-comparison.html

# per-machine state
.ingest-state.json
ingest.log
latest-digest.md
"@ | Set-Content "$GyrusDir\.gitignore" -Encoding ASCII
            & git -C $GyrusDir add -A
            # Fallback identity so `git commit` doesn't fail when user hasn't
            # configured user.email/user.name. Real config still takes precedence.
            & git -C $GyrusDir -c user.email=gyrus@localhost -c user.name=gyrus commit -m "gyrus: initial" --quiet
        }

        & gh repo create $RepoName --private --source $GyrusDir --remote origin --push 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Created private repo and pushed initial state"
            Write-Ok "Auto-sync enabled (every run pulls & pushes)"
        } else {
            Write-Warn "gh repo create failed — run ``gyrus init`` later to retry."
        }

    } elseif ($GhAction -eq "clone" -and -not [string]::IsNullOrWhiteSpace($Clone)) {
        # Normalize URL
        if ($Clone -notmatch "^(https?://|git@|ssh://)") {
            if ($Clone -match "^github\.com/") { $Clone = "https://$Clone" }
            else { $Clone = "https://github.com/$Clone" }
        }

        # Back up code files (they're gitignored so clone won't bring them)
        $Stash = "$env:TEMP\gyrus-install-stash-$([guid]::NewGuid().Guid.Substring(0,8))"
        New-Item -ItemType Directory -Path $Stash -Force | Out-Null
        Copy-Item "$GyrusDir\*.py" $Stash -ErrorAction SilentlyContinue

        # Clone into temp then overlay
        $TmpClone = "$env:TEMP\gyrus-install-clone-$([guid]::NewGuid().Guid.Substring(0,8))"
        & git clone $Clone $TmpClone 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Copy-Item "$TmpClone\*" $GyrusDir -Recurse -Force
            Copy-Item "$TmpClone\.git" $GyrusDir -Recurse -Force
            Copy-Item "$TmpClone\.gitignore" $GyrusDir -Force -ErrorAction SilentlyContinue
            Copy-Item "$Stash\*.py" $GyrusDir -Force -ErrorAction SilentlyContinue
            Remove-Item $TmpClone, $Stash -Recurse -Force -ErrorAction SilentlyContinue
            Write-Ok "Cloned existing knowledge base"
            Write-Ok "Auto-sync enabled (every run pulls & pushes)"
        } else {
            Write-Warn "git clone failed — run ``gyrus init --clone $Clone`` later to retry."
            Remove-Item $TmpClone, $Stash -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Dim "Skipped. Run ``gyrus init`` later to enable GitHub sync."
    }
}

# --- Step 5: Install skills ---
Write-Step "Step 5: Installing skills for your AI tools..."

$ClaudeDir = "$env:USERPROFILE\.claude"
if (Test-Path $ClaudeDir) {
    $ClaudeCmdDir = "$ClaudeDir\commands"
    New-Item -ItemType Directory -Path $ClaudeCmdDir -Force | Out-Null
    $SkillFile = Join-Path $ScriptDir "skills\claude-code\gyrus.md"
    if (Test-Path $SkillFile) {
        Copy-Item $SkillFile "$ClaudeCmdDir\gyrus.md" -Force
        Write-Ok "Claude Code: /gyrus command installed"
    }
}

$CodexDir = "$env:USERPROFILE\.codex"
if (Test-Path $CodexDir) {
    $CodexSkillDir = "$GyrusDir\skills\codex"
    New-Item -ItemType Directory -Path $CodexSkillDir -Force | Out-Null
    $CodexFile = Join-Path $ScriptDir "skills\codex\gyrus-instructions.md"
    if (Test-Path $CodexFile) {
        Copy-Item $CodexFile "$CodexSkillDir\gyrus-instructions.md" -Force
        Write-Ok "Codex: instructions saved"
    }
}

$CoworkDir = "$env:APPDATA\Claude\local-agent-mode-sessions"
if (Test-Path $CoworkDir) {
    $CoworkSkillDir = "$GyrusDir\skills\cowork\gyrus"
    New-Item -ItemType Directory -Path $CoworkSkillDir -Force | Out-Null
    $CoworkFile = Join-Path $ScriptDir "skills\cowork\gyrus\SKILL.md"
    if (Test-Path $CoworkFile) {
        Copy-Item $CoworkFile "$CoworkSkillDir\SKILL.md" -Force
        Write-Ok "Cowork: skill installed"
    }
}

# --- Step 6: Scheduled task ---
Write-Step "Step 6: Setting up automatic sync..."

Write-Host ""
Write-Host "  How often should Gyrus check for new sessions?" -ForegroundColor White
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "[1]" -ForegroundColor White -NoNewline; Write-Host " Every hour " -NoNewline; Write-Host "(recommended - costs nothing when idle)" -ForegroundColor DarkGray
Write-Host "  " -NoNewline; Write-Host "[2]" -ForegroundColor White -NoNewline; Write-Host " Every 30 minutes"
Write-Host "  " -NoNewline; Write-Host "[3]" -ForegroundColor White -NoNewline; Write-Host " Every 4 hours"
Write-Host "  " -NoNewline; Write-Host "[4]" -ForegroundColor White -NoNewline; Write-Host " Every 12 hours"
Write-Host "  " -NoNewline; Write-Host "[5]" -ForegroundColor White -NoNewline; Write-Host " Once a day"
Write-Host ""
Write-Dim "Gyrus only calls the LLM when it finds new sessions."
Write-Dim "No new work = no API calls = zero cost."
Write-Host ""
$FreqChoice = Read-Host "  Frequency [1]"
if ([string]::IsNullOrWhiteSpace($FreqChoice)) { $FreqChoice = "1" }

switch ($FreqChoice) {
    "2" { $Interval = New-TimeSpan -Minutes 30; $FreqLabel = "every 30 minutes" }
    "3" { $Interval = New-TimeSpan -Hours 4; $FreqLabel = "every 4 hours" }
    "4" { $Interval = New-TimeSpan -Hours 12; $FreqLabel = "every 12 hours" }
    "5" { $Interval = New-TimeSpan -Hours 24; $FreqLabel = "once a day" }
    default { $Interval = New-TimeSpan -Hours 1; $FreqLabel = "every hour" }
}

# Parse API key from .env
$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match "^([^#=]+)=(.*)$") {
        $envVars[$matches[1].Trim()] = $matches[2].Trim()
    }
}

$TaskName = "GyrusIngestion"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

# Use uv to run Python — self-contained, no system Python dependency
# ingest.py auto-loads .env, so no need to embed the API key in the task
$Action = New-ScheduledTaskAction -Execute $UvCmd -Argument "run --python $UvPython `"$IngestScript`"" -WorkingDirectory $GyrusDir
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval $Interval
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries

if ($existingTask) {
    Set-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings | Out-Null
    Write-Ok "Updated scheduled task ($FreqLabel)"
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Gyrus knowledge ingestion - $FreqLabel" | Out-Null
    Write-Ok "Created scheduled task ($FreqLabel)"
}

# --- Step 7: Scan for AI tools ---
Write-Step "Step 7: Scanning for AI tool sessions..."

$FoundTools = @()
$FoundCounts = @()
$FoundIndex = 0

function Scan-Source($Name, $Path) {
    if (Test-Path $Path) {
        $count = (Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 500).Count
        if ($count -gt 0) {
            $script:FoundIndex++
            $script:FoundTools += $Name
            $script:FoundCounts += $count
            Write-Host "  " -NoNewline
            Write-Host "[$script:FoundIndex]" -ForegroundColor Green -NoNewline
            Write-Host " ${Name}: " -NoNewline
            Write-Host "$count" -ForegroundColor White -NoNewline
            Write-Host " session files found"
        }
    }
}

Scan-Source "Claude Code" "$env:USERPROFILE\.claude\projects"
Scan-Source "Claude Cowork" "$env:APPDATA\Claude\local-agent-mode-sessions"
Scan-Source "Antigravity / Gemini" "$env:USERPROFILE\.gemini\antigravity\brain"
Scan-Source "Codex" "$env:USERPROFILE\.codex\sessions"
Scan-Source "Cursor" "$env:APPDATA\Cursor\User\workspaceStorage"
Scan-Source "Copilot (VS Code)" "$env:APPDATA\Code\User\workspaceStorage"
Scan-Source "Cline" "$env:APPDATA\Code\User\globalStorage\saoudrizwan.claude-dev\tasks"
Scan-Source "Continue.dev" "$env:USERPROFILE\.continue\sessions"
Scan-Source "OpenCode" "$env:LOCALAPPDATA\opencode\storage\session"
# Aider: search common project locations for history files
$AiderDirs = @("$env:USERPROFILE", "$env:USERPROFILE\Documents", "$env:USERPROFILE\Projects",
               "$env:USERPROFILE\repos", "$env:USERPROFILE\code", "$env:USERPROFILE\dev", "$env:USERPROFILE\src")
$AiderCount = 0
foreach ($d in $AiderDirs) {
    if (Test-Path $d) {
        $AiderCount += (Get-ChildItem -Path $d -Filter ".aider.chat.history.md" -Recurse -Depth 4 -File -ErrorAction SilentlyContinue | Select-Object -First 100).Count
    }
}
if ($AiderCount -gt 0) {
    $script:FoundIndex++
    $script:FoundTools += "Aider"
    $script:FoundCounts += $AiderCount
    Write-Host "  " -NoNewline
    Write-Host "[$script:FoundIndex]" -ForegroundColor Green -NoNewline
    Write-Host " Aider: " -NoNewline
    Write-Host "$AiderCount" -ForegroundColor White -NoNewline
    Write-Host " session files found"
}

$TotalFound = $FoundTools.Count

if ($TotalFound -eq 0) {
    Write-Warn "No AI tool sessions found on this machine."
    Write-Dim "Gyrus will still check for new sessions on each scheduled run."
    $ExcludeInput = ""
} else {
    Write-Host ""
    Write-Host "  Found $TotalFound AI tools with session history!" -ForegroundColor White
    Write-Host ""
    Write-Dim "Gyrus will scan all of them by default."
    Write-Dim "Press Enter to include all, or type numbers to exclude (e.g., '2 4'):"
    Write-Host ""
    $ExcludeInput = Read-Host "  Exclude (or Enter for all)"
}

# Map display names to ingest.py type keys
$ToolKeyMap = @{
    "Claude Code" = "claude-code"
    "Claude Cowork" = "cowork"
    "Antigravity / Gemini" = "antigravity"
    "Codex" = "codex"
    "Cursor" = "cursor"
    "Copilot (VS Code)" = "copilot"
    "Cline" = "cline"
    "Continue.dev" = "continue"
    "OpenCode" = "opencode"
    "Aider" = "aider"
}

$ExcludedKeys = @()
if (-not [string]::IsNullOrWhiteSpace($ExcludeInput)) {
    foreach ($num in ($ExcludeInput -split '\s+')) {
        $idx = [int]$num - 1
        if ($idx -ge 0 -and $idx -lt $TotalFound) {
            $toolName = $FoundTools[$idx]
            Write-Warn "Excluding: $toolName"
            if ($ToolKeyMap.ContainsKey($toolName)) {
                $ExcludedKeys += $ToolKeyMap[$toolName]
            }
        }
    }
}

# Always save excluded_tools to config.json (empty array = include all)
$ConfigFile = "$GyrusDir\config.json"
if (Test-Path $ConfigFile) {
    $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName excluded_tools -NotePropertyValue @($ExcludedKeys) -Force
    $cfg | ConvertTo-Json -Depth 4 | Set-Content $ConfigFile -Encoding ASCII
    if ($ExcludedKeys.Count -gt 0) {
        Write-Ok "Saved exclusions to config.json"
    } else {
        Write-Ok "All tools enabled"
    }
}

# --- Build knowledge base ---
Write-Host ""
Write-Host "  Ready to build your knowledge base?" -ForegroundColor White
Write-Dim "Gyrus will scan your sessions, extract insights, and build"
Write-Dim "organized wiki pages per project. Takes a few minutes."
Write-Host ""
$DoBuild = Read-Host "  Build now? [Y/n]"
if ([string]::IsNullOrWhiteSpace($DoBuild)) { $DoBuild = "Y" }

if ($DoBuild -match "^[Yy]") {
    Write-Host ""
    Write-Host "  Building your knowledge base..." -ForegroundColor White
    Write-Host ("-" * 45)

    & $UvCmd run --python $UvPython $IngestScript --anthropic-key $envVars["ANTHROPIC_API_KEY"] 2>&1

    Write-Host ("-" * 45)

    $ProjectDir = "$GyrusDir\projects"
    if (Test-Path $ProjectDir) {
        $PageCount = (Get-ChildItem "$ProjectDir\*.md" -ErrorAction SilentlyContinue).Count
        if ($PageCount -gt 0) {
            Write-Host ""
            Write-Host "  Gyrus found and organized $PageCount projects!" -ForegroundColor Green
            Write-Host ""
            Write-Host "  Try it:" -ForegroundColor White
            $FirstPage = (Get-ChildItem "$ProjectDir\*.md" | Select-Object -First 1).FullName
            Write-Host "    cat $FirstPage"
            Write-Host ""
        }
    }
} else {
    Write-Host ""
    Write-Dim "Skipped. Run this later (reads API key from .env automatically):"
    Write-Host "    cd $GyrusDir; `$env:ANTHROPIC_API_KEY=((Get-Content .env | Select-String '^ANTHROPIC_API_KEY=').ToString().Split('=',2)[1]); uv run --python $UvPython ingest.py --anthropic-key `$env:ANTHROPIC_API_KEY"
    Write-Host ""
}

# --- Done ---
Write-Host ""
Write-Host "  Gyrus is running!" -ForegroundColor Green
Write-Host ""
Write-Host "  From now on, Gyrus ($FreqLabel):"
Write-Host "  - Scans new AI tool sessions"
Write-Host "  - Extracts decisions, insights, status changes"
Write-Host "  - Refines your wiki pages (they get smarter over time)"
Write-Host ""
Write-Host "  Your knowledge base: $GyrusDir\projects\"
Write-Host "  Status overview:     $GyrusDir\status.md"
Write-Host "  Logs:                $LogFile"
Write-Host ""
