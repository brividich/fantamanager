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


class TeamCodeTests(TestCase):
    """A team code is looked up across every league: it must point at one
    team only, and guessing must hit a ceiling at every door."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.one = League.objects.create(name="Uno")
        self.two = League.objects.create(name="Due")
        self.a = Participant.objects.create(league=self.one, display_name="A", access_code="SAME01")
        self.b = Participant.objects.create(league=self.two, display_name="B", access_code="same01")
        Auction.objects.create(league=self.one, title="Asta", status=Auction.Status.LIVE)

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()       # the throttle counts live there: leave none behind

    def test_a_shared_code_lets_nobody_in(self):
        for path, field in (("/join/", "access_code"), ("/app/login/", "access_code")):
            resp = self.client.post(path, {field: "SAME01"})
            self.assertEqual(resp.status_code, 200, path)
            self.assertContains(resp, "usato da più squadre")
            self.assertNotIn("participant_id", self.client.session)

    def test_join_guesses_are_throttled(self):
        for i in range(10):
            self.client.post("/join/", {"access_code": f"WRONG{i}"})
        self.a.access_code = "UNIQUE77"
        self.a.save()
        resp = self.client.post("/join/", {"access_code": "UNIQUE77"})
        self.assertContains(resp, "Troppi tentativi")
        self.assertNotIn("participant_id", self.client.session)

    def test_admins_cannot_reuse_a_code_from_another_league(self):
        owner = User.objects.create_user("presidente", password="pwd12345", is_staff=True)
        self.one.owner = owner
        self.one.save()
        self.client.force_login(owner)
        self.client.post(f"/dashboard/participants/{self.a.id}/edit/", {
            "display_name": "A", "access_code": "TAKEN9", "is_active": "1"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.access_code, "TAKEN9")
        self.b.access_code = "OTHER1"
        self.b.save()
        Participant.objects.create(league=self.two, display_name="C", access_code="ZZZZZZ")
        self.client.post(f"/dashboard/participants/{self.a.id}/edit/", {
            "display_name": "A", "access_code": "zzzzzz", "is_active": "1"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.access_code, "TAKEN9")

    def test_generated_codes_are_long_and_unique(self):
        from ..models.participant import generate_access_code

        codes = {generate_access_code() for _ in range(50)}
        self.assertEqual(len(codes), 50)
        self.assertTrue(all(len(c) == 8 for c in codes))


class StandingsUrlTests(TestCase):
    """The standings link is typed by an admin and fetched by the server: it
    must never reach the server's own network."""

    def setUp(self):
        self.league = League.objects.create(name="Lega")
        Participant.objects.create(league=self.league, display_name="Alfa")
        self.fetched = []

    def _get(self, url, **kwargs):
        self.fetched.append(url)

        class Resp:
            status_code = 200
            headers = {}
            text = "<table><tr><td>Alfa</td></tr></table>"

            def raise_for_status(self):
                pass

        return Resp()

    @staticmethod
    def _resolve(table):
        def resolve(host, port):
            return [(None, None, None, "", (table[host], 0))]
        return resolve

    def _fetch(self, url, table):
        from ..providers.standings import fetch_remote_ranking

        self.league.standings_url = url
        return fetch_remote_ranking(self.league, get=self._get, resolve=self._resolve(table))

    def test_private_and_metadata_addresses_are_refused(self):
        for url, ip in (("http://db:5432/", "172.18.0.2"), ("http://meta/latest", "169.254.169.254"),
                        ("http://local/", "127.0.0.1"), ("file:///etc/passwd", "8.8.8.8")):
            from urllib.parse import urlsplit

            host = urlsplit(url).hostname or "x"
            self.assertIsNone(self._fetch(url, {host: ip, "x": ip}), url)
        self.assertEqual(self.fetched, [])

    def test_a_public_page_is_read(self):
        self.assertEqual(len(self._fetch("https://example.com/classifica", {"example.com": "93.184.216.34"})), 1)
