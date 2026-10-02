import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


spec = importlib.util.spec_from_file_location(
    "updater", Path(__file__).resolve().parents[1] / "scripts/update_pomodoro.py"
)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class CaskUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "Casks/pomodoro-blocker.rb"
        self.archive = b"release archive fixture"
        self.checksum = hashlib.sha256(self.archive).hexdigest()

    def release(self, version="0.1.0"):
        name = f"PomodoroBlocker-{version}-universal.zip"
        base = f"{updater.RELEASE_DOWNLOAD}/v{version}"
        return {
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": asset, "state": "uploaded", "browser_download_url": f"{base}/{asset}"}
                for asset in (name, name + ".sha256")
            ],
        }

    def read_asset(self, url):
        if url.endswith(".sha256"):
            name = url.rsplit("/", 1)[1].removesuffix(".sha256")
            return f"{self.checksum}  {name}\n".encode()
        return self.archive

    def save_version(self, version):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_text(updater.render_cask(version, self.checksum))

    def test_verified_release_is_written_and_repeat_is_a_noop(self):
        with patch.object(updater, "read_url", side_effect=self.read_asset) as read:
            self.assertTrue(updater.update_cask(self.release(), self.destination))
            cask = self.destination.read_text()
            self.assertIn(f'sha256 "{self.checksum}"', cask)
            self.assertIn('depends_on macos: :sonoma', cask)
            self.assertIn('app "PomodoroBlocker.app"', cask)
            self.assertNotIn("quarantine", cask)
            self.assertFalse(updater.update_cask(self.release(), self.destination))
            self.assertEqual(read.call_count, 2)

    def test_versions_compare_numerically_and_never_downgrade(self):
        self.save_version("0.9.0")
        with patch.object(updater, "read_url", side_effect=self.read_asset) as read:
            self.assertTrue(updater.update_cask(self.release("0.10.0"), self.destination))
            read.reset_mock()
            self.assertFalse(updater.update_cask(self.release("0.2.0"), self.destination))
            read.assert_not_called()

    def test_missing_draft_and_prerelease_leave_cask_unchanged(self):
        self.save_version("0.1.0")
        original = self.destination.read_bytes()
        for release in (None, {"draft": True}, {"prerelease": True}):
            self.assertFalse(updater.update_cask(release, self.destination))
        self.assertEqual(self.destination.read_bytes(), original)

    def test_invalid_tags_are_rejected(self):
        for tag in ("1.0.0", "v01.0.0", "v1.0.0-beta", "v1.0.0\n", 'v1.0.0"'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release = self.release()
                release["tag_name"] = tag
                updater.update_cask(release, self.destination)
        self.assertFalse(self.destination.exists())

    def test_missing_duplicate_unfinished_and_redirected_assets_are_rejected(self):
        missing = self.release()
        missing["assets"].pop()
        duplicate = self.release()
        duplicate["assets"].append(duplicate["assets"][0])
        unfinished = self.release()
        unfinished["assets"][0]["state"] = "new"
        redirected = self.release()
        redirected["assets"][0]["browser_download_url"] = "https://example.com/archive.zip"
        for release in (missing, duplicate, unfinished, redirected):
            with self.subTest(release=release), self.assertRaises(ValueError):
                updater.update_cask(release, self.destination)
        self.assertFalse(self.destination.exists())

    def test_mismatched_archive_preserves_existing_cask(self):
        self.save_version("0.0.1")
        original = self.destination.read_bytes()
        with patch.object(updater, "read_url", side_effect=[
            f'{"0" * 64}  PomodoroBlocker-0.1.0-universal.zip\n'.encode(), self.archive
        ]), self.assertRaisesRegex(ValueError, "checksum does not match"):
            updater.update_cask(self.release(), self.destination)
        self.assertEqual(self.destination.read_bytes(), original)

    def test_checksum_for_wrong_filename_is_rejected(self):
        with patch.object(updater, "read_url", return_value=f"{self.checksum}  other.zip\n".encode()), \
                self.assertRaisesRegex(ValueError, "expected archive"):
            updater.update_cask(self.release(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_latest_release_404_is_a_noop_but_other_errors_fail(self):
        for code in (404, 403):
            error = HTTPError(updater.LATEST_RELEASE, code, "fixture", None, None)
            with patch.object(updater, "read_url", side_effect=error):
                if code == 404:
                    self.assertIsNone(updater.latest_release())
                else:
                    with self.assertRaises(HTTPError):
                        updater.latest_release()

    def test_transient_network_failure_is_retried(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        with patch.object(updater, "urlopen", side_effect=[URLError("temporary"), response]) as request, \
                patch.object(updater.time, "sleep") as sleep:
            self.assertEqual(updater.read_url("https://github.com/fixture"), b"ok")
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
