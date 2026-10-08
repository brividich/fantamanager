"""Regressions for the security review: what a hostile team name, upload or
guess must never get through."""
from django.contrib.auth.models import User
from django.test import TestCase

from ..models import Auction, League, Participant, Player

EVIL = "</script><script>alert(1)</script>"


class JsonInScriptTests(TestCase):
    """A team name is shown inside page data: it must stay data, never close
    the <script> it travels in."""

    def setUp(self):
        self.owner = User.objects.create_superuser("root", password="pwd12345")
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name=EVIL,
                                               access_code="EVIL1")
        player = Player.objects.create(league=self.league, name=EVIL, team=EVIL)
        self.auction = Auction.objects.create(league=self.league, title="Asta", player=player,
                                              status=Auction.Status.LIVE)

    def _assert_inert(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(EVIL, resp.content.decode())

    def test_bid_page(self):
        self.client.post("/join/", {"access_code": "EVIL1"})
        self._assert_inert(self.client.get(f"/bid/{self.auction.id}/"))

    def test_screen(self):
        self.client.force_login(self.owner)
        self._assert_inert(self.client.get(f"/screen/{self.auction.id}/"))

    def test_console_and_supervisor(self):
        self.client.force_login(self.owner)
        self._assert_inert(self.client.get("/dashboard/", {"auction": self.auction.id}))
        self._assert_inert(self.client.get("/supervisor/"))


def _png():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (200, 20, 20)).save(buf, format="PNG")
    return buf.getvalue()


class UploadTests(TestCase):
    """Logos and kits are served from the site's own address: only real
    pictures, under a name the uploader doesn't choose."""

    def setUp(self):
        import tempfile

        from django.test import override_settings

        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(MEDIA_ROOT=self._media.name)
        self._override.enable()
        self.owner = User.objects.create_user("presidente", password="pwd12345", is_staff=True)
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.team = Participant.objects.create(league=self.league, display_name="Squadra")
        self.client.force_login(self.owner)

    def tearDown(self):
        self._override.disable()
        self._media.cleanup()

    def _post_logo(self, name, data, content_type):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(f"/dashboard/participants/{self.team.id}/edit/", {
            "display_name": "Squadra", "is_active": "1",
            "logo": SimpleUploadedFile(name, data, content_type=content_type),
        })

    def test_a_page_dressed_as_a_logo_is_refused(self):
        for name, data, ctype in (
            ("x.html", b"<script>alert(1)</script>", "text/html"),
            ("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "image/svg+xml"),
            ("x.png", b"<html><script>alert(1)</script></html>", "image/png"),
        ):
            self._post_logo(name, data, ctype)
            self.team.refresh_from_db()
            self.assertFalse(self.team.logo, name)

    def test_a_real_picture_gets_a_random_truthful_name(self):
        self._post_logo("evil.html", _png(), "text/html")
        self.team.refresh_from_db()
        self.assertTrue(self.team.logo.name.endswith(".png"))
        self.assertNotIn("evil", self.team.logo.name)
        resp = self.client.get("/" + self.team.logo.url.lstrip("/"))
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", resp["Content-Security-Policy"])

    def test_an_old_non_image_upload_comes_down_as_a_file(self):
        import os

        os.makedirs(os.path.join(self._media.name, "logos"), exist_ok=True)
        with open(os.path.join(self._media.name, "logos", "old.html"), "w") as fh:
            fh.write("<script>alert(1)</script>")
        resp = self.client.get("/media/logos/old.html")
        self.assertEqual(resp["Content-Type"], "application/octet-stream")
        self.assertEqual(resp["Content-Disposition"], "attachment")

    def test_an_oversized_body_is_refused_before_parsing(self):
        from django.test import override_settings

        with override_settings(FM_MAX_REQUEST_BYTES=1000):
            resp = self._post_logo("x.png", b"0" * 5000, "image/png")
        self.assertEqual(resp.status_code, 413)
