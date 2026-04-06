#!/bin/bash
# Gyrus Installer
# One command, one API key, done.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/prismindanalytics/gyrus/main/install.sh | bash
#   — or —
#   ./install.sh

set -euo pipefail

GYRUS_DIR="$HOME/.gyrus"
INGEST_SCRIPT="$GYRUS_DIR/ingest.py"
STORAGE_SCRIPT="$GYRUS_DIR/storage.py"
STORAGE_NOTION_SCRIPT="$GYRUS_DIR/storage_notion.py"
ENV_FILE="$GYRUS_DIR/.env"
LOG_FILE="$GYRUS_DIR/ingest.log"
UV_PYTHON="3.12"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

print_step() { echo -e "\n${BOLD}$1${NC}"; }
print_ok() { echo -e "  ${GREEN}✓${NC} $1"; }
print_warn() { echo -e "  ${YELLOW}!${NC} $1"; }
print_fail() { echo -e "  ${RED}✗${NC} $1"; }

echo ""
echo -e "${BOLD}Gyrus${NC} — your AI tools' shared brain"
echo "======================================="

# ─── Step 1: uv (Python toolchain) ───
print_step "Step 1: Setting up Python runtime..."

if command -v uv &>/dev/null; then
  UV=$(command -v uv)
  print_ok "uv found at $UV"
else
  echo -e "  ${DIM}Installing uv (Python toolchain by Astral — manages Python for you)...${NC}"
  curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
  # Source the env so uv is available in this session
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if command -v uv &>/dev/null; then
    UV=$(command -v uv)
    print_ok "uv installed at $UV"
  else
    print_fail "Could not install uv. Install manually: https://docs.astral.sh/uv/"
    exit 1
  fi
fi

# Ensure Python is available via uv (downloads if needed, no system modification)
echo -e "  ${DIM}Ensuring Python $UV_PYTHON is available...${NC}"
"$UV" python install "$UV_PYTHON" 2>/dev/null || true
print_ok "Python $UV_PYTHON ready (managed by uv — your system Python is untouched)"

# ─── Step 2: Storage location ───
print_step "Step 2: Where should Gyrus store your knowledge base?"

echo ""
echo -e "  ${BOLD}[1]${NC} Default: ${BOLD}~/.gyrus${NC} ${DIM}(local only)${NC}"
if [ "$(uname)" = "Darwin" ]; then
  ICLOUD_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/gyrus"
  echo -e "  ${BOLD}[2]${NC} iCloud:  ${DIM}~/Library/Mobile Documents/com~apple~CloudDocs/gyrus${NC}"
fi
echo -e "  ${BOLD}[3]${NC} Dropbox: ${DIM}~/Dropbox/gyrus${NC}"
echo -e "  ${BOLD}[4]${NC} Custom path ${DIM}(Obsidian vault, Google Drive, etc.)${NC}"
echo ""
echo -e "  ${DIM}To sync across machines, choose a cloud folder. Same knowledge base everywhere.${NC}"
echo ""
read -r -p "  Choice [1]: " SYNC_CHOICE < /dev/tty
SYNC_CHOICE="${SYNC_CHOICE:-1}"

CUSTOM_DIR=""
case "$SYNC_CHOICE" in
  2) CUSTOM_DIR="${ICLOUD_DIR:-}" ;;
  3) CUSTOM_DIR="$HOME/Dropbox/gyrus" ;;
  4)
    read -r -p "  Custom path: " CUSTOM_DIR < /dev/tty
    ;;
esac

