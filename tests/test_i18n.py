import ast
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from string import Formatter
from unittest.mock import patch

from multilayer import APP_NAME, WEBSITE, i18n
from multilayer.cli import main, parser
from multilayer.locale_en import EN
from multilayer.model import GUESTS, NETWORKS, VM, MultilayerError


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.old_language = i18n.language()
        self.addCleanup(i18n.set_language, self.old_language, False)
        i18n.set_language("ru", persist=False)

    def test_catalogue_covers_all_translatable_literals_and_preserves_placeholders(self):
        required = {APP_NAME, *NETWORKS.values(), "Горшков Сергей Владимирович"}
        required.update(guest for guest in GUESTS if re.search("[А-Яа-яЁё]", guest))
        root = Path(__file__).resolve().parents[1] / "multilayer"
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "tr"
                ):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        required.add(node.args[0].value)
        self.assertFalse(required - EN.keys())
        formatter = Formatter()
        for source in required:
            with self.subTest(source=source):
                self.assertFalse(re.search("[А-Яа-яЁё]", EN[source]))
                expected = {key for _, key, _, _ in formatter.parse(source) if key}
                translated = {key for _, key, _, _ in formatter.parse(EN[source]) if key}
                self.assertEqual(expected, translated)

    def test_settings_persist_reload_and_preserve_other_settings(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"MULTILAYER_CONFIG": str(Path(temporary) / "settings.json")}),
            patch.dict(os.environ, {}, clear=True),
        ):
            # The isolated environment must retain only this test's settings path.
            os.environ["MULTILAYER_CONFIG"] = str(Path(temporary) / "settings.json")
            path = i18n.settings_path()
            path.write_text('{"other": 42}')
            i18n.set_language("en")
            self.assertEqual(json.loads(path.read_text()), {"other": 42, "language": "en"})
            i18n._language = None
            self.assertEqual(i18n.language(), "en")
            i18n.set_language("ru")
            i18n._language = None
            self.assertEqual(i18n.language(), "ru")

    def test_bad_settings_default_to_russian(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / "settings.json"
            os.environ["MULTILAYER_CONFIG"] = str(path)
            for text in ("invalid", "[]", '{"language": "xx"}'):
                path.write_text(text)
                i18n._language = None
                self.assertEqual(i18n.language(), "ru")

    def test_environment_override_does_not_modify_saved_language(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / "settings.json"
            path.write_text('{"language": "ru"}')
            os.environ.update(MULTILAYER_CONFIG=str(path), MULTILAYER_LANG="en")
            i18n._language = None
            self.assertEqual(i18n.tr("Ошибка"), "Error")
            self.assertEqual(json.loads(path.read_text())["language"], "ru")

    def test_failed_persistence_does_not_switch_language(self):
        with patch("multilayer.i18n.settings_path") as path:
            path.return_value.parent.mkdir.side_effect = OSError("denied")
            with self.assertRaises(OSError):
                i18n.set_language("en")
            self.assertEqual(i18n.language(), "ru")

    def test_cli_and_validation_are_english(self):
        i18n.set_language("en", persist=False)
        help_text = parser().format_help()
        self.assertIn("Multilayer", help_text)
        self.assertNotRegex(help_text, "[А-Яа-яЁё]")
        self.assertIn(
            "RED OS", parser().parse_args(["create", "--name", "test", "--guest", "РЕД ОС"]).guest
        )
        with self.assertRaisesRegex(MultilayerError, "Disk size"):
            VM(name="test", disk_gb=0).validate()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_code:
            main(["--lang", "en", "--help"])
        self.assertEqual(exit_code.exception.code, 0)
        self.assertIn("VM data directory", output.getvalue())
        self.assertEqual(i18n.tr("Название"), "Name")


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from multilayer.branding import logo_path  # noqa: E402
from multilayer.engine import Engine  # noqa: E402
from multilayer.gui import STATUS, About, VMDialog, Window, translate_qt  # noqa: E402
from multilayer.store import Store  # noqa: E402


class WidgetTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.old_language = i18n.language()
        self.addCleanup(i18n.set_language, self.old_language, False)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = patch.dict(
            os.environ, {"MULTILAYER_CONFIG": str(Path(self.temporary.name) / "settings.json")}
        )
        self.config.start()
        self.addCleanup(self.config.stop)
        i18n.set_language("ru", persist=False)

    def test_switch_updates_menu_headers_fields_status_and_keeps_engine(self):
        engine = Engine(Store(Path(self.temporary.name) / "vms"))
        vm = VM(name="Русское имя", guest="Другая ОС")
        engine.store.save(vm)
        window = Window(engine)
        window.timer.stop()
        QThreadPool.globalInstance().waitForDone()
        self.app.processEvents()
        window.render_rows([(vm, "stopped")])
        window.change_language("en")
        self.assertIs(window.engine, engine)
        self.assertIn("Multilayer", window.windowTitle())
        self.assertEqual(
            [a.text() for a in window.menuBar().actions()], ["Diagnostics", "Language", "About"]
        )
        self.assertEqual(window.table.horizontalHeaderItem(0).text(), "Virtual machine")
        self.assertEqual(window.table.item(0, 0).text(), "Русское имя")
        self.assertEqual(window.table.item(0, 1).text(), "Other OS")
        self.assertEqual(window.table.item(0, 2).text(), "Stopped")
        dialog = VMDialog(vm)
        self.assertEqual(dialog.fields["guest"].currentData(), "Другая ОС")
        self.assertEqual(dialog.fields["guest"].currentText(), "Other OS")
        self.assertEqual(dialog.value().guest, "Другая ОС")
        window.change_language("ru")
        self.assertEqual(
            [a.text() for a in window.menuBar().actions()], ["Диагностика", "Язык", "О программе"]
        )
        self.assertEqual(window.table.item(0, 2).text(), "Выключена")
        self.assertEqual(json.loads(i18n.settings_path().read_text())["language"], "ru")
        dialog.deleteLater()
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_about_website_is_external_hyperlink_and_text_is_translated(self):
        i18n.set_language("en", persist=False)
        dialog = About()
        labels = dialog.findChildren(QLabel)
        link = next(label for label in labels if WEBSITE in label.text())
        self.assertIn('href="' + WEBSITE + '"', link.text())
        self.assertTrue(link.openExternalLinks())
        text = "\n".join(label.text() for label in labels)
        self.assertIn("developed for SteamOS", text)
        self.assertIn("free for commercial and non-commercial use", text)
        self.assertNotIn("Proxmox", text)
        self.assertTrue(
            any(
                button.text() == "Install / uninstall"
                for button in dialog.findChildren(QPushButton)
            )
        )
        dialog.deleteLater()

    def test_logo_is_renderable_and_status_labels_have_translations(self):
        self.assertTrue(logo_path().is_file())
        self.assertFalse(QIcon(str(logo_path())).pixmap(64, 64).isNull())
        self.assertTrue(set(STATUS.values()) <= EN.keys())

    def test_official_qt_standard_buttons_switch_with_language(self):
        i18n.set_language("ru", persist=False)
        translate_qt(self.app)
        self.assertEqual(QApplication.translate("QPlatformTheme", "Cancel"), "Отмена")
        i18n.set_language("en", persist=False)
        translate_qt(self.app)
        self.assertEqual(QApplication.translate("QPlatformTheme", "Cancel"), "Cancel")
