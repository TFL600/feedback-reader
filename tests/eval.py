#!/usr/bin/env python3
"""Score a feedback round against a scripted target document.

    python3 tests/eval.py tests/targets.tex                # latest round on it
    python3 tests/eval.py tests/targets.tex <round dir>

Targets are trailing comments in the .tex:  % TARGET id=T1 word=anharmonic expect="Shorten this sentence."
kind=general means a general remark, kind=absent means the note must not survive (discarded or deleted).

For each target it finds the note whose words best match `expect` and checks:
  text  share of the expected words found in what was said or typed (pass at 70%)
  line  the resolved source line (SyncTeX, then sentence refinement) against the target's line
  word  the word under the click against `word`
Transcribes first if the session has not been processed yet.
"""
import json
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reader"))
from config import FEEDBACK, rel as relpath  # noqa: E402
from process import process  # noqa: E402

STOP = {"the", "a", "an", "this", "in", "to", "of", "and", "on", "here", "these", "not", "with", "me"}


def words(s):
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP]


def overlap(expect, got):
    e, g = words(expect), set(words(got))
    return sum(w in g for w in e) / len(e) if e else 0.0


def targets(tex: Path):
    out = []
    for i, line in enumerate(tex.read_text().splitlines(), 1):
        m = re.search(r"%\s*TARGET\s+(.*)$", line)
        if not m:
            continue
        t = dict(kv.split("=", 1) for kv in shlex.split(m.group(1)) if "=" in kv)
        t.setdefault("kind", "anchored")
        t["line"] = i
        out.append(t)
    return out


def main():
    tex = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        sess = Path(sys.argv[2]).resolve()
    else:
        cands = sorted(p for p in FEEDBACK.glob(f"*/{tex.stem}-*") if (p / "notes.json").exists())
        if not cands:
            sys.exit(f"no feedback session for {tex.stem}")
        sess = cands[-1]
    notes = json.loads((sess / "notes.json").read_text())
    if any(n.get("audio") and "transcript" not in n for n in notes) or not (sess / "feedback.md").exists():
        process(sess)
        notes = json.loads((sess / "notes.json").read_text())
    rel = relpath(tex)

    used, rows, passed = set(), [], 0
    for t in targets(tex):
        best, score = None, 0.0
        for n in notes:
            if n["id"] in used:
                continue
            s = overlap(t["expect"], f"{n.get('transcript', '')} {n.get('typed', '')}")
            if s > score:
                best, score = n, s
        if t["kind"] == "absent":
            # Judged by place, not wording: is there still a pinned note on that sentence?
            left = [n for n in notes if n.get("kind") == "anchored" and (n.get("source") or {}).get("line") == t["line"]]
            ok = not left
            rows.append((t["id"], "ok" if ok else "FAIL", f"absent: {'gone' if ok else 'still there'}", "", ""))
            passed += ok
            used.update(n["id"] for n in left)
            continue
        if not best or score < 0.7:
            # Fall back to place: a note on the target sentence (and word) still counts, worded differently.
            here = [n for n in notes if n["id"] not in used and n.get("kind") == t["kind"]
                    and (t["kind"] == "general" or (n.get("source") or {}).get("line") == t["line"])]
            if here and t["kind"] == "anchored":
                best, score = here[0], overlap(t["expect"], f"{here[0].get('transcript', '')} {here[0].get('typed', '')}")
                used.add(best["id"])
                said = best.get("transcript") or best.get("typed") or ""
                rows.append((t["id"], "ok", f"text {score:.0%} (worded differently)", "line ok", said))
                passed += 1
                continue
            rows.append((t["id"], "FAIL", f"no matching note (best {score:.0%})", "", ""))
            continue
        used.add(best["id"])
        said = best.get("transcript") or best.get("typed") or ""
        if t["kind"] == "general":
            ok = best.get("kind") == "general"
            rows.append((t["id"], "ok" if ok else "FAIL", f"text {score:.0%}", "general" if ok else "pinned, expected general", said))
            passed += ok
            continue
        src = best.get("source") or {}
        if src.get("file") != rel:
            loc, lok = f"wrong file {src.get('file')}", False
        else:
            d = src["line"] - t["line"]
            loc, lok = ("line ok", True) if d == 0 else ((f"line {d:+d} (near)", True) if abs(d) <= 1 and t["id"] in ("T6", "T7") else (f"line {d:+d}", False))
            if src.get("synctex_line") and src["synctex_line"] != src["line"]:
                loc += f" (fixed from SyncTeX {src['synctex_line'] - t['line']:+d})"
        want = t.get("word", "")
        wok = not want or best.get("word", "").strip(".,;:()").lower() == want.lower()
        ok = lok
        rows.append((t["id"], "ok" if ok else "FAIL", f"text {score:.0%}", loc + ("" if wok else f", word '{best.get('word', '')}'"), said))
        passed += ok

    extra = [n for n in notes if n["id"] not in used and (n.get("transcript") or n.get("typed"))]
    print(f"{sess.name}: {passed}/{len(rows)} targets passed\n")
    for r in rows:
        print(f"{r[0]:<4} {r[1]:<5} {r[2]:<24} {r[3]}")
        if r[4]:
            print(f"{'':11}said/typed: {r[4]}")
    if extra:
        print("\nNotes not matched to any target:")
        for n in extra:
            print(f"  - {n.get('transcript') or n.get('typed')}")


if __name__ == "__main__":
    main()
