import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from multilayer import __version__


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for name in ("Multilayer.AppImage", "multilayer.deb", "Multilayer-Setup.exe"):
            (self.directory / name).write_bytes(name.encode())
        self.main = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "packaging/publish_release.py")
        )["main"]
        self.release = {
            "draft": True,
            "assets": [],
            "url": "https://api.github.com/repos/test/multilayer/releases/1",
            "upload_url": "https://uploads.github.com/repos/test/multilayer/releases/1/assets{?name,label}",
            "html_url": "https://github.com/test/multilayer/releases/tag/v" + __version__,
        }
        environment = patch.dict(
            "os.environ",
            {
                "GITHUB_REF_NAME": "v" + __version__,
                "GITHUB_REF_TYPE": "tag",
                "GITHUB_REPOSITORY": "test/multilayer",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def run_with(self, api):
        with patch.dict(self.main.__globals__, {"api": api}), patch("builtins.print"):
            self.main(self.directory)

    def test_uploads_all_installers_and_checksums_before_publishing(self):
        missing = HTTPError("https://api.github.com", 404, "Not Found", None, None)
        api = Mock(side_effect=[missing, self.release, {}, {}, {}, {}, self.release])
        self.run_with(api)
        self.assertTrue(api.call_args_list[1].args[2]["draft"])
        self.assertEqual(
            [call.args[1] for call in api.call_args_list[1:]], ["POST"] * 5 + ["PATCH"]
        )
        self.assertEqual(api.call_args_list[-1].args[2], {"draft": False})
        self.assertEqual(len((self.directory / "SHA256SUMS").read_text().splitlines()), 3)

    def test_published_release_is_never_modified(self):
        self.release["draft"] = False
        api = Mock(return_value=self.release)
        with self.assertRaises(ValueError):
            self.run_with(api)
        self.assertEqual(api.call_count, 1)

    def test_missing_installer_blocks_creation(self):
        (self.directory / "Multilayer-Setup.exe").unlink()
        api = Mock()
        with self.assertRaises(ValueError):
            self.run_with(api)
        api.assert_not_called()

    def test_tag_must_match_version(self):
        api = Mock()
        with patch.dict("os.environ", {"GITHUB_REF_NAME": "v9.9.9"}), self.assertRaises(ValueError):
            self.run_with(api)
        api.assert_not_called()

    def test_mismatched_existing_draft_asset_is_not_overwritten(self):
        self.release["assets"] = [{"name": "Multilayer.AppImage", "digest": "sha256:wrong"}]
        api = Mock(return_value=self.release)
        with self.assertRaises(ValueError):
            self.run_with(api)
        self.assertEqual(api.call_count, 1)
