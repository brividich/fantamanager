"""Anagrafica comune dei calciatori (API-Football) e collegamento dei listoni."""
import os
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..models import Footballer, League, Participant, Player
from ..providers import apifootball
from ..providers.importers import sync_players
from ..services import footballers

JUVE, INTER = (496, "Juventus", "https://media.api-sports.io/football/teams/496.png"), \
    (505, "Inter", "https://media.api-sports.io/football/teams/505.png")


class Resp:
    def __init__(self, data, headers=None):
        self.data = data
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def squad_player(pid, name, position, age=25, number=None):
    return {"id": pid, "name": name, "age": age, "number": number, "position": position,
            "photo": f"https://media.api-sports.io/football/players/{pid}.png"}


SQUADS = {
    496: [squad_player(1, "D. Vlahović", "Attacker", 25, 9), squad_player(2, "K. Yıldız", "Attacker", 20, 10),
          squad_player(3, "M. Locatelli", "Midfielder", 27, 5)],
    505: [squad_player(4, "Lautaro Martínez", "Attacker", 28, 10), squad_player(5, "Josep Martínez", "Goalkeeper", 27, 13),
          squad_player(6, "N. Barella", "Midfielder", 28, 23)],
}


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        apifootball._refused_seasons.clear()
        apifootball._limits.update(minute=None, day=None)
        self.addCleanup(apifootball._refused_seasons.clear)
        self.addCleanup(apifootball._limits.update, minute=None, day=None)
        patcher = mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test", "APIFOOTBALL_SEASON": "2026"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fake_get(self, squads=None, teams_refused=False, fail_on=None):
        squads = SQUADS if squads is None else squads

        def get(url, params=None, **kw):
            path = url.split("v3.football.api-sports.io", 1)[-1]
            self.calls.append((path, dict(params)))
            if path == "/teams" and "league" in params:
                if teams_refused:
                    return Resp({"errors": {"plan": "Free plans do not have access to this season."}})
                return Resp({"response": [{"team": {"id": t[0], "name": t[1], "logo": t[2]}} for t in (JUVE, INTER)]})
            if path == "/teams":
                return Resp({"response": [{"team": {"id": 496, "name": "Juventus"}},
                                          {"team": {"id": 505, "name": "Inter"}}]})
            if path == "/players/squads":
                if params["team"] == fail_on:
                    return Resp({"errors": {"requests": "You have reached the request limit for the day"}})
                return Resp({"response": [{"team": {"id": params["team"]}, "players": squads.get(params["team"], [])}]})
            raise AssertionError(path)
        return get

    def sync(self, **kw):
        return footballers.sync_registry(get=self.fake_get(**kw), sleep=lambda s: None)


class RegistrySyncTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.league = League.objects.create(name="L")
        self.other = League.objects.create(name="Altra")
        mk = lambda name, role, team, league=self.league: Player.objects.create(
            name=name, role=role, team=team, league=league)
        self.vlahovic = mk("Vlahovic", "A", "Juventus")
        self.yildiz = mk("Yildiz", "A", "Juventus")
        self.lautaro = mk("Martinez L.", "A", "Inter")
        self.josep = mk("Martinez Jo.", "P", "Inter")
        self.barella_other = mk("Barella", "C", "Inter", self.other)

    def test_downloads_every_serie_a_squad_once(self):
        report = self.sync()
        self.assertEqual(report["error"], "")
        self.assertEqual((report["clubs"], report["players"], report["created"]), (2, 6, 6))
        self.assertEqual([c[0] for c in self.calls], ["/teams", "/players/squads", "/players/squads"])
        lautaro = Footballer.objects.get(api_id=4)
        self.assertEqual((lautaro.position, lautaro.club_name, lautaro.club_api_id, lautaro.number, lautaro.age),
                         ("A", "Inter", 505, 10, 28))
        self.assertEqual(lautaro.club_logo, INTER[2])
        self.assertTrue(lautaro.in_serie_a)

    def test_links_the_listone_of_every_league(self):
        self.sync()
        for player, api_id in ((self.vlahovic, 1), (self.yildiz, 2), (self.lautaro, 4), (self.josep, 5),
                               (self.barella_other, 6)):
            player.refresh_from_db()
            self.assertEqual(player.footballer.api_id, api_id, player.name)
        # La foto arriva dall'anagrafica solo se il giocatore non ne aveva.
        self.assertEqual(self.vlahovic.photo_url, "https://media.api-sports.io/football/players/1.png")

    def test_keeps_an_existing_photo(self):
        Player.objects.filter(pk=self.vlahovic.pk).update(photo_url="https://example.com/v.png")
        self.sync()
        self.vlahovic.refresh_from_db()
        self.assertEqual(self.vlahovic.photo_url, "https://example.com/v.png")

    def test_a_second_sync_updates_and_marks_who_left(self):
        self.sync()
        moved = {496: SQUADS[496][:2] + [squad_player(6, "N. Barella", "Midfielder", 29, 23)], 505: SQUADS[505][:2]}
        report = self.sync(squads=moved)
        self.assertEqual((report["created"], report["left"]), (0, 1))
        barella = Footballer.objects.get(api_id=6)
        self.assertEqual((barella.club_name, barella.age, barella.in_serie_a), ("Juventus", 29, True))
        self.assertFalse(Footballer.objects.get(api_id=3).in_serie_a)

    def test_an_interrupted_sync_makes_nobody_leave(self):
        self.sync()
        report = self.sync(squads={496: SQUADS[496], 505: []}, fail_on=505)
        self.assertIn("limite giornaliero", report["error"])
        self.assertEqual(report["left"], 0)
        self.assertEqual(Footballer.objects.filter(in_serie_a=True).count(), 6)

    def test_free_plan_recognises_clubs_from_the_listone(self):
        report = self.sync(teams_refused=True)
        self.assertEqual(report["error"], "")
        self.assertEqual(report["clubs"], 2)
        self.assertEqual(Footballer.objects.count(), 6)

    def test_without_key_nothing_is_called(self):
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": ""}):
            report = self.sync()
        self.assertIn("APIFOOTBALL_KEY", report["error"])
        self.assertEqual(self.calls, [])


class LinkingTests(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="L")
        mk = lambda api_id, name, pos, club: Footballer.objects.create(
            api_id=api_id, name=name, position=pos, club_name=club, club_api_id=1)
        mk(1, "M. Thuram", "A", "Inter")
        mk(2, "K. Thuram", "C", "Juventus")
        mk(3, "F. Esposito", "A", "Inter")
        mk(4, "S. Esposito", "A", "Inter")
        mk(5, "R. Leão", "A", "AC Milan")

    def player(self, name, role, team):
        return Player.objects.create(name=name, role=role, team=team, league=self.league)

    def test_same_club_and_surname(self):
        thuram = self.player("Thuram", "A", "Inter")
        footballers.link_players()
        thuram.refresh_from_db()
        self.assertEqual(thuram.footballer.api_id, 1)

    def test_doubtful_matches_stay_unlinked(self):
        esposito = self.player("Esposito", "A", "Inter")
        footballers.link_players()
        esposito.refresh_from_db()
        self.assertIsNone(esposito.footballer)

    def test_initials_choose_between_namesakes(self):
        esposito = self.player("Esposito F.P.", "A", "Inter")
        footballers.link_players()
        esposito.refresh_from_db()
        self.assertEqual(esposito.footballer.api_id, 3)

    def test_club_code_and_accents(self):
        leao = self.player("Leao", "A", "MIL")
        footballers.link_players()
        leao.refresh_from_db()
        self.assertEqual(leao.footballer.api_id, 5)

    def test_moved_club_with_a_unique_surname_and_role(self):
        # Il listone lo dà ancora al Milan, l'anagrafica già alla Juventus.
        thuram = self.player("Thuram K.", "C", "Milan")
        footballers.link_players()
        thuram.refresh_from_db()
        self.assertEqual(thuram.footballer.api_id, 2)

    def test_listone_import_links_new_players(self):
        sync_players([{"name": "Leao", "role": "A", "team": "Milan", "price": "30"}], league=self.league)
        self.assertEqual(Player.objects.get(league=self.league, name="Leao").footballer.api_id, 5)


