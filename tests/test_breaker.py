"""
Tests for the trips.

A guard with no test on its denies is decoration, so every test here is about a
breaker firing, not firing twice, or deliberately failing open. Each test drives
the real CLI in a subprocess with a real JSON payload on stdin, because the exit
code is the entire contract with Claude Code and an in-process call cannot prove
an exit code.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BreakerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="breaker-test-")
        self.project = os.path.join(self.tmp, "project")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.project)
        os.makedirs(self.state)
        # a .git marks the project root without needing a real repo
        os.makedirs(os.path.join(self.project, ".git"))
        self.cfg_path = os.path.join(self.project, ".breaker.json")
        self.write_config({})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def write_config(self, cfg):
        with open(self.cfg_path, "w") as f:
            json.dump(cfg, f)

    def env(self, **extra):
        e = dict(os.environ)
        e["BREAKER_STATE_DIR"] = self.state
        e["PYTHONPATH"] = REPO + os.pathsep + e.get("PYTHONPATH", "")
        e.update(extra)
        return e

    def call(self, payload=None, args=(), raw_stdin=None, env=None):
        stdin = raw_stdin if raw_stdin is not None else json.dumps(payload or {})
        p = subprocess.run(
            [sys.executable, "-m", "breaker"] + list(args),
            input=stdin, capture_output=True, text=True,
            cwd=self.project, env=env or self.env(),
        )
        return p

    def payload(self, tool="Read", path=None, session="s1"):
        ti = {}
        if path is not None:
            ti["file_path"] = path
        return {
            "session_id": session,
            "cwd": self.project,
            "tool_name": tool,
            "tool_input": ti,
        }

    def edit(self, path, session="s1"):
        """An Edit on an existing file: counts, but can never be a NEW FILE."""
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        return self.call(self.payload("Edit", path, session))

    def reset(self, session="s1"):
        return self.call(self.payload(session=session), args=["--reset"])

    def f(self, name):
        p = os.path.join(self.project, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p


class TestSpin(BreakerCase):
    def test_spin_fires_at_the_threshold(self):
        self.write_config({"spin_first": 5, "spin_recheck": 4})
        for i in range(4):
            r = self.call(self.payload())
            self.assertEqual(r.returncode, 0, "call %d should be quiet" % i)
        r = self.call(self.payload())
        self.assertEqual(r.returncode, 2)
        self.assertIn("BREAKER", r.stderr)
        self.assertIn("5 tool calls", r.stderr)

    def test_spin_fires_once_then_rearms_further_out(self):
        self.write_config({"spin_first": 3, "spin_recheck": 5})
        for _ in range(2):
            self.assertEqual(self.call(self.payload()).returncode, 0)
        self.assertEqual(self.call(self.payload()).returncode, 2)  # call 3
        # re-armed at 3 + 5 = 8, so 4..7 are quiet
        for i in range(4, 8):
            self.assertEqual(self.call(self.payload()).returncode, 0,
                             "call %d should be quiet" % i)
        self.assertEqual(self.call(self.payload()).returncode, 2)  # call 8


class TestSpread(BreakerCase):
    def test_spread_fires(self):
        self.write_config({"file_spread": 4, "spin_first": 999})
        for i in range(3):
            r = self.edit(self.f("pkg/f%d.py" % i))
            self.assertEqual(r.returncode, 0)
        r = self.edit(self.f("pkg/f3.py"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("4 different files", r.stderr)

    def test_spread_fires_only_once_per_turn(self):
        self.write_config({"file_spread": 3, "spin_first": 999})
        for i in range(2):
            self.edit(self.f("pkg/f%d.py" % i))
        self.assertEqual(self.edit(self.f("pkg/f2.py")).returncode, 2)
        self.assertEqual(self.edit(self.f("pkg/f3.py")).returncode, 0)
        self.assertEqual(self.edit(self.f("pkg/f4.py")).returncode, 0)

    def test_same_file_twice_is_not_spread(self):
        self.write_config({"file_spread": 3, "spin_first": 999,
                           "same_file_loop": 99})
        p = self.f("pkg/one.py")
        for _ in range(6):
            self.assertEqual(self.edit(p).returncode, 0)


class TestLoop(BreakerCase):
    def test_loop_fires(self):
        self.write_config({"same_file_loop": 4, "spin_first": 999})
        p = self.f("pkg/round.py")
        for _ in range(3):
            self.assertEqual(self.edit(p).returncode, 0)
        r = self.edit(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("rewritten the same file 4 times", r.stderr)

    def test_loop_fires_only_once_per_turn(self):
        self.write_config({"same_file_loop": 3, "spin_first": 999})
        p = self.f("pkg/round.py")
        self.edit(p)
        self.edit(p)
        self.assertEqual(self.edit(p).returncode, 2)
        for _ in range(5):
            self.assertEqual(self.edit(p).returncode, 0)


class TestNewFile(BreakerCase):
    def test_new_root_file_fires_on_the_first_write(self):
        r = self.call(self.payload("Write", self.f("ranker.py")))
        self.assertEqual(r.returncode, 2)
        self.assertIn("a new file at the project root: ranker.py", r.stderr)
        self.assertIn("FOLD", r.stderr)
        self.assertIn("BUILD", r.stderr)

    def test_new_file_fires_only_once_per_turn(self):
        self.assertEqual(
            self.call(self.payload("Write", self.f("one.py"))).returncode, 2)
        self.assertEqual(
            self.call(self.payload("Write", self.f("two.py"))).returncode, 0)

    def test_deep_new_file_is_fine_when_root_only(self):
        r = self.call(self.payload("Write", self.f("pkg/sub/helper.py")))
        self.assertEqual(r.returncode, 0)

    def test_deep_new_file_fires_when_root_only_is_off(self):
        self.write_config({"root_only": False})
        r = self.call(self.payload("Write", self.f("pkg/sub/helper.py")))
        self.assertEqual(r.returncode, 2)

    def test_write_over_an_existing_root_file_is_not_a_birth(self):
        p = self.f("already.py")
        with open(p, "w") as fh:
            fh.write("x")
        self.assertEqual(self.call(self.payload("Write", p)).returncode, 0)

    def test_unlisted_extension_does_not_fire(self):
        self.assertEqual(
            self.call(self.payload("Write", self.f("notes.md"))).returncode, 0)

    def test_registry_write_fires_even_on_an_edit_deep_in_the_tree(self):
        self.write_config({"registry_files": ["registry.json"]})
        p = self.f("meta/registry.json")
        with open(p, "w") as fh:
            fh.write("{}")
        r = self.call(self.payload("Edit", p))
        self.assertEqual(r.returncode, 2)
        self.assertIn("registry", r.stderr)

    def test_registry_glob(self):
        self.write_config({"registry_files": ["config/*.registry.json"]})
        p = self.f("config/things.registry.json")
        with open(p, "w") as fh:
            fh.write("{}")
        self.assertEqual(self.call(self.payload("Edit", p)).returncode, 2)


class TestReset(BreakerCase):
    def test_reset_clears_the_counters(self):
        self.write_config({"spin_first": 3, "spin_recheck": 3})
        self.call(self.payload())
        self.call(self.payload())
        self.assertEqual(self.reset().returncode, 0)
        for _ in range(2):
            self.assertEqual(self.call(self.payload()).returncode, 0)
        self.assertEqual(self.call(self.payload()).returncode, 2)

    def test_reset_re_arms_a_spent_reason(self):
        r = self.call(self.payload("Write", self.f("a.py")))
        self.assertEqual(r.returncode, 2)
        self.reset()
        r = self.call(self.payload("Write", self.f("b.py")))
        self.assertEqual(r.returncode, 2)

    def test_sessions_do_not_share_counters(self):
        self.write_config({"spin_first": 2, "spin_recheck": 2})
        self.assertEqual(self.call(self.payload(session="a")).returncode, 0)
        self.assertEqual(self.call(self.payload(session="b")).returncode, 0)
        self.assertEqual(self.call(self.payload(session="a")).returncode, 2)


class TestIgnorePaths(BreakerCase):
    def test_ignored_paths_are_not_counted_for_spread(self):
        self.write_config({"file_spread": 3, "spin_first": 999})
        for i in range(6):
            r = self.edit(self.f("node_modules/dep%d/index.js" % i))
            self.assertEqual(r.returncode, 0)

    def test_ignored_path_does_not_trip_new_file(self):
        self.write_config({"ignore_paths": ["**/vendor.py"]})
        r = self.call(self.payload("Write", self.f("vendor.py")))
        self.assertEqual(r.returncode, 0)

    def test_custom_ignore_replaces_the_default(self):
        self.write_config({"file_spread": 3, "spin_first": 999,
                           "ignore_paths": ["**/generated/**"]})
        for i in range(5):
            self.edit(self.f("generated/g%d.py" % i))
        # nothing tripped on the ignored ones
        r = self.edit(self.f("pkg/a.py"))
        self.assertEqual(r.returncode, 0)


class TestFailsOpen(BreakerCase):
    def test_malformed_stdin_exits_zero(self):
        r = self.call(raw_stdin="{not json at all")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_empty_stdin_exits_zero(self):
        self.assertEqual(self.call(raw_stdin="").returncode, 0)

    def test_non_object_stdin_exits_zero(self):
        self.assertEqual(self.call(raw_stdin="[1,2,3]").returncode, 0)

    def test_malformed_config_falls_back_to_defaults(self):
        with open(self.cfg_path, "w") as f:
            f.write("{ this is not json")
        r = self.call(self.payload("Write", self.f("newthing.py")))
        # defaults still guard: NEW FILE fires
        self.assertEqual(r.returncode, 2)

    def test_unwritable_state_dir_exits_zero(self):
        blocked = os.path.join(self.tmp, "blocked")
        os.makedirs(blocked)
        os.chmod(blocked, 0o500)
        try:
            env = self.env(BREAKER_STATE_DIR=os.path.join(blocked, "sub"))
            self.write_config({"spin_first": 2, "spin_recheck": 2})
            for _ in range(5):
                r = self.call(self.payload(), env=env)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(r.stderr, "")
        finally:
            os.chmod(blocked, 0o700)

    def test_disabled_never_fires(self):
        self.write_config({"enabled": False, "spin_first": 2})
        for _ in range(6):
            self.assertEqual(self.call(self.payload()).returncode, 0)
        self.assertEqual(
            self.call(self.payload("Write", self.f("x.py"))).returncode, 0)

    def test_hostile_session_id_cannot_escape_the_state_dir(self):
        p = self.payload()
        p["session_id"] = "../../../../etc/passwd"
        r = self.call(p)
        self.assertEqual(r.returncode, 0)
        names = os.listdir(self.state)
        self.assertEqual(names, ["etcpasswd.json"])


class TestReport(BreakerCase):
    def test_report_json(self):
        self.write_config({"spin_first": 999, "same_file_loop": 99})
        p = self.f("pkg/a.py")
        self.edit(p)
        self.edit(p)
        self.edit(self.f("pkg/b.py"))
        r = self.call(args=["report", "--json", "--session", "s1"])
        self.assertEqual(r.returncode, 0)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["calls"], 3)
        self.assertEqual(rep["files_touched"], 2)
        self.assertEqual(os.path.basename(rep["most_rewritten"]), "a.py")
        self.assertEqual(rep["most_rewritten_count"], 2)
        self.assertEqual(rep["fired"], [])

    def test_report_text_names_the_trip(self):
        self.write_config({"spin_first": 2, "spin_recheck": 50})
        self.call(self.payload())
        self.call(self.payload())
        r = self.call(args=["report", "--session", "s1"])
        self.assertIn("tripped        spin", r.stdout)

    def test_report_with_no_state(self):
        r = self.call(args=["report", "--session", "nope"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("no state", r.stdout)

    def test_help_exits_zero(self):
        r = self.call(args=["--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)


if __name__ == "__main__":
    unittest.main()
