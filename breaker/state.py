"""
The per-turn counters, on disk.

This is code and not a note because a Claude Code hook is a fresh process on
every tool call. Nothing survives between calls except what got written down,
so "how many calls into this turn are we" only exists if a file says so.

State lives in ~/.claude/state/breaker/<session>.json -- outside the repo on
purpose, because the repo is the thing being guarded and a guard that dirties
the working tree gets committed by accident. Set BREAKER_STATE_DIR to move it.

Every function here swallows its own errors and returns something usable. A
broken breaker must never break the session: the worst a failed write may do is
lose the count, which means a missed trip, which is the same as not having the
tool installed. The worst a raised exception may do is kill a working session,
which is much worse than a missed trip.
"""
import json
import os
import time

STALE_DAYS = 7


def state_dir():
    return os.environ.get(
        "BREAKER_STATE_DIR", os.path.expanduser("~/.claude/state/breaker")
    )


def fresh(cfg):
    return {
        "calls": 0,
        "files": {},
        "next_trip": int(cfg.get("spin_first", 60)),
        "fired": [],
        "started": time.time(),
    }


def path_for(session_id):
    """Sanitize whatever the harness hands us into a safe filename.

    The session id arrives from outside this process. It is dropped straight
    into a path, so anything that is not alphanumeric, dash or underscore is
    thrown away -- that kills `../` traversal and shell-shaped names in one
    line rather than trying to detect them.
    """
    raw = session_id or ""
    safe = "".join(c for c in str(raw) if c.isalnum() or c in "-_")
    return os.path.join(state_dir(), "%s.json" % (safe[:64] or "nosession"))


def load(path, cfg):
    try:
        with open(path) as f:
            saved = json.load(f)
    except Exception:
        return fresh(cfg)
    base = fresh(cfg)
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in base:
                base[k] = v
    if not isinstance(base.get("files"), dict):
        base["files"] = {}
    if not isinstance(base.get("fired"), list):
        base["fired"] = []
    return base


def save(path, state):
    """Atomic write: tmp file in the same directory, then replace.

    Same directory matters -- os.replace is only atomic within one filesystem,
    and /tmp is often a different one. If any of it fails we clean up the tmp
    file and carry on counting from a stale number.
    """
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def sweep(days=STALE_DAYS):
    """Drop state files older than a week. Runs on --reset, so it costs one
    listdir per human message and never runs in the hot path."""
    cutoff = time.time() - days * 86400
    d = state_dir()
    try:
        names = os.listdir(d)
    except Exception:
        return 0
    dropped = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
                dropped += 1
        except Exception:
            pass
    return dropped


def latest():
    """The most recently touched session file.

    `breaker report` is typed by a human in a terminal, with no hook payload to
    read a session id out of, so it defaults to whichever session moved last.
    """
    d = state_dir()
    try:
        files = [
            os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json")
        ]
    except Exception:
        return None
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except Exception:
        return None
