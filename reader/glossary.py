#!/usr/bin/env python3
"""Project glossary for dictation: terms Whisper is likely to get wrong, ranked by use.

    python3 reader/glossary.py            # writes $FR_GLOSSARY from the .tex files in $FR_PROJECT

Picks up acronyms (MLIP, DFT), chemical formulas (CsPbI3, \\ce{...}), mixed-case names
(NequIP), hyphenated proper names (Huang-Rhys) and \\newacronym / \\gls entries, plus
anything listed by hand in $FR_GLOSSARY_EXTRA (always first). Feeds the
feedback reader's transcription as priority terms ahead of the global dictionary.
"""
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BUILD, FEEDBACK, GLOSSARY as OUT, GLOSSARY_DIRS, GLOSSARY_EXTRA as EXTRA, PROJECT  # noqa: E402
SKIP = {"I", "A", "II", "III", "IV", "PDF", "URL", "TODO", "LaTeX", "TeX", "Fig", "Eq", "Ref", "Table", "Figure"}

PATTERNS = [
    re.compile(r"\\ce\{([^}]+)\}"),                                  # \ce{CsPbI3}
    re.compile(r"\\newacronym(?:\[[^\]]*\])?\{[^}]*\}\{([^}]+)\}"),  # \newacronym{key}{SHORT}{long}
    re.compile(r"\b((?:[A-Z][a-z]?\d*){2,}\d)\b"),                   # formulas ending in a digit: CsPbI3, MAPbBr3
    re.compile(r"\b([A-Z]{2,}[a-z]*[A-Z0-9]*)\b"),                    # acronyms: DFT, MLIP, NequIP-ish
    re.compile(r"\b([A-Z][a-z]+[A-Z][A-Za-z]*)\b"),                   # mixed case: NequIP, LaTeX
    re.compile(r"\b([A-Z][a-z]+-[A-Z][a-z]+)\b"),                     # Huang-Rhys, Ruddlesden-Popper
]


def strip_comments(t):
    return re.sub(r"(?<!\\)%.*", "", t)


def count_terms(text, c=None):
    """Add the jargon in a piece of text (LaTeX source or plain PDF text) to a Counter."""
    c = Counter() if c is None else c
    for p in PATTERNS:
        for m in p.findall(text):
            term = m.strip()
            if term and term not in SKIP and len(term) > 1:
                c[term] += 1
    return c


def pdf_terms(pdf, limit=40):
    """Jargon of any PDF (a paper draft, a co-author's document), most used first. Text via
    pdftotext (poppler) or, failing that, macOS PDFKit; returns [] if neither works. Used for
    feedback rounds on PDFs that are not built from the project."""
    import json
    import subprocess
    if shutil.which("pdftotext"):
        cmd = ["pdftotext", "-q", str(pdf), "-"]
    else:  # macOS PDFKit through JXA: no Python dependency, works with any interpreter.
        js = f'ObjC.import("PDFKit"); var d = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath({json.dumps(str(pdf))})); d ? d.string.js : ""'
        cmd = ["osascript", "-l", "JavaScript", "-e", js]
    try:
        text = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # words hyphenated across lines
    return [t for t, n in count_terms(text).most_common() if n >= 2][:limit]


def project_tex():
    """Every .tex file in the project (or in FR_GLOSSARY_DIRS), minus build output and feedback rounds."""
    skip = [p for p in (BUILD, FEEDBACK) if p]
    roots = [PROJECT / d for d in GLOSSARY_DIRS] or [PROJECT]
    for f in (f for r in roots for f in sorted(r.rglob("*.tex"))):
        if any(part.startswith(".") for part in f.relative_to(PROJECT).parts):
            continue
        if any(f.is_relative_to(s) for s in skip):
            continue
        yield f


def main():
    if not PROJECT:
        sys.exit("set FR_PROJECT to the root of a LaTeX project")
    c = Counter()
    for f in project_tex():
        t = strip_comments(f.read_text(errors="ignore"))
        t = re.sub(r"\\(?:begin|end|usepackage|documentclass|label|ref|cite[a-z]*|includegraphics)\*?(\[[^\]]*\])?\{[^}]*\}", " ", t)
        count_terms(t, c)
    extra = [l.strip() for l in EXTRA.read_text().splitlines() if l.strip() and not l.startswith("#")] if EXTRA.exists() else []
    terms = extra + [t for t, _ in c.most_common() if t not in extra]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(terms) + "\n")
    print("\n".join(terms))


if __name__ == "__main__":
    main()