if [ -n "$CUSTOM_DIR" ]; then
  # Expand ~ if present
  CUSTOM_DIR="${CUSTOM_DIR/#\~/$HOME}"
  GYRUS_DIR="$CUSTOM_DIR"
  INGEST_SCRIPT="$GYRUS_DIR/ingest.py"
  STORAGE_SCRIPT="$GYRUS_DIR/storage.py"
  STORAGE_NOTION_SCRIPT="$GYRUS_DIR/storage_notion.py"
  ENV_FILE="$GYRUS_DIR/.env"
  LOG_FILE="$GYRUS_DIR/ingest.log"

  # Create symlink from default location if using custom path
  if [ "$GYRUS_DIR" != "$HOME/.gyrus" ]; then
    if [ -L "$HOME/.gyrus" ]; then
      rm "$HOME/.gyrus"
    elif [ -d "$HOME/.gyrus" ] && [ ! "$(ls -A "$HOME/.gyrus" 2>/dev/null)" ]; then
      rmdir "$HOME/.gyrus" 2>/dev/null || true
    fi
    if [ ! -e "$HOME/.gyrus" ]; then
      ln -s "$GYRUS_DIR" "$HOME/.gyrus"
      print_ok "Symlinked ~/.gyrus -> $GYRUS_DIR"
    fi
  fi
fi

# ─── Step 3: Download / copy scripts ───
print_step "Step 3: Installing..."

mkdir -p "$GYRUS_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# If running from the repo, copy. If running from curl, download.
if [ -f "$SCRIPT_DIR/ingest.py" ]; then
  cp "$SCRIPT_DIR/ingest.py" "$INGEST_SCRIPT"
  cp "$SCRIPT_DIR/storage.py" "$STORAGE_SCRIPT"
  if [ -f "$SCRIPT_DIR/storage_notion.py" ]; then
    cp "$SCRIPT_DIR/storage_notion.py" "$STORAGE_NOTION_SCRIPT"
  fi
  print_ok "Installed to $GYRUS_DIR"
else
  # Download from GitHub
  REPO_URL="https://raw.githubusercontent.com/prismindanalytics/gyrus/main"
  curl -fsSL "$REPO_URL/ingest.py" -o "$INGEST_SCRIPT"
  curl -fsSL "$REPO_URL/storage.py" -o "$STORAGE_SCRIPT"
  curl -fsSL "$REPO_URL/storage_notion.py" -o "$STORAGE_NOTION_SCRIPT"
  curl -fsSL "$REPO_URL/eval_prompts.py" -o "$GYRUS_DIR/eval_prompts.py" 2>/dev/null || true
  print_ok "Downloaded to $GYRUS_DIR"
fi

# Create `gyrus` CLI command
GYRUS_BIN="$HOME/.local/bin/gyrus"
mkdir -p "$(dirname "$GYRUS_BIN")"
export PATH="$HOME/.local/bin:$PATH"  # ensure it's in PATH for this session
cat > "$GYRUS_BIN" <<'WRAPPER'
#!/bin/bash
# Gyrus CLI — knowledge base for AI coding tools
# https://gyrus.sh

GYRUS_HOME="${GYRUS_HOME:-$HOME/.gyrus}"
UV_BIN="${UV_BIN:-$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")}"

# Translate subcommands to flags
case "${1:-}" in
  update)       shift; set -- --update "$@" ;;
  compare)      shift; set -- --compare-models "$@" ;;
  digest)       shift; set -- --digest "$@" ;;
  status)       shift; set -- --review-status "$@" ;;
  eval)         shift; set -- --eval "$@" ;;
  curate)       shift; set -- --eval-curate "$@" ;;
  run)          shift ;;  # explicit run, strip the word
  help|-h|--help)
    echo "Usage: gyrus [command] [options]"
    echo ""
    echo "Commands:"
    echo "  (none)       Run ingestion (extract + merge)"
    echo "  compare      Benchmark models on your sessions"
    echo "  status       Review and set project statuses"
    echo "  digest       Generate activity digest"
    echo "  eval         Run prompt quality evaluation"
    echo "  curate       Create golden test fixtures"
    echo "  update       Update Gyrus to latest version"
    echo ""
    echo "Options:"
    echo "  --dry-run    Run without saving"
    echo "  --backfill   Rebuild pages from existing thoughts"
    echo ""
    echo "Config: $GYRUS_HOME/config.json"
    echo "Docs:   https://gyrus.sh"
    exit 0
    ;;
esac

