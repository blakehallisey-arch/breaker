#!/usr/bin/env python3
"""
The breaker itself: count what the agent is doing since the human last spoke,
and interrupt it when the work stops looking like the ask.

Two hook wirings, one script:
  --reset      UserPromptSubmit -> a human just spoke, counters go to zero
  (no args)    PostToolUse      -> count this call, maybe trip

Four trips. The first three measure VOLUME since the last human message:
  SPIN      too many tool calls on one instruction
  SPREAD    too many distinct files written in one turn
  LOOP      the same file rewritten over and over

The fourth measures EXISTENCE, which is a different question:
  NEW FILE  a Write that creates a brand new source file at the project root,
            or any write to a file the project has declared a registry.

NEW FILE fires on the first occurrence, not on a threshold, and that is the
whole point. The other three can only tell you that a lot of work is happening.
None of them can tell you the work should not exist. By the time a count would
have caught an over-build, the over-build is already on disk.

This is code and not a note in a prompt because a prompt is a request. A hook
that exits 2 is a stop the model cannot talk itself out of.

Tripping writes to stderr and exits 2, which Claude Code puts in front of the
model as a hard interrupt. Each reason fires at most once per turn, and after a
trip the call counter re-arms further out, so a genuinely long job checks in a
few times instead of nagging every other call.
"""
import fnmatch
import json
import os
import sys

try:
    from . import config, state
except ImportError:  # run as a bare script path, which is how hooks are wired
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from breaker import config, state

VERSION = "0.1.0"
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

USAGE = """breaker %s -- a circuit breaker for a coding-agent session.

usage:
  python3 -m breaker              read a PostToolUse payload on stdin, count it,
                                  and exit 2 if the turn has tripped a breaker
  python3 -m breaker --reset      read a UserPromptSubmit payload on stdin and
                                  zero the counters for that session
  python3 -m breaker report       print this turn's counts
  python3 -m breaker report --json
  python3 -m breaker --help

options:
  --session ID   report on a specific session instead of the most recent one

exit codes:
  0  fine, carry on
  1  the CLI was used wrong
  2  a breaker tripped -- the message on stderr is for the model

config:   .breaker.json at the project root (see README)
state:    ~/.claude/state/breaker/<session>.json, or $BREAKER_STATE_DIR
network:  none. this tool never opens a socket.
""" % VERSION


# --- counting ---------------------------------------------------------------


def read_payload():
    """Whatever is on stdin, or {} if it is missing or malformed.

    A malformed payload is not an error worth stopping a session over. It means
    this call goes uncounted, which is the same as not being installed.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def written_path(data):
    """The file this tool call wrote, or "" if it did not write one."""
    if data.get("tool_name", "") not in WRITE_TOOLS:
        return ""
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    p = ti.get("file_path") or ti.get("notebook_path") or ""
    return p if isinstance(p, str) else ""


def relative(path, root):
    """Path relative to the project root, or None if it is outside it.

    Both sides get realpath'd first. On macOS the temp dir and /var are
    symlinks, so a raw relpath between a resolved root and an unresolved file
    comes back as a pile of `..` and every relative glob silently stops
    matching. That bug is invisible until a config pattern quietly does nothing.
    """
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except Exception:
        return None
    return None if rel.startswith("..") else rel


def ignored(path, root, patterns):
    """Match the pattern against both the absolute path and the repo-relative
    one, because people write `**/dist/**` thinking in relative terms and paste
    absolute paths into their config in the same afternoon."""
    if not path:
        return True
    forms = [path, os.path.realpath(path)]
    rel = relative(path, root)
    if rel:
        forms.append(rel)
        forms.append("./" + rel)
    for pat in patterns or []:
        for form in forms:
            if fnmatch.fnmatch(form, pat):
                return True
    return False


def is_registry(path, root, patterns):
    """A registry is any file the project says is a list of things that exist.
    Matched on the full path, the relative path, or the bare basename -- a bare
    name in the config should just work."""
    if not patterns:
        return None
    base = os.path.basename(path)
    forms = [path, os.path.realpath(path), base]
    rel = relative(path, root)
    if rel:
        forms.append(rel)
    for pat in patterns:
        if pat == base:
            return pat
        for form in forms:
            if fnmatch.fnmatch(form, pat):
                return pat
    return None


def new_file_reason(tool, path, root, cfg):
    """Is this call bringing a new thing into existence? A file fact.

    Two shapes, both cheap, neither requiring an opinion about what the human
    meant. A Write -- not an Edit, an Edit needs the file to already exist --
    that drops a source file straight at the project root; or any write to a
    declared registry. Those are the two things that are true at the moment a
    new build is born.
    """
    if not path:
        return None

    reg = is_registry(path, root, cfg.get("registry_files") or [])
    if reg:
        return "an entry in the registry %s" % (relative(path, root) or path)

    if tool != "Write":
        return None
    exts = tuple(cfg.get("new_file_extensions") or [])
    if not exts or not path.endswith(exts):
        return None
    if os.path.exists(path):
        # a Write over a file that is already there is a rewrite, not a birth.
        return None
    if cfg.get("root_only", True):
        try:
            if os.path.realpath(os.path.dirname(path)) != os.path.realpath(root):
                return None
        except Exception:
            return None
    return "a new file at the project root: %s" % os.path.basename(path)


# --- the interrupts ---------------------------------------------------------

VOLUME_TRIP = """BREAKER -- stop and look up.

