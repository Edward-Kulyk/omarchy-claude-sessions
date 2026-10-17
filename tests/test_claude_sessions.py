import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_sessions as cs


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ReadSessions(Base):
    def test_live_sessions_sorted_waiting_first(self):
        d = self.tmp / "sessions"
        write(d / "1.json", {"pid": 1, "sessionId": "a", "cwd": "/p/alpha", "status": "busy", "statusUpdatedAt": 5000})
        write(d / "2.json", {"pid": 2, "sessionId": "b", "cwd": "/p/beta", "status": "idle", "statusUpdatedAt": 9000})
        write(d / "3.json", {"pid": 3, "sessionId": "c", "cwd": "/p/gamma", "status": "idle", "statusUpdatedAt": 7000})
        write(d / "4.json", {"pid": 4, "sessionId": "dead", "cwd": "/p/x", "status": "busy"})
        write(d / "5.json", "garbage")
        write(d / "5.abc.key", "not json")
        got = cs.read_sessions(d, alive=lambda pid: pid != 4)
        self.assertEqual([s["project"] for s in got], ["gamma", "beta", "alpha"])
        self.assertEqual([s["status"] for s in got], ["waiting", "waiting", "busy"])
        self.assertEqual(got[0]["since"], 7)


class Transcript(Base):
    def lines(self, *recs):
        return "\n".join(json.dumps(r) for r in recs) + "\n"

    def test_tail_info(self):
        p = write(
            self.tmp / "t.jsonl",
            self.lines(
                {"type": "ai-title", "aiTitle": "Old title"},
                {"type": "assistant", "message": {"model": "m1", "usage": {"input_tokens": 1}}},
                {"type": "ai-title", "aiTitle": "New title"},
                {"type": "last-prompt", "lastPrompt": "do it"},
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-x",
                        "usage": {
                            "input_tokens": 2,
                            "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 10,
                        },
                    },
                },
                {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            ),
        )
        info = cs.transcript_info(p)
        expected = {"title": "New title", "lastPrompt": "do it", "contextTokens": 1012, "model": "claude-x"}
        self.assertEqual(info, expected)

    def test_title_outside_tail_uses_full_scan_and_cache(self):
        proj = self.tmp / "projects" / "-p"
        body = (
            self.lines({"type": "ai-title", "aiTitle": "Early title"})
            + ("x" * 2000 + "\n")
            + self.lines({"type": "last-prompt", "lastPrompt": "hi"})
        )
        write(proj / "sid.jsonl", body)
        sessions = [{"sessionId": "sid"}]
        cache = self.tmp / "cache" / "titles.json"
        orig = cs.TAIL_BYTES
        try:
            cs.transcript_info.__defaults__ = (100,)
            cs.enrich(sessions, self.tmp / "projects", cache)
        finally:
            cs.transcript_info.__defaults__ = (orig,)
        self.assertEqual(sessions[0]["title"], "Early title")
        self.assertEqual(sessions[0]["lastPrompt"], "hi")
        self.assertEqual(json.loads(cache.read_text()), {"sid": "Early title"})

    def test_missing_transcript(self):
        sessions = [{"sessionId": "nope"}]
        cs.enrich(sessions, self.tmp / "projects", self.tmp / "c.json")
        self.assertEqual(sessions[0]["title"], "")
        self.assertEqual(sessions[0]["contextTokens"], 0)

    def test_cache_drops_dead_sessions(self):
        cache = write(self.tmp / "c.json", {"gone": "x"})
        cs.enrich([], self.tmp / "projects", cache)
        self.assertEqual(json.loads(cache.read_text()), {})