cd "$GYRUS_HOME" && "$UV_BIN" run --python 3.12 ingest.py "$@"
WRAPPER
chmod +x "$GYRUS_BIN"
print_ok "Installed 'gyrus' command to $GYRUS_BIN"
echo -e "  ${DIM}Usage: gyrus compare, gyrus update, gyrus digest, gyrus help${NC}"
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
  echo ""
  echo -e "  ${YELLOW}!${NC} Add to your shell profile to use 'gyrus' from anywhere:"
  echo -e "  ${DIM}  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc${NC}"
fi

# ─── Step 4: API keys ───
print_step "Step 4: API keys"

echo ""
echo -e "  ${DIM}Enter keys for the providers you use (Enter to skip):${NC}"
echo -e "  ${DIM}At least one key is required. More keys = more models to compare.${NC}"
echo ""

HAS_KEY=false
ANTHRO_KEY=""
OPENAI_KEY=""
GOOGLE_KEY=""

# Check existing keys
[ -f "$ENV_FILE" ] && grep -q "ANTHROPIC_API_KEY=sk-" "$ENV_FILE" 2>/dev/null && HAS_KEY=true
[ -f "$ENV_FILE" ] && grep -q "OPENAI_API_KEY=sk-" "$ENV_FILE" 2>/dev/null && HAS_KEY=true
[ -f "$ENV_FILE" ] && grep -q "GEMINI_API_KEY=AI" "$ENV_FILE" 2>/dev/null && HAS_KEY=true

if [ "$HAS_KEY" = true ]; then
  print_ok "Keys already configured in .env"
else
  # Anthropic
  echo -e "  ${BOLD}Anthropic${NC} ${DIM}(https://console.anthropic.com/settings/keys)${NC}"
  read -r -p "    API key: " ANTHRO_KEY < /dev/tty
  if [ -n "$ANTHRO_KEY" ]; then print_ok "Saved"; else echo -e "    ${DIM}⊘ Skipped${NC}"; fi
  echo ""

  # OpenAI
  echo -e "  ${BOLD}OpenAI${NC} ${DIM}(https://platform.openai.com/api-keys)${NC}"
  read -r -p "    API key: " OPENAI_KEY < /dev/tty
  if [ -n "$OPENAI_KEY" ]; then print_ok "Saved"; else echo -e "    ${DIM}⊘ Skipped${NC}"; fi
  echo ""

  # Google
  echo -e "  ${BOLD}Google${NC} ${DIM}(https://aistudio.google.com/apikey)${NC}"
  read -r -p "    API key: " GOOGLE_KEY < /dev/tty
  if [ -n "$GOOGLE_KEY" ]; then print_ok "Saved"; else echo -e "    ${DIM}⊘ Skipped${NC}"; fi
  echo ""

  # Require at least one
  while [ -z "$ANTHRO_KEY" ] && [ -z "$OPENAI_KEY" ] && [ -z "$GOOGLE_KEY" ]; do
    print_warn "At least one API key is required."
    echo -e "  ${BOLD}Anthropic${NC} ${DIM}(https://console.anthropic.com/settings/keys)${NC}"
    read -r -p "    API key: " ANTHRO_KEY < /dev/tty
    if [ -n "$ANTHRO_KEY" ]; then break; fi
    echo -e "  ${BOLD}OpenAI${NC} ${DIM}(https://platform.openai.com/api-keys)${NC}"
    read -r -p "    API key: " OPENAI_KEY < /dev/tty
    if [ -n "$OPENAI_KEY" ]; then break; fi
  done

  # Write .env — only write keys that were actually entered (non-empty)
  : > "$ENV_FILE"
  [ -n "$ANTHRO_KEY" ] && echo "ANTHROPIC_API_KEY=${ANTHRO_KEY}" >> "$ENV_FILE"
  [ -n "$OPENAI_KEY" ] && echo "OPENAI_API_KEY=${OPENAI_KEY}" >> "$ENV_FILE"
  [ -n "$GOOGLE_KEY" ] && echo "GEMINI_API_KEY=${GOOGLE_KEY}" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  print_ok "Saved to $ENV_FILE"
fi