{headline}

Do not run another tool until you have answered these, in your head:
  1. What did the user actually ask for, in their words?
  2. What are you doing right now?
  3. Is 2 still inside 1, or did the job quietly get bigger?
  4. Is this approach working, or are you going in circles?

Then say ONE of these in a sentence before you continue:
  CONTINUE -- still on the ask; here is what is left.
  PIVOT    -- this is not working; here is the different approach.
  TRIM     -- I grew the job past the ask; cutting back to this.
  STOP     -- the ask is done or blocked; report and hand back.

Make the call yourself. Only hand back to the user if you genuinely cannot
tell what they wanted.
"""

NEW_FILE_TRIP = """BREAKER -- you are creating something new.

This call creates {what}.

The other breakers ask whether you are doing too much. This one asks whether
this should exist. Answer these three in writing before you write anything else:
  1. Which existing file already owns this DATA? Name it, or say none.
  2. Which existing file already owns this DECISION? Name it, or say none.
  3. If either exists, why is folding into it the wrong move?

Then say ONE of these:
  FOLD  -- an owner exists; I am putting it there instead.
  BUILD -- nothing owns this, and here is the one line saying why.
  STOP  -- the user asked a question, not for a build. Answer the question.

Question 2 is the one that matters. A duplicate is almost never caught by
noticing the new file is bad; it is caught by naming the file that already
made this decision and reading what it decided.

