#!/usr/bin/env python3
"""Click-to-dictate feedback reader: click a spot in a PDF, speak, and the note is pinned there.

    python3 reader/server.py draft.pdf         # resume the latest round for this PDF
    python3 reader/server.py draft.pdf --new   # start a fresh round
    python3 reader/server.py                   # start on the review queue; drop a PDF on the page

Opens http://127.0.0.1:8765 in the browser. Click anywhere on the PDF: a pin is dropped,
recording starts at once (the mic stays open for the session, so there is no start-up delay)
and you can also type. Enter saves, Esc discards, G makes a general remark with no anchor,
T a typed one that never records, M turns the mic on or off.

Each browser tab has its own PDF and round (?tab=<id>, see Tabs); bin/open-reader asks
/api/route which tab to use. Clips are transcribed in the background as they are saved
(Worker), so Finish round returns at once and process.py waits for whatever is left.

Rounds live in $FR_FEEDBACK/<group>/<pdf-stem>-<time>/ (see config.py). The group is
project (a PDF built inside $FR_PROJECT), sandbox (built from $FR_SANDBOX/*.tex: test rounds),
email (drafts rendered by bin/email-to-reader, which puts them in a folder named email-drafts)
or other (any PDF from elsewhere). tidy() keeps old rounds from piling up (see KEEP_DAYS).

Each note is stored in notes.json with its page, its coordinates in SyncTeX units (big points,
origin top left), the line of PDF text under the click, any typed text and the SyncTeX source
file and line. Audio goes to <round>/audio/. process.py turns a finished round into
feedback.md, a compact text file an agent reads to apply the edits.

Testing: run a second copy with FR_FEEDBACK=<scratch dir> and --port 8766, never on your real
rounds.

Stdlib only. Binds to 127.0.0.1, so nothing is reachable from the network.
"""
import argparse
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from config import (BUILD, COMMAND, FEEDBACK, PORT, PROJECT, PROJECT_COMMAND, PROJECT_GROUP,  # noqa: E402
                    SANDBOX, in_project, rel)

LOCK = threading.Lock()
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SAFE_EXT = {"webm", "ogg", "mp4", "m4a", "wav"}
GROUPS = (PROJECT_GROUP, "sandbox", "email", "other")
EMAIL_DIR = "email-drafts"  # parent folder name of PDFs made by bin/email-to-reader
KEEP_DAYS = 30  # after a round is finished: audio of project rounds, and whole sandbox/other rounds


def group_for(pdf: Path) -> str:
    p = pdf.resolve()
    if p.parent.name == EMAIL_DIR:
        return "email"
    if not in_project(p) or p.is_relative_to(FEEDBACK):
        return "other"
    return "sandbox" if SANDBOX and (SANDBOX / f"{pdf.stem}.tex").exists() else PROJECT_GROUP


def tidy(keep=()):
    """Delete what is no longer worth keeping. Never touches a round any tab still has open
    (once, finishing one round deleted an empty round open in another tab, and every note
    made there afterwards failed to save).
    - a session with no notes (opened, nothing said): gone after 10 minutes
    - a finished project round: audio gone after KEEP_DAYS (the transcripts stay in notes.json)
    - a finished sandbox or other round: the whole folder gone after KEEP_DAYS
    """
    now = time.time()
    keep = {Path(k).resolve() for k in keep if k}
    for d in FEEDBACK.glob("*/*-*"):
        if not d.is_dir() or d.parent.name not in GROUPS or d.resolve() in keep:
            continue
        try:
            notes = json.loads((d / "notes.json").read_text()) if (d / "notes.json").exists() else []
        except ValueError:
            continue
        if not notes:
            if now - d.stat().st_mtime > 600:
                shutil.rmtree(d, ignore_errors=True)
            continue
        done = next((d / f for f in ("PROCESSED", "FINISHED") if (d / f).exists()), None)
        if not done or (now - done.stat().st_mtime) / 86400 < KEEP_DAYS:
            continue
        if d.parent.name == PROJECT_GROUP:
            for f in (d / "audio").glob("*"):
                f.unlink()
            shutil.rmtree(d / "pdfs", ignore_errors=True)  # session.json keeps the commit to rebuild it
        else:
            shutil.rmtree(d, ignore_errors=True)


