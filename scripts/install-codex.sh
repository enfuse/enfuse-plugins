#!/bin/sh
# Add the enfuse.ai tools and skills to OpenAI Codex CLI the documented way:
#   codex mcp add (MCP server in ~/.codex/config.toml) and skills in ~/.agents/skills.
# Run from a clone of this repository:  sh scripts/install-codex.sh
# Installs nothing system-wide, needs no sudo, and changes only those two places.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
plugin="$here/plugins/enfuse"
command -v codex >/dev/null 2>&1 || { echo "codex is not installed (https://developers.openai.com/codex)"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }
python3 "$plugin/server/run.py" --version >/dev/null

if codex mcp list 2>/dev/null | grep -Eq '^enfuse([[:space:]]|$)'; then
  echo "Replacing the existing enfuse MCP server entry"
  codex mcp remove enfuse >/dev/null
fi
codex mcp add enfuse -- python3 "$plugin/server/run.py" serve

skills="${HOME}/.agents/skills"
mkdir -p "$skills"
for skill in connect status visit steer; do
  target="$skills/enfuse-$skill"
  mkdir -p "$target"
  # Codex lists skills by name, so each gets an enfuse- prefix; the plugin root becomes this clone's path.
  sed -e "s/^name: $skill\$/name: enfuse-$skill/" -e "s|\${CLAUDE_PLUGIN_ROOT}|$plugin|g" \
    "$plugin/skills/$skill/SKILL.md" > "$target/SKILL.md"
done
echo "Done. Start a new codex session, then ask it to run \$enfuse-connect (or say: connect this machine to enfuse.ai)."