Make the call yourself. Do not ask the user for permission to keep going.
"""


def trip_volume(kind, headline, st, path, cfg):
    st["fired"].append(kind)
    st["next_trip"] = st["calls"] + int(cfg.get("spin_recheck", 45))
    state.save(path, st)
    sys.stderr.write(VOLUME_TRIP.format(headline=headline))
    sys.exit(2)


def trip_new_file(what, st, path):
    st["fired"].append("newfile")
    state.save(path, st)
    sys.stderr.write(NEW_FILE_TRIP.format(what=what))
    sys.exit(2)


# --- the report -------------------------------------------------------------


def build_report(cfg, sess=None):
    if sess:
        p = state.path_for(sess)
    else:
        p = state.latest()
    if not p or not os.path.exists(p):
        return {
            "session": sess or None,
            "state_file": p,
            "found": False,
            "calls": 0,
            "files_touched": 0,
            "next_check_in": int(cfg.get("spin_first", 60)),
            "most_rewritten": None,
            "most_rewritten_count": 0,
            "fired": [],
        }
    st = state.load(p, cfg)
    files = st.get("files") or {}
    worst = max(files.items(), key=lambda kv: kv[1], default=(None, 0))
    return {
        "session": os.path.basename(p)[:-5],
        "state_file": p,
        "found": True,
        "calls": st.get("calls", 0),
        "files_touched": len(files),
        "next_check_in": st.get("next_trip"),
        "most_rewritten": worst[0],
        "most_rewritten_count": worst[1],
        "fired": st.get("fired") or [],
    }


def print_report(rep):
    if not rep["found"]:
        print("breaker -- no state for this session yet.")
        print("nothing has been counted since the last human message.")
        return
    print("breaker -- this turn")
    print("  session        %s" % rep["session"])
    print("  tool calls     %s (next check-in at %s)"
          % (rep["calls"], rep["next_check_in"]))
    print("  files touched  %s" % rep["files_touched"])
    if rep["most_rewritten"]:
        print("  most rewritten %s (%sx)"
              % (os.path.basename(rep["most_rewritten"]),
                 rep["most_rewritten_count"]))
    print("  tripped        %s"
          % (", ".join(rep["fired"]) if rep["fired"] else "nothing yet"))


# --- entry point ------------------------------------------------------------


def run(argv):
    args = list(argv)

    if "-h" in args or "--help" in args:
        sys.stdout.write(USAGE)
        return 0
    if "--version" in args:
        print(VERSION)
        return 0

    sess = None
    if "--session" in args:
        i = args.index("--session")
        if i + 1 >= len(args):
            sys.stderr.write("breaker: --session needs a value\n")
            return 1
        sess = args[i + 1]
        del args[i:i + 2]

    if args and args[0] == "report":
        cfg, _root = config.load(os.getcwd())
        rep = build_report(cfg, sess)
        if "--json" in args:
            print(json.dumps(rep, indent=2))
        else:
            print_report(rep)
        return 0

    data = read_payload()
    cwd = data.get("cwd") or os.getcwd()
    cfg, root = config.load(cwd)
    p = state.path_for(sess or data.get("session_id"))

    if "--reset" in args:
        # a human just spoke. this is what makes "since the last message" real.
        state.save(p, state.fresh(cfg))
        state.sweep()
        return 0

    if not cfg.get("enabled", True):
        return 0

    st = state.load(p, cfg)
    st["calls"] = st.get("calls", 0) + 1

    tool = data.get("tool_name", "")
    f = written_path(data)
    if f and not ignored(f, root, cfg.get("ignore_paths")):
        st["files"][f] = st["files"].get(f, 0) + 1
        # checked first, and on the very first write rather than the tenth --
        # this trip is about whether the work should start at all.
        if "newfile" not in st["fired"]:
            what = new_file_reason(tool, f, root, cfg)
            if what:
                trip_new_file(what, st, p)

    files = st["files"]
    fired = st["fired"]

    # LOOP -- the same file, over and over. Usually the approach is wrong, not
    # the next edit.
    if "loop" not in fired:
        worst = max(files.items(), key=lambda kv: kv[1], default=(None, 0))
        if worst[1] >= int(cfg.get("same_file_loop", 10)):
            trip_volume(
                "loop",
                "You have rewritten the same file %d times this turn: %s.\n"
                "That usually means the approach is wrong, not that the next\n"
                "edit is the one." % (worst[1], os.path.basename(worst[0])),
                st, p, cfg)

    # SPREAD -- the job got wider than the ask.
    if "spread" not in fired and len(files) >= int(cfg.get("file_spread", 14)):
        trip_volume(
            "spread",
            "You have written to %d different files since the last message\n"
            "from the user. That is a lot of ground for one ask." % len(files),
            st, p, cfg)

    # SPIN -- sheer volume on one instruction.
    if st["calls"] >= int(st.get("next_trip") or cfg.get("spin_first", 60)):
        trip_volume(
            "spin",
            "You are %d tool calls into a single instruction from the user."
            % st["calls"],
            st, p, cfg)

    state.save(p, st)
    return 0


def main():
    try:
        code = run(sys.argv[1:])
    except SystemExit:
        raise
    except Exception:
        # A breaker that crashes must not take the session with it. Exiting 0
        # is deliberate here and not an oversight: this hook's job is to STOP
        # work, so failing open costs a missed trip, while failing closed would
        # kill a session over a bug in a counter.
        sys.exit(0)
    sys.exit(code)


if __name__ == "__main__":
    main()