def synctex_exe():
    return shutil.which("synctex") or "/Library/TeX/texbin/synctex"


def synctex_lookup(pdf: Path, page: int, x: float, y: float):
    """PDF point -> (source file relative to the project, or absolute, line). None if unavailable."""
    exe = synctex_exe()
    if not Path(exe).exists():
        return None
    try:
        out = subprocess.run(
            [exe, "edit", "-o", f"{page}:{x:.2f}:{y:.2f}:{pdf}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    m_in = re.search(r"^Input:(.*)$", out, re.M)
    m_line = re.search(r"^Line:(\d+)", out, re.M)
    if not (m_in and m_line):
        return None
    src = Path(m_in.group(1).strip())
    if not src.is_absolute():
        src = (pdf.parent / src)
    src = Path(os.path.normpath(src))
    return {"file": rel(src), "line": int(m_line.group(1))}


class Session:
    def __init__(self, pdf: Path, new: bool):
        self.pdf = pdf
        root = FEEDBACK / group_for(pdf)
        root.mkdir(parents=True, exist_ok=True)
        stem = pdf.stem
        existing = sorted(p for p in root.glob(f"{stem}-*") if p.is_dir()
                          and not (p / "PROCESSED").exists() and not (p / "FINISHED").exists())
        if existing and not new:
            self.dir = existing[-1]
        else:
            self.dir = root / f"{stem}-{datetime.now():%Y-%m-%d-%H%M%S}"
        self.file = self.dir / "notes.json"
        self.ensure()
        self.snapshot()

    def ensure(self):
        """Make sure the round's folder exists. If something deleted it while a tab had it open,
        recreate it, so notes still save instead of failing silently."""
        (self.dir / "audio").mkdir(parents=True, exist_ok=True)
        meta = self.dir / "session.json"
        if not meta.exists():
            meta.write_text(json.dumps({"pdf": str(self.pdf), "started": datetime.now().isoformat(timespec="seconds")}, indent=2))

    def snapshot(self):
        """Keep a copy of every PDF version this round was reviewed on, so notes never lose their PDF.
        Each note's pdf_mtime names its version: pdfs/<int mtime>.pdf. session.json records the
        git commit (and whether the tree was dirty) so the PDF can also be rebuilt from source.
        Project copies go with the audio after KEEP_DAYS."""
        try:
            mtime = self.pdf.stat().st_mtime
        except OSError:
            return
        self.ensure()
        meta_f = self.dir / "session.json"
        meta = json.loads(meta_f.read_text())
        versions = meta.setdefault("pdf_versions", [])
        if any(v.get("pdf_mtime") == mtime for v in versions):
            return
        copy = self.dir / "pdfs" / f"{int(mtime)}.pdf"
        copy.parent.mkdir(exist_ok=True)
        shutil.copy2(self.pdf, copy)
        git = lambda *a: subprocess.run(["git", "-C", str(self.pdf.parent), *a], capture_output=True, text=True).stdout.strip()
        versions.append({"pdf_mtime": mtime, "copy": str(copy.relative_to(self.dir)),
                         "commit": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--porcelain")),
                         "seen": datetime.now().isoformat(timespec="seconds")})
        meta_f.write_text(json.dumps(meta, indent=2))

    def load(self):
        if self.file.exists():
            return json.loads(self.file.read_text())
        return []

    def save(self, notes):
        self.ensure()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False))
        tmp.replace(self.file)


RECENT = FEEDBACK / "recent.json"
HIDDEN = FEEDBACK / "hidden.json"  # PDFs taken off the review queue with the x, keyed by path -> mtime then


