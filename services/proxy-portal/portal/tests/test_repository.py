import shutil
import subprocess
import unittest
from pathlib import Path

from django.test import SimpleTestCase

PORTAL = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("git") and (PORTAL / ".gitignore").exists(), "needs git")
class IgnoredTest(SimpleTestCase):
    def test_secret_and_data_paths_are_ignored(self):
        for path in ("data/secret_key", "data/portal.sqlite3", "data/portal.sqlite3-journal", "e2e/run/ssl/Server-unsecure.pvk", "e2e/run/spool/status.json",
                     "portal/locale/zh_Hans/LC_MESSAGES/django.mo"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=PORTAL)
            self.assertEqual(result.returncode, 0, path)

    def test_sources_are_not_ignored(self):
        for path in ("portal/settings.py", "portal/locale/zh_Hans/LC_MESSAGES/django.po", "requirements.txt"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=PORTAL)
            self.assertEqual(result.returncode, 1, path)
