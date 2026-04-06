# Gyrus Installer for Windows
# One command, one API key, done.
#
# Usage:
#   .\install.ps1

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

Write-Host ""
Write-Host "  Default: " -NoNewline
Write-Host $GyrusDir -ForegroundColor White
Write-Dim "To sync across machines, point to a cloud folder:"
Write-Dim "  OneDrive:  ~\OneDrive\gyrus"
Write-Dim "  Dropbox:   ~\Dropbox\gyrus"
Write-Dim "  Obsidian:  ~\your-vault\gyrus"
Write-Host ""
$CustomDir = Read-Host "  Path (Enter for default)"

if (-not [string]::IsNullOrWhiteSpace($CustomDir)) {
    $CustomDir = $CustomDir -replace "^~", $env:USERPROFILE
    $GyrusDir = $CustomDir
    $IngestScript = "$GyrusDir\ingest.py"
    $StorageScript = "$GyrusDir\storage.py"
    $StorageNotionScript = "$GyrusDir\storage_notion.py"
    $EnvFile = "$GyrusDir\.env"
    $LogFile = "$GyrusDir\ingest.log"

    # Create junction from default location if using custom path
    $DefaultDir = "$env:USERPROFILE\.gyrus"
    if ($GyrusDir -ne $DefaultDir) {
        if (Test-Path $DefaultDir) {
            $item = Get-Item $DefaultDir -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                Remove-Item $DefaultDir -Force
            }
        }
        if (-not (Test-Path $DefaultDir)) {
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
    Write-Ok "Downloaded to $GyrusDir"
}

# --- Step 4: API key ---
Write-Step "Step 4: Anthropic API key"

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
        Write-Warn "This is the only key required."
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

# Create default config
$ConfigFile = "$GyrusDir\config.json"
if (-not (Test-Path $ConfigFile)) {
    @'
{
  "extract_model": "haiku",
  "merge_model": "sonnet"
}
'@ | Set-Content $ConfigFile -Encoding ASCII
    Write-Ok "Default config created (Haiku for extraction, Sonnet for merging)"
    Write-Dim "Change models anytime in $ConfigFile"
    Write-Dim "Options: haiku, sonnet, opus, gpt-5.4, gpt-5.4-mini, gpt-5.4-nano, gemini-flash, gemini-pro"
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
