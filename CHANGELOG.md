# Changelog

## 0.1.0 — first cut

Generalized out of a private prototype that had been running as a single
one-user hook.

- Four trips: SPIN, SPREAD, LOOP, NEW FILE.
- `--reset` on UserPromptSubmit makes "since the human's last message" real.
- `.breaker.json` config, so the thresholds and the new-file rules are not
  hardcoded to one person's repo layout.
- `registry_files` replaces a hardcoded path to one project's build manifest.
- `root_only` and `ignore_paths` added — the prototype counted everything,
  including `node_modules`, which is how it learned it needed them.
- `breaker report` and `report --json`.
- `install.sh` merges the two hook entries into `~/.claude/settings.json`
  in python, backs it up, and is idempotent.
- Tests on every trip, every once-per-turn guard, and every fail-open path.
