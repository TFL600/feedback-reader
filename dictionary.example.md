# Dictation dictionary

Copy to `~/.feedback-reader/dictionary.md` (or point `FR_DICTIONARY` at it). Read by `bin/dictation`,
which `bin/whisper-transcribe` calls for every clip.

- **Terms** bias Whisper towards the right spelling. They go into its prompt, which Whisper caps at
  224 tokens, so `dictation prompt` stops at 600 characters. Most important first. A document's
  own glossary (`reader/glossary.py`) is put in front of these automatically. `dictation budget`
  shows what fits.
- **More terms** fill whatever prompt room is left, and otherwise only fix casing. Put names and
  rarer words here so the list can grow without crowding out the core.
- **Corrections** are applied after transcription: `heard -> meant`, case-insensitive, whole
  words. Add one whenever a transcript gets a name or term wrong (`dictation add-fix`).

## Terms
- perovskite
- SyncTeX
- LaTeX

## More terms
- CsPbI3
- NequIP

## Corrections
- sink tech -> SyncTeX
- la tech -> LaTeX