def hidden():
    try:
        return json.loads(HIDDEN.read_text())
    except (OSError, ValueError):
        return {}


def set_hidden(path: str, on: bool):
    h = hidden()
    if on:
        h[path] = Path(path).stat().st_mtime
    else:
        h.pop(path, None)
    HIDDEN.parent.mkdir(exist_ok=True)
    HIDDEN.write_text(json.dumps(h, indent=2))
UPLOADS = FEEDBACK / "_pdfs"


def recent_list():
    try:
        paths = json.loads(RECENT.read_text())
    except (OSError, ValueError):
        paths = []
    out = [p for p in paths if Path(p).exists()]
    builds = sorted(BUILD.glob("*.pdf")) if BUILD else []
    for b in builds:
        if str(b) not in out:
            out.append(str(b))
    return out[:15]


def review_queue():
    """What to review next, for the start screen. Every PDF the reader knows (recently opened, built
    in $FR_BUILD, or with a feedback round) gets a status from its rounds:
    progress = a round with notes, not finished; updated = rebuilt since its last finished round;
    new = no notes yet; done = last round finished, PDF unchanged since. Sorted in that order."""
    rounds = {}
    for meta in FEEDBACK.glob("*/*-*/session.json"):
        d = meta.parent
        try:
            pdf = json.loads(meta.read_text())["pdf"]
            notes = json.loads((d / "notes.json").read_text()) if (d / "notes.json").exists() else []
        except (OSError, ValueError, KeyError):
            continue
        # When Finish round was pressed (FINISHED), not when the agent processed it: a rebuild with
        # the agent's edits comes after processing and must still show as "rebuilt after your round".
        mark = next((d / f for f in ("FINISHED", "PROCESSED") if (d / f).exists()), None)
        rounds.setdefault(pdf, []).append({
            "notes": len(notes), "finished": mark.stat().st_mtime if mark else None,
            "last": (d / "notes.json").stat().st_mtime if notes else meta.stat().st_mtime})
    # A round on a dropped copy (_pdfs/<name>.pdf) counts for the original of the same name, so the
    # queue does not show an older round as the last one.
    known = [p for p in recent_list() + list(rounds) if "_pdfs" not in Path(p).parts]
    for p in [p for p in rounds if "_pdfs" in Path(p).parts]:
        orig = next((k for k in known if Path(k).name == Path(p).name and Path(k).is_file()), None)
        if orig:
            rounds.setdefault(orig, []).extend(rounds.pop(p))
    paths = list(dict.fromkeys(recent_list() + list(rounds)))
    hide = hidden()
    out = []
    for p in paths:
        f = Path(p)
        if not f.is_file() or "_pdfs" in f.parts:
            continue
        rs = sorted(rounds.get(p, []), key=lambda r: r["last"])
        live = [r for r in rs if r["finished"] is None and r["notes"]]
        done = sorted((r for r in rs if r["finished"] is not None and r["notes"]), key=lambda r: r["finished"])
        mtime = f.stat().st_mtime
        if p in hide and mtime <= hide[p] + 1:
            continue  # taken off the list; comes back once the PDF is rebuilt
        if live:
            item = {"status": "progress", "notes": live[-1]["notes"], "when": live[-1]["last"]}
        elif done and mtime > done[-1]["finished"] + 60:
            item = {"status": "updated", "when": done[-1]["finished"]}
        elif done:
            item = {"status": "done", "when": done[-1]["finished"]}
        elif group_for(f) == "sandbox":
            continue  # test documents only show up once there is a round on them
        else:
            item = {"status": "new", "when": mtime}
        out.append({"path": p, "built": mtime, **item})
    order = {"progress": 0, "updated": 1, "new": 2, "done": 3}
    out.sort(key=lambda i: (order[i["status"]], -i["when"]))
    return out


