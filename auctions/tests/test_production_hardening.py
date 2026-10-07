"""Production hardening: the defaults and doors that matter once the app is
reachable by people other than whoever runs it."""
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import Auction, League, LeagueConfig, Participant, Player
from .. import remote, throttle

BASE_DIR = Path(settings.BASE_DIR)


class SettingsGuardTests(TestCase):
    """With DEBUG off, the app refuses to start on a key everybody can read."""

    def _check(self, **env):
        run_env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
        run_env.update(env)
        return subprocess.run([sys.executable, "manage.py", "check"], cwd=BASE_DIR,
                              env=run_env, capture_output=True, text=True, timeout=120)

    def test_debug_off_without_a_key_refuses_to_start(self):
        r = self._check(DJANGO_DEBUG="False")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)

    def test_debug_off_with_the_old_compose_default_refuses_to_start(self):
        r = self._check(DJANGO_DEBUG="False",
                        DJANGO_SECRET_KEY="fantamanager-secret-key-production-change-me")
        self.assertNotEqual(r.returncode, 0)

    def test_debug_off_with_a_real_key_starts(self):
        r = self._check(DJANGO_DEBUG="False", DJANGO_SECRET_KEY="x" * 50)
        self.assertEqual(r.returncode, 0, r.stderr)


class SeedDataTests(TestCase):
    def test_the_published_seed_carries_no_accounts_or_sessions(self):
        """data/seed_data.json is in a public repository: no password hashes,
        no session keys."""
        seed = json.loads((BASE_DIR / "data" / "seed_data.json").read_text(encoding="utf-8"))
        models = {o["model"] for o in seed}
        self.assertNotIn("auth.user", models)
        self.assertNotIn("sessions.session", models)


class MediaServingTests(TestCase):
    def test_uploads_are_served_with_debug_off(self):
        """Team logos used to need DEBUG on (``static()`` is a no-op without it)."""
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            (Path(tmp) / "logos").mkdir()
            (Path(tmp) / "logos" / "crest.png").write_bytes(b"\x89PNG fake")
            r = self.client.get("/media/logos/crest.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(b"".join(r.streaming_content), b"\x89PNG fake")
            # Path traversal is refused (400: Django flags it as suspicious).
            self.assertIn(self.client.get("/media/../settings.py").status_code, (400, 404))


class OwnerlessLeagueTests(TestCase):
    """A league with no owner is superadmin business, not "any logged-in account"."""

    def setUp(self):
        self.league = League.objects.create(name="Orfana", budget=Decimal("500"))
        self.team = Participant.objects.create(league=self.league, display_name="Squadra",
                                               credits=Decimal("500"))

    def test_a_league_admin_cannot_manage_an_ownerless_league(self):
        admin = User.objects.create_user("admin_lega", password="pw")
        League.objects.create(name="Mia", owner=admin)
        self.client.force_login(admin)
        r = self.client.post(f"/dashboard/team/{self.team.id}/credits/", {"mode": "set", "amount": "1"})
        self.assertEqual(r.status_code, 403)
        self.team.refresh_from_db()
        self.assertEqual(self.team.credits, Decimal("500"))

    def test_the_superadmin_still_can(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.local", "pw"))
        self.client.post(f"/dashboard/team/{self.team.id}/credits/", {"mode": "set", "amount": "1"})
        self.team.refresh_from_db()
        self.assertEqual(self.team.credits, Decimal("1"))


class SuperadminOnlyToolsTests(TestCase):
    def test_system_log_is_for_the_superadmin_only(self):
        admin = User.objects.create_user("admin_lega", password="pw")
        League.objects.create(name="Mia", owner=admin)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("admin_logs_tail")).status_code, 403)
        self.client.force_login(User.objects.create_superuser("root", "r@x.local", "pw"))
        self.assertEqual(self.client.get(reverse("admin_logs_tail")).status_code, 200)

    def test_quit_does_not_exist_on_a_server(self):
        """Stopping the process would take the service down for every league."""
        self.client.force_login(User.objects.create_superuser("root", "r@x.local", "pw"))
        with override_settings(DESKTOP_APP=False):
            self.assertEqual(self.client.post(reverse("admin_quit")).status_code, 404)

    @override_settings(DESKTOP_APP=True)
    def test_quit_on_the_desktop_is_for_the_superadmin_only(self):
        admin = User.objects.create_user("admin_lega", password="pw")
        self.client.force_login(admin)
        self.assertEqual(self.client.post(reverse("admin_quit")).status_code, 403)


