from __future__ import annotations

import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGING_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGING_DIR.parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import setup_guide
import setup_wizard


GUIDE = (REPO_ROOT / "docs" / "SETUP-NEW-PC.md").read_text(encoding="utf-8")


def _messages(path: Path, error_names: set[str]) -> list[str]:
    """Тексты отказов, которые человек видит на экране: постоянная часть первого аргумента."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in error_names and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.append(arg.value)
        elif isinstance(arg, ast.JoinedStr) and isinstance(arg.values[0], ast.Constant):
            # Сообщение, начатое подстановкой («“{name}” is not in …»), первой
            # константой даёт одну кавычку, — тогда берём самый длинный кусок.
            pieces = [part.value for part in arg.values if isinstance(part, ast.Constant)]
            found.append(arg.values[0].value if len(arg.values[0].value.strip(" “\"")) > 3 else max(pieces, key=len))
        elif isinstance(arg, ast.BinOp) and isinstance(arg.left, ast.Constant):
            found.append(arg.left.value)
    return found


class SetupGuideRenderingTests(unittest.TestCase):
    def test_the_real_guide_renders_without_leftover_markup(self):
        page = setup_guide.render_page(GUIDE, version="1.2.3")
        text = re.sub(r"<code>.*?</code>|<style>.*?</style>", "", page, flags=re.S)
        for token in ("**", "`", "|---", "[ ]", "## ", "\\"):
            self.assertNotIn(token, text, token)
        self.assertIn("<table>", page)
        self.assertIn("Shipped with version 1.2.3.", page)

    def test_markup_inside_code_stays_literal_and_html_is_escaped(self):
        body = setup_guide.render_body("Run `Setup-<version>.exe` **now** & *wait*.")
        self.assertEqual(
            body,
            "<p>Run <code>Setup-&lt;version&gt;.exe</code> <strong>now</strong> &amp; <em>wait</em>.</p>",
        )

    def test_nested_lists_checkboxes_and_numbering_after_a_break(self):
        body = setup_guide.render_body(
            "- [ ] first\n- plain\n  continued\n\n1. one\n   - sub\n2. two\n\nbreak\n\n3. three\n"
        )
        self.assertIn('<li class="task"><span class="box">☐</span> first', body)
        self.assertIn("<li>plain continued</li>", body)
        self.assertIn("<ol><li>one<ul><li>sub</li></ul></li><li>two</li></ol>", body)
        self.assertIn('<ol start="3"><li>three</li></ol>', body)

    def test_cli_writes_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / setup_guide.GUIDE_FILE_NAME
            with mock.patch.object(sys, "argv", ["setup_guide.py", "--output", str(output), "--version", "9.9"]):
                self.assertEqual(setup_guide.main(), 0)
            self.assertIn("Shipped with version 9.9.", output.read_text(encoding="utf-8"))


class SetupGuideMatchesTheProductTests(unittest.TestCase):
    """ACL-1175: страница правится в том же PR, что и установка, — иначе эти тесты красные."""

    def test_every_wizard_step_is_described(self):
        for step in setup_wizard.SetupWizard.STEPS:
            self.assertIn(f"**{step}.**", GUIDE, step)
        self.assertIn(f"Step N of {len(setup_wizard.SetupWizard.STEPS)}", GUIDE)

    def test_every_system_check_is_named(self):
        labels = _messages(PACKAGING_DIR / "setup_wizard.py", {"ReadinessCheck"})
        self.assertEqual(len(labels), 4, labels)
        for label in labels:
            self.assertIn(label, GUIDE, label)

    def test_every_setup_refusal_is_explained(self):
        messages = _messages(PACKAGING_DIR / "setup_wizard.py", {"WizardValidationError"})
        messages += _messages(REPO_ROOT / "src" / "analysis_agent" / "stats_workbook.py", {"StatsWorkbookError"})
        self.assertGreaterEqual(len(messages), 14, messages)
        for message in messages:
            # Длинный отказ в таблице приведён первой фразой.
            first_sentence = message.split(". ")[0].strip().lstrip("”").strip()
            self.assertIn(first_sentence, GUIDE, message)

    def test_every_update_button_failure_is_explained(self):
        """ACL-1175, критерий 3: что человек увидит при сбое кнопки Update, названо в гиде."""
        messages = _messages(REPO_ROOT / "src" / "web" / "release_updates.py", {"UpdateError"})
        self.assertGreaterEqual(len(messages), 15, messages)
        for message in messages:
            first_sentence = message.split(". ")[0].rstrip().rstrip(".:")
            self.assertIn(first_sentence, GUIDE, message)
        app_source = (REPO_ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
        block = app_source[app_source.index("async def _apply_release_update"):]
        block = block[:block.index("def _period_owner")]
        refusals = re.findall(r'"error": (?:message if|"([^"]+)")', block)
        refusals = [text for text in refusals if text]
        self.assertGreaterEqual(len(refusals), 4, refusals)
        for text in refusals:
            self.assertIn(text.split(". ")[0].rstrip("."), GUIDE, text)
        page = (REPO_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        for phrase in (
            "Update not started",
            "ACM did not come back within 10 minutes",
            "did not work, so ACM went back to",
            "ACM could not fully restore the previous version",
        ):
            self.assertIn(phrase, page, phrase)
            self.assertIn(phrase, GUIDE, phrase)

    def test_multilogin_token_expiry_banner_is_explained(self):
        page = (REPO_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        for phrase in ("Profiles cannot start until you sign in again.", "Sign in again to renew it."):
            self.assertIn(phrase, page, phrase)
            self.assertIn(phrase, GUIDE, phrase)

    def test_update_flow_names_what_the_button_shows(self):
        page = (REPO_ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
        self.assertIn("title: 'Update ACM to ' + name + '?'", page)
        self.assertIn("**Update ACM to <version>?**", GUIDE)

    def test_installer_and_wizard_point_at_the_shipped_guide(self):
        iss = (PACKAGING_DIR / "installer.iss").read_text(encoding="utf-8")
        self.assertIn('\\Setup guide"; Filename: "{app}\\_internal\\setup-guide.html"', iss)
        self.assertIn('Filename: "{app}\\_internal\\setup-guide.html"; Description: "Open the setup guide"', iss)
        spec = (PACKAGING_DIR / "acl_windows.spec").read_text(encoding="utf-8")
        self.assertIn('(str(SETUP_GUIDE_PATH), "."),', spec)
        build = (PACKAGING_DIR / "build.ps1").read_text(encoding="utf-8")
        self.assertIn('setup_guide.py', build)
        self.assertIn('"setup-guide.html"', build)


class OpenSetupGuideTests(unittest.TestCase):
    def test_frozen_bundle_opens_the_html_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "setup-guide.html").write_text("<p>ok</p>", encoding="utf-8")
            opened = []
            with mock.patch.object(sys, "_MEIPASS", tmp, create=True):
                problem = setup_wizard.open_setup_guide(lambda uri: opened.append(uri) or True)
        self.assertEqual(problem, "")
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("file:///") and opened[0].endswith("/setup-guide.html"))

    def test_source_checkout_opens_the_markdown(self):
        self.assertEqual(setup_wizard.setup_guide_path(), REPO_ROOT / "docs" / "SETUP-NEW-PC.md")

    def test_missing_page_or_failed_browser_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertIn("missing", setup_wizard.open_setup_guide(lambda uri: True))
            (Path(tmp) / "setup-guide.html").write_text("<p>ok</p>", encoding="utf-8")
            with mock.patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertIn("Open this file yourself", setup_wizard.open_setup_guide(lambda uri: False))

                def broken(uri):
                    raise OSError("no browser")

                self.assertIn("Open this file yourself", setup_wizard.open_setup_guide(broken))


if __name__ == "__main__":
    unittest.main()
