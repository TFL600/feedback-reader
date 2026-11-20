#!/usr/bin/env bash
# Install the apply-feedback skill into Claude Code, so an agent can apply your feedback rounds.
#
#   ./install.sh
#
# Copies skills/apply-feedback to ~/.claude/skills/apply-feedback, with this clone's path filled in.
# That is all it does: it asks first, changes nothing else, and needs no sudo.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/skills/apply-feedback/SKILL.md"
DEST="$HOME/.claude/skills/apply-feedback"

cat <<EOF
This adds a skill to Claude Code. Skills are instructions Claude follows in every session on this
machine, so only install skills you have read. This one is $SRC.

  copies to:  $DEST/SKILL.md
  fills in:   FR_READER=$HERE
EOF
[ -e "$DEST" ] && echo "  replaces:   the apply-feedback skill already installed there"
printf "Install it? [y/N] "
read -r answer
case "$answer" in
	y | Y | yes | YES) ;;
	*) echo "Nothing installed."; exit 0 ;;
esac

mkdir -p "$DEST"
sed "s|\$FR_READER|$HERE|g" "$SRC" > "$DEST/SKILL.md"
echo "Installed. In Claude Code, paste the command that Finish round copies (/apply-feedback <round>)."