def remember(pdf: Path):
    paths = [str(pdf)] + [p for p in recent_list() if p != str(pdf) and not (BUILD and p.startswith(str(BUILD)))]
    RECENT.parent.mkdir(exist_ok=True)
    RECENT.write_text(json.dumps(paths[:12], indent=2))


class Worker:
    """One background thread that transcribes each clip as soon as its note is saved, so Finish round
    does not wait for Whisper. Jobs run in order: a round's own job (process():
    leftovers, source lines, feedback.md) always runs after its clips. While it is queued or running
    the round holds a PROCESSING file with this server's pid; process.py waits for it."""

    def __init__(self):
        self.q, self.cv, self.busy = [], threading.Condition(), None
        self.glossaries = {}
        threading.Thread(target=self._run, daemon=True).start()

    def clip(self, sess: Path, nid: str):
        with self.cv:
            self.q.append(("clip", sess, nid))
            self.cv.notify()

    def round(self, sess: Path):
        (sess / "PROCESSING").write_text(str(os.getpid()))
        with self.cv:
            self.q.append(("round", sess, None))
            self.cv.notify()

    def left(self, sess: Path) -> int:
        """Clips of this round still waiting for Whisper (including the one running now)."""
        with self.cv:
            jobs = self.q + ([self.busy] if self.busy else [])
            return sum(1 for k, s, _ in jobs if k == "clip" and s == sess)

    def _glossary(self, sess: Path):
        if sess not in self.glossaries:
            from process import prepare_glossary
            self.glossaries[sess] = prepare_glossary(sess)
        return self.glossaries[sess]

    def _run(self):
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                self.busy = self.q.pop(0)
            kind, sess, nid = self.busy
            try:
                if kind == "clip":
                    self._clip(sess, nid)
                else:
                    from process import process
                    process(sess)
            except Exception as e:  # never kill the worker; process.py redoes whatever is missing
                print(f"worker: {kind} {sess.name} {nid or ''}: {e}", file=sys.stderr, flush=True)
            finally:
                if kind == "round":
                    (sess / "PROCESSING").unlink(missing_ok=True)
                self.busy = None

    def _clip(self, sess: Path, nid: str):
        from process import transcribe
        f = sess / "notes.json"
        with LOCK:
            n = next((n for n in json.loads(f.read_text()) if n["id"] == nid), None) if f.exists() else None
        if not n or not n.get("audio") or "transcript" in n or not (sess / "audio" / n["audio"]).exists():
            return
        text = transcribe(sess / "audio" / n["audio"], self._glossary(sess))
        with LOCK:  # the file may have changed while Whisper ran: write only this note's transcript
            notes = json.loads(f.read_text())
            for m in notes:
                if m["id"] == nid:
                    m["transcript"] = text
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(notes, indent=2, ensure_ascii=False))
            tmp.replace(f)


ALIVE = 150  # s since a tab last polled: browsers slow hidden tabs' timers to about once a minute