class TeamClaimTests(TestCase):
    """Registration is open: a team must not go to whoever asks for it."""

    def setUp(self):
        self.owner = User.objects.create_user("presidente", password="pw")
        self.league = League.objects.create(name="Lega", owner=self.owner)
        self.open_team = Participant.objects.create(league=self.league, display_name="Senza Codice",
                                                    access_code="")
        self.coded = Participant.objects.create(league=self.league, display_name="Con Codice",
                                                access_code="SEGRETO")

    def test_a_stranger_account_cannot_take_a_team_without_a_code(self):
        self.client.force_login(User.objects.create_user("sconosciuto", password="pw"))
        r = self.client.post(reverse("app_login"), {"participant_id": self.open_team.id})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.client.session.get("participant_id"))
        self.open_team.refresh_from_db()
        self.assertIsNone(self.open_team.user_id)

    def test_the_league_owner_can_take_it(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse("app_login"), {"participant_id": self.open_team.id})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.session.get("participant_id"), self.open_team.id)

    def test_a_code_does_not_take_a_team_away_from_its_account(self):
        holder = User.objects.create_user("titolare", password="pw")
        self.coded.user = holder
        self.coded.save(update_fields=["user"])
        self.client.force_login(User.objects.create_user("altro", password="pw"))
        r = self.client.post(reverse("onboarding"), {"action": "join_team", "access_code": "SEGRETO"})
        self.assertEqual(r.status_code, 200)
        self.coded.refresh_from_db()
        self.assertEqual(self.coded.user, holder)


class ThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user("mario", password="giusta")
        self.league = League.objects.create(name="Lega")
        self.team = Participant.objects.create(league=self.league, display_name="Squadra",
                                               access_code="4821")

    def test_passwords_cannot_be_guessed_without_limit(self):
        limit, _ = throttle.LIMITS["login"]
        for _ in range(limit):
            self.client.post(reverse("login"), {"identifier": "mario", "password": "sbagliata"})
        r = self.client.post(reverse("login"), {"identifier": "mario", "password": "giusta"})
        self.assertContains(r, "Troppi tentativi")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_django_admin_login_shares_the_limit(self):
        User.objects.create_superuser("root", "root@x.local", "giusta-root")
        limit, _ = throttle.LIMITS["login"]
        for _ in range(limit):
            self.client.post("/django-admin/login/", {"username": "root", "password": "sbagliata"})
        r = self.client.post("/django-admin/login/", {"username": "root", "password": "giusta-root"})
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_correct_password_within_the_limit_still_works(self):
        self.client.post(reverse("login"), {"identifier": "mario", "password": "sbagliata"})
        r = self.client.post(reverse("login"), {"identifier": "mario", "password": "giusta"})
        self.assertEqual(r.status_code, 302)

    def test_team_codes_cannot_be_walked(self):
        """A 4-digit PIN is 10,000 guesses: without a ceiling, minutes of work."""
        limit, _ = throttle.LIMITS["code"]
        for n in range(limit):
            self.client.post(reverse("app_login"), {"login_mode": "code", "access_code": f"{n:04d}"})
        r = self.client.post(reverse("app_login"), {"login_mode": "code", "access_code": "4821"})
        self.assertContains(r, "Troppi tentativi")
        self.assertIsNone(self.client.session.get("participant_id"))

    def test_behind_the_proxy_each_client_has_its_own_count(self):
        """All requests reach Daphne from the proxy's address: counting that
        would lock every user out because of one."""
        with override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")):
            limit, _ = throttle.LIMITS["login"]
            for _ in range(limit):
                self.client.post(reverse("login"), {"identifier": "mario", "password": "x"},
                                 HTTP_X_FORWARDED_FOR="203.0.113.7")
            r = self.client.post(reverse("login"), {"identifier": "mario", "password": "giusta"},
                                 HTTP_X_FORWARDED_FOR="198.51.100.2")
            self.assertEqual(r.status_code, 302)


class LoginBootstrapTests(TestCase):
    def test_on_a_server_logging_in_never_makes_a_superadmin(self):
        User.objects.create_user("primo", password="pw")
        with override_settings(DESKTOP_APP=False):
            self.client.post(reverse("login"), {"identifier": "primo", "password": "pw"})
        self.assertFalse(User.objects.get(username="primo").is_superuser)

    def test_on_the_desktop_an_old_database_gets_its_superadmin(self):
        User.objects.create_user("primo", password="pw")
        with override_settings(DESKTOP_APP=True):
            self.client.post(reverse("login"), {"identifier": "primo", "password": "pw"})
        self.assertTrue(User.objects.get(username="primo").is_superuser)