class Incremental(Base):
    def rec(self, r) -> str:
        return json.dumps(r) + "\n"

    def test_appends_are_folded_in_and_match_full_scan(self):
        p = write(self.tmp / "t.jsonl", self.rec({"type": "ai-title", "aiTitle": "T1"}))
        tc = cs.TranscriptCache(self.tmp)
        self.assertEqual(tc.info(p)["title"], "T1")
        with p.open("a") as fh:
            fh.write(self.rec({"type": "last-prompt", "lastPrompt": "go"}))
            fh.write(self.rec({"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 7}}}))
            fh.write('{"type": "ai-title", "aiTi')  # half-written line
        self.assertEqual(tc.info(p), {"title": "T1", "lastPrompt": "go", "contextTokens": 7, "model": "m"})
        with p.open("a") as fh:
            fh.write('tle": "T2"}\n')
        self.assertEqual(tc.info(p), cs.transcript_info(p))
        self.assertEqual(tc.info(p)["title"], "T2")

    def test_rewritten_file_is_rescanned(self):
        p = write(self.tmp / "t.jsonl", self.rec({"type": "ai-title", "aiTitle": "long title here"}))
        tc = cs.TranscriptCache(self.tmp)
        tc.info(p)
        write(p, self.rec({"type": "ai-title", "aiTitle": "x"}))
        self.assertEqual(tc.info(p)["title"], "x")

    def test_retain_drops_dead_sessions(self):
        write(self.tmp / "-p" / "sid.jsonl", self.rec({"type": "ai-title", "aiTitle": "T"}))
        tc = cs.TranscriptCache(self.tmp)
        path = tc.find("sid")
        assert path is not None
        tc.info(path)
        self.assertTrue(tc.has("sid"))
        tc.retain([])
        self.assertFalse(tc.has("sid"))
        self.assertEqual(tc.live_paths(), [])


class Watch(Base):
    """End to end: `watch` in a fake HOME emits once at start and again on each change, then on session exit."""

    def readline(self, proc, timeout=8.0):
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        self.assertTrue(ready, "watch emitted nothing")
        return json.loads(proc.stdout.readline())

    def test_emits_on_change_only(self):
        home = self.tmp
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )  # stands in for a Claude Code process
        self.addCleanup(sleeper.kill)
        reg = home / ".claude" / "sessions" / f"{sleeper.pid}.json"
        meta = {"pid": sleeper.pid, "sessionId": "sid", "cwd": "/p/alpha", "status": "busy"}
        write(reg, meta)
        tr = write(home / ".claude" / "projects" / "-p-alpha" / "sid.jsonl", '{"type": "ai-title", "aiTitle": "A"}\n')
        env = {**os.environ, "HOME": str(home)}
        proc = subprocess.Popen(
            [sys.executable, str(Path(cs.__file__)), "watch"], stdout=subprocess.PIPE, text=True, env=env
        )
        self.addCleanup(proc.kill)
        first = self.readline(proc)
        self.assertEqual([(s["status"], s["title"]) for s in first["sessions"]], [("busy", "A")])

        write(reg, {**meta, "status": "idle"})
        self.assertEqual(self.readline(proc)["sessions"][0]["status"], "waiting")

        with tr.open("a") as fh:
            fh.write('{"type": "last-prompt", "lastPrompt": "hello"}\n')
        self.assertEqual(self.readline(proc)["sessions"][0]["lastPrompt"], "hello")

        sleeper.kill()
        sleeper.wait()
        self.assertEqual(self.readline(proc)["sessions"], [])

        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        self.assertFalse(ready, "nothing changed, nothing should be emitted")
        proc.kill()
        proc.wait()


class Focus(unittest.TestCase):
    def test_walks_up_to_terminal(self):
        parents = {500: 400, 400: 300, 300: 1}
        clients = [{"pid": 300, "address": "0xterm"}, {"pid": 999, "address": "0xother"}]
        self.assertEqual(cs.find_window(500, clients, ppid=lambda p: parents.get(p, 0)), "0xterm")

    def test_no_window(self):
        self.assertEqual(cs.find_window(500, [], ppid=lambda p: {500: 1}.get(p, 0)), "")

    def test_cycle_safe(self):
        self.assertEqual(cs.find_window(5, [], ppid=lambda p: 5), "")

    def test_parent_pid_of_self(self):
        self.assertEqual(cs.parent_pid(os.getpid()), os.getppid())


class Main(unittest.TestCase):
    def test_usage(self):
        self.assertEqual(cs.main(["x", "bogus"]), 2)
        self.assertEqual(cs.main(["x", "focus", "abc"]), 2)


if __name__ == "__main__":
    unittest.main()
