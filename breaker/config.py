"""
Where the dials live.

This is code and not a note because the numbers have to be readable by the hook
on every single tool call, and because a project that writes forty files per
turn on purpose needs to be able to say so once, in a file, instead of arguing
with the breaker every day until someone uninstalls it.

Config is a JSON file named `.breaker.json` at the project root. JSON, not TOML,
because tomllib landed in Python 3.11 and this has to run on 3.9 with nothing
installed. Missing file means defaults. A malformed file means defaults too --
a broken config must never break the session, and a breaker that refuses to
start is a breaker that is not guarding anything.
"""
import json
import os

DEFAULTS = {
    # master switch. set false to leave the hook wired but silent.
    "enabled": True,
    # SPIN: tool calls in one turn before the first check-in,
    # then again every this many after that.
    "spin_first": 60,
    "spin_recheck": 45,
    # SPREAD: distinct files written or edited in one turn.
    "file_spread": 14,
    # LOOP: times one single file gets rewritten in one turn.
    "same_file_loop": 10,
    # NEW FILE: which extensions count as "a new source file".
    "new_file_extensions": [".py", ".js", ".ts", ".html", ".go", ".rs"],
    # NEW FILE: files that are registries -- a write to one of these means the
    # agent is adding an entry to a list of things that exist. Globs or bare
    # basenames both work. Empty by default because every project names its
    # registry something different.
    "registry_files": [],
    # NEW FILE: only trip for a file created at the project root. A new file
    # deep inside an existing package is usually a normal part of the work; a
    # new file dropped at the root is usually a new thing being born.
    "root_only": True,
    # never counted, in any trip.
    "ignore_paths": [
        "**/node_modules/**",
        "**/.git/**",
        "**/dist/**",
        "**/build/**",
        "**/*.lock",
    ],
}

CONFIG_NAME = ".breaker.json"


def find_root(start):
    """Walk up for the project root.

    A `.breaker.json` wins over a `.git`, so a subproject can carry its own
    dials without being dragged up to the monorepo root.
    """
    try:
        cur = os.path.realpath(start or os.getcwd())
    except Exception:
        return os.getcwd()
    if not os.path.isdir(cur):
        cur = os.path.dirname(cur)
    seen = cur
    while True:
        if os.path.isfile(os.path.join(cur, CONFIG_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    cur = seen
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return seen
        cur = parent


def load(cwd=None):
    """Return (config dict, project root). Never raises."""
    root = find_root(cwd)
    cfg = dict(DEFAULTS)
    # BREAKER_CONFIG points straight at a config file. Used by the tests, and
    # useful if you want one set of dials across several checkouts.
    override = os.environ.get("BREAKER_CONFIG")
    path = override or os.path.join(root, CONFIG_NAME)
    try:
        with open(path) as f:
            user = json.load(f)
        if isinstance(user, dict):
            for k, v in user.items():
                if k in DEFAULTS:
                    cfg[k] = v
    except Exception:
        pass
    return cfg, root
