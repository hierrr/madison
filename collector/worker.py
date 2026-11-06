#!/usr/bin/env python3
"""종료 작업의 영속 큐와 일회성 로컬 사용량 대조."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

KINDS = ("in", "out", "cr", "cw", "th")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def normalize(u, agent):
    if agent == "codex-cli":
        return {"in": max(0, int(u.get("input_tokens") or 0) - int(u.get("cached_input_tokens") or 0)),
                "out": int(u.get("output_tokens") or 0), "cr": int(u.get("cached_input_tokens") or 0),
                "cw": int(u.get("cache_write_input_tokens") or 0),
                "th": int(u.get("reasoning_output_tokens") or 0)}
    details = u.get("output_tokens_details") or {}
    return {"in": int(u.get("input_tokens") or 0), "out": int(u.get("output_tokens") or 0),
            "cr": int(u.get("cache_read_input_tokens") or 0), "cw": int(u.get("cache_creation_input_tokens") or 0),
            "th": int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0}


def snapshot(path, agent, sid, run_id, *, quiet_seconds=0):
    result = {"agent": agent, "session_id": sid, "run_id": run_id,
              "source": "transcript", "complete": False, "samples": []}
    if not path or not path.is_file():
        return {**result, "issue": "missing_source"}
    before = path.stat()
    if time.time() - before.st_mtime < quiet_seconds:
        return {**result, "issue": "active_deferred"}
    acc = defaultdict(lambda: {k: 0 for k in KINDS})
    samples = []
    current_model = "unknown"
    incomplete = False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                ev = json.loads(line)
            except ValueError:
                incomplete = True
                continue
            if not isinstance(ev, dict):
                continue
            pl = ev.get("payload")
            if agent == "codex-cli":
                if isinstance(pl, dict) and ev.get("type") == "turn_context" and pl.get("model"):
                    current_model = str(pl["model"])[:60]
                if not isinstance(pl, dict) or pl.get("type") != "token_count":
                    continue
                info = pl.get("info") or {}
                usage = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict):
                    continue
                value = normalize(usage, agent)
                model = current_model
            else:
                msg = ev.get("message")
                if ev.get("type") != "assistant" or not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                model = str(msg.get("model") or "unknown")[:60]
                for k, n in normalize(usage, agent).items():
                    acc[model][k] += n
                value = dict(acc[model])
            try:
                ts = datetime.fromisoformat(str(ev.get("timestamp") or "").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    raise ValueError
                ts = ts.astimezone(timezone.utc).isoformat()
            except ValueError:
                incomplete = True
                continue
            point = {"ts": ts, "model": model, "cum": value}
            if samples and samples[-1]["ts"] > ts:
                incomplete = True
            if (samples and samples[-1]["model"] == model and samples[-1]["ts"][:13] == ts[:13]
                    and all(value[k] >= samples[-1]["cum"][k] for k in KINDS)):
                samples[-1] = point
            else:
                samples.append(point)
    after = path.stat()
    if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
        return {**result, "issue": "active_deferred"}
    if len(samples) > 4000:
        return {**result, "issue": "source_too_large"}
    result.update(samples=samples, complete=not incomplete)
    if incomplete:
        result["issue"] = "incomplete_source"
    return result


def frontend():
    try:
        output = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                                text=True, timeout=0.5, check=False).stdout
        parents = {}
        for line in output.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                parents[int(parts[0])] = (int(parts[1]), parts[2])
        pid = os.getppid()
        for _ in range(10):
            pid, command = parents.get(pid, (0, ""))
            if "/Applications/Claude.app/" in command or "/Applications/Codex.app/" in command:
                return "app"
            if ("claude" in command and (" -p" in command or " --print" in command)) or "codex exec " in command:
                return "auto"
            if not pid:
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "cli"


class Worker:
    def __init__(self, directory, user_root=None):
        self.directory = Path(directory)
        self.user_root = Path(user_root) if user_root else Path.home()
        self.queue = self.directory / "pending"
        self.release = read_json(self.directory / "version.json", {})
        self.env = {}
        for line in (self.directory / "env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                self.env[key] = value.strip().strip("\"'")

    def request(self, route, body=None):
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        req = urllib.request.Request(self.env["MADISON_URL"].rstrip("/") + route, data=data,
                                     headers={"Authorization": "Bearer " + self.env["MADISON_TOKEN"],
                                              "Content-Type": "application/json",
                                              # Cloudflare can reject urllib's library-default signature (1010).
                                              "User-Agent": "MADISON-Collector/" + self.release.get("version", "unknown")})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def enqueue(self, agent, hook):
        job_id = str(uuid.uuid4())
        job = {"kind": "end", "id": job_id, "agent": agent, "ts": utcnow(), "frontend": frontend(),
               "hook": {k: hook[k] for k in ("session_id", "transcript_path", "cwd", "reason", "model") if k in hook}}
        if not isinstance(hook.get("session_id"), str) or not hook["session_id"]:
            raise ValueError("세션 ID 없음")
        atomic_json(self.queue / (job_id + ".json"), job)

    def schedule_audit(self, force=False):
        done = read_json(self.directory / "audit-done.json", {})
        if not force and done.get("revision") == self.release.get("audit_revision"):
            return
        path = self.queue / "audit.json"
        if not path.exists():
            atomic_json(path, {"kind": "audit", "id": str(uuid.uuid4()), "finished": [],
                               "revision": self.release["audit_revision"]})

    def install(self):
        atomic_json(self.queue / "install.json", {"kind": "install", "id": str(uuid.uuid4())})

    def send_end(self, path, job):
        hook = job["hook"]
        if not job.get("sent"):
            client = job["frontend"]
            if job["agent"] == "codex-cli":
                client = ""
                try:
                    with Path(hook["transcript_path"]).open() as stream:
                        meta = json.loads(stream.readline()).get("payload", {})
                    client = {"Codex Desktop": "app", "codex-desktop": "app", "codex_desktop": "app",
                              "codex-tui": "cli", "cli": "cli", "codex_exec": "auto", "exec": "auto"}.get(meta.get("originator"), "")
                except (OSError, ValueError, KeyError):
                    pass
            cwd = hook.get("cwd") or ""
            project, branch, origin = Path(cwd).name, "", ""
            if cwd and Path(cwd).is_dir():
                def git(*args):
                    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                                          text=True, timeout=3, check=False).stdout.strip()
                top = git("rev-parse", "--show-toplevel")
                if top:
                    project, branch, origin = Path(top).name, git("branch", "--show-current"), git("remote", "get-url", "origin")
            self.request("/api/events", {"agent": job["agent"], "session_id": hook["session_id"],
                         "event": "session_end", "event_id": "shutdown-" + job["id"], "ts": job["ts"],
                         "project": project, "branch": branch, "origin": origin,
                         "detail": {"reason": hook.get("reason") or "other", "frontend": client,
                                    "collection_mode": "hooks", "model": hook.get("model") or ""}})
            job["sent"] = True
            atomic_json(path, job)
        snap = snapshot(Path(hook["transcript_path"]) if hook.get("transcript_path") else None,
                        job["agent"], hook["session_id"], "end-" + job["id"])
        result = self.request("/api/collector/reconcile", snap)
        if result["status"] in ("active_deferred", "incomplete_source"):
            return False
        return True

    def inventory(self):
        claude = self.user_root / ".claude"
        codex = Path(os.environ.get("CODEX_HOME", str(self.user_root / ".codex")))
        paths = {}
        duplicate = set()
        for p in (claude / "projects").glob("*/*.jsonl"):
            key = ("claude-code", p.stem)
            if key in paths:
                duplicate.add(key)
            paths[key] = p
        totals = {}
        state_dbs = sorted(codex.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
        if state_dbs:
            try:
                with sqlite3.connect(state_dbs[0].as_uri() + "?mode=ro", uri=True) as db:
                    for sid, filename, total in db.execute("SELECT id,rollout_path,tokens_used FROM threads"):
                        paths[("codex-cli", sid)] = Path(filename)
                        totals[sid] = total
            except sqlite3.Error:
                pass
        for root in (codex / "sessions", codex / "archived_sessions"):
            for p in root.rglob("*.jsonl"):
                sid = p.stem[-36:]
                paths.setdefault(("codex-cli", sid), p)
        config = read_json(self.user_root / ".claude.json", {})
        cache = {}
        for project in config.get("projects", {}).values():
            if isinstance(project, dict) and project.get("lastSessionId"):
                cache[project["lastSessionId"]] = project.get("lastModelUsage") or {}
        info = {"claude_transcripts": sum(a == "claude-code" for a, _ in paths),
                "claude_last_sessions": len(cache), "claude_stats_cache": (claude / "stats-cache.json").exists(),
                "claude_insights_cache": (claude / "usage-data").is_dir(),
                "codex_state_db": bool(state_dbs), "codex_sessions": len(totals),
                "duplicate_transcripts": len(duplicate)}
        return paths, duplicate, totals, cache, info

    def audit(self, path, job):
        if not job.get("started"):
            self.request("/api/collector/status", {"action": "audit_started", "version": self.release["version"], "run_id": job["id"]})
            job["started"] = True
            atomic_json(path, job)
        sessions = self.request("/api/collector/sessions")["sessions"]
        paths, duplicate, totals, cache, info = self.inventory()
        known = {(r["agent"], r["session_id"]) for r in sessions}
        info["untracked_local_sessions"] = len(set(paths) - known)
        completed = set(job["finished"])
        for index, row in enumerate(sessions):
            if index % 8 == 0:
                self.drain_ends()
            agent, sid = row["agent"], row["session_id"]
            key = agent + ":" + sid
            if key in completed:
                continue
            try:
                snap = snapshot(paths.get((agent, sid)), agent, sid, job["id"], quiet_seconds=600)
            except (OSError, ValueError, TypeError):
                snap = {"agent": agent, "session_id": sid, "run_id": job["id"], "source": "transcript",
                        "complete": False, "samples": [], "issue": "source_read_error"}
            if (agent, sid) in duplicate:
                snap.update(complete=False, issue="duplicate_sources")
            if snap.get("issue") == "missing_source":
                if agent == "codex-cli" and sid in totals:
                    snap.update(source="local_db", issue="aggregate_only", local_total=totals[sid])
                elif agent == "claude-code" and sid in cache:
                    snap.update(source="last_session_cache", issue="aggregate_only", model_usage=cache[sid])
            if agent == "codex-cli" and snap["samples"] and sid in totals:
                last = snap["samples"][-1]["cum"]
                if totals[sid] != last["in"] + last["cr"] + last["out"]:
                    snap.update(complete=False, issue="local_db_conflict")
            self.request("/api/collector/reconcile", snap)
            completed.add(key)
            job["finished"] = sorted(completed)
            atomic_json(path, job)
        self.request("/api/collector/status", {"action": "audit_finished", "version": self.release["version"],
                                               "run_id": job["id"], "inventory": info})
        atomic_json(self.directory / "audit-done.json", {"revision": job["revision"], "run_id": job["id"], "finished_at": utcnow()})
        return True

    def drain_ends(self):
        for path in sorted(self.queue.glob("*.json")):
            job = read_json(path)
            if job and job.get("kind") == "end" and self.send_end(path, job):
                path.unlink(missing_ok=True)

    def run(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for _ in range(2):
                paths = sorted(self.queue.glob("*.json"), key=lambda p: (p.name in ("install.json", "audit.json"), p.name == "audit.json", p.name))
                for path in paths:
                    job = read_json(path)
                    if not job:
                        continue
                    try:
                        if job["kind"] == "end":
                            done = self.send_end(path, job)
                        elif job["kind"] == "install":
                            self.request("/api/collector/status", {"action": "installed", "version": self.release["version"]})
                            self.schedule_audit()
                            done = True
                        elif job["kind"] == "audit":
                            done = self.audit(path, job)
                        else:
                            done = False
                        if done:
                            path.unlink(missing_ok=True)
                    except Exception as exc:
                        detail = type(exc).__name__
                        if isinstance(exc, urllib.error.HTTPError):
                            detail += " HTTP " + str(exc.code)
                            ray = exc.headers.get("cf-ray") if exc.headers else None
                            if ray:
                                detail += " Cloudflare Ray " + ray[:100]
                        print("MADISON 작업 보류:", job.get("kind"), detail, file=sys.stderr)
                        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("enqueue", "run", "installed", "audit"))
    parser.add_argument("agent", nargs="?", choices=("claude-code", "codex-cli"))
    parser.add_argument("--mad-dir", type=Path, default=Path.home() / ".claude/madison")
    args = parser.parse_args()
    worker = Worker(args.mad_dir)
    if args.command == "enqueue":
        if not args.agent:
            parser.error("에이전트가 필요합니다")
        worker.enqueue(args.agent, json.load(sys.stdin))
    elif args.command == "installed":
        worker.install()
    elif args.command == "audit":
        worker.schedule_audit(force=True)
        worker.run()
    else:
        worker.run()


if __name__ == "__main__":
    main()