# Create default config — pick best models for available keys
CONFIG_FILE="$GYRUS_DIR/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
  # Auto-select best extract model based on available keys
  EXTRACT_MODEL="haiku"  # fallback
  MERGE_MODEL="haiku"    # fallback
  if [ -n "$OPENAI_KEY" ] || ([ -f "$ENV_FILE" ] && grep -q "OPENAI_API_KEY=sk-" "$ENV_FILE" 2>/dev/null); then
    EXTRACT_MODEL="gpt-4.1-mini"
  elif [ -n "$GOOGLE_KEY" ] || ([ -f "$ENV_FILE" ] && grep -q "GEMINI_API_KEY=AI" "$ENV_FILE" 2>/dev/null); then
    EXTRACT_MODEL="gemini-flash"
  fi
  if [ -n "$ANTHRO_KEY" ] || ([ -f "$ENV_FILE" ] && grep -q "ANTHROPIC_API_KEY=sk-" "$ENV_FILE" 2>/dev/null); then
    MERGE_MODEL="sonnet"
  elif [ -n "$OPENAI_KEY" ] || ([ -f "$ENV_FILE" ] && grep -q "OPENAI_API_KEY=sk-" "$ENV_FILE" 2>/dev/null); then
    MERGE_MODEL="gpt-4.1"
  elif [ -n "$GOOGLE_KEY" ] || ([ -f "$ENV_FILE" ] && grep -q "GEMINI_API_KEY=AI" "$ENV_FILE" 2>/dev/null); then
    MERGE_MODEL="gemini-pro"
  fi
  cat > "$CONFIG_FILE" <<CEOF
{
  "extract_model": "$EXTRACT_MODEL",
  "merge_model": "$MERGE_MODEL"
}
CEOF
  print_ok "Default config: $EXTRACT_MODEL (extraction), $MERGE_MODEL (merging)"
  echo -e "  ${DIM}Change anytime in $CONFIG_FILE or run: gyrus compare${NC}"
fi

# ─── Step 5: Install skills for AI tools ───
print_step "Step 5: Installing skills for your AI tools..."

# Helper: copy local skill or download from GitHub
install_skill() {
  local src_path="$1" dest_path="$2" label="$3"
  if [ -f "$SCRIPT_DIR/$src_path" ]; then
    mkdir -p "$(dirname "$dest_path")"
    cp "$SCRIPT_DIR/$src_path" "$dest_path"
    print_ok "$label"
  elif [ -n "${REPO_URL:-}" ]; then
    mkdir -p "$(dirname "$dest_path")"
    curl -fsSL "$REPO_URL/$src_path" -o "$dest_path" 2>/dev/null && print_ok "$label" || true
  fi
}

# Use REPO_URL for downloads (set during download path, may not exist for local installs)
SKILL_REPO_URL="https://raw.githubusercontent.com/prismindanalytics/gyrus/main"

# Detect available tools and offer skill installation
SKILL_OPTIONS=()
SKILL_LABELS=()
SKILL_INSTALLED=()

if [ -d "$HOME/.claude" ]; then
  SKILL_OPTIONS+=("claude-code")
  SKILL_LABELS+=("Claude Code /gyrus slash command")
fi
if [ -d "$HOME/.codex" ] || [ -d "$HOME/.codex/sessions" ]; then
  SKILL_OPTIONS+=("codex")
  SKILL_LABELS+=("Codex AGENTS.md instructions")
fi
COWORK_SKILLS_DIR="$HOME/Library/Application Support/Claude/local-agent-mode-sessions"
if [ -d "$COWORK_SKILLS_DIR" ] || [ -d "$HOME/.config/Claude/local-agent-mode-sessions" ]; then
  SKILL_OPTIONS+=("cowork")
  SKILL_LABELS+=("Cowork /gyrus skill")
fi

