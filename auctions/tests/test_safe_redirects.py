"""``?next=`` after a login or a PIN must never bounce the browser off-site.

A crafted link such as ``/login/?next=https://evil.example/`` would otherwise
land a freshly authenticated user on a look-alike page: the classic post-login
phishing redirect. Every view that honours ``next`` goes through
``views.common.safe_next``.
"""
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from .. import remote
from ..models import League, Participant
from ..views.common import safe_next

OFFSITE = ("https://evil.example/", "//evil.example/", "/\\evil.example/")


class SafeNextTests(TestCase):
    def test_offsite_targets_fall_back(self):
        for target in OFFSITE:
            with self.subTest(target=target):
                request = RequestFactory().get("/", {"next": target})
                self.assertEqual(safe_next(request, "/fallback/"), "/fallback/")

    def test_same_site_targets_are_kept(self):
        for target in ("/app/rosa/", "/dashboard/?league=3", "http://testserver/app/"):
            with self.subTest(target=target):
                request = RequestFactory().get("/", {"next": target})
                self.assertEqual(safe_next(request, "/fallback/"), target)

    def test_reads_post_too(self):
        request = RequestFactory().post("/", {"next": "/app/rosa/"})
        self.assertEqual(safe_next(request, "/fallback/"), "/app/rosa/")
        request = RequestFactory().post("/", {"next": "//evil.example/"})
        self.assertEqual(safe_next(request, "/fallback/"), "/fallback/")

    def test_no_next_is_the_fallback(self):
        self.assertEqual(safe_next(RequestFactory().get("/"), "/fallback/"), "/fallback/")


class LoginNextTests(TestCase):
    def setUp(self):
        User.objects.create_superuser("boss", "boss@example.com", "pw-boss-1")
        User.objects.create_user("mario", "mario@example.com", "pw-mario-1")

    def _login(self, next_url, via="post"):
        data = {"identifier": "mario", "password": "pw-mario-1"}
        if via == "post":
            return self.client_class().post("/login/", {**data, "next": next_url})
        return self.client_class().post("/login/?" + urlencode({"next": next_url}), data)

    def test_offsite_next_falls_back_to_home(self):
        for target in OFFSITE:
            for via in ("post", "get"):
                with self.subTest(target=target, via=via):
                    r = self._login(target, via)
                    self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_same_site_next_is_kept(self):
        r = self._login("/app/rosa/")
        self.assertRedirects(r, "/app/rosa/", fetch_redirect_response=False)

    def test_login_page_does_not_echo_an_offsite_next(self):
        r = self.client.get("/login/?" + urlencode({"next": "//evil.example/"}))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "evil.example")


class AppLoginNextTests(TestCase):
    def setUp(self):
        league = League.objects.create(name="L", budget=Decimal("500"))
        self.team = Participant.objects.create(display_name="Alfa", league=league,
                                               access_code="ALFA01", credits=Decimal("500"))

    def test_offsite_next_after_access_code_falls_back_to_the_app(self):
        for target in OFFSITE:
            with self.subTest(target=target):
                r = self.client_class().post("/app/login/",
                                             {"access_code": "ALFA01", "next": target})
                self.assertRedirects(r, "/app/", fetch_redirect_response=False)

    def test_offsite_next_after_a_token_link_falls_back_to_the_app(self):
        for target in OFFSITE:
            with self.subTest(target=target):
                query = urlencode({"t": self.team.public_token, "next": target})
                r = self.client_class().get("/app/login/?" + query)
                self.assertRedirects(r, "/app/", fetch_redirect_response=False)

    def test_same_site_next_is_kept(self):
        r = self.client.post("/app/login/", {"access_code": "ALFA01", "next": "/app/mercato/"})
        self.assertRedirects(r, "/app/mercato/", fetch_redirect_response=False)


class PinNextTests(TestCase):
    """/portal/ and /regia/unlock/ redirect to ``next`` once the PIN is right."""

    host = "abc-def.trycloudflare.com"

    def setUp(self):
        remote.stop()
        self.addCleanup(remote.stop)
        self.addCleanup(remote._set, pin="")
        remote._set(status="on", url=f"https://{self.host}", host=self.host, pin="424242")

    def test_portal_offsite_next_falls_back_to_the_dashboard(self):
        for target in OFFSITE:
            with self.subTest(target=target):
                r = self.client_class().post("/portal/", {"pin": "424242", "next": target})
                self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)

    def test_portal_same_site_next_is_kept(self):
        r = self.client.post("/portal/", {"pin": "424242", "next": "/app/"})
        self.assertRedirects(r, "/app/", fetch_redirect_response=False)

    def test_unlock_offsite_next_falls_back_to_the_console(self):
        for target in OFFSITE:
            with self.subTest(target=target):
                r = self.client_class().post("/regia/unlock/?" + urlencode({"next": target}),
                                             {"pin": "424242"}, HTTP_HOST=self.host)
                self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)

    def test_unlock_same_site_next_is_kept(self):
        r = self.client.post("/regia/unlock/", {"pin": "424242", "next": "/admin-auction/players/"},
                             HTTP_HOST=self.host)
        self.assertRedirects(r, "/admin-auction/players/", fetch_redirect_response=False)

    def test_unlock_page_does_not_echo_an_offsite_next(self):
        r = self.client.get("/regia/unlock/?" + urlencode({"next": "//evil.example/"}),
                            HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "evil.example")
