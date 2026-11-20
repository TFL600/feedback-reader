#!/usr/bin/env python3
"""Batch-transcribe a feedback session and write feedback.md for Claude.

    python3 reader/process.py               # latest round
    python3 reader/process.py <round dir>   # a given one

The reader transcribes each clip in the background as soon as it is saved, and Finish round
queues this same processing for the round (marked by a PROCESSING file holding the reader's pid).
Run from the command line, this waits for that to finish, then completes whatever is missing,
so it is always safe to run and quick when the reader got there first.

For every note: transcribe its audio (bin/whisper-transcribe: whisper.cpp, biased by the
document's glossary plus the dictation dictionary, then the dictionary's corrections), look up
the SyncTeX source line if missing, and read that line from the .tex file. With one sentence
per line, that line is the sentence that was clicked.

Writes transcripts back into notes.json and a compact feedback.md: one block per
note with location, the source sentence, the PDF text under the click, what was
typed and what was said. That file is all the agent reads (skills/apply-feedback), so a
round of 30 notes costs a few thousand tokens, not audio or video.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from config import BIN, FEEDBACK, GLOSSARY, PROJECT, in_project, rel, resolve_src  # noqa: E402
from server import synctex_lookup  # noqa: E402

WHISPER_SH = BIN / "whisper-transcribe"


def latest_session():
    s = sorted((p for p in FEEDBACK.glob("*/*-*") if (p / "notes.json").exists()), key=lambda p: p.name[-17:])
    if not s:
        sys.exit("no feedback sessions yet")
    return s[-1]


def transcribe(audio: Path, glossary: Path | None = None) -> str:
    env = dict(os.environ, WHISPER_LANG=os.environ.get("WHISPER_LANG", "en"))
    gl = glossary or GLOSSARY
    if gl and gl.exists():
        env["WHISPER_EXTRA"] = str(gl)
    r = subprocess.run(["bash", str(WHISPER_SH), str(audio)], capture_output=True, text=True, env=env)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "whisper failed")
    text = " ".join(r.stdout.split())
    # Whisper echoes its prompt on silence, and turns a breath or a click into "S." or "Thank you.".
    if text.startswith("Glossary:") or text in {"[BLANK_AUDIO]", "(silence)", "Thank you."}:
        return ""
    if len(re.sub(r"\W", "", text)) < 4:
        return ""
    return text


def _norm(s):
    """Crude detex for matching PDF text against source: drop commands, braces, spaces, case."""
    s = re.sub(r"\\[a-zA-Z]+\*?", "", s)
    return re.sub(r"[\s{}~$\\^_]", "", s).lower()


def refine_source(n):
    """SyncTeX resolves to the PDF line box, not the word. When a printed line holds the end of
    one sentence and the start of the next, use the clicked word and the words after it to pick
    the right source line (one sentence per line) among the neighbours."""
    src, text, word = n.get("source"), n.get("line_text", ""), n.get("word", "")
    if not (src and text and word):
        return src
    i = n.get("word_offset")
    if i is None or text[i:i + len(word)] != word:
        i = text.find(word)
    if i < 0:
        return src
    probe = _norm(" ".join(text[i:].split()[:3]))
    if len(probe) < 6:
        return src
    f = resolve_src(src["file"])
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return src
    L = src["line"]
    for cand in sorted(range(max(1, L - 3), min(len(lines), L + 3) + 1), key=lambda c: abs(c - L)):
        if probe in _norm(lines[cand - 1]):
            return {"file": src["file"], "line": cand, **({"synctex_line": L} if cand != L else {})}
    return src


def source_line(src):
    if not src:
        return ""
    f = resolve_src(src["file"])
    try:
        return f.read_text().splitlines()[src["line"] - 1].strip()
    except (OSError, IndexError):
        return ""


def wait_for_reader(sess: Path):
    """Block while the reader's background worker still holds this round (PROCESSING, its pid)."""
    f, said = sess / "PROCESSING", False
    while f.exists():
        try:
            os.kill(int(f.read_text().strip()), 0)
        except PermissionError:
            pass  # alive, another user's process (cannot happen here, but it is not dead)
        except (ValueError, OSError):
            return  # the reader is gone: do the work here
        if not said:
            print("waiting for the reader's background transcription...", file=sys.stderr, flush=True)
            said = True
        time.sleep(1)