if [ ${#SKILL_OPTIONS[@]} -gt 0 ]; then
  echo ""
  echo -e "  ${DIM}Detected AI tools. Skills let your tools query the Gyrus knowledge base.${NC}"
  echo ""
  for i in "${!SKILL_OPTIONS[@]}"; do
    echo -e "  ${GREEN}[$((i+1))]${NC} ${SKILL_LABELS[$i]}"
  done
  echo ""
  echo -e "  ${DIM}Press Enter to install all, or type numbers to skip (e.g., '2'):${NC}"
  read -r -p "  Skip (or Enter for all): " SKILL_SKIP < /dev/tty

  for i in "${!SKILL_OPTIONS[@]}"; do
    # Check if this index should be skipped
    skip=false
    if [ -n "${SKILL_SKIP:-}" ]; then
      for num in $SKILL_SKIP; do
        if [ "$((num-1))" -eq "$i" ]; then
          skip=true
          break
        fi
      done
    fi

    if [ "$skip" = false ]; then
      case "${SKILL_OPTIONS[$i]}" in
        claude-code)
          CLAUDE_CMD_DIR="$HOME/.claude/commands"
          install_skill "skills/claude-code/gyrus.md" "$CLAUDE_CMD_DIR/gyrus.md" "Claude Code: /gyrus command installed"
          ;;
        codex)
          install_skill "skills/codex/gyrus-instructions.md" "$GYRUS_DIR/skills/codex/gyrus-instructions.md" "Codex: instructions saved"
          echo -e "  ${DIM}  Add to your AGENTS.md: \"Read ~/.gyrus/skills/codex/gyrus-instructions.md for project context\"${NC}"
          ;;
        cowork)
          install_skill "skills/cowork/gyrus/SKILL.md" "$GYRUS_DIR/skills/cowork/gyrus/SKILL.md" "Cowork: /gyrus skill installed"
          ;;
      esac
    else
      echo -e "  ${DIM}⊘ Skipped: ${SKILL_LABELS[$i]}${NC}"
    fi
  done
else
  echo -e "  ${DIM}No AI tools detected — skills will be installed when you install tools later.${NC}"
fi

# ─── Step 6: Cron frequency ───
print_step "Step 6: Setting up automatic sync..."

echo ""
echo -e "  How often should Gyrus check for new sessions?"
echo ""
echo -e "  ${BOLD}[1]${NC} Every hour ${DIM}(recommended — costs nothing when idle)${NC}"
echo -e "  ${BOLD}[2]${NC} Every 30 minutes"
echo -e "  ${BOLD}[3]${NC} Every 4 hours"
echo -e "  ${BOLD}[4]${NC} Every 12 hours"
echo -e "  ${BOLD}[5]${NC} Once a day"
echo ""
echo -e "  ${DIM}Gyrus only calls the LLM when it finds new sessions.${NC}"
echo -e "  ${DIM}No new work = no API calls = zero cost.${NC}"
echo ""
read -r -p "  Frequency [1]: " FREQ_CHOICE < /dev/tty
FREQ_CHOICE="${FREQ_CHOICE:-1}"

case "$FREQ_CHOICE" in
  2) CRON_SCHEDULE="*/30 * * * *"; FREQ_LABEL="every 30 minutes" ;;
  3) CRON_SCHEDULE="0 */4 * * *"; FREQ_LABEL="every 4 hours" ;;
  4) CRON_SCHEDULE="0 */12 * * *"; FREQ_LABEL="every 12 hours" ;;
  5) CRON_SCHEDULE="0 9 * * *"; FREQ_LABEL="once a day (9 AM)" ;;
  *) CRON_SCHEDULE="0 * * * *"; FREQ_LABEL="every hour" ;;
esac

# Use uv to run Python — self-contained, no system Python dependency
UV_PATH=$(command -v uv)
# ingest.py auto-loads .env, so no need to embed the API key in the cron entry
CRON_CMD="$CRON_SCHEDULE cd \"$GYRUS_DIR\" && \"$UV_PATH\" run --python $UV_PYTHON \"$INGEST_SCRIPT\" >> \"$LOG_FILE\" 2>&1"

EXISTING_CRON=$(crontab -l 2>/dev/null || true)
if echo "$EXISTING_CRON" | grep -q "ingest.py"; then
  NEW_CRON=$(echo "$EXISTING_CRON" | grep -v "ingest.py" || true)
  (echo "$NEW_CRON"; echo "$CRON_CMD") | crontab -
  print_ok "Updated cron job ($FREQ_LABEL)"
else
  (echo "$EXISTING_CRON"; echo "$CRON_CMD") | crontab -
  print_ok "Installed cron job ($FREQ_LABEL)"
