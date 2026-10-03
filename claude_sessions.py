#!/usr/bin/env python3
"""Backend for the Claude sessions omarchy-shell widget.

claude_sessions.py list         -> JSON {now, sessions: [...]}
claude_sessions.py focus <pid>  -> focus the terminal window hosting that Claude session
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
TITLE_CACHE = HOME / ".cache" / "omarchy-claude-sessions" / "titles.json"
TAIL_BYTES = 512 * 1024


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


def transcript_info(path: Path, tail_bytes: int = TAIL_BYTES) -> dict[str, Any]:
    """Title, last prompt and current context size from the tail of a transcript."""
    info: dict[str, Any] = {"title": "", "lastPrompt": "", "contextTokens": 0, "model": ""}
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return info
    lines = chunk.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # first line is probably cut in half
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


def enrich(sessions: list[dict[str, Any]], projects_dir: Path = PROJECTS_DIR, cache: Path = TITLE_CACHE) -> None:
    titles = _load_titles(cache)
    changed = False
    for s in sessions:
        path = find_transcript(s["sessionId"], projects_dir)
        empty: dict[str, Any] = {"title": "", "lastPrompt": "", "contextTokens": 0, "model": ""}
        info = transcript_info(path) if path else empty
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
    if changed or set(titles) - live:
        titles = {k: v for k, v in titles.items() if k in live}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(titles, ensure_ascii=False))
        except OSError:
            pass


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
    if cmd == "focus" and len(argv) > 2 and argv[2].isdigit():
        return cmd_focus(int(argv[2]))
    print(f"usage: {argv[0]} list | focus <pid>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