def main():
    sess = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else latest_session()
    wait_for_reader(sess)
    kept = process(sess)
    print(f"{sess}/feedback.md · {kept} notes with content")


def prepare_glossary(sess: Path) -> Path | None:
    """Refresh the project glossary; for a PDF from elsewhere (a paper draft), its own jargon goes
    first in Whisper's prompt (sess/glossary.txt). None means: use the project glossary."""
    pdf = Path(json.loads((sess / "session.json").read_text())["pdf"])
    if PROJECT:
        subprocess.run([sys.executable, str(HERE / "glossary.py")], capture_output=True)
    if not in_project(pdf) or "_pdfs" in pdf.parts:
        from glossary import pdf_terms
        terms = pdf_terms(pdf)
        if terms:
            glossary = sess / "glossary.txt"
            glossary.write_text("\n".join(terms) + "\n")
            return glossary
    return None


def process(sess: Path) -> int:
    """Transcribe, resolve sources, write feedback.md. Returns the number of notes with content."""
    meta = json.loads((sess / "session.json").read_text())
    pdf = Path(meta["pdf"])
    notes = json.loads((sess / "notes.json").read_text())
    glossary = prepare_glossary(sess) if any(n.get("audio") and "transcript" not in n for n in notes) else None

    for i, n in enumerate(notes, 1):
        if n.get("audio") and "transcript" not in n:
            a = sess / "audio" / n["audio"]
            print(f"[{i}/{len(notes)}] transcribing {a.name}", file=sys.stderr)
            n["transcript"] = transcribe(a, glossary) if a.exists() else ""
        if n.get("kind") == "anchored":
            if not n.get("source"):
                n["source"] = synctex_lookup(pdf, n["page"], n["x"], n["y"])
            n["source"] = refine_source(n)
    (sess / "notes.json").write_text(json.dumps(notes, indent=2, ensure_ascii=False))

    out = [f"# Feedback {sess.name}", "", f"PDF: `{rel(pdf)}`, {len(notes)} notes. "
           "Source = SyncTeX line (one sentence per line). Clicked = PDF text under the pin. "
           "Typed and Said are the author's words; Said is a local transcript and may mishear terms.", ""]
    # Additions ("Add to this note": a second clip or typed text) are printed under their note.
    ids = {n["id"] for n in notes}
    adds = {}
    for n in notes:
        if n.get("parent") in ids:
            adds.setdefault(n["parent"], []).append(n)
    kept = 0
    for n in notes:
        if n.get("parent") in ids:
            continue
        group = [n] + adds.get(n["id"], [])
        if not any(m.get("transcript") or m.get("typed") for m in group):
            continue
        kept += 1
        if n.get("kind") == "general":
            out.append(f"## {kept} · general")
        else:
            src = n.get("source")
            loc = f"{src['file']}:{src['line']}" if src else "no source line"
            out.append(f"## {kept} · {loc} · p{n['page']}")
            line = source_line(src)
            if line:
                out.append(f"Source: {line}")
            if n.get("line_text"):
                out.append(f"Clicked: “{n['line_text']}”" + (f" (word: {n['word']})" if n.get("word") else ""))
        for j, m in enumerate(group):
            tag = " (added)" if j else ""
            if m.get("typed"):
                out.append(f"Typed{tag}: {m['typed']}")
            if m.get("transcript"):
                out.append(f"Said{tag}: {m['transcript']}")
        out.append("")
    (sess / "feedback.md").write_text("\n".join(out))
    return kept


if __name__ == "__main__":
    main()
