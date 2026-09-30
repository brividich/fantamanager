"""Admin views that act on a league named in the request (a posted
``league_id``, ``?league=``, a saved session) must check that the logged-in
user manages that league.

``staff_member_required`` only proves the user is logged in, and registration
is open: a manager who merely owns a team, or the admin of another league, is
logged in too. Data with no league at all (the legacy global pool) belongs to
superusers only.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from .. import remote, services
from ..models import Auction, AuctionSession, League, Participant, Player
from .common import make_live_auction

LISTONE_CSV = (
    "Id,R,Nome,Squadra,Qt.A,FVM\n"
    "9001,A,Nuovo Bomber,Juventus,20,45\n"
    "9002,D,Nuovo Terzino,Inter,5,10\n"
)
STATS_CSV = "Nome;Squadra;Pv;Mv;Fm;Gf;Ass\nAlfa Striker;Atalanta;30;7;9;20;5\n"
ROSE_CSV = "$,$,$\nDinamo Losca,7212,6\n"


def _upload(name, text):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class AdminLeagueRbacBase(TestCase):
    def setUp(self):
        remote.stop()
        self.addCleanup(remote.stop)
        self.client = Client()
        self.superadmin = User.objects.create_superuser("root", "root@x.local", "pw")
        self.owner = User.objects.create_user("admin_alfa", password="pw")
        self.foreign_admin = User.objects.create_user("admin_beta", password="pw")
        self.manager = User.objects.create_user("mario_manager", password="pw")

        self.league = League.objects.create(name="Lega Alfa", owner=self.owner, external_id="332175")
        self.other_league = League.objects.create(name="Lega Beta", owner=self.foreign_admin)

        self.team = Participant.objects.create(
            league=self.league, display_name="Alfa Real", access_code="ALFA01",
            user=self.manager, credits=Decimal("500"),
        )
        self.foreign_team = Participant.objects.create(
            league=self.other_league, display_name="Beta United", access_code="BETA01",
        )
        self.legacy_team = Participant.objects.create(
            league=None, display_name="Vecchia Guardia", access_code="OLD001",
        )
        self.player = Player.objects.create(
            league=self.league, name="Alfa Striker", role="A", team="Atalanta",
            initial_price=Decimal("10"), ext_id="7212",
        )
        self.foreign_player = Player.objects.create(
            league=self.other_league, name="Beta Striker", role="A", initial_price=Decimal("15"),
        )
        self.legacy_player = Player.objects.create(
            league=None, name="Legacy Keeper", role="P", initial_price=Decimal("5"),
        )
        self.auction = make_live_auction(league=self.league, title="Asta Alfa")

    def _as(self, user):
        self.client.force_login(user)

    def _intruders(self):
        """A manager who only owns a team, and the admin of another league."""
        return (self.manager, self.foreign_admin)

    def _pool_counts(self):
        return (Player.objects.filter(league=self.league).count(),
                Player.objects.filter(league=self.other_league).count(),
                Player.objects.filter(league__isnull=True).count())


class ClearPlayersTests(AdminLeagueRbacBase):
    url = "/admin-auction/players/clear/"

    def test_intruders_cannot_wipe_a_league_pool(self):
        before = self._pool_counts()
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(self.url, {"league_id": self.league.id})
            self.assertEqual(resp.status_code, 403, user.username)
            self.assertEqual(self._pool_counts(), before)

    def test_no_league_never_reaches_a_non_superuser(self):
        """Without a league the view used to run Player.objects.all().delete()."""
        before = self._pool_counts()
        for user in (*self._intruders(), self.owner):
            self._as(user)
            resp = self.client.post(self.url)
            self.assertEqual(resp.status_code, 403, user.username)
            self.assertEqual(self._pool_counts(), before)

    def test_an_unknown_league_is_a_404_not_the_global_pool(self):
        before = self._pool_counts()
        self._as(self.superadmin)
        for raw in ("999999", "abc"):
            resp = self.client.post(self.url, {"league_id": raw})
            self.assertEqual(resp.status_code, 404, raw)
        self.assertEqual(self._pool_counts(), before)

    def test_owner_wipes_only_their_league(self):
        self._as(self.owner)
        resp = self.client.post(self.url, {"league_id": self.league.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._pool_counts(), (0, 1, 1))

    def test_superuser_wipes_a_league_or_the_global_pool_never_everything(self):
        self._as(self.superadmin)
        resp = self.client.post(self.url, {"league_id": self.other_league.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._pool_counts(), (1, 0, 1))

        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._pool_counts(), (1, 0, 0))


class ImportPlayersTests(AdminLeagueRbacBase):
    url = "/admin-auction/players/import/"

    def test_intruders_cannot_import_into_a_league(self):
        before = self._pool_counts()
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "replace": "1",
                "csv_file": _upload("Quotazioni.csv", LISTONE_CSV),
            })
            self.assertEqual(resp.status_code, 403, user.username)
            self.assertEqual(self._pool_counts(), before)
        self.assertTrue(Player.objects.filter(pk=self.player.pk).exists())

    def test_the_global_pool_is_superuser_only(self):
        self._as(self.owner)
        resp = self.client.post(self.url, {"csv_file": _upload("Quotazioni.csv", LISTONE_CSV)})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Player.objects.filter(name="Nuovo Bomber").exists())

        self._as(self.superadmin)
        resp = self.client.post(self.url, {"csv_file": _upload("Quotazioni.csv", LISTONE_CSV)})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Player.objects.filter(name="Nuovo Bomber", league__isnull=True).exists())

    def test_owner_and_superuser_import_into_the_league(self):
        for user in (self.owner, self.superadmin):
            Player.objects.filter(name="Nuovo Bomber").delete()
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "prune": "0",
                "csv_file": _upload("Quotazioni.csv", LISTONE_CSV),
            })
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertTrue(resp.json()["ok"])
            self.assertTrue(Player.objects.filter(name="Nuovo Bomber", league=self.league).exists())


class ApplyPhotosTests(AdminLeagueRbacBase):
    url = "/admin-auction/players/photos/"

    def test_intruders_cannot_touch_a_league_pool(self):
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "template": "https://evil/{id}.png",
                "only_missing": "0",
            })
            self.assertEqual(resp.status_code, 403, user.username)
            self.player.refresh_from_db()
            self.assertEqual(self.player.photo_url, "")

    def test_the_global_pool_is_superuser_only(self):
        self.legacy_player.ext_id = "1"
        self.legacy_player.save()
        self._as(self.owner)
        resp = self.client.post(self.url, {"template": "https://x/{id}.png"})
        self.assertEqual(resp.status_code, 403)
        self.legacy_player.refresh_from_db()
        self.assertEqual(self.legacy_player.photo_url, "")

    def test_owner_and_superuser_apply_photos(self):
        for user, template in ((self.owner, "https://a/{id}.png"), (self.superadmin, "https://b/{id}.png")):
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "template": template, "only_missing": "0",
            })
            self.assertEqual(resp.status_code, 200, user.username)
            self.player.refresh_from_db()
            self.assertEqual(self.player.photo_url, template.format(id="7212"))


class ImportStatsTests(AdminLeagueRbacBase):
    url = "/admin-auction/players/stats/"

    def test_intruders_cannot_write_stats_into_a_league(self):
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "stats_file": _upload("mie.csv", STATS_CSV),
            })
            self.assertEqual(resp.status_code, 403, user.username)
            self.player.refresh_from_db()
            self.assertIsNone(self.player.goals)

    def test_the_global_pool_is_superuser_only(self):
        self._as(self.owner)
        resp = self.client.post(self.url, {"stats_file": _upload("mie.csv", STATS_CSV)})
        self.assertEqual(resp.status_code, 403)

    def test_owner_and_superuser_import_stats(self):
        for user in (self.owner, self.superadmin):
            Player.objects.filter(pk=self.player.pk).update(goals=None)
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "stats_file": _upload("mie.csv", STATS_CSV),
            })
            self.assertEqual(resp.status_code, 200, user.username)
            self.player.refresh_from_db()
            self.assertEqual(self.player.goals, 20)


class ImportRoseTests(AdminLeagueRbacBase):
    url = "/admin-auction/rose/import/"

    def test_intruders_cannot_assign_rosters_in_a_league(self):
        for user in self._intruders():
            self._as(user)
            for action in ("preview", "import"):
                resp = self.client.post(self.url, {
                    "league_id": self.league.id, "action": action,
                    "rose_file": _upload("rose.csv", ROSE_CSV),
                })
                self.assertEqual(resp.status_code, 403, (user.username, action))
        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)
        self.assertFalse(Participant.objects.filter(display_name="Dinamo Losca").exists())

    def test_the_only_league_fallback_is_checked_too(self):
        """With no id the import falls back to the only league: an outsider
        must not land in it just because it is the only one."""
        self.other_league.delete()
        self._as(self.foreign_admin)
        resp = self.client.post(self.url, {"rose_file": _upload("rose.csv", ROSE_CSV)})
        self.assertEqual(resp.status_code, 403)
        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)

    def test_no_league_is_superuser_only(self):
        self._as(self.owner)
        resp = self.client.post(self.url, {"rose_file": _upload("rose.csv", ROSE_CSV)})
        self.assertEqual(resp.status_code, 403)

    def test_owner_and_superuser_import_rosters(self):
        for user in (self.owner, self.superadmin):
            Player.objects.filter(pk=self.player.pk).update(owner=None, cost=0)
            self._as(user)
            resp = self.client.post(self.url, {
                "league_id": self.league.id, "rose_file": _upload("rose.csv", ROSE_CSV),
            })
            self.assertEqual(resp.status_code, 200, user.username)
            self.player.refresh_from_db()
            team = Participant.objects.get(display_name="Dinamo Losca")
            self.assertEqual(team.league_id, self.league.id)
            self.assertEqual(self.player.owner_id, team.id)


class ParticipantsPageTests(AdminLeagueRbacBase):
    url = "/admin-auction/participants/"

    def test_intruders_cannot_open_a_foreign_league_by_id(self):
        for user in self._intruders():
            self._as(user)
            resp = self.client.get(f"{self.url}?league={self.league.id}")
            self.assertEqual(resp.status_code, 403, user.username)
            self.assertNotIn(self.team.public_token, resp.content.decode())

    def test_without_a_league_no_foreign_join_links_leak(self):
        """No league picked used to list every team of every league."""
        for user in self._intruders():
            self._as(user)
            resp = self.client.get(self.url)
            self.assertEqual(resp.status_code, 200, user.username)
            body = resp.content.decode()
            self.assertNotIn(self.team.public_token, body)
            self.assertNotIn(self.legacy_team.public_token, body)
            self.assertNotIn(self.league, list(resp.context["leagues"]))

    def test_owner_and_superuser_see_the_join_links(self):
        for user in (self.owner, self.superadmin):
            self._as(user)
            resp = self.client.get(f"{self.url}?league={self.league.id}")
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertIn(self.team.public_token, resp.content.decode())
            self.assertNotIn(self.foreign_team.public_token, resp.content.decode())

    def test_superuser_still_sees_every_team_without_a_league(self):
        self._as(self.superadmin)
        body = self.client.get(f"{self.url}?league=all").content.decode()
        self.assertIn(self.team.public_token, body)
        self.assertIn(self.legacy_team.public_token, body)


class ParticipantQrTests(AdminLeagueRbacBase):
    def _qr(self, participant, **params):
        return self.client.get(f"/participants/{participant.id}/qr.png", params)

    def _assert_png(self, resp, msg=None):
        if resp.status_code == 503:
            self.skipTest("qrcode non installato")
        self.assertEqual(resp.status_code, 200, msg)
        self.assertEqual(resp["Content-Type"], "image/png")

    def test_sequential_ids_do_not_hand_out_join_tokens(self):
        for params in ({}, {"a": self.auction.id}, {"a": self.auction.id, "t": "wrong"},
                       {"a": self.auction.id, "t": ""}):
            resp = self._qr(self.team, **params)
            self.assertEqual(resp.status_code, 403, params)

    def test_intruders_get_403(self):
        for user in self._intruders():
            self._as(user)
            self.assertEqual(self._qr(self.team).status_code, 403, user.username)

    def test_another_leagues_screen_token_does_not_open_this_team(self):
        foreign_auction = make_live_auction(league=self.other_league)
        resp = self._qr(self.team, a=foreign_auction.id, t=foreign_auction.public_token)
        self.assertEqual(resp.status_code, 403)

    def test_the_big_screen_passes_with_its_auction_token(self):
        resp = self._qr(self.team, a=self.auction.id, t=self.auction.public_token)
        self._assert_png(resp)
        self.assertIn("private", resp["Cache-Control"])

    def test_the_screen_page_embeds_its_token_in_the_qr_links(self):
        body = self.client.get(f"/screen/{self.auction.id}/").content.decode()
        self.assertIn(f"/participants/{self.team.id}/qr.png?a={self.auction.id}"
                      f"&t={self.auction.public_token}", body)

    def test_owner_and_superuser_get_the_qr(self):
        for user in (self.owner, self.superadmin):
            self._as(user)
            self._assert_png(self._qr(self.team), user.username)

    def test_a_team_without_a_league_is_superuser_only(self):
        self._as(self.owner)
        self.assertEqual(self._qr(self.legacy_team).status_code, 403)
        self._as(self.superadmin)
        self._assert_png(self._qr(self.legacy_team))


class ResumeSessionTests(AdminLeagueRbacBase):
    def setUp(self):
        super().setUp()
        self.session = services.save_session(self.auction.id, name="Snap Alfa")
        self.legacy_session = AuctionSession.objects.create(
            name="Snap orfana", league=None,
            data={"participants": [{"display_name": "Orfani", "credits": "500"}]},
        )

    def _resumed(self, session):
        return Auction.objects.filter(resumed_from_session=session)

    def test_intruders_cannot_resume_a_foreign_session(self):
        leagues = League.objects.count()
        for user in self._intruders():
            self._as(user)
            resp = self.client.post(f"/admin-auction/sessions/{self.session.id}/resume/")
            self.assertEqual(resp.status_code, 403, user.username)
        self.assertFalse(self._resumed(self.session).exists())
        self.assertEqual(League.objects.count(), leagues)

    def test_a_session_without_a_league_is_superuser_only(self):
        self._as(self.owner)
        resp = self.client.post(f"/admin-auction/sessions/{self.legacy_session.id}/resume/")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self._resumed(self.legacy_session).exists())

        self._as(self.superadmin)
        resp = self.client.post(f"/admin-auction/sessions/{self.legacy_session.id}/resume/")
        self.assertEqual(resp.status_code, 302)
        revived = self._resumed(self.legacy_session).get()
        self.assertEqual(revived.league.owner, self.superadmin)

    def test_owner_and_superuser_resume_and_the_new_league_keeps_its_owner(self):
        for user in (self.owner, self.superadmin):
            self._as(user)
            resp = self.client.post(f"/admin-auction/sessions/{self.session.id}/resume/")
            self.assertEqual(resp.status_code, 302, user.username)
        revived = list(self._resumed(self.session).select_related("league"))
        self.assertEqual(len(revived), 2)
        # Never an ownerless league every account could administer.
        self.assertEqual({a.league.owner_id for a in revived}, {self.owner.id})

    def test_resume_latest_only_picks_a_session_the_user_manages(self):
        self._as(self.foreign_admin)
        resp = self.client.post("/admin-auction/sessions/resume-latest/")
        self.assertRedirects(resp, reverse("admin_sessions"), fetch_redirect_response=False)
        self.assertFalse(self._resumed(self.session).exists())
        self.assertFalse(self._resumed(self.legacy_session).exists())

        self._as(self.owner)
        resp = self.client.post("/admin-auction/sessions/resume-latest/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self._resumed(self.session).exists())

    def test_the_sessions_list_hides_foreign_sessions(self):
        self._as(self.foreign_admin)
        sessions = list(self.client.get("/admin-auction/sessions/").context["sessions"])
        self.assertEqual(sessions, [])

        self._as(self.owner)
        sessions = list(self.client.get("/admin-auction/sessions/").context["sessions"])
        self.assertEqual(sessions, [self.session])

        self._as(self.superadmin)
        sessions = set(self.client.get("/admin-auction/sessions/").context["sessions"])
        self.assertEqual(sessions, {self.session, self.legacy_session})


class FantapazzImportTests(AdminLeagueRbacBase):
    ROSE = [{"name": "Dinamo Losca", "credits": 10,
             "players": [{"name": "Alfa Striker", "role": "A", "club": "Atalanta", "cost": 6}]}]

    def _import_rose(self, **data):
        with mock.patch("auctions.providers.importers.parse_rose_xls", return_value=self.ROSE):
            return self.client.post("/admin-auction/fantapazz/import-rose/", {
                "rose_file": _upload("rose.xls", "x"), **data,
            })

    def test_intruders_cannot_import_rosters_into_a_league(self):
        for user in self._intruders():
            self._as(user)
            resp = self._import_rose(target_league_id=self.league.id)
            self.assertEqual(resp.status_code, 403, user.username)
        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)
        self.assertFalse(Participant.objects.filter(display_name="Dinamo Losca").exists())

    def test_the_fantapazz_id_only_matches_the_users_own_leagues(self):
        """Lega Alfa mirrors Fantapazz league 332175; another admin importing the
        same Fantapazz league lands in their own league, never in Lega Alfa."""
        self._as(self.foreign_admin)
        resp = self._import_rose(league_id="332175")
        self.assertEqual(resp.status_code, 200)
        team = Participant.objects.get(display_name="Dinamo Losca")
        self.assertEqual(team.league_id, self.other_league.id)
        self.player.refresh_from_db()
        self.assertIsNone(self.player.owner_id)

    def test_no_league_is_superuser_only(self):
        self._as(self.manager)
        resp = self._import_rose()
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Participant.objects.filter(display_name="Dinamo Losca").exists())

    def test_owner_and_superuser_import_rosters(self):
        for user in (self.owner, self.superadmin):
            Participant.objects.filter(display_name="Dinamo Losca").delete()
            self._as(user)
            resp = self._import_rose(target_league_id=self.league.id)
            self.assertEqual(resp.status_code, 200, user.username)
            team = Participant.objects.get(display_name="Dinamo Losca")
            self.assertEqual(team.league_id, self.league.id)

    def test_the_import_action_is_refused_before_contacting_fantapazz(self):
        self._as(self.foreign_admin)
        with mock.patch("auctions.views.admin_fantapazz._fp_provider") as provider:
            resp = self.client.post("/admin-auction/fantapazz/", {
                "action": "import", "target_league_id": self.league.id, "cookie": "x",
            })
        self.assertEqual(resp.status_code, 403)
        provider.assert_not_called()
        self.assertEqual(Player.objects.filter(league=self.league).count(), 1)

    def test_the_page_only_lists_manageable_leagues(self):
        self._as(self.foreign_admin)
        resp = self.client.get("/admin-auction/fantapazz/")
        self.assertEqual(list(resp.context["leagues"]), [self.other_league])