fi

if [ "$(uname)" = "Darwin" ]; then
  echo -e "  ${DIM}Note: On macOS, cron needs Full Disk Access to read AI tool sessions.${NC}"
  echo -e "  ${DIM}Go to System Settings → Privacy & Security → Full Disk Access → add /usr/sbin/cron${NC}"
fi

# ─── Step 7: Scan & Select Sources ───
print_step "Step 7: Scanning for AI tool sessions..."

FOUND_TOOLS=()
FOUND_COUNTS=()
FOUND_INDEX=0

check_source() {
  local name="$1"
  local path="$2"
  local pattern="${3:---}"  # optional file pattern
  if [ -d "$path" ]; then
    local count label
    if [ "$pattern" = "--" ]; then
      count=$(find "$path" -maxdepth 3 -type d 2>/dev/null | wc -l | tr -d ' ' || echo "0")
    else
      count=$(find "$path" -maxdepth 4 -name "$pattern" -type f 2>/dev/null | wc -l | tr -d ' ' || echo "0")
    fi
    if [ "$count" -gt 0 ] 2>/dev/null; then
      FOUND_INDEX=$((FOUND_INDEX + 1))
      FOUND_TOOLS+=("$name")
      FOUND_COUNTS+=("$count")
      if [ "$count" -gt 999 ]; then
        label="${count%???},${count#${count%???}}"  # rough thousands formatting
      else
        label="$count"
      fi
      echo -e "  ${GREEN}[$FOUND_INDEX]${NC} $name: ${BOLD}$label${NC} session files found"
    fi
  fi
}

check_source "Claude Code" "$HOME/.claude/projects" "*.jsonl"
# Cowork: macOS vs Linux
if [ -d "$HOME/Library/Application Support/Claude/local-agent-mode-sessions" ]; then
  check_source "Claude Cowork" "$HOME/Library/Application Support/Claude/local-agent-mode-sessions"
elif [ -d "$HOME/.config/Claude/local-agent-mode-sessions" ]; then
  check_source "Claude Cowork" "$HOME/.config/Claude/local-agent-mode-sessions"
fi
# Antigravity sessions are directories, not files
check_source "Antigravity / Gemini" "$HOME/.gemini/antigravity/brain"
check_source "Codex" "$HOME/.codex/sessions" "*.jsonl"
# Cursor: macOS vs Linux
if [ -d "$HOME/Library/Application Support/Cursor/User/workspaceStorage" ]; then
  check_source "Cursor" "$HOME/Library/Application Support/Cursor/User/workspaceStorage"
elif [ -d "$HOME/.config/Cursor/User/workspaceStorage" ]; then
  check_source "Cursor" "$HOME/.config/Cursor/User/workspaceStorage"
fi
# Copilot (VS Code)
if [ -d "$HOME/Library/Application Support/Code/User/workspaceStorage" ]; then
  check_source "Copilot (VS Code)" "$HOME/Library/Application Support/Code/User/workspaceStorage"
elif [ -d "$HOME/.config/Code/User/workspaceStorage" ]; then
  check_source "Copilot (VS Code)" "$HOME/.config/Code/User/workspaceStorage"
fi
# Cline
if [ -d "$HOME/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks" ]; then
  check_source "Cline" "$HOME/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks"
elif [ -d "$HOME/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks" ]; then
  check_source "Cline" "$HOME/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks"
fi
check_source "Continue.dev" "$HOME/.continue/sessions"
check_source "OpenCode" "$HOME/.local/share/opencode/storage/session"
# Aider: check for history files in common project locations
AIDER_DIRS=""
for d in "$HOME/Documents" "$HOME/Projects" "$HOME/repos" "$HOME/code" "$HOME/dev" "$HOME/src"; do
  [ -d "$d" ] && AIDER_DIRS="$AIDER_DIRS $d"
done
if [ -n "$AIDER_DIRS" ]; then
  AIDER_COUNT=$(find $AIDER_DIRS -maxdepth 4 -name ".aider.chat.history.md" -type f 2>/dev/null | sort -u | wc -l | tr -d ' ' || echo "0")
