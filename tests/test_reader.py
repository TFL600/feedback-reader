"""Tests for the feedback reader. Standard library only:

    python3 -m unittest discover -s tests -v

Every test runs against a throwaway settings file and folders, never your real rounds. The
integration tests start the server on a free port, so they can run while a real reader is open.
Whisper and SyncTeX are not needed: the notes here are typed.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="fr-test-")).resolve()  # macOS: /var is a symlink
PROJECT = TMP / "project"
(PROJECT / "build" / "review").mkdir(parents=True)
(PROJECT / "sandbox").mkdir()
CONFIG = TMP / "config.env"
CONFIG.write_text(
    "# test settings\n"
    f"FR_HOME={TMP / 'home'}\n"
    f"FR_PROJECT={PROJECT}\n"
    "FR_PROJECT_GROUP=thesis\n"
    "FR_COMMAND=/from-file\n"
    'FR_PROJECT_COMMAND="/project-feedback"\n'
)
# Isolate the settings before any reader module is imported: nothing may touch real rounds.
for k in [k for k in os.environ if k.startswith("FR_")]:
    del os.environ[k]
os.environ["FR_CONFIG"] = str(CONFIG)
os.environ["FR_COMMAND"] = "/apply-feedback"  # the environment wins over the file
sys.path.insert(0, str(ROOT / "reader"))

import config  # noqa: E402
import glossary  # noqa: E402
import process  # noqa: E402
import server  # noqa: E402

# A one-page PDF, enough for the server (which never parses it) and for path handling.
PDF_BYTES = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def make_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF_BYTES)
    return path


class ConfigTest(unittest.TestCase):
    def test_file_settings_are_read(self):
        self.assertEqual(config.HOME, (TMP / "home").resolve())
        self.assertEqual(config.FEEDBACK, (TMP / "home" / "rounds").resolve())
        self.assertEqual(config.PROJECT_GROUP, "thesis")
        self.assertEqual(config.PROJECT_COMMAND, "/project-feedback")  # quotes stripped

    def test_environment_wins_over_file(self):
        self.assertEqual(config.COMMAND, "/apply-feedback")

    def test_defaults_follow_the_project(self):
        self.assertEqual(config.BUILD, PROJECT.resolve() / "build")
        self.assertEqual(config.SANDBOX, PROJECT.resolve() / "sandbox")
        self.assertEqual(config.PORT, 8765)

    def test_rel_and_resolve_src_round_trip(self):
        inside = PROJECT / "chapters" / "intro.tex"
        self.assertEqual(config.rel(inside), "chapters/intro.tex")
        self.assertEqual(config.resolve_src("chapters/intro.tex"), PROJECT.resolve() / "chapters" / "intro.tex")
        outside = TMP / "elsewhere.pdf"
        self.assertEqual(config.rel(outside), str(outside.resolve()))

    def test_shell_scripts_read_the_same_file(self):
        out = subprocess.run(["bash", "-c", f'. "{ROOT}/bin/_config.sh"; echo "$FR_PROJECT_GROUP|$FR_COMMAND"'],
                             capture_output=True, text=True, env={**os.environ}).stdout.strip()
        self.assertEqual(out, "thesis|/apply-feedback")


class GroupsAndTabsTest(unittest.TestCase):
    def test_group_for(self):
        self.assertEqual(server.group_for(make_pdf(PROJECT / "build" / "main.pdf")), "thesis")
        (PROJECT / "sandbox" / "trial.tex").write_text("x")
        self.assertEqual(server.group_for(make_pdf(PROJECT / "build" / "trial.pdf")), "sandbox")
        self.assertEqual(server.group_for(make_pdf(TMP / "drafts" / "email-drafts" / "e.pdf")), "email")
        self.assertEqual(server.group_for(make_pdf(TMP / "paper.pdf")), "other")
        self.assertEqual(server.group_for(make_pdf(config.FEEDBACK / "_pdfs" / "dropped.pdf")), "other")

    def test_same_draft_ignores_the_render_stamp(self):
        d = TMP / "drafts"
        self.assertTrue(server.same_draft(d / "letter-2026-11-20-101500.pdf", d / "letter-2026-11-20-113000.pdf"))
        self.assertFalse(server.same_draft(d / "letter-2026-11-20-101500.pdf", d / "cv-2026-11-20-101500.pdf"))
        self.assertFalse(server.same_draft(d / "letter.pdf", TMP / "other" / "letter.pdf"))

    def test_mode_pair_links_a_build_to_its_review(self):
        main = make_pdf(PROJECT / "build" / "main.pdf")
        review = make_pdf(PROJECT / "build" / "review" / "review-main.pdf")
        self.assertEqual(server.mode_pair(main), ("write", str(review)))
        self.assertEqual(server.mode_pair(review), ("review", str(main)))


class ProcessTest(unittest.TestCase):
    def test_refine_source_moves_to_the_clicked_sentence(self):
        tex = PROJECT / "refine.tex"
        tex.write_text("First sentence ends here.\nSecond sentence starts on the same printed line.\n")
        note = {"source": {"file": "refine.tex", "line": 1},
                "line_text": "First sentence ends here. Second sentence starts on the same printed line.",
                "word": "Second"}
        self.assertEqual(process.refine_source(note), {"file": "refine.tex", "line": 2, "synctex_line": 1})

    def test_source_line_reads_the_sentence(self):
        (PROJECT / "src.tex").write_text("one\n  two  \nthree\n")
        self.assertEqual(process.source_line({"file": "src.tex", "line": 2}), "two")
        self.assertEqual(process.source_line(None), "")

    def test_glossary_picks_up_jargon(self):
        terms = glossary.count_terms(r"We trained NequIP on \ce{CsPbI3} with DFT and Huang-Rhys factors.")
        for t in ("NequIP", "CsPbI3", "DFT", "Huang-Rhys"):
            self.assertIn(t, terms)

    def test_silence_phrases(self):
        for t in ("Thank you.", "All right.", "I’m going to go.", "Thank you. Thank you.", "[BLANK_AUDIO]", ""):
            self.assertTrue(process.is_silence(t), t)
        for t in ("Cut this sentence.", "All right, cut this.", "Okay. Move the figure up."):
            self.assertFalse(process.is_silence(t), t)

    def test_typing_noise_gets_no_said_line(self):
        sess = TMP / "silence-round"
        sess.mkdir()
        (sess / "session.json").write_text(json.dumps({"pdf": str(make_pdf(TMP / "silence" / "d.pdf"))}))
        notes = [{"id": "n1", "kind": "general", "typed": "Cut the second paragraph.", "transcript": "All right."},
                 {"id": "n2", "kind": "general", "typed": "Typed too.", "transcript": "Move it up."},
                 {"id": "n3", "kind": "general", "transcript": "Thank you."}]
        (sess / "notes.json").write_text(json.dumps(notes))
        process.process(sess)
        md = (sess / "feedback.md").read_text()
        self.assertIn("Typed: Cut the second paragraph.", md)
        self.assertNotIn("Said: All right.", md)
        self.assertIn("Said: Move it up.", md)
        self.assertIn("Said: Thank you.", md)  # nothing typed: the transcript is all there is


class DictationTest(unittest.TestCase):
    def run_dictation(self, *args, stdin=""):
        d = TMP / "dictionary.md"
        d.write_text("# Dictation dictionary\n\n## Terms\n- SyncTeX\n- CsPbI3\n\n## More terms\n\n## Corrections\n- sink tech -> SyncTeX\n")
        env = {**os.environ, "FR_DICTIONARY": str(d)}
        return subprocess.run([sys.executable, str(ROOT / "bin" / "dictation"), *args], input=stdin,
                              capture_output=True, text=True, env=env).stdout

    def test_corrections_and_casing(self):
        self.assertEqual(self.run_dictation("fix", stdin="Use sink tech for cspbi3."), "Use SyncTeX for CsPbI3.")

    def test_prompt_lists_the_terms(self):
        self.assertEqual(self.run_dictation("prompt").strip(), "Glossary: SyncTeX, CsPbI3.")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTest(unittest.TestCase):
    """A real server on a free port: route, note, finish, process."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.home = Path(tempfile.mkdtemp(prefix="fr-server-")).resolve()
        env = {**os.environ, "FR_HOME": str(cls.home), "FR_PORT": str(cls.port)}
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "reader" / "server.py"), "--no-browser"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                cls.call("GET", "/api/queue")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    @classmethod
    def call(cls, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", data=data, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def test_route_keeps_documents_apart(self):
        a, b = make_pdf(TMP / "route" / "a.pdf"), make_pdf(TMP / "route" / "b.pdf")
        ta = self.call("POST", "/api/route", {"path": str(a)})
        self.call("GET", f"/api/state?tab={ta['tab']}")  # the tab is alive
        tb = self.call("POST", "/api/route", {"path": str(b)})
        self.assertNotEqual(ta["tab"], tb["tab"], "another document must get its own tab")
        again = self.call("POST", "/api/route", {"path": str(a)})
        self.assertEqual(again, {"tab": ta["tab"], "new": False})

    def test_a_round_end_to_end(self):
        pdf = make_pdf(TMP / "round" / "draft.pdf")
        tab = self.call("POST", "/api/route", {"path": str(pdf)})["tab"]
        self.call("POST", f"/api/note?tab={tab}", {"id": "n1", "kind": "anchored", "page": 1, "x": 100, "y": 120,
                                                    "line_text": "A sentence worth cutting.", "word": "cutting",
                                                    "typed": "Cut this.", "created": "2026-01-01T10:00:00"})
        self.call("POST", f"/api/note?tab={tab}", {"id": "n2", "kind": "general", "typed": "British spelling throughout.",
                                                    "created": "2026-01-01T10:01:00"})
        state = self.call("GET", f"/api/state?tab={tab}")
        self.assertEqual([n["id"] for n in state["notes"]], ["n1", "n2"])
        done = self.call("POST", f"/api/finish?tab={tab}")
        self.assertEqual(done["kept"], 2)
        round_dir = self.home / "rounds" / "other" / done["finished"]
        self.assertEqual(done["command"], f"/apply-feedback {round_dir}")
        for _ in range(100):  # the worker writes feedback.md in the background
            if (round_dir / "feedback.md").exists() and not (round_dir / "PROCESSING").exists():
                break
            time.sleep(0.1)
        md = (round_dir / "feedback.md").read_text()
        self.assertIn("Typed: Cut this.", md)
        self.assertIn("Clicked: “A sentence worth cutting.” (word: cutting)", md)
        self.assertIn("## 2 · general", md)

    def test_a_second_server_exits_cleanly(self):
        env = {**os.environ, "FR_HOME": str(self.home)}
        r = subprocess.run([sys.executable, str(ROOT / "reader" / "server.py"), "--no-browser", "--port", str(self.port)],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already in use", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("--- start ", r.stdout)

    def test_open_reader_reuses_a_running_server(self):
        log = self.home / "reader.log"
        log.write_text("earlier run\n")
        env = {**os.environ, "FR_HOME": str(self.home), "FR_PORT": str(self.port), "FR_LOG": str(log), "FR_NO_BROWSER": "1"}
        pdf = make_pdf(TMP / "open" / "o.pdf")
        r = subprocess.run([str(ROOT / "bin" / "open-reader"), str(pdf)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text(), "earlier run\n", "a running reader must not be restarted or its log cut")

    def test_bad_ids_are_refused(self):
        pdf = make_pdf(TMP / "bad" / "x.pdf")
        tab = self.call("POST", "/api/route", {"path": str(pdf)})["tab"]
        with self.assertRaises(urllib.error.HTTPError):
            self.call("POST", f"/api/note?tab={tab}", {"id": "../../etc", "kind": "general"})


if __name__ == "__main__":
    unittest.main()
