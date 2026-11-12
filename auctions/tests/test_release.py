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

    def test_the_image_knows_its_build(self):
        """Commit e data della build arrivano nell'immagine e si vedono nell'app:
        così «è aggiornata?» si risponde confrontandoli con l'ultimo commit."""
        import os
        from unittest import mock
        from django.test import RequestFactory
        from auctions.context_processors import app_version

        self.assertIn("ARG FM_BUILD", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
        src = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
        self.assertIn("FM_BUILD=${{ github.sha }}", src)
        env = {"FM_BUILD": "0123456789abcdef", "FM_BUILD_DATE": "2026-11-11T23:41:15+01:00"}
        with mock.patch.dict(os.environ, env):
            ctx = app_version(RequestFactory().get("/"))
        self.assertEqual((ctx["app_build"], ctx["app_build_date"]), ("0123456", "11/11/2026"))
        with mock.patch.dict(os.environ, {"FM_BUILD": "", "FM_BUILD_DATE": "non è una data"}):
            ctx = app_version(RequestFactory().get("/"))
        self.assertEqual((ctx["app_build"], ctx["app_build_date"]), ("", ""))