class Tabs:
    """Each browser tab shows its own PDF and round (?tab=<id> on every request), so a second document
    opens beside one that already has notes instead of replacing it."""

    def __init__(self, default: Path | None = None):
        self.tabs = {}      # id -> {"s": Session or None, "seen": time, "busy": note open, "queue": show the queue}
        self.sessions = {}  # PDF path -> Session: tabs on the same PDF share one round
        self.default = default
        self.lock = threading.Lock()

    def dirs(self):
        with self.lock:
            return [s.dir for s in self.sessions.values()] + [t["s"].dir for t in self.tabs.values() if t["s"]]

    def session_for(self, pdf: Path):
        s = self.sessions.get(str(pdf))
        if s is None or any((s.dir / f).exists() for f in ("FINISHED", "PROCESSED")):
            s = self.sessions[str(pdf)] = Session(pdf, new=False)
        return s

    def get(self, tid: str, pdf_hint: str = ""):
        with self.lock:
            t = self.tabs.get(tid)
            if t is None:
                # A new tab, or a tab from before a restart: it says which PDF it had.
                t = self.tabs[tid] = {"s": None, "seen": 0, "busy": False, "queue": False}
                p = Path(pdf_hint) if pdf_hint else self.default
                self.default = None
                if p and p.is_file():
                    t["s"] = self.session_for(p)
            return t

    def open(self, tid: str, pdf: Path):
        pdf = pdf.expanduser().resolve()
        if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
            raise ValueError(f"not a PDF: {pdf}")
        t = self.get(tid)
        with LOCK:
            t["s"] = self.session_for(pdf)
        remember(pdf)
        if str(pdf) in hidden():
            set_hidden(str(pdf), False)
        return t["s"]

    def finished(self, old, new):
        with self.lock:
            self.sessions[str(new.pdf)] = new
            for t in self.tabs.values():
                if t["s"] is old:
                    t["s"] = new

    def route(self, pdf: Path | None):
        """Which tab open-reader should use: a tab already on this document (the same PDF, or an
        earlier render of the same draft: md-to-reader stamps each render with the time),
        else a new one. A tab on a different PDF is never taken over, notes or not: separate
        documents get separate tabs.
        Returns (tab id, whether it is new)."""
        now = time.time()
        with self.lock:
            alive = sorted(((k, t) for k, t in self.tabs.items() if now - t["seen"] < ALIVE), key=lambda kt: -kt[1]["seen"])
        if pdf:
            for k, t in alive:
                if t["s"] and t["s"].pdf == pdf:
                    return k, False
            for k, t in alive:
                if t["s"] and not t["busy"] and not t["s"].load() and same_draft(t["s"].pdf, pdf):
                    return k, False
        # No reuse of "empty" tabs: after a restart a tab can look empty until it reports its PDF, and
        # reusing it swapped one document for another. A new tab costs nothing.
        k = "t" + os.urandom(4).hex()
        self.get(k)
        return k, True


def same_draft(a: Path, b: Path) -> bool:
    """Two renders of one draft: same folder, same name once the -YYYY-MM-DD-HHMMSS stamp is cut."""
    cut = lambda p: re.sub(r"-\d{4}-\d{2}-\d{2}-\d{6}$", "", p.stem)
    return a.parent == b.parent and cut(a) == cut(b)


