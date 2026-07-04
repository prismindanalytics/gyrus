#!/bin/bash
# Gyrus Uninstaller
# Usage: curl -fsSL https://gyrus.sh/uninstall | bash

set -euo pipefail

GYRUS_DIR="$HOME/.gyrus"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

echo ""
echo -e "${BOLD}Gyrus Uninstaller${NC}"
echo "=================="
echo ""

# Identify OUR cron entry precisely — the marker comment on new installs, or a
# gyrus-path + ingest.py line on older ones — so we never touch an unrelated
# user cron job that merely runs some other ingest.py.
GYRUS_CRON_RE='# gyrus-autosync|(\.gyrus|gyrus-local)[^#]*ingest\.py'
has_gyrus_cron() { crontab -l 2>/dev/null | grep -qE "$GYRUS_CRON_RE"; }

# Check if installed
if [ ! -d "$GYRUS_DIR" ] && ! has_gyrus_cron; then
  echo "Gyrus doesn't appear to be installed."
  exit 0
fi

# Show what will be removed
echo -e "This will remove:"
[ -d "$GYRUS_DIR" ] && echo -e "  • ${BOLD}$GYRUS_DIR${NC} (knowledge base, config, scripts)"
has_gyrus_cron && echo -e "  • Cron job (scheduled sync)"
[ -f "$HOME/.claude/commands/gyrus.md" ] && echo -e "  • Claude Code /gyrus skill"
echo ""

# Ask to backup. Guard the glob: with no *.md files and pipefail set, an
# unguarded `ls *.md` fails and the trailing `|| echo 0` fires in addition to
# the pipeline output, yielding a non-integer count ("0\n0").
if [ -d "$GYRUS_DIR/projects" ]; then
  PAGE_COUNT=$({ ls "$GYRUS_DIR/projects/"*.md 2>/dev/null || true; } | wc -l | tr -d ' ')
  if [ "${PAGE_COUNT:-0}" -gt 0 ]; then
    echo -e "${YELLOW}!${NC} You have ${BOLD}$PAGE_COUNT project pages${NC} in $GYRUS_DIR/projects/"
    echo -e "  ${DIM}Back them up before uninstalling if you want to keep them.${NC}"
    echo ""
  fi
fi

read -r -p "Uninstall Gyrus? [y/N]: " CONFIRM < /dev/tty
if [[ ! "$CONFIRM" =~ ^[Yy] ]]; then
  echo "Cancelled."
  exit 0
fi

echo ""

# Remove cron job (only our marked/gyrus-path line)
if has_gyrus_cron; then
  crontab -l 2>/dev/null | grep -vE "$GYRUS_CRON_RE" | crontab - 2>/dev/null || true
  echo -e "  ${GREEN}✓${NC} Removed cron job"
fi

# Remove Windows scheduled task (if on WSL/Git Bash)
if command -v schtasks.exe &>/dev/null; then
  schtasks.exe /Delete /TN "Gyrus" /F 2>/dev/null && echo -e "  ${GREEN}✓${NC} Removed scheduled task" || true
fi

# Remove Claude Code skill
if [ -f "$HOME/.claude/commands/gyrus.md" ]; then
  rm -f "$HOME/.claude/commands/gyrus.md"
  echo -e "  ${GREEN}✓${NC} Removed Claude Code /gyrus skill"
fi

# Remove Codex skill
if [ -f "$GYRUS_DIR/skills/codex/gyrus-instructions.md" ]; then
  echo -e "  ${GREEN}✓${NC} Codex instructions will be removed with ~/.gyrus"
fi

# Remove the "# Gyrus Knowledge Base" instruction block from global agent
# config files so they stop pointing tools at a deleted knowledge base.
# Handles both the marker-wrapped block (new installs) and the older unmarked
# block (stops at its known last line).
remove_gyrus_block() {
  local file="$1" tail_line="$2"
  [ -f "$file" ] || return 0
  grep -q "# Gyrus Knowledge Base" "$file" 2>/dev/null || return 0
  awk -v tail="$tail_line" '
    /# Gyrus Knowledge Base/ { skip=1 }
    skip {
      if ($0 ~ /<!-- END GYRUS -->/ || index($0, tail) > 0) { skip=0 }
      next
    }
    { print }
  ' "$file" > "$file.gyrus-tmp" && mv "$file.gyrus-tmp" "$file"
  echo -e "  ${GREEN}✓${NC} Removed Gyrus block from $(basename "$file")"
}
remove_gyrus_block "$HOME/.claude/CLAUDE.md" "Use /gyrus for the full skill"
remove_gyrus_block "$HOME/AGENTS.md" "For full instructions:"

# Remove the Cowork skills-plugin gyrus skill (best-effort; session-scoped).
COWORK_PLUGIN="$HOME/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin"
if [ -d "$COWORK_PLUGIN" ]; then
  find "$COWORK_PLUGIN" -type d -name gyrus -path '*/skills/gyrus' -prune \
    -exec rm -rf {} + 2>/dev/null || true
fi

# Remove symlink if exists, then offer to remove the target too
if [ -L "$HOME/.gyrus" ]; then
  REAL_DIR=$(readlink "$HOME/.gyrus")
  # Resolve relative symlinks
  case "$REAL_DIR" in
    /*) ;;
    *) REAL_DIR="$HOME/$REAL_DIR" ;;
  esac
  rm -f "$HOME/.gyrus"
  echo -e "  ${GREEN}✓${NC} Removed symlink ~/.gyrus -> $REAL_DIR"

  if [ -d "$REAL_DIR" ]; then
    echo ""
    read -r -p "  Also remove the actual data directory $REAL_DIR? [y/N]: " REMOVE_TARGET < /dev/tty
    if [[ "${REMOVE_TARGET:-n}" =~ ^[Yy] ]]; then
      rm -rf "$REAL_DIR"
      echo -e "  ${GREEN}✓${NC} Removed $REAL_DIR"
    else
      echo -e "  ${DIM}Kept $REAL_DIR (remove manually if you change your mind).${NC}"
    fi
  fi
elif [ -d "$GYRUS_DIR" ]; then
  rm -rf "$GYRUS_DIR"
  echo -e "  ${GREEN}✓${NC} Removed $GYRUS_DIR"
fi

# Remove the `gyrus` shell wrapper
if [ -f "$HOME/.local/bin/gyrus" ]; then
  rm -f "$HOME/.local/bin/gyrus"
  echo -e "  ${GREEN}✓${NC} Removed ~/.local/bin/gyrus"
fi

# Remove the `gyrus.cmd` Windows wrapper (if running under WSL/Git Bash)
if [ -f "$HOME/.local/bin/gyrus.cmd" ]; then
  rm -f "$HOME/.local/bin/gyrus.cmd"
  echo -e "  ${GREEN}✓${NC} Removed ~/.local/bin/gyrus.cmd"
fi

echo ""
echo -e "${GREEN}Gyrus has been uninstalled.${NC}"
echo -e "${DIM}Your GitHub knowledge-base repo (if any) was NOT touched.${NC}"
echo ""
