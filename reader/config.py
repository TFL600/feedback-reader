"""Paths and settings. Nothing is tied to one machine: everything comes from environment variables
or from a settings file outside the repo, ~/.feedback-reader/config.env (FR_CONFIG names another).
That file holds KEY=value lines (# comments, ~ expanded). An environment variable wins over it.

    FR_HOME            where the reader keeps its own state           default ~/.feedback-reader
    FR_FEEDBACK        where feedback rounds are stored               default $FR_HOME/rounds
    FR_PROJECT         root of a LaTeX project (a git repo) whose PDFs get source lines.
                       Optional: without it every PDF is an "other" document.
    FR_PROJECT_GROUP   folder name for that project's rounds          default project
    FR_BUILD           where that project builds its PDFs             default $FR_PROJECT/build
    FR_SANDBOX         test documents (see tests/eval.py)             default $FR_PROJECT/sandbox
    FR_PORT            port of the local server                       default 8765
    FR_COMMAND         slash command Finish round copies              default /apply-feedback
    FR_PROJECT_COMMAND the same, for rounds on the project's PDFs     default $FR_COMMAND
    FR_GLOSSARY        priority terms for Whisper (glossary.py)       default $FR_HOME/glossary.txt
    FR_GLOSSARY_EXTRA  hand-picked terms, always first                default $FR_HOME/glossary-extra.txt
    FR_GLOSSARY_DIRS   folders of the project glossary.py scans, colon-separated, relative to
                       the project                                    default the whole project
The bin/ scripts read the same file (bin/_config.sh) for FR_PORT, FR_HOME, FR_DRAFTS,
FR_EMAIL_DRAFTS, FR_LOG, FR_BROWSER, FR_WHISPER_MODEL and FR_DICTIONARY.
"""
import os
from pathlib import Path


def _file_settings() -> dict:
    f = Path(os.environ.get("FR_CONFIG") or Path.home() / ".feedback-reader" / "config.env").expanduser()
    out = {}
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        out[k] = v
    return out


SETTINGS = {**_file_settings(), **{k: v for k, v in os.environ.items() if k.startswith("FR_")}}


def get(name, default=None):
    return SETTINGS.get(name) or default


def _path(name, default=None):
    v = get(name)
    return Path(os.path.expanduser(v)).resolve() if v else default


HOME = _path("FR_HOME", Path.home() / ".feedback-reader")
FEEDBACK = _path("FR_FEEDBACK", HOME / "rounds")
PROJECT = _path("FR_PROJECT")
PROJECT_GROUP = get("FR_PROJECT_GROUP", "project")
BUILD = _path("FR_BUILD", PROJECT / "build" if PROJECT else None)
SANDBOX = _path("FR_SANDBOX", PROJECT / "sandbox" if PROJECT else None)
PORT = int(get("FR_PORT", "8765"))
COMMAND = get("FR_COMMAND", "/apply-feedback")
PROJECT_COMMAND = get("FR_PROJECT_COMMAND", COMMAND)
GLOSSARY = _path("FR_GLOSSARY", HOME / "glossary.txt")
GLOSSARY_EXTRA = _path("FR_GLOSSARY_EXTRA", HOME / "glossary-extra.txt")
GLOSSARY_DIRS = [d for d in get("FR_GLOSSARY_DIRS", "").split(":") if d]
BIN = Path(__file__).resolve().parent.parent / "bin"


def in_project(p: Path) -> bool:
    return bool(PROJECT) and Path(p).resolve().is_relative_to(PROJECT)


def rel(p: Path) -> str:
    """A path as shown to the agent: relative to the project when inside it, else absolute."""
    p = Path(p).resolve()
    return str(p.relative_to(PROJECT)) if in_project(p) else str(p)


def resolve_src(file: str) -> Path:
    """The inverse of rel() for a SyncTeX source file."""
    f = Path(file)
    return f if f.is_absolute() or not PROJECT else PROJECT / f
