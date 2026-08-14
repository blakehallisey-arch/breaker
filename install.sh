#!/usr/bin/env bash
# Wire breaker into ~/.claude/settings.json.
#
# Two entries: PostToolUse runs the hook, UserPromptSubmit runs it with --reset.
# The merge is done in python, not sed, because settings.json belongs to the
# user and probably already has hooks in it. A regex edit of someone else's
# JSON is how you delete their config; json.load and json.dump is how you do
# not. Idempotent -- run it twice and the second run changes nothing.
#
# It backs up settings.json first and prints exactly what it changed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/breaker/hook.py"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

if [ ! -f "$HOOK" ]; then
  echo "install: cannot find $HOOK" >&2
  exit 1
fi

chmod +x "$HOOK" 2>/dev/null || true
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

BACKUP="$SETTINGS.breaker-backup.$(date +%Y%m%d%H%M%S)"
cp "$SETTINGS" "$BACKUP"

PYTHON="${PYTHON:-python3}"
set +e
"$PYTHON" - "$SETTINGS" "$HOOK" "$BACKUP" "$PYTHON" <<'PY'
import json, sys

settings_path, hook, backup, python = sys.argv[1:5]

# call the interpreter explicitly rather than relying on the shebang and the
# exec bit -- a checkout from a zip loses the exec bit and the hook then fails
# silently, which for this tool means never guarding anything again.
run = "%s %s" % (python, hook)

try:
    with open(settings_path) as f:
        settings = json.load(f)
    if not isinstance(settings, dict):
        raise ValueError("settings.json is not an object")
except ValueError as e:
    # Refuse rather than guess. Overwriting an unreadable settings.json would
    # silently throw away every hook the user already has.
    print("install: %s is not valid JSON (%s). Nothing changed." % (settings_path, e))
    print("install: your backup is at %s" % backup)
    sys.exit(1)

wanted = [
    ("PostToolUse", run, "count every tool call, trip the breaker"),
    ("UserPromptSubmit", run + " --reset", "a human spoke, zero the counters"),
]

hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    print("install: settings.json has a 'hooks' key that is not an object. Nothing changed.")
    sys.exit(1)

changed = []
for event, command, why in wanted:
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        print("install: hooks.%s is not a list. Nothing changed for it." % event)
        continue
    already = False
    for g in groups:
        for h in (g or {}).get("hooks", []) if isinstance(g, dict) else []:
            if isinstance(h, dict) and h.get("command") == command:
                already = True
    if already:
        print("  unchanged  %-16s already runs breaker" % event)
        continue
    entry = {"hooks": [{"type": "command", "command": command}]}
    if event == "PostToolUse":
        entry["matcher"] = "*"
    groups.append(entry)
    changed.append("%s -> %s" % (event, command))
    print("  added      %-16s %s" % (event, why))

if not changed:
    print("install: nothing to do, breaker was already wired.")
    sys.exit(3)   # 3 tells the shell to drop the backup it just took

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("install: wrote %s" % settings_path)
print("install: backup at %s" % backup)
PY
rc=$?
set -e

if [ "$rc" = "3" ]; then
  rm -f "$BACKUP"
  exit 0
fi
if [ "$rc" != "0" ]; then
  exit "$rc"
fi

echo
echo "breaker is wired. Restart Claude Code, then check it with:"
echo "  $PYTHON $HOOK report"
echo
echo "To remove it, delete the two entries from $SETTINGS, or restore $BACKUP."
