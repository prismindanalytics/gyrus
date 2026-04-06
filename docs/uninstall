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

# Check if installed
if [ ! -d "$GYRUS_DIR" ] && ! crontab -l 2>/dev/null | grep -q "ingest.py"; then
  echo "Gyrus doesn't appear to be installed."
  exit 0
fi

# Show what will be removed
echo -e "This will remove:"
[ -d "$GYRUS_DIR" ] && echo -e "  • ${BOLD}$GYRUS_DIR${NC} (knowledge base, config, scripts)"
crontab -l 2>/dev/null | grep -q "ingest.py" && echo -e "  • Cron job (scheduled sync)"
[ -f "$HOME/.claude/commands/gyrus.md" ] && echo -e "  • Claude Code /gyrus skill"
echo ""

# Ask to backup
if [ -d "$GYRUS_DIR/projects" ]; then
  PAGE_COUNT=$(ls "$GYRUS_DIR/projects/"*.md 2>/dev/null | wc -l | tr -d ' ' || echo "0")
  if [ "$PAGE_COUNT" -gt 0 ]; then
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

# Remove cron job
if crontab -l 2>/dev/null | grep -q "ingest.py"; then
  crontab -l 2>/dev/null | grep -v "ingest.py" | crontab - 2>/dev/null || true
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

# Remove symlink if exists
if [ -L "$HOME/.gyrus" ]; then
  REAL_DIR=$(readlink "$HOME/.gyrus")
  rm -f "$HOME/.gyrus"
  echo -e "  ${GREEN}✓${NC} Removed symlink ~/.gyrus -> $REAL_DIR"
  echo -e "  ${DIM}Note: The actual directory at $REAL_DIR was NOT removed.${NC}"
elif [ -d "$GYRUS_DIR" ]; then
  rm -rf "$GYRUS_DIR"
  echo -e "  ${GREEN}✓${NC} Removed $GYRUS_DIR"
fi

echo ""
echo -e "${GREEN}Gyrus has been uninstalled.${NC}"
echo ""
