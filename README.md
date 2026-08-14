# breaker

A circuit breaker for a coding-agent session. It counts what the agent has done
since your last message and interrupts it when the work stops looking like the
ask.

## The problem

Someone asked a read question: show me the logic, and the top items I should
work on. The session answered it by writing a new ranking script, installing a
scheduled job to run that script five times a day, wiring it into a second
tool, and merging two pull requests. None of that was asked for. The ranker it
had just duplicated already existed — and the docstring at the top of the
existing one recorded that the same hypotheses had been tested and were dead.
Nothing stopped any of it. The permission prompts all said yes because every
individual write was reasonable; the linters passed because the code was fine.

Every guard in that setup measured how *much* work was happening. Not one of
them could ask whether the work should exist at all. That is why breaker's
fourth trip fires on a file fact — a Write that creates a new source file at
the project root — and fires on the first one, not the tenth. By the time a
threshold would have caught an over-build, the over-build is already on disk.

## Install

```
git clone https://github.com/blakehallisey-arch/breaker && cd breaker
./install.sh
```

That merges two entries into `~/.claude/settings.json` (PostToolUse runs the
hook, UserPromptSubmit runs it with `--reset`) without touching hooks you
already have. It backs the file up first and prints exactly what it changed.
Run it twice and the second run changes nothing.

## What it looks like

Real output. Six PostToolUse payloads piped into the hook in a scratch project
with `spin_first` set to 6, so the transcript fits on a page — the default is 60.

```
$ for i in 1 2 3 4 5; do echo "$PAYLOAD" | python3 -m breaker; echo "call $i -> exit $?"; done
call 1 -> exit 0
call 2 -> exit 0
call 3 -> exit 0
call 4 -> exit 0
call 5 -> exit 0

$ echo "$PAYLOAD" | python3 -m breaker; echo "exit $?"
BREAKER -- stop and look up.

You are 6 tool calls into a single instruction from the user.

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
exit 2
```

The NEW FILE trip asks a different set of questions, because it is a different
question:

```
$ echo "$WRITE_PAYLOAD" | python3 -m breaker; echo "exit $?"
BREAKER -- you are creating something new.

This call creates a new file at the project root: rank_items.py.

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
exit 2
```

And the report, so you can see where a turn stands without waiting for a trip:

```
$ python3 -m breaker report
breaker -- this turn
  session        demo
  tool calls     6 (next check-in at 51)
  files touched  1
  most rewritten rank.py (6x)
  tripped        spin
```

`--json` prints the same thing machine-readable.

## How it works

It is a Claude Code PostToolUse hook. Every tool call the agent makes, the
harness pipes a small JSON payload to the hook on stdin and the hook adds one
to a counter. A UserPromptSubmit hook runs the same script with `--reset`,
which zeroes the counters. That reset is the whole reason "since the human's
last message" means anything.

Four trips:

| trip | fires when | default |
|---|---|---|
| SPIN | tool calls in one turn | 60, then every 45 |
| SPREAD | distinct files written in one turn | 14 |
| LOOP | one file rewritten over and over | 10 |
| NEW FILE | a Write creating a new source file at the project root, or any write to a declared registry | first occurrence |

On a trip the hook writes the message to stderr and exits 2. Claude Code reads
exit 2 on a hook as a hard interrupt and puts the stderr text in front of the
model, so the agent has to stop and answer before it can run another tool.

Each reason fires at most once per turn, so it does not nag. After a trip, SPIN
re-arms further out (`spin_recheck`), which turns a long legitimate job into a
few check-ins instead of a wall.

NEW FILE is checked first and only on a `Write`, never an `Edit` — an Edit needs
the file to already exist, so it cannot be a birth. A Write over a file that is
already on disk is a rewrite, not a birth, and does not trip either.