else
  AIDER_COUNT=0
fi
if [ "$AIDER_COUNT" -gt 0 ]; then
  FOUND_INDEX=$((FOUND_INDEX + 1))
  FOUND_TOOLS+=("Aider")
  FOUND_COUNTS+=("$AIDER_COUNT")
  echo -e "  ${GREEN}[$FOUND_INDEX]${NC} Aider: ${BOLD}$AIDER_COUNT${NC} session files found"
fi

TOTAL_FOUND=${#FOUND_TOOLS[@]}

if [ "$TOTAL_FOUND" -eq 0 ]; then
  print_warn "No AI tool sessions found on this machine."
  echo -e "  ${DIM}Gyrus will still check for new sessions on each scheduled run.${NC}"
  EXCLUDE_INPUT=""
else
  echo ""
  echo -e "  ${BOLD}Found $TOTAL_FOUND AI tools with session history!${NC}"
  echo ""
  echo -e "  ${DIM}Gyrus will scan all of them by default.${NC}"
  echo -e "  ${DIM}Press Enter to include all, or type numbers to exclude (e.g., '2 4'):${NC}"
  echo ""
  read -r -p "  Exclude (or Enter for all): " EXCLUDE_INPUT < /dev/tty
fi

tool_key_from_name() {
  case "$1" in
    "Claude Code") echo "claude-code" ;;
    "Claude Cowork") echo "cowork" ;;
    "Antigravity / Gemini") echo "antigravity" ;;
    "Codex") echo "codex" ;;
    "Cursor") echo "cursor" ;;
    "Copilot (VS Code)") echo "copilot" ;;
    "Cline") echo "cline" ;;
    "Continue.dev") echo "continue" ;;
    "OpenCode") echo "opencode" ;;
    "Aider") echo "aider" ;;
    *) echo "" ;;
  esac
}

EXCLUDED_KEYS=()
if [ -n "${EXCLUDE_INPUT:-}" ]; then
  for num in $EXCLUDE_INPUT; do
    idx=$((num - 1))
    if [ "$idx" -ge 0 ] && [ "$idx" -lt "$TOTAL_FOUND" ]; then
      tool_name="${FOUND_TOOLS[$idx]}"
      tool_key="$(tool_key_from_name "$tool_name")"
      print_warn "Excluding: $tool_name"
      if [ -n "$tool_key" ]; then
        EXCLUDED_KEYS+=("$tool_key")
      fi
    fi
  done
fi

# Always save excluded_tools to config.json (empty array = include all)
CONFIG_FILE="$GYRUS_DIR/config.json"
if [ -f "$CONFIG_FILE" ]; then
  if [ "${#EXCLUDED_KEYS[@]}" -gt 0 ]; then
    EXCLUDE_JSON=$(printf '"%s",' "${EXCLUDED_KEYS[@]}")
    EXCLUDE_JSON="[${EXCLUDE_JSON%,}]"
  else
    EXCLUDE_JSON="[]"
  fi
  "$UV" run --python "$UV_PYTHON" -c "
import json
with open('$CONFIG_FILE') as f: cfg = json.load(f)
cfg['excluded_tools'] = $EXCLUDE_JSON
with open('$CONFIG_FILE', 'w') as f: json.dump(cfg, f, indent=2)
" 2>/dev/null || true
  if [ "${#EXCLUDED_KEYS[@]}" -gt 0 ]; then
    print_ok "Saved exclusions to config.json"
  else
    print_ok "All tools enabled"
  fi
fi

