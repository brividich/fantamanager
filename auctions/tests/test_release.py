"""Il numero di versione e la pubblicazione dell'immagine Docker."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from liveauction import __version__

ROOT = Path(settings.BASE_DIR)


class ReleaseTests(SimpleTestCase):
    def test_version_is_semver_and_in_sync(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        iss = (ROOT / "packaging/installer.iss").read_text(encoding="utf-8")
        self.assertIn(f'#define MyAppVersion "{__version__}"', iss)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        first = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(1), __version__)

    def test_the_image_is_published_only_after_tests_and_a_smoke_run(self):
        src = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
        self.assertIn("uses: ./.github/workflows/tests.yml", src)
        self.assertRegex(src, r"smoke:\n(?:.*\n)*?    needs: tests")
        self.assertRegex(src, r"build-and-push:\n    needs: smoke")
        self.assertIn("/healthz/", src)
        self.assertIn("check --deploy", src)
        self.assertIn("workflow_call:", (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