class FootballersPageTests(TestCase):
    """La stessa anagrafica in console e nell'app, con accanto la propria lega."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("owner_fb", password="pw")
        self.stranger = User.objects.create_user("stranger_fb", password="pw")
        self.league = League.objects.create(name="Lega Calciatori", owner=self.owner)
        self.other = League.objects.create(name="Lega Altrui", owner=self.stranger)
        self.team = Participant.objects.create(display_name="Owner FC", league=self.league, user=self.owner,
                                               credits=Decimal("300"))
        rival = Participant.objects.create(display_name="Rivali FC", league=self.other, credits=Decimal("300"))
        self.lautaro = Footballer.objects.create(api_id=4, name="Lautaro Martínez", position="A",
                                                 club_name="Inter", club_api_id=505, age=28)
        self.barella = Footballer.objects.create(api_id=6, name="N. Barella", position="C",
                                                 club_name="Inter", club_api_id=505)
        Footballer.objects.create(api_id=7, name="A. Gone", position="D", club_name="Inter", in_serie_a=False)
        Player.objects.create(name="Martinez L.", role="A", team="Inter", league=self.league,
                              owner=self.team, cost=Decimal("45"), footballer=self.lautaro)
        Player.objects.create(name="Barella", role="C", team="Inter", league=self.other,
                              owner=rival, cost=Decimal("30"), footballer=self.barella)

    @staticmethod
    def _part(resp):
        html = resp.content.decode()
        part = html[html.index("<!-- footballers:start -->"):html.index("<!-- footballers:end -->")]
        return re.sub(r'name="(next|csrfmiddlewaretoken)" value="[^"]*"', "", part)

    def test_same_screen_in_console_and_app(self):
        self.client.force_login(self.owner)
        q = f"?league={self.league.id}"
        console = self.client.get(reverse("admin_footballers") + q)
        app = self.client.get(reverse("app_footballers") + q)
        self.assertEqual(console.status_code, 200)
        self.assertEqual(app.status_code, 200)
        self.assertEqual(self._part(console), self._part(app))

    def test_shows_who_owns_the_player_in_your_league_only(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("app_footballers"))
        self.assertContains(resp, "Lautaro Martínez")
        self.assertContains(resp, "Owner FC")
        self.assertContains(resp, "Non nel listone")  # Barella è di un'altra lega
        self.assertNotContains(resp, "Rivali FC")
        self.assertNotContains(resp, "A. Gone")

    def test_console_hides_leagues_you_do_not_manage(self):
        self.client.force_login(self.stranger)
        resp = self.client.get(reverse("admin_footballers") + f"?league={self.league.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Lautaro Martínez")
        self.assertNotContains(resp, "Owner FC")

    def test_filters(self):
        self.client.force_login(self.owner)
        url = reverse("app_footballers")
        self.assertNotContains(self.client.get(url + "?role=C"), "Lautaro Martínez")
        self.assertNotContains(self.client.get(url + "?lega=owned"), "N. Barella")
        self.assertNotContains(self.client.get(url + "?lega=out"), "Lautaro Martínez")
        self.assertContains(self.client.get(url + "?q=martinez l"), "Lautaro Martínez")  # nome del listone
        self.assertContains(self.client.get(url + "?usciti=1"), "A. Gone")

    def test_only_a_superuser_starts_the_sync(self):
        self.client.force_login(self.owner)
        with mock.patch.object(footballers, "start_sync") as start:
            self.client.post(reverse("admin_footballers_sync"))
        start.assert_not_called()

        root = User.objects.create_superuser("root_fb", password="pw")
        self.client.force_login(root)
        back = reverse("app_footballers")
        with mock.patch.dict(os.environ, {"APIFOOTBALL_KEY": "test"}), \
                mock.patch.object(footballers, "start_sync", return_value=True) as start:
            resp = self.client.post(reverse("admin_footballers_sync"), {"next": back})
        start.assert_called_once()
        self.assertRedirects(resp, back, fetch_redirect_response=False)