# ─── Step 8: Compare models (optional) ───
if [ "$TOTAL_FOUND" -gt 0 ]; then
  print_step "Step 8: Choose your extraction model"
  echo ""
  echo -e "  ${DIM}Gyrus can test different AI models on your sessions${NC}"
  echo -e "  ${DIM}so you can compare quality, speed, and cost.${NC}"
  echo -e "  ${DIM}Takes ~2 minutes. Opens a comparison page in your browser.${NC}"
  echo ""
  read -r -p "  Compare models? [Y/n]: " DO_COMPARE < /dev/tty
  DO_COMPARE="${DO_COMPARE:-Y}"

  set -a; source "$ENV_FILE"; set +a

  if [[ "$DO_COMPARE" =~ ^[Yy] ]]; then
    echo ""
    # Build key flags
    KEY_FLAGS=""
    [ -n "${ANTHROPIC_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --anthropic-key $ANTHROPIC_API_KEY"
    [ -n "${OPENAI_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --openai-key $OPENAI_API_KEY"
    [ -n "${GEMINI_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --google-key $GEMINI_API_KEY"
    "$UV" run --python "$UV_PYTHON" "$INGEST_SCRIPT" --compare-models $KEY_FLAGS < /dev/tty 2>&1 || true
  else
    echo -e "  ${DIM}Skipped. Run later with: gyrus compare${NC}"
  fi
fi

# ─── The Wow Moment: First Run ───
echo ""
echo -e "${BOLD}  Ready to build your knowledge base?${NC}"
echo -e "  ${DIM}Gyrus will show a cost and time estimate first.${NC}"
echo -e "  ${DIM}You can choose to run now and watch, run in background, or cancel.${NC}"
echo ""
read -r -p "  Start? [Y/n]: " DO_BUILD < /dev/tty
DO_BUILD="${DO_BUILD:-Y}"

set -a; source "$ENV_FILE"; set +a

if [[ "$DO_BUILD" =~ ^[Yy] ]]; then
  echo ""
  echo -e "${BOLD}  Building your knowledge base...${NC}"
  echo "─────────────────────────────────────────"
  # Build key flags from .env
  KEY_FLAGS=""
  [ -n "${ANTHROPIC_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --anthropic-key $ANTHROPIC_API_KEY"
  [ -n "${OPENAI_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --openai-key $OPENAI_API_KEY"
  [ -n "${GEMINI_API_KEY:-}" ] && KEY_FLAGS="$KEY_FLAGS --google-key $GEMINI_API_KEY"
  "$UV" run --python "$UV_PYTHON" "$INGEST_SCRIPT" $KEY_FLAGS < /dev/tty 2>&1 || true
  echo "─────────────────────────────────────────"

  # Show the wow result
  PAGE_COUNT=$(ls "$GYRUS_DIR/projects/"*.md 2>/dev/null | wc -l | tr -d ' ')
  if [ "$PAGE_COUNT" -gt 0 ]; then
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  Gyrus found and organized $PAGE_COUNT projects!${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  Here's what it built:"
    echo ""
    # Show project list from status.md
    if [ -f "$GYRUS_DIR/status.md" ]; then
      tail -n +4 "$GYRUS_DIR/status.md" | head -20 | while IFS= read -r line; do
        echo -e "  ${BLUE}$line${NC}"
      done
      if [ "$PAGE_COUNT" -gt 20 ]; then
        echo -e "  ${DIM}  ...and $((PAGE_COUNT - 20)) more${NC}"
      fi
    fi
    echo ""
    echo -e "  ${BOLD}Try it:${NC}"
    FIRST_PAGE=$(ls "$GYRUS_DIR/projects/" 2>/dev/null | head -1)
    echo "    cat \"$GYRUS_DIR/projects/$FIRST_PAGE\""
    echo ""
  fi
else
  echo ""
  echo -e "  ${DIM}Skipped. Run later with: gyrus${NC}"
  echo ""
fi

# ─── Done ───
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Gyrus is running!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  From now on, Gyrus ($FREQ_LABEL):"
echo "  • Scans new AI tool sessions"
echo "  • Extracts decisions, insights, status changes"
echo "  • Refines your wiki pages (they get smarter over time)"
echo ""
echo "  Your knowledge base: $GYRUS_DIR/projects/"
echo "  Status overview:     $GYRUS_DIR/status.md"
echo "  Logs:                $LOG_FILE"
echo ""
echo "  Commands:"
echo "    gyrus                # run ingestion"
echo "    gyrus compare        # benchmark and choose models"
echo "    gyrus status         # review project statuses"
echo "    gyrus digest         # generate activity digest"
echo "    gyrus update         # update to latest version"
echo "    gyrus help           # show all commands"
echo ""
