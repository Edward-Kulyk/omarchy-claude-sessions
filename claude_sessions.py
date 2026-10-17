#!/usr/bin/env python3
"""Backend for the Claude sessions omarchy-shell widget.

claude_sessions.py list         -> JSON {now, sessions: [...]}
claude_sessions.py watch        -> the same JSON, one line per change (inotify; the widget keeps one running)
claude_sessions.py focus <pid>  -> focus the terminal window hosting that Claude session
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
TITLE_CACHE = HOME / ".cache" / "omarchy-claude-sessions" / "titles.json"
TAIL_BYTES = 512 * 1024
HEARTBEAT_S = 60.0  # watch: full re-read even without events (catches anything inotify cannot see)
MISSING_POLL_S = 3.0  # watch: a live session has no transcript yet, look for it this often
NO_INOTIFY_POLL_S = 5.0  # watch: fallback interval when inotify is unavailable
DEBOUNCE_S = 0.3  # watch: registry change / session exit -> re-read after this
TRANSCRIPT_DEBOUNCE_S = 2.0  # watch: transcript appends (busy sessions write a lot) -> at most this often


def pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def read_sessions(sessions_dir: Path = SESSIONS_DIR, alive: Callable[[int], bool] = pid_alive) -> list[dict[str, Any]]:
    """Live interactive sessions from Claude's own ~/.claude/sessions/<pid>.json files."""
    out: list[dict[str, Any]] = []
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not alive(pid):
            continue
        cwd = str(data.get("cwd") or "")
        out.append(
            {
                "pid": pid,
                "sessionId": str(data.get("sessionId") or ""),
                "name": str(data.get("name") or ""),
                "cwd": cwd,
                "project": Path(cwd).name or cwd or "~",
                "status": "busy" if data.get("status") == "busy" else "waiting",
                "since": int(data.get("statusUpdatedAt") or data.get("updatedAt") or 0) // 1000,
                "startedAt": int(data.get("startedAt") or 0) // 1000,
            }
        )
    # Waiting sessions first (longest waiting on top), then busy ones.
    out.sort(key=lambda s: (s["status"] != "waiting", s["since"]))
    return out


def find_transcript(session_id: str, projects_dir: Path = PROJECTS_DIR) -> Path | None:
    if not session_id:
        return None
    return next(iter(projects_dir.glob(f"*/{session_id}.jsonl")), None)


def _records(lines: list[str]) -> list[dict[str, Any]]:
    recs = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs


def _empty_info() -> dict[str, Any]:
    return {"title": "", "lastPrompt": "", "contextTokens": 0, "model": ""}


def _scan(lines: list[str]) -> dict[str, Any]:
    """Latest title / last prompt / context size found in `lines` (newest record wins)."""
    info = _empty_info()
    for rec in reversed(_records(lines)):
        kind = rec.get("type")
        if kind == "ai-title" and not info["title"]:
            info["title"] = str(rec.get("aiTitle") or "")
        elif kind == "last-prompt" and not info["lastPrompt"]:
            info["lastPrompt"] = str(rec.get("lastPrompt") or "")
        elif kind == "assistant" and not info["contextTokens"]:
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            info["contextTokens"] = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            info["model"] = str(msg.get("model") or "")
        if info["title"] and info["lastPrompt"] and info["contextTokens"]:
            break
    return info


def _read_tail(path: Path, tail_bytes: int) -> tuple[dict[str, Any], int, int]:
    """Scan the last `tail_bytes` of a transcript -> (info, offset after the last complete line, inode)."""
    try:
        st = path.stat()
        with path.open("rb") as fh:
            start = max(0, st.st_size - tail_bytes)
            fh.seek(start)
            chunk = fh.read(st.st_size - start)
    except OSError:
        return _empty_info(), 0, 0
    lines = chunk.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line is probably cut in half
    return _scan(lines), start + chunk.rfind(b"\n") + 1, st.st_ino


def transcript_info(path: Path, tail_bytes: int = TAIL_BYTES) -> dict[str, Any]:
    """Title, last prompt and current context size from the tail of a transcript."""
    return _read_tail(path, tail_bytes)[0]


@dataclass
class _Tail:
    ino: int
    offset: int  # bytes up to here (always a line boundary) are already folded into `info`
    info: dict[str, Any]