State lives in `~/.claude/state/breaker/<session>.json`, one file per session,
written to a temp file and renamed so a half-written file is never read. Files
older than seven days are swept on reset. Set `BREAKER_STATE_DIR` to move it.
Nothing is ever written inside your repo.

It fails open, on purpose and everywhere. Malformed stdin, an unreadable config,
an unwritable state directory, a bug in the counter: all of them exit 0 and stay
quiet. This hook's job is to *stop* work, so failing open costs a missed trip,
while failing closed would kill a working session over a broken counter. That
is the opposite of the right call for a deny-hook, and it is the right call here.

No network. No telemetry. No account. It reads stdin, reads one config file,
writes one state file, and prints to stderr.

## Configuration

`.breaker.json` at the project root. Missing or malformed means defaults.
A full example is in `examples/.breaker.json`.

| key | default | what it does |
|---|---|---|
| `enabled` | `true` | master switch; false leaves it wired and silent |
| `spin_first` | `60` | tool calls in one turn before the first SPIN check-in |
| `spin_recheck` | `45` | calls between check-ins after a trip |
| `file_spread` | `14` | distinct files written in one turn before SPREAD |
| `same_file_loop` | `10` | rewrites of one file before LOOP |
| `new_file_extensions` | `[".py",".js",".ts",".html",".go",".rs"]` | which extensions count as a new source file |
| `registry_files` | `[]` | files that are lists of things that exist; a write to one is a NEW FILE trip. Globs or bare basenames |
| `root_only` | `true` | only trip NEW FILE at the project root. A new file deep in an existing package is usually normal work |
| `ignore_paths` | `["**/node_modules/**","**/.git/**","**/dist/**","**/build/**","**/*.lock"]` | never counted by any trip |

The project root is the nearest directory above the working directory holding a
`.breaker.json`, or failing that a `.git`. `BREAKER_CONFIG` points at a config
file directly if you want one set of dials across several checkouts.

Two knobs worth thinking about. `registry_files` is empty by default because
every project names its registry something different — if you have a file that
lists the things that exist (a builds manifest, a plugin index, a routing
table), put it here; adding a row to it is the other moment a new thing is
born. And `root_only` is the difference between a trip you keep and a trip you
uninstall: with it off, every new test file and every new module fires the
breaker.

## What this is not

It does not know whether the work is good. It only knows the shape of the turn:
how many calls, how many files, how many times the same file. A perfectly
correct refactor across fifteen files trips SPREAD, and a session going in
circles under the thresholds trips nothing.

It cannot see work done inside a subagent. A subagent runs its own tool calls in
its own context, and the hook only sees the calls made in the session it is
wired into. If the agent delegates the over-build, breaker counts one tool call.

It counts tool calls, not tokens and not dollars. A turn that trips SPIN might
be cheap and a turn that never trips might be very expensive. If you want a
spend lid, that is a different tool.

It does not block anything. Exit 2 is an interrupt the model must answer, not a
permission denial. An agent that decides CONTINUE continues. It is a stop sign,
not a wall — for a wall you want a PreToolUse deny.

It is not battle-tested. The prototype it came from ran in one person's private
workshop; this generalization has passed its tests and nothing else.

## Part of a family

Six small tools for the case where an AI coding agent does the work and a human
is not watching every step.

| repo | one line |
|---|---|
| [curfew](https://github.com/blakehallisey-arch/curfew) | write-time policy for an unattended agent — deny by rule, not by prompt |
| breaker | stops a session that is spinning, spreading, or inventing work |
| [shipgate](https://github.com/blakehallisey-arch/shipgate) | will not let a merge through until the checks it actually needs have run |
| [nightwatch](https://github.com/blakehallisey-arch/nightwatch) | the run rail — a queue, a budget lid, a window, and an honest log |
| [draftdiff](https://github.com/blakehallisey-arch/draftdiff) | learns your voice from the edits you make before you hit send |
| [ledger](https://github.com/blakehallisey-arch/ledger) | gives stateless agents a memory of what you did with their advice |
