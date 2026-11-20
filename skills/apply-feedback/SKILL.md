---
name: apply-feedback
description: Applies a round of spoken click-to-dictate feedback from the feedback reader to the document it was given on. Transcribes what is left, maps every pin to its source sentence (SyncTeX for LaTeX, the clicked text otherwise), makes the edits, asks only about the genuinely ambiguous ones, logs every before/after pair and learns dictionary fixes. Use when the user pastes `/apply-feedback <round dir>` (copied by the reader's Finish round button), says "apply my feedback" or "process my notes", or has just finished a reader round.
---

# apply-feedback

The reader (`reader/server.py`) stores pins, typed text and audio. This skill turns one finished round
into edits. It reads text only: `feedback.md` is a few thousand tokens per round.

`FR_READER` below is the folder this repo is cloned in. `install.sh` fills it in when it installs this skill.

## Steps

1. **Process first.** Finish round copies `/apply-feedback <round dir>` at once and the reader keeps
   transcribing in the background (a `PROCESSING` file marks a round it is still on), so always run
   `python3 $FR_READER/reader/process.py <round dir>` before reading anything. It waits for the
   reader, transcribes what is left, fills SyncTeX source lines (corrected to the clicked sentence)
   and writes `<round dir>/feedback.md`. Read that, not `notes.json`. `session.json` names the PDF.
   Lines marked `(added)` are a follow-up recorded on the same note: read them together.
2. **Route by document.**
   - **LaTeX with SyncTeX** (notes have a `Source:` file and line): edit the source at that line.
   - **Markdown drafts** made by `md-to-reader`: `<pdf>.sources` lists the markdown files. Edit them
     at the clicked text (grep a distinctive fragment), then rerun `md-to-reader` for another round.
   - **Email drafts** (PDF in a folder named `email-drafts`): `p<N>` is the Nth email in the source file.
   - **A review PDF** (`review-*.pdf`, made by `reader/review.py`): every note is a verdict on a change.
     "Reject", "undo", "keep the old one" restore the red struck-out text. Anything else is a new
     instruction. A change with no note is accepted. Find the real source line by grepping the blue
     (new) text.
   - **A scripted test round** on `tests/targets.tex`: run `tests/eval.py` and report its score
     instead of editing anything.
   - **Anything else** (a Word export, a co-author's PDF, no source): group notes by page and clicked
     text, propose concrete edits in chat, and edit the source only if asked.
3. **Locate each note.** With one sentence per source line, `Source:` is usually the sentence meant.
   Check it against `Clicked:`: SyncTeX can be a line or two off around equations, figures and
   captions. If they disagree, grep for the clicked text and use that. A pin between two sentences
   or at a paragraph end may mean the paragraph: read the note.
4. **Interpret loosely.** Notes are spoken ("this is too long", "cut", "more on how we trained it").
   Map each to one kind: DELETE, SHORTEN, FIX, ADD, REPHRASE, MOVE, EMPHASISE, CITE, ASK, CHECK.
   `Said:` is a local transcript and can mishear terms: read it with the source sentence in view and
   fix obvious mishearings silently. General notes apply to the whole document.
   Questions in notes ("is this relevant?", "what do you think?") get a real answer in chat, with a
   recommendation, not a silent change.
5. **Edit in place.** Re-read the target lines right before writing (the author may be editing too).
   Keep one sentence per line. Never touch text no note points at, except where a general note asks.
   Never invent a citation or a number: an unverifiable CITE or CHECK becomes a question.
6. **Ask only what is ambiguous.** Collect the questions first and ask them together.
7. **Log** every applied note to `$FR_HOME/log.jsonl` (default `~/.feedback-reader/log.jsonl`), one
   JSON object per line: `{"date","session","file","line","before","note","after","kind"}`. This is
   the corpus that style rules and few-shot examples can later be learned from.
8. **Dictionary.** For each term Whisper got wrong, run
   `$FR_READER/bin/dictation add-fix "<heard>" "<meant>"`, and `dictation add-term "<term>"` for new
   technical terms. List them in the report.
9. **Close.** `touch <round dir>/PROCESSED`. For LaTeX: rebuild with `latexmk -synctex=1`, commit, and
   show the changes as a red/blue PDF with `python3 $FR_READER/reader/review.py <main.tex> --rev <commit
   before this round>`, which switches the open reader to it.
10. **Report in one line:** notes applied per kind, questions asked, dictionary entries added, and
    "changes are open in the reader: no note = accept, pin a change to reject or revise, then Finish
    round".