class TranscriptCache:
    """Incremental transcript reader for `watch`: the tail is scanned once, then only appended lines are read."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR, tail_bytes: int = TAIL_BYTES) -> None:
        self.projects_dir = projects_dir
        self.tail_bytes = tail_bytes
        self._paths: dict[str, Path] = {}
        self._tails: dict[Path, _Tail] = {}

    def find(self, session_id: str, projects_dir: Path | None = None) -> Path | None:
        path = self._paths.get(session_id)
        if path is None or not path.exists():
            path = find_transcript(session_id, projects_dir or self.projects_dir)
            if path is None:
                self._paths.pop(session_id, None)
                return None
            self._paths[session_id] = path
        return path

    def info(self, path: Path, tail_bytes: int | None = None) -> dict[str, Any]:
        try:
            st = path.stat()
        except OSError:
            self._tails.pop(path, None)
            return _empty_info()
        tail = self._tails.get(path)
        if tail is None or tail.ino != st.st_ino or st.st_size < tail.offset:
            info, offset, ino = _read_tail(path, self.tail_bytes)
            tail = self._tails[path] = _Tail(ino, offset, info)
        elif st.st_size > tail.offset:
            try:
                with path.open("rb") as fh:
                    fh.seek(tail.offset)
                    chunk = fh.read(st.st_size - tail.offset)
            except OSError:
                return dict(tail.info)
            end = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
            if end:
                new = _scan(chunk[:end].decode("utf-8", "replace").splitlines())
                for key in ("title", "lastPrompt"):
                    if new[key]:
                        tail.info[key] = new[key]
                if new["contextTokens"]:
                    tail.info["contextTokens"] = new["contextTokens"]
                    tail.info["model"] = new["model"]
                elif new["model"] and not tail.info["model"]:
                    tail.info["model"] = new["model"]
                tail.offset += end
        return dict(tail.info)

    def live_paths(self) -> list[Path]:
        return list(self._tails)

    def has(self, session_id: str) -> bool:
        return session_id in self._paths

    def retain(self, session_ids: Iterable[str]) -> None:
        keep = set(session_ids)
        self._paths = {k: v for k, v in self._paths.items() if k in keep}
        paths = set(self._paths.values())
        self._tails = {k: v for k, v in self._tails.items() if k in paths}


def full_title(path: Path) -> str:
    """Latest ai-title anywhere in the transcript (fallback when the tail has none)."""
    title = ""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if b'"ai-title"' in raw:
                    try:
                        title = str(json.loads(raw).get("aiTitle") or title)
                    except ValueError:
                        pass
    except OSError:
        pass
    return title


def _load_titles(cache: Path) -> dict[str, str]:
    try:
        data = json.loads(cache.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def enrich(
    sessions: list[dict[str, Any]],
    projects_dir: Path = PROJECTS_DIR,
    cache: Path = TITLE_CACHE,
    transcripts: TranscriptCache | None = None,
) -> None:
    titles = _load_titles(cache)
    changed = False
    find = transcripts.find if transcripts else find_transcript
    read = transcripts.info if transcripts else transcript_info
    for s in sessions:
        path = find(s["sessionId"], projects_dir)
        info = read(path) if path else _empty_info()
        sid = s["sessionId"]
        if info["title"]:
            if titles.get(sid) != info["title"]:
                titles[sid] = info["title"]
                changed = True
        elif path and sid not in titles:
            titles[sid] = full_title(path)
            changed = True
        s.update(info)
        s["title"] = info["title"] or titles.get(sid, "")
    live = {s["sessionId"] for s in sessions}
    if transcripts:
        transcripts.retain(live)
    if changed or set(titles) - live:
        titles = {k: v for k, v in titles.items() if k in live}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(titles, ensure_ascii=False))
        except OSError:
            pass


# --- watch: one long-lived process instead of re-spawning `list` -------------------------------------------------

IN_MODIFY = 0x002
IN_CLOSE_WRITE = 0x008
IN_MOVED_FROM = 0x040
IN_MOVED_TO = 0x080
IN_CREATE = 0x100
IN_DELETE = 0x200
IN_DELETE_SELF = 0x400
IN_MOVE_SELF = 0x800
IN_IGNORED = 0x8000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
DIR_MASK = IN_CREATE | IN_DELETE | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
FILE_MASK = IN_MODIFY | IN_DELETE_SELF | IN_MOVE_SELF
_EVENT = struct.Struct("iIII")  # wd, mask, cookie, len (+ name)


class Inotify:
    """Minimal stdlib-only inotify (ctypes) that remembers what each watch is for."""

    def __init__(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self._add = libc.inotify_add_watch
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._rm = libc.inotify_rm_watch
        self._rm.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._wds: dict[Path, int] = {}
        self._kinds: dict[int, str] = {}

    def sync(self, wanted: dict[Path, tuple[int, str]]) -> None:
        """Watch exactly `wanted` ({path: (mask, kind)}); paths that do not exist (yet) are skipped."""
        for path in [p for p in self._wds if p not in wanted]:
            self._rm(self.fd, self._wds.pop(path))  # its IN_IGNORED drops the kind
        for path, (mask, kind) in wanted.items():
            if path in self._wds:
                continue
            wd = self._add(self.fd, os.fsencode(path), mask)
            if wd >= 0:
                self._wds[path] = wd
                self._kinds[wd] = kind

    def read(self) -> set[str]:
        """Drain pending events -> the kinds of watches that fired."""
        kinds: set[str] = set()
        while True:
            try:
                buf = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                return kinds
            pos = 0
            while pos + _EVENT.size <= len(buf):
                wd, mask, _cookie, length = _EVENT.unpack_from(buf, pos)
                pos += _EVENT.size + length
                kind = self._kinds.get(wd)
                if kind:
                    kinds.add(kind)
                if mask & IN_IGNORED:  # watch gone (file deleted/rotated): re-added on the next sync
                    self._kinds.pop(wd, None)
                    self._wds = {p: w for p, w in self._wds.items() if w != wd}


def _pidfd(pid: int) -> int | None:
    try:
        return os.pidfd_open(pid)
    except (OSError, AttributeError):
        return None


def snapshot(transcripts: TranscriptCache) -> dict[str, Any]:
    sessions = read_sessions()
    enrich(sessions, transcripts=transcripts)
    return {"now": int(time.time()), "sessions": sessions}


def fingerprint(payload: dict[str, Any]) -> str:
    """What `watch` compares to decide whether to emit: everything except the clock."""
    return json.dumps({k: v for k, v in payload.items() if k != "now"}, sort_keys=True)


def watch_targets(transcripts: TranscriptCache) -> dict[Path, tuple[int, str]]:
    targets = {SESSIONS_DIR: (DIR_MASK, "registry")}
    targets.update({p: (FILE_MASK, "transcript") for p in transcripts.live_paths()})
    return targets


def cmd_watch(emit: Callable[[str], None] | None = None) -> int:
    """Print `list` JSON once, then again whenever it changes; exits when the parent (the shell) goes away."""

    def default_emit(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    emit = emit or default_emit
    transcripts = TranscriptCache()
    try:
        ino: Inotify | None = Inotify()
    except OSError:
        ino = None
    poller = select.poll()
    if ino:
        poller.register(ino.fd, select.POLLIN)
    parent = _pidfd(os.getppid())
    if parent is not None:
        poller.register(parent, select.POLLIN)
    pidfds: dict[int, int] = {}  # fd -> session pid; readable once that Claude Code process exits
    exited: set[int] = set()  # pids whose pidfd already fired (a zombie still has /proc/<pid>; don't re-arm)
    last = ""
    while True:
        payload = snapshot(transcripts)
        fp = fingerprint(payload)
        if fp != last:
            try:
                emit(json.dumps(payload, ensure_ascii=False))
            except BrokenPipeError:
                return 0
            last = fp
        live = {s["pid"] for s in payload["sessions"]}
        exited &= live
        for fd in [fd for fd, pid in pidfds.items() if pid not in live]:
            poller.unregister(fd)
            os.close(fd)
            del pidfds[fd]
        for pid in live - set(pidfds.values()) - exited:
            new_fd = _pidfd(pid)
            if new_fd is not None:
                pidfds[new_fd] = pid
                poller.register(new_fd, select.POLLIN)
        if ino:
            ino.sync(watch_targets(transcripts))
            missing = any(s["sessionId"] and not transcripts.has(s["sessionId"]) for s in payload["sessions"])
            timeout = MISSING_POLL_S if missing else HEARTBEAT_S
        else:
            timeout = NO_INOTIFY_POLL_S
        ready = poller.poll(int(timeout * 1000))
        if not ready:
            continue
        kinds: set[str] = set()
        for fd, _ev in ready:
            if fd == parent:
                return 0
            if fd in pidfds:  # session exited: stop polling its (now permanently readable) pidfd
                poller.unregister(fd)
                os.close(fd)
                exited.add(pidfds[fd])
                pidfds[fd] = -1
                kinds.add("exit")
            elif ino and fd == ino.fd:
                kinds |= ino.read()
        pidfds = {fd: pid for fd, pid in pidfds.items() if pid != -1}
        # Coalesce bursts; transcript appends of busy sessions are only worth a re-read every few seconds.
        time.sleep(TRANSCRIPT_DEBOUNCE_S if kinds <= {"transcript"} else DEBOUNCE_S)
        if ino:
            ino.read()


def parent_pid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0
    # comm may contain spaces/parens: ppid is the 2nd field after the last ')'
    return int(stat.rsplit(")", 1)[1].split()[1])


def find_window(pid: int, clients: list[dict[str, Any]], ppid: Callable[[int], int] = parent_pid) -> str:
    """Hyprland address of the window whose process is `pid` or one of its ancestors."""
    by_pid = {int(c.get("pid") or 0): str(c.get("address") or "") for c in clients}
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid in by_pid:
            return by_pid[pid]
        seen.add(pid)
        pid = ppid(pid)
    return ""


def cmd_list() -> None:
    sessions = read_sessions()
    enrich(sessions)
    print(json.dumps({"now": int(time.time()), "sessions": sessions}, ensure_ascii=False))


def cmd_focus(pid: int) -> int:
    out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, check=False)  # noqa: S607
    try:
        clients = json.loads(out.stdout)
    except ValueError:
        return 1
    address = find_window(pid, clients)
    if not address:
        return 1
    subprocess.run(  # noqa: S603
        ["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{address}" }})'],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        cmd_list()
        return 0
    if cmd == "watch":
        return cmd_watch()
    if cmd == "focus" and len(argv) > 2 and argv[2].isdigit():
        return cmd_focus(int(argv[2]))
    print(f"usage: {argv[0]} list | watch | focus <pid>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
