#!/usr/bin/env python3
"""Build a review PDF of what changed since a commit, and open it in the feedback reader.

    python3 reader/review.py main.tex --rev HEAD~1        # the whole document, flattened
    python3 reader/review.py chapters/intro.tex --rev 56becac

Old text struck through in red, new text in blue (latexdiff). Paths are relative to
$FR_PROJECT. Writes $FR_BUILD/review/review-<stem>.pdf with SyncTeX, then tells the running
reader to open it, so the next thing the author sees is the changes. Reviewing: no note on a
change accepts it; a note on it ("reject", "keep the old one", or a new instruction) goes back
to the agent like any other round.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BUILD, PORT, PROJECT  # noqa: E402

if not PROJECT:
    sys.exit("set FR_PROJECT to the root of the LaTeX project (a git repo)")
REPO = PROJECT
OUT = BUILD / "review"
TEXBIN = "/Library/TeX/texbin"  # MacTeX; added to PATH only if it exists


def run(cmd, **kw):
    path = os.environ.get("PATH", "")
    env = dict(os.environ, PATH=f"{TEXBIN}:{path}" if Path(TEXBIN).is_dir() else path)
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, errors="replace", **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tex", help="document to diff, relative to the project (main.tex, chapters/intro.tex)")
    ap.add_argument("--rev", default="HEAD~1", help="commit to compare the working tree against")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    tex = Path(a.tex)
    old_root = OUT / "old"
    shutil.rmtree(old_root, ignore_errors=True)
    old_root.mkdir(parents=True)
    # The old version of every file (chapters included), so \input/\include flatten correctly.
    arch = subprocess.run(["git", "archive", a.rev], cwd=REPO, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(arch)) as t:
        t.extractall(old_root, filter="data")

    name = f"review-{tex.stem}"
    diff_tex = OUT / f"{name}.tex"
    r = run(["latexdiff", "--flatten", str(old_root / tex), str(tex)])
    if r.returncode:
        sys.exit("latexdiff failed:\n" + r.stderr[-2000:])
    diff_tex.write_text(r.stdout)

    # Compile from the repo root so the class, figures and bibliography resolve as usual.
    r = run(["latexmk", "-pdf", f"-outdir={OUT}", "-synctex=1", "-interaction=nonstopmode", "-f", os.path.relpath(diff_tex, REPO)])
    pdf = OUT / f"{name}.pdf"
    if not pdf.exists():
        sys.exit("build failed, see " + str(OUT / f"{name}.log"))
    shutil.rmtree(old_root, ignore_errors=True)
    print(pdf)

    if not a.no_open:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/open", data=json.dumps({"path": str(pdf)}).encode(), method="POST")
            urllib.request.urlopen(req, timeout=3)
            print("opened in the feedback reader")
        except OSError:
            print(f"reader not running: open it with bin/open-reader {pdf}")


if __name__ == "__main__":
    main()