class TenantIsolationTests(TestCase):
    """Anyone can sign up and so reach the console: an organiser must never
    touch, or even see, another organiser's league."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.local", "pwd12345")
        self.me = User.objects.create_user("presidente", "pres@x.local", "pwd12345")
        self.them = User.objects.create_user("vicino", "vicino@x.local", "pwd12345")
        self.mine = League.objects.create(name="Mia", owner=self.me)
        self.theirs = League.objects.create(name="Altrui", owner=self.them)
        self.their_team = Participant.objects.create(league=self.theirs, display_name="Squadra Altrui")
        Player.objects.create(league=self.theirs, name="Bomber", role="A", team="Inter",
                              owner=self.their_team)
        self.client.force_login(self.me)

    def test_no_auction_in_someone_elses_league(self):
        resp = self.client.post(reverse("admin_wizard_create"),
                                {"league_id": self.theirs.id, "title": "Presa", "start_now": "1"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Auction.objects.filter(league=self.theirs).exists())

    def test_a_new_league_cannot_take_other_teams(self):
        free = Participant.objects.create(display_name="Senza lega")
        self.client.post(reverse("admin_create_league"),
                         {"name": "Nuova", "attach_ids": [self.their_team.id, free.id]})
        self.their_team.refresh_from_db()
        free.refresh_from_db()
        self.assertEqual(self.their_team.league, self.theirs)
        self.assertIsNone(free.league)     # the global pool is the superadmin's

    def test_the_superadmin_still_adopts_league_less_teams(self):
        free = Participant.objects.create(display_name="Senza lega")
        self.client.force_login(self.root)
        self.client.post(reverse("admin_create_league"),
                         {"name": "Nuova", "attach_ids": [self.their_team.id, free.id]})
        free.refresh_from_db()
        self.their_team.refresh_from_db()
        self.assertEqual(free.league.name, "Nuova")
        self.assertEqual(self.their_team.league, self.theirs)   # never a team that plays elsewhere

    def test_an_organisers_league_does_not_rewrite_the_global_defaults(self):
        before = LeagueConfig.get().budget
        self.client.post(reverse("admin_create_league"), {"name": "Nuova", "budget": "7"})
        self.assertEqual(LeagueConfig.get().budget, before)
        self.assertTrue(League.objects.filter(name="Nuova", owner=self.me).exists())

    def test_pickers_list_only_my_leagues(self):
        for name in ("admin_auction_wizard", "admin_create_league", "admin_export"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertNotContains(resp, "Altrui", msg_prefix=name)

    def test_exports_never_fall_back_to_every_league(self):
        for name in ("admin_export_xlsx", "admin_export_csv", "admin_export_leghe"):
            resp = self.client.get(reverse(name), {"league": self.theirs.id})
            self.assertEqual(resp.status_code, 403, name)
        resp = self.client.get(reverse("admin_export"), {"league": self.theirs.id})
        self.assertNotContains(resp, "Squadra Altrui")

    def test_the_superadmin_exports_everything(self):
        self.client.force_login(self.root)
        resp = self.client.get(reverse("admin_export_csv"), {"league": "all"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Squadra Altrui", resp.content.decode("utf-8"))

    def test_the_account_list_shows_only_my_people(self):
        Participant.objects.create(league=self.mine, display_name="Mia Squadra")
        resp = self.client.get(f"/dashboard/{self.mine.id}/")
        self.assertEqual(resp.status_code, 200)
        users = {u.username for u in resp.context["available_users"]}
        self.assertNotIn("vicino", users)
        self.assertNotContains(resp, "vicino@x.local")

    def test_a_stranger_account_cannot_be_linked_to_my_team(self):
        team = Participant.objects.create(league=self.mine, display_name="Mia Squadra")
        self.client.post(reverse("admin_edit_participant", args=[team.id]),
                         {"display_name": "Mia Squadra", "user_id": self.them.id, "is_active": "1"})
        team.refresh_from_db()
        self.assertIsNone(team.user)

    def test_no_team_in_the_global_pool(self):
        League.objects.all().delete()
        self.client.post(reverse("admin_create_participant"), {"display_name": "Abusiva"})
        self.assertFalse(Participant.objects.filter(display_name="Abusiva").exists())


class RemoteAccessIsTheSuperadminsTests(TestCase):
    """The tunnel opens the whole server, and its status carries the regia PIN."""

    def setUp(self):
        self.me = User.objects.create_user("presidente", password="pwd12345")
        self.client.force_login(self.me)

    def test_an_organiser_can_neither_read_nor_open_the_tunnel(self):
        with mock.patch.object(remote, "start") as start, mock.patch.object(remote, "stop") as stop:
            self.assertEqual(self.client.get(reverse("admin_remote_status")).status_code, 403)
            self.assertEqual(self.client.get(reverse("admin_remote_page")).status_code, 403)
            self.assertEqual(self.client.post(reverse("admin_remote_start"), {"port": "5432"}).status_code, 403)
            self.assertEqual(self.client.post(reverse("admin_remote_stop")).status_code, 403)
        start.assert_not_called()
        stop.assert_not_called()

    def test_the_regia_page_does_not_carry_the_pin(self):
        league = League.objects.create(name="Mia", owner=self.me)
        auction = Auction.objects.create(league=league, title="Asta")
        with mock.patch.object(remote, "status", return_value={"status": "on", "pin": "424242"}):
            resp = self.client.get(f"/regia/{auction.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "424242")

    def test_the_superadmin_reads_it(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.local", "pwd12345"))
        self.assertEqual(self.client.get(reverse("admin_remote_status")).status_code, 200)

    def test_there_is_no_fantapazz_browser_login(self):
        with mock.patch("subprocess.Popen") as popen:
            resp = self.client.post("/dashboard/fantapazz/browser-login/")
        self.assertEqual(resp.status_code, 404)
        popen.assert_not_called()


class SignInPageTests(TestCase):
    def setUp(self):
        league = League.objects.create(name="Lega Privata")
        Auction.objects.create(league=league, title="Asta Segreta", status=Auction.Status.LIVE)

    @override_settings(PUBLIC_TOKENS_REQUIRED=True)
    def test_online_strangers_do_not_see_the_running_auctions(self):
        for path in ("/login/", "/register/"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertNotContains(resp, "Asta Segreta", msg_prefix=path)
            self.assertNotContains(resp, "Lega Privata", msg_prefix=path)

    @override_settings(PUBLIC_TOKENS_REQUIRED=False)
    def test_on_the_lan_they_are_quick_links(self):
        self.assertContains(self.client.get("/login/"), "Asta Segreta")


class SupervisorBackupDownloadTests(TestCase):
    def setUp(self):
        self.superadmin = User.objects.create_superuser("super", "super@test.local", "pass1234")
        self.user = User.objects.create_user("normal", "normal@test.local", "pass1234")

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("supervisor_backup_download"))
        self.assertEqual(resp.status_code, 302)

    def test_normal_user_forbidden(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("supervisor_backup_download"))
        self.assertEqual(resp.status_code, 403)

    def test_path_traversal_blocked(self):
        self.client.force_login(self.superadmin)
        resp = self.client.get(reverse("supervisor_backup_download") + "?file=../../manage.py")
        self.assertIn(resp.status_code, (403, 404))

    def test_superadmin_can_download_snapshot(self):
        # The test database lives in memory: no file to copy, so the
        # snapshot is simulated; what is checked is that it gets served.
        self.client.force_login(self.superadmin)
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "db-20261006-120000.sqlite3"
            snap.write_bytes(b"SQLite format 3\x00")
            # SQLite branch on any test database (CI also runs on PostgreSQL).
            with mock.patch("auctions.backup._is_postgres", return_value=False), \
                    mock.patch("auctions.backup._db_path", return_value=Path(tmp) / "db.sqlite3"), \
                    mock.patch("auctions.backup.backup_database", return_value=snap):
                resp = self.client.get(reverse("supervisor_backup_download"))
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.has_header("Content-Disposition"))
            self.assertIn("attachment", resp["Content-Disposition"])
            self.assertIn(snap.name, resp["Content-Disposition"])
            resp.close()

    def test_postgres_serves_the_latest_dump_not_the_request_file(self):
        self.client.force_login(self.superadmin)
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=tmp), \
                mock.patch("auctions.backup._is_postgres", return_value=True):
            dump = Path(tmp) / "pg-20261006-120000.sql.gz"
            dump.write_bytes(b"\x1f\x8b dump")
            resp = self.client.get(reverse("supervisor_backup_download"))
            self.assertEqual(resp.status_code, 200)
            self.assertIn(dump.name, resp["Content-Disposition"])
            self.assertEqual(b"".join(resp.streaming_content), b"\x1f\x8b dump")
            # A fresh dump was asked for, for the next download.
            self.assertTrue(any(p.name != dump.name for p in Path(tmp).iterdir()))

    def test_postgres_without_dumps_is_404(self):
        self.client.force_login(self.superadmin)
        with tempfile.TemporaryDirectory() as tmp, override_settings(BACKUP_DIR=tmp), \
                mock.patch("auctions.backup._is_postgres", return_value=True):
            resp = self.client.get(reverse("supervisor_backup_download"))
            self.assertEqual(resp.status_code, 404)