def make_handler(tabs: Tabs, worker: Worker):
    # Every request names its tab (?tab=<id>); each tab has its own Session (None until a PDF is
    # opened). Finishing a round or opening another PDF swaps in a different one. Notes are on disk.
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def _tab(self, q):
            tid = q.get("tab", "default")
            if not SAFE_ID.match(tid):
                tid = "default"
            t = tabs.get(tid, q.get("pdf", ""))
            t["seen"] = time.time()
            return tid, t

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/queue":
                return self._send(200, review_queue())
            tid, t = self._tab(q)
            session = t["s"]
            if u.path == "/api/state":
                t["busy"] = q.get("busy") == "1"
                show_queue, t["queue"] = t["queue"], False
                if not session:
                    return self._send(200, {"pdf": None, "tab": tid, "show_queue": show_queue, "recent": recent_list(), "synctex": Path(synctex_exe()).exists()})
                has_sync = session.pdf.with_suffix(".synctex.gz").exists() or session.pdf.with_suffix(".synctex").exists()
                return self._send(200, {
                    "pdf": session.pdf.name,
                    "tab": tid,
                    "show_queue": show_queue,
                    "group": session.dir.parent.name,
                    "pdf_path": str(session.pdf),
                    "pdf_mtime": session.pdf.stat().st_mtime,
                    "session": session.dir.name,
                    "notes": session.load(),
                    "synctex": has_sync and Path(synctex_exe()).exists(),
                    "recent": recent_list(),
                })
            if not session:
                return self._send(404, {"error": "no PDF open"})
            if u.path.startswith("/audio/"):
                name = u.path[len("/audio/"):]
                stem, _, ext = name.rpartition(".")
                f = session.dir / "audio" / name
                if not SAFE_ID.match(stem) or ext not in SAFE_EXT or not f.exists():
                    return self._send(404, {"error": "no audio"})
                return self._send(200, f.read_bytes(), {"mp4": "audio/mp4", "ogg": "audio/ogg", "wav": "audio/wav", "m4a": "audio/mp4"}.get(ext, "audio/webm"))
            if u.path == "/doc.pdf":
                return self._send(200, session.pdf.read_bytes(), "application/pdf")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/hide":
                # The x on the review queue: off the list until the PDF changes or is opened again.
                path = json.loads(self._body() or b"{}").get("path", "")
                if not Path(path).is_file():
                    return self._send(400, {"error": "no such PDF"})
                set_hidden(path, True)
                return self._send(200, {"ok": True})
            if u.path == "/api/route":
                # open-reader asks where a PDF (or the queue, with no path) should go. See Tabs.route.
                path = json.loads(self._body() or b"{}").get("path") or ""
                pdf = Path(path).expanduser().resolve() if path else None
                tid, new = tabs.route(pdf)
                try:
                    if pdf:
                        tabs.open(tid, pdf)
                    else:
                        tabs.get(tid)["queue"] = True
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"tab": tid, "new": new})
            tid, t = self._tab(q)
            session = t["s"]
            if u.path == "/api/bye":
                t["seen"] = 0  # tab closed or reloading: not a target for open-reader, but kept for a reload
                return self._send(200, {"ok": True})
            if u.path == "/api/open":
                # Open a PDF by path (the queue, Raycast, Finder). Keeps SyncTeX if it is there.
                try:
                    s = tabs.open(tid, Path(json.loads(self._body() or b"{}").get("path", "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"ok": True, "pdf": s.pdf.name, "session": s.dir.name})
            if u.path == "/api/upload":
                # A PDF dropped onto the page: the browser only gives bytes, not a path, so keep a copy.
                name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(q.get("name", "dropped.pdf")).name)
                if not name.lower().endswith(".pdf"):
                    return self._send(400, {"error": "only PDFs"})
                data = self._body()
                # The same bytes as a PDF the reader knows (a project build dragged in from Finder): open
                # the original, so the round keeps SyncTeX source lines and its group.
                for known in [*recent_list(), *(map(str, BUILD.glob("*.pdf")) if BUILD else [])]:
                    k = Path(known)
                    if k.name == name and k.is_file() and k.stat().st_size == len(data) and k.read_bytes() == data:
                        s = tabs.open(tid, k)
                        return self._send(200, {"ok": True, "pdf": s.pdf.name, "session": s.dir.name, "original": str(k)})
                UPLOADS.mkdir(parents=True, exist_ok=True)
                dest = UPLOADS / name
                dest.write_bytes(data)
                s = tabs.open(tid, dest)
                return self._send(200, {"ok": True, "pdf": s.pdf.name, "session": s.dir.name})
            if not session:
                return self._send(400, {"error": "no PDF open"})
            if u.path == "/api/external":
                # Open the current PDF in the system's default PDF viewer, e.g. to follow links.
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(session.pdf)])
                return self._send(200, {"ok": True})
            if u.path == "/api/finish":
                # Close the round at once and hand it on: the worker finishes the transcripts and
                # writes feedback.md in the background, and process.py (run by the skill) waits for it.
                notes = session.load()
                ids = {n["id"] for n in notes}
                kept = sum(1 for n in notes if n.get("parent") not in ids and any(
                    m.get("typed") or m.get("audio") for m in notes if m is n or m.get("parent") == n["id"]))
                with LOCK:
                    done = session.dir
                    (done / "FINISHED").touch()
                    worker.round(done)
                    new = Session(session.pdf, new=True)
                tabs.finished(session, new)
                tidy([new.dir, *tabs.dirs()])
                if PROJECT and done.parent.name in (PROJECT_GROUP, "sandbox"):
                    command, where = f"{PROJECT_COMMAND} {rel(done)}", f"in a Claude Code session in {PROJECT.name}"
                else:
                    command, where = f"{COMMAND} {done}", "in any Claude Code session"
                return self._send(200, {"kept": kept, "finished": done.name, "new_session": new.dir.name,
                                        "left": worker.left(done), "command": command, "where": where})
            if u.path == "/api/note":
                note = json.loads(self._body() or b"{}")
                if not SAFE_ID.match(str(note.get("id", ""))):
                    return self._send(400, {"error": "bad id"})
                if note.get("kind") == "anchored" and "source" not in note:
                    note["source"] = synctex_lookup(session.pdf, int(note["page"]), float(note["x"]), float(note["y"]))
                note["pdf_mtime"] = session.pdf.stat().st_mtime
                session.snapshot()
                with LOCK:
                    notes = [n for n in session.load() if n["id"] != note["id"]]
                    notes.append(note)
                    notes.sort(key=lambda n: n.get("created", ""))
                    session.save(notes)
                if note.get("audio") and "transcript" not in note:
                    worker.clip(session.dir, note["id"])
                return self._send(200, note)
            if u.path == "/api/audio":
                nid, ext = q.get("id", ""), q.get("ext", "webm")
                if not SAFE_ID.match(nid) or ext not in SAFE_EXT:
                    return self._send(400, {"error": "bad id or ext"})
                session.ensure()
                (session.dir / "audio" / f"{nid}.{ext}").write_bytes(self._body())
                return self._send(200, {"ok": True})
            if u.path == "/api/delete":
                # One note (?id=) or several ({"ids": [...]}). Additions to a note go with it.
                body = json.loads(self._body() or b"{}")
                ids = set(body.get("ids") or ([q["id"]] if q.get("id") else []))
                if not ids or not all(SAFE_ID.match(str(i)) for i in ids):
                    return self._send(400, {"error": "bad id"})
                with LOCK:
                    notes = session.load()
                    gone = {n["id"] for n in notes if n["id"] in ids or n.get("parent") in ids}
                    session.save([n for n in notes if n["id"] not in gone])
                for nid in gone:
                    for f in (session.dir / "audio").glob(f"{nid}.*"):
                        f.unlink()
                return self._send(200, {"ok": True, "deleted": sorted(gone)})
            return self._send(404, {"error": "not found"})

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdf", type=Path, nargs="?", help="PDF to open (optional: drop one onto the page instead)")
    ap.add_argument("--new", action="store_true", help="start a new session instead of resuming")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    # open-reader appends to one log across restarts: stamp each start so a crash stays traceable.
    print(f"--- start {datetime.now():%Y-%m-%d %H:%M:%S} pid {os.getpid()} port {a.port}", flush=True)
    tabs = Tabs()
    worker = Worker()
    # Bind before touching any round: a second copy must not resume rounds the first one owns.
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(tabs, worker))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"port {a.port} is already in use (another reader running?), not starting a second one")
        raise
    first = None
    if a.pdf:
        pdf = a.pdf.resolve()
        if not pdf.exists():
            sys.exit(f"no such PDF: {pdf}")
        first = Session(pdf, a.new)
        tabs.sessions[str(pdf)] = first
        tabs.default = pdf  # the first tab to connect shows it
        remember(pdf)
    tidy([first.dir] if first else [])
    # Clips saved before a restart that never got a transcript, and rounds left half-processed.
    for meta in FEEDBACK.glob("*/*-*/session.json"):
        d = meta.parent
        if (d / "PROCESSING").exists():
            worker.round(d)
    url = f"http://127.0.0.1:{a.port}/"
    where = first.dir if first else "no PDF yet"
    print(f"session {where}  ->  {url}  (Ctrl-C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped. Notes are saved as you go.")


if __name__ == "__main__":
    main()
